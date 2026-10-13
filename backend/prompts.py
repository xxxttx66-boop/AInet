"""AI 提示词模板（集中管理，中文）。评分（spec §9）与三级提示（spec §7）。

约定：
- 系统提示词写角色、评判标准与输出 JSON 格式；用户提示词只放本次的数据（场景、学员记录、信号）。
- 一律要求只输出一个 JSON 对象（配合 response_format=json_object），字段名固定，便于稳健解析。
- 分数让 AI 给 0~100 的「得分率」，权重、截断、未通关折扣都由代码计算（AI 不自行打折，避免折扣算两次）。
"""
from __future__ import annotations

import json
from typing import Any

# ---------------- 评分 ----------------

SCORE_SYSTEM = """你是华为 VRP 网络设备排障实训的评分老师。系统会给你一道故障排查题（含标准排障思路）和学员在设备终端上的完整操作记录（命令及设备输出），请评判学员的排障过程。

评分分三个维度，其中「结果」由系统按判定规则确定性计算，你不评结果分。你只给下面两个维度打「得分率」（0~100 的整数）：

1. process（过程 / 方法论）：
   - 先定位再改配置：动手改配置之前，是否用 display / ping 等查看命令确认了故障点；没看就改属于盲目试错，要明显扣分。
   - 分层、有序排查：由近及远、两端对比，物理层 → 数据链路层 → 网络层 → 协议，路径是否合理。
   - 效率：命令是否必要、有没有大量重复或无关的命令；能读懂输出并据此推进，而不是乱敲。
   - 验证：修复后有没有用 ping / display 等验证。
   - 如果学员用了第 3 级提示（直接揭示答案），说明是照提示修复，过程分最高不超过 60。
   - 即使最终没修好，也按实际表现给分，不要因为没修好而额外打折（系统会另行处理）。
   - 几乎没有操作（例如只敲了一两条命令就放弃）时，过程分应很低。
2. side（规范 / 副作用）：
   - 满分 100 表示没有任何多余或有害的操作。
   - 扣分项：关闭了与故障无关的接口、删除或改动了与故障无关的正确配置、重启进程 / reset / reboot / save 等危险命令、改错后没有撤销、操作导致新的接口或邻居 Down（日志里出现 DOWN / Down）、大量报错命令。
   - 系统会给出用规则抽取的「确定性信号」，请结合信号和操作记录综合判断；信号只是线索，要看上下文（例如修复过程中必要的 undo 不算副作用；故障注入引起的日志不算学员的责任）。

关于「设备复位」：练习结束后系统会自动把设备配置复位到开练前（撤销场景和学员的全部改动），并告诉你复位结果。复位是系统自己的操作：
- 复位成功是系统的正常行为，不是学员的功劳，也不说明学员自己撤销了改动；
- 复位失败、有无法自动撤销的配置或不在复位范围内的配置，是系统的问题，不能因此扣学员的分，也不要在点评里把它算成学员的问题；
- 学员自己敲过的多余 / 有害命令仍按操作记录照常评判（与复位结果无关）。

其他说明：
- 每条命令前标注了执行它的设备（如 AR0>），点评时以工单为准区分各设备的角色，不要把设备张冠李戴。
- 命令可能使用缩写（dis = display，int = interface，sys = system-view 等）；输出可能被截断。
- 「标准思路」字段要对照参考排障路径，分步骤讲清楚这道题应该怎么查、每一步看什么、为什么，面向学员，通俗具体。
- 不要建议学员执行 save（保存配置），也不要单独写一条关于保存配置的建议。
- 如果给了「学员与导师的对话」：向导师提问本身不扣分；提问是否有针对性、能否据导师的引导自己推进排查，可作为过程分的参考。
  导师回复被系统判定含答案级内容的，视同学员使用了第 3 级提示（过程分最高不超过 60）。
- 点评要具体，引用学员实际敲过的命令，不要空话套话。

只输出一个 JSON 对象，不要输出任何其他文字，格式如下：
{
  "process": {"rate": 0到100的整数, "reason": "过程分理由，2~4 句"},
  "side": {"rate": 0到100的整数, "reason": "规范/副作用分理由，1~3 句"},
  "comment": {
    "summary": "一句话总评",
    "good": ["做得好的地方，每条一句，没有就给空数组"],
    "problems": ["存在的问题，每条一句"],
    "suggestions": ["改进建议，每条一句"]
  },
  "standard_path": "标准排障思路讲解，分步骤，可用 1. 2. 3. 编号"
}"""


def _fmt_offset(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "--:--"
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def format_logs(logs: list[dict[str, Any]], max_commands: int, output_chars: int,
                started_ts: float | None = None) -> str:
    """命令记录 -> 文本。每条：序号、相对开始的时间、设备、命令，下面缩进放输出摘要。"""
    if not logs:
        return "（学员没有执行任何命令）"
    items = logs
    skipped = 0
    if len(items) > max_commands:
        head = max_commands // 2
        skipped = len(items) - max_commands
        items = items[:head] + [None] + items[-(max_commands - head):]   # type: ignore[list-item]
    lines: list[str] = []
    n = 0
    for it in items:
        if it is None:
            lines.append(f"……（中间省略 {skipped} 条命令）……")
            n += skipped
            continue
        n += 1
        off = (it.get("t") - started_ts) if (started_ts is not None and it.get("t") is not None) else None
        lines.append(f"[{n}] +{_fmt_offset(off)} {it.get('device') or '?'}> {it.get('command') or ''}")
        out = (it.get("output") or "").strip()
        if out:
            if len(out) > output_chars:
                out = out[:output_chars] + f"……（截断，原长 {len(out)} 字符）"
            lines.extend("    " + x for x in out.split("\n"))
    return "\n".join(lines)


def _reset_text(reset: dict[str, Any] | None) -> str:
    if reset is None:
        return "（无记录）"
    if not reset.get("snapshot", True):
        return "未找到开练前快照，只撤销了场景自身的改动（系统问题，与学员无关，不扣分）"
    lines = ["已全部复位到开练前配置" if reset.get("ok") else "复位未完全成功（系统问题，与学员无关，不扣分）"]
    for dev, ls in (reset.get("leftovers") or {}).items():
        lines.append(f"- {dev} 无法自动撤销的配置行：" + "；".join(map(str, ls))[:400])
    for dev, ls in (reset.get("excluded") or {}).items():
        lines.append(f"- {dev} 不在复位范围内的配置：" + "；".join(map(str, ls))[:400])
    return "\n".join(lines)


def build_score_user(scenario: dict[str, Any], *, passed: bool, duration_s: int | None, hints_used: int,
                     logs_text: str, signals: dict[str, Any], reset: dict[str, Any] | None = None,
                     exam: dict[str, Any] | None = None, agent_text: str = "") -> str:
    s = scenario
    mode_lines: list[str] = []
    if exam:
        # 考试（里程碑 6）：学员自己判断修好了再点「提交本题」，系统才验收；不会自动提示通关
        subs = exam.get("submissions") or []
        mode_lines = [
            "模式：考试（系统不自动判定，学员认为修好后点「提交本题」由系统验收；"
            f"本题最多提交 {exam.get('max_submissions', '-')} 次，实际提交 {len(subs)} 次"
            + ("，其中系统在本题结束时代为提交 1 次" if exam.get("auto_submitted") else "") + "）",
            "各次提交结果：" + ("；".join(f"第 {x.get('n')} 次{'通过' if x.get('passed') else '未通过'}"
                                    f"（{x.get('passed_items')}/{x.get('total')} 项）" for x in subs) or "无"),
        ]
        if exam.get("timed_out"):
            mode_lines.append("本题因时间用完而结束（单题限时或考试总时长到）")
        if exam.get("time_limit_s"):
            mode_lines.append(f"本题限时：{_fmt_offset(exam['time_limit_s'])}（分:秒）")
        mode_lines.append("评分时注意：考试中学员拿不到自动判定的反馈，修复后是否自己用 ping / display 验证过再提交，"
                          "是过程分的重要依据；多次提交失败说明验证不充分。")
    fault = s.get("hints", ["", "", ""])[-1] if s.get("hints") else ""
    sol = s.get("solution") or {}
    parts = [
        "## 题目",
        f"标题：{s.get('title', '')}（难度 {s.get('level', '-')}）",
        f"知识点：{'、'.join(s.get('knowledge_tags') or []) or '-'}",
        f"工单（学员看到的症状）：{s.get('symptom', '')}",
        f"学员可登录的设备：{'、'.join(s.get('accessible_devices') or [])}",
        f"故障真相（学员看不到）：{fault}",
        f"参考修复命令：{json.dumps(sol, ensure_ascii=False) if sol else '-'}",
        f"参考排障路径：{s.get('reference_path') or '-'}",
        "评分要点：" + ("\n" + "\n".join(f"- {p}" for p in s["scoring_points"]) if s.get("scoring_points") else "-"),
        "",
        "## 学员本次" + ("考试作答" if exam else "练习"),
        *mode_lines,
        f"结果（系统判定）：{'已修复（通关）' if passed else ('未修复' if exam else '未修复（放弃）')}",
        f"用时：{_fmt_offset(duration_s) if duration_s is not None else '未知'}（分:秒）",
        f"使用提示：{hints_used} 级（共 3 级：1 方向、2 命令建议、3 直接揭示）",
        f"结束后的设备复位（系统操作，见系统提示中的说明）：{_reset_text(reset)}",
        "",
        "## 确定性信号（规则抽取，供参考）",
        json.dumps(signals, ensure_ascii=False, indent=1),
        "",
        "## 操作记录（按时间顺序；缩进行是该命令的设备输出摘要，可能含其后出现的设备日志）",
        logs_text,
        "",
    ]
    if agent_text:
        parts += ["## 学员与 Agent 导师的对话（摘要，过程分参考）", agent_text, ""]
    parts += ["请按系统提示中的格式输出 JSON。"]
    return "\n".join(parts)


# ---------------- 三级提示 ----------------

HINT_LEVEL_RULES = {
    1: ("方向提示", "只指出排查方向：该查哪一层、哪类问题、从哪里查起。不给具体命令，不说出故障点、错误的配置值和修复方法；"
                    "不要直接点名故障所在的设备或接口——只有学员已经用命令排除了其他位置（例如已确认本端正常）时，"
                    "才可以顺着他的结论引导去查剩下的位置。不超过 80 字。"),
    2: ("命令建议", "给出接下来最该执行的 1~3 条查看类命令（display / ping / tracert 等），并说明在输出里重点看什么、"
                    "和什么做对比。只能建议查看类命令，不给配置命令，不说出故障点、错误的配置值和修复方法。不超过 120 字。"),
    3: ("直接揭示", "直接说明故障点在哪台设备、哪个接口或哪项配置、错在哪里，并给出修复步骤和配置命令，"
                    "以及修复后如何验证。不超过 200 字。"),
}

HINT_SYSTEM = """你是华为 VRP 网络设备排障实训的助教，学员正在终端上排查一道故障题，主动向你求助。你要根据「提示级别」给出一条中文提示。

级别定义：
- 第 1 级「方向提示」：只给排查方向。
- 第 2 级「命令建议」：给出具体的查看命令和看什么。
- 第 3 级「直接揭示」：说出故障点和修复方法。

要求：
- 结合学员已经做过的操作：他已经查过、已排除的不要再让他重复；他已经看到异常输出却没注意到的，引导他回头看；他走偏了要拉回来。
- 第 1、2 级绝不能泄露答案：不能说出故障点、错误的配置值或修复命令，也不能出现「禁止出现的内容」里的任何一项（原样或换个写法都不行）。
- 练习环境不保存配置：不要建议 save，也不要建议 reboot、reset 等与本题无关的危险命令。
- 语气像老师，简洁具体，直接对学员说话，不要寒暄，不要复述题目。

只输出一个 JSON 对象，不要输出任何其他文字：{"hint": "提示内容"}"""


def build_hint_user(scenario: dict[str, Any], level: int, logs_text: str, banned: list[str]) -> str:
    s = scenario
    name, rule = HINT_LEVEL_RULES[level]
    parts = [
        f"## 提示级别：第 {level} 级「{name}」",
        f"本级要求：{rule}",
        "",
        "## 题目",
        f"标题：{s.get('title', '')}",
        f"工单（学员看到的症状）：{s.get('symptom', '')}",
        f"学员可登录的设备：{'、'.join(s.get('accessible_devices') or [])}",
        f"参考排障路径（供你把握方向，第 1、2 级不要照抄给学员）：{s.get('reference_path') or '-'}",
        f"故障真相（第 1、2 级绝不能透露）：{(s.get('hints') or ['', '', ''])[-1]}",
        f"该级别的参考提示（可以参考措辞，但要结合学员当前进度改写）：{(s.get('hints') or ['', '', ''])[level - 1]}",
    ]
    if level < 3 and banned:
        parts += ["", "## 禁止出现的内容（第 1、2 级）", *[f"- {b}" for b in banned]]
    parts += ["", "## 学员到目前为止的操作（缩进行是设备输出摘要）", logs_text, "",
              f"请输出第 {level} 级提示的 JSON。"]
    return "\n".join(parts)


# ---------------- Agent 面板（spec §16） ----------------
# 注意：系统提示词里不要写具体的配置命令示例（快照测试会检查发给模型的内容里没有任何场景的答案命令）。

AGENT_COMMON = """工作方式：
- 每次提问时系统会附上「环境快照」：实验后端与设备平台、当前拓扑（设备、状态、连线）、学员最近在终端里敲的命令和输出摘要，练习中还有当前工单。回答要基于快照和你查到的实际输出，不要编造设备上没有的信息。
- 需要设备的实时信息时，调用工具 run_readonly(device, command) 在设备上执行一条只读命令：只能是 display 或 ping 开头的完整命令（不要用缩写），每次提问最多执行 3 条，命令由系统在后台执行，学员终端里看不到。设备忙（学员正在输入）时会返回失败，这时直接根据已有信息回答，或请学员稍后再问。
- 学员的终端由学员自己操作；你不能、也不要声称自己修改了任何配置。
- 始终用中文回答，简洁具体；调用工具前不需要先预告；可以用 Markdown 的列表、行内代码和代码块，不要用表格和标题。"""

AGENT_SYSTEM_LAB = """你是 ainet 实训平台里的网络实验助手，帮助学员理解他正在操作的华为网络设备（eNSP / eNSP Pro 模拟器，VRP 或 YunShan 系统）和当前拓扑。现在是「自由实验」模式：学员可以随意提问，你可以直接解释原理、解读命令输出、给出配置建议和完整的配置步骤。

""" + AGENT_COMMON

AGENT_SYSTEM_MENTOR = """你是 ainet 实训平台里的排障导师。学员正在做一道故障排查练习（工单见环境快照），你的任务是引导他自己找到并修复故障，而不是替他做题。

导师规则（必须遵守）：
- 只引导思路、不给答案：可以讲原理、解释命令输出的含义、建议下一步查看什么、用提问引导学员对比两端；不能直接指出故障在哪台设备的哪项配置、不能说出错误的配置值应该改成什么，也不能给出用来修复的配置命令。
- 即使学员直接索要答案，也只给进一步的引导，并提醒他可以使用面板下方的分级提示（第 3 级会直接揭示答案并扣分）。
- 优先让学员自己在终端里执行查看命令；只有学员明确请你帮忙查看，或需要确认环境现状时，才调用工具。
- 引用查到的输出时只说现象，不要把结论和修复方法一起说出来。
- 你的回复会经过系统检查：包含答案级内容（修复命令、故障配置值等）时，会按使用第 3 级提示给学员记一次扣分，所以务必守住上面的规则。

""" + AGENT_COMMON

AGENT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_readonly",
        "description": "在指定设备上执行一条只读命令（display ... 或 ping ...），返回设备输出。系统在后台执行，学员终端看不到。",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "设备名，取环境快照里的设备名"},
                "command": {"type": "string", "description": "完整的只读命令，只能以 display 或 ping 开头，不要缩写"},
            },
            "required": ["device", "command"],
        },
    },
}]


def format_agent_dialog(messages: list[dict[str, Any]], max_items: int = 16, chars: int = 240) -> str:
    """评分用的导师对话摘要：学员提问原文 + 导师回复摘要 + 执行过的只读命令 + 是否判定泄露。"""
    if not messages:
        return ""
    items = messages[-max_items:]
    skipped = len(messages) - len(items)
    lines: list[str] = []
    if skipped:
        lines.append(f"（更早的 {skipped} 条对话已省略）")
    for m in items:
        text = " ".join(str(m.get("content") or "").split())
        if len(text) > chars:
            text = text[:chars] + "……"
        if m.get("role") == "user":
            lines.append(f"学员问：{text}")
            continue
        tools: Any = []
        try:
            tools = json.loads(m.get("tools_json") or "[]")
        except ValueError:
            pass
        ran = "；".join(f"{t.get('device')}> {t.get('command')}" for t in tools if isinstance(t, dict))
        lines.append(f"导师答：{text}" + (f"（导师替学员查看了：{ran}）" if ran else "")
                     + ("【系统判定此回复含答案级内容，已按第 3 级提示记扣分】" if m.get("leaked") else ""))
    return "\n".join(lines)
