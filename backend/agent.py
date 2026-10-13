"""Agent 面板（spec §16）：工作台右侧的 AI 对话，流式回答，可在设备上执行只读命令。

- 场景：自由实验（lab，不限制）/ 练习（practice，导师模式：只引导不给答案，回复过泄露检查）/
  考试（exam：不提供，接口返回 403，由 main.py 在开始流式输出之前判断）。场景只由服务端按当前状态判定，
  不信任前端传来的任何模式字段。
- 环境快照（每次请求在服务端现组装）：实验后端类型与平台、当前拓扑（设备 / 型号 / 状态 / 连线）、
  学员最近的终端命令与输出摘要（TermLog 监听 ConsoleHub 的 command / output 事件，截断）、
  练习中的工单（只用 scenarios.public() 的面向学员字段，再过一遍 FORBIDDEN_KEYS 兜底；不含种子、
  参数取值、提示内容、判定命令等）。
- 只读命令工具 run_readonly(device, command)：check_command() 严格白名单（display / ping，长度、字符集、
  管道过滤词、ping 次数），走 ConsoleHub.run_hidden(kind="check") 隐藏事务——学员终端看不到，也不产生
  command 事件（不进学员命令采集）；学员正在输入半行时 hub 返回 busy，照实告诉模型和学员，不排队重试。
  每轮提问最多执行 AGENT_MAX_TOOL_CALLS（3）条；练习中只能查本题开放的设备。
- 泄露检查（练习）：最终回复用 hints.leaks(text, hints.banned_terms(scenario))（与第 1、2 级提示同一套），
  命中则按第 3 级提示记一次扣分（hints_used 至少记到 3，attempts.agent_penalties + 1），并在对话里告知学员。
- 对话落库（agent_messages，练习中关联 attempt_id），评分时作为过程分参考（grading / prompts.format_agent_dialog）。
- AI 不可用 / 超时：以 SSE error 事件给出友好提示，不抛异常、不卡住前端。

SSE 事件（data: 一行 JSON）：
  {"type": "meta", "mode", "devices": [...]}       开始，告知服务端判定的场景
  {"type": "delta", "text"}                         回复文字增量
  {"type": "tool", "device", "command", "status", "output", "reason"}   执行了一条只读命令（status: ok/busy/error/rejected）
  {"type": "penalty", "message", "hints_used"}      导师回复判定含答案级内容，已记扣分
  {"type": "error", "message"}                      AI 不可用等（前端显示友好提示）
  {"type": "done", "message_id"}                    结束
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any, AsyncIterator, Callable

import ai
import config
import db
import hints
import prompts
import scenarios as sc
from lab import console_of

log = logging.getLogger("ainet.agent")

# 发给模型的工单里绝不能出现的字段（答案保护）。scenarios.public() 本来就不含它们，这里再兜一层。
FORBIDDEN_KEYS = frozenset({
    "inject", "restore", "checks", "answer", "root_cause", "expected_fix", "solution", "baseline",
    "hints", "reference_path", "scoring_points", "params", "param_values", "seed",
})

MAX_HISTORY = 12             # 请求体里带的历史条数上限（只取最近的）
MAX_HISTORY_CHARS = 12000
MAX_MESSAGE_CHARS = 2000
TERM_RECENT = 15             # 快照里最近的终端命令条数
TERM_OUTPUT_CHARS = 600
TERM_TOTAL_CHARS = 5000
TOOL_OUTPUT_TO_MODEL = 3000
TOOL_OUTPUT_TO_UI = 2000
MAX_ROUNDS = 5

STATUS_TEXT = {"online": "运行中", "stopped": "已停止", "na": "无 console", "starting": "启动中",
               "stopping": "停止中", "error": "异常", "unknown": "状态未知"}


# ---- 只读命令白名单 ----

CMD_MAX_LEN = 120
PING_MAX_COUNT = 5
PING_DEFAULT_COUNT = 3
_CMD_CHARS = re.compile(r"^[A-Za-z0-9 ._:/\-|]+$")
_FILTER_WORDS = ("include", "exclude", "begin")
# display 里也有不适合后台执行的：输出极长 / 执行很久
_DISPLAY_BLOCK = re.compile(r"^(diag|diagnostic-information)")


def check_command(raw: Any) -> tuple[str | None, str]:
    """校验并规范化只读命令。返回 (规范化后的命令, "") 或 (None, 拒绝原因)。

    只允许完整的 display ... / ping ...（不认缩写）；ASCII 可见字符、长度上限；不允许 ; 换行等控制字符、
    ? 与 Tab（会让设备停在半行上）、全角符号；管道只允许 display 后接 | include / exclude / begin。
    ping 没写 -c 时补 -c 3，-c 不能超过 5。
    """
    if not isinstance(raw, str):
        return None, "命令必须是字符串"
    if len(raw) > CMD_MAX_LEN:
        return None, f"命令过长（上限 {CMD_MAX_LEN} 字符）"
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None, "命令里不能有换行、Tab 等控制字符"
    if not raw.strip():
        return None, "命令为空"
    if not _CMD_CHARS.match(raw):        # 先查原文：全角字符、不间断空格等不会被 split() 悄悄吞掉
        return None, "命令里有不允许的字符（只允许字母、数字、空格和 . _ : / - |，不能有 ; ? 等）"
    cmd = " ".join(raw.split(" "))
    cmd = " ".join(cmd.split())
    parts = cmd.split("|")
    if len(parts) > 2:
        return None, "只允许一个管道过滤"
    words = parts[0].split()
    if not words:
        return None, "命令为空"
    verb = words[0].lower()
    if verb not in ("display", "ping"):
        return None, "只允许执行 display 或 ping 开头的完整命令"
    words[0] = verb
    if verb == "display":
        if len(words) < 2:
            return None, "display 后面要跟查看对象"
        if _DISPLAY_BLOCK.match(words[1].lower()):
            return None, "这条 display 命令输出过长，不在后台执行"
    if len(parts) == 2:
        filt = parts[1].split()
        if verb != "display" or len(filt) < 2 or filt[0].lower() not in _FILTER_WORDS:
            return None, "管道只允许 display ... | include / exclude / begin 关键字"
        tail = " | " + " ".join([filt[0].lower(), *filt[1:]])
    else:
        tail = ""
    if verb == "ping":
        args = words[1:]
        if not args or args[-1].startswith("-"):
            return None, "ping 缺少目标地址"
        if "-c" in args:
            i = args.index("-c")
            if i + 1 >= len(args) - 1 or not args[i + 1].isdigit():
                return None, "ping -c 后面要跟次数"
            if not 1 <= int(args[i + 1]) <= PING_MAX_COUNT:
                return None, f"ping 次数不能超过 {PING_MAX_COUNT}"
        else:
            args = ["-c", str(PING_DEFAULT_COUNT), *args]
        if args.count("-c") > 1:
            return None, "ping -c 只能出现一次"
        words = ["ping", *args]
    return " ".join(words) + tail, ""


# ---- 学员最近的终端命令（ConsoleHub 监听者） ----

class TermLog:
    """记录学员经终端提交的命令与输出摘要（hub 的 command / output 事件）。隐藏事务不产生这些事件，
    所以 Agent 自己执行的只读命令、判定 / 注入命令都不会出现在这里。"""

    def __init__(self, maxlen: int = 80) -> None:
        self.items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._last: dict[int, dict[str, Any]] = {}

    def on_event(self, event: str, port: int, data: str) -> None:
        if event == "command":
            it = {"port": port, "command": data, "output": "", "t": time.time()}
            self.items.append(it)
            self._last[port] = it
        elif event == "output":
            it = self._last.get(port)
            if it is not None and not it["output"]:
                it["output"] = data or ""

    def recent(self, ports: set[int] | None = None, limit: int = TERM_RECENT) -> list[dict[str, Any]]:
        rows = [it for it in self.items if ports is None or it["port"] in ports]
        return [dict(it) for it in rows[-limit:]]

    def clear(self) -> None:
        self.items.clear()
        self._last.clear()


# ---- 环境快照 ----

def ticket_view(scenario: dict[str, Any], active: Any = None) -> dict[str, Any]:
    """练习工单：只取面向学员的字段（scenarios.public），再去掉 FORBIDDEN_KEYS。"""
    pub = {k: v for k, v in sc.public(scenario).items() if k not in FORBIDDEN_KEYS}
    out = {k: pub[k] for k in ("title", "symptom", "accessible_devices", "knowledge_tags", "level") if k in pub}
    if active is not None:
        lc = active.last_check or {}
        out["progress"] = {
            "passed": active.passed_at is not None,
            "hints_used": active.hints_used,
            "last_check": ({"passed": lc.get("passed"), "total": lc.get("total")} if lc.get("status") == "ok"
                           else None),
        }
    return out


def _abbr(name: str) -> str:
    return (name or "").replace("GigabitEthernet", "GE").replace("Ethernet", "Eth")


def build_snapshot(cur: dict[str, Any], lab_info: dict[str, Any], mode: str, allowed: set[int],
                   termlog: TermLog | None, ticket: dict[str, Any] | None) -> dict[str, Any]:
    """组装环境快照（纯函数，便于测试）。allowed：Agent 可执行只读命令的 console 键。"""
    topo = cur.get("topology") or {}
    status = cur.get("devices") or {}
    devices = []
    by_id: dict[str, str] = {}
    port_name: dict[int, str] = {}
    for d in topo.get("devices") or []:
        k = console_of(d)
        st = status.get(d.get("id"), "stopped") if k > 0 else "na"
        by_id[d.get("id")] = d.get("name")
        if k > 0:
            port_name[k] = d.get("name")
        devices.append({"name": d.get("name"), "model": d.get("model"), "kind": d.get("kind"),
                        "status": st, "console": k,
                        "readonly_ok": k in allowed and st == "online"})
    links = []
    for l in topo.get("links") or []:
        a, b = by_id.get(l.get("src_id")), by_id.get(l.get("dst_id"))
        if a and b:
            links.append(f"{a} {_abbr(l.get('src_label', ''))} — {b} {_abbr(l.get('dst_label', ''))}")
    term: list[dict[str, Any]] = []
    if termlog is not None:
        total = 0
        for it in reversed(termlog.recent(set(port_name), TERM_RECENT)):
            out = (it.get("output") or "").strip()
            if len(out) > TERM_OUTPUT_CHARS:
                out = out[:TERM_OUTPUT_CHARS] + f"……（截断，原长 {len(out)} 字符）"
            total += len(out) + len(it["command"])
            if total > TERM_TOTAL_CHARS and term:
                break
            term.append({"device": port_name.get(it["port"], str(it["port"])), "command": it["command"],
                         "output": out})
        term.reverse()
    return {
        "mode": mode,
        "lab": {"kind": lab_info.get("kind"), "label": lab_info.get("label"), "platform": lab_info.get("platform")},
        "topology": {"name": topo.get("name"), "state": cur.get("state"), "running": cur.get("running"),
                     "total": cur.get("total"), "devices": devices, "links": links} if topo else None,
        "terminal": term,
        "ticket": ticket,
    }


def snapshot_text(snap: dict[str, Any]) -> str:
    """快照 -> 发给模型的文字。"""
    lab = snap["lab"]
    plat = {"vrp5": "华为 VRP5", "yunshan": "华为 YunShan OS"}.get(lab.get("platform") or "", lab.get("platform") or "-")
    lines = ["## 环境快照（系统在本次提问时自动生成）",
             f"场景：{ {'lab': '自由实验', 'practice': '练习（导师模式）'}.get(snap['mode'], snap['mode']) }",
             f"实验后端：{lab.get('label') or lab.get('kind')}；设备平台：{plat}"]
    t = snap.get("topology")
    if not t:
        lines.append("当前拓扑：未检测到（模拟器里可能没有运行中的拓扑）")
    else:
        lines.append(f"当前拓扑：{t.get('name') or '-'}（运行 {t.get('running', '-')}/{t.get('total', '-')} 台）")
        lines.append("设备：")
        for d in t["devices"]:
            note = "可执行只读命令" if d["readonly_ok"] else (
                "本题不开放" if snap["mode"] == "practice" and d["status"] == "online" else "不可执行命令")
            lines.append(f"- {d['name']}（{d.get('model') or '-'}，{STATUS_TEXT.get(d['status'], d['status'])}，{note}）")
        lines.append("连线：" + ("" if t["links"] else "无"))
        lines += [f"- {x}" for x in t["links"]]
    tk = snap.get("ticket")
    if tk:
        lines += ["", "## 当前练习工单（学员看到的内容）", f"标题：{tk.get('title')}", f"症状：{tk.get('symptom')}",
                  f"本题可登录的设备：{'、'.join(tk.get('accessible_devices') or [])}"]
        pg = tk.get("progress") or {}
        lc = pg.get("last_check")
        lines.append(f"进度：{'已通关' if pg.get('passed') else '未通关'}；已用提示 {pg.get('hints_used', 0)} 级"
                     + (f"；最近一次判定通过 {lc['passed']}/{lc['total']} 项" if lc else ""))
    lines += ["", "## 学员最近在终端里执行的命令（缩进行是输出摘要，按时间顺序）"]
    if not snap["terminal"]:
        lines.append("（暂无）")
    for it in snap["terminal"]:
        lines.append(f"{it['device']}> {it['command']}")
        if it["output"]:
            lines += ["    " + x for x in it["output"].split("\n")]
    return "\n".join(lines)


def clean_history(history: Any) -> list[dict[str, str]]:
    """请求体里的历史：只接受 user / assistant 的纯文本，限条数与总长度（从最近的往前取）。"""
    out: list[dict[str, str]] = []
    if not isinstance(history, list):
        return out
    total = 0
    for m in reversed(history[-MAX_HISTORY:]):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        text = m.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        text = text[:MAX_MESSAGE_CHARS * 2]
        total += len(text)
        if total > MAX_HISTORY_CHARS:
            break
        out.append({"role": m["role"], "content": text})
    out.reverse()
    return out


def sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


class AgentForbidden(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class AgentService:
    """Agent 对话。hub：ConsoleHub；pm：PracticeManager；exam：ExamManager；provider：() -> LabProvider。"""

    def __init__(self, hub: Any, pm: Any, exam: Any, provider: Callable[[], Any],
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.hub = hub
        self.pm = pm
        self.exam = exam
        self.provider = provider
        self.client_factory = client_factory
        self.termlog = TermLog()
        self.env_gen = 0                         # 模拟环境切换代数（labenv.LabManager 切换时 +1）
        self.env_desc = ""
        self.labmgr: Any = None                  # labenv.LabManager（main 注入）：切换中拒绝提问
        self._locks: dict[int, asyncio.Lock] = {}
        self._bg: set[asyncio.Task] = set()     # 后台任务的引用（防止被 GC）
        hub.add_listener(self.termlog.on_event)

    def on_env_switch(self, desc: str) -> None:
        """模拟环境切换了（spec §5.2）：旧环境的终端命令记录作废；进行中的对话之后不再执行命令，结束时提示。"""
        self.env_gen += 1
        self.env_desc = desc
        self.termlog.clear()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    # ---- 场景 ----

    def exam_active(self) -> bool:
        a = self.pm.active
        return self.exam.exam is not None or (a is not None and a.mode == "exam")

    def mode(self) -> str:
        if self.exam_active():
            return "exam"
        a = self.pm.active
        return "practice" if a is not None and a.mode == "practice" else "lab"

    def precheck(self, student_id: int) -> None:
        """开始流式输出之前的检查（之后就改不了 HTTP 状态码了）。"""
        if self.exam_active():
            raise AgentForbidden(403, "考试进行中，不能使用 Agent")
        if not config.AI_AGENT_ENABLED:
            raise AgentForbidden(403, "Agent 功能已关闭")
        if getattr(self.labmgr, "switching", False):
            raise AgentForbidden(409, "模拟环境切换中，请稍后再问")
        lock = self._locks.get(student_id)
        if lock is not None and lock.locked():
            raise AgentForbidden(429, "上一个问题还在回答中，请稍候")

    async def _context(self) -> tuple[str, dict[str, Any], dict[str, int], set[int], Any]:
        """返回 (mode, 快照, 设备名(小写)->console 键, 可执行键集合, 练习 Active 或 None)。"""
        provider = self.provider()
        cur = await provider.resolve()
        mode = self.mode()
        a = self.pm.active if mode == "practice" else None
        try:
            allowed = set(provider.allowed(await provider.current()))
        except Exception:
            allowed = set()
        allowed = self.pm.allowed_filter(allowed)
        ticket = ticket_view(a.scenario, a) if a is not None else None
        snap = build_snapshot(cur, provider.info(), mode, allowed, self.termlog, ticket)
        names: dict[str, int] = {}
        for d in (snap["topology"] or {}).get("devices", []):
            if d["readonly_ok"]:
                names[str(d["name"]).lower()] = d["console"]
        return mode, snap, names, allowed, a

    # ---- 工具 ----

    async def run_tool(self, device: Any, command: Any, names: dict[str, int], gen: int | None = None) -> dict[str, Any]:
        """执行一条只读命令。返回 {device, command, status, output, reason}。不抛异常。
        gen：对话开始时的环境切换代数；之后环境切换过就不再执行（设备名 / console 键属于旧环境）。"""
        dev = str(device or "").strip()
        res: dict[str, Any] = {"device": dev, "command": str(command or "")[:CMD_MAX_LEN], "status": "rejected",
                               "output": "", "reason": ""}
        cmd, why = check_command(command)
        if cmd is None:
            res["reason"] = why
            return res
        res["command"] = cmd
        port = names.get(dev.lower())
        if port is None:
            res["reason"] = f"设备 {dev or '（未指定）'} 不存在、未运行或当前不开放"
            return res
        if self.exam_active():          # 流式过程中考试开始了
            res["reason"] = "考试进行中，不能执行命令"
            return res
        if gen is not None and gen != self.env_gen:
            res["reason"] = f"模拟环境已切换为 {self.env_desc}，不再在旧环境的设备上执行命令"
            return res
        if self.labmgr is not None and hasattr(self.labmgr, "touch"):
            self.labmgr.touch("agent")      # Agent 执行命令算设备活动（空闲停机重新计时，spec §10.5）
        try:
            # 独立任务 + shield：对话流被取消（学员点「停止」、断开连接，Starlette 会反复取消当前任务）时，
            # 隐藏事务仍然完整做完并恢复转发，不会把 console 通道留在隐藏状态
            task = self._spawn(self.hub.run_hidden(port, [cmd], kind="check", cmd_timeout=config.AGENT_TOOL_TIMEOUT))
            r = await asyncio.shield(task)
        except Exception as exc:        # 不让工具异常打断整轮对话
            log.exception("Agent 只读命令执行异常")
            res.update(status="error", reason=f"执行出错：{type(exc).__name__}")
            return res
        if r.status == "ok":
            res.update(status="ok", output=(r.outputs[0] if r.outputs else ""))
        elif r.status == "busy":
            res.update(status="busy", reason=f"设备暂不能执行（{r.reason}），未执行")
        else:
            res.update(status="error", reason=r.reason or "执行失败")
        return res

    # ---- 对话 ----

    async def chat(self, student_id: int, message: str, history: Any) -> AsyncIterator[str]:
        """一次提问：产出 SSE 文本块。调用前先 precheck()。"""
        lock = self._locks.setdefault(student_id, asyncio.Lock())
        async with lock:
            async for chunk in self._chat(student_id, message, history):
                yield chunk

    async def _chat(self, student_id: int, message: str, history: Any) -> AsyncIterator[str]:
        message = (message or "").strip()[:MAX_MESSAGE_CHARS]
        gen0 = self.env_gen
        try:
            mode, snap, names, allowed, a = await self._context()
        except Exception as exc:
            log.exception("组装 Agent 环境快照失败")
            yield sse({"type": "error", "message": f"读取当前环境失败：{type(exc).__name__}"})
            yield sse({"type": "done", "message_id": None})
            return
        if mode == "exam":
            yield sse({"type": "error", "message": "考试进行中，不能使用 Agent"})
            yield sse({"type": "done", "message_id": None})
            return
        attempt_id = a.attempt_id if a is not None else None
        await asyncio.to_thread(db.add_agent_message, student_id, attempt_id, mode, "user", message)
        yield sse({"type": "meta", "mode": mode, "attempt_id": attempt_id,
                   "devices": sorted(d["name"] for d in (snap["topology"] or {}).get("devices", []) if d["readonly_ok"])})
        system = prompts.AGENT_SYSTEM_MENTOR if mode == "practice" else prompts.AGENT_SYSTEM_LAB
        messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                          {"role": "system", "content": snapshot_text(snap)},
                                          *clean_history(history), {"role": "user", "content": message}]
        text_parts: list[str] = []
        tools_done: list[dict[str, Any]] = []
        error = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.AI_AGENT_DEADLINE
        try:
            client = (self.client_factory or ai.get_client)()
            for _ in range(MAX_ROUNDS):
                allow_tools = len(tools_done) < config.AGENT_MAX_TOOL_CALLS
                end: dict[str, Any] = {}
                round_text = ""
                text_parts.append("")
                remaining = deadline - loop.time()
                if remaining <= 1:
                    raise ai.AIError("timeout", "整轮回答超过时长上限")
                gen = client.chat_stream(messages, tools=prompts.AGENT_TOOLS if allow_tools else None,
                                         max_tokens=config.AI_AGENT_MAX_TOKENS,
                                         timeout=min(config.AI_AGENT_TIMEOUT, remaining))
                async for ev in gen:
                    if ev.get("type") == "delta" and ev.get("text"):
                        round_text += ev["text"]
                        text_parts[-1] = round_text
                        yield sse({"type": "delta", "text": ev["text"]})
                    elif ev.get("type") == "end":
                        end = ev
                    if loop.time() > deadline:
                        raise ai.AIError("timeout", "整轮回答超过时长上限")
                calls = end.get("tool_calls") or []
                if not calls or not allow_tools:
                    break
                messages.append({"role": "assistant", "content": round_text or None, "tool_calls": [
                    {"id": c.get("id") or f"call_{i}", "type": "function",
                     "function": {"name": c.get("name") or "", "arguments": c.get("arguments") or ""}}
                    for i, c in enumerate(calls)]})
                for i, c in enumerate(calls):
                    cid = c.get("id") or f"call_{i}"
                    if c.get("name") != "run_readonly":
                        result = {"status": "rejected", "reason": f"未知工具：{c.get('name')}"}
                    elif len(tools_done) >= config.AGENT_MAX_TOOL_CALLS:
                        result = {"status": "rejected",
                                  "reason": f"本次提问已执行 {config.AGENT_MAX_TOOL_CALLS} 条命令，达到上限"}
                    else:
                        try:
                            args = json.loads(c.get("arguments") or "{}")
                            if not isinstance(args, dict):
                                raise ValueError("参数不是对象")
                        except ValueError as exc:
                            result = {"status": "rejected", "reason": f"工具参数不是合法 JSON：{exc}"}
                        else:
                            result = await self.run_tool(args.get("device"), args.get("command"), names, gen0)
                            if result["status"] != "rejected":
                                tools_done.append(result)
                            yield sse({"type": "tool", **{k: (v[:TOOL_OUTPUT_TO_UI] if k == "output" else v)
                                                          for k, v in result.items()}})
                    body = result.get("output") if result.get("status") == "ok" else \
                        f"[{result.get('status')}] {result.get('reason')}"
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "content": (body or "（无输出）")[:TOOL_OUTPUT_TO_MODEL]})
        except asyncio.CancelledError:
            self._spawn(self._finalize(student_id, attempt_id, mode, a, text_parts, tools_done, "", cancelled=True))
            raise
        except ai.AIError as exc:
            error = exc.message
        except Exception as exc:
            log.exception("Agent 对话出错")
            error = ai.redact(f"{type(exc).__name__}: {exc}")[:200]
        if error:
            yield sse({"type": "error", "message": friendly(error)})
        if self.env_gen != gen0:
            yield sse({"type": "notice", "message": f"回答过程中模拟环境已切换为 {self.env_desc}，"
                                                   "以上内容基于切换前的环境，请按新环境重新提问"})
        fin = self._spawn(self._finalize(student_id, attempt_id, mode, a, text_parts, tools_done, error))
        mid, penalty_msg = await asyncio.shield(fin)
        if penalty_msg:
            yield sse({"type": "penalty", "message": penalty_msg, "hints_used": a.hints_used})
        yield sse({"type": "done", "message_id": mid})

    async def _finalize(self, student_id: int, attempt_id: int | None, mode: str, a: Any, text_parts: list[str],
                        tools_done: list[dict[str, Any]], error: str, cancelled: bool = False) -> tuple[int, str]:
        """泄露检查（练习）、扣分、回复落库。返回 (消息 id, 扣分提示)。"""
        text = "\n\n".join(t.strip() for t in text_parts if t.strip())
        leaked = False
        penalty_msg = ""
        if mode == "practice" and a is not None and text:
            hit = hints.leaks(text, hints.banned_terms(a.scenario))
            if hit:
                leaked = True
                log.warning("导师回复含答案级内容（%s），按第 3 级提示记扣分", "、".join(hit))
                penalty_msg = await self._penalize(a)
        if cancelled:
            content = (text + "\n\n（回答被中断）") if text else "（回答被中断）"
        else:
            content = text or (f"（未能回答：{friendly(error)}）" if error else "（无回复）")
        mid = await asyncio.to_thread(
            db.add_agent_message, student_id, attempt_id, mode, "assistant", content,
            json.dumps([{k: t[k] for k in ("device", "command", "status")} for t in tools_done], ensure_ascii=False),
            leaked)
        return mid, penalty_msg

    async def _penalize(self, a: Any) -> str:
        """导师回复泄露：按第 3 级提示记一次扣分（与 PracticeManager.hint 相同的写法）。练习已结束则不记。"""
        pm = self.pm
        if pm.active is not a or a.closing or a.mode != "practice":
            return ""
        already = a.hints_used >= 3
        a.hints_used = max(a.hints_used, 3)
        await asyncio.to_thread(db.update_attempt, a.attempt_id, hints_used=a.hints_used)
        await asyncio.to_thread(db.add_agent_penalty, a.attempt_id)
        pm._persist(a)
        return ("导师的这条回复包含了接近答案的内容（如修复命令或故障配置），按使用第 3 级提示记一次扣分。"
                + ("（你之前已用过第 3 级提示，提示扣分不重复计算。）" if already else ""))

    async def history(self, student_id: int) -> dict[str, Any]:
        """当前场景的对话记录（页面刷新后恢复）：练习中取本次练习的，否则取自由实验最近的。"""
        mode = self.mode()
        if mode == "exam":
            return {"mode": mode, "messages": []}
        a = self.pm.active if mode == "practice" else None
        rows = await asyncio.to_thread(db.agent_messages, attempt_id=a.attempt_id, limit=60) if a is not None \
            else await asyncio.to_thread(db.agent_messages, student_id=student_id, limit=40)
        msgs = []
        for r in rows:
            if a is not None and r["student_id"] != student_id:
                continue
            try:
                tools = json.loads(r.get("tools_json") or "[]")
            except ValueError:
                tools = []
            msgs.append({"role": r["role"], "content": r["content"], "tools": tools, "leaked": bool(r["leaked"]),
                         "at": r["created_at"]})
        return {"mode": mode, "attempt_id": a.attempt_id if a is not None else None, "messages": msgs}


def friendly(reason: str) -> str:
    r = reason or ""
    if r.startswith(ai.KIND_TEXT["no_key"]):
        return "AI 服务未配置（没有 DeepSeek API Key），Agent 暂不可用"
    if r.startswith(ai.KIND_TEXT["timeout"]):
        return "AI 响应超时，请稍后再问一次"
    if r.startswith((ai.KIND_TEXT["network"], ai.KIND_TEXT["server"])):
        return "暂时连不上 AI 服务，请稍后再试"
    if r.startswith(ai.KIND_TEXT["rate_limit"]):
        return "AI 请求太频繁，请稍等片刻再问"
    if r.startswith((ai.KIND_TEXT["auth"], ai.KIND_TEXT["quota"])):
        return "AI 服务暂不可用（鉴权或余额问题），请联系老师"
    return f"AI 暂时不可用：{r}" if r else "AI 暂时不可用"
