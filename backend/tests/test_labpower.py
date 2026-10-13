"""设备启动可靠性（spec §10.5）：启动看门狗（超时自动停止再启动、仍失败标 start_failed、错误状态、服务重启后接着跟踪）、
错开启动、停机间隔（根因对策）、就绪探测（initializing → online、busy 期间开练 409、探测走隐藏事务）、切换沙箱自动停掉
旧沙箱设备（及开关）、空闲自动停机（假时钟）、AINET_LAB_FORCE。全部假服务（FakeRest / 假设备），不碰真实设备。"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import config
import db
import lab as lab_lib
import labenv
import labpower
import scenarios as sc
from console_hub import ConsoleHub, HiddenResult
from enspro import EnsproClient, EnsproProvider
from fake_enspro import FakeRest, sandbox_json
from fake_vrp import FakeVRP


class FakeHub:
    def __init__(self):
        self.notes: list[str] = []
        self.disconnected: list[int] = []
        self.listeners = []
        self.subs = 0

    def add_listener(self, fn):
        self.listeners.append(fn)

    def notify_all(self, message):
        self.notes.append(message)
        return self.subs

    async def disconnect(self, port, message=""):
        self.disconnected.append(port)


def harness(rest: FakeRest, sandbox: int = 2, *, clock=time.time):
    """LabManager + EnsproProvider（FakeRest）+ 假 hub / 练习 / 考试；看门狗参数调成毫秒级。"""
    client = EnsproClient("http://enspro.test", transport=rest.transport())
    p = EnsproProvider(client=client, sandbox=sandbox, password=lambda: "x", cache_ttl=0)
    p.stop_poll = 0.02
    mgr = labenv.LabManager(make=lambda k, **kw: p)
    mgr.power = labpower.PowerWatch(mgr, clock=clock)
    mgr.idle = labpower.IdleStop(mgr)
    mgr.key, mgr.sandbox, mgr.provider = "enspro", sandbox, p
    p.watch = mgr.power
    hub = FakeHub()
    mgr.bind(hub, SimpleNamespace(active=None, _op=asyncio.Lock(), lab=None),
             SimpleNamespace(exam=None, _lock=asyncio.Lock()))
    w = mgr.power
    w.poll, w.start_timeout, w.stagger, w.restart_gap, w.probe_enabled = 0.02, 0.3, 0.0, 0.0, False
    w.early_check = 0.0
    return SimpleNamespace(mgr=mgr, p=p, w=w, hub=hub, rest=rest)


def per_dev(rest, path):
    n: dict[int, int] = {}
    for _, p, b in rest.calls:
        if p == path:
            for d in b["deviceIds"]:
                n[d] = n.get(d, 0) + 1
    return n


async def until(cond, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = cond()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return True
        await asyncio.sleep(step)
    return False


async def status_of(h, name="T1"):
    cur = await h.p.resolve()
    d = next(d for d in cur["topology"]["devices"] if d["name"] == name)
    return d["status"], cur


# ---------------- 看门狗 ----------------

def test_watchdog_restarts_stuck_device_then_ready(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    rest.stuck = {6: 1}                                # 第一次启动卡在 6，停止再启动后正常

    async def run():
        h = harness(rest)
        r = await h.w.start([6])
        assert r["started"] == [6]
        assert (await status_of(h))[0] == "starting"
        assert await until(lambda: any(e["event"] == "restart" for e in h.w.events))
        assert await until(lambda: 6 not in h.w.tracks, timeout=5)
        st, _ = await status_of(h)
        assert st == "online"
        ev = [e["event"] for e in h.w.events if e["device"] == "T1"]
        assert ev[:1] == ["restart"] and "ready" in ev and "failed" not in ev
        assert per_dev(rest, "/service/ensp/device/batchStop") == {6: 1}
        assert per_dev(rest, "/service/ensp/device/batchStart") == {6: 2}
        assert h.hub.disconnected == [6]               # 重启前断开了该设备的终端
        assert "batchReboot" not in json.dumps([p for _, p, _ in rest.calls])
        await h.w.close()
    asyncio.run(run())


def test_watchdog_marks_start_failed_after_retry_and_manual_start_restarts(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 4}
    rest.instant_start = True
    n = config.ENSPRO_START_RETRIES
    rest.stuck = {6: n + 1}                            # 每次自动重启后仍然卡住

    async def run():
        h = harness(rest)
        await h.w.start([6])
        assert await until(lambda: getattr(h.w.tracks.get(6), "phase", "") == "failed", timeout=5)
        st, cur = await status_of(h)
        assert st == "start_failed"
        reason = cur["start_errors"]["6"]
        assert "停在「启动系统」" in reason and f"已自动停止再启动 {n} 次" in reason
        assert "启动失败" in cur["message"] and "启动失败" in cur["progress"]["6"]
        # 开练前的就绪判断：给出启动失败的原因
        s = {"topology": "ainet_test", "required_devices": ["T1", "T2"], "platform": "yunshan", "hosts": []}
        rd = sc.readiness(s, cur)
        assert not rd["ready"] and "启动失败" in rd["reason"] and "T1" in rd["reason"]
        # 终端不放行，原因说明启动失败
        assert 6 not in h.p.allowed(cur) and "启动失败" in h.p.deny_reason(cur, 6)
        # 用户再点「启动」：先停止再启动（计数清零），这次正常
        n_stop = per_dev(rest, "/service/ensp/device/batchStop").get(6, 0)
        r = await h.w.start([6])
        assert r["restarted"] == [6]
        assert per_dev(rest, "/service/ensp/device/batchStop")[6] == n_stop + 1
        assert await until(lambda: 6 not in h.w.tracks, timeout=5)
        assert (await status_of(h))[0] == "online"
        await h.w.close()
    asyncio.run(run())


def test_watchdog_error_state_triggers_restart(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    rest.stuck, rest.stuck_code = {6: 1}, 5            # 状态 5：创建容器失败

    async def run():
        h = harness(rest)
        h.w.start_timeout = 60                           # 不靠超时：错误状态立即处理
        await h.w.start([6])
        assert await until(lambda: 6 not in h.w.tracks, timeout=5)
        restart = [e for e in h.w.events if e["event"] == "restart"]
        assert restart and "创建容器失败" in restart[0]["detail"]
        await h.w.close()
    asyncio.run(run())


def boot_log_zip(stuck: bool) -> bytes:
    """与真机 downloadLog 同形的日志包：zip 里有 op_*.log 与 vpnf_*.tar.gz（容器日志）。"""
    import io
    import tarfile
    import zipfile
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode="w:gz") as tf:
        files = {"vpnf/containers/8-x/TOR/log/service_failure_proc.log":
                 "[2026-10-07 00:34:02.893][0xc57][NOTICE][0][sfed]=== exit, service result(exit-code), exit code(exited),"
                 " exit status(17) ===\n" if stuck else "[2026-10-07 00:40:02.231][0xc67][NOTICE][0][sfed]Service status handler"
                 " oper type : start.\n"}
        if stuck:
            files["vpnf/containers/8-x/TOR/log/systemctl_reboot.log"] = \
                "ignore systemctl reboot command at 20261007003405, param: reboot --no-block\n"
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as zf:
        zf.writestr("op_8-x.log", "---create---\n")
        zf.writestr("vpnf_8-x.tar.gz", tb.getvalue())
    return zb.getvalue()


def test_boot_loop_reason_from_device_log():
    from enspro import boot_loop_reason
    why = boot_loop_reason(boot_log_zip(True))
    assert why and "sfed 退出码 17" in why and "复位循环" in why
    assert boot_loop_reason(boot_log_zip(False)) is None
    assert boot_loop_reason(b"not a zip") is None


def test_watchdog_early_check_restarts_before_timeout(db_path):
    """启动「启动系统」40 秒（测试里 0.1 秒）时查设备日志：已陷入板卡复位循环就不等 start_timeout 立即重启。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    rest.stuck = {6: 1}

    async def run():
        h = harness(rest)
        h.w.start_timeout, h.w.early_check = 60, 0.1
        logs = []

        async def boot_failure(dev):
            logs.append(dev)
            return "设备系统启动早期服务异常退出（sfed 退出码 17）、请求重启被容器忽略，陷入板卡复位循环" if rest.status[dev] == 6 else None
        h.p.boot_failure = boot_failure
        t0 = time.monotonic()
        await h.w.start([6])
        assert await until(lambda: 6 not in h.w.tracks, timeout=5)
        assert time.monotonic() - t0 < 5 and logs
        restart = [e for e in h.w.events if e["event"] == "restart"]
        assert restart and "复位循环" in restart[0]["detail"]
        assert per_dev(rest, "/service/ensp/device/batchStart") == {6: 2}
        await h.w.close()
    asyncio.run(run())


def test_staggered_start(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True

    async def run():
        h = harness(rest)
        h.w.stagger = 0.3
        r = await h.w.start([6, 7])
        assert r["started"] == [6] and r["queued"] == [7]
        st, cur = await status_of(h, "T2")
        assert st == "starting" and "排队" in cur["progress"]["7"]
        assert await until(lambda: len([t for t in rest.times if t[1].endswith("batchStart")]) == 2, timeout=3)
        starts = [t for t in rest.times if t[1].endswith("batchStart")]
        assert [b["deviceIds"] for _, _, b in starts] == [[6], [7]]
        assert starts[1][0] - starts[0][0] >= 0.28
        # stagger = 0：一次批量启动
        rest.status = {6: 0, 7: 0}
        h.w.forget([6, 7])
        h.w.stagger = 0
        rest.times.clear()
        await h.w.start([6, 7])
        assert [b["deviceIds"] for _, p, b in rest.times if p.endswith("batchStart")] == [[6, 7]]
        await h.w.close()
    asyncio.run(run())


def test_restart_gap_after_stop_and_reboot_is_stop_wait_start(db_path):
    """根因对策：停止后不到 restart_gap 秒的启动推迟发出；重启 = 停止 → 等间隔 → 启动（不用 batchReboot）。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 4, 7: 4}
    rest.instant_start = True

    async def run():
        h = harness(rest)
        h.w.restart_gap = 0.4
        await h.w.stop([6])
        t_stop = rest.times[-1][0]
        r = await h.w.start([6])
        assert r["started"] == [] and r["queued"] == [6]
        st, cur = await status_of(h)
        assert st == "starting" and "停机" in cur["progress"]["6"]
        assert await until(lambda: any(p.endswith("batchStart") for _, p, _ in rest.times), timeout=3)
        t_start = next(t for t, p, _ in rest.times if p.endswith("batchStart"))
        assert t_start - t_stop >= 0.38
        # 重启
        rest.times.clear()
        res = await h.w.reboot([7])
        assert res["via"] == "stop+start" and res["start_after_s"] == 0.4
        assert await until(lambda: any(p.endswith("batchStart") for _, p, _ in rest.times), timeout=3)
        (t1, p1, _), (t2, p2, _) = rest.times[:2]
        assert p1.endswith("batchStop") and p2.endswith("batchStart") and t2 - t1 >= 0.38
        assert not any(p.endswith("batchReboot") for _, p, _ in rest.calls)
        await h.w.close()
    asyncio.run(run())


def test_watchdog_resumes_after_service_restart(db_path):
    """服务重启：跟踪状态存在 app_settings，新进程接着跟踪（卡住的照样自动重启；排队中的照样发出）。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.stuck = {6: 1}
    rest.instant_start = True

    async def first():
        h = harness(rest)
        h.w.start_timeout = 60
        h.w.restart_gap = 30                           # 7 排在停机间隔之后，服务「重启」前还没发出
        h.w.note_stopped(2, [7])
        await h.w.start([6, 7])
        assert h.w.tracks[7].phase == "queued"
        await h.w.close()                              # 服务退出
    asyncio.run(first())
    saved = json.loads(db.get_setting(labpower.SETTING_POWER))
    assert {t["name"]: t["phase"] for t in saved["tracks"]} == {"T1": "starting", "T2": "queued"}
    # 把记录里的时间往前拨：T1 已启动 200 秒、T2 的停机间隔已过
    for t in saved["tracks"]:
        t["since"] -= 200
        t["not_before"] -= 200
    db.set_setting(labpower.SETTING_POWER, json.dumps(saved))

    async def second():
        h = harness(rest)
        h.w.start_timeout = 150
        assert h.w.resume() == 2
        st, cur = await status_of(h, "T1")
        assert st == "starting"
        assert await until(lambda: not h.w.tracks, timeout=5)
        assert per_dev(rest, "/service/ensp/device/batchStop") == {6: 1}            # T1 超时 → 停止再启动
        assert per_dev(rest, "/service/ensp/device/batchStart") == {6: 2, 7: 1}     # T2 排队的启动补发
        assert (await status_of(h, "T1"))[0] == "online" and (await status_of(h, "T2"))[0] == "online"
        await h.w.close()
    asyncio.run(second())
    # 记录只认当前环境 / 沙箱：换了沙箱不恢复
    db.set_setting(labpower.SETTING_POWER, json.dumps({**saved, "sandbox": 3}))

    async def third():
        h = harness(rest)
        assert h.w.resume() == 0 and not h.w.tracks
    asyncio.run(third())


def test_device_stopped_elsewhere_is_forgotten(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}

    async def run():
        h = harness(rest)
        h.w.stop_grace = 0.1
        h.w.start_timeout = 60
        await h.w.start([6])
        rest.status[6] = 0                             # 在 eNSP Pro 网页上停掉了
        assert await until(lambda: 6 not in h.w.tracks, timeout=3)
        assert (await status_of(h))[0] == "stopped"
        await h.w.close()
    asyncio.run(run())


# ---------------- 就绪探测 ----------------

def test_probe_initializing_until_not_busy(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    answers = [(False, "设备忙：Error: The system is busy in building configuration."), (None, "学员正在输入命令"),
               (True, "")]
    calls = []

    async def prober(dev):
        calls.append(dev)
        return answers.pop(0) if answers else (True, "")

    async def run():
        h = harness(rest)
        h.w.prober, h.w.probe_enabled, h.w.probe_interval = prober, True, 0.05
        await h.w.start([6])
        assert await until(lambda: getattr(h.w.tracks.get(6), "phase", "") == "initializing", timeout=3)
        st, cur = await status_of(h)
        assert st == "initializing" and cur["running"] == 0 and "初始化" in cur["message"]
        assert 6 in h.p.allowed(cur)                   # 初始化中可以开终端看启动过程
        s = {"topology": "ainet_test", "required_devices": ["T1"], "platform": "yunshan", "hosts": []}
        rd = sc.readiness(s, cur)
        assert not rd["ready"] and "initializing" in rd["reason"] and "busy" in rd["reason"]
        assert await until(lambda: 6 not in h.w.tracks, timeout=3)
        assert len(calls) == 3 and (await status_of(h))[0] == "online"
        await h.w.close()
    asyncio.run(run())


def test_probe_timeout_counts_as_stuck(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True

    async def busy(dev):
        return False, "设备忙"

    async def run():
        h = harness(rest)
        h.w.prober, h.w.probe_enabled, h.w.probe_interval, h.w.probe_timeout = busy, True, 0.02, 0.2
        await h.w.start([6])
        assert await until(lambda: getattr(h.w.tracks.get(6), "phase", "") == "failed", timeout=5)
        assert per_dev(rest, "/service/ensp/device/batchStop") == {6: config.ENSPRO_START_RETRIES}
        assert "仍未就绪" in h.w.tracks[6].reason and "设备忙" in h.w.tracks[6].reason
        await h.w.close()
    asyncio.run(run())


class BusyYunShan(FakeVRP):
    """刚冷启动的 YunShan（2026-10-07 T2 实测）：构建配置期间读配置报 busy，之后正常。"""

    def __init__(self):
        super().__init__(host="HUAWEI", page=50)
        self.busy = True

    def _execute(self, cmd):
        if cmd == "display current-configuration | include sysname":
            self.executed.append(cmd)
            out = ("Error: The system is busy in building configuration. Please wait for a moment..." if self.busy
                   else "sysname HUAWEI")
            self.emit("\r\n" + out + "\r\n" + self.prompt)
            return
        super()._execute(cmd)


def test_console_probe_is_hidden_and_detects_busy():
    """真正的探测走 ConsoleHub 隐藏事务：学员终端看不到、不产生命令记录；busy → False，不 busy → True。"""
    dev = BusyYunShan()

    async def run():
        hub = ConsoleHub(dev.factory, min_reconnect=0, ready_timeout=1.0, settle=0.05, default_sysname="HUAWEI")
        events = []
        hub.add_listener(lambda e, p, d: events.append((e, d)))
        sub = await hub.subscribe(6)
        await asyncio.sleep(0.2)
        while not sub.queue.empty():
            sub.queue.get_nowait()
        mgr = SimpleNamespace(hub=hub)
        w = labpower.PowerWatch(mgr)
        ok, note = await w._probe_console(6)
        assert ok is False and "busy" in note
        dev.busy = False
        ok, note = await w._probe_console(6)
        assert ok is True
        seen = b""
        while not sub.queue.empty():
            seen += sub.queue.get_nowait() or b""
        assert b"display" not in seen and b"busy" not in seen
        assert not [e for e in events if e[0] in ("command", "output")]
        assert dev.view is None and dev.executed.count("display current-configuration | include sysname") == 2
        await hub.close()
    asyncio.run(run())


# ---------------- 接口：开练 409、准备环境 probe 阶段、旧沙箱停机 ----------------

@pytest.fixture
def app(db_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    async def ok():
        return True, ""
    for k in ("ensp", "enspro"):
        monkeypatch.setitem(lab_lib.REGISTRY, k, lab_lib.LabEnv(**{**lab_lib.REGISTRY[k].__dict__, "probe": ok}))
    lab_lib.clear_probe_cache()
    rest = FakeRest()
    rest.extra[3] = {**sandbox_json(3, {6: 0, 7: 0}), "name": "ainet_campus"}

    def make(kind=None, **kw):
        kind = kind or config.LAB_PROVIDER
        if kind == "enspro":
            client = EnsproClient("http://enspro.test", transport=rest.transport())
            return EnsproProvider(client=client, sandbox=kw.get("sandbox"), password=lambda: "x", cache_ttl=0)
        return lab_lib.EnspProvider()
    monkeypatch.setattr(lab_lib, "make_provider", make)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200
        yield SimpleNamespace(c=c, rest=rest)
    main.SESSIONS.clear()


def test_practice_start_409_while_initializing_and_prepare_probe_stage(app):
    c, rest = app.c, app.rest
    mgr = c.app.state.labmgr
    gate = {"ready": False}

    async def prober(dev):
        return (True, "") if gate["ready"] else (False, "设备忙：Error: The system is busy in building configuration.")
    w = mgr.power
    w.prober, w.probe_enabled, w.probe_interval, w.poll = prober, True, 0.05, 0.05
    mgr.prepare_poll = 0.05
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    assert r.status_code == 202
    end = time.time() + 5
    job = None
    while time.time() < end:
        job = c.get("/api/lab/prepare").json()["job"]
        if job["stage"] == "probe":
            break
        time.sleep(0.05)
    assert job["stage"] == "probe" and job["devices"] == {"T1": "initializing", "T2": "initializing"}, job
    assert "busy" in job["message"]
    topo = c.get("/api/topology/current").json()
    assert {d["name"]: d["status"] for d in topo["topology"]["devices"]} == {"T1": "initializing", "T2": "initializing"}
    r = c.post("/api/practice/start", json={"scenario_id": "enspro-t1-t2-link"})
    assert r.status_code == 409 and "initializing" in r.json()["detail"] and "busy" in r.json()["detail"]
    gate["ready"] = True
    end = time.time() + 5
    while time.time() < end and not c.get("/api/lab/prepare").json()["job"]["done"]:
        time.sleep(0.05)
    job = c.get("/api/lab/prepare").json()["job"]
    assert job["ok"] and job["devices"] == {"T1": "online", "T2": "online"} and job["stopped_old"] == []


def test_switch_sandbox_stops_old_devices(app, monkeypatch):
    c, rest = app.c, app.rest
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    rest.status = {6: 4, 7: 4}
    r = c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 3})
    assert r.status_code == 200, r.text
    st = r.json()["stopped_old"]
    assert st["ok"] and st["sandbox"] == 2 and st["sandbox_name"] == "ainet_test" and st["devices"] == ["T1", "T2"]
    assert rest.status == {6: 0, 7: 0}
    stop_calls = [b for _, p, b in rest.calls if p.endswith("batchStop")]
    assert stop_calls == [{"deviceIds": [6, 7], "sandboxId": 2}]
    assert c.get("/api/lab").json()["last_stopped_old"]["devices"] == ["T1", "T2"]
    # 停机记下了时刻：切回去立刻启动也要遵守停机间隔
    assert (2, 6) in c.app.state.labmgr.power.stopped_at
    # 开关关掉：不停
    monkeypatch.setattr(config, "ENSPRO_STOP_OLD", False)
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    rest.status = {6: 4, 7: 4}
    r = c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 3})
    assert r.json()["stopped_old"] is None and rest.status == {6: 4, 7: 4}


def test_prepare_reports_stopped_old_devices(app):
    c, rest = app.c, app.rest
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    rest.extra[3] = {**sandbox_json(3, {6: 4, 7: 0}), "name": "ainet_campus"}   # 沙箱 3 的 T1 在跑
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    stops = []
    orig = rest.handle

    def handle(req):
        if req.url.path.endswith("batchStop"):
            body = json.loads(req.content)
            stops.append(body)
            if body["sandboxId"] == 3:
                rest.extra[3] = {**sandbox_json(3, {6: 0, 7: 0}), "name": "ainet_campus"}
            return rest._ok(None)
        return orig(req)
    rest.handle = handle                               # 之后新建的后端（切到沙箱 3）用这个处理函数
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 3}).status_code == 200
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    end = time.time() + 5
    while time.time() < end and not c.get("/api/lab/prepare").json()["job"]["done"]:
        time.sleep(0.05)
    job = c.get("/api/lab/prepare").json()["job"]
    assert job["ok"], job
    assert job["stopped_old"] == [{"ok": True, "sandbox": 3, "sandbox_name": "ainet_campus", "devices": ["T1"]}]
    assert stops == [{"deviceIds": [6], "sandboxId": 3}]


def test_power_api_goes_through_watchdog(app):
    c, rest = app.c, app.rest
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    mgr = c.app.state.labmgr
    rest.status = {6: 0, 7: 0}
    r = c.post("/api/lab/devices/start", json={"devices": [6, 7]})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["action"] == "start"
    assert set(mgr.power.tracks) == {6, 7}
    r = c.post("/api/lab/devices/stop", json={"devices": [6]})
    assert r.status_code == 200 and 6 not in mgr.power.tracks
    r = c.post("/api/lab/devices/reboot", json={"devices": [7]})
    assert r.status_code == 200 and r.json()["via"] == "stop+start"
    info = c.get("/api/lab").json()
    assert "watch" in info and "idle_stop" in info and info["source"] in ("db", "env-default", "env")


# ---------------- 空闲自动停机（假时钟） ----------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def idle_harness(rest, *, minutes=1.0, warn=10.0):
    clk = Clock()
    h = harness(rest)
    h.mgr.idle = labpower.IdleStop(h.mgr, clock=clk)
    h.mgr.idle.minutes, h.mgr.idle.warn = minutes, warn
    h.hub.listeners = [h.mgr.idle.on_hub]
    return h, clk


def test_idle_stop_warns_then_stops(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 4, 7: 4}

    async def run():
        h, clk = idle_harness(rest)
        h.hub.subs = 1
        idle = h.mgr.idle
        clk.t += 45
        assert await idle.check() is None and not h.hub.notes
        clk.t += 6                                      # 51 秒：进入预告窗口（提前 10 秒）
        assert await idle.check() is None
        assert len(h.hub.notes) == 1 and "自动停止" in h.hub.notes[0] and "T1" in h.hub.notes[0]
        assert await idle.check() is None and len(h.hub.notes) == 1     # 只预告一次
        clk.t += 10                                     # 61 秒：停
        res = await idle.check()
        assert res and res["devices"] == ["T1", "T2"] and rest.status == {6: 0, 7: 0}
        assert len(h.hub.notes) == 2 and "已自动停止" in h.hub.notes[1]
        assert sorted(h.hub.disconnected) == [6, 7]
        assert (2, 6) in h.w.stopped_at                 # 经看门狗停机：记下停机时刻
        clk.t += 120                                    # 都停了：不再动作
        assert await idle.check() is None
    asyncio.run(run())


def test_idle_stop_skips_practice_and_activity_resets(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 4, 7: 4}

    async def run():
        h, clk = idle_harness(rest)
        idle = h.mgr.idle
        h.mgr.pm.active = SimpleNamespace(mode="practice")
        clk.t += 600
        assert await idle.check() is None and rest.status == {6: 4, 7: 4}
        h.mgr.pm.active = None
        clk.t += 50                                     # 练习一结束重新计时：50 秒 < 60 秒
        assert await idle.check() is None and rest.status == {6: 4, 7: 4}
        # 终端活动（hub 事件）重新计时；异步日志不算
        for fn in h.hub.listeners:
            fn("input", 6, "")
        clk.t += 55
        assert await idle.check() is None
        for fn in h.hub.listeners:
            fn("log", 6, "Oct  7 2026 ... %%01IFNET/4/LINK_STATE")
        clk.t += 6
        assert await idle.check() is not None and rest.status == {6: 0, 7: 0}
        # Agent 命令 / 启停也算活动
        rest.status = {6: 4, 7: 4}
        h.mgr.touch("agent")
        clk.t += 59
        assert await idle.check() is None
        # 设备在启动 / 初始化中：不停
        await h.w.start([6])
        clk.t += 600
        assert await idle.check() is None and rest.status[7] == 4
        await h.w.close()
    asyncio.run(run())


def test_idle_stop_disabled_with_zero(db_path):
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 4, 7: 4}

    async def run():
        h, clk = idle_harness(rest, minutes=0)
        clk.t += 10 ** 6
        assert await h.mgr.idle.check() is None and rest.status == {6: 4, 7: 4}
    asyncio.run(run())


# ---------------- AINET_LAB_FORCE ----------------

def test_lab_force_env_overrides_db(db_path, monkeypatch, caplog):
    db.init_db()
    db.set_setting(labenv.SETTING_ENV, "ensp")
    db.set_setting(labenv.SETTING_SANDBOX.format("enspro"), "2")
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", 3)
    made = []
    make = lambda k, **kw: (made.append((k, kw.get("sandbox"))), lab_lib.EnspProvider())[1]   # noqa: E731
    caplog.set_level("INFO", logger="ainet.labenv")
    # 不强制：库里的记录优先
    mgr = labenv.LabManager(make=make)
    mgr.boot()
    assert (mgr.key, mgr.source) == ("ensp", "db") and "数据库记录" in caplog.text
    # 强制：以环境变量为准，并写回库
    monkeypatch.setattr(config, "LAB_FORCE", True)
    caplog.clear()
    mgr = labenv.LabManager(make=make)
    mgr.boot()
    assert (mgr.key, mgr.sandbox, mgr.source) == ("enspro", 3, "env") and made[-1] == ("enspro", 3)
    assert db.get_setting(labenv.SETTING_ENV) == "enspro" and db.get_setting("lab:sandbox:enspro") == "3"
    assert "环境变量" in caplog.text and "AINET_LAB_FORCE" in caplog.text
    # 强制但没设沙箱：沿用库里的沙箱
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", None)
    mgr = labenv.LabManager(make=make)
    mgr.boot()
    assert (mgr.key, mgr.sandbox) == ("enspro", 3)
