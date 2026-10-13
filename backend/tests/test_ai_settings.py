"""AI 接口设置（里程碑 6.18，学员菜单「AI 设置」）：URL / 模型 / Key 的校验、secrets 文件原子更新（保留其他行）、
优先级（页面保存 > 环境变量 > secrets 文件；AINET_LAB_FORCE=1 时环境变量优先）、保存后立即生效、Key 绝不回显 / 不入库 /
不进日志、测试连接（httpx MockTransport，不发真实外网请求）、接口 401 / 422、练习进行中照样可改。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

import ai
import config
import db
import main

KEY = "sk-test0123456789abcd"
KEY2 = "sk-other99999999wxyz"


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    p = tmp_path / "data" / "secrets.env"
    monkeypatch.setenv(ai.SECRETS_ENV, str(p))
    return p


# ---------------- 校验与文件 ----------------

def test_normalize_values():
    assert ai.normalize_base_url(" https://api.example.com/v1/ ") == "https://api.example.com/v1"
    assert ai.normalize_base_url("HTTP://10.0.0.5:8000") == "http://10.0.0.5:8000"
    for bad in ("", "ftp://x", "https://u:p@x", "https://x/?a=1", "https://x/#f", "api.deepseek.com", "https:// x"):
        with pytest.raises(ValueError):
            ai.normalize_base_url(bad)
    assert ai.normalize_model("deepseek-flash") == "deepseek-flash"
    for bad in ("", "a b", "x" * 101, "-x"):
        with pytest.raises(ValueError):
            ai.normalize_model(bad)
    assert ai.normalize_key(f"  {KEY} ") == KEY
    for bad in ("short", "sk-has space1234", 'sk-"quoted"1234', "sk-a#comment1234", "sk-\n12345678"):
        with pytest.raises(ValueError):
            ai.normalize_key(bad)
    assert ai.mask_tail(KEY) == "sk-***abcd" and ai.mask_tail("abc") == "***" and ai.mask_tail(None) == ""


def test_write_key_file_keeps_other_lines(secrets_file):
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text("﻿# 注释\nENSPRO_DEVICE_PASSWORD=pw1\nexport DEEPSEEK_API_KEY=old1\nX=1\n"
                            "DEEPSEEK_API_KEY=old2\n", encoding="utf-8")
    ai.write_key_file(str(secrets_file), KEY)
    lines = secrets_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["# 注释", "ENSPRO_DEVICE_PASSWORD=pw1", f"DEEPSEEK_API_KEY={KEY}", "X=1"]
    assert ai.read_secrets_file(str(secrets_file)) == KEY
    ai.write_key_file(str(secrets_file), None)
    assert secrets_file.read_text(encoding="utf-8").splitlines() == ["# 注释", "ENSPRO_DEVICE_PASSWORD=pw1", "X=1"]
    assert not [p for p in secrets_file.parent.iterdir() if p.name.startswith(".secrets-")]   # 没有残留临时文件
    new = secrets_file.parent / "sub" / "new.env"
    ai.write_key_file(str(new), KEY2)
    assert new.read_text(encoding="utf-8") == f"DEEPSEEK_API_KEY={KEY2}\n"


def test_key_priority_page_env_file_force(secrets_file, db_path, monkeypatch):
    db.init_db()
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text(f"DEEPSEEK_API_KEY={KEY2}\n", encoding="utf-8")
    assert ai.key_info()[:2] == (KEY2, "file")
    monkeypatch.setenv(ai.KEY_ENV, "sk-fromenv0000000env1")
    assert ai.key_info()[:2] == ("sk-fromenv0000000env1", "env")           # 原有顺序：环境变量 > 文件
    ai.apply_settings(api_key=KEY)
    assert ai.key_info()[:2] == (KEY, "page")                              # 页面保存的优先于环境变量
    ai.load_settings()                                                     # 重启后仍是页面的
    assert ai.key_info()[:2] == (KEY, "page")
    monkeypatch.setattr(config, "LAB_FORCE", True)
    assert ai.key_info()[1] == "force"                                     # AINET_LAB_FORCE=1：环境变量优先
    monkeypatch.setattr(config, "LAB_FORCE", False)
    ai.apply_settings(clear_key=True)
    assert ai.key_info()[:2] == ("sk-fromenv0000000env1", "env")
    assert ai.read_secrets_file(str(secrets_file)) is None


def test_url_model_priority_and_immediate_effect(db_path, monkeypatch, secrets_file):
    db.init_db()
    ai.load_settings()
    assert ai.effective_base_url() == (config.AI_BASE_URL.rstrip("/"), "default")
    monkeypatch.setenv(ai.URL_ENV, "https://env.example.com")
    monkeypatch.setattr(config, "AI_BASE_URL", "https://env.example.com")
    assert ai.effective_base_url() == ("https://env.example.com", "env")
    assert ai.apply_settings(base_url="https://page.example.com/v1", model="m-page") == ["base_url", "model"]
    assert ai.effective_base_url() == ("https://page.example.com/v1", "db") and ai.effective_model() == ("m-page", "db")
    assert db.get_setting("ai:base_url") == "https://page.example.com/v1" and db.get_setting("ai:model") == "m-page"
    monkeypatch.setattr(config, "LAB_FORCE", True)
    assert ai.effective_base_url()[1] == "force" and ai.effective_model()[1] == "db"   # 没设 AINET_AI_MODEL
    monkeypatch.setattr(config, "LAB_FORCE", False)
    # 客户端按 (Key, URL, 模型) 缓存：保存后下一次取就是新的
    monkeypatch.setattr(ai, "make_client", ai._default_make_client)
    ai.apply_settings(api_key=KEY)
    c1 = ai.get_client()
    assert (c1.base_url, c1.model) == ("https://page.example.com/v1", "m-page") and ai.get_client() is c1
    ai.apply_settings(model="m2")
    c2 = ai.get_client()
    assert c2 is not c1 and c2.model == "m2" and c1.model == "m-page"       # 旧客户端（已发出的请求）不受影响
    assert KEY not in repr(c2)
    ai.apply_settings(base_url=None, model=None)
    assert ai.effective_base_url()[1] == "env" and db.get_setting("ai:base_url") is None


# ---------------- 接口 ----------------

@pytest.fixture
def app(db_path, secrets_file, caplog):
    from fastapi.testclient import TestClient

    caplog.set_level(logging.INFO, logger="ainet")
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        yield c
    main.SESSIONS.clear()


def _login(c):
    assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200


def _all_settings_values() -> str:
    conn = db.connect()
    try:
        return "\n".join(f"{k}={v}" for k, v in conn.execute("SELECT key, value FROM app_settings"))
    finally:
        conn.close()


def test_api_settings_save_and_never_echo_key(app, secrets_file, caplog):
    c = app
    assert c.get("/api/ai/settings").status_code == 401
    assert c.post("/api/ai/settings", json={"api_key": KEY}).status_code == 401
    assert c.post("/api/ai/settings/test", json={}).status_code == 401
    _login(c)
    d = c.get("/api/ai/settings").json()
    assert d["key"]["configured"] is False and d["key"]["source"] == "none" and d["base_url"]["source"] == "default"
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text("ENSPRO_DEVICE_PASSWORD=pw1\n", encoding="utf-8")
    r = c.post("/api/ai/settings", json={"api_key": KEY, "base_url": "https://llm.example.com/v1/", "model": "m-x"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert sorted(d["changed"]) == ["api_key", "base_url", "model"]
    assert d["key"] == {**d["key"], "configured": True, "source": "page", "masked": "sk-***abcd", "page": True}
    assert d["base_url"]["value"] == "https://llm.example.com/v1" and d["model"]["value"] == "m-x"
    assert KEY not in r.text and KEY not in c.get("/api/ai/settings").text
    assert secrets_file.read_text(encoding="utf-8").splitlines() == ["ENSPRO_DEVICE_PASSWORD=pw1", f"DEEPSEEK_API_KEY={KEY}"]
    assert KEY not in _all_settings_values() and db.get_setting("ai:key_page") == "1"
    # 只改模型：Key 不变；api_key 空串 = 不改
    r = c.post("/api/ai/settings", json={"model": "m-y", "api_key": ""})
    assert r.json()["changed"] == ["model"] and ai.key_info()[0] == KEY
    # 校验
    for body in ({"base_url": "ftp://x"}, {"model": "a b"}, {"api_key": "has space 123456"}, {}):
        assert c.post("/api/ai/settings", json=body).status_code == 422, body
    # 练习进行中照样可改
    pm = c.app.state.practice
    pm.active = SimpleNamespace(mode="practice", scenario={"id": "x"})
    try:
        assert c.post("/api/ai/settings", json={"api_key": KEY2}).status_code == 200
    finally:
        pm.active = None
    assert ai.key_info()[0] == KEY2
    r = c.post("/api/ai/settings", json={"clear_key": True, "base_url": None})
    assert r.json()["key"]["configured"] is False and r.json()["base_url"]["source"] == "default"
    assert secrets_file.read_text(encoding="utf-8").splitlines() == ["ENSPRO_DEVICE_PASSWORD=pw1"]
    assert KEY not in caplog.text and KEY2 not in caplog.text


def _transport(handler, seen):
    def h(req):
        seen.append((req.method, str(req.url), req.headers.get("authorization")))
        return handler(req)
    return httpx.MockTransport(h)


def test_api_settings_test_connection(app, monkeypatch):
    c = app
    _login(c)
    assert c.post("/api/ai/settings/test", json={}).status_code == 422         # 没有任何 Key
    seen: list = []
    monkeypatch.setattr(ai, "_TEST_TRANSPORT", _transport(
        lambda req: httpx.Response(200, json={"data": [{"id": "deepseek-flash"}, {"id": "m2"}]}), seen))
    r = c.post("/api/ai/settings/test", json={"base_url": "https://llm.example.com/v1/", "api_key": KEY})
    d = r.json()
    assert r.status_code == 200 and d["ok"] is True and d["models"] == 2 and d["model_listed"] is True
    assert seen == [("GET", "https://llm.example.com/v1/models", f"Bearer {KEY}")]
    assert d["key_masked"] == "sk-***abcd" and KEY not in r.text
    assert c.get("/api/ai/settings").json()["key"]["configured"] is False      # 测试不保存
    d = c.post("/api/ai/settings/test", json={"api_key": KEY, "model": "nope"}).json()
    assert d["ok"] is True and d["model_listed"] is False and "不在" in d["message"]
    # 鉴权失败：原因可读、不含 Key
    monkeypatch.setattr(ai, "_TEST_TRANSPORT", _transport(
        lambda req: httpx.Response(401, json={"error": {"message": f"Incorrect API key {KEY}"}}), []))
    r = c.post("/api/ai/settings/test", json={"api_key": KEY})
    assert r.json()["ok"] is False and r.json()["kind"] == "auth" and KEY not in r.text
    # 没有 /models：改发 1 token 补全
    seen = []

    def no_models(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(404)
        assert req.read() and b'"max_tokens":1' in req.read().replace(b" ", b"")
        return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
    monkeypatch.setattr(ai, "_TEST_TRANSPORT", _transport(no_models, seen))
    d = c.post("/api/ai/settings/test", json={"api_key": KEY, "base_url": "http://10.0.0.9:8000"}).json()
    assert d["ok"] is True and [s[1] for s in seen] == ["http://10.0.0.9:8000/models",
                                                       "http://10.0.0.9:8000/chat/completions"]
    # 连不上

    def down(req):
        raise httpx.ConnectError("refused", request=req)
    monkeypatch.setattr(ai, "_TEST_TRANSPORT", httpx.MockTransport(down))
    d =c.post("/api/ai/settings/test", json={"api_key": KEY}).json()
    assert d["ok"] is False and d["kind"] == "network"
    assert c.post("/api/ai/settings/test", json={"api_key": KEY, "base_url": "nope"}).status_code == 422
