"""评分（spec §9）与 AI 分级提示（spec §7）：公式与折扣、分数截断、确定性信号、降级、Key 不落库、
提示泄露防护与回退、练习 API（结束后异步评分、结果轮询、重新评分、只能看自己的结果、提示 source）。
AI 客户端一律 mock，不访问网络。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import ai
import config
import db
import grading
import hints
import main
import scenarios as sc
from test_practice_api import FakeHub, env, hub, login  # noqa: F401  (env 是 fixture)
from practice_helpers import end_practice

KEY = "sk-gradingtestSECRETKEY123"
# 运行时用的是按种子展开后的场景（里程碑 6 参数化）；种子 0 = 默认值，与参数化之前的原题一致
CAMPUS = sc.expand(sc.load_dir(config.SCENARIOS_DIR)[0]["campus-ar0-ar2-link"], 0)
OSPF = sc.load_dir(config.SCENARIOS_DIR)[0]["ospf-2-1-r1-shutdown"]


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


class FakeAI:
    """假 AI 客户端：按顺序返回 data 或抛异常；记录请求。"""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    async def chat_json(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        if kw.get("validate"):
            kw["validate"](item)
        return ai.AIResult(data=item, model="deepseek-flash", thinking=kw["thinking"], attempts=1, elapsed=0.1,
                           usage={"total_tokens": 42})


GOOD = {"process": {"rate": 80, "reason": "先看了两端接口再改"}, "side": {"rate": 100, "reason": "无副作用"},
        "comment": {"summary": "不错", "good": ["先定位"], "problems": [], "suggestions": ["改完再 ping"]},
        "standard_path": "1. ping 2. display ip interface brief 3. 改地址"}


# ---- 公式 ----

def test_compute_passed():
    s = grading.compute(True, GOOD)
    assert s["result"] == 50 and s["process"] == 28 and s["side"] == 15 and s["total"] == 93
    assert s["dimensions"]["process"]["reason"] == "先看了两端接口再改"


def test_compute_failed_applies_discount_to_process_only():
    s = grading.compute(False, GOOD)
    assert s["result"] == 0 and s["process"] == round(28 * 0.6, 1) and s["side"] == 15
    assert s["total"] == round(0 + 28 * 0.6 + 15, 1)
    assert "×0.6" in s["dimensions"]["process"]["adjust"]


def test_compute_clamps_out_of_range_and_garbage():
    s = grading.compute(True, {"process": {"rate": 180}, "side": {"rate": -20}})
    assert s["process"] == 35 and s["side"] == 0
    s = grading.compute(True, {"process": "70分", "side": {"score": "50"}})
    assert s["process"] == 24.5 and s["side"] == 7.5


def test_compute_configurable_weights_discount_and_hint_penalty(monkeypatch):
    monkeypatch.setattr(config, "SCORE_WEIGHT_RESULT", 40)
    monkeypatch.setattr(config, "SCORE_WEIGHT_PROCESS", 40)
    monkeypatch.setattr(config, "SCORE_WEIGHT_SIDE", 20)
    monkeypatch.setattr(config, "SCORE_FAIL_DISCOUNT", 0.5)
    monkeypatch.setattr(config, "SCORE_HINT_PENALTY", 2)
    s = grading.compute(False, {"process": {"rate": 50}, "side": {"rate": 50}}, hints_used=3)
    assert s["process"] == round((20 - 6) * 0.5, 1) and s["side"] == 10 and s["result"] == 0


def test_compute_degraded_has_no_fake_scores():
    s = grading.compute(True, None)
    assert s["total"] == 50 and s["process"] is None and s["side"] is None


def test_standard_path_tolerates_key_variants():
    assert grading.standard_path({"standard_path": "1. a"}) == "1. a"
    assert grading.standard_path({"comment": {"standard_path": ["1. a", "2. b"]}}) == "1. a\n2. b"
    assert grading.standard_path({"标准思路": "x"}) == "x"
    assert grading.standard_path({}) == ""


def test_validate_score_requires_both_rates():
    grading.validate_score(GOOD)
    with pytest.raises(ValueError):
        grading.validate_score({"process": {"rate": 50}})
    with pytest.raises(ValueError):
        grading.validate_score({"process": {"rate": "无"}, "side": {"rate": 1}})


# ---- 确定性信号 ----

def _logs(*rows):
    return [{"device": d, "command": c, "output": o} for d, c, o in rows]


def test_signals_blind_config_dangerous_errors_and_down_logs():
    down = ("Oct  4 2026 11:02:03-08:00 AR2 %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface "
            "GigabitEthernet0/0/1 has entered the DOWN state.")
    pre_down = down.replace("0/0/1", "0/0/0")
    logs = _logs(("AR2", "display clock", pre_down),          # 注入余波：在第一条配置命令之前，不算
                 ("AR2", "system-view", "Enter system view"),
                 ("AR2", "interface GigabitEthernet0/0/1", ""),
                 ("AR2", "shutdown", down),
                 ("AR2", "ip adress 1.1.1.1 24", "        ^\nError: Unrecognized command"),
                 ("AR2", "ip address 10.0.12.2 24", ""))
    s = grading.extract_signals(CAMPUS, logs)
    assert s["first_config_index"] == 4 and s["readonly_before_first_config"] == 1
    assert s["config_without_prior_inspection"] is False
    assert [d["command"] for d in s["dangerous_or_extra_commands"]] == ["shutdown"]
    assert [e["command"] for e in s["error_commands"]] == ["ip adress 1.1.1.1 24"]
    assert len(s["down_logs_after_student_config"]) == 1 and "0/0/1" in s["down_logs_after_student_config"][0]["log"]


def test_signals_config_without_inspection_and_solution_not_dangerous():
    s = grading.extract_signals(OSPF, _logs(("R1", "sys", ""), ("R1", "int g0/0/0", ""), ("R1", "undo shutdown", "")))
    assert s["config_without_prior_inspection"] is True and s["dangerous_or_extra_commands"] == []


# ---- score_attempt：写库、降级、Key 不落库 ----

def _attempt(db_path, passed=1, logs=()):
    db.init_db()
    ids = db.sync_scenarios([CAMPUS])
    att = db.create_attempt(1, ids[CAMPUS["id"]])
    for d, c, o in logs:
        db.add_attempt_log(att["id"], d, c, o)
    db.update_attempt(att["id"], passed=passed, ended_at=att["started_at"], hints_used=1)
    return att["id"]


def test_score_attempt_success_writes_columns_and_json(db_path):
    aid = _attempt(db_path, 1, [("AR2", "display ip interface brief", "GE0/0/0 10.0.21.2/24 up up")])
    fake = FakeAI(GOOD)
    out = run(grading.score_attempt(aid, CAMPUS, client_factory=lambda: fake))
    assert out["status"] == "done" and out["scores"]["total"] == 93
    att = db.get_attempt(aid)
    assert (att["score_total"], att["score_result"], att["score_process"], att["score_side"]) == (93, 50, 28, 15)
    assert att["score_status"] == "done" and json.loads(att["score_json"])["comment"]["good"] == ["先定位"]
    req = fake.calls[0]
    assert "display ip interface brief" in req["user"] and "10.0.21.2/24" in req["user"]   # 命令与输出都送给 AI
    assert "AR0 与 AR2 互联不通" in req["user"] and CAMPUS["reference_path"] in req["user"]
    assert req["thinking"] == config.AI_SCORE_THINKING and req["max_tokens"] == config.AI_SCORE_MAX_TOKENS


def test_score_attempt_degrades_without_key_and_never_leaks_key(db_path, monkeypatch):
    aid = _attempt(db_path, 0)

    def boom():
        raise ai.AIError("auth", f"Incorrect API key provided: {KEY}")
    out = run(grading.score_attempt(aid, CAMPUS, client_factory=boom))
    assert out["status"] == "degraded" and "AI 点评不可用" in out["reason"] and "鉴权失败" in out["reason"]
    att = db.get_attempt(aid)
    assert att["score_total"] == 0 and att["score_process"] is None and att["score_side"] is None
    assert KEY not in att["score_json"]


def test_score_attempt_unexpected_exception_is_redacted(db_path):
    aid = _attempt(db_path, 1)

    class Weird:
        async def chat_json(self, *a, **kw):
            raise RuntimeError(f"boom with {KEY}")
    out = run(grading.score_attempt(aid, CAMPUS, client_factory=lambda: Weird()))
    assert out["status"] == "degraded" and KEY not in json.dumps(out, ensure_ascii=False)
    assert out["scores"]["total"] == 50


def test_score_attempt_parse_failure_degrades(db_path):
    aid = _attempt(db_path, 1)
    out = run(grading.score_attempt(aid, CAMPUS, client_factory=lambda: FakeAI(ai.AIError("parse", "找不到 JSON"))))
    assert out["status"] == "degraded" and "无法解析" in out["reason"] and out["scores"]["process"] is None


def test_score_attempt_ai_disabled(db_path, monkeypatch):
    monkeypatch.setattr(config, "AI_SCORING_ENABLED", False)
    aid = _attempt(db_path, 1)
    out = run(grading.score_attempt(aid, CAMPUS, client_factory=lambda: FakeAI(GOOD)))
    assert out["status"] == "degraded" and "关闭" in out["reason"]


# ---- 提示泄露防护 ----

def test_banned_terms_campus():
    b = hints.banned_terms(CAMPUS)
    assert "ip address 10.0.12.2 24" in b and "ip address 10.0.21.2 24" in b
    assert "ping -c 2 10.0.12.2" not in b                      # 判定命令是只读查看命令，不算泄露
    assert "10.0.21.2" in b and "10.0.12.2" not in b          # 工单里有的地址不禁
    assert "system-view" not in b and not any(x.startswith("interface") for x in b)


def test_leaks_normalizes_and_matches_whole_ip():
    b = hints.banned_terms(CAMPUS)
    assert hints.leaks("改成  IP ADDRESS 10.0.12.2   24 就好", b) == ["ip address 10.0.12.2 24"]
    assert hints.leaks("先执行 ping -c 2 10.0.12.2 看看", b) == []
    assert hints.leaks("对端地址是 10.0.21.2。", b) == ["10.0.21.2"]
    assert hints.leaks("对端地址 10.0.21.20 无关", b) == []
    assert hints.leaks("看看两端接口地址是否在同一网段", b) == []


def test_static_hints_level_1_2_do_not_leak():
    # 所有场景的静态第 1、2 级提示都不能含禁止内容（AI 超时 / 失败时会回退到它们）
    import scenarios as sc
    loaded, errors = sc.load_dir(str(Path(__file__).resolve().parents[2] / "scenarios"), [])
    assert errors == []
    items = list(loaded.values())
    assert len(items) >= 5
    for s in items:
        b = hints.banned_terms(s)
        for lvl in (1, 2):
            assert hints.leaks(s["hints"][lvl - 1], b) == [], (s["id"], lvl)


@pytest.mark.parametrize("level,text,source", [
    (1, "先看看两台路由器互联接口是不是在同一网段。", "ai"),
    (1, "把 AR2 改成 ip address 10.0.12.2 24 就好。", "static"),        # 泄露修复命令
    (2, "执行 ping -c 2 10.0.12.2 看看。", "ai"),                       # 判定命令是只读查看命令，允许
    (2, "AR2 的地址是 10.0.21.2，不对。", "static"),                     # 泄露错误配置值
    (3, "AR2 GE0/0/0 被配成 10.0.21.2/24，改为 ip address 10.0.12.2 24。", "ai"),   # 第 3 级可以揭示
])
def test_generate_hint_leak_filter(level, text, source):
    fake = FakeAI({"hint": text})
    r = run(hints.generate(CAMPUS, level, [], client_factory=lambda: fake))
    assert r["source"] == source
    assert r["text"] == (text if source == "ai" else CAMPUS["hints"][level - 1])
    req = fake.calls[0]
    assert req["thinking"] is False and req["deadline"] == config.AI_HINT_TIMEOUT
    if level < 3:
        assert "ip address 10.0.21.2 24" in req["user"]                 # 提示词里列出了禁止内容


def test_generate_hint_uses_student_logs_and_falls_back_on_error():
    logs = [{"device": "AR0", "command": "display ip interface brief", "output": "GE0/0/1 10.0.12.1/24 up up"}]
    fake = FakeAI({"hint": "去 AR2 上看看。"})
    r = run(hints.generate(CAMPUS, 1, logs, client_factory=lambda: fake))
    assert r["source"] == "ai" and "GE0/0/1 10.0.12.1/24" in fake.calls[0]["user"]
    r = run(hints.generate(CAMPUS, 2, logs, client_factory=lambda: FakeAI(ai.AIError("timeout"))))
    assert r["source"] == "static" and r["text"] == CAMPUS["hints"][1] and "超时" in r["reason"]


def test_generate_hint_slow_ai_times_out(monkeypatch):
    monkeypatch.setattr(config, "AI_HINT_TIMEOUT", 0.1)

    class Slow:
        async def chat_json(self, *a, **kw):
            await asyncio.sleep(5)
    t = time.time()
    r = run(hints.generate(CAMPUS, 1, [], client_factory=lambda: Slow()))
    assert r["source"] == "static" and time.time() - t < 3


# ---- 练习 API ----

@pytest.fixture
def aclient(env, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "ConsoleHub", FakeHub)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        c.app.state.practice.busy_retry = 0.05
        yield c
    main.SESSIONS.clear()


def _poll(c, aid, want=("done", "degraded", "failed")):
    end = time.time() + 5
    while time.time() < end:
        r = c.get(f"/api/practice/result/{aid}")
        if r.json()["status"] in want:
            return r.json()
        time.sleep(0.05)
    raise AssertionError(r.json())


def _start(c):
    r = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 200, r.text
    return r.json()["attempt_id"]


def test_end_schedules_async_scoring_and_result_api(aclient, monkeypatch):
    fake = FakeAI(GOOD)
    monkeypatch.setattr(ai, "make_client", lambda: fake)
    login(aclient)
    aid = _start(aclient)
    hub(aclient).fixed = True
    assert aclient.post("/api/practice/check").json()["passed"]
    e = end_practice(aclient)
    assert e["restored"] and e["score"]["status"] == "scoring"
    res = _poll(aclient, aid)
    assert res["status"] == "done" and res["score_total"] == 93 and res["passed"]
    assert res["detail"]["comment"]["summary"] == "不错" and res["detail"]["standard_path"]
    me = aclient.get("/api/me").json()
    assert me["recent_attempts"][0]["score_total"] == 93 and me["recent_attempts"][0]["score_status"] == "done"


def test_degraded_scoring_without_key_and_rescore(aclient, monkeypatch):
    login(aclient)
    aid = _start(aclient)
    end_practice(aclient)
    res = _poll(aclient, aid)
    assert res["status"] == "degraded" and res["score_total"] == 0 and res["score_process"] is None
    assert "AI 点评不可用" in res["detail"]["reason"]
    # 配好 AI 后重新评分
    monkeypatch.setattr(ai, "make_client", lambda: FakeAI(GOOD))
    r = aclient.post(f"/api/practice/rescore/{aid}")
    assert r.status_code == 200
    res = _poll(aclient, aid, ("done",))
    assert res["score_process"] == round(28 * 0.6, 1) and res["score_total"] == round(28 * 0.6 + 15, 1)


def test_result_permissions_and_rescore_rules(aclient):
    login(aclient)
    aid = _start(aclient)
    # 练习进行中不能重新评分
    assert aclient.post(f"/api/practice/rescore/{aid}").status_code == 409
    assert aclient.get(f"/api/practice/result/{aid}").json()["status"] == "none"
    end_practice(aclient)
    _poll(aclient, aid)
    # 别的学生看不到、也不能重评（404，与不存在一样）
    aclient.post("/api/students", json={"login": "bob", "name": "Bob", "password": "x"})
    assert aclient.post("/api/login", json={"login": "bob", "password": "x"}).status_code == 200
    assert aclient.get(f"/api/practice/result/{aid}").status_code == 404
    assert aclient.post(f"/api/practice/rescore/{aid}").status_code == 404
    assert aclient.get("/api/practice/result/99999").status_code == 404
    aclient.post("/api/logout")
    assert aclient.get(f"/api/practice/result/{aid}").status_code == 401
    assert aclient.post(f"/api/practice/rescore/{aid}").status_code == 401


def test_stale_scoring_marked_failed_on_startup(db_path):
    aid = _attempt(db_path, 1)
    db.update_attempt(aid, score_status="scoring")
    assert db.fail_stale_scoring("服务重启") == 1
    att = db.get_attempt(aid)
    assert att["score_status"] == "failed" and "服务重启" in att["score_json"]


def test_hint_api_returns_source_and_caches(aclient, monkeypatch):
    fake = FakeAI({"hint": "先比较两端接口所在网段。"})
    monkeypatch.setattr(ai, "make_client", lambda: fake)
    login(aclient)
    _start(aclient)
    h1 = aclient.post("/api/practice/hint", json={"level": 1}).json()
    assert h1 == {"level": 1, "text": "先比较两端接口所在网段。", "source": "ai", "hints_used": 1}
    again = aclient.post("/api/practice/hint", json={"level": 1}).json()
    assert again["text"] == h1["text"] and len(fake.calls) == 1        # 同级再看不重复调 AI
    fake.items = [{"hint": "AR2 的地址是 10.0.21.2，不对"}]              # 第 2 级泄露故障配置值 -> 静态
    h2 = aclient.post("/api/practice/hint", json={"level": 2}).json()
    assert h2["source"] == "static" and h2["text"] == CAMPUS["hints"][1] and h2["hints_used"] == 2
    assert "fallback_reason" in h2
    end_practice(aclient)
def test_slow_hint_after_end_does_not_resurrect_practice(aclient, monkeypatch):
    """提示生成中学员点了结束：迟到的提示不能把 practice_active 写回、也不能改 hints_used。"""
    import concurrent.futures

    gate = {"release": None}

    class SlowAI:
        async def chat_json(self, *a, **kw):
            if gate["release"] is None:        # 结束后的评分也会调 AI：共用同一个闸门，不覆盖提示那一次的
                gate["release"] = asyncio.Event()
            await gate["release"].wait()
            return ai.AIResult(data={"hint": "迟到的提示"}, model="m", thinking=False, attempts=1, elapsed=1)
    monkeypatch.setattr(ai, "make_client", lambda: SlowAI())
    login(aclient)
    aid = _start(aclient)
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        fut = ex.submit(aclient.post, "/api/practice/hint", json={"level": 1})
        end = time.time() + 5
        while gate["release"] is None and time.time() < end:
            time.sleep(0.02)
        assert end_practice(aclient)["restored"]

        async def release():
            gate["release"].set()
        aclient.portal.call(release)
        r = fut.result(timeout=10)
    assert r.status_code == 409
    assert db.get_setting("practice_active") is None
    assert db.get_attempt(aid)["hints_used"] == 0
    assert aclient.get("/api/practice/current").json()["active"] is False


def test_hint_api_static_when_no_key(aclient):
    login(aclient)
    _start(aclient)
    h = aclient.post("/api/practice/hint", json={"level": 1}).json()
    assert h["source"] == "static" and h["text"] == CAMPUS["hints"][0]
    end_practice(aclient)