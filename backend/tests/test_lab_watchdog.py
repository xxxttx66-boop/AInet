"""启动看门狗参数按环境配置（spec §5.2「接入设置」/ §10.5，里程碑 6.16）：参数校验与范围、优先级（库里的记录 /
AINET_LAB_FORCE / 环境变量 / 内置缺省）、修改后对当前环境的 PowerWatch 立即生效（进行中的启动按新值继续）、非当前环境只记库、
切换环境时换参数、409 / 422 / 400、准备环境的等待上限与自动重启上限、超时误判为 start_failed 的设备事后恢复为 online、接口。
全部假服务（FakeRest / 假 provider），不碰真实设备。"""
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
from fake_enspro import FakeRest
from test_labpower import harness, per_dev, status_of, until


# ---------------- 校验与优先级 ----------------

def _p(name):
    return next(p for p in lab_lib.get_env("enspro").watch if p.name == name)


@pytest.mark.parametrize("name,raw,want", [
    ("start_timeout", 300, 300.0), ("start_timeout", "60", 60.0), ("start_timeout", 1800, 1800.0),
    ("start_retries", 0, 0), ("start_retries", "10", 10), ("start_retries", 3.0, 3),
    ("early_check", 0, 0.0), ("early_check", 40, 40.0), ("probe_timeout", 30, 30.0),
])
def test_normalize_watch_ok(name, raw, want):
    v = lab_lib.normalize_watch(_p(name), raw)
    assert v == want and type(v) is type(want)


@pytest.mark.parametrize("name,raw,why", [
    ("start_timeout", 59, "60~1800"), ("start_timeout", 1801, "60~1800"), ("start_timeout", "abc", "数字"),
    ("start_timeout", True, "数字"), ("start_timeout", float("nan"), "数字"), ("start_retries", 11, "0~10"),
    ("start_retries", -1, "0~10"), ("start_retries", 2.5, "整数"), ("early_check", 5, "0 = 关闭"),
    ("probe_timeout", 10, "30~1800"),
])
def test_normalize_watch_rejects(name, raw, why):
    with pytest.raises(ValueError) as ei:
        lab_lib.normalize_watch(_p(name), raw)
    assert why in str(ei.value)


def test_only_power_envs_have_watchdog():
    assert lab_lib.watch_view("ensp") is None and lab_lib.watch_values("ensp") == {}
    v = lab_lib.watch_view("enspro")
    assert [x["name"] for x in v["params"]] == ["start_timeout", "start_retries", "early_check", "probe_timeout",
                                               "max_devices"]   # max_devices：里程碑 6.17
    st = v["params"][0]
    assert st["env_var"] == "AINET_ENSPRO_START_TIMEOUT" and st["builtin"] == 150 and (st["min"], st["max"]) == (60, 1800)


def test_watch_sources_and_precedence(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "ENSPRO_START_TIMEOUT", 150.0)
    monkeypatch.delenv("AINET_ENSPRO_START_TIMEOUT", raising=False)
    assert lab_lib.watch_value("enspro", "start_timeout") == 150 and lab_lib.watch_source("enspro", "start_timeout") == "default"
    # 环境变量（config 在导入时已读入；这里只看来源判断）
    monkeypatch.setenv("AINET_ENSPRO_START_TIMEOUT", "300")
    monkeypatch.setattr(config, "ENSPRO_START_TIMEOUT", 300.0)
    assert lab_lib.watch_value("enspro", "start_timeout") == 300 and lab_lib.watch_source("enspro", "start_timeout") == "env"
    # 库里的记录优先；不合法的项忽略
    db.set_setting("lab:watch:enspro", json.dumps({"start_timeout": 240, "start_retries": 99}))
    lab_lib.load_watch()
    assert lab_lib.watch_value("enspro", "start_timeout") == 240 and lab_lib.watch_source("enspro", "start_timeout") == "db"
    assert lab_lib.watch_source("enspro", "start_retries") in ("env", "default")
    v = {x["name"]: x for x in lab_lib.watch_view("enspro")["params"]}
    assert v["start_timeout"]["value"] == 240 and v["start_timeout"]["default"] == 300
    # 记录整个坏掉：全部回到缺省
    db.set_setting("lab:watch:enspro", "{not json")
    lab_lib.load_watch()
    assert lab_lib.watch_source("enspro", "start_timeout") == "env"


def test_lab_force_env_wins_and_writes_back(db_path, monkeypatch):
    db.init_db()
    db.set_setting("lab:watch:enspro", json.dumps({"start_timeout": 240, "start_retries": 5}))
    monkeypatch.setenv("AINET_ENSPRO_START_TIMEOUT", "300")
    monkeypatch.setattr(config, "ENSPRO_START_TIMEOUT", 300.0)
    lab_lib.load_watch(force=True)
    assert lab_lib.watch_value("enspro", "start_timeout") == 300 and lab_lib.watch_source("enspro", "start_timeout") == "force"
    assert lab_lib.watch_value("enspro", "start_retries") == 5 and lab_lib.watch_source("enspro", "start_retries") == "db"
    assert json.loads(db.get_setting("lab:watch:enspro")) == {"start_timeout": 300.0, "start_retries": 5}


# ---------------- LabManager：修改、生效、409 / 422 ----------------

class Prov(lab_lib.LabProvider):
    def __init__(self, kind, sandbox=None):
        self.kind, self.sandbox_id = kind, sandbox
        self.platform = lab_lib.get_env(kind).platform
        self.power = lab_lib.get_env(kind).power

    async def resolve(self):
        return {"state": "none", "message": "", "topology": None, "lab": self.info()}


def make_mgr():
    mgr = labenv.LabManager(make=lambda k, **kw: Prov(k, kw.get("sandbox")))

    class Hub:
        async def reset_all(self, message="", default_sysname=None, **kw):
            return 0
    mgr.bind(Hub(), SimpleNamespace(active=None, _op=asyncio.Lock(), lab=None),
             SimpleNamespace(exam=None, _lock=asyncio.Lock()), SimpleNamespace(on_env_switch=lambda d: None))
    mgr._stop_old = lambda old: asyncio.sleep(0)
    return mgr


def test_boot_applies_db_params_and_set_takes_effect_immediately(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", 2)
    db.set_setting("lab:watch:enspro", json.dumps({"start_timeout": 300}))

    async def run():
        mgr = make_mgr()
        mgr.boot()
        w = mgr.power
        assert (w.start_timeout, w.retries, w.configured_for) == (300, config.ENSPRO_START_RETRIES, "enspro")
        r = await mgr.set_watch("enspro", {"start_timeout": 420, "start_retries": 5})
        assert r["changed"] and r["applied"] and (w.start_timeout, w.retries) == (420, 5)
        src = {x["name"]: x["source"] for x in r["watchdog"]["params"]}
        assert src["start_timeout"] == "db" and src["start_retries"] == "db"
        assert json.loads(db.get_setting("lab:watch:enspro")) == {"start_timeout": 420.0, "start_retries": 5}
        # 同样的值再存：没有变化
        assert (await mgr.set_watch("enspro", {"start_timeout": 420}))["changed"] is False
        # null = 删掉记录，回到缺省
        r = await mgr.set_watch("enspro", {"start_timeout": None})
        assert w.start_timeout == config.ENSPRO_START_TIMEOUT
        assert {x["name"]: x["source"] for x in r["watchdog"]["params"]}["start_timeout"] in ("env", "default")
        assert json.loads(db.get_setting("lab:watch:enspro")) == {"start_retries": 5}
        r = await mgr.set_watch("enspro", {"start_retries": None})
        assert db.get_setting("lab:watch:enspro") is None and w.retries == config.ENSPRO_START_RETRIES
        # 环境列表 / 接入设置都带 watchdog；经典版为 None
        info = mgr.endpoint_info("enspro")
        assert info["watchdog"]["values"]["start_timeout"] == config.ENSPRO_START_TIMEOUT
        assert mgr.endpoint_info("ensp")["watchdog"] is None
    asyncio.run(run())


def test_set_watch_refused_when_busy_or_invalid(db_path):
    db.init_db()

    async def run():
        mgr = make_mgr()
        mgr.boot()                                   # 当前环境 eNSP（经典版）
        for vals, why in [({"start_timeout": 30}, "60~1800"), ({"start_retries": 11}, "0~10"),
                          ({"nope": 1}, "没有看门狗参数"), ({"start_timeout": 100, "early_check": 100}, "要小于启动超时")]:
            with pytest.raises(lab_lib.LabError) as ei:
                await mgr.set_watch("enspro", vals)
            assert ei.value.status == 422 and why in ei.value.detail, (vals, ei.value.detail)
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_watch("ensp", {"start_timeout": 300})
        assert ei.value.status == 400 and "没有启动看门狗" in ei.value.detail
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_watch("nope", {"start_timeout": 300})
        assert ei.value.status == 422
        mgr.pm.active = SimpleNamespace(mode="practice")
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_watch("enspro", {"start_timeout": 300})
        assert ei.value.status == 409 and "练习进行中" in ei.value.detail and "看门狗参数" in ei.value.detail
        assert mgr.watch_info("enspro")["can_edit"] is False
        mgr.pm.active = None
        mgr.exam.exam = object()
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_watch("enspro", {"start_timeout": 300})
        assert ei.value.status == 409 and "考试进行中" in ei.value.detail
        mgr.exam.exam = None
        mgr.job = {"done": False, "title": "某题", "message": "等待设备"}
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_watch("enspro", {"start_timeout": 300})
        assert ei.value.status == 409 and "准备环境" in ei.value.detail
    asyncio.run(run())
    assert db.get_setting("lab:watch:enspro") is None


def test_non_current_env_only_saved_then_applied_on_switch(db_path, monkeypatch):
    db.init_db()

    async def run():
        mgr = make_mgr()
        mgr.boot()                                   # 当前 eNSP：看门狗还是 config 缺省
        w = mgr.power
        assert w.configured_for is None
        r = await mgr.set_watch("enspro", {"start_timeout": 300, "start_retries": 2})
        assert r["applied"] is False and w.start_timeout == config.ENSPRO_START_TIMEOUT
        # 准备任务的「最多 N 次」在切换前就按目标环境的配置
        assert mgr._retries_of(lab_lib.get_env("enspro")) == 2
        monkeypatch.setattr(lab_lib, "available", lambda k, force=False: asyncio.sleep(0, {"available": True}))
        await mgr.switch("enspro", 2, sandbox_given=True)
        classic = w
        w = mgr.power
        assert w is not classic
        assert (w.start_timeout, w.retries, w.configured_for) == (300, 2, "enspro")
        # 各沙箱有独立运行上下文；修改一个计时器不能改变另一个。
        w.start_timeout = 0.3
        await mgr._replace(lab_lib.get_env("enspro"), 3)
        other = mgr.power
        assert other is not w and other.start_timeout == 300 and w.start_timeout == 0.3
        await mgr._replace(lab_lib.get_env("ensp"), None)
        assert mgr.power.start_timeout == config.ENSPRO_START_TIMEOUT
        await mgr.set_watch("enspro", {"start_timeout": 360})
        assert w.start_timeout == other.start_timeout == 360
        assert mgr.power.start_timeout == config.ENSPRO_START_TIMEOUT
        await mgr.close()
    asyncio.run(run())


def test_prepare_deadline_follows_env_params(db_path):
    db.init_db()
    mgr = make_mgr()
    w = mgr.power
    w.configure({"start_timeout": 150, "start_retries": 3, "probe_timeout": 300})
    w.probe_enabled, w.stagger = True, 5.0
    short = mgr._prepare_deadline(2)
    w.configure({"start_timeout": 300})
    long = mgr._prepare_deadline(2)
    assert long - short == 150 * 4                   # 每轮（启动超时 + 60）×（重试 + 1）
    assert long == 5 + (300 + 60) * 4 + 300 + 60


# ---------------- 进行中的看门狗轮次按新值继续 ----------------

def test_running_round_uses_new_timeout(db_path):
    """设备在「启动系统」：启动超时从 0.3 秒调到 60 秒后，这一轮不再被判卡住；调回 0.3 秒立即按卡住处理。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 4}                       # instant_start=False：启动后一直是 6

    async def run():
        h = harness(rest)
        h.w.start_timeout = 60.0
        await h.w.start([6])
        await asyncio.sleep(0.5)
        assert h.w.tracks[6].phase == "starting" and not h.w.events
        h.w.configure({"start_timeout": 0.3, "start_retries": 0})
        assert await until(lambda: h.w.tracks.get(6) is not None and h.w.tracks[6].phase == "failed")
        assert [e["event"] for e in h.w.events] == ["failed"]       # 重试次数改成 0：不自动重启
        await h.w.close()
    asyncio.run(run())


# ---------------- 第 4 项：超时误判的 start_failed 事后恢复 ----------------

def test_start_failed_device_recovers_when_it_comes_online(db_path):
    """慢设备被超时判为 start_failed（最后一轮不停设备），之后接口报运行中 → 就绪探测通过 → online，start_errors 清掉。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 4}

    async def run():
        h = harness(rest)
        probes = []

        async def prober(dev):
            probes.append(dev)
            return True, ""
        h.w.prober, h.w.probe_enabled, h.w.probe_interval = prober, True, 0.0
        h.w.retries = 0
        await h.w.start([6])
        assert await until(lambda: getattr(h.w.tracks.get(6), "phase", "") == "failed")
        st, cur = await status_of(h)
        assert st == "start_failed" and "6" in cur["start_errors"]
        assert per_dev(rest, "/service/ensp/device/batchStop") == {}       # 没停：设备接着启动
        rest.status[6] = 4                                                  # 设备随后自行启动完成
        assert await until(lambda: 6 not in h.w.tracks)
        st, cur = await status_of(h)
        assert st == "online" and cur["start_errors"] == {} and probes == [6]
        ev = [(e["event"], e["detail"]) for e in h.w.events]
        assert [x for x, _ in ev] == ["failed", "initializing", "ready"]
        assert "此前判为启动失败" in ev[1][1] and "已恢复" in ev[2][1]
        await h.w.close()
    asyncio.run(run())


def test_start_on_failed_but_online_device_does_not_restart(db_path):
    """用户对已恢复运行（接口报运行中）的 start_failed 设备点「启动」：不再停止重启，交给看门狗确认就绪。"""
    db.init_db()
    rest = FakeRest()
    rest.status = {6: 0, 7: 4}

    async def run():
        h = harness(rest)
        h.w.retries = 0
        await h.w.start([6])
        assert await until(lambda: getattr(h.w.tracks.get(6), "phase", "") == "failed")
        await h.w.close()                            # 停掉看门狗循环，模拟「接口刚报运行中、看门狗还没轮到」
        h.w._closed = False
        rest.status[6] = 4
        r = await h.w.start([6])
        assert r["recovering"] == [6] and r["restarted"] == [] and r["started"] == []
        assert per_dev(rest, "/service/ensp/device/batchStop") == {}
        assert await until(lambda: 6 not in h.w.tracks)
        assert (await status_of(h))[0] == "online"
        await h.w.close()
    asyncio.run(run())


# ---------------- 接口 ----------------

@pytest.fixture
def app(db_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        yield c
    main.SESSIONS.clear()


def test_api_watchdog(app):
    import main
    c = app
    for m, b in [("get", None), ("post", {"start_timeout": 300})]:
        kw = {"json": b} if b is not None else {}
        assert getattr(c, m)("/api/lab/environments/enspro/watchdog", **kw).status_code == 401
    assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200
    d = c.get("/api/lab/environments/enspro/watchdog").json()
    assert d["can_edit"] is True and d["current"] is False
    assert {x["name"]: x["source"] for x in d["watchdog"]["params"]}["start_timeout"] == "default"
    assert c.get("/api/lab/environments/ensp/watchdog").status_code == 400
    envs = {e["key"]: e for e in c.get("/api/lab/environments").json()["environments"]}
    assert envs["ensp"]["watchdog"] is None and envs["enspro"]["watchdog"]["values"]["start_retries"] == 3
    assert c.post("/api/lab/environments/enspro/watchdog", json={}).status_code == 422
    r = c.post("/api/lab/environments/enspro/watchdog", json={"start_timeout": 30})
    assert r.status_code == 422 and "60~1800" in r.json()["detail"]
    assert c.post("/api/lab/environments/enspro/watchdog", json={"start_retries": 2.5}).status_code == 422
    r = c.post("/api/lab/environments/enspro/watchdog", json={"start_timeout": 300, "start_retries": 4})
    assert r.status_code == 200 and r.json()["watchdog"]["values"]["start_timeout"] == 300
    assert json.loads(db.get_setting("lab:watch:enspro")) == {"start_timeout": 300.0, "start_retries": 4}
    r = c.post("/api/lab/environments/enspro/watchdog", json={"start_timeout": None})
    assert r.status_code == 200 and json.loads(db.get_setting("lab:watch:enspro")) == {"start_retries": 4}
    main.LABMGR.pm.active = SimpleNamespace(mode="practice")
    try:
        r = c.post("/api/lab/environments/enspro/watchdog", json={"start_timeout": 600})
        assert r.status_code == 409 and "练习进行中" in r.json()["detail"]
    finally:
        main.LABMGR.pm.active = None
