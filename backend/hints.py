"""AI 分级提示（spec §7）：① 方向 → ② 命令建议 → ③ 直接揭示。

- 内容结合学员到目前为止的命令和输出动态生成；关闭思考、总时长上限 AI_HINT_TIMEOUT（缺省 15 秒）。
- 第 1、2 级的泄露防护：提示词里列出禁止出现的内容；生成结果再用规则检查，出现 solution / inject 里的
  配置命令、或只在故障配置里出现的 IP 地址时，丢弃 AI 结果、回退到静态提示。
  checks 的判定命令不在禁止之列：它们按场景校验规则都是只读的查看命令（display / ping / tracert），
  说出来不泄露答案；而且常常就是最自然的排查命令（如 ACL 题的 display acl all），禁掉反而教不好。
  比较前统一转小写、压缩空白；VRP 缩写写法（如 dis ip int br）匹配不到，是已知局限。
- AI 不可用（无 Key、超时、解析失败、被泄露检查拦下）时回退到场景 JSON 里的静态 hints，并标注来源。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

import ai
import config
import prompts

log = logging.getLogger("ainet.hints")

_NAV_LINE = re.compile(r"^(system-view|sys|interface\s+\S+|int\s+\S+|return|quit|q)$", re.I)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def banned_terms(scenario: dict[str, Any]) -> list[str]:
    """第 1、2 级不能出现的内容（原文形式，给提示词用；检查时再归一化）。"""
    out: list[str] = []

    def add(x: str) -> None:
        x = x.strip()
        if x and _norm(x) not in {_norm(y) for y in out}:
            out.append(x)
    for field in ("solution", "inject"):
        for cmds in (scenario.get(field) or {}).values():
            for c in cmds:
                if not _NAV_LINE.match(c.strip()):
                    add(c)
    # 只在故障 / 修复配置里出现、工单里没有的 IP（例如注入的错误地址）
    symptom_ips = set(_IP_RE.findall(scenario.get("symptom", "")))
    for field in ("inject", "solution"):
        for cmds in (scenario.get(field) or {}).values():
            for c in cmds:
                for ip in _IP_RE.findall(c):
                    if ip not in symptom_ips:
                        add(ip)
    return out


def leaks(text: str, banned: list[str]) -> list[str]:
    """返回 text 中出现的禁止内容。IP 按完整地址匹配（10.0.12.2 不会命中 10.0.12.20）。"""
    t = _norm(text)
    hit = []
    for b in banned:
        nb = _norm(b)
        if not nb:
            continue
        if _IP_RE.fullmatch(nb):
            if re.search(rf"(?<![\d.]){re.escape(nb)}(?![\d.]*\d)", t):
                hit.append(b)
        elif nb in t:
            hit.append(b)
    return hit


def static_hint(scenario: dict[str, Any], level: int) -> str:
    return scenario["hints"][level - 1]


async def generate(scenario: dict[str, Any], level: int, logs: list[dict[str, Any]],
                   started_ts: float | None = None,
                   client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """返回 {text, source: "ai" | "static", reason?}。不抛异常。"""
    if not config.AI_HINTS_ENABLED:
        return {"text": static_hint(scenario, level), "source": "static", "reason": ai.KIND_TEXT["disabled"]}
    banned = banned_terms(scenario) if level < 3 else []
    logs_text = prompts.format_logs(logs, 40, 600, started_ts)
    user = prompts.build_hint_user(scenario, level, logs_text, banned)

    def validate(data: dict[str, Any]) -> None:
        if not isinstance(data.get("hint"), str) or not data["hint"].strip():
            raise ValueError("缺少 hint")
    reason = ""
    try:
        client = (client_factory or ai.get_client)()
        res = await asyncio.wait_for(
            client.chat_json(prompts.HINT_SYSTEM, user, thinking=config.AI_HINT_THINKING,
                             max_tokens=config.AI_HINT_MAX_TOKENS, timeout=config.AI_HINT_TIMEOUT,
                             deadline=config.AI_HINT_TIMEOUT, validate=validate),
            config.AI_HINT_TIMEOUT + 2)
        text = res.data["hint"].strip()[:600]
        hit = leaks(text, banned)
        if not hit:
            return {"text": text, "source": "ai", "usage": res.usage, "elapsed_s": res.elapsed}
        reason = "AI 提示包含不该出现的内容，已改用静态提示"
        log.warning("第 %s 级 AI 提示泄露（%s），回退静态提示", level, "、".join(hit))
    except asyncio.TimeoutError:
        reason = ai.KIND_TEXT["timeout"]
    except ai.AIError as exc:
        reason = exc.message
    except Exception as exc:
        reason = ai.redact(f"{type(exc).__name__}: {exc}")[:200]
        log.exception("生成 AI 提示出错")
    if reason:
        log.info("第 %s 级提示回退静态：%s", level, reason)
    return {"text": static_hint(scenario, level), "source": "static", "reason": reason}
