"""配置级快速复位（spec §10、§14 第 1 项，里程碑 4）：把设备整机配置还原到开练前的快照。

eNSP 的 VRP（AR1220 V200R003、S5700）没有整份替换 running-config 的命令（2026-10-04 实测：AR 上没有
configuration replace / rollback configuration / commit / load；S5700 只有 clear configuration interface，
只能清单个接口）。所以采用「快照差异回滚」：

1. 开练前 snapshot(port)：隐藏的 check 事务执行 display current-configuration，存原文。
2. 结束时 restore_to(port, 快照)：再抓一次当前配置，按 VRP 配置的层级结构（列 0 的 `#` 分段、视图块、
   缩进行）与快照比对，生成撤销命令，在对应视图里执行；执行后重新抓取、重新比对，直到一致或轮数用完
   （每轮都从新抓的配置重新计算，不沿用上一轮的结论）。仍不一致的行原样列出，交给调用方提示「需要在
   eNSP 中重载拓扑」（spec §10 兜底），不当作成功。

差异与撤销规则（纯函数，tests/test_config_reset.py 用真机抓到的配置做样例）：
- 解析：列 0 的 `#` 是段分隔（缩进的 ` #` 是 BGP 等块内分隔，忽略）；每段第一行的缩进是这段的基准，
  比基准缩进更深的行是上一行的子行（AR 上 ` snmp-agent ...` 这类顶层行本身带 1 个空格）。
  `[V200R003C00]` 版本行、`return`、命令回显、提示符、异步日志行不参与比对。行内连续空白压成一个，
  去掉行尾空格（AR 的 `aaa `、`ip address ... ` 带行尾空格）。
- 删除先于添加；删除分两段：先删两边都有的块里多出来的子行（倒序：先删接口上的 traffic-filter、
  port default vlan，再删 ACL、VLAN 本身；先删 sub 地址再删主地址），再删多出来的顶层行 / 块（倒序）。
  多出来的块只 undo 块头（undo interface LoopBack0 / undo ospf 1 / undo acl number 3000），不再进去逐条删。
- 添加按快照里的顺序：缺少的块整块补回（进入块头即创建；每个子块各自一组、沿完整路径进入，从不发 quit），
  两边都有的块补回缺少的子行。
- 每组命令从用户视图开始：system-view + 逐级进入视图，进入视图的命令报错就跳过整组（hub 的 groups），
  避免 undo 落到别的视图里。[Y/N] 确认自动回答 y（undo ospf 1 会问）。
- undo 写法：以 `undo ` 开头的行去掉 undo；description / port link-type / port default vlan /
  stp mode / shutdown 等用规则表；其余先试 `undo <整行>`，失败的下一轮再试去掉末尾 1~2 个参数（最多
  去 2 个，不会退化成 `undo ip address` 这种会删掉全部地址的写法；ip address 行不截断）。
- `vlan batch`：按 VLAN 集合比较，只 undo 多出来的、补回缺少的（整行 undo 会把快照里本来就有的 VLAN
  也删掉）。`vlan N` 块（带 description 等）：N 仍在快照的 VLAN 集合里时只删子行，不删 VLAN。
- sysname：单独处理，放在最后一组，一条 `sysname <快照里的名字>`（AR 的快照不显示 sysname，即默认
  Huawei；S5700 显式显示 sysname Huawei）。hub 跟踪事务中的改名（提示符变化）。
- 排除（不比对、不复位，单独列出）：user-interface 块（console / vty 的认证方式等，改错会把学员和系统
  一起锁在 console 外，场景校验也禁止碰它）；aaa 里的 local-user 行（密码以不可逆密文显示，恢复需要
  原文；且不影响网络排障题）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from console_hub import LOG_RE, PROMPT_RE, clean

log = logging.getLogger("ainet.reset")

DEFAULT_SYSNAME = "Huawei"
MAX_ROUNDS = 4
SNAPSHOT_CMD = "display current-configuration"

_VERSION_RE = re.compile(r"^\[V\d+R\d+\w*\]$")


# ---- 解析 ----

@dataclass
class Node:
    line: str                                   # 规范化后的行（压缩空白、去缩进与行尾空格）
    children: list["Node"] = field(default_factory=list)

    def key(self) -> str:
        return self.line


def norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def parse(text: str) -> list[Node]:
    """display current-configuration 原文 -> 顶层节点列表。"""
    roots: list[Node] = []
    stack: list[tuple[int, Node]] = []
    base: int | None = None
    for raw in clean(text).split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if line.startswith("#"):
            stack, base = [], None          # 列 0 的 #：新的一段
            continue
        if line.startswith("!"):
            continue                        # YunShan 的注释行（!Software Version、!Last configuration was updated at …）
        if s == "#" or s == SNAPSHOT_CMD or _VERSION_RE.match(s) or LOG_RE.match(s) or PROMPT_RE.match(s):
            continue
        if line == "return":
            break
        indent = len(line) - len(line.lstrip(" "))
        if base is None:
            base = indent
        indent = max(indent, base)
        node = Node(norm_line(s))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((indent, node))
    return roots


def render(nodes: list[Node], depth: int = 0) -> list[str]:
    out: list[str] = []
    for n in nodes:
        out.append(" " * depth + n.line)
        out += render(n.children, depth + 1)
    return out


# ---- 分类 ----

def _excluded_top(n: Node) -> bool:
    return n.line.startswith("user-interface ")


def _excluded_child(parent: str, n: Node) -> bool:
    return parent == "aaa" and n.line.startswith("local-user ")


def sysname_of(nodes: list[Node]) -> str:
    for n in nodes:
        if n.line.startswith("sysname "):
            return n.line.split(" ", 1)[1]
    return DEFAULT_SYSNAME


def _vlan_ids(spec: str) -> set[int]:
    """`10 20 30 to 35` -> {10, 20, 30..35}"""
    toks = spec.split()
    out: set[int] = set()
    i = 0
    while i < len(toks):
        if not toks[i].isdigit():
            i += 1
            continue
        a = int(toks[i])
        if i + 2 < len(toks) and toks[i + 1] == "to" and toks[i + 2].isdigit():
            out.update(range(a, int(toks[i + 2]) + 1))
            i += 3
        else:
            out.add(a)
            i += 1
    return out


def vlan_spec(ids: set[int]) -> str:
    """{10, 20, 30..35} -> `10 20 30 to 35`（连续 3 个及以上用 to）。"""
    s = sorted(ids)
    parts: list[str] = []
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{s[i]} to {s[j]}")
        else:
            parts.extend(str(x) for x in s[i:j + 1])
        i = j + 1
    return " ".join(parts)


def vlans_of(nodes: list[Node]) -> set[int]:
    out: set[int] = set()
    for n in nodes:
        if n.line.startswith("vlan batch "):
            out |= _vlan_ids(n.line[len("vlan batch "):])
    return out


def _special_top(n: Node) -> bool:
    return n.line.startswith(("sysname ", "vlan batch "))


def _vlan_block_id(line: str) -> int | None:
    m = re.match(r"^vlan (\d+)$", line)
    return int(m.group(1)) if m else None


# ---- undo 写法 ----

# (行的正则, undo 命令模板)；模板里的 {0} {1} 是正则分组
_UNDO_RULES: tuple[tuple[str, str], ...] = (
    (r"^description\b", "undo description"),
    (r"^port link-type\b", "undo port link-type"),
    (r"^port default vlan\b", "undo port default vlan"),
    (r"^shutdown$", "undo shutdown"),
    (r"^stp mode\b", "undo stp mode"),
    # 实测 AR1220：undo traffic-filter inbound acl 3000 报 Too many parameters，只能带方向
    (r"^traffic-filter (inbound|outbound)\b", "undo traffic-filter {0}"),
    # 实测：undo rule 10 deny icmp 报 Too many parameters，按编号删
    (r"^(rule \d+)\b", "undo {0}"),
    (r"^(ospf \d+)\b", "undo {0}"),
    (r"^(rip \d+)\b", "undo {0}"),
    (r"^(isis \d+)\b", "undo {0}"),
    (r"^(bgp \d+)\b", "undo {0}"),
    (r"^(interface \S+)", "undo {0}"),
    (r"^(acl (?:number|name) \S+)", "undo {0}"),
    (r"^(vlan \d+)$", "undo {0}"),
    (r"^(ip pool \S+)", "undo {0}"),
    (r"^(area \S+)", "undo {0}"),
)
_NO_TRUNCATE = re.compile(r"^(ip address|ipv6 address)\b")
MAX_TRUNCATE = 2


def undo_candidates(line: str) -> list[str]:
    """撤销一行配置的候选命令，按尝试顺序。"""
    if line.startswith("undo "):
        return [line[5:]]
    for pat, tpl in _UNDO_RULES:
        m = re.match(pat, line)
        if m:
            return [tpl.format(*m.groups())]
    cands = ["undo " + line]
    if not _NO_TRUNCATE.match(line):
        toks = line.split()
        for k in range(1, MAX_TRUNCATE + 1):
            if len(toks) - k >= 2:
                cands.append("undo " + " ".join(toks[:-k]))
    return cands


# ---- 计划 ----

@dataclass
class Op:
    path: tuple[str, ...]        # 进入的视图（块头），不含 system-view
    kind: str                    # remove / add / sysname / vlan-
    line: str                    # 原配置行（用来记尝试次数、报告）
    cmds: list[str]              # 本轮要执行的命令（add 整块时是块头 + 子行）

    @property
    def key(self) -> tuple[str, tuple[str, ...], str]:
        return (self.kind, self.path, self.line)


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    exhausted: list[Op] = field(default_factory=list)   # 候选写法都试过了仍然多出来的
    excluded: list[str] = field(default_factory=list)   # 排除类配置的差异（只报告）

    @property
    def empty(self) -> bool:
        return not self.ops and not self.exhausted

    def groups(self) -> list[tuple[list[str], int]]:
        """按视图路径合并成 hub 的 groups：[(['system-view', *path, *cmds], guard), ...]。相邻同路径合并。"""
        out: list[tuple[list[str], int]] = []
        last: tuple[str, ...] | None = None
        for op in self.ops:
            if op.kind == "add-block":
                # 整块补回：块头本身就是进入视图的命令，guard 包含它
                out.append((["system-view", *op.path, *op.cmds], 1 + len(op.path) + 1))
                last = None
                continue
            if last == op.path and out:
                out[-1][0].extend(op.cmds)
            else:
                out.append((["system-view", *op.path, *op.cmds], 1 + len(op.path)))
                last = op.path
        return out


def make_plan(target: list[Node], current: list[Node], attempts: dict | None = None) -> Plan:
    """target（快照）与 current（当前）-> 本轮的撤销 / 补回操作。attempts 记录每个删除操作已试过几次。"""
    attempts = {} if attempts is None else attempts
    plan = Plan()
    removes_child: list[Op] = []
    removes_top: list[Op] = []
    adds: list[Op] = []
    tgt_vlans, cur_vlans = vlans_of(target), vlans_of(current)

    def remove(path: tuple[str, ...], line: str, bucket: list[Op], kind: str = "remove") -> None:
        cands = undo_candidates(line)
        op = Op(path, kind, line, [])
        n = attempts.get(op.key, 0)
        if n >= len(cands):
            plan.exhausted.append(op)
            return
        op.cmds = [cands[n]]
        bucket.append(op)

    def walk_remove_children(path: tuple[str, ...], children: list[Node], bucket: list[Op]) -> None:
        """一个要删掉的块：倒序删它的子行（子块同样先删子行再删块头）。"""
        for c in reversed(children):
            if c.children:
                walk_remove_children(path + (c.line,), c.children, bucket)
                remove(path, c.line, bucket, kind="remove-after")
            else:
                remove(path, c.line, bucket)

    def walk(path: tuple[str, ...], tgt: list[Node], cur: list[Node], top: bool) -> None:
        parent = path[-1] if path else ""
        t_map = {n.key(): n for n in tgt}
        c_map = {n.key(): n for n in cur}
        # 删除：当前多出来的（倒序）
        for n in reversed(cur):
            if top and _excluded_top(n) or not top and _excluded_child(parent, n):
                if n.key() not in t_map or render(n.children) != render(t_map[n.key()].children):
                    plan.excluded.append(f"{' / '.join(path + (n.line,))}")
                continue
            if top and _special_top(n):
                continue
            if n.key() in t_map:
                continue
            vid = _vlan_block_id(n.line) if top else None
            if vid is not None and vid in tgt_vlans:
                walk(path + (n.line,), [], n.children, False)   # VLAN 本身保留，只删子行
                continue
            if vid is not None:
                continue                                        # 随 undo vlan batch 一起删
            bucket = removes_top if top else removes_child
            hdr = Op(path, "remove", n.line, [])
            if n.children and (not top or attempts.get(hdr.key, 0) > 0):
                # 块内还有子行时有的块头删不掉（实测 undo area 0.0.0.1：Please delete the network
                # command(s) of the area first）。嵌套块一律先删子行再删块头；顶层块先直接 undo 块头，
                # 失败了下一轮再走这条路
                walk_remove_children(path + (n.line,), n.children, bucket)
                remove(path, n.line, bucket, kind="remove-after")
            else:
                remove(path, n.line, bucket)
        # 两边都有的块：递归（子块的删除归入子行删除）
        for n in reversed(cur):
            if n.key() in t_map and not (top and (_excluded_top(n) or _special_top(n))):
                tn = t_map[n.key()]
                if n.children or tn.children:
                    walk(path + (n.line,), tn.children, n.children, False)
        # 添加：快照里有、当前没有的（顺序）
        for n in tgt:
            if top and _excluded_top(n) or not top and _excluded_child(parent, n):
                if n.key() not in c_map:
                    plan.excluded.append(f"{' / '.join(path + (n.line,))}")
                continue
            if top and _special_top(n):
                continue
            if n.key() in c_map:
                continue
            if n.children:
                add_block(path, n, adds)
            else:
                adds.append(Op(path, "add", n.line, [n.line]))

    walk((), target, current, True)

    # VLAN：按集合比较
    extra, missing = cur_vlans - tgt_vlans, tgt_vlans - cur_vlans
    vlan_ops_rm: list[Op] = []
    if extra:
        op = Op((), "vlan-", vlan_spec(extra), [f"undo vlan batch {vlan_spec(extra)}"])
        if attempts.get(op.key, 0) >= 2:
            plan.exhausted.append(op)
        else:
            vlan_ops_rm.append(op)
    vlan_ops_add = [Op((), "vlan+", vlan_spec(missing), [f"vlan batch {vlan_spec(missing)}"])] if missing else []

    plan.ops = removes_child + removes_top + vlan_ops_rm + vlan_ops_add + adds
    t_name, c_name = sysname_of(target), sysname_of(current)
    if t_name != c_name:
        plan.ops.append(Op((), "sysname", t_name, [f"sysname {t_name}"]))
    return plan


def add_block(path: tuple[str, ...], node: Node, out: list[Op]) -> None:
    """整块补回：块头 + 它的单行子项一组，每个子块各自一组（从用户视图沿完整路径进入）。

    不用 quit 逐级退出：某个块头进不去时，后面的 quit 会多退一级，退到用户视图再 quit 就登出了 console。
    """
    leaves = [c.line for c in node.children if not c.children]
    out.append(Op(path, "add-block", node.line, [node.line] + leaves))
    for c in node.children:
        if c.children:
            add_block(path + (node.line,), c, out)


def note_attempts(plan: Plan, attempts: dict) -> None:
    for op in plan.ops:
        attempts[op.key] = attempts.get(op.key, 0) + 1


def differences(target: list[Node], current: list[Node]) -> list[str]:
    """人能看懂的差异清单（不含排除类）：`多出：视图 / 行`、`缺少：视图 / 行`。"""
    out: list[str] = []

    def skip(n: Node, parent: str, top: bool) -> bool:
        if top:
            return _excluded_top(n) or _special_top(n)
        return _excluded_child(parent, n)

    def walk(path: tuple[str, ...], tgt: list[Node], cur: list[Node], top: bool) -> None:
        parent = path[-1] if path else ""
        t_map = {n.key(): n for n in tgt}
        c_map = {n.key(): n for n in cur}
        for n in cur:
            if skip(n, parent, top):
                continue
            if n.key() not in t_map:
                out.append("多出：" + " / ".join(path + (n.line,)))
            elif n.children or t_map[n.key()].children:
                walk(path + (n.line,), t_map[n.key()].children, n.children, False)
        for n in tgt:
            if skip(n, parent, top):
                continue
            if n.key() not in c_map:
                out.append("缺少：" + " / ".join(path + (n.line,)))

    walk((), target, current, True)
    t_name, c_name = sysname_of(target), sysname_of(current)
    if t_name != c_name:
        out.append(f"设备名：当前 {c_name}，应为 {t_name}")
    tv, cv = vlans_of(target), vlans_of(current)
    if cv - tv:
        out.append(f"多出：vlan batch {vlan_spec(cv - tv)}")
    if tv - cv:
        out.append(f"缺少：vlan batch {vlan_spec(tv - cv)}")
    return out


def same(target_text: str, current_text: str) -> bool:
    return not differences(parse(target_text), parse(current_text))


# ---- 执行（经 ConsoleHub 隐藏事务） ----

@dataclass
class ResetResult:
    status: str                       # ok / dirty（仍有差异）/ busy / error
    reason: str = ""
    leftovers: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    rounds: int = 0
    commands: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "leftovers": self.leftovers,
                "excluded": self.excluded, "rounds": self.rounds, "commands": self.commands}


class ConfigReset:
    """snapshot(port) / restore_to(port, 快照)：都走 hub 的隐藏事务，学员终端看不到。

    busy（学员正在输入、停在分页 / 系统视图等）时按 busy_retry 重试到 deadline，仍不行返回 busy。
    """

    def __init__(self, hub: Any, *, deadline: float = 10.0, busy_retry: float = 2.0,
                 max_rounds: int = MAX_ROUNDS, cmd_timeout: float = 30.0) -> None:
        self.hub = hub
        self.deadline = deadline
        self.busy_retry = busy_retry
        self.max_rounds = max_rounds
        self.cmd_timeout = cmd_timeout

    async def _retry(self, fn):
        loop = asyncio.get_running_loop()
        end = loop.time() + self.deadline
        while True:
            r = await fn()
            if r.status != "busy" or loop.time() >= end:
                return r
            await asyncio.sleep(min(self.busy_retry, max(0.05, end - loop.time())))

    async def capture(self, port: int):
        """返回 HiddenResult；ok 时 outputs[0] 为配置原文。"""
        return await self._retry(lambda: self.hub.run_hidden(port, [SNAPSHOT_CMD], kind="check",
                                                             cmd_timeout=self.cmd_timeout))

    async def snapshot(self, port: int) -> str:
        """抓取整机配置；失败抛 ResetError（status=busy / error）。"""
        r = await self.capture(port)
        if not r.ok:
            raise ResetError(r.status, r.reason)
        text = r.outputs[0] if r.outputs else ""
        if not parse(text):
            raise ResetError("error", "抓到的配置为空")
        return text

    async def restore_to(self, port: int, snapshot: str) -> ResetResult:
        target = parse(snapshot)
        attempts: dict = {}
        res = ResetResult("ok")
        for rnd in range(self.max_rounds + 1):
            r = await self.capture(port)
            if not r.ok:
                return ResetResult(r.status, f"抓取配置失败：{r.reason}", rounds=rnd,
                                   commands=res.commands, warnings=res.warnings)
            current = parse(r.outputs[0] if r.outputs else "")
            plan = make_plan(target, current, attempts)
            res.excluded = plan.excluded
            if plan.empty:
                res.status, res.rounds = "ok", rnd
                return res
            if not plan.ops or rnd == self.max_rounds:
                res.status, res.rounds = "dirty", rnd
                res.leftovers = differences(target, current)
                return res
            note_attempts(plan, attempts)
            groups = plan.groups()
            log.info("复位 console %s 第 %d 轮：%s", port, rnd + 1, " ; ".join(" > ".join(g[0]) for g in groups))
            res.commands += sum(len(g[0]) for g in groups)
            rr = await self._retry(lambda: self.hub.run_hidden(port, [], kind="config", groups=groups,
                                                               confirm=True, cmd_timeout=self.cmd_timeout))
            res.warnings += rr.warnings
            if rr.status != "ok":
                return ResetResult(rr.status, f"执行复位命令失败：{rr.reason}", rounds=rnd + 1,
                                   commands=res.commands, warnings=res.warnings)
        return res


class ResetError(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
