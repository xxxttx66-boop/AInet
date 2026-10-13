import pytest
from starlette.websockets import WebSocketDisconnect

import config

PROFILE_KEYS = {"student", "progress", "mastery", "recent_attempts", "wrong_scenarios", "exams",
                "auto_login"}   # exams：里程碑 6；auto_login：里程碑 6.17


def login(client, login, password):
    return client.post("/api/login", json={"login": login, "password": password})


def test_default_admin_can_login(client):
    r = login(client, config.DEFAULT_LOGIN, config.DEFAULT_PASSWORD)
    assert r.status_code == 200
    assert r.json()["login"] == "admin"
    assert "password_plain" not in r.json()


def test_create_student_and_duplicate_rejected(client):
    r = client.post("/api/students", json={"login": "alice", "name": "爱丽丝", "password": "pw"})
    assert r.status_code == 201
    assert r.json()["login"] == "alice"
    r = client.post("/api/students", json={"login": "alice", "name": "X", "password": "y"})
    assert r.status_code == 409
    r = client.post("/api/students", json={"login": "", "name": "X", "password": "y"})
    assert r.status_code == 422
    r = client.post("/api/students", json={"login": "   ", "name": "X", "password": "y"})
    assert r.status_code == 422
    logins = [s["login"] for s in client.get("/api/students").json()]
    assert logins == ["admin", "alice"]
    assert all("password_plain" not in s for s in client.get("/api/students").json())


def test_login_wrong_password(client):
    r = login(client, "admin", "wrong")
    assert r.status_code == 401
    assert client.get("/api/me").status_code == 401
    assert login(client, "nobody", "admin").status_code == 401


def test_me_requires_login_and_has_full_structure(client):
    assert client.get("/api/me").status_code == 401
    login(client, "admin", "admin")
    r = client.get("/api/me")
    assert r.status_code == 200
    p = r.json()
    assert set(p) == PROFILE_KEYS
    assert {"id", "login", "name", "created_at"} <= set(p["student"])
    assert p["student"]["login"] == "admin"
    assert p["progress"] == {"completed_scenarios": 0}
    assert p["mastery"] == [] and p["recent_attempts"] == [] and p["wrong_scenarios"] == []


def test_logout_invalidates_session(client):
    login(client, "admin", "admin")
    token = client.cookies.get(config.SESSION_COOKIE)
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401
    # 即使旧 token 被重放，服务端也已作废
    client.cookies.set(config.SESSION_COOKIE, token)
    assert client.get("/api/me").status_code == 401


def test_switch_student(client):
    client.post("/api/students", json={"login": "bob", "name": "鲍勃", "password": "pw"})
    login(client, "admin", "admin")
    old_token = client.cookies.get(config.SESSION_COOKIE)
    assert client.get("/api/me").json()["student"]["login"] == "admin"
    assert login(client, "bob", "pw").status_code == 200
    assert client.get("/api/me").json()["student"]["login"] == "bob"
    # 切换后旧 token 作废
    import main
    assert old_token not in main.SESSIONS


def test_index_redirects_when_not_logged_in(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"  # 相对地址：部署在子路径下也能跳对
    assert client.get("/login").status_code == 200
    login(client, "admin", "admin")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "xterm" in r.text


def test_ws_rejected_when_not_logged_in(client):
    with client.websocket_connect("/ws/console/2000") as ws:
        msg = ws.receive_text()
        assert "未登录" in msg
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008


def test_ws_passes_auth_when_logged_in(client, monkeypatch):
    """登录后越过鉴权：用非白名单端口验证走到了白名单检查，不连真实设备。"""
    login(client, "admin", "admin")
    with client.websocket_connect("/ws/console/9999") as ws:
        msg = ws.receive_text()
        assert "白名单" in msg and "未登录" not in msg


def test_frontend_uses_relative_urls():
    # 前端不写死根路径，才能部署在反向代理的子路径（如 /ainet/）下
    import re
    from pathlib import Path
    front = Path(__file__).resolve().parents[2] / "frontend"
    bad = []
    for f in list(front.glob("*.html")) + list(front.glob("*.js")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""['"`]/(api|ws|vendor|static|login|profile)\b|href="/\"""", line):
                bad.append(f"{f.name}:{i}: {line.strip()}")
    assert not bad, "\n".join(bad)
