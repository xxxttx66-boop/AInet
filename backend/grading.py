"""评分（spec §9）：结果分确定性计算 + AI 评过程分与规范/副作用分。

- 结果分（缺省 50）：场景 checks 是否全部通过（attempts.passed），不交给 AI。
- 过程分（缺省 35）：AI 给 0~100 得分率 × 权重；再扣提示分（SCORE_HINT_PENALTY × 已用级数，缺省 0）；
  结果未达标时 × SCORE_FAIL_DISCOUNT（缺省 0.6）。
- 规范/副作用分（缺省 15）：先用规则抽取确定性信号（危险 / 多余配置命令、报错、重复命令、学员改配置之后出现的
  接口 / 邻居 Down 日志），连同操作记录交给 AI 综合给得分率 × 权重。
- AI 不可用（无 Key、网络、解析失败等）时降级：只给结果分，过程分与规范分存 NULL（不伪造），
  score_status = "degraded"，score_json 里写明「AI 点评不可用：原因」。score_total 此时等于结果分。
- 时机：练习结束（通关或放弃）且设备还原之后，由 PracticeManager.end() 调 Scorer.schedule() 异步执行；
  状态写在 attempts.score_status（scoring / done / degraded / failed），全文在 attempts.score_json。
- 考试（里程碑 6）：评分选项 opts 覆盖默认值——weights（三维满分，按比例缩放到合计 100）、fail_discount、
  hint_penalty（每级提示扣分）与 hint_penalty_on（process：从过程分扣，与练习一致；total：从本题总分扣，
  AI 不可用时也照扣）、exam（提交次数、是否超时等背景，交给 AI）。实际使用的选项写进 score_json.options，
  重新评分时沿用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable

import ai
import config
import db
import prompts
import scenarios as sc

log = logging.getLogger("ainet.grading")

# ---- 命令分类 ----

_NAV = re.compile(r"^(system-view|sys|sy|system|interface|int|inter|return|ret|quit|q|qu|qui|ospf|bgp|rip|isis|"
                  r"area|vlan|user-interface|aaa|acl|undo\s+terminal|terminal|screen-width|language-mode)\b")
# 危险或通常多余的命令：(正则, 说明)。命令先转小写、压缩空白。
_DANGEROUS: tuple[tuple[str, str], ...] = (
    (r"^shutdown$", "关闭接口（shutdown）"),
    (r"^reset\s", "reset 类命令（清除进程 / 表项 / 计数）"),
    (r"^reboot\b", "重启设备"),
    (r"^save\b", "保存配置"),
    (r"^undo\s+(ospf|bgp|rip|isis)\b", "删除路由协议进程"),
    (r"^undo\s+(network|area|peer)\b", "删除路由协议的 network / area / peer 配置"),
    (r"^undo\s+interface\b", "删除接口"),
    (r"^undo\s+vlan\b", "删除 VLAN"),
    (r"^undo\s+ip\s+route-static\b", "删除静态路由"),
    (r"^(delete|format|rmdir)\b", "删除文件 / 格式化"),
    (r"^undo\s+info-center\b", "关闭信息中心"),
)
_DOWN_RE = re.compile(r"(DOWN state|\bDown\b|\bDOWN\b|state changed .*Down|->\s*Down)", re.I)


def norm_cmd(cmd: str) -> str:
    return re.sub(r"\s+", " ", (cmd or "").strip().lower())


def cmd_kind(cmd: str) -> str:
    """readonly（查看）/ nav（切换视图）/ config（改配置）。"""
    c = norm_cmd(cmd)
    if not c:
        return "nav"
    if sc.is_readonly(c) or c.startswith(("display ", "dis ", "disp ", "ping ", "tracert ")) or c in ("display", "dis"):
        return "readonly"
    if re.match(r"^(debugging|undo debugging|terminal debugging|terminal monitor|\?)", c):
        return "readonly"
    if _NAV.match(c) and not c.startswith("undo "):
        return "nav"
    return "config"


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


def extract_signals(scenario: dict[str, Any], logs: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性信号（副作用评分与过程评分的线索）。"""
    kinds = [cmd_kind(l.get("command") or "") for l in logs]
    first_cfg = next((i for i, k in enumerate(kinds) if k == "config"), None)
    readonly_before = sum(1 for k in kinds[:first_cfg if first_cfg is not None else len(kinds)] if k == "readonly")
    sol_lines = {norm_cmd(c) for cmds in (scenario.get("solution") or {}).values() for c in cmds}
    dangerous, errors, down_logs = [], [], []
    seen: dict[str, int] = {}
    for i, l in enumerate(logs):
        c = norm_cmd(l.get("command") or "")
        seen[c] = seen.get(c, 0) + 1
        if kinds[i] == "config" and c not in sol_lines:
            for pat, why in _DANGEROUS:
                if re.search(pat, c):
                    dangerous.append({"index": i + 1, "device": l.get("device"), "command": l.get("command"),
                                      "why": why})
                    break
        out = l.get("output") or ""
        if "Error:" in out or re.search(r"^\s*\^\s*$", out, re.M):
            errors.append({"index": i + 1, "device": l.get("device"), "command": l.get("command")})
        # 学员第一条配置命令之前的 Down 日志是故障注入的余波，不算学员的
        if first_cfg is not None and i >= first_cfg:
            for line in out.split("\n"):
                if ai_log_line(line) and not noise_log_line(line) and _DOWN_RE.search(line):
                    down_logs.append({"after_index": i + 1, "device": l.get("device"), "log": line.strip()[:200]})
    repeated = sorted(((c, n) for c, n in seen.items() if n >= 3 and cmd_kind(c) != "nav"), key=lambda x: -x[1])
    config_cmds = [l.get("command") for l, k in zip(logs, kinds) if k == "config"]
    return {
        "commands_total": len(logs),
        "readonly_commands": kinds.count("readonly"),
        "config_commands": len(config_cmds),
        "readonly_before_first_config": readonly_before,
        "first_config_index": None if first_cfg is None else first_cfg + 1,
        "config_without_prior_inspection": first_cfg is not None and readonly_before == 0,
        "config_commands_list": config_cmds[:30],
        "dangerous_or_extra_commands": dangerous[:20],
        "error_commands": errors[:20],
        "repeated_commands": [{"command": c, "times": n} for c, n in repeated[:10]],
        "down_logs_after_student_config": down_logs[:20],
    }


def ai_log_line(line: str) -> bool:
    from console_hub import LOG_RE
    return LOG_RE.match(line.strip()) is not None


def noise_log_line(line: str) -> bool:
    """S5700 的 DATASYNC 配置同步通知：hub 已不再记录，旧记录里若有也不当副作用。"""
    from console_hub import is_noise_log
    return is_noise_log(line)


def reset_info(attempt: dict[str, Any]) -> dict[str, Any] | None:
    """结束时整机复位的结果（practice.end 写入 attempts.reset_json）；没有记录（旧数据）返回 None。"""
    raw = attempt.get("reset_json")
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def reset_brief(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """送给 AI / 存进 score_json 的复位摘要（不含各设备的完整明细）。"""
    if info is None:
        return None
    return {"ok": bool(info.get("ok")), "snapshot": bool(info.get("snapshot", True)),
            "leftovers": info.get("leftovers") or {}, "excluded": info.get("excluded") or {}}


# ---- 分数 ----

def weights(opts: dict[str, Any] | None = None) -> dict[str, float]:
    """三维满分。opts.weights 覆盖默认值，并按比例缩放到合计 100（模板里写 5:3:2 也可以）。"""
    w = {"result": config.SCORE_WEIGHT_RESULT, "process": config.SCORE_WEIGHT_PROCESS,
         "side": config.SCORE_WEIGHT_SIDE}
    ov = (opts or {}).get("weights") or {}
    if ov:
        w = {k: max(0.0, float(ov.get(k, w[k]))) for k in w}
        total = sum(w.values())
        if total > 0:
            w = {k: round(v * 100 / total, 4) for k, v in w.items()}
    return w


def validate_score(data: dict[str, Any]) -> None:
    """AI 评分 JSON 的最低要求：两个维度都要有可解析的得分率。缺了算解析失败（会重试一次）。"""
    for k in ("process", "side"):
        v = data.get(k)
        rate = v.get("rate", v.get("score")) if isinstance(v, dict) else v
        if ai.clamp(rate, 0, 100) is None:
            raise ValueError(f"缺少 {k}.rate")


def _rate(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("rate", v.get("score"))
    return ai.clamp(v, 0, 100)


def _text(v: Any, limit: int = 2000) -> str:
    if isinstance(v, list):
        v = "\n".join(str(x) for x in v)
    return str(v or "").strip()[:limit]


def _str_list(v: Any, limit: int = 8) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n；;]+", v) if x.strip()]
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:300] for x in v if str(x).strip()][:limit]


def _penalty_rate(opts: dict[str, Any] | None) -> float:
    o = opts or {}
    return max(0.0, float(o["hint_penalty"])) if o.get("hint_penalty") is not None else max(0.0, config.SCORE_HINT_PENALTY)


def compute(passed: bool, ai_data: dict[str, Any] | None, hints_used: int = 0,
            opts: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 spec §9 计算三维分数。ai_data 为 None（降级）时过程分 / 规范分为 None，总分 = 结果分。

    opts（考试模板）：weights / fail_discount / hint_penalty / hint_penalty_on，见模块说明。
    """
    o = opts or {}
    w = weights(o)
    fail_discount = float(o["fail_discount"]) if o.get("fail_discount") is not None else config.SCORE_FAIL_DISCOUNT
    on_total = o.get("hint_penalty_on") == "total"
    result = w["result"] if passed else 0.0
    dims: dict[str, Any] = {"result": {"score": round(result, 1), "max": w["result"],
                                       "reason": "场景判定全部通过" if passed else "场景判定未全部通过（未修复）"}}
    if ai_data is None:
        dims["process"] = {"score": None, "max": w["process"], "reason": ""}
        dims["side"] = {"score": None, "max": w["side"], "reason": ""}
        total, penalty = result, 0.0
        if on_total and hints_used:
            penalty = min(total, _penalty_rate(o) * hints_used)
            total -= penalty
            if penalty:
                dims["result"]["adjust"] = f"使用 {hints_used} 级提示，本题总分扣 {round(penalty, 1)}"
        return {"total": round(total, 1), "result": round(result, 1), "process": None, "side": None,
                "dimensions": dims, "discount": None, "hint_penalty": round(penalty, 1)}
    p_rate = _rate(ai_data.get("process"))
    s_rate = _rate(ai_data.get("side"))
    p_rate = 0.0 if p_rate is None else p_rate
    s_rate = 0.0 if s_rate is None else s_rate
    process_raw = w["process"] * p_rate / 100
    penalty = 0.0 if on_total else min(process_raw, _penalty_rate(o) * max(0, hints_used))
    discount = 1.0 if passed else fail_discount
    process = max(0.0, (process_raw - penalty) * discount)
    side = w["side"] * s_rate / 100
    pr = ai_data.get("process") if isinstance(ai_data.get("process"), dict) else {}
    sr = ai_data.get("side") if isinstance(ai_data.get("side"), dict) else {}
    dims["process"] = {"score": round(process, 1), "max": w["process"], "rate": round(p_rate),
                       "raw": round(process_raw, 1), "reason": _text(pr.get("reason"), 800)}
    dims["side"] = {"score": round(side, 1), "max": w["side"], "rate": round(s_rate),
                    "reason": _text(sr.get("reason"), 800)}
    notes = []
    if penalty:
        notes.append(f"使用 {hints_used} 级提示，过程分扣 {round(penalty, 1)}")
    if not passed:
        notes.append(f"结果未达标，过程分 ×{discount:g}")
    if notes:
        dims["process"]["adjust"] = "；".join(notes)
    total = result + process + side
    if on_total and hints_used:
        penalty = min(total, _penalty_rate(o) * hints_used)
        total -= penalty
        if penalty:
            dims["result"]["adjust"] = f"使用 {hints_used} 级提示，本题总分扣 {round(penalty, 1)}"
    return {"total": round(total, 1), "result": round(result, 1), "process": round(process, 1),
            "side": round(side, 1), "dimensions": dims, "discount": discount, "hint_penalty": round(penalty, 1)}


_PATH_KEYS = ("standard_path", "standardPath", "standard_approach", "standard_steps", "reference_explanation",
              "标准思路", "标准排障思路")


def standard_path(ai_data: dict[str, Any]) -> str:
    """标准思路讲解：模型偶尔把它放进 comment 里或换个键名，都接受。"""
    for src in (ai_data, ai_data.get("comment") if isinstance(ai_data.get("comment"), dict) else {}):
        for k in _PATH_KEYS:
            v = src.get(k)
            if v:
                return _text(v, 3000)
    return ""


def build_comment(ai_data: dict[str, Any]) -> dict[str, Any]:
    c = ai_data.get("comment") if isinstance(ai_data.get("comment"), dict) else {}
    return {"summary": _text(c.get("summary"), 400), "good": _str_list(c.get("good")),
            "problems": _str_list(c.get("problems")), "suggestions": _str_list(c.get("suggestions"))}


# ---- 评分任务 ----

def _prepare(attempt: dict[str, Any], scenario: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    logs = db.attempt_logs(attempt["id"])
    for l in logs:
        l["t"] = _ts(l.get("ts"))
    signals = extract_signals(scenario, logs)
    text = prompts.format_logs(logs, config.SCORE_PROMPT_MAX_COMMANDS, config.SCORE_PROMPT_OUTPUT_CHARS,
                               _ts(attempt.get("started_at")))
    return logs, signals, text


def _duration(attempt: dict[str, Any]) -> int | None:
    a, b = _ts(attempt.get("started_at")), _ts(attempt.get("ended_at"))
    return int(b - a) if a is not None and b is not None else None


def expand_for_attempt(scenario: dict[str, Any], att: dict[str, Any]) -> dict[str, Any]:
    """参数化场景按该次练习记录的种子展开（库里镜像的是模板）；已展开或无参数的原样返回。"""
    if scenario.get("params"):
        return sc.expand(scenario, att.get("seed") or 0)
    return scenario


async def score_attempt(attempt_id: int, scenario: dict[str, Any] | None = None,
                        client_factory: Callable[[], Any] | None = None,
                        opts: dict[str, Any] | None = None) -> dict[str, Any]:
    """评一次练习，写库并返回 score_json。AI 失败时降级为只给结果分。

    opts 为 None 时沿用上一次评分记录里的选项（考试题重新评分仍按模板权重）。
    """
    att = await asyncio.to_thread(db.get_attempt, attempt_id)
    if att is None:
        raise ValueError(f"练习记录不存在：{attempt_id}")
    if scenario is None:
        scenario = await asyncio.to_thread(db.scenario_by_db_id, att.get("scenario_id"))
    if scenario is None:
        raise ValueError("找不到该练习的场景")
    scenario = expand_for_attempt(scenario, att)
    if opts is None:
        try:
            opts = (json.loads(att["score_json"]) if att.get("score_json") else {}).get("options")
        except (ValueError, AttributeError):
            opts = None
    passed = att.get("passed") == 1
    hints_used = int(att.get("hints_used") or 0)
    logs, signals, logs_text = await asyncio.to_thread(_prepare, att, scenario)
    # Agent 面板（spec §16）：导师对话摘要作为过程分参考；判定泄露的回复已在练习中把 hints_used 记到 3 级
    dialog = await asyncio.to_thread(db.agent_messages, attempt_id=attempt_id)
    agent_text = prompts.format_agent_dialog(dialog)
    reset = reset_brief(reset_info(att))
    o = opts or {}
    base = {"passed": passed, "weights": weights(o),
            "fail_discount": o["fail_discount"] if o.get("fail_discount") is not None else config.SCORE_FAIL_DISCOUNT,
            "options": opts,
            "hints_used": hints_used, "commands": len(logs), "signals": signals, "reset": reset,
            "agent": {"questions": sum(1 for m in dialog if m.get("role") == "user"),
                      "penalties": int(att.get("agent_penalties") or 0)},
            "reference_path": scenario.get("reference_path", ""), "scored_at": ai.now_iso()}
    ai_data: dict[str, Any] | None = None
    reason = ""
    meta: dict[str, Any] = {}
    if not config.AI_SCORING_ENABLED:
        reason = ai.KIND_TEXT["disabled"]
    else:
        try:
            client = (client_factory or ai.get_client)()
            user = prompts.build_score_user(scenario, passed=passed, duration_s=_duration(att),
                                            hints_used=hints_used, logs_text=logs_text, signals=signals,
                                            reset=reset, exam=o.get("exam"), agent_text=agent_text)
            res = await client.chat_json(prompts.SCORE_SYSTEM, user, thinking=config.AI_SCORE_THINKING,
                                         max_tokens=config.AI_SCORE_MAX_TOKENS, timeout=config.AI_SCORE_TIMEOUT,
                                         validate=validate_score)
            ai_data = res.data
            meta = {"model": res.model, "thinking": res.thinking, "usage": res.usage, "attempts": res.attempts,
                    "elapsed_s": res.elapsed}
        except ai.AIError as exc:
            reason = exc.message
            log.warning("AI 评分不可用（attempt %s）：%s", attempt_id, reason)
        except Exception as exc:   # 不让意外错误把 key 之类带出去
            reason = ai.redact(f"{type(exc).__name__}: {exc}")[:300]
            log.exception("AI 评分异常（attempt %s）", attempt_id)
    scores = compute(passed, ai_data, hints_used, o)
    if ai_data is not None:
        meta["raw_keys"] = sorted(map(str, ai_data.keys()))[:20]      # 诊断用：模型实际返回的字段
        out = {**base, "status": "done", "ai": {"available": True, **meta}, "scores": scores,
               "comment": build_comment(ai_data), "standard_path": standard_path(ai_data)}
    else:
        out = {**base, "status": "degraded", "ai": {"available": False, "reason": reason},
               "reason": f"AI 点评不可用：{reason}", "scores": scores, "comment": None, "standard_path": ""}
    await asyncio.to_thread(db.update_attempt, attempt_id, score_status=out["status"],
                            score_json=json.dumps(out, ensure_ascii=False), score_total=scores["total"],
                            score_result=scores["result"], score_process=scores["process"],
                            score_side=scores["side"])
    return out


class Scorer:
    """后台评分任务管理：schedule() 立即返回，任务引用保存在 self.tasks 里（防止被 GC）。"""

    def __init__(self, scenario_lookup: Callable[[int], dict[str, Any] | None] | None = None) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.scenario_lookup = scenario_lookup

    def busy(self, attempt_id: int) -> bool:
        t = self.tasks.get(attempt_id)
        return t is not None and not t.done()

    async def schedule(self, attempt_id: int, scenario: dict[str, Any] | None = None,
                       opts: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.busy(attempt_id):
            return {"status": "scoring"}
        if opts is None:     # 重新评分：沿用上次的评分选项（score_json 马上会被「评分中」覆盖）
            att = await asyncio.to_thread(db.get_attempt, attempt_id)
            try:
                opts = (json.loads(att["score_json"]) if att and att.get("score_json") else {}).get("options")
            except (ValueError, AttributeError):
                opts = None
        await asyncio.to_thread(db.update_attempt, attempt_id, score_status="scoring",
                                score_json=json.dumps({"status": "scoring", "options": opts}, ensure_ascii=False))
        self.tasks[attempt_id] = asyncio.create_task(self._run(attempt_id, scenario, opts))
        return {"status": "scoring"}

    async def _run(self, attempt_id: int, scenario: dict[str, Any] | None, opts: dict[str, Any] | None = None) -> None:
        try:
            await score_attempt(attempt_id, scenario, opts=opts)
        except Exception as exc:
            msg = ai.redact(f"{type(exc).__name__}: {exc}")[:300]
            log.exception("评分失败（attempt %s）", attempt_id)
            try:
                await asyncio.to_thread(db.update_attempt, attempt_id, score_status="failed",
                                        score_json=json.dumps({"status": "failed", "reason": f"评分失败：{msg}",
                                                               "options": opts}, ensure_ascii=False))
            except Exception:
                log.exception("写评分失败状态出错")
        finally:
            t = self.tasks.get(attempt_id)
            if t is asyncio.current_task():
                self.tasks.pop(attempt_id, None)

    async def wait(self, attempt_id: int) -> None:
        t = self.tasks.get(attempt_id)
        if t is not None:
            await asyncio.shield(t)

    async def close(self) -> None:
        for t in list(self.tasks.values()):
            t.cancel()


def result_view(att: dict[str, Any]) -> dict[str, Any]:
    """GET /api/practice/result 的返回：状态 + 分数 + 点评（score_json 全文）。"""
    status = att.get("score_status") or "none"
    try:
        detail = json.loads(att["score_json"]) if att.get("score_json") else {}
    except ValueError:
        detail = {}
    return {"attempt_id": att["id"], "status": status, "passed": att.get("passed") == 1,
            "ended": att.get("ended_at") is not None,
            "score_total": att.get("score_total"), "score_result": att.get("score_result"),
            "score_process": att.get("score_process"), "score_side": att.get("score_side"),
            "detail": detail}
