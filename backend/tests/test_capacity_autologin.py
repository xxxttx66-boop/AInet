"""单人部署（里程碑 6.17）：自动登录（AINET_AUTO_LOGIN）与按环境配置的「同时运行设备上限」（lab:watch:<环境> 的
max_devices）：计数口径（PC 不计）、参数校验与来源、课程页隐藏超上限的题、按 id 开始 409、准备环境时先停非本题设备。
全部假服务（FakeRest），不碰真实设备。"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

import config
import db
import lab as lab_lib
import main
import scenarios as sc
from test_labenv import _wait_job, app, login, probes_ok  # noqa: F401

# 本机上限 3 台时要隐藏的题（4 台非 PC 设备）：NAT 2 道 + OSPF 3 道；campus-pc1-gateway-ys 有 4 台但 PC1 不计
OVER3 = {"campus-ar2-easyip-acl-ys", "campus-ar2-nat-static-ys", "ospf-r1-shutdown-ys", "ospf-r2-hello-mismatch-ys",
         "ospf-r3-area-mismatch-ys"}


# ---------------- 计数口径 ----------------

def _load(sid):
    loaded, _ = sc.load_dir(config.SCENARIOS_DIR)
    return loaded[sid]


def test_device_load_counts_non_pc_devices():
    assert sc.device_load(_load("campus-pc1-gateway-ys")) == 3          # hosts 里的 PC1 不计
    assert sc.device_load(_load("ospf-r1-shutdown-ys")) == 4            # R1~R3 按名字算路由器
    assert sc.device_load(_load("enspro-t1-t2-link")) == 2
    s = {"id": "x", "topology": "sb", "required_devices": ["A", "Client1", "Cloud1", "B"]}
    assert sc.device_load(s) == 2                                       # 名字是 PC 类 / Cloud 的不计
    topo = {"name": "sb", "devices": [{"name": "A", "kind": "router"}, {"name": "B", "kind": "pc"},
                                      {"name": "Client1", "kind": "pc"}, {"name": "Cloud1", "kind": "cloud"}]}
    assert sc.device_load(s, topo) == 1                                 # 有本题拓扑时以设备类型为准
    assert sc.device_load(s, {"name": "sb", "devices": [{"name": "A", "kind": "router"}]}) is None   # 判断不了
    assert sc.device_load(s, {"name": "other", "devices": []}) == 2     # 不是本题拓扑：按题目信息


def _p():
    return next(p for p in lab_lib.get_env("enspro").watch if p.name == "max_devices")


def test_max_devices_param_validation_and_sources(db_path, monkeypatch):
    p = _p()
    assert lab_lib.normalize_watch(p, 0) == 0 and lab_lib.normalize_watch(p, "64") == 64
    assert type(lab_lib.normalize_watch(p, 3.0)) is int
    for bad in (65, -1, 1.5, "abc", True):
        with pytest.raises(ValueError):
            lab_lib.normalize_watch(p, bad)
    assert lab_lib.max_devices("enspro") == 0 and lab_lib.max_devices("ensp") == 0
    assert lab_lib.watch_source("enspro", "max_devices") == "default"
    monkeypatch.setenv("AINET_ENSPRO_MAX_DEVICES", "3")
    monkeypatch.setattr(config, "ENSPRO_MAX_DEVICES", 3)
    assert lab_lib.max_devices("enspro") == 3 and lab_lib.watch_source("enspro", "max_devices") == "env"
    db.init_db()
    db.set_setting("lab:watch:enspro", '{"max_devices": 5}')
    lab_lib.load_watch()
    assert lab_lib.max_devices("enspro") == 5 and lab_lib.watch_source("enspro", "max_devices") == "db"
    lab_lib.load_watch(force=True)                 # AINET_LAB_FORCE=1：环境变量优先并写回库
    assert lab_lib.max_devices("enspro") == 3 and lab_lib.watch_source("enspro", "max_devices") == "force"
    assert '"max_devices": 3' in db.get_setting("lab:watch:enspro")


# ---------------- 课程页过滤与 409 ----------------

def _set_max(c, n):
    r = c.post("/api/lab/environments/enspro/watchdog", json={"max_devices": n})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_scenarios_hides_over_capacity(app):
    c = app.c
    login(c)
    d = c.get("/api/scenarios?env=enspro").json()
    assert d["max_devices"] == 0 and d["capacity_hidden"] == 0
    all_ids = {s["id"] for s in d["scenarios"]}
    assert OVER3 <= all_ids
    w = _set_max(c, 3)
    assert w["watchdog"]["values"]["max_devices"] == 3 and w["applied"] is False
    d = c.get("/api/scenarios?env=enspro").json()
    ids = {s["id"] for s in d["scenarios"]}
    assert all_ids - ids == OVER3, all_ids - ids
    assert "campus-pc1-gateway-ys" in ids
    assert d["max_devices"] == 3 and d["capacity_hidden"] == 5 and d["drafts_hidden"] == 0
    assert d["capacity_hidden_chapters"].get("L05") == 3 and sum(d["capacity_hidden_chapters"].values()) == 5
    # 经典版环境没有这个参数；?all=1（操作页找工单）不过滤
    assert c.get("/api/scenarios?env=ensp").json()["capacity_hidden"] == 0
    assert OVER3 <= {s["id"] for s in c.get("/api/scenarios?all=1").json()["scenarios"]}
    # 进行中的那道照列
    pm = c.app.state.practice
    pm.active = SimpleNamespace(scenario={"id": "ospf-r1-shutdown-ys"}, mode="practice")
    try:
        ids = {s["id"] for s in c.get("/api/scenarios?env=enspro").json()["scenarios"]}
        assert "ospf-r1-shutdown-ys" in ids and "ospf-r2-hello-mismatch-ys" not in ids
    finally:
        pm.active = None
    _set_max(c, 0)
    assert c.get("/api/scenarios?env=enspro").json()["capacity_hidden"] == 0


def test_start_over_capacity_is_409(app):
    c = app.c
    login(c)
    _set_max(c, 3)
    for url, body in (("/api/practice/start", {"scenario_id": "ospf-r1-shutdown-ys"}),
                      ("/api/exam/start_single", {"scenario_id": "campus-ar2-nat-static-ys"}),
                      ("/api/lab/prepare", {"scenario_id": "ospf-r3-area-mismatch-ys"})):
        r = c.post(url, json=body)
        assert r.status_code == 409, (url, r.text)
        detail = r.json()["detail"]
        assert "超过本机上限 3 台" in detail and "4 台" in detail and "busy" not in detail, detail
    cfg = {"questions": [{"scenario": "enspro-t1-t2-link", "seed": 0}, {"scenario": "ospf-r1-shutdown-ys", "seed": 0}]}
    tid = c.post("/api/exam/templates", json={"name": "含超上限题", "config": cfg}).json()["id"]
    r = c.post("/api/exam/start", json={"template_id": tid})
    assert r.status_code == 409 and "超过本机上限" in r.json()["detail"]
    assert c.app.state.exam.exam is None and c.app.state.labmgr.job is None


def test_prepare_stops_extra_devices_over_limit(app):
    c, rest = app.c, app.rest
    login(c)
    rest.more = ((8, "T3"),)
    rest.pc = True
    rest.status = {6: 0, 7: 0, 8: 4, 12: 4}          # 非本题的 T3 与 PC1 在运行
    rest.instant_start = True
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    _set_max(c, 2)
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"], job
    assert job["stopped_extra"] == ["T3"]
    paths = [(p, b) for _, p, b in rest.calls if p.endswith(("batchStop", "batchStart"))]
    i_stop = next(i for i, (p, b) in enumerate(paths) if p.endswith("batchStop") and b["deviceIds"] == [8])
    i_start = next(i for i, (p, b) in enumerate(paths) if p.endswith("batchStart"))
    assert i_stop < i_start
    assert rest.status[8] == 0 and rest.status[12] == 4          # PC 不计、不停
    assert sorted(next(b for p, b in paths if p.endswith("batchStart"))["deviceIds"]) == [6, 7]


def test_prepare_keeps_extra_devices_within_limit(app):
    c, rest = app.c, app.rest
    login(c)
    rest.more = ((8, "T3"),)
    rest.status = {6: 0, 7: 0, 8: 4}
    rest.instant_start = True
    c.app.state.labmgr.prepare_poll = 0.05
    _set_max(c, 3)                                   # 2 + 1 = 3，不超
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"] and job["stopped_extra"] == []
    assert not any(p.endswith("batchStop") for _, p, _ in rest.calls)
    assert rest.status[8] == 4


def test_capacity_uses_sandbox_device_kinds(app):
    """见过本题沙箱后按 deviceType 计数（T1 / T2 是 ENSP-AR）；本题设备不在沙箱里 = 判断不了，不隐藏、只记一次日志。"""
    c = app.c
    login(c)
    _set_max(c, 1)
    mgr = c.app.state.labmgr
    s = c.app.state.practice.scenarios["enspro-t1-t2-link"]
    assert mgr.capacity(s) == {"limit": 1, "count": 2, "over": True}
    mgr.note_topology({"topology": {"name": "ainet_test", "devices": [{"name": "T1", "kind": "router"}]}}, "enspro")
    assert mgr.capacity(s)["over"] is False and mgr.capacity(s)["count"] is None
    assert "enspro-t1-t2-link" in {x["id"] for x in c.get("/api/scenarios?env=enspro").json()["scenarios"]}


# ---------------- 跨沙箱计数（真机发现：启动时没有「上一个沙箱」，stop_old 不触发） ----------------

def _campus_running(rest):
    """另一个沙箱 ainet_campus（id 3）里 4 台设备 + 1 台 PC 在运行（deviceId 与沙箱 2 不重叠）。"""
    from fake_enspro import PC_DEVICE, sandbox_json
    devs = [(20, "R1"), (21, "R2"), (22, "R3"), (23, "LSW0")]
    sb = sandbox_json(3, {d: 4 for d, _ in devs}, more=tuple(devs))
    sb["devices"] = [d for d in sb["devices"] if d["id"] >= 20] + [
        {**PC_DEVICE, "id": 24, "name": "PC9", "sandboxId": 3, "status": 4}]
    rest.extra[3] = {**sb, "name": "ainet_campus"}


def test_prepare_stops_devices_in_other_sandboxes_over_limit(app):
    c, rest = app.c, app.rest
    login(c)
    _campus_running(rest)
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    _set_max(c, 3)
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"], job
    assert job["stopped_other"] == [{"sandbox": 3, "sandbox_name": "ainet_campus", "devices": ["R1", "R2", "R3", "LSW0"]}]
    assert job["stopped_extra"] == []
    ops = [(p, b) for _, p, b in rest.calls if p.endswith(("batchStop", "batchStart"))]
    i_stop = next(i for i, (p, b) in enumerate(ops) if p.endswith("batchStop") and b["sandboxId"] == 3)
    assert sorted(ops[i_stop][1]["deviceIds"]) == [20, 21, 22, 23]                    # PC9 不停
    assert i_stop < next(i for i, (p, b) in enumerate(ops) if p.endswith("batchStart"))
    st = {d["id"]: d["status"] for d in rest.extra[3]["devices"]}
    assert st == {20: 0, 21: 0, 22: 0, 23: 0, 24: 4}


def test_prepare_other_sandboxes_untouched_without_limit_or_within(app):
    c, rest = app.c, app.rest
    login(c)
    _campus_running(rest)
    rest.instant_start = True
    c.app.state.labmgr.prepare_poll = 0.05
    for n in (0, 6):                                  # 0 = 不限（只按 stop_old 规则）；6 >= 2 + 4
        _set_max(c, n)
        rest.status = {6: 0, 7: 0}
        assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"}).status_code == 202
        job = _wait_job(c, lambda j: j["done"])
        assert job["ok"] and job["stopped_other"] == [], (n, job)
        assert not any(p.endswith("batchStop") for _, p, _ in rest.calls), n


# ---------------- 自动登录 ----------------

@pytest.fixture
def auto(db_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    def run(login_name):
        monkeypatch.setattr(config, "AUTO_LOGIN", login_name)
        main.SESSIONS.clear()
        caplog.set_level(logging.INFO, logger="ainet")
        return TestClient(main.app)
    yield run
    main.SESSIONS.clear()
    main.AUTO.update(login="", sid=None, token=None)


def test_auto_login_creates_student_and_issues_session(auto, caplog):
    with auto("kiosk") as c:
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "courses"
        assert c.get("/courses", follow_redirects=False).status_code == 200
        me = c.get("/api/me").json()
        assert me["auto_login"] is True and me["student"]["login"] == "kiosk" and me["student"]["name"] == "kiosk"
        token = c.cookies.get(config.SESSION_COOKIE)
        assert token and main.SESSIONS.get(token) == me["student"]["id"]
        # 登出后下一次请求又自动登录（前端已隐藏登出）
        assert c.post("/api/logout").status_code == 200
        c.cookies.clear()
        assert c.get("/api/me").status_code == 200
        # 已有会话（另一个学员显式登录）照常使用
        assert c.post("/api/students", json={"login": "bob", "name": "鲍勃", "password": "pw"}).status_code == 201
        assert c.post("/api/login", json={"login": "bob", "password": "pw"}).status_code == 200
        assert c.get("/api/me").json()["student"]["login"] == "bob"
    conn = db.connect()
    try:
        pw = conn.execute("SELECT password_plain FROM students WHERE login = 'kiosk'").fetchone()[0]
    finally:
        conn.close()
    assert pw and len(pw) >= 16
    logs = caplog.text
    assert "自动登录已开启：kiosk" in logs and pw not in logs
    assert main.current_student_id({}) == main.AUTO["sid"]       # WebSocket 等没有 cookie 的请求也认


def test_auto_login_existing_admin_keeps_password(auto):
    with auto("admin") as c:
        assert c.get("/api/me").json()["student"]["login"] == "admin"
    assert db.verify_password("admin", "admin") is not None


def test_auto_login_off_by_default(auto):
    with auto("") as c:
        assert c.get("/api/me").status_code == 401
        assert c.get("/login", follow_redirects=False).status_code == 200
        assert c.get("/courses", follow_redirects=False).status_code == 303
        assert config.SESSION_COOKIE not in c.cookies
    assert main.current_student_id({}) is None
