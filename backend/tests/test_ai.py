"""ai.py：稳健 JSON 解析、分数截断、Key 读取顺序、Key 脱敏、客户端重试与错误分类（假 SDK，不访问网络）。"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

import ai

KEY = "sk-test1234567890abcdefSECRET"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


# ---- 解析 ----

@pytest.mark.parametrize("text", [
    '{"hint": "看接口"}',
    '好的，结果如下：\n{"hint": "看接口"}\n希望有帮助',
    '```json\n{"hint": "看接口"}\n```',
    '```\n{"hint": "看接口"}\n```',
    '前言 {不是json} 然后 {"hint": "看接口"} 结尾',
])
def test_parse_json_loose_variants(text):
    assert ai.parse_json_loose(text) == {"hint": "看接口"}


def test_parse_json_tolerates_raw_newlines_in_strings():
    assert ai.parse_json_loose('{"standard_path": "1. 先 ping\n2. 再看接口"}') == {"standard_path": "1. 先 ping\n2. 再看接口"}


def test_parse_json_nested_and_braces_in_strings():
    t = '说明：{"a": {"b": "x}y{"}, "c": [1, 2]} 多余'
    assert ai.parse_json_loose(t) == {"a": {"b": "x}y{"}, "c": [1, 2]}


@pytest.mark.parametrize("text", ["", "   ", "没有 JSON", "[1, 2]", '{"a": 1'])
def test_parse_json_failures(text):
    with pytest.raises(ValueError):
        ai.parse_json_loose(text)


@pytest.mark.parametrize("v,expect", [(50, 50), (150, 100), (-5, 0), ("85", 85), ("85分", 85), ("90/100", 90),
                                      (72.5, 72.5), (None, None), ("无", None), (True, None),
                                      (float("nan"), None)])
def test_clamp(v, expect):
    assert ai.clamp(v, 0, 100) == expect


# ---- Key 读取顺序与脱敏 ----

def test_key_order_env_then_explicit_file_then_defaults(monkeypatch, tmp_path):
    f1 = tmp_path / "explicit.env"
    f1.write_text("# 注释\nexport DEEPSEEK_API_KEY='sk-fromexplicit'\r\n", encoding="utf-8")
    d1 = tmp_path / "data_secrets.env"
    d1.write_text("﻿DEEPSEEK_API_KEY=sk-fromdata\r\n", encoding="utf-8")       # 带 BOM、CRLF
    d2 = tmp_path / "home_secrets.env"
    d2.write_text('OTHER=1\nDEEPSEEK_API_KEY="sk-fromhome"\n', encoding="utf-8")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fromenv")
    monkeypatch.setenv("AINET_SECRETS_FILE", str(f1))
    assert ai.load_api_key()[0] == "sk-fromenv"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    k, src = ai.load_api_key()
    assert k == "sk-fromexplicit" and "explicit.env" in src and k not in src
    # 指定了 AINET_SECRETS_FILE 但文件不存在：不再去找缺省位置
    monkeypatch.setenv("AINET_SECRETS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(ai.os.path, "expanduser", lambda p: str(tmp_path / "nohome"))
    assert ai.load_api_key()[0] is None
    # 缺省：backend/data/secrets.env 优先于 ~/.config/ainet/secrets.env
    monkeypatch.delenv("AINET_SECRETS_FILE")
    monkeypatch.setattr(ai, "secrets_candidates", lambda: [str(d1), str(d2)])
    assert ai.load_api_key()[0] == "sk-fromdata"
    d1.unlink()
    assert ai.load_api_key()[0] == "sk-fromhome"


def test_default_candidates(monkeypatch):
    monkeypatch.delenv("AINET_SECRETS_FILE", raising=False)
    c = ai.secrets_candidates()
    assert c[0].replace("\\", "/").endswith("backend/data/secrets.env")
    assert c[1].replace("\\", "/").endswith(".config/ainet/secrets.env")


def test_mask_and_redact():
    assert ai.mask_key(KEY) == "sk-***"
    assert ai.mask_key(None) == "(无)"
    s = ai.redact(f"Incorrect API key provided: {KEY}", KEY)
    assert KEY not in s and "sk-***" in s
    assert "sk-abcdef123456" not in ai.redact("token sk-abcdef123456 bad")     # 形似 key 的串也脱敏
    e = ai.AIError("auth", f"bad key {KEY}")
    assert KEY not in str(e) and KEY not in e.message


def test_make_client_without_key_raises_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "make_client", ai._default_make_client)
    with pytest.raises(ai.AIError) as ei:
        ai.get_client()
    assert ei.value.kind == "no_key"


def test_client_repr_and_logs_do_not_contain_key(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setattr(ai, "make_client", ai._default_make_client)
    caplog.set_level(logging.DEBUG)
    c = ai.get_client()
    assert KEY not in repr(c)
    assert KEY not in caplog.text and "sk-***" in caplog.text


# ---- 客户端：假 SDK ----

def _resp(content, finish="stop", reasoning=None):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))
    return SimpleNamespace(model="deepseek-flash", usage=usage,
                           choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


_REQ = httpx.Request("POST", "https://api.deepseek.com/chat/completions")


def _status_exc(cls, code):
    return cls(f"Error code: {code} - key {KEY}", response=httpx.Response(code, request=_REQ), body=None)


class FakeSDK:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client(script, **kw):
    sdk = FakeSDK(script)
    return ai.DeepSeekClient(KEY, sdk=sdk, backoff=0.01, max_retries=kw.pop("max_retries", 2), **kw), sdk


def test_chat_json_request_shape_and_usage():
    c, sdk = client([_resp('{"hint": "x"}', reasoning=3)])
    r = run(c.chat_json("sys", "user", thinking=False, max_tokens=100, timeout=5))
    assert r.data == {"hint": "x"} and r.attempts == 1 and r.usage["total_tokens"] == 15
    kw = sdk.calls[0]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["extra_body"] == {"thinking": {"type": "disabled"}} and kw["max_tokens"] == 100
    assert kw["messages"][0] == {"role": "system", "content": "sys"}


def test_retry_on_timeout_then_success():
    c, sdk = client([openai.APITimeoutError(request=_REQ), _resp('{"a": 1}')])
    r = run(c.chat_json("s", "u", thinking=False, max_tokens=10, timeout=5))
    assert r.data == {"a": 1} and r.attempts == 2


def test_retries_are_bounded():
    c, sdk = client([openai.APIConnectionError(request=_REQ)] * 5, max_retries=2)
    with pytest.raises(ai.AIError) as ei:
        run(c.chat_json("s", "u", thinking=False, max_tokens=10, timeout=5))
    assert ei.value.kind == "network" and len(sdk.calls) == 3


@pytest.mark.parametrize("exc,kind,calls", [
    (_status_exc(openai.AuthenticationError, 401), "auth", 1),
    (_status_exc(openai.RateLimitError, 429), "rate_limit", 3),
    (_status_exc(openai.InternalServerError, 503), "server", 3),
    (_status_exc(openai.BadRequestError, 400), "bad_request", 1),
    (_status_exc(openai.APIStatusError, 402), "quota", 1),
])
def test_error_classification_and_no_key_leak(exc, kind, calls):
    c, sdk = client([exc] * 3)
    with pytest.raises(ai.AIError) as ei:
        run(c.chat_json("s", "u", thinking=False, max_tokens=10, timeout=5))
    assert ei.value.kind == kind and len(sdk.calls) == calls
    assert KEY not in str(ei.value)


def test_parse_failure_retries_once_with_thinking_off_then_raises():
    c, sdk = client([_resp("我不会输出 JSON"), _resp("还是不会")])
    with pytest.raises(ai.AIError) as ei:
        run(c.chat_json("s", "u", thinking=True, max_tokens=10, timeout=5))
    assert ei.value.kind == "parse" and len(sdk.calls) == 2
    assert sdk.calls[0]["extra_body"]["thinking"]["type"] == "enabled"
    assert sdk.calls[1]["extra_body"]["thinking"]["type"] == "disabled"


def test_empty_content_or_length_counts_as_parse_failure_and_recovers():
    c, sdk = client([_resp("", finish="length"), _resp('```json\n{"ok": true}\n```')])
    r = run(c.chat_json("s", "u", thinking=True, max_tokens=10, timeout=5))
    assert r.data == {"ok": True} and r.thinking is False and r.attempts == 2


def test_validate_failure_triggers_parse_retry():
    def need_hint(d):
        if "hint" not in d:
            raise ValueError("缺少 hint")
    c, sdk = client([_resp('{"other": 1}'), _resp('{"hint": "y"}')])
    r = run(c.chat_json("s", "u", thinking=False, max_tokens=10, timeout=5, validate=need_hint))
    assert r.data == {"hint": "y"}
