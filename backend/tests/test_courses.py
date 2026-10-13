"""课程页 / 单层顶栏 / PC 设置气泡（里程碑 6.9）的后端部分：

- 课程数据：scenarios/chapters.json、场景新字段（chapter / difficulty / est_minutes / summary / tags）与校验，
  新字段不泄露答案（遍历全部场景 × 多个种子）；GET /api/scenarios 带 chapters 与每题 progress。
- 单题考试：POST /api/exam/start_single（临时单题模板、默认规则）、开题失败整场回滚、考完出成绩单。
- 模式切换的后端约束：考试中不能开练习 / 结束练习 / 再开考；练习中开考试 409，结束练习（按放弃记录）后可以开。
- PC 设置：PracticeManager.set_host（练习中只改开放的设备、先拍快照、伪命令进操作记录、结束时随复位还原、
  服务重启后照样还原）；接口 GET|POST /api/lab/devices/{console}/host（假 eNSP Pro：权限、校验、非 PC 拒绝；
  eNSP 经典版 400）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import pytest

import config
import db
import hints
import lab as lab_lib
import main
import scenarios as sc
from console_hub import HiddenResult
from enspro import EnsproClient, EnsproProvider, mask_dotted
from fake_enspro import FakeRest, FakeStompServer, NetYunShan
from fake_vrp import FakeNet
from practice import PracticeError, PracticeManager, host_snapshot, parse_host_snapshot, same_host, secret_strings
from test_enspro_api import BASE, PW, ServerThread
from test_exam import Q3, ex, exam_env, hub, login, make_template, start, tick, wait_scored  # noqa: F401
from practice_helpers import end_practice

ROOT = Path(config.SCENARIOS_DIR)


def run(coro, timeout=40):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---------------- 课程数据 ----------------

def test_chapters_file_and_every_scenario_has_course_fields():
    chapters, errs = sc.load_chapters(str(ROOT))
    assert not errs
    assert [c["id"] for c in chapters] == [f"L{i:02d}" for i in range(1, 13)]
    assert chapters[0]["title"] == "链路与接口" and chapters[-1]["title"] == "综合排障"
    loaded, errors = sc.load_dir(str(ROOT))
    assert not errors, errors                       # chapters.json 不被当成场景加载
    assert len(loaded) >= 11
    ids = {c["id"] for c in chapters}
    for s in loaded.values():
        for k in sc.COURSE_FIELDS:
            assert k in s, (s["id"], k)
        assert s["chapter"] in ids and 1 <= s["difficulty"] <= 5 and s["est_minutes"] > 0
        p = sc.public(sc.expand(s, 0))
        assert {k: p[k] for k in sc.COURSE_FIELDS} == {k: s[k] for k in sc.COURSE_FIELDS}


def _secrets(s):
    out = set(secret_strings(s))
    out |= {s.get("reference_path") or "", *(s.get("scoring_points") or []), *(s.get("hints") or [])}
    out |= set(hints.banned_terms(s))
    return {x for x in out if isinstance(x, str) and len(x) >= 4}


def test_course_fields_do_not_leak_answers_for_any_seed():
    loaded, _ = sc.load_dir(str(ROOT))
    n = 0
    for s in loaded.values():
        for seed in (0, 1, 7, 123456):
            e = sc.expand(s, seed)
            text = json.dumps({k: sc.public(e)[k] for k in sc.COURSE_FIELDS}, ensure_ascii=False)
            hits = [x for x in _secrets(e) if x in text]
            assert not hits, (s["id"], seed, hits)
            assert not hints.leaks(text, hints.banned_terms(e)), (s["id"], seed)
            n += 1
    assert n >= 44


def _plain():
    return json.loads((ROOT / "ospf-2-1-r1-shutdown.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("patch, msg", [
    ({"chapter": "L99"}, "不在 chapters.json 中"),
    ({"difficulty": 6}, "difficulty"),
    ({"difficulty": True}, "difficulty"),
    ({"est_minutes": 0}, "est_minutes"),
    ({"summary": ""}, "summary"),
    ({"summary": "第一行\n第二行"}, "summary"),
    ({"tags": "OSPF"}, "tags"),
    ({"summary": "R1 的 GE0/0/0 被 shutdown 了"}, "泄露答案"),     # 注入命令
    ({"tags": ["OSPF", "undo shutdown"]}, "泄露答案"),             # 修复命令
    ({"summary": "邻居 {x} 消失"}, "占位符"),
])
def test_course_field_validation(patch, msg):
    s = {**_plain(), **patch}
    with pytest.raises(sc.ScenarioError, match=msg):
        sc.validate(s, None, {f"L{i:02d}" for i in range(1, 13)})


def test_summary_leak_check_covers_parameter_samples():
    """参数化场景：注入的错误地址（只在故障配置里出现的 IP）写进 summary 也会被抽样校验拦住。"""
    raw = json.loads((ROOT / "enspro-t1-t2-link.json").read_text(encoding="utf-8"))
    raw["summary"] = "T2 的接口地址被改成了 10.0.21.2"          # 种子 0 下 wrong=21 的注入地址
    with pytest.raises(sc.ScenarioError, match="泄露答案"):
        sc.validate(raw)


def test_load_dir_reports_unknown_chapter(tmp_path):
    shutil.copy(ROOT / "chapters.json", tmp_path / "chapters.json")
    raw = _plain()
    raw["chapter"] = "L77"
    (tmp_path / "x.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    loaded, errors = sc.load_dir(str(tmp_path))
    assert not loaded and len(errors) == 1 and "L77" in errors[0]
    (tmp_path / "chapters.json").write_text("{broken", encoding="utf-8")
    loaded, errors = sc.load_dir(str(tmp_path))
    assert any("chapters.json" in e for e in errors)


def test_api_scenarios_has_chapters_and_progress(ex):
    login(ex)
    data = ex.get("/api/scenarios").json()
    assert [c["id"] for c in data["chapters"]][:3] == ["L01", "L02", "L03"]
    by = {s["id"]: s for s in data["scenarios"]}
    assert by[Q3[0]]["progress"] == "none" and by[Q3[0]]["chapter"] == "L02" and by[Q3[0]]["summary"]
    # 练习放弃 → 未通过；再练一次通关 → 已通过
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200
    assert ex.get("/api/scenarios").json()["scenarios"][0]["progress"] in ("none",)  # 进行中的不算
    assert end_practice(ex)["active"] is False
    by = {s["id"]: s for s in ex.get("/api/scenarios").json()["scenarios"]}
    assert by[Q3[0]]["progress"] == "failed"
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200
    hub(ex).fixed = True
    assert ex.post("/api/practice/check").json()["passed"]
    end_practice(ex)
    by = {s["id"]: s for s in ex.get("/api/scenarios").json()["scenarios"]}
    assert by[Q3[0]]["progress"] == "passed" and by[Q3[1]]["progress"] == "none"


# ---------------- 草稿题（未真机验证，"draft": true，docs/question-bank-plan.md） ----------------

def test_draft_field_validation_and_public():
    s = _plain()
    assert sc.validate(s)["draft"] is False and sc.public(sc.validate(s))["draft"] is False
    s["draft"] = True
    v = sc.validate(s)
    assert v["draft"] is True and sc.public(sc.expand(v, 0))["draft"] is True
    for bad in ("yes", 1, None, ["x"]):
        with pytest.raises(sc.ScenarioError, match="draft"):
            sc.validate({**_plain(), "draft": bad})


def _mark_draft(c, sid):
    pm = c.app.state.practice
    pm.scenarios[sid] = {**pm.scenarios[sid], "draft": True}


def test_api_scenarios_hides_drafts_by_default(ex, monkeypatch):
    login(ex)
    _mark_draft(ex, Q3[1])
    monkeypatch.setattr(config, "SHOW_DRAFTS", False)
    pm = ex.app.state.practice
    for q in ("", "?all=1"):
        data = ex.get(f"/api/scenarios{q}").json()
        ids = {s["id"] for s in data["scenarios"]}
        assert Q3[1] not in ids and Q3[0] in ids, q
        plat = data["platform"]
        drafts = {k for k, v in pm.scenarios.items() if v.get("draft") and (plat is None or v.get("platform", "vrp5") == plat)}
        assert not ids & drafts and Q3[1] in drafts, q
        assert data["show_drafts"] is False and data["drafts_hidden"] == len(drafts), q
        assert all(s["draft"] is False for s in data["scenarios"])
    # 按 id 开始练习不受限制（真机验证要用）；进行中的草稿题照列，操作页才能找到工单
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[1]}).status_code == 200
    by = {s["id"]: s for s in ex.get("/api/scenarios?all=1").json()["scenarios"]}
    assert by[Q3[1]]["draft"] is True
    assert end_practice(ex)["active"] is False
    assert Q3[1] not in {s["id"] for s in ex.get("/api/scenarios").json()["scenarios"]}


def test_api_scenarios_shows_drafts_with_switch(ex, monkeypatch):
    login(ex)
    _mark_draft(ex, Q3[1])
    monkeypatch.setattr(config, "SHOW_DRAFTS", True)
    data = ex.get("/api/scenarios").json()
    by = {s["id"]: s for s in data["scenarios"]}
    assert by[Q3[1]]["draft"] is True and by[Q3[0]]["draft"] is False
    assert data["show_drafts"] is True and data["drafts_hidden"] == 0


def test_other_platform_count_ignores_hidden_drafts(ex, monkeypatch):
    login(ex)
    pm = ex.app.state.practice
    monkeypatch.setattr(config, "SHOW_DRAFTS", False)
    ys = [k for k, v in pm.scenarios.items() if v.get("platform") == "yunshan"]
    drafts = [k for k in ys if pm.scenarios[k].get("draft")]
    before = ex.get("/api/scenarios?env=ensp").json()["other_platform"]
    assert before == len(ys) - len(drafts)
    victim = next(k for k in ys if k not in drafts)
    _mark_draft(ex, victim)
    assert ex.get("/api/scenarios?env=ensp").json()["other_platform"] == before - 1


def test_titles_have_no_platform_suffix_and_title_platform_is_unique():
    """2026-10-07：题名去掉「（YunShan）」「（VRP5）」后缀，平台改用标签显示；(题名, 平台) 仍然唯一。"""
    loaded, errors = sc.load_dir(str(ROOT))
    assert not errors, errors
    seen = set()
    for s in loaded.values():
        assert not re.search(r"（(YunShan|VRP5)）$", s["title"]), s["title"]
        key = (s["title"], sc.platform_of(s))
        assert key not in seen, key
        seen.add(key)
    plats = {sc.platform_of(s) for s in loaded.values()}
    assert plats <= set(sc.PLATFORM_TAGS) and plats == {"vrp5", "yunshan"}


def test_courses_page_requires_login(ex):
    r = ex.get("/courses", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"
    login(ex)
    r = ex.get("/courses")
    assert r.status_code == 200 and "课程" in r.text
    assert ex.get("/static/courses.js").status_code == 200


# ---------------- 单题考试 ----------------

def test_single_exam_flow_and_report(ex):
    login(ex)
    r = ex.post("/api/exam/start_single", json={"scenario_id": Q3[0]})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["active"] and st["single"] and st["total"] == 1 and st["name"].startswith("单题考试 · ")
    assert st["config"]["duration_s"] == 900 and st["config"]["hints_allowed"] is False
    assert st["config"]["show_topology"] is True and st["question"]["status"] == "active"
    assert db.get_exam(st["exam_id"])["template_id"] is None
    # 考试中：Agent 403、提示 403、练习接口 409
    assert ex.post("/api/agent/chat", json={"message": "hi", "history": []}).status_code == 403
    assert ex.post("/api/exam/hint", json={"level": 1}).status_code == 403
    hub(ex).fixed = True
    sub = ex.post("/api/exam/submit").json()
    assert sub["submission"]["passed"]
    fin = ex.post("/api/exam/finish").json()
    assert fin == {"active": False, "finished": True, "exam_id": st["exam_id"]}
    wait_scored(ex, st["exam_id"])
    rep = ex.get(f"/api/exam/{st['exam_id']}/report").json()
    assert rep["name"] == st["name"] and len(rep["questions"]) == 1 and rep["questions"][0]["passed"]
    # 题名不带平台后缀：考试名末尾、成绩单与命令回放的每题都带平台，两个平台的同名题能分清
    assert st["name"].endswith(" · VRP5") and rep["questions"][0]["platform"] == "vrp5"
    assert ex.get(f"/api/exam/{st['exam_id']}/replay").json()["questions"][0]["platform"] == "vrp5"
    prof = ex.get("/api/me").json()
    assert prof["recent_attempts"][0]["scenario_platform"] == "vrp5"
    assert ex.get("/api/exams").json()[0]["template_id"] is None
    assert ex.get("/api/exam/templates").json()["templates"][0]["name"] == "Campus 基础排障测验"   # 不进模板表


def test_single_exam_start_failure_rolls_back(ex):
    """开题失败（注入报错 / 设备忙）：整场回滚、不计时，返回 409；/api/exams 里没有这场；随后可以重试。"""
    login(ex)
    hub(ex).inject_busy = 1
    hub(ex).inject_error = "Error: The system is busy in building configuration"
    r = ex.post("/api/exam/start_single", json={"scenario_id": Q3[0]})
    assert r.status_code == 409 and "busy" in r.json()["detail"]
    assert ex.app.state.exam.exam is None and ex.app.state.practice.active is None
    assert ex.get("/api/exams").json() == [] and ex.get("/api/exam/current").json()["active"] is False
    r = ex.post("/api/exam/start_single", json={"scenario_id": Q3[0]})
    assert r.status_code == 200, r.text
    assert ex.post("/api/exam/start_single", json={"scenario_id": "no-such"}).status_code == 409   # 已有考试
    ex.post("/api/exam/finish")
    assert ex.post("/api/exam/start_single", json={"scenario_id": "no-such"}).status_code == 404


def test_template_exam_failure_still_keeps_exam_running(ex):
    """模板考试的行为不变：开题失败留在 pending（可「重新开始本题」/「跳过」），考试照常进行。"""
    login(ex)
    tid = make_template(ex)
    hub(ex).inject_busy = 1
    st = start(ex, tid)
    assert st["active"] and st["question"]["status"] == "pending" and st["question"]["error"]


# ---------------- 模式切换的后端约束 ----------------

def test_mode_switch_constraints(ex):
    login(ex)
    # 练习中：开考（单题 / 模板）409；结束练习（按放弃记录、复位）之后可以开单题考试
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200
    r = ex.post("/api/exam/start_single", json={"scenario_id": Q3[0]})
    assert r.status_code == 409 and "练习" in r.json()["detail"]
    assert ex.post("/api/exam/start", json={"template_id": make_template(ex)}).status_code == 409
    end = end_practice(ex)
    assert end["active"] is False and end["passed"] is False
    att = db.get_attempt(end["attempt_id"])
    assert att["ended_at"] and att["passed"] == 0                       # 按放弃记录
    st = ex.post("/api/exam/start_single", json={"scenario_id": Q3[0]}).json()
    assert st["active"]
    # 考试中：不能开练习、不能用练习接口结束、不能再开考、不能切环境
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 409
    assert ex.post("/api/practice/end").status_code == 409
    assert ex.post("/api/practice/check").status_code == 409
    assert ex.post("/api/exam/start_single", json={"scenario_id": Q3[1]}).status_code == 409
    assert ex.post("/api/lab/environment", json={"key": "enspro"}).status_code == 409
    assert ex.post("/api/lab/prepare", json={"scenario_id": Q3[1]}).status_code == 409
    # 交卷后可以继续练习同一题
    ex.post("/api/exam/finish")
    assert ex.post("/api/practice/start", json={"scenario_id": Q3[0]}).status_code == 200
    end_practice(ex)
# ---------------- PC 设置：PracticeManager.set_host ----------------

CFG = "#\nsysname HUAWEI\n#\ninterface GE0/0/0\n#\nreturn"
SCEN = {
    "id": "pc-bubble", "title": "PC 气泡演示", "topology": "ainet_campus", "platform": "yunshan",
    "required_devices": ["AR0", "PC1", "PC2"], "accessible_devices": ["AR0", "PC2"], "hosts": ["PC1"],
    "symptom": "PC2 ping 不通网关",
    "baseline": {"PC1": ["ip address 10.0.10.10 24", "ip gateway 10.0.10.1"]},
    "inject": {"AR0": ["system-view", "interface GE0/0/0", "shutdown"]},
    "restore": {"PC1": ["undo ip address", "undo ip gateway"],
                "AR0": ["system-view", "interface GE0/0/0", "undo shutdown"]},
    "checks": [{"device": "AR0", "command": "ping -c 2 10.0.10.20", "expect": "\\b[12] packet\\(s\\) received"}],
    "hints": ["看看接口状态", "display ip interface brief", "undo shutdown"],
}
CUR = {"state": "running", "lab": {"kind": "enspro", "platform": "yunshan", "power": True, "label": "eNSP Pro"},
       "topology": {"name": "ainet_campus", "path": "enspro://sandbox/3", "devices": [
           {"name": "AR0", "console": 9, "kind": "router", "status": "online"},
           {"name": "PC1", "console": 12, "kind": "pc", "status": "online"},
           {"name": "PC2", "console": 13, "kind": "pc", "status": "online"},
           {"name": "PC3", "console": 14, "kind": "pc", "status": "online"}]}}


class Hub:
    def __init__(self) -> None:
        self.pinned: set[int] = set()
        self.listeners: list = []
        self.ping_ok = False

    def add_listener(self, fn): self.listeners.append(fn)

    async def run_hidden(self, port, commands, kind="check", cmd_timeout=None, tolerate_errors=False, groups=None,
                         confirm=False):
        if commands == ["display current-configuration"]:
            return HiddenResult("ok", outputs=[CFG])
        if kind == "check":
            return HiddenResult("ok", outputs=["2 packet(s) received" if self.ping_ok else "0 packet(s) received"])
        return HiddenResult("ok", outputs=[""] * len(commands))

    def reset_replay(self, p): pass
    def pin(self, ps): self.pinned |= set(ps)
    def unpin(self, ps): self.pinned -= set(ps)
    def drop_subscribers(self, ps, msg=""): pass
    def flush_output(self, p): pass


class Lab:
    """假实验后端：终端主机地址（dns=True 时读得到 DNS）。"""
    kind = "enspro"

    def __init__(self, dns: bool = False) -> None:
        base = {"ip": "", "mask": "", "gateway": ""}
        self.hosts = {12: dict(base), 13: {"ip": "10.0.10.20", "mask": "255.255.255.0", "gateway": "10.0.10.1"},
                      14: dict(base)}
        if dns:
            for h in self.hosts.values():
                h["dns"] = ""
        self.dns = dns
        self.sets: list = []
        self.fail = ""

    async def host_get(self, key):
        return dict(self.hosts[key])

    async def host_set(self, key, cfg):
        if self.fail:
            raise lab_lib.LabError(502, self.fail)
        self.sets.append((key, dict(cfg)))
        h = {"ip": cfg.get("ip", ""), "mask": mask_dotted(cfg.get("mask", "")), "gateway": cfg.get("gateway", "")}
        if self.dns:
            h["dns"] = cfg["dns"] if "dns" in cfg else self.hosts[key].get("dns", "")
        self.hosts[key] = h
        return dict(h)


@pytest.fixture
def pcm(db_path):
    db.init_db()
    s = sc.validate(json.loads(json.dumps(SCEN)))
    ids = db.sync_scenarios([s])
    if not db.list_students():
        db.create_student("stu", "学员", "pw")

    async def resolve():
        return json.loads(json.dumps(CUR))

    def make(lab=None):
        m = PracticeManager(Hub(), {s["id"]: s}, ids, resolve, debounce=0.05, min_interval=0, busy_retry=0.01,
                            op_deadline=0.5)
        m.baseline_auto = False
        m.lab = lab or Lab()
        return m
    return make


def _sid() -> int:
    return db.list_students()[0]["id"]


def test_set_host_without_practice_just_sets(pcm):
    async def go():
        m = pcm()
        got = await m.set_host(14, "PC3", {"ip": "10.0.10.30", "mask": "24", "gateway": "10.0.10.1"})
        assert got == {"ip": "10.0.10.30", "mask": "255.255.255.0", "gateway": "10.0.10.1"}
        assert m.lab.hosts[14]["ip"] == "10.0.10.30"
    run(go())


def test_set_host_in_practice_logs_pseudo_command_and_restores_on_end(pcm):
    async def go():
        m = pcm()
        await m.start(_sid(), "pc-bubble", seed=0)
        a = m.active
        # PC2 不在场景 hosts 里，但拓扑里是 PC：开练快照按地址配置拍（不是 display current-configuration）
        assert parse_host_snapshot(a.snapshots["PC2"])["ip"] == "10.0.10.20" and "PC2" in a.hosts
        got = await m.set_host(13, "PC2", {"ip": "10.0.10.99", "mask": "24", "gateway": "10.0.10.254"})
        assert got["ip"] == "10.0.10.99"
        assert parse_host_snapshot(a.snapshots["PC2"])["ip"] == "10.0.10.20"   # 快照不被覆盖
        assert a.ports["PC2"] == 13 and 13 in m.hub.pinned and a.commands == 1
        await m.set_host(13, "PC2", {"ip": "10.0.10.98", "mask": "24", "gateway": "10.0.10.1", "dns": "8.8.8.8"})
        await m.flush_logs()
        logs = db.attempt_logs(a.attempt_id)
        assert [l["command"] for l in logs] == [
            "[PC 配置] PC2 ip=10.0.10.99 mask=24 gw=10.0.10.254 dns=-",
            "[PC 配置] PC2 ip=10.0.10.98 mask=24 gw=10.0.10.1 dns=8.8.8.8"]
        assert logs[0]["device"] == "PC2" and logs[0]["output"].startswith("已生效")
        assert parse_host_snapshot(db.get_configs(a.attempt_id)["PC2"])["ip"] == "10.0.10.20"   # 快照落库
        # 不开放的 PC（PC1 在 hosts 里但不可访问、PC3 不是本题设备）：拒绝
        for port, name in ((12, "PC1"), (14, "PC3")):
            with pytest.raises(PracticeError, match="不开放") as e:
                await m.set_host(port, name, {"ip": "10.0.10.77", "mask": "24", "gateway": ""})
            assert e.value.status == 403
        res = await m.end()
        assert res["reset"]["PC2"]["status"] == "ok"
        assert m.lab.hosts[13] == {"ip": "10.0.10.20", "mask": "255.255.255.0", "gateway": "10.0.10.1"}
        assert m.lab.hosts[12] == {"ip": "", "mask": "", "gateway": ""}            # hosts 里的 PC1 照旧还原
        assert res["commands"] == 2
    run(go())


def test_set_host_takes_snapshot_when_missing(pcm):
    """兜底：没有开练快照的开放 PC（例如旧版本恢复的练习）在第一次修改前补拍，结束时照样还原。"""
    async def go():
        m = pcm()
        await m.start(_sid(), "pc-bubble", seed=0)
        a = m.active
        a.snapshots.pop("PC2")
        await m.set_host(13, "PC2", {"ip": "10.0.10.99", "mask": "24", "gateway": ""})
        assert parse_host_snapshot(a.snapshots["PC2"])["ip"] == "10.0.10.20"
        assert parse_host_snapshot(db.get_configs(a.attempt_id)["PC2"])["ip"] == "10.0.10.20"
        res = await m.end()
        assert res["reset"]["PC2"]["status"] == "ok" and m.lab.hosts[13]["ip"] == "10.0.10.20"
    run(go())


def test_set_host_failure_is_logged_and_raised(pcm):
    async def go():
        m = pcm()
        await m.start(_sid(), "pc-bubble", seed=0)
        m.lab.fail = "eNSP Pro 设置 PC 地址失败：timeout"
        with pytest.raises(PracticeError, match="timeout"):
            await m.set_host(13, "PC2", {"ip": "10.0.10.99", "mask": "24", "gateway": ""})
        await m.flush_logs()
        logs = db.attempt_logs(m.active.attempt_id)
        assert logs[-1]["output"].startswith("设置失败")
        m.lab.fail = ""
        await m.end()
    run(go())


def test_set_host_snapshot_survives_restart(pcm):
    """服务重启后恢复的练习：经气泡改过的 PC 照样随复位还原（端口与快照都已落库）。"""
    async def go():
        lab = Lab()
        m = pcm(lab)
        await m.start(_sid(), "pc-bubble", seed=0)
        await m.set_host(13, "PC2", {"ip": "10.0.10.99", "mask": "24", "gateway": ""})
        await m.flush_logs()
        m2 = pcm(lab)
        m2.recover()
        assert m2.active is not None and m2.active.ports["PC2"] == 13
        res = await m2.end()
        assert res["reset"]["PC2"]["status"] == "ok" and lab.hosts[13]["ip"] == "10.0.10.20"
    run(go())


def test_set_host_dns_is_snapshotted_and_restored(pcm):
    async def go():
        lab = Lab(dns=True)
        m = pcm(lab)
        await m.start(_sid(), "pc-bubble", seed=0)
        await m.set_host(13, "PC2", {"ip": "10.0.10.20", "mask": "24", "gateway": "10.0.10.1", "dns": "1.1.1.1"})
        assert lab.hosts[13]["dns"] == "1.1.1.1"
        await m.end()
        assert lab.hosts[13]["dns"] == "" and lab.sets[-1][1]["dns"] == ""
    run(go())


def test_host_snapshot_dns_compat():
    old = host_snapshot({"ip": "10.0.0.1", "mask": "24", "gateway": ""})
    assert "dns" not in parse_host_snapshot(old)                       # 旧快照没有 dns
    new = host_snapshot({"ip": "10.0.0.1", "mask": "24", "gateway": "", "dns": "8.8.8.8"})
    assert parse_host_snapshot(new)["dns"] == "8.8.8.8"
    # dns 只在两边都有时比较：旧的干净基线不会因为读到 DNS 而每次都触发复位
    assert same_host(parse_host_snapshot(old), {"ip": "10.0.0.1", "mask": "255.255.255.0", "gateway": "", "dns": "9.9.9.9"})
    assert not same_host({"ip": "10.0.0.1", "mask": "24", "dns": "8.8.8.8"}, {"ip": "10.0.0.1", "mask": "24", "dns": ""})


# ---------------- PC 设置接口（假 eNSP Pro） ----------------

@pytest.fixture
def pro(db_path, monkeypatch):
    from fastapi.testclient import TestClient

    net = FakeNet([(("T1", "GE0/0/1"), ("T2", "GE0/0/1"))])
    t1 = net.add("T1", NetYunShan(BASE, password=PW))
    t2 = net.add("T2", NetYunShan(BASE, password=PW))
    rest = FakeRest()
    rest.pc = True
    rest.status[12] = 4
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "JUDGE_DEBOUNCE", 60.0)
    with ServerThread({6: t1, 7: t2}) as server:
        def make(kind=None, **kw):
            client = EnsproClient(f"http://127.0.0.1:{server.port}", transport=rest.transport())
            p = EnsproProvider(client=client, sandbox=2, password=lambda: PW, cache_ttl=0,
                               quiet=0.15, login_timeout=8, heartbeat=0)
            p.stop_poll = 0.02
            return p
        monkeypatch.setattr(lab_lib, "make_provider", make)
        main.SESSIONS.clear()
        with TestClient(main.app) as c:
            yield c, rest
        main.SESSIONS.clear()


def test_host_api_requires_login_and_validates(pro):
    c, rest = pro
    assert c.get("/api/lab/devices/12/host").status_code == 401
    assert c.post("/api/lab/devices/12/host", json={"ip": "10.0.10.9", "mask": "24"}).status_code == 401
    login(c)
    r = c.get("/api/lab/devices/12/host")
    assert r.status_code == 200, r.text
    assert r.json()["device"] == "PC1" and r.json()["status"] == "online" and r.json()["config"]["ip"] == ""
    for body, msg in (({"ip": "10.0.10.300", "mask": "24"}, "IP"), ({"ip": "10.0.10.9"}, "掩码"),
                      ({"ip": "10.0.10.9", "mask": "33"}, "掩码"), ({"ip": "10.0.10.9", "mask": "24", "dns": "x"}, "DNS")):
        r = c.post("/api/lab/devices/12/host", json=body)
        assert r.status_code == 422 and msg in r.json()["detail"], (body, r.text)
    r = c.post("/api/lab/devices/12/host", json={"ip": "10.0.10.9", "mask": "24", "gateway": "10.0.10.1"})
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {"ip": "10.0.10.9", "mask": "255.255.255.0", "gateway": "10.0.10.1", "dns": ""}
    assert rest.attrs[12]["ipv4.config.ip"] == "10.0.10.9" and r.json()["dns_supported"]
    r = c.post("/api/lab/devices/12/host", json={"ip": "10.0.10.9", "mask": "24", "gateway": "", "dns": "8.8.4.4"})
    assert r.status_code == 200 and rest.attrs[12]["ipv4.config.dns1"] == "8.8.4.4"   # 真机属性名（§6.2）
    assert "ipv4.config.dns" not in rest.attrs[12]
    assert r.json()["config"]["dns"] == "8.8.4.4" and r.json()["dns_supported"]
    # 路由器不是 PC：拒绝；不存在的设备 404
    assert c.post("/api/lab/devices/6/host", json={"ip": "10.0.10.9", "mask": "24"}).status_code == 400
    assert c.get("/api/lab/devices/99/host").status_code == 404
    # 已停止的 PC 不能设置
    rest.status[12] = 0
    assert c.post("/api/lab/devices/12/host", json={"ip": "10.0.10.9", "mask": "24"}).status_code == 409


def test_host_api_refused_on_classic_ensp(ex):
    login(ex)
    for m, body in (("get", None), ("post", {"ip": "10.0.10.9", "mask": "24"})):
        r = getattr(ex, m)("/api/lab/devices/2000/host", **({"json": body} if body else {}))
        assert r.status_code == 400 and "eNSP 软件中双击" in r.json()["detail"]
