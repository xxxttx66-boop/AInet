"""用户添加的环境实例（里程碑 6.19，课程页环境列「+」）：添加 / 改名 / 设密码 / 删除、持久化与重启加载、按实例的设备
console 密码（ENSPRO_DEVICE_PASSWORD__<KEY>，只写不读、原子写 secrets 文件且保留其他行）、题目按平台对所有实例可见、
准备环境在用户添加的实例上走通、按实例的设备上限、删除时的 409 与清理。

全部假服务：eNSP Pro 接口是 FakeRest（每个实例一个），在线探测只连本机回环上的临时监听端口或 127.0.0.1:9（不可用），
不碰真机、不连任何远程主机；密码都是假的，并断言它不出现在任何接口返回与日志里。"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from types import SimpleNamespace

import pytest

import ai
import config
import db
import lab as lab_lib
import labenv
import main
from enspro import EnsproClient, EnsproProvider
from fake_enspro import FakeRest
from test_capacity_autologin import OVER3
from test_labenv import _wait_job, login, probes_ok  # noqa: F401

FAKE_PW = "Fake-Inst-Pw#2024"
FAKE_PW2 = "Other-Fake#9"
DEEPSEEK = "DEEPSEEK_API_KEY=sk-keep-this-line-1234"


@pytest.fixture(autouse=True)
def _fast_scheme_detect(monkeypatch):
    """listener 只 listen 不 accept：http:// 地址的自动识别 https 会等 TLS / 明文 HTTP 超时，测试里缩短。"""
    monkeypatch.setattr(lab_lib, "SCHEME_TIMEOUT", 0.15)


@pytest.fixture
def listener():
    """本机回环上的临时监听端口（探测只做 TCP 连接，http 不做 TLS）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield f"http://127.0.0.1:{s.getsockname()[1]}"
    s.close()


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    p = tmp_path / "secrets.env"
    p.write_text(f"# ainet secrets\n{DEEPSEEK}\n", encoding="utf-8")
    monkeypatch.setenv("AINET_SECRETS_FILE", str(p))
    return p


@pytest.fixture
def app(db_path, monkeypatch, probes_ok, secrets_file):  # noqa: F811
    from fastapi.testclient import TestClient

    rests: dict[str, FakeRest] = {}
    made: list = []

    def make(kind=None, **kw):
        kind = kind or config.LAB_PROVIDER
        env = lab_lib.get_env(kind)
        if env.type == "enspro":                     # 按环境类型建后端：每个 eNSP Pro 实例一个假服务
            rest = rests.setdefault(kind, FakeRest())
            client = EnsproClient(f"http://{kind}.test", transport=rest.transport())
            p = EnsproProvider(client=client, sandbox=kw.get("sandbox"), password=lambda: "x", cache_ttl=0, env_key=kind)
        else:
            p = lab_lib.EnspProvider()
        made.append(p)
        return p
    monkeypatch.setattr(lab_lib, "make_provider", make)
    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        login(c)
        yield SimpleNamespace(c=c, rests=rests, made=made, secrets=secrets_file)
    main.SESSIONS.clear()


def _add(c, url, label="机房 B", **kw):
    r = c.post("/api/lab/environments", json={"type": "enspro", "label": label, "url": url, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _envs(c, refresh=True):
    return {e["key"]: e for e in c.get(f"/api/lab/environments{'?refresh=1' if refresh else ''}").json()["environments"]}


# ---------------- 按实例的设备密码 ----------------

def test_device_password_per_instance(monkeypatch, secrets_file):
    monkeypatch.setenv("ENSPRO_DEVICE_PASSWORD", "global-fake")
    assert lab_lib.device_secret_name("enspro-2") == "ENSPRO_DEVICE_PASSWORD__ENSPRO_2"
    assert lab_lib.device_password("enspro") == "global-fake"      # 内置：回落到 ENSPRO_DEVICE_PASSWORD
    assert lab_lib.device_password("enspro-2") is None             # 用户添加的实例：不回落
    monkeypatch.setenv("ENSPRO_DEVICE_PASSWORD__ENSPRO_2", "inst-fake")
    assert lab_lib.device_password("enspro-2") == "inst-fake" and lab_lib.device_password_source("enspro-2") == "env"
    ai.write_key_file(str(secrets_file), "builtin-inst-fake", "ENSPRO_DEVICE_PASSWORD__ENSPRO")
    assert lab_lib.device_password("enspro") == "builtin-inst-fake"   # 实例专属优先于全局
    assert lab_lib.device_password_source("enspro") == "file"
    assert DEEPSEEK in secrets_file.read_text()                         # 写一行不影响其他行
    # 后端用自己实例的密码；连接器的报错里写实例的密码名
    env = lab_lib.register_custom("enspro-2", "enspro", "机房 B")
    p = env.make({"client": EnsproClient("http://x.test"), "sandbox": 1})
    assert isinstance(p, EnsproProvider) and p.env_key == "enspro-2" and p.kind == "enspro"
    assert p.password() == "inst-fake" and p.secret_name == "ENSPRO_DEVICE_PASSWORD__ENSPRO_2" and p.label() == "机房 B"
    assert EnsproProvider(client=EnsproClient("http://x.test")).secret_name == "ENSPRO_DEVICE_PASSWORD"
    # secrets 视图只报有无，键名按实例
    assert lab_lib.secrets_view(env) == {"ENSPRO_DEVICE_PASSWORD__ENSPRO_2": True}
    assert lab_lib.secrets_view(lab_lib.get_env("enspro")) == {"ENSPRO_DEVICE_PASSWORD": True}
    monkeypatch.delenv("ENSPRO_DEVICE_PASSWORD__ENSPRO_2")
    assert lab_lib.secrets_view(env) == {"ENSPRO_DEVICE_PASSWORD__ENSPRO_2": False}


def test_probe_missing_secret_names_instance_key(listener, secrets_file):
    lab_lib.register_custom("enspro-2", "enspro", "机房 B")
    lab_lib.set_endpoint_override("enspro-2", listener)
    r = asyncio.run(lab_lib.available("enspro-2", force=True))
    assert r["available"] is False and r["reason_code"] == "missing_secret"
    assert "ENSPRO_DEVICE_PASSWORD__ENSPRO_2" in r["reason"]
    ai.write_key_file(str(secrets_file), FAKE_PW, "ENSPRO_DEVICE_PASSWORD__ENSPRO_2")
    assert asyncio.run(lab_lib.available("enspro-2", force=True))["available"] is True
    # 添加前的测试连接不查密码
    assert asyncio.run(lab_lib.probe_type("enspro", listener))["available"] is True
    assert asyncio.run(lab_lib.probe_type("enspro", "https://127.0.0.1:9"))["reason_code"] == "unreachable"


def test_load_custom_envs_ignores_bad_records(db_path):
    db.init_db()
    db.set_setting("lab:envs", json.dumps([
        {"key": "enspro-2", "type": "enspro", "label": "B"}, {"key": "enspro", "type": "enspro", "label": "撞内置"},
        {"key": "enspro-2", "type": "enspro", "label": "重复"}, {"key": "bad key", "type": "enspro", "label": "x"},
        {"key": "foo-3", "type": "foo", "label": "未知类型"}, {"key": "ensp-4", "type": "enspro", "label": "前缀不符"}, 7]))
    lab_lib.load_custom_envs()
    assert [e.key for e in lab_lib.custom_envs()] == ["enspro-2"] and lab_lib.get_env("enspro-2").label == "B"
    assert lab_lib.get_env("enspro").label == "eNSP Pro" and not lab_lib.get_env("enspro").custom
    db.set_setting("lab:envs", "not json")
    lab_lib.load_custom_envs()                       # 整体替换：库里没有的实例去掉
    assert lab_lib.custom_envs() == []
    # 实例的看门狗参数不读全局环境变量（缺省 = 内置值）
    env = lab_lib.register_custom("enspro-5", "enspro", "E")
    assert all(p.env_var == "" for p in env.watch)
    assert lab_lib.watch_value("enspro-5", "start_timeout") == 150 and lab_lib.watch_source("enspro-5", "start_timeout") == "default"


# ---------------- 接口：添加 / 修改 / 删除 / 持久化 ----------------

def test_add_edit_persist_and_reload(app, listener, caplog):
    c = app.c
    caplog.set_level(logging.DEBUG)
    d = c.get("/api/lab/environments").json()
    assert [t["key"] for t in d["types"]] == ["enspro"]
    t = d["types"][0]
    assert t["device_password"] and {p["name"] for p in t["watch"]} >= {"start_timeout", "max_devices"}
    # 校验
    for body, why in (({"type": "nope", "label": "x", "url": listener}, "不支持"),
                      ({"type": "enspro", "label": " ", "url": listener}, "显示名"),
                      ({"type": "enspro", "label": "eNSP pro", "url": listener}, "已被环境 enspro"),
                      ({"type": "enspro", "label": "x", "url": "ftp://1.2.3.4"}, "http"),
                      ({"type": "enspro", "label": "x", "url": listener, "start_timeout": 5}, "启动超时"),
                      ({"type": "enspro", "label": "x", "url": listener, "early_check": 200, "start_timeout": 100}, "早期"),
                      ({"type": "enspro", "label": "x", "url": listener, "device_password": "a b"}, "密码")):
        r = c.post("/api/lab/environments", json=body)
        assert r.status_code == 422 and why in r.json()["detail"], (body, r.text)
    assert lab_lib.custom_envs() == []
    # 添加：地址、密码（只写不读）、启动超时、设备上限
    r = _add(c, listener + "/", device_password=FAKE_PW, start_timeout=300, max_devices=3)
    assert r["key"] == "enspro-2" and r["warning"] == "" and r["probe"]["available"] is True
    e = r["environment"]
    assert e["custom"] and e["type"] == "enspro" and e["label"] == "机房 B" and e["platform"] == "yunshan"
    assert e["endpoint"]["value"] == listener and e["endpoint"]["source"] == "db"
    assert e["device_password"] == {"name": "ENSPRO_DEVICE_PASSWORD__ENSPRO_2", "configured": True, "source": "file"}
    assert e["secrets"] == {"ENSPRO_DEVICE_PASSWORD__ENSPRO_2": True}
    assert e["watchdog"]["values"]["start_timeout"] == 300 and e["watchdog"]["values"]["max_devices"] == 3
    text = app.secrets.read_text()
    assert f"ENSPRO_DEVICE_PASSWORD__ENSPRO_2={FAKE_PW}" in text and DEEPSEEK in text and "# ainet secrets" in text
    assert json.loads(db.get_setting("lab:envs")) == [{"key": "enspro-2", "type": "enspro", "label": "机房 B"}]
    assert db.get_setting("lab:endpoint:enspro-2") == listener
    assert json.loads(db.get_setting("lab:watch:enspro-2")) == {"start_timeout": 300, "max_devices": 3}
    envs = _envs(c)
    assert envs["enspro-2"]["available"] is True and envs["enspro-2"]["current"] is False
    assert envs["enspro"]["custom"] is False and envs["enspro"]["device_password"]["name"] == "ENSPRO_DEVICE_PASSWORD__ENSPRO"
    assert envs["ensp"]["device_password"] is None
    # 第二个：连不上也保存，返回警告
    r2 = _add(c, "https://127.0.0.1:9", label="机房 C")
    assert r2["key"] == "enspro-3" and "连不上" in r2["warning"] and r2["probe"]["reason_code"] == "unreachable"
    assert _envs(c)["enspro-3"]["available"] is False
    # 添加前的测试连接
    pr = c.post("/api/lab/environments/probe", json={"type": "enspro", "url": listener}).json()
    assert pr["available"] is True and pr["value"] == listener
    assert c.post("/api/lab/environments/probe", json={"type": "enspro", "url": "ftp://x"}).status_code == 422
    # 改名 / 设密码 / 清密码；内置不能改名，不存在 404
    r = c.patch("/api/lab/environments/enspro-3", json={"label": "机房 C2", "device_password": FAKE_PW2})
    assert r.status_code == 200 and r.json()["changed"] == ["label", "device_password"], r.text
    assert r.json()["environment"]["device_password"]["configured"] is True
    assert c.post("/api/lab/environments/enspro-3", json={"label": "机房 C2"}).json()["changed"] == []
    assert c.patch("/api/lab/environments/enspro-3", json={"label": "机房 B"}).status_code == 422     # 重名
    assert c.patch("/api/lab/environments/enspro", json={"label": "改内置"}).status_code == 422
    assert c.patch("/api/lab/environments/ensp", json={"device_password": "x"}).status_code == 422
    assert c.patch("/api/lab/environments/nope-9", json={"label": "x"}).status_code == 404
    assert c.patch("/api/lab/environments/enspro-3", json={}).status_code == 422
    r = c.patch("/api/lab/environments/enspro-3", json={"clear_password": True})
    assert r.json()["changed"] == ["device_password"] and r.json()["environment"]["device_password"]["configured"] is False
    assert "ENSPRO_DEVICE_PASSWORD__ENSPRO_3" not in app.secrets.read_text()
    # 自定义实例的地址不能清空（没有缺省值）
    assert c.post("/api/lab/environments/enspro-2/endpoint", json={"value": ""}).status_code == 422
    # 密码绝不出现在接口返回与日志里
    for u in ("/api/lab/environments", "/api/lab/environments/enspro-2/endpoint", "/api/lab/environments/enspro-2/watchdog"):
        assert FAKE_PW not in c.get(u).text and FAKE_PW2 not in c.get(u).text
    assert FAKE_PW not in caplog.text and FAKE_PW2 not in caplog.text
    # 重启：内存里的注册表清掉后，由库里的 lab:envs 重新注册，地址 / 参数 / 名字都在
    for x in lab_lib.custom_envs():
        lab_lib.unregister(x.key)
    db.set_setting("lab:env", "enspro-2")
    db.set_setting("lab:sandbox:enspro-2", "2")
    mgr =labenv.LabManager(make=lambda k, **kw: SimpleNamespace(power=False, kind="enspro", env_key=k, **kw))
    mgr.boot()
    assert (mgr.key, mgr.sandbox) == ("enspro-2", 2)              # 当前环境是用户添加的实例：启动时不会被当成未注册
    assert [x.key for x in lab_lib.custom_envs()] == ["enspro-2", "enspro-3"]
    assert lab_lib.get_env("enspro-3").label == "机房 C2"
    assert lab_lib.endpoint_value("enspro-2") == listener and lab_lib.watch_value("enspro-2", "start_timeout") == 300
    assert lab_lib.max_devices("enspro-2") == 3 and lab_lib.max_devices("enspro") == 0


def test_delete_409_and_cleanup(app, listener):
    c = app.c
    _add(c, listener, device_password=FAKE_PW, start_timeout=200)
    _add(c, listener, label="机房 C", device_password=FAKE_PW2)
    assert c.delete("/api/lab/environments/enspro").status_code == 400        # 内置不能删
    assert c.delete("/api/lab/environments/nope-2").status_code == 404
    # 当前环境 409
    r = c.post("/api/lab/environment", json={"key": "enspro-2", "sandbox": 2})
    assert r.status_code == 200, r.text
    assert c.get("/api/lab").json()["key"] == "enspro-2" and c.get("/api/lab").json()["kind"] == "enspro"
    assert c.app.state.labmgr.provider.env_key == "enspro-2" and c.app.state.labmgr.provider.label() == "机房 B"
    r = c.delete("/api/lab/environments/enspro-2")
    assert r.status_code == 409 and "当前环境" in r.json()["detail"]
    # 练习进行中 409
    pm = c.app.state.practice
    pm.active = SimpleNamespace(scenario={"id": "x"}, mode="practice")
    try:
        r = c.delete("/api/lab/environments/enspro-3")
        assert r.status_code == 409 and "练习进行中" in r.json()["detail"]
    finally:
        pm.active = None
    # 考试进行中：添加 / 修改也 409
    pm.active = SimpleNamespace(scenario={"id": "x"}, mode="exam")
    try:
        assert c.post("/api/lab/environments", json={"type": "enspro", "label": "x", "url": listener}).status_code == 409
        assert c.patch("/api/lab/environments/enspro-3", json={"label": "y"}).status_code == 409
    finally:
        pm.active = None
    # 切回经典版后删除：清理设置、基线、secrets 行（其他行保留）、内存
    assert c.post("/api/lab/environment", json={"key": "ensp"}).status_code == 200
    db.set_setting("baseline:enspro-2:ainet_test:T1", '{"config": "x"}')
    db.set_setting("baseline:enspro:ainet_test:T1", '{"config": "keep"}')
    db.set_setting("baseline:enspro-20:ainet_test:T1", '{"config": "keep"}')
    r = c.delete("/api/lab/environments/enspro-2")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == "enspro-2" and r.json()["baselines_removed"] == 1 and r.json()["secret_removed"] is True
    for k in ("lab:endpoint:enspro-2", "lab:watch:enspro-2", "lab:sandbox:enspro-2", "baseline:enspro-2:ainet_test:T1"):
        assert db.get_setting(k) is None, k
    assert db.get_setting("baseline:enspro:ainet_test:T1") and db.get_setting("baseline:enspro-20:ainet_test:T1")
    text = app.secrets.read_text()
    assert "ENSPRO_DEVICE_PASSWORD__ENSPRO_2" not in text and f"ENSPRO_DEVICE_PASSWORD__ENSPRO_3={FAKE_PW2}" in text
    assert DEEPSEEK in text
    assert "enspro-2" not in lab_lib.REGISTRY and "enspro-2" not in _envs(c)
    assert [x["key"] for x in json.loads(db.get_setting("lab:envs"))] == ["enspro-3"]
    assert c.post("/api/lab/environment", json={"key": "enspro-2"}).status_code == 422
    # 编号不复用
    assert _add(c, listener, label="机房 D")["key"] == "enspro-4"


# ---------------- 题目对所有实例可见、准备环境、设备上限 ----------------

def test_scenarios_visible_on_every_instance_and_capacity_per_instance(app, listener):
    c = app.c
    _add(c, listener, device_password=FAKE_PW)
    a = c.get("/api/scenarios?env=enspro").json()
    b = c.get("/api/scenarios?env=enspro-2").json()
    assert b["platform"] == "yunshan" and b["env"] == "enspro-2"
    assert {s["id"] for s in a["scenarios"]} == {s["id"] for s in b["scenarios"]} and b["scenarios"]
    # 设备上限按实例：只给 enspro-2 设 3，只在浏览 enspro-2 时隐藏
    assert c.post("/api/lab/environments/enspro-2/watchdog", json={"max_devices": 3}).status_code == 200
    b = c.get("/api/scenarios?env=enspro-2").json()
    assert b["max_devices"] == 3 and b["capacity_hidden"] == 5
    assert OVER3.isdisjoint({s["id"] for s in b["scenarios"]})
    a = c.get("/api/scenarios?env=enspro").json()
    assert a["max_devices"] == 0 and OVER3 <= {s["id"] for s in a["scenarios"]}
    # 指定在 enspro-2 上准备超上限的题：409；在内置 enspro 上不受 enspro-2 的上限影响（会排队准备，这里只看不是 409 上限）
    r = c.post("/api/lab/prepare", json={"scenario_id": "ospf-r1-shutdown-ys", "environment": "enspro-2"})
    assert r.status_code == 409 and "上限 3 台" in r.json()["detail"]


def test_prepare_on_added_instance(app, listener):
    c = app.c
    _add(c, listener, device_password=FAKE_PW)
    mgr = c.app.state.labmgr
    mgr.prepare_poll = 0.05
    # 平台不符 / 环境不存在
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link", "environment": "ensp"})
    assert r.status_code == 409 and "不能在 eNSP 里做" in r.json()["detail"]
    assert c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link", "environment": "nope-3"}).status_code == 422
    rest = app.rests.setdefault("enspro-2", FakeRest())
    rest.status = {6: 0, 7: 0}
    rest.instant_start = True
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link", "environment": "enspro-2"})
    assert r.status_code == 202, r.text
    assert r.json()["environment"] == "enspro-2" and r.json()["env_label"] == "机房 B"
    job = _wait_job(c, lambda j: j["done"])
    assert job["ok"], job
    assert (mgr.key, mgr.sandbox) == ("enspro-2", 2) and job["devices"] == {"AR1": "online", "AR2": "online"}
    assert any(p == "/service/ensp/device/batchStart" for _, p, b in rest.calls if b)
    assert "enspro" not in app.rests                   # 内置 eNSP Pro 的假服务一次都没建
    assert mgr.power.configured_for == "enspro-2"      # 看门狗换成该实例的参数
    # 当前环境是 enspro-2 时，不指定 environment 的准备也留在 enspro-2（不跳回第一个 YunShan 环境）
    r = c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    assert r.status_code == 202 and r.json()["environment"] == "enspro-2"
    _wait_job(c, lambda j: j["done"])
    # 干净基线按实例区分（内置 enspro 的键不变，见 test_labenv.test_baseline_key_follows_sandbox）
    pm = c.app.state.practice
    cur = c.portal.call(pm.resolve)
    assert pm._baseline_key(cur, "AR1") == "baseline:enspro-2:ainet_test:AR1"
    assert c.get("/api/scenarios?env=enspro-2").json()["current_env"] == "enspro-2"
    # 环境列表里当前环境是 enspro-2，顶栏只读显示用的 label 是实例名
    d = c.get("/api/lab/environments").json()
    assert d["current"] == "enspro-2" and d["label"] == "机房 B"
    assert time.time() > 0
