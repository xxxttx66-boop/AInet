"""自动识别 https（spec §5.2「接入设置」，里程碑 6.20）：eNSP Pro 地址可以不写协议；写 http:// 或没写协议时先试 TLS 握手，
通就改成 https:// 并返回 normalized_from（原输入）与「已自动改为 https」；TLS 不通而明文 HTTP 有响应就保留 http 并警告；
都不通按输入（没写协议按 https）。已存的 http 地址不自动迁移，测试连接时给 suggest。

全部用本机回环上的临时假服务（只绑 127.0.0.1，测试结束关闭）：假 TLS 服务（openssl 现场生成的一次性自签证书，没有 openssl
时跳过）、明文 HTTP 服务、接受后立即关闭的服务；或 monkeypatch 探测函数。不连真机、不连任何远程主机，不涉及密码。"""
from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

import db
import lab as lab_lib
from test_lab_instances import _add, app, secrets_file  # noqa: F401
from test_labenv import make_mgr, probes_ok  # noqa: F401


class FakeServer:
    """127.0.0.1 上的临时服务：mode = tls（完成 TLS 握手）/ http（明文，回 HTTP 响应，TLS ClientHello 也回 400）/
    close（接受后立即关闭）。每个连接一个线程，stop() 关掉监听。"""

    def __init__(self, mode: str, cert: tuple[str, str] | None = None):
        self.mode = mode
        self.ctx = None
        if mode == "tls":
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*cert)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        conn.settimeout(2)
        try:
            if self.mode == "tls":
                with self.ctx.wrap_socket(conn, server_side=True) as s:
                    try:
                        s.recv(1)
                    except OSError:
                        pass
            elif self.mode == "http":
                if conn.recv(4096):
                    conn.sendall(b"HTTP/1.0 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def stop(self):
        self.running = False
        self.t.join(1)
        self.sock.close()


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("没有 openssl，无法生成测试用自签证书")
    d = tmp_path_factory.mktemp("tls")
    key, crt = str(d / "k.pem"), str(d / "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=localhost"],
                   check=True, capture_output=True, timeout=30)
    return crt, key


@pytest.fixture
def servers(cert):
    s = {m: FakeServer(m, cert if m == "tls" else None) for m in ("tls", "http", "close")}
    yield {m: x.port for m, x in s.items()}
    for x in s.values():
        x.stop()


def _det(raw):
    return asyncio.run(lab_lib.detect_scheme("url", raw))


# ---------------- lab.detect_scheme ----------------

def test_detect_tls_port(servers):
    p = servers["tls"]
    r = _det(f"http://127.0.0.1:{p}/")
    assert r == {"value": f"https://127.0.0.1:{p}", "scheme_check": "https", "normalized_from": f"http://127.0.0.1:{p}/",
                 "notice": "已自动改为 https"}
    r = _det(f"127.0.0.1:{p}")
    assert r["value"] == f"https://127.0.0.1:{p}" and r["normalized_from"] == f"127.0.0.1:{p}" and r["notice"] == "已自动改为 https"


def test_detect_plain_http_port_keeps_http_with_warning(servers):
    p = servers["http"]
    r = _det(f"http://127.0.0.1:{p}")
    assert r["value"] == f"http://127.0.0.1:{p}" and r["scheme_check"] == "http" and "normalized_from" not in r
    assert "明文 HTTP" in r["warning"]
    r = _det(f"127.0.0.1:{p}")                     # 没写协议：明文 HTTP 端口按 http，告诉用户改了什么
    assert r["value"] == f"http://127.0.0.1:{p}" and r["normalized_from"] == f"127.0.0.1:{p}" and "明文 HTTP" in r["warning"]


def test_detect_inconclusive_and_unreachable_keep_input(servers, monkeypatch):
    monkeypatch.setattr(lab_lib, "SCHEME_TIMEOUT", 0.3)
    p = servers["close"]
    r = _det(f"http://127.0.0.1:{p}")              # 能连上，但既没有 TLS 也没有 HTTP 响应：按输入
    assert r == {"value": f"http://127.0.0.1:{p}", "scheme_check": "unknown"}
    r = _det(f"127.0.0.1:{p}")
    assert r["value"] == f"https://127.0.0.1:{p}" and r["scheme_check"] == "unknown" and r["normalized_from"] == f"127.0.0.1:{p}"
    r = _det("http://127.0.0.1:9")                 # 连不上：按输入
    assert r == {"value": "http://127.0.0.1:9", "scheme_check": "unreachable"}
    r = _det("127.0.0.1:9")                        # 没写协议、连不上：按 https 保存
    assert r["value"] == "https://127.0.0.1:9" and r["scheme_check"] == "unreachable" and "按 https" in r["notice"]


def test_detect_skips_https_and_host_and_keeps_port_80(monkeypatch):
    calls: list = []

    async def fake(host, port):
        calls.append((host, port))
        return True
    for f in ("_tcp_ok", "_tls_ok", "_plain_http_ok"):
        monkeypatch.setattr(lab_lib, f, fake)
    assert _det("https://192.0.2.1:8443/") == {"value": "https://192.0.2.1:8443", "scheme_check": "skip"}
    assert asyncio.run(lab_lib.detect_scheme("host", "192.0.2.1")) == {"value": "192.0.2.1", "scheme_check": "skip"}
    assert calls == []                             # 写了 https:// 或 host 类：不探测、不连
    r = _det("http://192.0.2.1")                   # TEST-NET，探测已 mock：不带端口时按原来的 80 端口试 https
    assert r["value"] == "https://192.0.2.1:80" and calls == [("192.0.2.1", 80), ("192.0.2.1", 80)]
    calls.clear()
    r = _det("enspro.lan")                         # 只有主机名：缺省端口 8443
    assert r["value"] == "https://enspro.lan:8443" and calls[0] == ("enspro.lan", 8443)
    with pytest.raises(ValueError):
        _det("ftp://x")


# ---------------- 接口：添加 / 添加前测试连接 / 改地址 / 测试连接 ----------------

def test_api_add_probe_set_and_suggest(app, servers):  # noqa: F811
    c = app.c
    tls, http = servers["tls"], servers["http"]
    pr = c.post("/api/lab/environments/probe", json={"type": "enspro", "url": f"127.0.0.1:{tls}"}).json()
    assert pr["available"] is True and pr["value"] == f"https://127.0.0.1:{tls}"
    assert pr["normalized_from"] == f"127.0.0.1:{tls}" and pr["notice"] == "已自动改为 https"
    # 添加时写 http://，该端口其实是 https：存 https://
    r = _add(c, f"http://127.0.0.1:{tls}")
    assert r["normalized_from"] == f"http://127.0.0.1:{tls}" and r["notice"] == "已自动改为 https"
    assert r["environment"]["endpoint"]["value"] == f"https://127.0.0.1:{tls}" and r["probe"]["available"] is True
    assert db.get_setting("lab:endpoint:enspro-2") == f"https://127.0.0.1:{tls}"
    # 明文 HTTP 端口：保留 http，给警告
    r = _add(c, f"http://127.0.0.1:{http}", label="机房 C")
    assert r["environment"]["endpoint"]["value"] == f"http://127.0.0.1:{http}" and "normalized_from" not in r
    assert "明文 HTTP" in r["warning"]
    # 改地址：没写协议 → https
    s = c.post("/api/lab/environments/enspro-3/endpoint", json={"value": f"127.0.0.1:{tls}"}).json()
    assert s["endpoint"]["value"] == f"https://127.0.0.1:{tls}" and s["normalized_from"] == f"127.0.0.1:{tls}"
    assert s["notice"] == "已自动改为 https"
    # 测试连接给定 http 地址：返回规范化后的 https（不保存）
    t = c.post("/api/lab/environments/enspro-2/endpoint/test", json={"value": f"http://127.0.0.1:{tls}"}).json()
    assert t["value"] == f"https://127.0.0.1:{tls}" and t["normalized_from"] == f"http://127.0.0.1:{tls}"
    # 已存的 http 地址（旧记录）：不自动迁移；测试当前地址时发现 https 可用，给 suggest
    db.set_setting("lab:endpoint:enspro-2", f"http://127.0.0.1:{tls}")
    lab_lib.set_endpoint_override("enspro-2", f"http://127.0.0.1:{tls}", "db")
    t = c.post("/api/lab/environments/enspro-2/endpoint/test", json={}).json()
    assert t["value"] == f"http://127.0.0.1:{tls}" and t["suggest"] == f"https://127.0.0.1:{tls}"
    assert "normalized_from" not in t
    assert c.get("/api/lab/environments/enspro-2/endpoint").json()["endpoint"]["value"] == f"http://127.0.0.1:{tls}"
    assert db.get_setting("lab:endpoint:enspro-2") == f"http://127.0.0.1:{tls}"
    lab_lib.load_endpoints()                       # 重启装载也不迁移
    assert lab_lib.endpoint_value("enspro-2") == f"http://127.0.0.1:{tls}"
    # 明文 HTTP 的旧记录：不给 suggest
    t = c.post("/api/lab/environments/enspro-3/endpoint/test", json={}).json()
    assert "suggest" not in t


def test_set_endpoint_detects_before_lock(db_path, monkeypatch):
    """set_endpoint 的自动识别在拿锁之前做（网络 I/O 不占切换 / 考试 / 练习的锁）；结果带回 normalized_from。"""
    db.init_db()
    seen: list = []

    async def fake_detect(kind, raw):
        seen.append(mgr._lock.locked())
        return {"value": "https://192.0.2.7:8443", "scheme_check": "https", "normalized_from": raw,
                "notice": lab_lib.NOTICE_HTTPS}
    monkeypatch.setattr(lab_lib, "detect_scheme", fake_detect)

    async def run():
        mgr.boot()
        return await mgr.set_endpoint("enspro", "192.0.2.7")
    mgr = make_mgr([])
    r = asyncio.run(run())
    assert seen == [False] and r["endpoint"]["value"] == "https://192.0.2.7:8443" and r["normalized_from"] == "192.0.2.7"
    assert db.get_setting("lab:endpoint:enspro") == "https://192.0.2.7:8443"
