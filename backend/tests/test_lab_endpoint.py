"""接入设置（spec §5.2「接入设置」，里程碑 6.12）：接入地址的校验与规范化、优先级（库里的记录 / AINET_LAB_FORCE / 环境变量 /
缺省）、离线原因分类（unreachable / tls / auth / missing_secret / other）、凭据只报有无、eNSP console 主机不是本机时
runtime_local=false、修改地址的 409 / 重建当前后端 / 通知终端、接口（读取 / 修改 / 测试连接）。

全部用本机回环上的临时端口或 TEST-NET 地址（192.0.2.x，只做格式与本机判断，不连），不碰真机、不连 eNSP Pro 虚机；
用到的设备密码都是假的，并断言它不出现在任何接口返回与日志里。"""
from __future__ import annotations

import asyncio
import asyncio.base_events
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import config
import db
import lab as lab_lib
import labenv
import main
from enspro import EnsproClient, EnsproProvider

FAKE_PW = "Fake-Endpoint-Pw#77"     # 假密码：只用来断言「只报有无、不泄露」


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    lab_lib.clear_probe_cache()
    yield
    lab_lib.clear_probe_cache()


@pytest.fixture
def no_local_names(monkeypatch):
    """本机地址判断不做主机名解析（结果只取决于环回判断）。"""
    monkeypatch.setattr(lab_lib, "_local_addresses", lambda: set())


# ---------------- 校验与规范化 ----------------

@pytest.mark.parametrize("raw,want", [
    ("https://10.0.0.5:8443/", "https://10.0.0.5:8443"),
    ("  HTTPS://enspro.lan:8443//  ", "https://enspro.lan:8443"),
    ("http://10.0.0.5", "http://10.0.0.5"),
    ("https://[fd00::5]:8443", "https://[fd00::5]:8443"),
    ("https://host/ensp/", "https://host/ensp"),
    # 不写协议（里程碑 6.20）：按 https；只有主机名时补缺省端口 8443
    ("203.0.113.10:8443", "https://203.0.113.10:8443"),
    ("enspro.lan:8443", "https://enspro.lan:8443"),         # urlsplit 会把 enspro.lan 当成协议，不能靠它判断
    ("localhost:8443/", "https://localhost:8443"),
    ("enspro.lan", "https://enspro.lan:8443"),
    ("10.0.0.5", "https://10.0.0.5:8443"),
    ("fd00::5", "https://[fd00::5]:8443"),
    ("[fd00::5]:9443", "https://[fd00::5]:9443"),
])
def test_normalize_url_ok(raw, want):
    assert lab_lib.normalize_endpoint("url", raw) == want


@pytest.mark.parametrize("raw,why", [
    ("", "不能为空"), ("ftp://10.0.0.5", "http"), ("10.0.0.5:99999", "格式"),
    ("https://user:secret@10.0.0.5:8443", "用户名或密码"), ("https://10.0.0.5:8443/?a=1", "查询串"),
    ("https://10.0.0.5:8443/#x", "片段"), ("https://10.0.0.5:99999", "格式"), ("https://10.0.0.5 :8443", "空白"),
    ("https://", "主机名"),
])
def test_normalize_url_rejects(raw, why):
    with pytest.raises(ValueError) as ei:
        lab_lib.normalize_endpoint("url", raw)
    assert why in str(ei.value)


def test_normalize_host():
    assert lab_lib.normalize_endpoint("host", " 192.0.2.72 ") == "192.0.2.72"
    assert lab_lib.normalize_endpoint("host", "[::1]") == "::1"
    assert lab_lib.normalize_endpoint("host", "eNSP-Host.lan") == "ensp-host.lan"
    for bad in ("http://10.0.0.1", "10.0.0.1:2000", "a b", "-bad", "x/y", ""):
        with pytest.raises(ValueError):
            lab_lib.normalize_endpoint("host", bad)


# ---------------- 优先级与来源 ----------------

def test_endpoint_sources_and_db_precedence(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "ENSPRO_URL", "https://127.0.0.1:9")
    assert lab_lib.endpoint_value("enspro") == "https://127.0.0.1:9"
    assert lab_lib.endpoint_source("enspro") == "default"
    monkeypatch.setenv("AINET_ENSPRO_URL", "https://127.0.0.1:9")
    assert lab_lib.endpoint_source("enspro") == "env"
    # 库里的记录优先于环境变量（与 lab:env 一致）
    db.set_setting("lab:endpoint:enspro", "https://10.9.9.9:8443/")
    db.set_setting("lab:endpoint:ensp", "not a host!")           # 不合法的记录：忽略，用缺省值
    lab_lib.load_endpoints(force=False)
    assert lab_lib.endpoint_value("enspro") == "https://10.9.9.9:8443"
    assert lab_lib.endpoint_source("enspro") == "db"
    assert lab_lib.endpoint_value("ensp") == config.ENSP_HOST and lab_lib.endpoint_source("ensp") == "default"
    # 新建的 eNSP Pro 后端用生效的地址
    p = lab_lib.make_provider("enspro", sandbox=2)
    assert p.client.base == "https://10.9.9.9:8443"
    asyncio.run(p.close())


def test_lab_force_env_wins_and_writes_back(db_path, monkeypatch):
    db.init_db()
    db.set_setting("lab:endpoint:enspro", "https://10.9.9.9:8443")
    db.set_setting("lab:endpoint:ensp", "10.1.1.1")
    monkeypatch.setattr(config, "LAB_FORCE", True)
    monkeypatch.setenv("AINET_ENSPRO_URL", "https://10.2.2.2:8443")
    monkeypatch.setattr(config, "ENSPRO_URL", "https://10.2.2.2:8443")
    monkeypatch.delenv("ENSP_HOST", raising=False)               # 没设 ENSP_HOST：沿用库里的记录
    mgr = labenv.LabManager(make=lambda k, **kw: lab_lib.EnspProvider())
    mgr.boot()
    assert lab_lib.endpoint_value("enspro") == "https://10.2.2.2:8443"
    assert lab_lib.endpoint_source("enspro") == "force"
    assert db.get_setting("lab:endpoint:enspro") == "https://10.2.2.2:8443"
    assert lab_lib.endpoint_value("ensp") == "10.1.1.1" and lab_lib.endpoint_source("ensp") == "db"


# ---------------- 离线原因分类 ----------------

async def _tcp_server(handler):
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def test_reason_unreachable_tls_missing_secret_ok(monkeypatch):
    async def closer(r, w):            # 接受连接后立即关掉：TCP 能连，TLS 握手失败
        w.close()

    async def run():
        srv, port = await _tcp_server(closer)
        try:
            r1 = await lab_lib.probe_endpoint("enspro", "https://127.0.0.1:9")
            r2 = await lab_lib.probe_endpoint("enspro", f"https://127.0.0.1:{port}")
            r3 = await lab_lib.probe_endpoint("enspro", f"http://127.0.0.1:{port}")
            monkeypatch.setenv("ENSPRO_DEVICE_PASSWORD", FAKE_PW)
            r4 = await lab_lib.probe_endpoint("enspro", f"http://127.0.0.1:{port}")
        finally:
            srv.close()
            await srv.wait_closed()
        return r1, r2, r3, r4
    r1, r2, r3, r4 = asyncio.run(run())
    assert r1["available"] is False and r1["reason_code"] == "unreachable" and "127.0.0.1:9" in r1["reason"]
    assert "接入设置" in r1["reason"]
    assert r2["available"] is False and r2["reason_code"] == "tls" and "TLS" in r2["reason"]
    assert r3["available"] is False and r3["reason_code"] == "missing_secret"
    assert "ENSPRO_DEVICE_PASSWORD" in r3["reason"] and "secrets.env" in r3["reason"]
    assert r4 == {**r4, "available": True, "reason": "", "reason_code": ""}
    assert FAKE_PW not in json.dumps([r1, r2, r3, r4], ensure_ascii=False)


def test_available_reports_reason_code_and_caches_it(monkeypatch):
    monkeypatch.setattr(config, "ENSPRO_URL", "https://127.0.0.1:9")
    r = asyncio.run(lab_lib.available("enspro", force=True))
    assert r["available"] is False and r["reason_code"] == "unreachable"
    assert asyncio.run(lab_lib.available("enspro"))["reason_code"] == "unreachable"     # 缓存里也有分类

    async def two():                    # 旧式探测只返回 (在线, 原因)：离线分类记 other
        return False, "坏了"
    monkeypatch.setitem(lab_lib.REGISTRY, "two", lab_lib.LabEnv("two", "Two", "vrp5", lambda o: None, two))
    assert asyncio.run(lab_lib.available("two", force=True))["reason_code"] == "other"

    async def slow():
        await asyncio.sleep(5)
        return True, ""
    monkeypatch.setitem(lab_lib.REGISTRY, "slow", lab_lib.LabEnv("slow", "Slow", "vrp5", lambda o: None, slow))
    monkeypatch.setattr(lab_lib, "PROBE_TIMEOUT", 0.2)
    assert asyncio.run(lab_lib.available("slow", force=True))["reason_code"] == "unreachable"


def test_ensp_remote_host_runtime_not_local(monkeypatch, no_local_names):
    monkeypatch.setattr(config, "ENSP_HOST", "192.0.2.10")            # TEST-NET：只做本机判断，不连
    v = lab_lib.endpoint_view("ensp")
    assert v["runtime_local"] is False and v["value"] == "192.0.2.10"
    r = asyncio.run(lab_lib.available("ensp", force=True))
    assert r["available"] is False and r["reason_code"] == "other"
    assert "设备运行状态无法检测" in r["reason"] and "节点代理" in r["reason"]
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")                       # 远程调试覆盖：照旧算在线
    assert asyncio.run(lab_lib.available("ensp", force=True))["available"] is True
    monkeypatch.setattr(config, "ENSP_HOST", "127.0.0.1")
    assert lab_lib.endpoint_view("ensp")["runtime_local"] is True
    assert lab_lib.is_local_host("localhost") and lab_lib.is_local_host("::1")


def test_ensp_unresolvable_host_is_unreachable(monkeypatch, no_local_names):
    async def fail(*a, **kw):
        raise OSError(-2, "Name or service not known")
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fail)      # 不真的查 DNS
    r = asyncio.run(lab_lib.probe_endpoint("ensp", "no-such-host.invalid"))
    assert r["available"] is False and r["reason_code"] == "unreachable" and "解析不了主机名" in r["reason"]


def test_secrets_view_only_presence(monkeypatch, tmp_path):
    env = lab_lib.get_env("enspro")
    assert lab_lib.secrets_view(env) == {"ENSPRO_DEVICE_PASSWORD": False}
    assert lab_lib.secrets_view(lab_lib.get_env("ensp")) == {}
    f = tmp_path / "secrets.env"
    f.write_text(f"ENSPRO_DEVICE_PASSWORD={FAKE_PW}\n", encoding="utf-8")
    monkeypatch.setenv("AINET_SECRETS_FILE", str(f))
    assert lab_lib.secrets_view(env) == {"ENSPRO_DEVICE_PASSWORD": True}


def test_enspro_login_rejected_sets_auth_health():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/getUserInfo":
            return httpx.Response(200, json={"code": 401001, "message": "用户名或密码错误"})
        return httpx.Response(200, text="ok")
    client = EnsproClient("http://enspro.test", transport=httpx.MockTransport(handler))
    p = EnsproProvider(client=client, sandbox=2, password=lambda: "x", cache_ttl=0)
    assert p.health() is None

    async def run():
        try:
            await client.ensure()
        except Exception:
            pass
        h = p.health()
        client.lost_reason = "eNSP Pro 会话已失效：该账号在其它地方被重新登录"
        h2 = p.health()
        await p.close()
        return h, h2
    h, h2 = asyncio.run(run())
    assert h[0] == "auth" and "登录 eNSP Pro 失败" in h[1]
    assert h2 == ("auth", "eNSP Pro 会话已失效：该账号在其它地方被重新登录")


# ---------------- 修改地址（LabManager 单元） ----------------

class Prov(lab_lib.LabProvider):
    def __init__(self, kind, log, sandbox=None, slow=0.0, health=None):
        self.kind, self.log, self.sandbox_id, self.slow, self._health = kind, log, sandbox, slow, health
        self.platform = lab_lib.get_env(kind).platform

    async def resolve(self):
        return {"state": "none", "message": "", "topology": None, "lab": self.info()}

    def health(self):
        return self._health

    async def close(self):
        self.log.append(("close", self.kind))
        await asyncio.sleep(self.slow)


def make_mgr(log, slow=0.0, health=None):
    mgr = labenv.LabManager(make=lambda k, **kw: (log.append(("make", k, lab_lib.endpoint_value(k))),
                                                  Prov(k, log, kw.get("sandbox"), slow, health))[1])

    class Hub:
        async def reset_all(self, message="", default_sysname=None, **kw):
            log.append(("hub-reset", message.strip()))
            return 0
    mgr.bind(Hub(), SimpleNamespace(active=None, _op=asyncio.Lock(), lab=None),
             SimpleNamespace(exam=None, _lock=asyncio.Lock()), SimpleNamespace(on_env_switch=lambda d: None))
    return mgr


def test_set_endpoint_current_env_rebuilds_and_notifies(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", 2)
    monkeypatch.setattr(labenv, "CLOSE_TIMEOUT", 0.2)
    log: list = []

    async def run():
        mgr = make_mgr(log, slow=5.0)                 # 旧后端登出卡住：不超过 CLOSE_TIMEOUT
        mgr.boot()
        stopped = []

        async def stop_old(old):
            stopped.append(old)
        mgr._stop_old = stop_old
        gen0 = mgr.gen
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await mgr.set_endpoint("enspro", "https://10.7.7.7:8443/")
        assert loop.time() - t0 < 2.0
        assert r["changed"] and r["rebuilt"] and r["endpoint"]["value"] == "https://10.7.7.7:8443"
        assert r["endpoint"]["source"] == "db" and mgr.gen == gen0 + 1 and mgr.sandbox == 2
        assert stopped == []                          # 改地址不停旧沙箱的设备
        notes = [m for k, m, *_ in log if k == "hub-reset"]
        assert notes and "接入地址已改为 https://10.7.7.7:8443" in notes[-1] and "终端已断开" in notes[-1]
        assert log[-1] == ("make", "enspro", "https://10.7.7.7:8443")
        # 同一地址再存一次：不重建
        n = len(log)
        r2 = await mgr.set_endpoint("enspro", "https://10.7.7.7:8443")
        assert r2["changed"] is False and r2["rebuilt"] is False and len(log) == n
        # 改另一个（非当前）环境：只记库，不重建
        r3 = await mgr.set_endpoint("ensp", "10.3.3.3")
        assert r3["rebuilt"] is False and len(log) == n and lab_lib.endpoint_value("ensp") == "10.3.3.3"
        # 空 = 回到缺省值（删库里的记录），当前环境重建
        r4 = await mgr.set_endpoint("enspro", "")
        assert r4["rebuilt"] and r4["endpoint"]["source"] in ("env", "default")
        assert r4["endpoint"]["value"] == config.ENSPRO_URL
        return mgr
    asyncio.run(run())
    assert db.get_setting("lab:endpoint:enspro") is None and db.get_setting("lab:endpoint:ensp") == "10.3.3.3"
    # 重启：库里的记录生效
    lab_lib.set_endpoint_override("ensp", None)
    make_mgr([]).boot()
    assert lab_lib.endpoint_value("ensp") == "10.3.3.3" and lab_lib.endpoint_source("ensp") == "db"


def test_set_endpoint_refused_when_busy_or_invalid(db_path):
    db.init_db()
    log: list = []

    async def run():
        mgr = make_mgr(log)
        mgr.boot()
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_endpoint("enspro", "ftp://x")
        assert ei.value.status == 422
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_endpoint("nope", "https://x")
        assert ei.value.status == 422
        mgr.pm.active = SimpleNamespace(mode="practice")
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_endpoint("enspro", "https://10.0.0.1")
        assert ei.value.status == 409 and "练习进行中" in ei.value.detail and "修改接入地址" in ei.value.detail
        mgr.pm.active = None
        mgr.exam.exam = object()
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_endpoint("ensp", "10.0.0.1")
        assert ei.value.status == 409 and "考试进行中" in ei.value.detail
        assert mgr.endpoint_info("ensp")["can_edit"] is False
        mgr.exam.exam = None
        mgr.job = {"done": False, "title": "某题", "message": "等待设备"}
        with pytest.raises(lab_lib.LabError) as ei:
            await mgr.set_endpoint("ensp", "10.0.0.1")
        assert ei.value.status == 409 and "准备环境" in ei.value.detail
        assert mgr.switching is False
    asyncio.run(run())
    assert db.get_setting("lab:endpoint:enspro") is None and db.get_setting("lab:endpoint:ensp") is None


def test_environments_view_auth_overlay_and_secrets(db_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")

    async def ok(endpoint=None):
        return True, ""
    monkeypatch.setitem(lab_lib.REGISTRY, "enspro", lab_lib.LabEnv(**{**lab_lib.REGISTRY["enspro"].__dict__,
                                                                      "probe": ok}))
    mgr = make_mgr([], health=("auth", "eNSP Pro 会话已失效：该账号在其它地方被重新登录"))
    mgr.boot()
    out = asyncio.run(mgr.environments(force=True))
    e = {x["key"]: x for x in out["environments"]}
    assert e["enspro"]["available"] is True and e["enspro"]["warning"] is True
    assert e["enspro"]["reason_code"] == "auth" and "会话已失效" in e["enspro"]["reason"]
    assert e["enspro"]["secrets"] == {"ENSPRO_DEVICE_PASSWORD": False}
    assert e["enspro"]["endpoint"]["kind"] == "url" and e["enspro"]["runtime_local"] is None
    assert e["ensp"]["endpoint"]["kind"] == "host" and e["ensp"]["runtime_local"] is True    # conftest：127.0.0.1
    assert "warning" not in e["ensp"]


# ---------------- 接口 ----------------

@pytest.fixture
def app(db_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        yield c
    main.SESSIONS.clear()


def login(c):
    assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200


def test_api_endpoint_requires_login(app):
    for m, u, b in [("get", "/api/lab/environments/enspro/endpoint", None),
                    ("post", "/api/lab/environments/enspro/endpoint", {"value": "https://10.0.0.1"}),
                    ("post", "/api/lab/environments/enspro/endpoint/test", {})]:
        assert getattr(app, m)(u, **({"json": b} if b is not None else {})).status_code == 401, u


def test_api_endpoint_get_set_test_and_no_secret_leak(app, monkeypatch, caplog):
    c = app
    login(c)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ENSPRO_DEVICE_PASSWORD", FAKE_PW)
    r = c.get("/api/lab/environments/enspro/endpoint")
    assert r.status_code == 200
    d = r.json()
    assert d["endpoint"]["value"] == "https://127.0.0.1:9" and d["endpoint"]["source"] == "default"
    assert d["secrets"] == {"ENSPRO_DEVICE_PASSWORD": True} and d["can_edit"] is True and d["current"] is False
    envs = c.get("/api/lab/environments?refresh=1")
    assert envs.status_code == 200
    ep = {x["key"]: x for x in envs.json()["environments"]}["enspro"]
    assert ep["available"] is False and ep["reason_code"] == "unreachable" and ep["secrets"]["ENSPRO_DEVICE_PASSWORD"]
    # 测试连接：不保存
    t = c.post("/api/lab/environments/enspro/endpoint/test", json={"value": "https://127.0.0.1:9/"})
    assert t.status_code == 200 and t.json()["reason_code"] == "unreachable" and t.json()["value"] == "https://127.0.0.1:9"
    assert c.post("/api/lab/environments/enspro/endpoint/test", json={"value": "ftp://x"}).status_code == 422
    assert db.get_setting("lab:endpoint:enspro") is None
    # 修改：格式不对 422；合法的存库、来源变成 db
    assert c.post("/api/lab/environments/enspro/endpoint", json={"value": "https://u:p@10.0.0.1"}).status_code == 422
    assert c.post("/api/lab/environments/nope/endpoint", json={"value": "x"}).status_code == 422
    r = c.post("/api/lab/environments/enspro/endpoint", json={"value": "https://127.0.0.1:19/"})
    assert r.status_code == 200 and r.json()["rebuilt"] is False                 # 当前环境是 eNSP：不重建
    assert db.get_setting("lab:endpoint:enspro") == "https://127.0.0.1:19"
    assert c.get("/api/lab/environments/enspro/endpoint").json()["endpoint"]["source"] == "db"
    # eNSP console 主机：改成非本机地址 → runtime_local=false，当前环境重建（gen + 1）
    gen = main.LABMGR.gen
    r = c.post("/api/lab/environments/ensp/endpoint", json={"value": "192.0.2.20"})
    assert r.status_code == 200 and r.json()["rebuilt"] is True and r.json()["runtime_local"] is False
    assert main.LABMGR.gen == gen + 1 and main.LAB.endpoint(2000) == "192.0.2.20:2000"
    t = c.post("/api/lab/environments/ensp/endpoint/test", json={})
    assert t.status_code == 200 and t.json()["runtime_local"] is False and t.json()["value"] == "192.0.2.20"
    r = c.post("/api/lab/environments/ensp/endpoint", json={"value": ""})              # 恢复缺省
    assert r.status_code == 200 and r.json()["endpoint"]["value"] == "127.0.0.1"
    # 凭据值绝不出现在接口返回与日志里
    for u in ("/api/lab/environments", "/api/lab/environments/enspro/endpoint", "/api/lab"):
        assert FAKE_PW not in c.get(u).text
    assert FAKE_PW not in caplog.text


def test_api_endpoint_409_during_practice(app):
    c = app
    login(c)
    main.LABMGR.pm.active = SimpleNamespace(mode="practice")
    try:
        r = c.post("/api/lab/environments/enspro/endpoint", json={"value": "https://10.0.0.1"})
        assert r.status_code == 409 and "练习进行中" in r.json()["detail"]
        assert c.get("/api/lab/environments/enspro/endpoint").json()["can_edit"] is False
    finally:
        main.LABMGR.pm.active = None
