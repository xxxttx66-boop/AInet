"""考试模式（里程碑 6）：模板管理、状态机（开始 → 作答 → 提交 → 重试 → 下一题 → 超时 → 交卷）、计时（假时钟）、
系统操作时间顺延、服务重启后恢复、禁止提示与提示扣分、答案字段不泄露（遍历考试期间全部接口）、评分汇总、
命令回放、权限（只能看自己的考试）、迁移幂等、与练习互斥。

设备用 ExamHub 模拟（不连任何真实 console）：注入后 ping 判定失败，测试里置 hub.fixed = True 模拟学员修好。
AI 一律不可用（conftest 屏蔽），评分降级为只给结果分，便于断言分数。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

import config
import db
import main
import scenarios as sc
from console_hub import HiddenResult
from practice import secret_strings
from practice_helpers import end_practice

TOPOS = Path(__file__).resolve().parent / "fixtures" / "topos"
FACTORY = (Path(__file__).resolve().parent / "fixtures" / "configs" / "ar1220_factory.cfg").read_text(encoding="utf-8")
AR0, AR2, LSW0 = ("B3B8A475-25C8-4a6c-B29D-F49A6F4F9F0E", "14D97C46-57BE-4d7a-BFB6-696F4390C76B",
                  "DE880FB4-C0C7-4dc4-848F-7BEB833A9BB4")
P_AR0, P_LSW0, P_AR2 = 2000, 2001, 2003
Q3 = ["campus-ar0-ar2-link", "campus-lsw0-access-vlan", "campus-ar2-return-route"]


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


class ExamHub:
    """假 ConsoleHub：配置事务里出现本题注入命令即「故障生效」，restore（undo）即恢复；判定 ping 按故障态给结果。"""

    def __init__(self, *a, **kw) -> None:
        self.calls: list[tuple[int, str, list[str]]] = []
        self.listeners = []
        self.fault = False
        self.fixed = False
        self.busy = 0                 # 接下来 n 次隐藏事务返回 busy
        self.restore_busy = 0         # 接下来 n 次 restore 事务返回 busy（学员停在系统视图；reclaim 收回终端后清零）
        self.restore_error = None     # restore 事务一直返回的 error（设备连不上等，reclaim 也救不了）
        self.reclaimed: list[int] = []
        self.inject_busy = 0
        self.inject_error = None      # 注入失败时的报错文本（None = 带出命令原文）
        self.config_delay = 0.0       # 每个配置事务推进假时钟的秒数（模拟注入 / 复位耗时）
        self.clock: Clock | None = None
        self.pm = None                # PracticeManager（按当前题的 inject / restore 认出注入与还原事务）
        self.dropped = []
        self.pinned: set[int] = set()

    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        self.listeners.remove(fn)

    def emit(self, event, port, data):
        for fn in self.listeners:
            fn(event, port, data)

    async def run_hidden(self, port, commands, kind="check", cmd_timeout=None, tolerate_errors=False,
                         groups=None, confirm=False):
        if groups is not None:
            commands = [c for cmds, _ in groups for c in cmds]
        self.calls.append((port, kind, list(commands)))
        if self.busy:
            self.busy -= 1
            return HiddenResult("busy", "学员正在输入命令")
        if commands == ["display current-configuration"]:
            return HiddenResult("ok", outputs=[FACTORY])
        if kind == "config":
            if self.clock is not None:
                self.clock.t += self.config_delay
            a = self.pm.active if self.pm is not None else None
            sc_ = a.scenario if a is not None else {}
            if groups is None and commands in (sc_.get("restore") or {}).values():
                if self.restore_error:
                    return HiddenResult("error", self.restore_error)
                if self.restore_busy:
                    self.restore_busy -= 1
                    return HiddenResult("busy", "学员处于系统视图，暂不注入（请先在终端回到用户视图 <...>）")
                self.fault = False
                self.fixed = False
            elif groups is None and commands in (sc_.get("inject") or {}).values():
                if self.inject_busy:
                    self.inject_busy -= 1
                    return HiddenResult("error", self.inject_error or f"命令执行失败：{commands[-1]}：Error: simulated")
                self.fault = True
            return HiddenResult("ok")
        ok = (not self.fault) or self.fixed
        return HiddenResult("ok", outputs=[f"  2 packet(s) transmitted\n  {2 if ok else 0} packet(s) received"
                                           for _ in commands])

    def drop_subscribers(self, ports, message=""):
        self.dropped.append(set(ports))

    def reset_replay(self, port):
        pass

    def pin(self, ports):
        self.pinned |= set(ports)

    def unpin(self, ports):
        self.pinned -= set(ports)

    def flush_output(self, port):
        pass

    async def reclaim(self, port):
        self.reclaimed.append(port)
        self.restore_busy = 0         # 强制收回终端：学员不再停在系统视图
        return HiddenResult("ok", prompt="<Huawei>")

    async def close(self):
        pass


@pytest.fixture
def exam_env(monkeypatch, db_path):
    monkeypatch.setattr(config, "TOPO_DIRS", [str(TOPOS)])
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", f"{AR0},{AR2},{LSW0}")
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")
    monkeypatch.setattr(config, "EXAM_RESET_RETRY", 5)
    monkeypatch.setattr(main, "ConsoleHub", ExamHub)


def open_client(clock: Clock):
    from fastapi.testclient import TestClient

    c = TestClient(main.app)
    c.__enter__()
    c.app.state.practice.op_deadline = 0.3
    c.app.state.practice.busy_retry = 0.02
    c.app.state.practice.end_retry_wait = 0.01
    c.app.state.exam.clock = clock
    c.app.state.hub.clock = clock
    c.app.state.hub.pm = c.app.state.practice
    return c


@pytest.fixture
def ex(exam_env):
    main.SESSIONS.clear()
    clock = Clock()
    c = open_client(clock)
    c.clock = clock
    yield c
    c.__exit__(None, None, None)
    main.SESSIONS.clear()


def login(c, who="admin", pw="admin"):
    r = c.post("/api/login", json={"login": who, "password": pw})
    assert r.status_code == 200, r.text


def test_capacity_is_checked_again_before_next_exam_question(ex):
    login(ex)
    r = ex.post("/api/exam/templates", json={"name": "capacity boundary", "config": {
        "questions": [{"scenario": sid} for sid in Q3[:2]], "duration_s": 900}})
    assert r.status_code == 201, r.text
    assert ex.post("/api/exam/start", json={"template_id": r.json()["id"]}).json()["question"]["status"] == "active"
    ex.app.state.practice.capacity_guard = lambda s: "设备上限已改变" if s["id"] == Q3[1] else None
    result = ex.post("/api/exam/next").json()
    assert result["current"] == 1 and result["question"]["status"] == "pending"
    assert "设备上限" in result["question"]["error"]
    assert ex.app.state.practice.active is None


def hub(c) -> ExamHub:
    return c.app.state.hub


def tick(c):
    c.portal.call(c.app.state.exam.tick)


def wait_scored(c, exam_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        atts = db.exam_attempts(exam_id)
        if atts and all(a["score_status"] not in (None, "scoring") for a in atts):
            return atts
        time.sleep(0.05)
    raise AssertionError("评分未完成")


def make_template(c, **cfg) -> int:
    body = {"questions": [{"scenario": s, "seed": None} for s in Q3], "duration_s": 3600,
            "question_duration_s": 600, "retries": 1, **cfg}
    r = c.post("/api/exam/templates", json={"name": "测验", "config": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def start(c, tid) -> dict:
    r = c.post("/api/exam/start", json={"template_id": tid})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 模板 ----------------

def test_builtin_template_seeded_once_and_not_recreated(ex):
    login(ex)
    ts = ex.get("/api/exam/templates").json()["templates"]
    assert [t["name"] for t in ts] == ["Campus 基础排障测验"]
    cfg = ts[0]["config"]
    assert [q["scenario"] for q in cfg["questions"]] == Q3 and cfg["auto_judge"] is False
    assert ex.delete(f"/api/exam/templates/{ts[0]['id']}").status_code == 200
    ex.app.state.exam.ensure_builtin()
    assert ex.get("/api/exam/templates").json()["templates"] == []


def test_template_crud_and_validation(ex):
    login(ex)
    tid = make_template(ex, hints={"allowed": True, "penalty": 3})
    t = ex.get(f"/api/exam/templates/{tid}").json()
    assert t["config"]["hints"] == {"allowed": True, "penalty": 3, "max_level": 3}
    assert t["config"]["questions"][0] == {"scenario": Q3[0], "seed": None, "weight": 1}
    r = ex.put(f"/api/exam/templates/{tid}", json={"name": "改名", "config": {
        "questions": [{"scenario": Q3[0], "seed": 7, "weight": 2}], "duration_s": 60}})
    assert r.status_code == 200 and r.json()["name"] == "改名" and r.json()["config"]["questions"][0]["seed"] == 7
    bad = [({"questions": []}, "至少一题"), ({"questions": ["nope"]}, "场景不存在"),
           ({"questions": [Q3[0]], "duration_s": 0}, "duration_s"), ({"questions": [Q3[0]], "retries": -1}, "retries"),
           ({"questions": [Q3[0]], "foo": 1}, "未知的配置项"), ({"questions": [Q3[0]], "weights": {"result": 0}}, "weights"),
           ({"questions": [{"scenario": Q3[0], "seed": "x"}]}, "seed"), ({"questions": [Q3[0]], "shuffle": "yes"}, "shuffle")]
    for cfg, word in bad:
        r = ex.post("/api/exam/templates", json={"name": "x", "config": cfg})
        assert r.status_code == 422 and word in r.json()["detail"], (cfg, r.text)
    assert ex.delete("/api/exam/templates/99999").status_code == 404
    assert ex.get("/api/exam/templates/99999").status_code == 404


# ---------------- 状态机 ----------------

def test_full_state_machine_submit_retry_next_timeout_finish(ex):
    login(ex)
    tid = make_template(ex)
    st = start(ex, tid)
    eid = st["exam_id"]
    q = st["question"]
    assert st["total"] == 3 and st["current"] == 0 and q["status"] == "active"
    assert q["max_submissions"] == 2 and q["remaining_submissions"] == 2
    assert {d["name"] for d in q["devices"]} == {"AR0", "AR2"}
    assert hub(ex).fault and ex.app.state.practice.active.mode == "exam"
    assert ex.app.state.practice.active.auto_judge is False
    # 第 1 题：没修好就提交 → 未通过、计一次；修好后重试 → 通过并锁定
    r = ex.post("/api/exam/submit").json()
    assert r["submission"] == {"status": "ok", "counted": True, "n": 1, "at": r["submission"]["at"],
                               "passed": False, "passed_items": 0, "total": 1}
    assert r["question"]["remaining_submissions"] == 1 and not r["question"]["locked"]
    hub(ex).fixed = True
    r = ex.post("/api/exam/submit").json()
    assert r["submission"]["passed"] is True and r["question"]["locked"] and r["question"]["passed"]
    assert ex.post("/api/exam/submit").status_code == 409                      # 已通过不能再提交
    # 下一题：第 1 题复位、评分，第 2 题开始
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 1 and st["questions"][0]["status"] == "done" and st["question"]["status"] == "active"
    assert {d["name"] for d in st["question"]["devices"]} == {"AR0", "LSW0"}
    # 第 2 题：两次都失败 → 次数用完锁定
    assert ex.post("/api/exam/submit").json()["submission"]["passed"] is False
    r = ex.post("/api/exam/submit").json()
    assert r["submission"]["passed"] is False and r["question"]["locked"] and r["question"]["remaining_submissions"] == 0
    r = ex.post("/api/exam/submit")
    assert r.status_code == 409 and "次数已用完" in r.json()["detail"]
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 2 and st["question"]["status"] == "active"
    # 第 3 题：不作答，单题超时 → 系统代为提交一次 → 复位 → 最后一题 → 自动交卷
    ex.clock.t += 599
    tick(ex)
    assert ex.get("/api/exam/current").json()["current"] == 2
    ex.clock.t += 2
    tick(ex)
    cur = ex.get("/api/exam/current").json()
    assert cur["active"] is False and cur["last_exam_id"] == eid
    assert ex.app.state.practice.active is None
    atts = wait_scored(ex, eid)
    assert [a["mode"] for a in atts] == ["exam"] * 3 and [a["exam_index"] for a in atts] == [0, 1, 2]
    assert [a["passed"] for a in atts] == [1, 0, 0] and all(a["exam_id"] == eid for a in atts)
    rep = ex.get(f"/api/exam/{eid}/report").json()
    qs = rep["questions"]
    assert [q["status"] for q in qs] == ["done", "done", "done"]
    assert [len(q["submissions"]) for q in qs] == [2, 2, 1]
    assert qs[2]["timed_out"] and qs[2]["auto_submitted"] and not qs[0]["timed_out"]
    assert [q["score_total"] for q in qs] == [50, 0, 0]           # AI 不可用：只有结果分
    assert rep["score_total"] == pytest.approx(16.7) and rep["score_status"] == "degraded"
    assert all(q["duration_s"] is not None and q["commands"] == 0 for q in qs)
    prof = ex.get("/api/me").json()
    assert prof["exams"][0]["id"] == eid and prof["exams"][0]["score_total"] == pytest.approx(16.7)


def test_total_time_up_auto_finishes_and_marks_rest_unanswered(ex):
    login(ex)
    tid = make_template(ex, duration_s=300, question_duration_s=None)
    st = start(ex, tid)
    hub(ex).fixed = True                         # 学员修好了但没点提交
    ex.clock.t += 301
    tick(ex)
    assert ex.get("/api/exam/current").json()["active"] is False
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert [q["status"] for q in rep["questions"]] == ["done", "unanswered", "unanswered"]
    q0 = rep["questions"][0]
    assert q0["auto_submitted"] and q0["timed_out"] and q0["submissions"][0]["passed"]
    wait_scored(ex, st["exam_id"])
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert rep["questions"][0]["score_total"] == 50 and rep["questions"][1]["score_total"] == 0
    assert rep["score_total"] == pytest.approx(16.7)


def test_system_operation_time_extends_deadline(ex):
    """注入 / 复位耗时不算学员的时间：每个配置事务推进假时钟 10 秒，截止时间同步顺延。"""
    login(ex)
    tid = make_template(ex, duration_s=600, question_duration_s=None)
    hub(ex).config_delay = 10
    t0 = ex.clock.t
    st = start(ex, tid)
    used = ex.clock.t - t0
    assert used >= 20                                         # baseline + inject 至少两个配置事务
    assert st["remaining_s"] >= 599 - 1                       # 剩余时间没被系统操作吃掉
    row = db.get_exam(st["exam_id"])
    assert json.loads(row["state_json"])["paused_s"] == pytest.approx(used, abs=0.2)


def test_finish_early_with_confirm_flow(ex):
    login(ex)
    st = start(ex, make_template(ex))
    r = ex.post("/api/exam/finish").json()
    assert r == {"active": False, "finished": True, "exam_id": st["exam_id"]}
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert [q["status"] for q in rep["questions"]] == ["done", "unanswered", "unanswered"]
    assert rep["questions"][0]["auto_submitted"]               # 交卷时当前题自动提交
    assert ex.post("/api/exam/submit").status_code == 409


def test_question_start_failure_masked_then_retry_or_skip(ex):
    login(ex)
    hub(ex).inject_busy = 1
    st = start(ex, make_template(ex))
    q = st["question"]
    assert q["status"] == "pending" and "注入故障失败" in q["error"]
    assert "（已隐藏）" in q["error"] and "10.0.21" not in q["error"] and "ip address" not in q["error"]
    st = ex.post("/api/exam/retry_start").json()
    assert st["question"]["status"] == "active" and st["question"]["error"] is None
    # 跳过：开始失败时「下一题」= 本题未作答
    hub(ex).fixed = True
    ex.post("/api/exam/submit")
    hub(ex).inject_busy = 1
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 1 and st["question"]["status"] == "pending"
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 2 and st["questions"][1]["status"] == "unanswered"


def test_mask_hides_device_rendered_forms():
    """设备回显 / 复位差异里的写法与场景原文不同（点分掩码、vlan batch 合并），按 IP 与参数取值整段隐藏。"""
    from practice import mask

    lib = {k: sc.expand(v, 0) for k, v in sc.load_dir(config.SCENARIOS_DIR, [])[0].items()}
    link = lib["campus-ar0-ar2-link"]
    t = mask("在 AR2 上注入故障失败：命令执行失败：ip address 10.0.21.2 255.255.255.0：Error: x", link)
    assert "10.0.21.2" not in t and "（已隐藏）" in t
    assert "10.0.12.2" in mask("ping 10.0.12.2 超时", link)          # 工单里的地址不是秘密
    assert mask("10.0.21.25 与 110.0.21.2", link) == "10.0.21.25 与 110.0.21.2"   # 整段匹配
    vlan = sc.expand(sc.load_dir(config.SCENARIOS_DIR, [])[0]["campus-lsw0-access-vlan"], 9)
    w = str(vlan["param_values"]["wrong_vlan"])
    t = mask(f"执行复位命令失败：vlan batch 10 {w}：Error", vlan)     # 复位差异生成的写法，不是场景原文
    assert w not in t and "（已隐藏）" in t
    hop = sc.expand(sc.load_dir(config.SCENARIOS_DIR, [])[0]["campus-ar2-return-route"], 4242)
    bad = f"10.0.12.{hop['param_values']['bad_hop']}"
    assert bad not in mask(f"undo ip route-static 10.0.10.0 255.255.255.0 {bad}", hop)


def test_start_failure_with_device_rendered_error_is_masked(ex):
    login(ex)
    tid = make_template(ex, questions=[{"scenario": Q3[0], "seed": 777}])
    wrong = sc.expand(ex.app.state.practice.scenarios[Q3[0]], 777)["param_values"]["wrong"]
    hub(ex).inject_busy = 1
    hub(ex).inject_error = f"命令执行失败：ip address 10.0.{wrong}.2 255.255.255.0：Error: The address already exists."
    st = start(ex, tid)
    err = st["question"]["error"]
    assert st["question"]["status"] == "pending" and f"10.0.{wrong}.2" not in err and "（已隐藏）" in err
    assert f"10.0.{wrong}.2" not in ex.get("/api/exam/current").text


def test_auto_judge_template_passes_without_submission(ex):
    login(ex)
    st = start(ex, make_template(ex, auto_judge=True, questions=[{"scenario": Q3[0], "seed": None},
                                                                {"scenario": Q3[1], "seed": None}]))
    pm = ex.app.state.practice
    pm.debounce_s, pm.min_interval = 0.05, 0.0
    assert pm.active.auto_judge is True
    hub(ex).fixed = True
    ex.portal.call(hub(ex).emit, "command", P_AR2, "ip address 10.0.12.2 24")
    end = time.time() + 5
    while time.time() < end and pm.active.passed_at is None:
        time.sleep(0.05)
    cur = ex.get("/api/exam/current").json()
    q = cur["question"]
    assert q["passed"] and q["locked"] and q["submissions"] == [] and cur["questions"][0]["passed"]
    assert q["last_check"]["all"] is True
    assert ex.post("/api/exam/submit").status_code == 409          # 已通过
    ex.post("/api/exam/next")
    ex.post("/api/exam/finish")
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert rep["questions"][0]["passed"] and rep["questions"][0]["submissions"] == []
    assert not rep["questions"][0]["auto_submitted"]


def test_shuffle_order_is_seeded_and_report_follows_it(ex):
    import random

    login(ex)
    qs = [{"scenario": Q3[0], "seed": 1}, {"scenario": Q3[1], "seed": 2}, {"scenario": Q3[2], "seed": 3},
          {"scenario": Q3[0], "seed": 4}, {"scenario": Q3[1], "seed": 5}]
    tid = make_template(ex, shuffle=True, questions=qs)
    orders = set()
    for _ in range(4):
        st = start(ex, tid)
        state = json.loads(db.get_exam(st["exam_id"])["state_json"])
        order = list(range(len(qs)))
        random.Random(state["order_seed"]).shuffle(order)
        got = [(q["source_index"], q["scenario"], q["seed"]) for q in state["questions"]]
        assert got == [(k, qs[k]["scenario"], qs[k]["seed"]) for k in order]
        assert [x["title"] for x in st["questions"]] == [q["title"] for q in state["questions"]]
        orders.add(tuple(order))
        ex.post("/api/exam/finish")
        rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
        assert [q["title"] for q in rep["questions"]] == [q["title"] for q in state["questions"]]
        assert db.exam_attempts(st["exam_id"])[0]["seed"] == qs[order[0]]["seed"]
    assert len(orders) > 1                                           # 每场考试顺序不同


def test_reset_busy_reclaims_terminal_and_moves_on(ex):
    """学员停在系统视图（以前本题卡在 ending、等学员回用户视图）：结束本题时强制收回终端，直接进入下一题。"""
    login(ex)
    start(ex, make_template(ex))
    h = hub(ex)
    h.restore_busy = 10**6                                    # 学员一直停在系统视图
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 1 and st["question"]["status"] == "active"
    assert h.reclaimed and h.restore_busy == 0
    q0 = ex.get("/api/exam/current").json()["questions"][0]
    assert q0["status"] == "done"


def test_reset_error_still_ends_question(ex):
    """设备一直连不上（复位重试几轮仍失败）：本题照样结束（reset_ok=False）进入下一题，不卡在 ending。"""
    login(ex)
    start(ex, make_template(ex))
    h = hub(ex)
    h.restore_error = "连不上设备 console：refused"
    st = ex.post("/api/exam/next").json()
    assert st["current"] == 1 and st["question"]["status"] == "active"
    pm = ex.app.state.practice
    assert pm.active is not None and pm.active.phase == "running"
    exam = ex.app.state.exam.exam
    q0 = exam["state"]["questions"][0]
    assert q0["status"] == "done" and q0["reset_ok"] is False and not q0.get("reset_error")


# ---------------- 重启恢复 ----------------

def test_restart_recovers_exam_and_time_keeps_running(exam_env):
    main.SESSIONS.clear()
    clock = Clock()
    c = open_client(clock)
    login(c)
    st = start(c, make_template(c, duration_s=900, question_duration_s=300))
    eid, aid = st["exam_id"], c.app.state.practice.active.attempt_id
    c.post("/api/exam/submit")
    c.__exit__(None, None, None)
    clock.t += 120                                            # 停机 2 分钟
    c = open_client(clock)
    hub(c).fault = True                                       # 设备状态不随服务重启改变（新 hub 实例）
    try:
        login(c)                                              # 会话在内存里，重启后要重新登录
        a = c.app.state.practice.active
        assert a is not None and a.attempt_id == aid and a.mode == "exam" and len(a.submissions) == 1
        cur = c.get("/api/exam/current").json()
        assert cur["active"] and cur["exam_id"] == eid and cur["current"] == 0
        assert cur["question"]["remaining_submissions"] == 1
        assert 170 <= cur["question"]["remaining_s"] <= 181       # 300 - 120（停机时间照常流逝）
        assert 770 <= cur["remaining_s"] <= 781
        clock.t += 200                                        # 单题超时 → 进入第 2 题
        tick(c)
        cur = c.get("/api/exam/current").json()
        assert cur["current"] == 1 and cur["questions"][0]["status"] == "done"
        assert cur["questions"][0]["passed"] is False
    finally:
        c.__exit__(None, None, None)
    # 再次重启：停机期间总时长用完，恢复后立刻交卷
    clock.t += 2000
    c = open_client(clock)
    try:
        tick(c)
        login(c)
        assert c.get("/api/exam/current").json()["active"] is False
        assert db.get_exam(eid)["status"] == "finished"
        assert c.app.state.practice.active is None
    finally:
        c.__exit__(None, None, None)
        main.SESSIONS.clear()


# ---------------- 提示 ----------------

def test_hints_forbidden_by_template(ex):
    login(ex)
    start(ex, make_template(ex, hints={"allowed": False}))
    r = ex.post("/api/exam/hint", json={"level": 1})
    assert r.status_code == 403 and "不允许使用提示" in r.json()["detail"]
    assert ex.post("/api/practice/hint", json={"level": 1}).status_code == 409   # 练习接口也绕不过去
    assert ex.app.state.practice.active.hints_used == 0


def test_hints_allowed_penalty_and_max_level(ex):
    login(ex)
    st = start(ex, make_template(ex, hints={"allowed": True, "penalty": 10, "max_level": 2}))
    r = ex.post("/api/exam/hint", json={"level": 1}).json()
    assert r["level"] == 1 and r["source"] == "static" and r["penalty_per_level"] == 10
    assert ex.post("/api/exam/hint", json={"level": 3}).status_code == 403
    assert ex.post("/api/exam/hint", json={"level": 2}).status_code == 200
    assert ex.get("/api/exam/current").json()["question"]["hints_used"] == 2
    hub(ex).fixed = True
    ex.post("/api/exam/submit")
    ex.post("/api/exam/finish")
    wait_scored(ex, st["exam_id"])
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert rep["questions"][0]["hints_used"] == 2
    assert rep["questions"][0]["score_total"] == 30           # 50 - 2 级 × 10（扣在本题总分上，AI 不可用也照扣）


# ---------------- 评分汇总 ----------------

def test_weighted_aggregate_and_template_weights(ex):
    login(ex)
    tid = make_template(ex, questions=[{"scenario": Q3[0], "seed": 3, "weight": 2},
                                       {"scenario": Q3[1], "seed": None, "weight": 1}],
                        weights={"result": 6, "process": 3, "side": 1})
    st = start(ex, tid)
    hub(ex).fixed = True
    ex.post("/api/exam/submit")
    ex.post("/api/exam/next")
    ex.post("/api/exam/finish")
    atts = wait_scored(ex, st["exam_id"])
    assert atts[0]["seed"] == 3                                # 模板固定种子
    d = json.loads(atts[0]["score_json"])
    assert d["weights"] == {"result": 60.0, "process": 30.0, "side": 10.0}   # 按比例缩放到 100
    assert d["options"]["exam"]["max_submissions"] == 2
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert [q["score_total"] for q in rep["questions"]] == [60, 0]
    assert rep["score_total"] == pytest.approx(40.0)          # (2×60 + 1×0) / 3
    # 重新评分沿用模板权重
    r = ex.post(f"/api/practice/rescore/{atts[0]['id']}")
    assert r.status_code == 200
    wait_scored(ex, st["exam_id"])
    assert json.loads(db.get_attempt(atts[0]["id"])["score_json"])["weights"]["result"] == 60.0


# ---------------- 回放 ----------------

def test_replay_in_time_order_and_disabled_by_template(ex):
    login(ex)
    st = start(ex, make_template(ex))
    h = hub(ex)
    for ev in (("command", P_AR0, "ping 10.0.12.2"), ("output", P_AR0, "0 packet(s) received"),
               ("command", P_AR2, "display ip interface brief"), ("output", P_AR2, "GE0/0/0 10.0.21.2/24 up up"),
               ("log", P_AR2, "%%01IFNET/4/LINK_STATE(l)")):
        ex.portal.call(h.emit, *ev)                           # hub 事件在服务的事件循环里触发
    ex.portal.call(ex.app.state.practice.flush_logs)
    assert ex.get(f"/api/exam/{st['exam_id']}/replay").status_code == 409    # 交卷前不能看
    ex.post("/api/exam/finish")
    rp = ex.get(f"/api/exam/{st['exam_id']}/replay").json()
    logs = rp["questions"][0]["logs"]
    assert [(l["device"], l["command"]) for l in logs] == [("AR0", "ping 10.0.12.2"), ("AR2", "display ip interface brief")]
    assert logs[1]["output"] == "GE0/0/0 10.0.21.2/24 up up\n%%01IFNET/4/LINK_STATE(l)"
    assert all(l["offset_s"] is not None and l["offset_s"] >= 0 for l in logs)
    assert [q["logs"] for q in rp["questions"][1:]] == [[], []]
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert rep["questions"][0]["commands"] == 2 and rep["replay_available"]
    # 模板关闭回放
    st2 = start(ex, make_template(ex, record_replay=False))
    ex.post("/api/exam/finish")
    r = ex.get(f"/api/exam/{st2['exam_id']}/replay")
    assert r.status_code == 403
    assert ex.get(f"/api/exam/{st2['exam_id']}/report").json()["replay_available"] is False


# ---------------- 权限与互斥 ----------------

def test_only_own_exam_visible_and_operable(ex):
    login(ex)
    assert ex.post("/api/students", json={"login": "bob", "name": "Bob", "password": "p"}).status_code == 201
    st = start(ex, make_template(ex))
    eid = st["exam_id"]
    aid = ex.app.state.practice.active.attempt_id
    # 考试中自己也不能看本场各题的评分全文
    assert ex.get(f"/api/practice/result/{aid}").status_code == 409
    login(ex, "bob", "p")
    assert ex.get("/api/exam/current").json() == {"active": False, "other_running": True}
    for url in ("/api/exam/submit", "/api/exam/next", "/api/exam/finish", "/api/exam/retry_start"):
        assert ex.post(url).status_code == 403, url
    assert ex.post("/api/exam/hint", json={"level": 1}).status_code in (403,)
    assert ex.post("/api/exam/start", json={"template_id": 1}).status_code == 409
    login(ex)
    ex.post("/api/exam/finish")
    assert ex.get(f"/api/exam/{eid}/report").status_code == 200
    login(ex, "bob", "p")
    assert ex.get(f"/api/exam/{eid}/report").status_code == 404
    assert ex.get(f"/api/exam/{eid}/replay").status_code == 404
    assert ex.get(f"/api/practice/result/{aid}").status_code == 404
    assert ex.get("/api/exams").json() == []
    assert ex.get("/api/me").json()["exams"] == []
    for url in ("/api/exam/current", "/api/exams", f"/api/exam/{eid}/report"):
        main.SESSIONS.clear()
        ex.cookies.clear()
        assert ex.get(url).status_code == 401


def test_exam_and_practice_are_mutually_exclusive(ex):
    login(ex)
    tid = make_template(ex)
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200
    r = ex.post("/api/exam/start", json={"template_id": tid})
    assert r.status_code == 409 and "练习" in r.json()["detail"]
    end_practice(ex)
    st = start(ex, tid)
    r = ex.post("/api/practice/start", json={"scenario_id": Q3[0]})
    assert r.status_code == 409 and "考试" in r.json()["detail"]
    for url in ("/api/practice/check", "/api/practice/end"):
        r = ex.post(url)
        assert r.status_code == 409 and "考试" in r.json()["detail"], url
    assert ex.get("/api/practice/current").json() == {"active": False, "exam_active": True, "last_result": None}
    assert ex.post("/api/exam/start", json={"template_id": tid}).status_code == 409
    # 题与题之间（当前题刚复位、下一题还没开始）也不能开始练习
    hub(ex).inject_busy = 1
    ex.post("/api/exam/next")
    assert ex.app.state.practice.active is None
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 409
    ex.post("/api/exam/finish")
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200


def test_start_refused_when_topology_not_ready(exam_env, monkeypatch):
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", f"{AR0},{AR2}")      # LSW0 没启动
    main.SESSIONS.clear()
    c = open_client(Clock())
    try:
        login(c)
        r = c.post("/api/exam/start", json={"template_id": make_template(c)})
        assert r.status_code == 409 and "第 2 题未就绪" in r.json()["detail"] and "LSW0" in r.json()["detail"]
        assert db.list_exams(1) == []
    finally:
        c.__exit__(None, None, None)


def test_topology_hidden_when_template_disallows(ex):
    login(ex)
    full = ex.get("/api/topology/current").json()["topology"]
    assert full["links"]
    start(ex, make_template(ex, show_topology=False))
    t = ex.get("/api/topology/current").json()["topology"]
    assert t["hidden"] and t["links"] == [] and t["shapes"] == [] and t["notes"] == []
    assert {d["name"] for d in t["devices"]} >= {"AR0", "AR2"}            # 终端仍可按设备打开
    assert ex.get("/api/exam/current").json()["config"]["show_topology"] is False
    ex.post("/api/exam/finish")
    assert ex.get("/api/topology/current").json()["topology"]["links"]


# ---------------- 答案保护 ----------------

FORBIDDEN_KEYS = ("checks", "solution", "inject", "reference_path", "scoring_points", "restore", "baseline",
                  "seed", "param_values", "expect", "standard_path")


def _keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def test_no_answer_fields_leak_during_exam(ex):
    """考试进行中遍历全部 /api 接口：响应里既没有答案字段名，也没有展开后的判定命令 / 正则、注入与修复命令、
    参考路径、评分要点原文。新增接口必须加进覆盖列表（否则本测试失败）。"""
    login(ex)
    tid = make_template(ex, hints={"allowed": True, "penalty": 1, "max_level": 3})
    hub(ex).inject_busy = 0
    st = start(ex, tid)
    eid = st["exam_id"]
    # 第 1 题作答并结束（产生已评分的考试题记录），停在第 2 题
    hub(ex).fixed = True
    ex.post("/api/exam/submit")
    ex.post("/api/exam/next")
    a = ex.app.state.practice.active
    aid_prev = db.exam_attempts(eid)[0]["id"]
    secrets_: list[str] = []
    rows = json.loads(db.get_exam(eid)["state_json"])["questions"]
    for q in rows:
        e = sc.expand(ex.app.state.practice.scenarios[q["scenario"]], q["seed"])
        secrets_ += secret_strings(e)
        secrets_ += [e["reference_path"], *e["scoring_points"]]
    secrets_ = [s for s in set(secrets_) if len(s) >= 6]
    assert len(secrets_) > 10
    calls = {
        ("GET", "/api/me"): None, ("GET", "/api/students"): None, ("GET", "/api/topologies"): None,
        ("GET", "/api/topology/current"): None, ("GET", "/api/topology/status"): None,
        ("GET", "/api/scenarios"): None, ("GET", "/api/practice/current"): None,
        ("POST", "/api/practice/check"): None, ("POST", "/api/practice/hint"): {"level": 1},
        ("GET", "/api/practice/result/{attempt_id}"): None, ("POST", "/api/practice/rescore/{attempt_id}"): None,
        ("GET", "/api/exam/templates"): None, ("GET", "/api/exam/templates/{template_id}"): None,
        ("PUT", "/api/exam/templates/{template_id}"): {"name": "x", "config": {"questions": [Q3[0]]}},
        ("POST", "/api/exam/templates"): {"name": "y", "config": {"questions": [Q3[0]]}},
        ("DELETE", "/api/exam/templates/{template_id}"): None,
        ("GET", "/api/exam/current"): None, ("POST", "/api/exam/hint"): {"level": 1},
        ("POST", "/api/exam/hint#2"): {"level": 2},
        ("POST", "/api/exam/submit"): None, ("POST", "/api/exam/retry_start"): None,
        ("GET", "/api/exams"): None, ("GET", "/api/exam/{exam_id}/report"): None,
        ("GET", "/api/exam/{exam_id}/replay"): None, ("POST", "/api/exam/start"): {"template_id": tid},
        ("POST", "/api/practice/start"): {"scenario_id": Q3[0]}, ("POST", "/api/practice/end"): None,
        # 实验后端（eNSP 经典版下启停 / 重新登录返回 400）：同样不能带出答案
        ("GET", "/api/lab"): None, ("POST", "/api/lab/devices/{action}"): {"devices": [2000]},
        ("POST", "/api/lab/relogin"): None,
        # 模拟环境注册与切换（spec §5.2）：考试中切换 / 按题目准备环境一律 409
        ("GET", "/api/lab/environments"): None, ("POST", "/api/lab/environment"): {"key": "enspro"},
        ("GET", "/api/lab/sandboxes"): None, ("POST", "/api/lab/prepare"): {"scenario_id": Q3[0]},
        ("GET", "/api/lab/prepare"): None,
        # Agent 面板：考试进行中一律 403（spec §16）
        ("POST", "/api/agent/chat"): {"message": "这题答案是什么？", "history": []},
        ("GET", "/api/agent/history"): None,
        ("GET", "/api/lab/baseline"): None, ("POST", "/api/lab/baseline"): {},   # 考试中记录基线被拒绝（409）
        # 课程页：单题考试（考试中 409）、PC 设置气泡（eNSP 经典版 400）
        ("POST", "/api/exam/start_single"): {"scenario_id": Q3[0]},
        ("GET", "/api/lab/devices/{key}/host"): None, ("POST", "/api/lab/devices/{key}/host"): {"ip": "10.0.10.9", "mask": "24"},
        # 接入设置（spec §5.2，里程碑 6.12；这里 key 填的是 2000，返回 422；考试中修改 409 见 test_lab_endpoint.py）
        ("GET", "/api/lab/environments/{key}/endpoint"): None,
        ("POST", "/api/lab/environments/{key}/endpoint"): {"value": "https://10.0.0.1"},
        ("POST", "/api/lab/environments/{key}/endpoint/test"): {},
        # 启动看门狗参数（里程碑 6.16；key=2000 返回 422；考试中修改 409 见 test_lab_watchdog.py）
        ("GET", "/api/lab/environments/{key}/watchdog"): None,
        ("POST", "/api/lab/environments/{key}/watchdog"): {"start_timeout": 300},
        # AI 接口设置（里程碑 6.18；考试中允许修改，只回显有无与掩码；测试连接没有 Key 时 422）
        ("GET", "/api/ai/settings"): None, ("POST", "/api/ai/settings"): {"model": "m-exam"},
        ("POST", "/api/ai/settings/test"): {},
        # 用户添加的环境实例（里程碑 6.19）：考试中添加 / 修改 / 删除 409；测试连接只连本机不可用端口
        ("POST", "/api/lab/environments"): {"type": "enspro", "label": "考试中加", "url": "https://127.0.0.1:9"},
        ("POST", "/api/lab/environments/probe"): {"type": "enspro", "url": "https://127.0.0.1:9"},
        ("PATCH", "/api/lab/environments/{key}"): {"label": "x"},
        ("POST", "/api/lab/environments/{key}"): {"label": "x"},
        ("DELETE", "/api/lab/environments/{key}"): None,
    }
    # 不在遍历范围内的接口（原因）：登录 / 登出 / 建学生与考试无关；next / finish 会改变考试进度，单独测（交卷后才有成绩单）
    excluded = {("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/students"),
                ("POST", "/api/exam/next"), ("POST", "/api/exam/finish")}
    routes = {(m, r.path) for r in main.app.routes if getattr(r, "path", "").startswith("/api")
              for m in getattr(r, "methods", set()) - {"HEAD"}}
    covered = {(m, p.split("#")[0]) for m, p in calls}
    assert routes - excluded == covered, routes - excluded ^ covered
    ids = {"attempt_id": aid_prev, "template_id": tid, "exam_id": eid, "action": "stop", "key": 2000}
    for (m, p), body in calls.items():
        url = p.split("#")[0].format(**ids)
        r = ex.request(m, url, json=body)
        assert r.status_code < 500, (m, url, r.text)
        if p.startswith("/api/agent/"):
            assert r.status_code == 403, (m, url, r.text)      # 考试中 Agent 一律拒绝
        text = r.text
        try:
            keys = _keys(r.json(), set())
        except ValueError:
            keys = set()
        if p.startswith("/api/exam/templates"):
            keys -= {"seed"}                    # 模板里的固定种子是出题配置，不是答案
        assert not (keys & set(FORBIDDEN_KEYS)), (m, url, keys & set(FORBIDDEN_KEYS))
        if p == "/api/exam/hint#2" or (p == "/api/practice/hint"):
            continue                            # 第 2 级提示可以出现判定类查看命令（spec §7）
        hits = [s for s in secrets_ if s in text]
        assert not hits, (m, url, hits[:3])
    assert ex.app.state.exam.exam is not None and ex.app.state.exam.exam["id"] == eid
    # 交卷后成绩单可以看参考思路，但仍不给 checks 原文 / 注入命令
    ex.post("/api/exam/finish")
    rep = ex.get(f"/api/exam/{eid}/report")
    assert rep.status_code == 200
    keys = _keys(rep.json(), set())
    assert not keys & {"checks", "solution", "inject", "scoring_points", "expect", "restore", "baseline"}
    e0 = sc.expand(ex.app.state.practice.scenarios[rows[0]["scenario"]], rows[0]["seed"])
    assert e0["checks"][0]["command"] not in rep.text
    assert rep.json()["questions"][0]["reference_path"] == e0["reference_path"]


# ---------------- 迁移 ----------------

LEGACY = json.loads((Path(__file__).resolve().parent / "fixtures" / "legacy_db_schemas.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", list(LEGACY))
def test_migration_adds_exam_schema_idempotently(key, db_path):
    ver = LEGACY[key]
    conn = sqlite3.connect(db_path)
    conn.executescript(ver["schema"])
    for table, col, typ in ver["migrations"]:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("INSERT INTO students (login, name, password_plain, created_at) VALUES ('stu', 'S', 'p', 'x')")
    conn.execute("INSERT INTO exam_templates (name, config_json) VALUES ('旧模板', '{}')")
    conn.commit()
    conn.close()
    for _ in range(3):
        db.init_db()
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
        assert {"seed", "param_values_json", "exam_id", "exam_index"} <= cols
        assert {"created_at", "updated_at"} <= {r[1] for r in conn.execute("PRAGMA table_info(exam_templates)")}
        assert "exams" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert [t["name"] for t in db.list_templates()] == ["旧模板"]
    eid = db.create_exam(1, 1, "旧模板", "{}", "2026-10-05T00:00:00+00:00", "2026-10-05T01:00:00+00:00", "{}")
    assert db.get_profile(1)["exams"][0]["id"] == eid
    a = db.create_attempt(1, None, "exam", seed=5, param_values={"x": 1}, exam_id=eid, exam_template_id=1, exam_index=0)
    assert db.get_attempt(a["id"])["seed"] == 5
    assert db.delete_template(1) and db.get_attempt(a["id"])["exam_template_id"] is None
