"""模拟环境注册与运行时切换（spec §5.2）：注册表、可用性探测（超时 / 缓存）、切换流程（锁、409、旧连接关闭、
eNSP Pro 退出登录、缓存清空、并发串行）、设置持久化与重启恢复、沙箱列表（假 eNSP Pro）、题目按平台过滤、
考试模板平台不匹配被拦、Agent 快照跟着切换、按题目准备环境（切沙箱 + 启动设备）。全部假服务，不碰真实设备。"""
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
import main
from enspro import EnsproClient, EnsproProvider
from fake_enspro import FakeRest, sandbox_json


# ---------------- 注册表与探测 ----------------

def test_registry_has_builtin_envs():
    keys = [e.key for e in lab_lib.environments()]
    assert keys[:2] == ["ensp", "enspro"]
    assert lab_lib.get_env("ENSPRO").platform == "yunshan" and lab_lib.get_env("ensp").platform == "vrp5"
    assert lab_lib.get_env("enspro").sandboxes and not lab_lib.get_env("ensp").sandboxes
    assert lab_lib.env_of_platform("yunshan").key == "enspro"
    with pytest.raises(ValueError):
        lab_lib.get_env("nope")
    assert isinstance(lab_lib.make_provider("ensp"), lab_lib.EnspProvider)
    assert isinstance(lab_lib.make_provider("enspro", sandbox=3), EnsproProvider)
    assert lab_lib.make_provider("enspro", sandbox=3).sandbox_id == 3


def test_register_new_env_one_line(monkeypatch):
    """新增一种环境 = 实现 LabProvider + register 一行。"""
    class Dummy(lab_lib.LabProvider):
        kind, platform = "dummy", "vrp5"

    async def probe():
        return True, ""
    monkeypatch.setitem(lab_lib.REGISTRY, "dummy", lab_lib.LabEnv("dummy", "Dummy", "vrp5", lambda o: Dummy(), probe,
                                                                  order=9))
    assert isinstance(lab_lib.make_provider("dummy"), Dummy)
    assert [e.key for e in lab_lib.environments()][-1] == "dummy"
    assert asyncio.run(lab_lib.available("dummy", force=True))["available"] is True


def test_probe_timeout_and_cache(monkeypatch):
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(5)
        return True, ""
    monkeypatch.setitem(lab_lib.REGISTRY, "slow", lab_lib.LabEnv("slow", "Slow", "vrp5", lambda o: None, slow))
    monkeypatch.setattr(lab_lib, "PROBE_TIMEOUT", 0.2)
    lab_lib.clear_probe_cache()

    async def run():
        t0 = time.monotonic()
        r = await lab_lib.available("slow")
        dt = time.monotonic() - t0
        r2 = await lab_lib.available("slow")            # 缓存：不再探测
        r3 = await lab_lib.available("slow", force=True)
        return r, dt, r2, r3
    r, dt, r2, r3 = asyncio.run(run())
    assert r["available"] is False and "超时" in r["reason"] and dt < 1.0
    assert r2 == {**r2, "available": False} and len(calls) == 2 and r3["available"] is False


def test_probe_enspro_unreachable_and_ensp_not_windows(monkeypatch):
    monkeypatch.setattr(config, "ENSPRO_URL", "https://127.0.0.1:9")
    lab_lib.clear_probe_cache()
    r = asyncio.run(lab_lib.available("enspro", force=True))
    assert r["available"] is False and "127.0.0.1:9" in r["reason"]
    monkeypatch.setenv("AINET_ENSP_CLIENT", "0")
    r = asyncio.run(lab_lib.available("ensp", force=True))
    assert r["available"] is False and "eNSP 没有运行" in r["reason"]
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")
    assert asyncio.run(lab_lib.available("ensp", force=True))["available"] is True


def test_environments_parallel_within_3s(monkeypatch):
    async def slow():
        await asyncio.sleep(10)
        return True, ""
    for k in ("s1", "s2", "s3"):
        monkeypatch.setitem(lab_lib.REGISTRY, k, lab_lib.LabEnv(k, k, "vrp5", lambda o: None, slow, order=5))
    lab_lib.clear_probe_cache()
    mgr = labenv.LabManager()
    mgr.key = "ensp"
    mgr.provider = lab_lib.EnspProvider()
    mgr.pm = SimpleNamespace(active=None)
    mgr.exam = SimpleNamespace(exam=None)
    monkeypatch.setattr(db, "get_setting", lambda k: None)
    t0 = time.monotonic()
    out = asyncio.run(mgr.environments(force=True))
    assert time.monotonic() - t0 < 3.0          # 并行探测，各自 PROBE_TIMEOUT（2 秒）
    assert {e["key"]: e["available"] for e in out["environments"]}["s1"] is False


# ---------------- 切换流程（LabManager 单元） ----------------

class Prov(lab_lib.LabProvider):
    def __init__(self, kind, platform, log, sandbox=None, slow=0.0):
        self.kind, self.platform, self.log, self.sandbox_id, self.slow = kind, platform, log, sandbox, slow

    async def resolve(self):
        return {"state": "none", "message": "", "topology": None, "lab": self.info()}

    async def close(self):
        self.log.append(("close-begin", self.kind, self.sandbox_id))
        await asyncio.sleep(self.slow)
        self.log.append(("close-end", self.kind, self.sandbox_id))


def make_mgr(log, slow=0.0):
    mgr = labenv.LabManager(make=lambda k, **kw: (log.append(("make", k, kw.get("sandbox"))),
                                                  Prov(k, lab_lib.get_env(k).platform, log, kw.get("sandbox"), slow))[1])

    class Hub:
        default_sysname = "Huawei"

        async def reset_all(self, message="", default_sysname=None, **kw):
            log.append(("hub-reset", message.strip()))
            self.default_sysname = default_sysname
            return 0
    agent = SimpleNamespace(gens=[], on_env_switch=lambda d: agent.gens.append(d))
    mgr.bind(Hub(), SimpleNamespace(active=None, _op=asyncio.Lock(), lab=None),
             SimpleNamespace(exam=None, _lock=asyncio.Lock()), agent)
    return mgr


@pytest.fixture
def probes_ok(monkeypatch):
    async def ok():
        return True, ""
    for k in ("ensp", "enspro"):
        monkeypatch.setitem(lab_lib.REGISTRY, k, lab_lib.LabEnv(**{**lab_lib.REGISTRY[k].__dict__, "probe": ok}))
    lab_lib.clear_probe_cache()


def test_switch_flow_and_persistence(db_path, probes_ok, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    log: list = []

    async def run():
        mgr = make_mgr(log)
        mgr.boot()
        assert mgr.key == "ensp" and mgr.sandbox is None
        r = await mgr.switch("enspro", 3, sandbox_given=True)
        assert r["changed"] and r["key"] == "enspro" and r["sandbox"] == 3 and r["gen"] == 1
        # 顺序：断开 hub → 关闭旧后端（登出）→ 新建
        i_hub = log.index(next(x for x in log if x[0] == "hub-reset"))
        i_close = log.index(("close-end", "ensp", None))
        i_make = log.index(("make", "enspro", 3))
        assert i_hub < i_close < i_make
        assert "eNSP Pro" in log[i_hub][1] and mgr.hub.default_sysname == "HUAWEI"
        assert mgr.pm.lab is mgr.provider and mgr.agent.gens == ["eNSP Pro（沙箱 3）"]
        assert (await mgr.switch("enspro", 3, sandbox_given=True))["changed"] is False     # 同一环境同一沙箱：不动
        await mgr.switch("ensp")
        assert mgr.hub.default_sysname == "Huawei"
        return mgr
    asyncio.run(run())
    assert db.get_setting("lab:env") == "ensp" and db.get_setting("lab:sandbox:enspro") == "3"

    async def restart():
        mgr = make_mgr(log)
        mgr.boot()                                   # 重启恢复：库里的记录优先于环境变量
        assert mgr.key == "ensp"
        await mgr.switch("enspro")                   # 不给沙箱：沿用上次记的 3
        assert mgr.sandbox == 3
        mgr2 = make_mgr(log)
        mgr2.boot()
        assert (mgr2.key, mgr2.sandbox) == ("enspro", 3)
    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    asyncio.run(restart())


def test_env_defaults_when_no_record(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", 7)
    mgr = make_mgr([])
    mgr.boot()
    assert (mgr.key, mgr.sandbox) == ("enspro", 7)


def test_switch_refused_when_busy_or_offline(db_path, monkeypatch):
    db.init_db()

    async def off():
        return False, "连不上"
    monkeypatch.setitem(lab_lib.REGISTRY, "enspro", lab_lib.LabEnv(**{**lab_lib.REGISTRY["enspro"].__dict__,
                                                                      "probe": off}))
    lab_lib.clear_probe_cache()

    async def run():
        mgr = make_mgr([])
        mgr.boot()
        mgr.pm.active = SimpleNamespace(mode="practice")
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("enspro", 2, sandbox_given=True)
        assert e.value.status == 409 and "练习进行中" in e.value.detail
        mgr.pm.active = None
        mgr.exam.exam = {"id": 1}
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("enspro")
        assert e.value.status == 409 and "考试进行中" in e.value.detail
        mgr.exam.exam = None
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("enspro")
        assert e.value.status == 409 and "不可用" in e.value.detail and "连不上" in e.value.detail
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("bogus")
        assert e.value.status == 422
        assert mgr.key == "ensp" and not mgr.switching
    asyncio.run(run())


def test_concurrent_switches_are_serialized(db_path, probes_ok):
    db.init_db()
    log: list = []

    async def run():
        mgr = make_mgr(log, slow=0.1)
        mgr.boot()
        await asyncio.gather(mgr.switch("enspro", 2, sandbox_given=True), mgr.switch("enspro", 3, sandbox_given=True),
                             mgr.switch("ensp"))
        return mgr
    mgr = asyncio.run(run())
    # 每次切换的「关闭旧后端」都完整做完才轮到下一次（close-begin / close-end 成对相邻，不交错）
    seq = [x[0] for x in log if x[0].startswith("close")]
    assert seq == ["close-begin", "close-end"] * (len(seq) // 2) and len(seq) == 6
    assert mgr.key == "ensp" and mgr.gen == 3


def test_switch_waits_for_practice_start_lock(db_path, probes_ok):
    """练习正在开始（持有 PracticeManager._op）时，切换等它结束后再检查，看到练习进行中就 409。"""
    db.init_db()

    async def run():
        mgr = make_mgr([])
        mgr.boot()
        await mgr.pm._op.acquire()

        async def finish_start():
            await asyncio.sleep(0.1)
            mgr.pm.active = SimpleNamespace(mode="practice")
            mgr.pm._op.release()
        asyncio.ensure_future(finish_start())
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("enspro")
        assert e.value.status == 409
    asyncio.run(run())


# ---------------- ConsoleHub.reset_all ----------------

def test_hub_reset_all_closes_everything():
    from console_hub import ConsoleHub

    class Conn:
        closed = 0

        def __init__(self, port):
            self.q: asyncio.Queue = asyncio.Queue()

        async def connect(self):
            pass

        async def send(self, data):
            pass

        async def read(self):
            return await self.q.get()

        async def close(self):
            Conn.closed += 1
            self.q.put_nowait(b"")

    async def run():
        hub = ConsoleHub(Conn, min_reconnect=0)
        s1 = await hub.subscribe(2000)
        s2 = await hub.subscribe(2001)
        hub.pin([2000])
        n = await hub.reset_all("\r\n[切换]\r\n", default_sysname="HUAWEI")
        assert n == 2 and hub.channels == {} and hub.pinned == set() and hub.default_sysname == "HUAWEI"
        assert Conn.closed == 2
        for s in (s1, s2):
            items = []
            while not s.queue.empty():
                items.append(s.queue.get_nowait())
            assert items[-2:] == [b"\r\n[\xe5\x88\x87\xe6\x8d\xa2]\r\n", None]
        hub.unsubscribe(s1)                           # 旧订阅者退订不报错
    asyncio.run(run())


# ---------------- 接口（假 eNSP Pro） ----------------

@pytest.fixture
def app(db_path, monkeypatch, probes_ok):
    from fastapi.testclient import TestClient

    rest = FakeRest()
    rest.extra[3] = {**sandbox_json(3, {6: 0, 7: 0}), "name": "ainet_campus"}
    made: list = []

    def make(kind=None, **kw):
        kind = kind or config.LAB_PROVIDER
        if kind == "enspro":
            client = EnsproClient("http://enspro.test", transport=rest.transport())
            p = EnsproProvider(client=client, sandbox=kw.get("sandbox"), password=lambda: "x", cache_ttl=0)
        else:
            p = lab_lib.EnspProvider()
        made.append(p)
        return p
    monkeypatch.setattr(lab_lib, "make_provider", make)
    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        yield SimpleNamespace(c=c, rest=rest, made=made)
    main.SESSIONS.clear()


def login(c, who="admin", pw="admin"):
    assert c.post("/api/login", json={"login": who, "password": pw}).status_code == 200


def test_api_requires_login(app):
    c = app.c
    for m, u, b in [("get", "/api/lab/environments", None), ("post", "/api/lab/environment", {"key": "enspro"}),
                    ("get", "/api/lab/sandboxes", None), ("post", "/api/lab/prepare", {"scenario_id": "x"}),
                    ("get", "/api/lab/prepare", None)]:
        assert getattr(c, m)(u, **({"json": b} if b else {})).status_code == 401, u


def test_api_switch_any_logged_in_user(app):
    c, rest = app.c, app.rest
    c.post("/api/students", json={"login": "bob", "password": "pw", "name": "鲍勃"})
    login(c, "bob", "pw")                           # 单人使用：任何已登录用户都能切换
    env = c.get("/api/lab/environments").json()
    assert env["current"] == "ensp" and env["can_switch"] and env["locked"] == ""
    assert [(e["key"], e["available"]) for e in env["environments"]][:2] == [("ensp", True), ("enspro", True)]
    assert c.get("/api/lab/sandboxes").status_code == 409          # eNSP 经典版没有沙箱
    r = c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2})
    assert r.status_code == 200 and r.json()["changed"], r.text
    lab = c.get("/api/lab").json()
    assert lab["kind"] == "enspro" and lab["key"] == "enspro" and lab["sandbox"] == 2 and lab["gen"] == 1
    st = c.get("/api/topology/status").json()
    assert st["env"]["key"] == "enspro" and st["env"]["gen"] == 1 and st["path"] == "enspro://sandbox/2"
    assert main.LAB is c.app.state.labmgr.provider is c.app.state.practice.lab
    # 沙箱列表
    sb = c.get("/api/lab/sandboxes").json()
    assert [(s["id"], s["name"], s["devices"], s["running"], s["current"]) for s in sb["sandboxes"]] == \
        [(2, "ainet_test", 2, 2, True), (3, "ainet_campus", 2, 0, False)]
    assert any(p == "/service/ensp/sandbox/getByPage" and b.get("projectId") == 1 for _, p, b in rest.calls if b)
    # 同一环境的沙箱共享 REST 会话；切换选中沙箱不能把后台看门狗登出。
    out_before = rest.logouts
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 3}).status_code == 200
    assert rest.logouts == out_before
    cur = c.get("/api/topology/current").json()
    assert cur["topology"]["name"] == "ainet_campus" and cur["env"]["sandbox"] == 3
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 99}).status_code == 404
    # 切回 eNSP：eNSP Pro 的后台环境仍由独立上下文监测，服务关闭时统一登出。
    out_before = rest.logouts
    assert c.post("/api/lab/environment", json={"key": "ensp"}).status_code == 200
    assert rest.logouts == out_before
    assert db.get_setting("lab:env") == "ensp" and db.get_setting("lab:sandbox:enspro") == "3"


def test_api_switch_409_during_practice(app):
    c = app.c
    login(c)
    c.app.state.practice.active = SimpleNamespace(mode="practice", allowed_ports=set())
    try:
        r = c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2})
        assert r.status_code == 409 and "练习进行中" in r.json()["detail"]
        assert c.get("/api/lab/environments").json()["locked"]
        r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
        assert r.status_code == 409 and "练习进行中" in r.json()["detail"]
    finally:
        c.app.state.practice.active = None


def test_api_switch_closes_open_terminals(app, monkeypatch):
    """切换前已有的 hub 通道全部关闭，终端订阅者收到「模拟环境已切换」。"""
    c = app.c
    login(c)
    hub = c.app.state.hub
    from console_hub import Subscriber, _Channel
    ch = _Channel(2000)
    sub = Subscriber(2000)
    ch.subs.add(sub)
    hub.channels[2000] = ch
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    assert hub.channels == {}
    msgs = []
    while not sub.queue.empty():
        msgs.append(sub.queue.get_nowait())
    assert msgs[-1] is None and "模拟环境已切换为 eNSP Pro" in msgs[-2].decode()


def test_scenarios_filtered_by_platform_without_switching(app):
    c = app.c
    login(c)
    d = c.get("/api/scenarios").json()
    assert d["env"] == "ensp" and d["platform"] == "vrp5" and d["other_platform"] > 0
    assert d["scenarios"] and all(s["platform"] == "vrp5" for s in d["scenarios"])
    d = c.get("/api/scenarios?env=enspro").json()
    assert d["scenarios"] and all(s["platform"] == "yunshan" for s in d["scenarios"])
    assert d["current_env"] == "ensp" and c.get("/api/lab").json()["key"] == "ensp"        # 不触发切换
    assert c.get("/api/scenarios?env=nope").status_code == 422
    allp = {s["platform"] for s in c.get("/api/scenarios?all=1").json()["scenarios"]}
    assert allp == {"vrp5", "yunshan"}


def test_scenarios_empty_reason(app, monkeypatch):
    c = app.c
    login(c)
    pm = c.app.state.practice
    monkeypatch.setattr(pm, "scenarios", {k: v for k, v in pm.scenarios.items() if v.get("platform") != "yunshan"})
    d = c.get("/api/scenarios?env=enspro").json()
    assert d["scenarios"] == [] and "还没有题目" in d["empty_reason"]


def test_mixed_platform_exam_templates_are_rejected_and_legacy_templates_are_blocked(app):
    c = app.c
    login(c)
    cfg = {"questions": [{"scenario": "campus-ar0-ar2-link", "seed": 0}, {"scenario": "enspro-t1-t2-link", "seed": 0}]}
    created = c.post("/api/exam/templates", json={"name": "混合", "config": cfg})
    assert created.status_code == 422 and "同一设备平台" in created.json()["detail"]

    # 老数据库中可能已有混合平台模板；仍能在列表里说明兼容性，但绝不允许开考。
    tid = db.create_template("旧混合模板", json.dumps(cfg, ensure_ascii=False))
    t = next(x for x in c.get("/api/exam/templates").json()["templates"] if x["id"] == tid)
    assert t["platforms"] == ["vrp5", "yunshan"] and t["platform_ok"] is False
    assert [m["index"] for m in t["mismatch"]] == [1]
    r = c.post("/api/exam/start", json={"template_id": tid})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "第 2 题" in detail and "不匹配" in detail and "第 1 题" not in detail
    assert c.app.state.exam.exam is None

    # 云服务器环境能匹配 YunShan 模板；环境切换只改测试运行上下文，不触碰真实模拟器。
    switched = c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2})
    assert switched.status_code == 200, switched.text
    templates = c.get("/api/exam/templates").json()
    ys = next(x for x in templates["templates"] if x["name"] == "Campus 基础排障测验（YunShan）")
    assert templates["platform"] == "yunshan" and ys["platform_ok"] is True and ys["mismatch"] == []
    # 旧的混合模板依然会被拒绝，即使当前环境匹配其中一半。
    blocked = c.post("/api/exam/start", json={"template_id": tid})
    assert blocked.status_code == 409 and "第 1 题" in blocked.json()["detail"]


def test_agent_snapshot_follows_switch(app):
    c = app.c
    login(c)
    svc = c.app.state.agent
    svc.termlog.on_event("command", 2000, "display version")

    async def ctx():
        return (await svc._context())[1]["lab"]
    assert c.portal.call(ctx)["kind"] == "ensp"
    g0 = svc.env_gen
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    snap = c.portal.call(ctx)
    assert snap["kind"] == "enspro" and snap["platform"] == "yunshan"
    assert svc.env_gen == g0 + 1 and svc.termlog.recent() == []          # 旧环境的终端命令不再进快照

    async def tool():
        return await svc.run_tool("AR1", "display version", {"ar1": 6}, gen=g0)
    r = c.portal.call(tool)
    assert r["status"] == "rejected" and "已切换" in r["reason"]


def test_baseline_key_follows_sandbox(app):
    c = app.c
    login(c)
    pm = c.app.state.practice
    for sb, name in ((2, "ainet_test"), (3, "ainet_campus")):
        assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": sb}).status_code == 200
        cur = c.portal.call(pm.resolve)
        assert pm._baseline_key(cur, "AR1") == f"baseline:enspro:{name}:AR1"


def test_baseline_renames_legacy_ainet_test_device_keys(app, monkeypatch):
    from practice import Active

    pm = app.c.app.state.practice
    config_text = "sysname HUAWEI\nreturn\n"
    old = json.dumps({"config": config_text, "at": "2026-10-17T00:00:00+00:00"})
    settings = {"baseline:enspro:ainet_test:T1": old}
    writes = []
    monkeypatch.setattr(db, "get_setting", lambda key: settings.get(key))

    def save(key, value):
        settings[key] = value
        writes.append((key, value))
    monkeypatch.setattr(db, "set_setting", save)

    async def snapshot(_active, _device):
        return config_text
    monkeypatch.setattr(pm, "_snapshot", snapshot)
    active = Active(attempt_id=0, scenario={"required_devices": ["AR1"]}, student_id=0, started_at="",
                    ports={"AR1": 6}, allowed_ports=set())
    cur = {"lab": {"kind": "enspro"}, "topology": {"name": "ainet_test"}}
    result = app.c.portal.call(lambda: pm._check_baseline(active, cur, "AR1"))
    assert result == config_text
    assert settings["baseline:enspro:ainet_test:AR1"] == old
    assert writes == [("baseline:enspro:ainet_test:AR1", old)]


def test_prepare_switches_sandbox_and_starts_devices(app):
    c, rest = app.c, app.rest
    login(c)
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    assert r.status_code == 202, r.text
    assert r.json()["scenario"] == "enspro-t1-t2-link" and r.json()["done"] is False
    end = time.time() + 10
    job = None
    while time.time() < end:
        job = c.get("/api/lab/prepare").json()["job"]
        if job["done"]:
            break
        time.sleep(0.05)
    assert job["ok"], job
    assert job["sandbox"] == 2 and job["devices"] == {"AR1": "online", "AR2": "online"} and job["stage"] == "done"
    assert mgr.key == "enspro" and mgr.sandbox == 2
    assert any(p == "/service/ensp/device/batchStart" and sorted(b["deviceIds"]) == [6, 7]
               for _, p, b in rest.calls if b)
    # 再准备一次：已在目标沙箱、设备已运行，不再切换也不再启动
    gen = mgr.gen
    n_start = sum(p.endswith("batchStart") for _, p, _ in rest.calls)
    c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    while not c.get("/api/lab/prepare").json()["job"]["done"]:
        time.sleep(0.05)
    assert c.get("/api/lab/prepare").json()["job"]["ok"] and mgr.gen == gen
    assert sum(p.endswith("batchStart") for _, p, _ in rest.calls) == n_start


def test_prepare_errors(app, monkeypatch):
    c = app.c
    login(c)
    assert c.post("/api/lab/prepare", json={"scenario_id": "nope"}).status_code == 404

    async def off():
        return False, "连不上 192.168.56.101:8443"
    monkeypatch.setitem(lab_lib.REGISTRY, "enspro", lab_lib.LabEnv(**{**lab_lib.REGISTRY["enspro"].__dict__,
                                                                      "probe": off}))
    lab_lib.clear_probe_cache()
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    assert r.status_code == 409 and "离线" in r.json()["detail"]
    # eNSP 经典版的题：不能由 ainet 启动设备 → 任务失败并说明
    r = c.post("/api/lab/prepare", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 202
    while not c.get("/api/lab/prepare").json()["job"]["done"]:
        time.sleep(0.05)
    job = c.get("/api/lab/prepare").json()["job"]
    assert job["ok"] is False and job["error"] and job["error_status"] == 409


def test_prepare_restarts_stuck_device_once(app, monkeypatch):
    """设备启动后一直停在「启动中」：超过 prepare_stuck 秒停止再启动一次（eNSP Pro 真机偶发卡在状态 6）。"""
    c, rest = app.c, app.rest
    login(c)
    rest.status = {6: 0, 7: 0}
    rest.instant_start = False                       # batchStart 后一直是 6（启动系统）
    rest.reboot_405 = True                           # 同真机：重启退回「停止 → 启动」
    orig = rest.handle

    def handle(req):
        if req.url.path == "/service/ensp/device/batchStop":
            rest.instant_start = True                # 停过一次之后再启动就能起来
        return orig(req)
    rest.handle = handle
    mgr = c.app.state.labmgr
    # 卡住检测由启动看门狗负责（spec §10.5）：启动超过 start_timeout 秒仍在启动阶段 → 停止 → 启动一次
    mgr.prepare_poll = 0.05
    mgr.power.start_timeout, mgr.power.poll = 0.3, 0.05
    # 准备环境会从 eNSP 切到 eNSP Pro，切换时看门狗换成 eNSP Pro 的参数（库里没有记录 = config 缺省值，spec §5.2）
    monkeypatch.setattr(config, "ENSPRO_START_TIMEOUT", 0.3)
    for p in [mgr.provider]:
        p.stop_poll = 0.05
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    end = time.time() + 15
    while time.time() < end and not c.get("/api/lab/prepare").json()["job"]["done"]:
        time.sleep(0.05)
    job = c.get("/api/lab/prepare").json()["job"]
    assert job["ok"], job
    # 看门狗逐台处理卡住的设备：AR1、AR2 各停止 1 次、启动 2 次（第一次一起启动，重启时各自启动）
    def per_dev(path):
        n = {6: 0, 7: 0}
        for _, p, b in rest.calls:
            if p == path:
                for d in b["deviceIds"]:
                    n[d] += 1
        return n
    assert per_dev("/service/ensp/device/batchStop") == {6: 1, 7: 1}
    assert per_dev("/service/ensp/device/batchStart") == {6: 2, 7: 2}
    assert "/service/ensp/device/batchReboot" not in [p for _, p, _ in rest.calls]


def _wait_job(c, cond, timeout=10.0):
    end = time.time() + timeout
    job = None
    while time.time() < end:
        job = c.get("/api/lab/prepare").json()["job"]
        if cond(job):
            return job
        time.sleep(0.05)
    raise AssertionError(f"等待超时：{job}")


def test_prepare_view_steps_device_info_and_estimate(app):
    """课程页准备浮层用的字段（spec §16.3）：步骤条（跳过的标 skipped）、每台设备详情、进度、预计还需、环境名 / 沙箱名。"""
    c, rest = app.c, app.rest
    login(c)
    rest.status = {6: 0, 7: 0}
    rest.instant_start = False                       # batchStart 后停在 6（启动系统），手动改成运行中
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).json()
    # 开始时：conftest 关了就绪探测，没有「确认就绪」；全部 pending
    assert [(x["key"], x["label"], x["state"]) for x in r["steps"]] == [
        ("switch", "切换环境", "pending"), ("sandbox", "查找沙箱", "pending"), ("start", "启动设备", "pending"),
        ("wait", "等待运行", "pending"), ("done", "完成", "pending")]
    assert r["env_label"] == "eNSP Pro" and r["power"] is True and r["sandbox_name"] == "ainet_test"
    assert r["retries"] == mgr.power.retries and r["progress"] == 0.0 and r["estimate"] is None
    job = _wait_job(c, lambda j: j["stage"] == "wait")
    states = {x["key"]: x["state"] for x in job["steps"]}
    assert states == {"switch": "done", "sandbox": "done", "start": "done", "wait": "active", "done": "pending"}
    t1 = job["device_info"]["AR1"]
    assert t1["status"] == "starting" and t1["kind"] == "router" and t1["restarts"] == 0 and not t1["restarting"]
    assert t1["phase"] == "starting" and t1["round_s"] >= 0
    est = job["estimate"]
    # 两台 AR 一起冷启动：经验值 120 + 10 秒，减去已用的一点点
    assert 100 <= est["remaining_s"] <= 130 and est["lo_s"] < est["remaining_s"] < est["hi_s"] and est["max_s"] > 0
    assert 0.08 <= job["progress"] < 0.2
    rest.status[6] = rest.status[7] = 4
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"] and job["progress"] == 1.0 and job["estimate"] is None
    assert all(x["state"] == "done" for x in job["steps"])
    assert job["devices"] == {"AR1": "online", "AR2": "online"}
    # 再准备一次：已在目标沙箱、设备都在运行 → 切换环境 / 启动 / 等待都标为跳过
    c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    job = _wait_job(c, lambda j: j["done"])
    assert {x["key"]: x["state"] for x in job["steps"]} == {
        "switch": "skipped", "sandbox": "done", "start": "skipped", "wait": "skipped", "done": "done"}


def test_prepare_view_reports_restart_count(app, monkeypatch):
    """看门狗自动重启卡住的设备时：阶段 restart 的文字带第几次 / 最多几次，device_info 报 restarting 与 restarts。"""
    c, rest = app.c, app.rest
    login(c)
    rest.status = {6: 0, 7: 0}
    rest.instant_start = False
    orig = rest.handle

    def handle(req):
        if req.url.path == "/service/ensp/device/batchStop":
            rest.instant_start = True
        return orig(req)
    rest.handle = handle
    assert c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 2}).status_code == 200
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    mgr.power.start_timeout, mgr.power.poll, mgr.power.restart_gap = 0.3, 0.05, 0.5
    monkeypatch.setattr(config, "ENSPRO_START_TIMEOUT", 0.3)       # 切到 eNSP Pro 时按该环境的参数
    mgr.provider.stop_poll = 0.05
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    seen = []
    end = time.time() + 15
    while time.time() < end:
        job = c.get("/api/lab/prepare").json()["job"]
        seen.append(job)
        if job["done"]:
            break
        time.sleep(0.03)
    assert seen[-1]["ok"], seen[-1]
    rs = [j for j in seen if j["stage"] == "restart"]
    assert rs and f"（第 1 次，最多 {mgr.power.retries} 次）" in rs[0]["message"]
    assert any(j["steps"][3]["key"] == "wait" and j["steps"][3]["state"] == "active" for j in rs)   # restart 属于「等待运行」
    assert any(d["restarting"] and d["restarts"] == 1 for j in rs for d in j["device_info"].values())
    assert seen[-1]["device_info"]["AR1"]["restarts"] == 1 and not seen[-1]["device_info"]["AR1"]["restarting"]


def test_prepare_view_classic_and_error_step(app):
    """eNSP 经典版：步骤只有切换环境 → 核对设备 → 完成（不硬塞启动 / 沙箱）；失败时当前步骤标 error。"""
    c = app.c
    login(c)
    r = c.post("/api/lab/prepare", json={"scenario_id": "campus-ar0-ar2-link"}).json()
    assert [x["key"] for x in r["steps"]] == ["switch", "check", "done"] and r["power"] is False
    assert r["sandbox_name"] is None and r["retries"] == 0
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"] is False and job["stage"] == "error" and job["estimate"] is None
    assert {x["key"]: x["state"] for x in job["steps"]} == {"switch": "skipped", "check": "error", "done": "pending"}


def test_explicit_switch_refused_while_prepare_job_running(db_path, probes_ok):
    """按题目准备环境的任务进行中（等设备启动）时，显式切换返回 409，避免任务在被切走的环境上空等（真机 e2e 发现）。"""
    db.init_db()

    async def run():
        mgr = make_mgr([])
        mgr.boot()
        mgr.job = {"id": 1, "title": "AR1 与 AR2 互联不通", "message": "等待设备运行", "done": False}
        with pytest.raises(lab_lib.LabError) as e:
            await mgr.switch("enspro", 3, sandbox_given=True)
        assert e.value.status == 409 and "准备环境" in e.value.detail
        r = await mgr.switch("enspro", 3, sandbox_given=True, _job=True)      # 任务自己的切换照常
        assert r["changed"]
    asyncio.run(run())
