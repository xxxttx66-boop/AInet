"""场景库（spec §6）：scenarios/*.json 加载、校验、就绪判断。

场景以 JSON 文件随仓库版本管理；启动时加载并校验，通过的同步进 db 的 scenarios 表（按 id 即 key
upsert），这样 attempts 有外键可引用、档案页 / 错题本能 JOIN 出标题，文件以后删掉了历史记录也还在。
运行时以内存中的加载结果为准（JSON 文件是单一事实源，库里只是镜像）。

字段（spec §6 的落地版）：
  id, title, level, knowledge_tags[], topology（拓扑文件名，按文件名去扩展名、忽略大小写比对）,
  required_devices[], accessible_devices[]（⊆ required）, symptom（工单式描述）,
  baseline{设备:[命令]}, inject{设备:[命令]}, restore{设备:[命令]},
  checks[{device, command, expect(正则), negate?, desc}]（全部满足即通关）,
  hints[3]（方向 → 命令 → 揭示）, reference_path, solution{设备:[命令]}（可选，参考修复命令，不给学员看）,
  note（可选，出题备注）, scoring_points[]（可选，评分要点，送给 AI 评分参照，不给学员看）,
  platform（可选，设备系统：vrp5 = eNSP 经典版 AR/S，缺省；yunshan = eNSP Pro 的 YunShan OS。topology 对 eNSP Pro
  是沙箱名）。就绪判断要求场景平台与当前实验后端一致（lab.py）。
  课程页字段（可选，2026-10-07，只放面向学员的内容，不能泄露答案）：chapter（章节 id，须在 chapters.json 中）、
  difficulty（1~5）、est_minutes（预计用时，分钟）、summary（一句话症状）、tags[]（知识点标签）。summary / tags
  不能含注入 / 修复 / 判定命令和只在故障配置里出现的 IP（与第 1、2 级静态提示同一套检查，参数化场景每个抽样都查）。
  hosts[]（可选，⊆ required）：终端主机（eNSP Pro 的 PC-CLIENT 等）。主机的命令行只能 ping，地址由实验后端的接口
  配置：baseline / inject / restore 里主机的「命令」只能是 HOST_COMMANDS（ip address / ip gateway / undo …），
  由练习流程翻译成接口调用；checks 在主机上只能用 ping（Linux iputils 格式：`2 packets transmitted, 2 received`）。
  draft（可选 bool，2026-10-07）：未在真机验证的草稿题（docs/question-bank-plan.md）。GET /api/scenarios 缺省不列出
  （config.SHOW_DRAFTS / AINET_SHOW_DRAFTS=1 时列出），按 id 开始练习不受限制（真机验证用）。

校验（spec §6.1 的规则子集）：
- 必填字段与类型；hints 恰好 3 条；checks 至少 1 条、expect 能编译。
- 设备引用：baseline / inject / restore / checks / accessible 的设备都必须在 required_devices 中；
  拓扑文件能找到时，required 设备必须存在于拓扑且有 console（com_port > 0）。
  不校验接口名——旧格式 .topo 的接口序号与设备真实接口不一定对得上（spec §3）。
- checks 只允许只读命令（display / ping / tracert 前缀）。
- baseline / inject / restore 不能包含切断 console / 管理通道或破坏性的命令（FORBIDDEN）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import params as P
import topology
from lab import console_of

READONLY_PREFIXES = ("display", "dis", "ping", "tracert")

# 终端主机（hosts）的配置「命令」：不发给设备，由练习流程翻译成实验后端的接口调用（lab.host_set）
HOST_COMMANDS: tuple[tuple[str, str], ...] = (
    (r"^ip address (\d{1,3}(?:\.\d{1,3}){3}) (\d{1,2}|\d{1,3}(?:\.\d{1,3}){3})$", "address"),
    (r"^ip gateway (\d{1,3}(?:\.\d{1,3}){3})$", "gateway"),
    (r"^undo ip address$", "undo-address"),
    (r"^undo ip gateway$", "undo-gateway"),
)


def apply_host_commands(cfg: dict[str, str], cmds: list[str]) -> dict[str, str]:
    """按 HOST_COMMANDS 依次修改主机配置 {ip, mask, gateway}，返回新配置；不认识的命令抛 ValueError。"""
    out = {"ip": cfg.get("ip", ""), "mask": cfg.get("mask", ""), "gateway": cfg.get("gateway", "")}
    for raw in cmds:
        c = re.sub(r"\s+", " ", raw.strip())
        if not c:
            continue
        for pat, op in HOST_COMMANDS:
            m = re.match(pat, c, re.I)
            if m is None:
                continue
            if op == "address":
                out["ip"], out["mask"] = m.group(1), m.group(2)
            elif op == "gateway":
                out["gateway"] = m.group(1)
            elif op == "undo-address":
                out["ip"] = out["mask"] = ""
            else:
                out["gateway"] = ""
            break
        else:
            raise ValueError(f"终端主机不支持的配置命令：{raw!r}（只能用 ip address <IP> <掩码> / ip gateway <IP> / "
                             "undo ip address / undo ip gateway）")
    return out

# (正则, 原因)：命令先转小写、压缩空白再匹配
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"^user-interface\b", "进入 user-interface（console / vty）视图可能切断管理通道"),
    (r"^undo\s+user-interface\b", "删除 user-interface"),
    (r"^(authentication-mode|set\s+authentication|idle-timeout|protocol\s+inbound)\b", "修改登录认证 / 超时"),
    (r"^reboot\b", "重启设备"),
    (r"^schedule\s+reboot\b", "定时重启"),
    (r"^reset\s+saved-configuration\b", "清空保存的配置"),
    (r"^startup\b", "修改启动配置"),
    (r"^save\b", "保存配置（场景不得改动设备的保存配置）"),
    (r"^(format|delete|rmdir)\b", "删除文件 / 格式化存储"),
    (r"^undo\s+info-center\b", "关闭信息中心会让设备日志不可见"),
    (r"^info-center\s+disable\b", "关闭信息中心"),
    (r"^sysname\b", "修改设备名会打乱提示符识别"),
    (r"^quit$", "quit 在用户视图会登出 console，用 return 回到用户视图"),
    (r"^screen-length\b", "console 不支持 screen-length"),
)

CHAPTERS_FILE = "chapters.json"          # 课程章节（scenarios/ 目录下，不是场景）
COURSE_FIELDS = ("chapter", "difficulty", "est_minutes", "summary", "tags")
REQUIRED = ("id", "title", "topology", "required_devices", "accessible_devices", "symptom",
            "checks", "hints", "restore")
PLATFORMS = {"vrp5": "VRP5（eNSP 经典版）", "yunshan": "YunShan OS（eNSP Pro）"}
# 平台短标签：题名不带平台后缀（2026-10-07 起），档案 / 成绩单 / 模板管理里与题名并列显示，区分两个平台的同名题
PLATFORM_TAGS = {"vrp5": "VRP5", "yunshan": "YunShan"}


def platform_of(s: dict[str, Any]) -> str:
    return s.get("platform") or "vrp5"


class ScenarioError(ValueError):
    pass


def _norm_cmd(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip().lower())


def forbidden_reason(cmd: str) -> str | None:
    c = _norm_cmd(cmd)
    for pat, why in FORBIDDEN:
        if re.search(pat, c):
            return why
    return None


def is_readonly(cmd: str) -> bool:
    c = _norm_cmd(cmd)
    return any(c == p or c.startswith(p + " ") for p in READONLY_PREFIXES)


def topo_key(name_or_path: str) -> str:
    """拓扑比对键：文件名去扩展名，忽略大小写（兼容 Windows 路径与仅给文件名）。"""
    base = re.split(r"[\\/]", name_or_path.strip())[-1]
    if base.lower().endswith(".topo"):
        base = base[:-5]
    return base.strip().casefold()


# 同时运行设备上限的计数口径（里程碑 6.17）：PC 类终端（PC-CLIENT / SERVER / STA / MCS 等，很轻）与 Cloud 不计
UNCOUNTED_KINDS = ("pc", "cloud")


def topo_matches(s: dict[str, Any], topo: dict[str, Any] | None) -> bool:
    """topo（resolve() 的 topology）是不是本题的拓扑 / 沙箱（与 readiness 同一比对规则）。"""
    if not topo:
        return False
    want = topo_key(s["topology"])
    return topo_key(topo.get("path") or topo.get("name") or "") == want or topo_key(topo.get("name") or "") == want


def device_load(s: dict[str, Any], topo: dict[str, Any] | None = None) -> int | None:
    """本题要同时运行的路由器 / 交换机 / 防火墙等非 PC 设备台数（同时运行设备上限的计数口径，里程碑 6.17）。

    - 有本题拓扑（沙箱）时以拓扑里的设备类型为准：kind 为 pc / cloud 的不计（eNSP Pro 按 deviceType 判断，
      PC-CLIENT 等为 pc），其余（router / switch / other，如防火墙）都计；本题设备不在拓扑里 = 判断不了，返回 None。
    - 拿不到拓扑时按题目自身的信息：hosts[] 里声明的终端主机不计，设备名按 eNSP 命名惯例是 PC / CLIENT / SERVER /
      STA / MCS / Cloud 开头的不计（topology.device_kind），其余都计（R1、T1 这类名字按路由器 / 交换机算）。"""
    req = list(s.get("required_devices") or [])
    if topo_matches(s, topo):
        by = {d.get("name"): d for d in topo.get("devices") or []}
        if any(d not in by for d in req):
            return None
        return sum(by[d].get("kind") not in UNCOUNTED_KINDS for d in req)
    hosts = set(s.get("hosts") or [])
    return sum(d not in hosts and topology.device_kind(d) not in UNCOUNTED_KINDS for d in req)


def _device_map(v: Any, field: str, devices: set[str], errs: list[str]) -> dict[str, list[str]]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        errs.append(f"{field} 必须是 {{设备名: [命令...]}}")
        return {}
    out: dict[str, list[str]] = {}
    for dev, cmds in v.items():
        if dev not in devices:
            errs.append(f"{field} 引用的设备 {dev} 不在 required_devices 中")
        if not isinstance(cmds, list) or not all(isinstance(c, str) for c in cmds):
            errs.append(f"{field}.{dev} 必须是命令字符串数组")
            continue
        out[dev] = cmds
    return out


def load_chapters(path: str) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """读 scenarios/chapters.json：[{id, title, order?, desc?}]（按 order 排序）。文件不存在返回 (None, [])。"""
    fp = os.path.join(path, CHAPTERS_FILE)
    if not os.path.isfile(fp):
        return None, []
    try:
        with open(fp, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        return None, [f"{CHAPTERS_FILE}：{exc}"]
    items = raw.get("chapters") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return None, [f"{CHAPTERS_FILE}：应为 {{chapters: [{{id, title}}]}}"]
    out, errs, seen = [], [], set()
    for i, c in enumerate(items):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not isinstance(c.get("title"), str) \
                or not c["id"].strip() or not c["title"].strip():
            errs.append(f"{CHAPTERS_FILE}：第 {i + 1} 项需要非空字符串 id / title")
            continue
        if c["id"] in seen:
            errs.append(f"{CHAPTERS_FILE}：章节 id 重复：{c['id']}")
            continue
        seen.add(c["id"])
        out.append({"id": c["id"], "title": c["title"], "order": c.get("order", i + 1), "desc": str(c.get("desc", ""))})
    out.sort(key=lambda c: (c["order"], c["id"]))
    return out, errs


def validate(s: Any, topo: dict[str, Any] | None = None, chapters: set[str] | None = None) -> dict[str, Any]:
    """校验一个场景对象，返回规范化后的副本；不合法抛 ScenarioError（列出全部问题）。

    topo：场景拓扑的 parse_topo 结果（找得到时传入，用于核对设备存在且有 console）。

    带 params 的参数化场景（里程碑 6）：先查 params 结构与引用（未声明 / 未引用 / 设备名与接口名不能参数化），
    再对参数空间抽样（种子 0 = 默认值、种子 1..N、各参数边界值），每次展开都要通过下面的全部规则
    （禁止命令、只读 checks、静态提示不泄露……），且注入命令展开后不能与参考修复相同。
    返回的是模板（仍带占位符与 params），运行时用 expand(模板, 种子) 取展开后的场景。
    """
    if not isinstance(s, dict):
        raise ScenarioError("场景必须是 JSON 对象")
    perrs = P.check_spec(s)
    if perrs:
        raise ScenarioError("；".join(perrs))
    if not P.has_params(s):
        out = _validate_plain(s, topo, chapters)
        out.pop("params", None)
        return out
    errs: list[str] = []
    seen: set[str] = set()
    for label, seed, ov in P.samples(s):
        try:
            e = P.expand_raw(s, seed, ov)
            _validate_plain(e, topo, chapters)
            found = P.injected_equals_fix(s, e)
        except (ScenarioError, P.ParamError) as exc:
            found = [str(exc)]
        for x in found:
            if x not in seen:          # 同一问题在多个种子上出现只报一次
                seen.add(x)
                errs.append(f"{label}：{x}")
        if len(errs) >= 6:
            break
    if errs:
        raise ScenarioError("；".join(errs))
    return dict(s)


def expand(s: dict[str, Any], seed: int | None = 0) -> dict[str, Any]:
    """按种子展开场景（validate 的返回值）→ 规范化后的场景，带 seed / param_values。

    没有 params 的场景直接复制（seed 照记，便于统一记录）。展开结果才可以交给注入、判定、提示、评分。
    """
    seed = int(seed or 0)
    if not P.has_params(s):
        out = dict(s)
        out["seed"], out["param_values"] = seed, {}
        return out
    out = _validate_plain(P.expand_raw(s, seed), None)
    if "_file" in s:
        out["_file"] = s["_file"]
    return out


def _validate_plain(s: Any, topo: dict[str, Any] | None = None, chapters: set[str] | None = None) -> dict[str, Any]:
    """不含参数的场景校验（参数化场景对每次展开的结果调用）。"""
    errs: list[str] = []
    if not isinstance(s, dict):
        raise ScenarioError("场景必须是 JSON 对象")
    for k in REQUIRED:
        if k not in s:
            errs.append(f"缺少字段 {k}")
    if errs:
        raise ScenarioError("；".join(errs))
    if not isinstance(s["id"], str) or not re.fullmatch(r"[A-Za-z0-9_.\-]+", s["id"]):
        errs.append("id 只能包含字母、数字、_ . -")
    for k in ("title", "topology", "symptom"):
        if not isinstance(s[k], str) or not s[k].strip():
            errs.append(f"{k} 必须是非空字符串")
    req = s["required_devices"]
    if not isinstance(req, list) or not req or not all(isinstance(d, str) for d in req):
        errs.append("required_devices 必须是非空设备名数组")
        req = []
    devices = set(req)
    acc = s["accessible_devices"]
    if not isinstance(acc, list) or not all(isinstance(d, str) for d in acc):
        errs.append("accessible_devices 必须是设备名数组")
        acc = []
    for d in acc:
        if d not in devices:
            errs.append(f"accessible_devices 中的 {d} 不在 required_devices 中")
    platform = s.get("platform", "vrp5")
    if platform not in PLATFORMS:
        errs.append(f"platform 只能是 {' / '.join(PLATFORMS)}：{platform!r}")
    hosts = s.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(d, str) for d in hosts):
        errs.append("hosts 必须是设备名数组")
        hosts = []
    for d in hosts:
        if d not in devices:
            errs.append(f"hosts 中的 {d} 不在 required_devices 中")
    tags = s.get("knowledge_tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        errs.append("knowledge_tags 必须是字符串数组")
    draft = s.get("draft", False)
    if not isinstance(draft, bool):
        errs.append(f"draft 必须是 true / false：{draft!r}")

    maps = {f: _device_map(s.get(f), f, devices, errs) for f in ("baseline", "inject", "restore", "solution")}
    for f in ("baseline", "inject", "restore"):
        for dev, cmds in maps[f].items():
            if dev in hosts:
                try:
                    apply_host_commands({}, cmds)
                except ValueError as exc:
                    errs.append(f"{f}.{dev}：{exc}")
                continue
            for c in cmds:
                why = forbidden_reason(c)
                if why:
                    errs.append(f"{f}.{dev} 含禁止命令 {c!r}：{why}")
    if not maps["inject"]:
        errs.append("inject 不能为空（场景必须注入故障）")
    for dev in maps["inject"]:
        if dev not in maps["restore"]:
            errs.append(f"inject 改动了 {dev}，restore 必须给出 {dev} 的还原命令")
    for dev in maps["baseline"]:
        if dev not in maps["restore"]:
            errs.append(f"baseline 改动了 {dev}，restore 必须给出 {dev} 的还原命令")

    checks = s["checks"]
    norm_checks = []
    if not isinstance(checks, list) or not checks:
        errs.append("checks 至少一条")
        checks = []
    for i, c in enumerate(checks):
        if not isinstance(c, dict) or not all(k in c for k in ("device", "command", "expect")):
            errs.append(f"checks[{i}] 需要 device / command / expect")
            continue
        if c["device"] not in devices:
            errs.append(f"checks[{i}] 的设备 {c['device']} 不在 required_devices 中")
        if not is_readonly(str(c["command"])):
            errs.append(f"checks[{i}] 只允许只读命令（{' / '.join(READONLY_PREFIXES)}）：{c['command']!r}")
        elif c["device"] in hosts and not _norm_cmd(str(c["command"])).startswith("ping "):
            errs.append(f"checks[{i}] 在终端主机 {c['device']} 上只能用 ping：{c['command']!r}")
        try:
            re.compile(str(c["expect"]))
        except re.error as exc:
            errs.append(f"checks[{i}] 的 expect 不是合法正则：{exc}")
        norm_checks.append({"device": c["device"], "command": str(c["command"]).strip(),
                            "expect": str(c["expect"]), "negate": bool(c.get("negate", False)),
                            "desc": str(c.get("desc", ""))})

    hints = s["hints"]
    if not isinstance(hints, list) or len(hints) != 3 or not all(isinstance(h, str) and h for h in hints):
        errs.append("hints 必须恰好 3 条非空字符串（方向 → 命令 → 揭示）")
    else:
        # AI 超时 / 失败时回退到静态提示：第 1、2 级的静态提示同样不能含修复配置、注入配置或故障特有的值
        import hints as _hints
        try:
            banned = _hints.banned_terms(s)
        except (AttributeError, TypeError):   # inject / solution 格式错误另有报错，这里不重复
            banned = []
        for lvl in (1, 2):
            hit = _hints.leaks(hints[lvl - 1], banned)
            if hit:
                errs.append(f"第 {lvl} 级静态提示泄露答案：{'、'.join(hit)}")

    # 课程页字段（可选）：只能是面向学员的内容，不能泄露答案
    course = _course_fields(s, chapters, errs)

    # 评分要点（spec §9，可选）：字符串列表，送给 AI 评分参照，不给学员看
    points = s.get("scoring_points", [])
    if not isinstance(points, list) or not all(isinstance(p, str) and p.strip() for p in points):
        errs.append("scoring_points 必须是非空字符串列表")
        points = []

    if topo is not None:
        by_name = {d["name"]: d for d in topo["devices"]}
        for d in req:
            if d not in by_name:
                errs.append(f"设备 {d} 不在拓扑 {topo['name']} 中")
            elif console_of(by_name[d]) <= 0:
                errs.append(f"设备 {d} 没有 console（com_port=0），系统无法注入或判定")
            elif d in hosts and by_name[d].get("kind") not in (None, "pc"):
                errs.append(f"hosts 中的 {d} 在拓扑里不是终端主机（{by_name[d].get('kind')}）")
    if errs:
        raise ScenarioError("；".join(errs))

    out = dict(s)
    out.update(baseline=maps["baseline"], inject=maps["inject"], restore=maps["restore"],
               solution=maps["solution"], checks=norm_checks, knowledge_tags=list(tags),
               level=s.get("level", ""), reference_path=s.get("reference_path", ""), platform=platform,
               scoring_points=[p.strip() for p in points], hosts=list(hosts), draft=draft is True, **course)
    return out


def _course_fields(s: dict[str, Any], chapters: set[str] | None, errs: list[str]) -> dict[str, Any]:
    """校验课程页字段（chapter / difficulty / est_minutes / summary / tags），返回规范化后的值（缺省的不返回）。"""
    out: dict[str, Any] = {}
    if "chapter" in s:
        ch = s["chapter"]
        if not isinstance(ch, str) or not ch.strip():
            errs.append("chapter 必须是非空字符串（章节 id）")
        elif chapters is not None and ch not in chapters:
            errs.append(f"chapter {ch!r} 不在 {CHAPTERS_FILE} 中（可选：{'、'.join(sorted(chapters))}）")
        else:
            out["chapter"] = ch
    if "difficulty" in s:
        d = s["difficulty"]
        if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 5:
            errs.append("difficulty 必须是 1~5 的整数")
        else:
            out["difficulty"] = d
    if "est_minutes" in s:
        m = s["est_minutes"]
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 600:
            errs.append("est_minutes 必须是 1~600 的整数（分钟）")
        else:
            out["est_minutes"] = m
    texts: list[tuple[str, str]] = []
    if "summary" in s:
        v = s["summary"]
        if not isinstance(v, str) or not v.strip() or len(v) > 80 or "\n" in v:
            errs.append("summary 必须是一句话（非空、不换行、不超过 80 字）")
        else:
            out["summary"] = v.strip()
            texts.append(("summary", v))
    if "tags" in s:
        v = s["tags"]
        if not isinstance(v, list) or not all(isinstance(t, str) and t.strip() and len(t) <= 20 for t in v):
            errs.append("tags 必须是字符串数组（每个不超过 20 字）")
        else:
            out["tags"] = [t.strip() for t in v]
            texts += [("tags", t) for t in v]
    for k in ("summary", "tags"):
        if k in s and isinstance(s[k], (str, list)) and "{" in json.dumps(s[k], ensure_ascii=False):
            errs.append(f"{k} 不能用参数占位符（课程页按默认值展示，不随种子变化）")
    if texts:
        # 与第 1、2 级静态提示同一套：修复 / 注入配置、只在故障配置里出现的 IP；另加判定命令与正则
        import hints as _hints
        try:
            banned = _hints.banned_terms(s)
        except (AttributeError, TypeError):
            banned = []
        secrets = [c.get("command", "") for c in s.get("checks") or [] if isinstance(c, dict)]
        for f in ("inject", "solution"):
            for cmds in (s.get(f) or {}).values() if isinstance(s.get(f), dict) else []:
                secrets += [c for c in cmds if isinstance(c, str)]
        for field, text in texts:
            hit = _hints.leaks(text, banned)
            low = text.lower()
            hit += [x for x in secrets if len(x.strip()) >= 6 and x.strip().lower() in low]
            if hit:
                errs.append(f"{field} 泄露答案：{'、'.join(dict.fromkeys(hit))}")
    return out


def _find_topo(name: str, entries: list[dict[str, Any]]) -> str | None:
    key = topo_key(name)
    for e in entries:
        if topo_key(e["path"]) == key:
            return e["path"]
    return None


def load_dir(path: str, topologies: list[dict[str, Any]] | None = None
             ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """加载目录下全部 *.json。返回 ({id: 场景}, 错误列表)；单个文件出错不影响其他文件。

    topologies：topology.list_topologies() 的结果（None 时现扫）。找不到场景的拓扑文件时只跳过
    设备核对（开发机上可能没有该拓扑），就绪判断时还会再按当前运行的拓扑核对一次。
    """
    if topologies is None:
        try:
            topologies = topology.list_topologies()
        except Exception:
            topologies = []
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not os.path.isdir(path):
        return result, [f"场景目录不存在：{path}"]
    chapters, cerrs = load_chapters(path)
    errors += cerrs
    chapter_ids = {c["id"] for c in chapters} if chapters is not None else None
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json") or fn == CHAPTERS_FILE:
            continue
        fp = os.path.join(path, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                raw = json.load(f)
            tp = _find_topo(raw.get("topology", "") if isinstance(raw, dict) else "", topologies)
            s = validate(raw, topology.parse_topo(tp) if tp else None, chapter_ids)
            if s["id"] in result:
                raise ScenarioError(f"id 重复：{s['id']}")
            s["_file"] = fn
            result[s["id"]] = s
        except (OSError, ValueError) as exc:
            errors.append(f"{fn}：{exc}")
    return result, errors


def devices_of(s: dict[str, Any]) -> list[str]:
    return list(s["required_devices"])


def readiness(s: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """按 spec §6.3 判断当前 eNSP 状态能否开始本场景。cur 为 topology.resolve_running() 的结果。

    返回 {ready, reason, ports{设备: console 端口}}。
    """
    topo_name = topo_key(s["topology"])
    display_topo = re.split(r"[\\/]", s["topology"])[-1].removesuffix(".topo")
    req = s["required_devices"]
    lab_info = cur.get("lab") or {"kind": "ensp", "platform": "vrp5", "power": False, "label": "eNSP"}
    want = s.get("platform", "vrp5")
    if lab_info.get("platform", "vrp5") != want:
        return {"ready": False, "ports": {},
                "reason": f"本题是 {PLATFORMS.get(want, want)} 的题目，当前实验后端是 {lab_info.get('label')}"
                          f"（请切换模拟环境）"}
    if lab_info.get("kind") == "enspro":
        hint = f"请切换到沙箱 {display_topo}，并启动 {'、'.join(req)}"
    else:
        hint = f"请在 eNSP 打开 {display_topo} 并启动 {'、'.join(req)}"
    t = cur.get("topology")
    if cur.get("state") != "running" or t is None:
        msg = cur.get("message") or "eNSP 中没有运行的设备"
        return {"ready": False, "reason": f"{msg}；{hint}", "ports": {}}
    if topo_key(t.get("path") or t["name"]) != topo_name and topo_key(t["name"]) != topo_name:
        return {"ready": False, "reason": f"{lab_info.get('label', 'eNSP')} 当前拓扑是 {t['name']}，不是本题的 {display_topo}；{hint}",
                "ports": {}}
    by_name = {d["name"]: d for d in t["devices"]}
    missing = [d for d in req if d not in by_name or console_of(by_name[d]) <= 0]
    if missing:
        return {"ready": False, "reason": f"拓扑 {t['name']} 中缺少有 console 的设备：{'、'.join(missing)}",
                "ports": {}}
    not_host = [d for d in s.get("hosts", []) if by_name[d].get("kind") not in (None, "pc")]
    if not_host:
        return {"ready": False, "ports": {},
                "reason": f"拓扑 {t['name']} 中的 {'、'.join(not_host)} 不是终端主机（PC），与本题不符"}
    # eNSP Pro 启动看门狗（spec §10.5）：接口已报运行中、但设备系统还在初始化（改配置会报 busy），或启动失败
    init = [d for d in req if by_name[d].get("status") == "initializing"]
    if init:
        return {"ready": False, "ports": {}, "initializing": init,
                "reason": f"设备 {'、'.join(init)} 系统初始化中（initializing：刚启动的设备改配置会报 busy），"
                          f"请稍候再开始（通常 1~2 分钟）"}
    failed = [d for d in req if by_name[d].get("status") == "start_failed"]
    if failed:
        errs = cur.get("start_errors") or {}
        detail = "；".join(f"{d}：{errs.get(by_name[d].get('id'), '')}".rstrip("：") for d in failed)
        return {"ready": False, "ports": {}, "reason": f"设备启动失败（{detail}）。请在拓扑图上对它再点一次「启动」"}
    stopped = [d for d in req if by_name[d].get("status") != "online"]
    if stopped:
        where = "在拓扑图上点设备「启动」" if lab_info.get("power") else f"在 {lab_info.get('label', 'eNSP')} 中启动"
        return {"ready": False, "reason": f"请{where} {'、'.join(stopped)}（本题需要 {'、'.join(req)}）",
                "ports": {}}
    return {"ready": True, "reason": "", "ports": {d: console_of(by_name[d]) for d in req}}


def public(s: dict[str, Any]) -> dict[str, Any]:
    """给前端的场景信息：不含 checks 命令、inject / restore / solution、提示内容。"""
    return {
        "id": s["id"], "title": s["title"], "level": s.get("level", ""),
        "knowledge_tags": s.get("knowledge_tags", []), "topology": s["topology"],
        "required_devices": s["required_devices"], "accessible_devices": s["accessible_devices"],
        "symptom": s["symptom"], "checks_total": len(s["checks"]), "hints_total": len(s["hints"]),
        "platform": s.get("platform", "vrp5"),
        "draft": bool(s.get("draft", False)),        # 未真机验证的草稿题（缺省不列出，config.SHOW_DRAFTS）
        # 课程页（面向学员，加载时已查过不泄露答案）
        **{k: s[k] for k in COURSE_FIELDS if k in s},
    }
