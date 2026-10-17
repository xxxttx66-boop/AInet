"""考试模式（spec §8，里程碑 6）：考试模板 + 逐题作答 + 「提交本题」验收 + 服务端计时 + 成绩单 / 命令回放。

考试模板（exam_templates.config_json，normalize_config 补默认值并校验）：
  questions[{scenario, seed: 整数 | null(开考时随机), weight}]、duration_s（总时长）、question_duration_s（单题时长，
  可空）、hints{allowed, penalty（每级扣本题总分）, max_level}、weights{result, process, side}（覆盖默认三维满分）、
  retries（每题提交失败后还能再提交几次）、show_topology、auto_judge（考试默认关闭）、record_replay、shuffle。

状态机（exams.state_json，每次变化都落库，服务重启后从库里恢复，时间照常流逝）：
  考试  running ──交卷 / 总时长到──> finishing（当前题复位未完成时）──> finished
  每题  pending ─开始─> starting ─成功─> active ─提交（可多次）/ 下一题 / 超时─> ending ─复位成功─> done
               └─开始失败：留在 pending 并记 error（学员可「重新开始本题」或「跳过」）
        没开始的题在交卷时记为 unanswered（未作答，0 分）。
  - 本题结果在进入 ending 时就冻结：还有提交机会且未通过时，系统按当前设备状态代为提交一次（计入次数）；
    之后的复位（场景 restore + 整机复位）暂不可执行（学员停在系统视图等）时，题目停在 ending，计时器每
    EXAM_RESET_RETRY 秒重试，界面提示学员回到用户视图；复位完成前不会开始下一题（下一题要用同一批设备）。
  - 一题的底层能力（快照、baseline / inject、确认故障生效、隐藏事务、命令与输出采集、复位、异步评分）全部复用
    PracticeManager（mode="exam"），attempts.mode = "exam" 并关联 exam_id / exam_index。

计时（以服务端为准，clock 可注入，测试用假时钟）：
  - 总截止时间 deadline 存在库里（绝对时间）。系统注入故障、复位设备的耗时不算学员的时间：每次「开始一题」
    「结束一题」的操作耗时都会加到 deadline 上（同样落库）。服务停机期间时间照常流逝。
  - 单题截止时间在该题进入 active 时设定。
  - 计时器 tick() 每 EXAM_TICK 秒检查一次：总时长到 → 自动交卷；单题超时 → 自动进入下一题。

答案保护：考试进行中，所有接口都不返回 checks / solution / inject / reference_path / scoring_points
（以及种子与参数取值）；错误信息里的场景命令替换成「（已隐藏）」；本场考试各题的练习结果接口在交卷前拒绝。
成绩单只在交卷后给出，含 AI 点评和参考思路，不给 checks 原文。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

import config
import db
import scenarios as sc
from practice import PracticeError, PracticeManager, new_seed

log = logging.getLogger("ainet.exam")

DEFAULT_CONFIG: dict[str, Any] = {
    "questions": [],
    "duration_s": 1800,
    "question_duration_s": None,
    "hints": {"allowed": False, "penalty": 5, "max_level": 3},
    "weights": None,
    "retries": 1,
    "show_topology": True,
    "auto_judge": False,
    "record_replay": True,
    "shuffle": False,
}

# 单题考试（课程页 / 操作页「单题考试」：本题的考试，默认规则，spec §8）：临时单题模板，不进模板表（template_id 为空）
SINGLE_CONFIG: dict[str, Any] = {
    "duration_s": 900, "question_duration_s": None,
    "hints": {"allowed": False, "penalty": 5, "max_level": 3},
    "weights": None, "retries": 1, "show_topology": True, "auto_judge": False, "record_replay": True, "shuffle": False,
}
SINGLE_PREFIX = "单题考试 · "

BUILTIN_SETTING = "exam_builtin_seeded"
BUILTIN_NAME = "Campus 基础排障测验"
BUILTIN_CONFIG: dict[str, Any] = {
    "questions": [{"scenario": "campus-ar0-ar2-link", "seed": None},
                  {"scenario": "campus-lsw0-access-vlan", "seed": None},
                  {"scenario": "campus-ar2-return-route", "seed": None}],
    "duration_s": 2700, "question_duration_s": 900,
    "hints": {"allowed": True, "penalty": 5, "max_level": 2},
    "weights": {"result": 50, "process": 35, "side": 15},
    "retries": 1, "show_topology": True, "auto_judge": False, "record_replay": True, "shuffle": False,
}
BUILTIN_YUNSHAN_SETTING = "exam_builtin_yunshan_seeded"
BUILTIN_YUNSHAN_NAME = "Campus 基础排障测验（YunShan）"
BUILTIN_YUNSHAN_CONFIG: dict[str, Any] = {
    "questions": [{"scenario": "campus-ar0-ar2-link-ys", "seed": None},
                  {"scenario": "campus-lsw0-access-vlan-ys", "seed": None},
                  {"scenario": "campus-ar2-return-route-ys", "seed": None}],
    "duration_s": 2700, "question_duration_s": 900,
    "hints": {"allowed": True, "penalty": 5, "max_level": 2},
    "weights": {"result": 50, "process": 35, "side": 15},
    "retries": 1, "show_topology": True, "auto_judge": False, "record_replay": True, "shuffle": False,
}


class ExamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


# ---------------- 模板 ----------------

def _num(v: Any, name: str, lo: float, hi: float, integer: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ExamError(422, f"{name} 必须是数字")
    if not lo <= v <= hi:
        raise ExamError(422, f"{name} 应在 {lo:g}~{hi:g} 之间")
    if integer and int(v) != v:
        raise ExamError(422, f"{name} 必须是整数")
    return int(v) if integer else float(v)


def normalize_config(raw: Any, scenario_ids: set[str] | None = None) -> dict[str, Any]:
    """校验并补全模板配置。scenario_ids 给出时要求题目都在场景库里。"""
    if not isinstance(raw, dict):
        raise ExamError(422, "考试配置必须是 JSON 对象")
    unknown = set(raw) - set(DEFAULT_CONFIG)
    if unknown:
        raise ExamError(422, f"未知的配置项：{'、'.join(sorted(unknown))}")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    qs = raw.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ExamError(422, "questions 至少一题")
    if len(qs) > 50:
        raise ExamError(422, "questions 最多 50 题")
    out_q = []
    for i, q in enumerate(qs):
        if isinstance(q, str):
            q = {"scenario": q}
        if not isinstance(q, dict) or not isinstance(q.get("scenario"), str) or not q["scenario"]:
            raise ExamError(422, f"第 {i + 1} 题需要 scenario（场景 id）")
        extra = set(q) - {"scenario", "seed", "weight"}
        if extra:
            raise ExamError(422, f"第 {i + 1} 题有未知字段：{'、'.join(sorted(extra))}")
        if scenario_ids is not None and q["scenario"] not in scenario_ids:
            raise ExamError(422, f"第 {i + 1} 题的场景不存在：{q['scenario']}")
        seed = q.get("seed")
        if seed is not None:
            seed = _num(seed, f"第 {i + 1} 题的 seed", 0, 2**31 - 1, integer=True)
        weight = _num(q.get("weight", 1), f"第 {i + 1} 题的 weight", 0, 100)
        out_q.append({"scenario": q["scenario"], "seed": seed, "weight": weight})
    if sum(q["weight"] for q in out_q) <= 0:
        raise ExamError(422, "题目权重之和必须大于 0")
    cfg["questions"] = out_q
    if "duration_s" in raw:
        cfg["duration_s"] = _num(raw["duration_s"], "duration_s（总时长，秒）", 1, 86400, integer=True)
    if raw.get("question_duration_s") is not None:
        cfg["question_duration_s"] = _num(raw["question_duration_s"], "question_duration_s（单题时长，秒）", 1, 86400,
                                          integer=True)
    h = raw.get("hints", {})
    if not isinstance(h, dict) or set(h) - {"allowed", "penalty", "max_level"}:
        raise ExamError(422, "hints 应为 {allowed, penalty, max_level}")
    cfg["hints"].update({k: v for k, v in h.items()})
    cfg["hints"]["allowed"] = bool(cfg["hints"]["allowed"])
    cfg["hints"]["penalty"] = _num(cfg["hints"]["penalty"], "hints.penalty（每级提示扣分）", 0, 100)
    cfg["hints"]["max_level"] = _num(cfg["hints"]["max_level"], "hints.max_level", 1, 3, integer=True)
    w = raw.get("weights")
    if w is not None:
        if not isinstance(w, dict) or set(w) - {"result", "process", "side"}:
            raise ExamError(422, "weights 应为 {result, process, side}")
        ww = {k: _num(w.get(k, 0), f"weights.{k}", 0, 100) for k in ("result", "process", "side")}
        if sum(ww.values()) <= 0:
            raise ExamError(422, "weights 之和必须大于 0")
        cfg["weights"] = ww
    if "retries" in raw:
        cfg["retries"] = _num(raw["retries"], "retries（每题允许重试次数）", 0, 20, integer=True)
    for k in ("show_topology", "auto_judge", "record_replay", "shuffle"):
        if k in raw:
            if not isinstance(raw[k], bool):
                raise ExamError(422, f"{k} 必须是 true / false")
            cfg[k] = raw[k]
    return cfg


def platform_mismatch(cfg: dict[str, Any], scenarios: dict[str, dict[str, Any]], platform: str) -> list[tuple[int, str, str]]:
    """模板里平台与 platform 不符的题：[(题序, 标题, 题目平台)]。"""
    out = []
    for i, q in enumerate(cfg.get("questions") or []):
        s = scenarios.get(q.get("scenario"))
        if s is not None and s.get("platform", "vrp5") != platform:
            out.append((i, s.get("title", q["scenario"]), s.get("platform", "vrp5")))
    return out


def template_platforms(cfg: dict[str, Any], scenarios: dict[str, dict[str, Any]]) -> list[str]:
    """模板用到的设备平台（去重，按出现顺序）。"""
    out: list[str] = []
    for q in cfg.get("questions") or []:
        s = scenarios.get(q.get("scenario") if isinstance(q, dict) else q)
        p = (s or {}).get("platform", "vrp5")
        if s is not None and p not in out:
            out.append(p)
    return out


def template_view(row: dict[str, Any]) -> dict[str, Any]:
    try:
        cfg = json.loads(row["config_json"] or "{}")
    except ValueError:
        cfg = {}
    return {"id": row["id"], "name": row["name"], "config": cfg,
            "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}


# ---------------- 考试管理 ----------------

class ExamManager:
    def __init__(self, pm: PracticeManager, *, clock: Callable[[], float] = time.time,
                 tick_s: float | None = None) -> None:
        self.pm = pm
        self.clock = clock
        self.tick_s = config.EXAM_TICK if tick_s is None else tick_s
        self.exam: dict[str, Any] | None = None       # 进行中的考试（内存态，与 exams 表同步）
        self.last_exam_id: dict[int, int] = {}        # 学生 -> 最近交卷的考试（前端跳成绩单）
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_reset_try = 0.0
        self._agg_pending: set[int] = set()           # 已交卷、等评分汇总的考试

    # ---- 生命周期 ----

    def scenario_ids(self) -> set[str]:
        return set(self.pm.scenarios)

    def ensure_builtin(self) -> None:
        """首次启动时内置两种设备平台的示例模板（删除后不会自动重建）。"""
        ids = self.scenario_ids()
        existing_names = {t["name"] for t in db.list_templates()}
        for setting, name, source in (
            (BUILTIN_SETTING, BUILTIN_NAME, BUILTIN_CONFIG),
            (BUILTIN_YUNSHAN_SETTING, BUILTIN_YUNSHAN_NAME, BUILTIN_YUNSHAN_CONFIG),
        ):
            if db.get_setting(setting):
                continue
            qs = [q for q in source["questions"] if q["scenario"] in ids]
            if qs and name not in existing_names:
                cfg = normalize_config({**source, "questions": qs}, ids)
                db.create_template(name, json.dumps(cfg, ensure_ascii=False))
            db.set_setting(setting, "1")

    def recover(self) -> None:
        """服务启动：恢复进行中的考试（practice.recover 已恢复当前题的 active）。"""
        for row in db.running_exams():
            try:
                ex = self._load(row)
            except Exception:
                log.exception("恢复考试 %s 失败，标记为已结束", row["id"])
                db.update_exam(row["id"], status="finished", ended_at=_iso(self.clock()))
                continue
            if self.exam is not None:          # 单人系统不应出现两场；多余的直接结束
                db.update_exam(row["id"], status="finished", ended_at=_iso(self.clock()))
                continue
            self.exam = ex
            a = self.pm.active
            for i, q in enumerate(ex["state"]["questions"]):
                if q["status"] in ("active", "ending") and not (a is not None and a.attempt_id == q.get("attempt_id")):
                    # 当前题的练习态没能恢复（场景文件被删等）：只能按已冻结的结果收尾
                    q["status"] = "done"
                    q["error"] = "服务重启后无法恢复本题的设备状态，本题按已有结果计"
                elif q["status"] == "starting":
                    q["status"] = "pending"
                    q["error"] = "服务重启，本题开始被中断，请点「重新开始本题」"
            self._persist()
        for row in db.list_exams_scoring():
            self._agg_pending.add(row["id"])

    def _load(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "student_id": row["student_id"], "template_id": row["template_id"],
                "name": row["template_name"], "config": json.loads(row["config_json"]),
                "started_at": row["started_at"], "deadline": _ts(row["deadline_at"]),
                "state": json.loads(row["state_json"]), "status": row["status"]}

    def start_loop(self) -> None:
        if self._task is None and self.tick_s > 0:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await self.tick()
            except Exception:
                log.exception("考试计时器出错")

    def _persist(self) -> None:
        ex = self.exam
        if ex is None:
            return
        db.update_exam(ex["id"], status=ex["status"], deadline_at=_iso(ex["deadline"]),
                       state_json=json.dumps(ex["state"], ensure_ascii=False))

    @asynccontextmanager
    async def _system_time(self):
        """系统操作（注入 / 复位）耗时不计入考试时间：结束后顺延总截止时间。"""
        t0 = self.clock()
        try:
            yield
        finally:
            ex = self.exam
            if ex is not None:
                dt = max(0.0, self.clock() - t0)
                ex["deadline"] += dt
                ex["state"]["paused_s"] = round(ex["state"].get("paused_s", 0) + dt, 1)
                self._persist()

    # ---- 查询 ----

    def owner(self, student_id: int) -> dict[str, Any]:
        ex = self.exam
        if ex is None:
            raise ExamError(409, "没有进行中的考试")
        if ex["student_id"] != student_id:
            raise ExamError(403, "这不是你的考试")
        return ex

    def hide_topology(self) -> bool:
        ex = self.exam
        return ex is not None and not ex["config"].get("show_topology", True)

    def running_for(self, exam_id: int | None) -> bool:
        return exam_id is not None and self.exam is not None and self.exam["id"] == exam_id

    def _cur(self, ex: dict[str, Any]) -> dict[str, Any] | None:
        st = ex["state"]
        i = st.get("current", 0)
        return st["questions"][i] if 0 <= i < len(st["questions"]) else None

    def state(self, student_id: int | None = None) -> dict[str, Any]:
        """GET /api/exam/current。只含学员能看的信息（工单、设备、提交结果、计时），不含任何答案字段。"""
        ex = self.exam
        if ex is None:
            return {"active": False, "last_exam_id": self.last_exam_id.get(student_id) if student_id else None}
        if student_id is not None and ex["student_id"] != student_id:
            return {"active": False, "other_running": True}
        now = self.clock()
        cfg = ex["config"]
        st = ex["state"]
        q = self._cur(ex)
        a = self.pm.active
        qv: dict[str, Any] | None = None
        if q is not None:
            on_dev = a is not None and a.mode == "exam" and a.attempt_id == q.get("attempt_id")
            subs = (a.submissions if on_dev else q.get("submissions")) or []
            maxs = 1 + cfg["retries"]
            # 自动判定开启时（auto_judge）也可能在没有提交的情况下通过
            passed = any(x.get("passed") for x in subs) or (on_dev and a.passed_at is not None) or bool(q.get("passed"))
            qv = {
                "index": st["current"], "status": q["status"],
                "scenario_id": cfg["questions"][st["current"]]["scenario"],
                "title": q.get("title"),
                "symptom": q.get("symptom"),
                "devices": ([{"name": d, "port": a.ports[d]} for d in a.scenario["accessible_devices"]]
                            if on_dev else []),
                "submissions": subs, "max_submissions": maxs, "remaining_submissions": max(0, maxs - len(subs)),
                "passed": passed, "locked": passed or len(subs) >= maxs,
                "hints_used": a.hints_used if on_dev else q.get("hints_used", 0),
                "hints": q.get("hints", []),
                "started_at": q.get("started_at"),
                "remaining_s": (max(0, int(q["deadline"] - now)) if q.get("deadline") else None),
                "error": q.get("error"), "reset_error": q.get("reset_error"),
                "timed_out": q.get("timed_out", False),
                "last_check": (self.pm.state(a).get("last_check") if on_dev and a.auto_judge else None),
            }
        return {
            "active": True, "exam_id": ex["id"], "name": ex["name"], "status": ex["status"],
            "single": bool(st.get("single")),
            "phase": st.get("phase", "running"), "started_at": ex["started_at"],
            "deadline_at": _iso(ex["deadline"]), "remaining_s": max(0, int(ex["deadline"] - now)),
            "server_time": _iso(now), "total": len(st["questions"]), "current": st.get("current", 0),
            "questions": [{"index": i, "title": x.get("title"), "status": x["status"],
                           "passed": (any(s.get("passed") for s in x.get("submissions") or []) or bool(x.get("passed"))
                                      or (qv is not None and i == st["current"] and qv["passed"]))}
                          for i, x in enumerate(st["questions"])],
            "question": qv,
            "config": {"duration_s": cfg["duration_s"], "question_duration_s": cfg["question_duration_s"],
                       "retries": cfg["retries"], "show_topology": cfg["show_topology"],
                       "auto_judge": cfg["auto_judge"], "record_replay": cfg["record_replay"],
                       "hints_allowed": cfg["hints"]["allowed"], "hint_penalty": cfg["hints"]["penalty"],
                       "hint_max_level": cfg["hints"]["max_level"]},
        }

    # ---- 开考 ----

    def _check_free(self, student_id: int) -> None:
        if self.exam is not None:
            raise ExamError(409, "已有进行中的考试" if self.exam["student_id"] == student_id
                            else "其他学生的考试进行中")
        if self.pm.resetting:
            raise ExamError(409, "上一次练习正在后台复位设备，请稍候（通常几十秒）再开始考试")
        if self.pm.active is not None:
            raise ExamError(409, "有进行中的练习，请先结束练习再开始考试")

    async def start(self, student_id: int, template_id: int) -> dict[str, Any]:
        async with self._lock:
            self._check_free(student_id)
            row = await asyncio.to_thread(db.get_template, template_id)
            if row is None:
                raise ExamError(404, "考试模板不存在")
            try:
                cfg = normalize_config(json.loads(row["config_json"] or "{}"), self.scenario_ids())
            except ValueError:
                raise ExamError(422, "模板配置不是合法 JSON") from None
            return await self._start_exam(student_id, template_id, row["name"], cfg)

    async def start_single(self, student_id: int, scenario_id: str) -> dict[str, Any]:
        """单题考试：本题的考试，默认规则（计时 15 分钟、无提示、拓扑可见、交卷后出成绩单；考试中 Agent 一律不可用）。

        复用考试引擎：临时单题模板（不进模板表）。与模板考试不同，题目没能开始（设备忙、故障没生效等）时整场回滚
        （删掉考试记录、不计时）并返回 409，调用方稍后重试即可（YunShan 刚冷启动时「system is busy」，课程页每 20 秒重试）。"""
        async with self._lock:
            self._check_free(student_id)
            s = self.pm.scenarios.get(scenario_id)
            if s is None:
                raise ExamError(404, f"场景不存在：{scenario_id}")
            cfg = normalize_config({**SINGLE_CONFIG, "questions": [{"scenario": scenario_id, "seed": None}]},
                                   self.scenario_ids())
            title = sc.public(sc.expand(s, 0))["title"]
            # 题名不再带「（YunShan）」后缀，两个平台可能同名：考试名末尾带平台，档案的考试记录里能分清
            name = f"{SINGLE_PREFIX}{title} · {sc.PLATFORM_TAGS.get(sc.platform_of(s), sc.platform_of(s))}"
            return await self._start_exam(student_id, None, name, cfg, single=True)

    async def _start_exam(self, student_id: int, template_id: int | None, name: str, cfg: dict[str, Any],
                          single: bool = False) -> dict[str, Any]:
        # 开始计时前核对 eNSP 拓扑与每道题一致（spec §8 / §6.3）
        cur = await self.pm.resolve()
        # 先核对平台：模板里有与当前模拟环境平台不符的题，一次说清楚是哪几道（spec §5.2）
        lab_info = cur.get("lab") or {}
        bad = platform_mismatch(cfg, self.pm.scenarios, lab_info.get("platform", "vrp5"))
        if bad:
            items = "；".join(f"第 {i + 1} 题「{t}」（{sc.PLATFORMS.get(p, p)}）" for i, t, p in bad)
            raise ExamError(409, f"考试模板「{name}」有 {len(bad)} 道题与当前模拟环境 {lab_info.get('label', '')}"
                                 f"（{sc.PLATFORMS.get(lab_info.get('platform', 'vrp5'), '')}）不匹配：{items}。"
                                 "请切换模拟环境，或修改模板")
        for i, q in enumerate(cfg["questions"]):
            rd = sc.readiness(self.pm.scenarios[q["scenario"]], cur)
            if not rd["ready"]:
                raise ExamError(409, f"第 {i + 1} 题未就绪：{rd['reason']}")
        order = list(range(len(cfg["questions"])))
        order_seed = new_seed()
        if cfg["shuffle"]:
            random.Random(order_seed).shuffle(order)
        questions = []
        for k in order:
            q = cfg["questions"][k]
            seed = q["seed"] if q["seed"] is not None else new_seed()
            pub = sc.public(sc.expand(self.pm.scenarios[q["scenario"]], seed))
            questions.append({"scenario": q["scenario"], "seed": seed, "weight": q["weight"], "source_index": k,
                              "title": pub["title"], "symptom": pub["symptom"], "status": "pending",
                              "attempt_id": None, "submissions": [], "hints_used": 0, "hints": []})
        now = self.clock()
        state = {"questions": questions, "current": 0, "phase": "running", "order_seed": order_seed,
                 "paused_s": 0.0, **({"single": True} if single else {})}
        exam_id = await asyncio.to_thread(
            db.create_exam, student_id, template_id, name, json.dumps(cfg, ensure_ascii=False),
            _iso(now), _iso(now + cfg["duration_s"]), json.dumps(state, ensure_ascii=False))
        self.exam = {"id": exam_id, "student_id": student_id, "template_id": template_id, "name": name,
                     "config": cfg, "started_at": _iso(now), "deadline": now + cfg["duration_s"],
                     "state": state, "status": "running"}
        await self._start_question(0)
        q0 = state["questions"][0]
        if single and q0["status"] != "active":
            # 单题考试：题目没能开始就整场回滚（不留一场停在「开题失败」的考试、不计时），调用方稍后重试
            self.exam = None
            await asyncio.to_thread(db.delete_exam, exam_id)
            raise ExamError(409, q0.get("error") or "本题没能开始，请稍后重试")
        return self.state(student_id)

    async def _start_question(self, i: int) -> None:
        ex = self.exam
        assert ex is not None
        st = ex["state"]
        st["current"] = i
        q = st["questions"][i]
        q["status"] = "starting"
        q.pop("error", None)
        self._persist()
        cfg = ex["config"]
        try:
            async with self._system_time():
                await self.pm.start(ex["student_id"], q["scenario"], q["seed"], mode="exam", exam_id=ex["id"],
                                    exam_template_id=ex["template_id"], exam_index=i,
                                    auto_judge=cfg["auto_judge"], max_submissions=1 + cfg["retries"])
        except PracticeError as exc:
            q["status"] = "pending"
            q["error"] = exc.detail
            self._persist()
            log.warning("考试 %s 第 %s 题开始失败：%s", ex["id"], i + 1, exc.detail)
            return
        a = self.pm.active
        now = self.clock()
        q.update(status="active", attempt_id=a.attempt_id, started_at=_iso(now), submissions=[], hints_used=0)
        if cfg["question_duration_s"]:
            q["deadline"] = now + cfg["question_duration_s"]
        self._persist()

    # ---- 学员操作 ----

    def _active_question(self, ex: dict[str, Any]) -> dict[str, Any]:
        q = self._cur(ex)
        a = self.pm.active
        if q is None or q["status"] != "active" or a is None or a.attempt_id != q.get("attempt_id"):
            raise ExamError(409, "当前没有可作答的题目")
        return q

    async def submit(self, student_id: int) -> dict[str, Any]:
        async with self._lock:
            ex = self.owner(student_id)
            q = self._active_question(ex)
            try:
                r = await self.pm.submit()
            except PracticeError as exc:
                raise ExamError(exc.status, exc.detail) from None
            a = self.pm.active
            if a is not None and a.attempt_id == q["attempt_id"]:
                q["submissions"] = list(a.submissions)
            self._persist()
            return {"submission": r, **self.state(student_id)}

    async def hint(self, student_id: int, level: int) -> dict[str, Any]:
        ex = self.owner(student_id)
        h = ex["config"]["hints"]
        if not h["allowed"]:
            raise ExamError(403, "本场考试不允许使用提示")
        if level > h["max_level"]:
            raise ExamError(403, f"本场考试最多只能使用第 {h['max_level']} 级提示")
        q = self._active_question(ex)
        try:
            r = await self.pm.hint(level, mode="exam")
        except PracticeError as exc:
            raise ExamError(exc.status, exc.detail) from None
        if self.exam is ex and q.get("attempt_id") == (self.pm.active.attempt_id if self.pm.active else None):
            q["hints_used"] = r["hints_used"]
            hs = q.setdefault("hints", [])
            while len(hs) < level:
                hs.append(None)
            hs[level - 1] = {"text": r["text"], "source": r["source"]}
            self._persist()
        return {**r, "penalty_per_level": h["penalty"]}

    async def next(self, student_id: int) -> dict[str, Any]:
        """结束当前题（自动提交一次 → 复位 → 评分）并开始下一题；当前题开始失败时为「跳过本题」。"""
        async with self._lock:
            ex = self.owner(student_id)
            if ex["state"].get("phase") == "finishing":
                await self._try_finish(ex)
                return self.state(student_id)
            q = self._cur(ex)
            if q is None:
                raise ExamError(409, "没有下一题")
            if q["status"] == "pending":
                q["status"] = "unanswered"
                q["error"] = None
            elif q["status"] in ("active", "ending"):
                if not await self._end_question(q):
                    return self.state(student_id)        # 复位暂不可执行：停在 ending，计时器重试
            await self._advance()
            return self.state(student_id)

    async def retry_start(self, student_id: int) -> dict[str, Any]:
        async with self._lock:
            ex = self.owner(student_id)
            q = self._cur(ex)
            if q is None or q["status"] != "pending":
                raise ExamError(409, "当前题不需要重新开始")
            await self._start_question(ex["state"]["current"])
            return self.state(student_id)

    async def finish(self, student_id: int) -> dict[str, Any]:
        """交卷：当前题按设备状态自动提交并复位，没开始的题记为未作答。"""
        async with self._lock:
            ex = self.owner(student_id)
            await self._try_finish(ex)
            st = self.state(student_id)
            return st if self.exam is not None else {"active": False, "finished": True, "exam_id": ex["id"]}

    # ---- 内部流程 ----

    def _score_opts(self, ex: dict[str, Any], q: dict[str, Any]) -> dict[str, Any]:
        cfg = ex["config"]
        return {"weights": cfg["weights"], "hint_penalty": cfg["hints"]["penalty"], "hint_penalty_on": "total",
                "exam": {"exam_id": ex["id"], "index": ex["state"]["current"],
                         "submissions": q.get("submissions") or [], "max_submissions": 1 + cfg["retries"],
                         "auto_submitted": bool(q.get("auto_submitted")), "timed_out": bool(q.get("timed_out")),
                         "time_limit_s": cfg["question_duration_s"]}}

    async def _end_question(self, q: dict[str, Any], timed_out: bool = False) -> bool:
        """冻结本题结果（必要时代为提交一次）→ 复位 → 评分。复位完成返回 True。"""
        ex = self.exam
        assert ex is not None
        a = self.pm.active
        if q["status"] == "active":
            if a is not None and a.attempt_id == q["attempt_id"] and not a.locked and a.phase == "running":
                try:
                    r = await self.pm.submit()
                    if r.get("counted"):
                        q["auto_submitted"] = True
                    else:
                        q["auto_submit_error"] = r.get("reason")
                except PracticeError as exc:
                    q["auto_submit_error"] = exc.detail
            if a is not None and a.attempt_id == q["attempt_id"]:
                q["submissions"] = list(a.submissions)
                q["hints_used"] = a.hints_used
            q["status"] = "ending"
            q["timed_out"] = q.get("timed_out", False) or timed_out
            q["ended_at"] = _iso(self.clock())
            self._persist()
        if q["status"] != "ending":
            return True
        a = self.pm.active
        if a is None or a.attempt_id != q["attempt_id"]:
            q["status"] = "done"
            self._persist()
            return True
        self._last_reset_try = self.clock()
        try:
            async with self._system_time():
                res = await self.pm.end(mode="exam", score_opts=self._score_opts(ex, q))
        except PracticeError as exc:
            q["reset_error"] = exc.detail
            self._persist()
            return False
        if res.get("active"):
            q["reset_error"] = res.get("restore_error") or "复位未完成"
            self._persist()
            return False
        q.update(status="done", passed=bool(res.get("passed")), reset_ok=bool(res.get("reset_ok")),
                 commands=res.get("commands"), duration_s=res.get("duration_s"), reset_error=None)
        self._persist()
        return True

    async def _advance(self) -> None:
        ex = self.exam
        assert ex is not None
        st = ex["state"]
        nxt = st["current"] + 1
        if nxt >= len(st["questions"]) or self.clock() >= ex["deadline"]:
            await self._try_finish(ex)
            return
        await self._start_question(nxt)

    async def _try_finish(self, ex: dict[str, Any]) -> None:
        st = ex["state"]
        st["phase"] = "finishing"
        for q in st["questions"]:
            if q["status"] == "pending":
                q["status"] = "unanswered"
        self._persist()
        q = self._cur(ex)
        if q is not None and q["status"] in ("active", "ending"):
            if not await self._end_question(q):
                return
        await self._complete(ex)

    async def _complete(self, ex: dict[str, Any]) -> None:
        st = ex["state"]
        for q in st["questions"]:
            if q["status"] in ("pending", "starting"):
                q["status"] = "unanswered"
        st["phase"] = "finished"
        ex["status"] = "finished"
        now = self.clock()
        await asyncio.to_thread(db.update_exam, ex["id"], status="finished", ended_at=_iso(now),
                                state_json=json.dumps(st, ensure_ascii=False), score_status="scoring")
        self.exam = None
        self.last_exam_id[ex["student_id"]] = ex["id"]
        self._agg_pending.add(ex["id"])
        await asyncio.to_thread(self.aggregate, ex["id"])

    async def tick(self) -> None:
        """计时器：到点自动提交 / 进入下一题 / 交卷；复位未完成的题定期重试。"""
        if self._lock.locked():
            return
        async with self._lock:
            for eid in list(self._agg_pending):
                await asyncio.to_thread(self.aggregate, eid)
            ex = self.exam
            if ex is None:
                return
            now = self.clock()
            st = ex["state"]
            q = self._cur(ex)
            if st.get("phase") == "finishing" or (q is not None and q["status"] == "ending"):
                if now - self._last_reset_try < config.EXAM_RESET_RETRY:
                    return
                if st.get("phase") == "finishing":
                    await self._try_finish(ex)
                elif await self._end_question(q):
                    await self._advance()
                return
            if now >= ex["deadline"]:
                log.info("考试 %s 总时长到，自动交卷", ex["id"])
                if q is not None and q["status"] == "active":
                    q["timed_out"] = True
                await self._try_finish(ex)
                return
            if q is not None and q["status"] == "active" and q.get("deadline") and now >= q["deadline"]:
                log.info("考试 %s 第 %s 题超时，进入下一题", ex["id"], st["current"] + 1)
                if await self._end_question(q, timed_out=True):
                    await self._advance()

    # ---- 成绩 ----

    def aggregate(self, exam_id: int) -> dict[str, Any] | None:
        """按各题评分汇总考试总分（加权平均，未作答记 0）。全部评完才落库为最终分。"""
        row = db.get_exam(exam_id)
        if row is None or row["status"] != "finished":
            self._agg_pending.discard(exam_id)
            return None
        st = json.loads(row["state_json"])
        atts = {a["id"]: a for a in db.exam_attempts(exam_id)}
        total_w = sum(q["weight"] for q in st["questions"]) or 1
        acc, status = 0.0, "done"
        for q in st["questions"]:
            att = atts.get(q.get("attempt_id")) if q.get("attempt_id") else None
            if att is None:
                continue
            s = att.get("score_status")
            if s == "scoring" or s is None:
                status = "scoring"
            elif s == "degraded" and status == "done":
                status = "degraded"
            elif s == "failed" and status in ("done", "degraded"):
                status = "partial"
            acc += q["weight"] * float(att.get("score_total") or 0)
        total = round(acc / total_w, 1)
        db.update_exam(exam_id, score_total=total if status != "scoring" else None, score_status=status)
        if status != "scoring":
            self._agg_pending.discard(exam_id)
        return {"total": total, "status": status}

    def _reference_path(self, q: dict[str, Any], att: dict[str, Any]) -> str:
        scen = self.pm.scenarios.get(q.get("scenario") or "")
        if scen is None:
            return ""
        seed = att.get("seed") if att.get("seed") is not None else q.get("seed")
        try:
            return sc.expand(scen, seed).get("reference_path") or ""
        except Exception:
            return ""

    def report(self, exam_id: int, student_id: int) -> dict[str, Any]:
        """成绩单（交卷后）：各题得分、三项分数、点评、参考思路、用时、命令数、提示数。不含 checks 原文。"""
        row = db.get_exam(exam_id)
        if row is None or row["student_id"] != student_id:
            raise ExamError(404, "考试记录不存在")
        if row["status"] != "finished":
            raise ExamError(409, "考试进行中，交卷后才能查看成绩单")
        agg = self.aggregate(exam_id) or {}
        row = db.get_exam(exam_id) or row
        cfg = json.loads(row["config_json"])
        st = json.loads(row["state_json"])
        atts = {a["id"]: a for a in db.exam_attempts(exam_id)}
        items = []
        for i, q in enumerate(st["questions"]):
            att = atts.get(q.get("attempt_id")) if q.get("attempt_id") else None
            item: dict[str, Any] = {
                "index": i, "title": q.get("title"), "platform": self._q_platform(q),
                "symptom": q.get("symptom"), "weight": q["weight"],
                "status": q["status"], "answered": att is not None,
                "submissions": q.get("submissions") or [], "auto_submitted": bool(q.get("auto_submitted")),
                "timed_out": bool(q.get("timed_out")), "hints_used": q.get("hints_used", 0),
                "attempt_id": q.get("attempt_id"),
            }
            if att is not None:
                try:
                    d = json.loads(att["score_json"]) if att.get("score_json") else {}
                except ValueError:
                    d = {}
                started, ended = _ts(att.get("started_at")), _ts(att.get("ended_at"))
                item.update({
                    "passed": att.get("passed") == 1,
                    "duration_s": int(ended - started) if started and ended else None,
                    "commands": q.get("commands") if q.get("commands") is not None else len(db.attempt_logs(att["id"])),
                    "hints_used": att.get("hints_used") or 0,
                    "score_status": att.get("score_status") or "none",
                    "score_total": att.get("score_total"),
                    "scores": (d.get("scores") or {}).get("dimensions"),
                    "comment": d.get("comment"),
                    "reason": d.get("reason"),
                    # 只给参考思路（AI 讲解或场景 reference_path），不给 checks / solution 原文
                    "standard_path": d.get("standard_path") or "",
                    # 评分是异步的：刚交卷时 score_json 可能还没写好，参考思路回退到按本题种子展开的场景
                    "reference_path": d.get("reference_path") or self._reference_path(q, att),
                    "reset_ok": q.get("reset_ok"),
                })
            else:
                item.update({"passed": False, "score_total": 0, "score_status": "unanswered"})
            items.append(item)
        return {
            "exam_id": row["id"], "name": row["template_name"], "started_at": row["started_at"],
            "ended_at": row["ended_at"], "score_total": row["score_total"] if row["score_total"] is not None
            else agg.get("total"), "score_status": row["score_status"] or agg.get("status"),
            "paused_s": st.get("paused_s", 0),
            "config": {"duration_s": cfg["duration_s"], "question_duration_s": cfg["question_duration_s"],
                       "retries": cfg["retries"], "hints": cfg["hints"], "weights": cfg["weights"],
                       "record_replay": cfg["record_replay"], "show_topology": cfg["show_topology"],
                       "auto_judge": cfg["auto_judge"]},
            "questions": items, "replay_available": bool(cfg["record_replay"]),
        }

    def replay(self, exam_id: int, student_id: int) -> dict[str, Any]:
        """命令回放：按时间顺序给出每道题学员敲过的命令和输出（attempt_logs）。模板关闭回放时拒绝。"""
        row = db.get_exam(exam_id)
        if row is None or row["student_id"] != student_id:
            raise ExamError(404, "考试记录不存在")
        if row["status"] != "finished":
            raise ExamError(409, "考试进行中，交卷后才能查看回放")
        cfg = json.loads(row["config_json"])
        if not cfg.get("record_replay", True):
            raise ExamError(403, "本场考试未开启命令回放")
        st = json.loads(row["state_json"])
        atts = {a["id"]: a for a in db.exam_attempts(exam_id)}
        out = []
        for i, q in enumerate(st["questions"]):
            att = atts.get(q.get("attempt_id")) if q.get("attempt_id") else None
            logs = []
            if att is not None:
                t0 = _ts(att["started_at"])
                for l in db.attempt_logs(att["id"]):
                    t = _ts(l["ts"])
                    logs.append({"ts": l["ts"], "offset_s": int(t - t0) if t is not None and t0 is not None else None,
                                 "device": l.get("device"), "command": l.get("command"), "output": l.get("output") or ""})
            out.append({"index": i, "title": q.get("title"), "platform": self._q_platform(q), "status": q["status"],
                        "started_at": att["started_at"] if att else None, "logs": logs})
        return {"exam_id": exam_id, "name": row["template_name"], "questions": out}

    def _q_platform(self, q: dict[str, Any]) -> str:
        """考试题的平台（成绩单 / 命令回放上与题名并列显示，两个平台的同名题靠它区分）。"""
        s = self.pm.scenarios.get(q.get("scenario"))
        return sc.platform_of(s) if s else "vrp5"

    def score_opts_for_attempt(self, att: dict[str, Any]) -> dict[str, Any] | None:
        """考试题重新评分：按考试当时的模板快照给评分选项。"""
        if not att.get("exam_id"):
            return None
        row = db.get_exam(att["exam_id"])
        if row is None:
            return None
        cfg = json.loads(row["config_json"])
        st = json.loads(row["state_json"])
        q = next((x for x in st["questions"] if x.get("attempt_id") == att["id"]), {})
        return {"weights": cfg["weights"], "hint_penalty": cfg["hints"]["penalty"], "hint_penalty_on": "total",
                "exam": {"exam_id": row["id"], "index": att.get("exam_index"), "submissions": q.get("submissions") or [],
                         "max_submissions": 1 + cfg["retries"], "auto_submitted": bool(q.get("auto_submitted")),
                         "timed_out": bool(q.get("timed_out")), "time_limit_s": cfg["question_duration_s"]}}

