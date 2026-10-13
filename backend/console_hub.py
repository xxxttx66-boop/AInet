"""ConsoleHub：每个 console 端口只保持一条设备连接，多个订阅者共享（spec §5）。

背景（spec §3 实测）：
- 同一 console 端口的多个 TCP 连接共享输入输出，像共用一根串口：系统另开连接发命令，学员终端
  会看到；学员敲到一半时字符还会混在一起。
- console 刚断开的会话，几分钟内重连可能收不到任何输出；高频连接/断开疑似诱发 eNSP_Client 崩溃。
所以：每个端口懒加载一个 DeviceConnector 并长期持有；浏览器标签页（WebSocket）只是订阅者，关标签只
退订、不断开设备连接；所有订阅者退出并空闲超过 idle_close 秒后才关闭；重连有最小间隔。

隐藏事务 run_hidden(port, commands, kind)：系统判定（kind="check"）与注入 / 还原（kind="config"）用。
1. 拿到该端口的互斥锁；
2. 前提检查：学员当前输入行为空（hub 跟踪自上次回车以来敲过的可见字符，处理退格；用过方向键 /
   Tab 补全时视为非空），且设备最后一行是提示符（不是 [Y/N]、Password:、---- More ---- 等）；
   config 事务还要求处于用户视图 <...>。不满足就返回 status="busy"，调用方稍后重试；
3. 暂停向订阅者转发设备输出；事务期间学员的输入**暂存**，事务结束后按原顺序写回设备（像串口的
   键盘预输入）。选暂存而非丢弃：事务通常 1~5 秒，学员这时敲下的字符被吞掉会以为键盘失灵；暂存后
   事务结束时设备照常回显，体验与设备稍慢一致；
4. 逐条发送命令，读取到提示符为止，遇到 ---- More ---- 发空格并在结果里去掉分页符；每条命令有超时；
5. 捕获期间设备吐出的异步日志行（形如 `Oct  4 2026 11:02:03-08:00 AR0 %%01...`）在事务结束后照常
   转发给学员（并像 VRP 一样重印提示符），不吞掉；
6. 恢复转发。事务期间的回显与输出不进订阅者、也不进新订阅者的回放缓冲，学员屏幕上看不到判定 /
   注入命令。config 事务结束时确保回到用户视图（必要时补发 return）；check 事务要求结束时的提示符与
   开始时完全一致（只用 display / ping 这类任何视图都可执行的命令，不切换视图）。

配置复位（config_reset，里程碑 4）用到的扩展：confirm=True 时命令停在 [Y/N] 上自动回答 y（只回答新出现的
确认）；groups 按组执行（每组从用户视图进入视图，进视图失败跳过整组）；sysname X / undo sysname 改提示符时跟踪新主机名，
事务结束后把新提示符重印给学员。S5700 的 DS/4/... 日志不带 %%、打印后不重印提示符，也可能插在分页符之后：settled_line /
ends_with_more 忽略末尾的日志行。

结束练习（强制收回终端）：freeze(ports) 之后这些端口上学员的输入一律丢弃；reclaim(port) 不管学员停在分页、[Y/N]、
系统视图还是敲了半行，都把设备带回用户视图的空提示符，之后的还原 / 复位事务不会因学员终端状态返回 busy。
只在结束复位时用；练习中的判定 / 注入照旧按 busy 语义等学员。

命令记录：学员每次回车提交的命令行（取设备回显的那一行，Tab 补全 / 历史命令也能拿到实际文本）连同端口
交给监听者（练习管理器），用于命令回放和过程评分；学员每次按键也会通知监听者（自动判定的防抖）。
命令之后学员可见的设备输出（到下一个提示符为止）整理成摘要（去分页符 / 控制字符、截断）以 "output" 事件交出；
不在采集中时看到的异步日志行以 "log" 事件交出（副作用评分用）。隐藏事务的输出不经过可见通道，天然不会被记录。
与复位 / 隐藏事务的衔接：进入隐藏之前先结束学员上一条命令的采集（设备已停在提示符上），所以事务结束时补发的
日志、sysname 改名后重印的提示符都不会算进学员命令的输出；自动 [Y/N]、按组执行的命令只在隐藏通道里，不产生
command 事件。S5700 的 DATASYNC 配置同步日志（NOISE_LOG_RE）不作为 "log" 事件、不进输出摘要；学员命令落在这类
日志之后的新行上（交换机不重印提示符）时仍按提示符之后的命令记录（input_after_prompt）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("ainet.hub")

# ---- 文本处理（纯函数，便于单测） ----

MORE = "---- More ----"
# 分页符本身 + VRP 用来擦掉它的 ESC[nD 空格 ESC[nD
_MORE_RE = re.compile(r"[ ]*---- More ----(?:\x1b\[\d*D[ ]*\x1b\[\d*D)?")
# 日志行末尾的孤立分页擦除序列（见 strip_more）
_LOG_ERASE_RE = re.compile(r"(?m)^(\r?[A-Z][a-z]{2} +\d{1,2} \d{4} \d{2}:\d{2}:\d{2}[^\n\x1b]*)"
                           r"\x1b\[(\d+)D[ ]*\x1b\[\2D")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 提示符：整行只有 <名字> 或 [名字...]（[V200R003C00] 这类配置行由「行尾无换行 + 主机名」进一步排除）
PROMPT_RE = re.compile(r"^([<\[])([^\s<>\[\]]+)([>\]])\s*$")
# eNSP Pro 的 PC（PC-CLIENT 等终端设备）提示符 `PC>`：没有视图，只能执行 ping / ipconfig 一类命令（2026-10-06 实测）
PC_PROMPT_RE = re.compile(r"^[A-Za-z][\w-]{0,31}>\s*$")
# 提示符行 + 其后学员输入的内容
_PROMPT_LINE_RE = re.compile(r"^([<\[][^\s<>\[\]]+[>\]]|[A-Za-z][\w-]{0,31}>)(.*)$")
# 异步日志行：Oct  4 2026 11:02:03-08:00 AR0 %%01IFNET/4/LINK_STATE(l)[0]:...
# S5700 还有不带 %% 的：Oct  4 2026 15:20:16-08:00 Huawei DS/4/DATASYNC_CFGCHANGE:OID ...（2026-10-04 实测，
# 改配置后约 7 秒出现；交换机打印日志后不重印提示符）
# YunShan（AR6700，2026-10-06 eNSP Pro 实测）还有不带时间戳的异步提示：删 ACL / traffic-filter 之后数秒，打印在后面某条
# 命令的提示符之后、且不重印提示符：`Error: Failed to apply the traffic-filter. To check the cause, run the display
# traffic-filter applied-record command. (slot=0)`。不当作那条命令的错误，按日志处理（否则 hub 一直等不到提示符而超时）。
ASYNC_MSG = r"Error: Failed to apply the traffic-filter\b"
LOG_RE = re.compile(r"^(?:[A-Z][a-z]{2} +\d{1,2} \d{4} \d{2}:\d{2}:\d{2}(?:[-+]\d{2}:\d{2})? \S+ "
                    r"(?:%%\d+|[A-Z][A-Z0-9_]*/\d+/[A-Z0-9_]+)|" + ASYNC_MSG + ")")
# 配置同步通知（S5700 每次改配置后约 7 秒打印 DS/4/DATASYNC_CFGCHANGE ... configurations changed.）：只说明「配置
# 变了」，不是接口 / 协议状态变化。hub 照样把它当日志处理（提示符、分页判断），但不作为 "log" 事件交出、不进学员
# 命令的输出摘要——它总在改配置之后数秒才到，会被错挂到之后的命令上，也不是副作用（评分侧同样忽略，见 grading）。
NOISE_LOG_RE = re.compile(r"\bDS/\d+/DATASYNC_[A-Z0-9_]*|^" + ASYNC_MSG)
_SYSNAME_RE = re.compile(r"^sysname\s+(\S+)\s*$")
DEFAULT_SYSNAME = "Huawei"
# 登录提示（console 空闲超时被登出、学员在用户视图 quit 之后）：hub 不能在这里补回车
LOGIN_PROMPT_RE = re.compile(r"(Password|Username|login):\s*$", re.I)
# 确认提示：VRP5 `[Y/N]:`；YunShan 还有 `[Y(yes)/N(no)/C(cancel)]:` 这类
CONFIRM_RE = re.compile(r"\[\s*Y(?:\(yes\))?\s*/\s*N(?:\(no\))?(?:\s*/\s*C(?:\(cancel\))?)?\s*\]\s*:?\s*$", re.I)


CONFIRM_ANY_RE = re.compile(r"\[\s*Y(?:\(yes\))?\s*/\s*N(?:\(no\))?(?:\s*/\s*C(?:\(cancel\))?)?\s*\]", re.I)


def is_prompt(line: str) -> re.Match[str] | None:
    """整行是提示符：VRP / YunShan 的 <名字>、[名字...]，或 eNSP Pro 终端设备（PC）的 `PC>`。"""
    return PROMPT_RE.match(line) or PC_PROMPT_RE.match(line)


def is_confirm(line: str) -> bool:
    """最后一行停在 [Y/N] 一类的确认提示上。"""
    return CONFIRM_RE.search(line) is not None


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def strip_more(text: str) -> str:
    """去分页符。分页符之后插进日志时（S5700，2026-10-04 实测），设备在日志行末尾、不换行直接发擦除序列
    ESC[42D 空格 ESC[42D 再接着输出下一行配置：`...records is 4095.ESC[42D    ESC[42Dinterface GigabitEthernet0/0/24`。
    不处理的话这一整行会被当成日志拿掉，配置快照丢一行。只在日志行上把这段孤立的擦除序列换成换行。"""
    return _LOG_ERASE_RE.sub(r"\1\n", _MORE_RE.sub("", text))


def clean(text: str) -> str:
    """去分页符、ANSI 序列和 \\r。"""
    return strip_ansi(strip_more(text)).replace("\r", "")


def render_line(raw: str) -> str:
    """按终端语义渲染一行：\\b / ESC[nD 左移，ESC[nC 右移，ESC[K 清到行尾，其余字符覆盖写入。"""
    buf: list[str] = []
    pos = 0
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\x1b":
            m = _ANSI_RE.match(raw, i)
            if m:
                seq = m.group(0)
                n = int(seq[2:-1]) if seq[2:-1].isdigit() else 1
                if seq.endswith("D"):
                    pos = max(0, pos - n)
                elif seq.endswith("C"):
                    pos += n
                    buf.extend(" " * max(0, pos - len(buf)))
                elif seq.endswith("K"):
                    del buf[pos:]
                i = m.end()
                continue
        elif c == "\b":
            pos = max(0, pos - 1)
        elif c == "\r":
            pos = 0
        elif c >= " ":
            if pos < len(buf):
                buf[pos] = c
            else:
                buf.append(c)
            pos += 1
        i += 1
    return "".join(buf).rstrip()


def last_line(text: str) -> str:
    """设备输出的最后一行（渲染后、去分页符）。"""
    t = strip_more(text)
    return render_line(t.rsplit("\n", 1)[-1])


def is_log(line: str) -> bool:
    return LOG_RE.match(line.strip()) is not None


def is_noise_log(line: str) -> bool:
    """配置同步之类的通知日志（见 NOISE_LOG_RE）：不算学员操作的输出，也不算副作用。"""
    return is_log(line) and NOISE_LOG_RE.search(line) is not None


def settled_line(text: str) -> str:
    """最后一行；若末尾是异步日志（及其后的空行），取日志之前的那一行。

    S5700 在提示符之后打印日志时不重印提示符，设备实际仍停在日志前的提示符上。
    只有末尾确实出现过日志行时才往前找，单纯以换行结尾（输出还在继续）仍返回空行。
    """
    lines = strip_more(text).split("\n")
    k = len(lines)
    saw_log = False
    while k > 1:
        cur = render_line(lines[k - 1]).strip()
        if cur and LOG_RE.match(cur):
            saw_log = True
        elif cur:
            break
        k -= 1
    return render_line(lines[k - 1]) if saw_log else render_line(lines[-1])


def input_after_prompt(text: str) -> str | None:
    """当前行在提示符之后时，返回提示符后面（学员已输入、设备已回显）的内容；否则 None。

    S5700 在提示符之后打印日志（如 DATASYNC_CFGCHANGE）不重印提示符，学员接着敲的字符落在日志后的新行上：
    当前行之前隔着的只有日志行 / 空行、再往前是单独的提示符时，也算在提示符之后。
    """
    lines = strip_more(text).split("\n")
    cur = render_line(lines[-1])
    m = _PROMPT_LINE_RE.match(cur)
    if m is not None:
        return m.group(2)
    # 日志行末尾没有换行时（S5700 改名后实测：`[SW-TEST]` 换行后打印 DATASYNC 日志、不换行），学员敲的字符直接接在
    # 日志后面，回显没法从日志里分出来：返回 ""，调用方改用跟踪的输入
    log_tail = is_log(cur)
    k = len(lines) - 2
    saw_log = log_tail
    while k >= 0:
        prev = render_line(lines[k]).strip()
        if prev and is_log(prev):
            saw_log = True
        elif prev:
            break
        k -= 1
    if saw_log and k >= 0 and is_prompt(render_line(lines[k])):
        return "" if log_tail else cur
    return None


def prompt_of(text: str) -> str | None:
    """输出末尾停在提示符上时返回该提示符，否则 None。"""
    line = last_line(text)
    m = is_prompt(line)
    return m.group(0).strip() if m else None


def ends_with_more(text: str) -> bool:
    """输出停在分页符上。分页符之后还可能插进异步日志（实测 S5700 停在 ---- More ---- 时打印了
    DS/4/DATASYNC_CFGCHANGE），设备仍在等空格，所以忽略末尾的日志行。"""
    t = strip_ansi(text).rstrip(" \r\n")
    while True:
        head, sep, last = t.rpartition("\n")
        if sep and LOG_RE.match(last.strip()):
            t = head.rstrip(" \r\n")
            continue
        return t.endswith(MORE)


def is_subsequence(needle: str, hay: str) -> int:
    """needle 的字符按顺序出现在 hay 中时，返回最后一个字符之后的位置；否则 -1。

    命令回显可能被异步日志打断（VRP 打印日志后会重印提示符和已输入的半截命令），所以不能要求回显连续。
    """
    pos = 0
    for ch in needle:
        pos = hay.find(ch, pos)
        if pos < 0:
            return -1
        pos += 1
    return pos


def split_logs(text: str) -> tuple[str, list[str]]:
    """把异步日志行从（已 clean 的）文本里拿出来。返回 (其余文本, 日志行列表)。"""
    keep, logs = [], []
    for line in text.split("\n"):
        if LOG_RE.match(line.strip()):
            logs.append(line.strip())
        else:
            keep.append(line)
    return "\n".join(keep), logs


_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def summarize_output(raw: str, limit: int = 2000) -> str:
    """学员命令的设备输出摘要：去分页符 / ANSI / 控制字符，去掉首行回显与末尾提示符，过长保留头尾。"""
    lines = [_CTRL_RE.sub("", render_line(x)) for x in strip_more(raw).split("\n")]
    lines = lines[1:]                          # 第一行是命令回显（或回车后的空行）
    lines = [x for x in lines if not is_noise_log(x)]
    while lines and not lines[-1].strip():
        lines.pop()                            # 日志拿掉后留下的空行（S5700 日志后不重印提示符）
    if lines and is_prompt(lines[-1]):
        lines.pop()
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(x.rstrip() for x in lines)).strip("\n")
    if len(text) > limit > 0:
        head = int(limit * 0.7)
        tail = max(0, limit - head)
        text = f"{text[:head]}\n…（省略 {len(text) - head - tail} 字符）…\n{text[-tail:] if tail else ''}"
    return text


class _OutputRecorder:
    """采集学员命令的设备输出（只看学员可见的输出，隐藏事务的回显与输出本来就不经过这里）。

    学员提交命令时 start()；之后可见输出 feed() 进来，输出停在提示符（且不在分页上）时结束；下一条命令提交
    时强制结束上一条。不在采集中时只按行找异步日志（接口 / 邻居 Down 等，供副作用评分）。
    """
    HEAD, TAIL = 12000, 4000

    def __init__(self) -> None:
        self.active = False
        self.head = ""
        self.tail = ""
        self.cut = False
        self.partial = ""                      # 不在采集中时的半行（找日志用）

    def start(self) -> None:
        self.active, self.head, self.tail, self.cut = True, "", "", False

    def feed(self, text: str) -> tuple[bool, list[str]]:
        """返回 (本条命令是否已输出完, 采集之外看到的日志行)。"""
        if not self.active:
            buf = self.partial + text
            *lines, self.partial = buf.split("\n")
            self.partial = self.partial[-2000:]
            return False, [s for s in (render_line(strip_more(x)).strip() for x in lines)
                           if is_log(s) and not is_noise_log(s)]
        if len(self.head) < self.HEAD:
            self.head += text
        else:
            self.cut = True
            self.tail = (self.tail + text)[-self.TAIL:]
        last = self.tail if self.cut else self.head
        # settled_line：S5700 在提示符之后打印日志不重印提示符，设备实际已停在提示符上
        return is_prompt(settled_line(last)) is not None and not ends_with_more(last), []

    def take(self) -> str:
        raw = self.head + ("\n…（输出过长，中间省略）…\n" + self.tail if self.cut else "")
        self.active, self.head, self.tail, self.cut, self.partial = False, "", "", False, ""
        return raw


# ---- 结果与订阅者 ----

@dataclass
class HiddenResult:
    status: str                 # ok / busy（暂不可执行，稍后重试）/ error
    reason: str = ""
    outputs: list[str] = field(default_factory=list)  # 每条命令的输出（已去回显、分页符、日志、提示符）
    warnings: list[str] = field(default_factory=list)  # tolerate_errors 时被跳过的出错命令
    failed: list[str] = field(default_factory=list)    # 报错的命令原文（含 groups 里被跳过的视图命令）
    prompt: str = ""                                    # 事务结束时的提示符（sysname 改名后与开始时不同）

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Subscriber:
    """一个订阅者（一个浏览器终端标签）。设备输出放进 queue，None 表示设备连接已断开。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()


class HubConnectError(Exception):
    """连不上设备 console。"""


# (event, port, data)。event: "input"（学员按键）| "command"（提交的命令行）|
# "output"（上一条命令的输出摘要，紧跟在对应 command 之后、下一条 command 之前）| "log"（采集之外的异步日志行）
Listener = Callable[[str, int, str], Any]


class _Channel:
    def __init__(self, port: int) -> None:
        self.port = port
        self.conn: Any = None
        self.reader: asyncio.Task | None = None
        self.subs: set[Subscriber] = set()
        self.lock = asyncio.Lock()            # 隐藏事务互斥
        self.connect_lock = asyncio.Lock()
        self.hidden = False
        self.capture = ""                     # 事务期间捕获的设备输出
        self.data_event = asyncio.Event()
        self.pending_input = bytearray()      # 事务期间暂存的学员输入
        self.tail = ""                        # 学员可见的最近设备输出（分析提示符 / 当前行用）
        self.replay: deque[bytes] = deque()   # 学员可见的最近输出（新订阅者回放）
        self.replay_size = 0
        self.line: list[str] = []             # 学员自上次回车以来的可见输入
        self.line_dirty = False               # 用过方向键 / Tab：行内容不可知
        self.esc = ""                         # 跨 write 的半截 ESC 序列
        self.last_cr = False
        self.last_attempt = 0.0               # 上次尝试连接的时刻（monotonic）
        self.fresh = False                    # 刚建立连接、还没见到提示符
        self.idle_task: asyncio.Task | None = None
        self.eof = False
        self.logs: list[str] = []             # 事务期间捕获的异步日志行
        self.error = ""
        self.cur_prompt = ""                  # 事务中的用户视图提示符（sysname 改名会变）
        self.rec = _OutputRecorder()          # 学员命令的输出采集

    @property
    def connected(self) -> bool:
        return self.conn is not None and not self.eof


_TAIL_MAX = 4096
_REPLAY_MAX = 8192


class ConsoleHub:
    def __init__(self, factory: Callable[[int], Any], *, idle_close: float = 600.0,
                 min_reconnect: float = 3.0, cmd_timeout: float = 20.0,
                 ready_timeout: float = 6.0, settle: float = 0.15, output_max: int = 2000,
                 default_sysname: str = DEFAULT_SYSNAME) -> None:
        """factory(port) -> 未连接的 DeviceConnector。output_max：命令输出摘要的字符上限。
        default_sysname：undo sysname 之后的设备名（VRP5 为 Huawei，YunShan 为 HUAWEI，由实验后端的平台决定）。"""
        self.factory = factory
        self.default_sysname = default_sysname
        self.output_max = output_max
        self.idle_close = idle_close
        self.min_reconnect = min_reconnect
        self.cmd_timeout = cmd_timeout
        self.ready_timeout = ready_timeout
        self.settle = settle
        self.echo_timeout = 4.0               # 只读命令多久没回显就重发一次（见 _exec）
        self.channels: dict[int, _Channel] = {}
        self.listeners: list[Listener] = []
        self.pinned: set[int] = set()         # 不做空闲关闭的端口（进行中练习的设备）
        self.frozen: set[int] = set()         # 学员输入一律丢弃的端口（练习结束、后台复位期间）
        self.reclaim_timeout = 15.0           # reclaim 收回终端的总时限
        self.connects = 0                     # 建立过的设备连接数（测试 / 诊断）

    # ---- 连接管理 ----

    def _channel(self, port: int) -> _Channel:
        ch = self.channels.get(port)
        if ch is None:
            ch = self.channels[port] = _Channel(port)
        return ch

    async def _ensure_connected(self, ch: _Channel) -> None:
        async with ch.connect_lock:
            if ch.connected:
                return
            await self._drop_conn(ch)
            wait = ch.last_attempt + self.min_reconnect - time.monotonic()
            if ch.last_attempt and wait > 0:
                await asyncio.sleep(wait)     # 重连间隔下限，避免高频连接 / 断开
            ch.last_attempt = time.monotonic()
            conn = self.factory(ch.port)
            try:
                await conn.connect()
            except Exception as exc:
                try:
                    await conn.close()
                except Exception:
                    pass
                raise HubConnectError(repr(exc)) from exc
            self.connects += 1
            ch.conn, ch.eof, ch.fresh = conn, False, True
            ch.tail, ch.line, ch.line_dirty, ch.esc = "", [], False, ""
            ch.replay.clear()
            ch.replay_size = 0
            ch.reader = asyncio.create_task(self._read_loop(ch, conn))
            # 只在新建设备连接时唤醒一次（发 \r 触发提示符）；已有连接上新增订阅者绝不发送，
            # 以免提交别的标签敲了一半的命令
            await conn.send(b"\r")

    async def _drop_conn(self, ch: _Channel) -> None:
        conn, ch.conn = ch.conn, None
        if ch.reader is not None and ch.reader is not asyncio.current_task():
            ch.reader.cancel()
        ch.reader = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    async def _read_loop(self, ch: _Channel, conn: Any) -> None:
        try:
            while True:
                data = await conn.read()
                if not data:
                    break
                self._on_device(ch, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if ch.conn is conn:
            ch.eof = True
            ch.data_event.set()
            for sub in list(ch.subs):
                sub.queue.put_nowait(None)
            ch.subs.clear()
            ch.conn = None
            try:
                await conn.close()
            except Exception:
                pass

    def _on_device(self, ch: _Channel, data: bytes) -> None:
        text = data.decode("utf-8", "replace")
        if ch.hidden:
            ch.capture += text
            ch.data_event.set()
            return
        self._visible(ch, data, text)

    def _visible(self, ch: _Channel, data: bytes, text: str | None = None) -> None:
        text = data.decode("utf-8", "replace") if text is None else text
        ch.tail = (ch.tail + text)[-_TAIL_MAX:]
        ch.replay.append(data)
        ch.replay_size += len(data)
        while ch.replay_size > _REPLAY_MAX and len(ch.replay) > 1:
            ch.replay_size -= len(ch.replay.popleft())
        for sub in ch.subs:
            sub.queue.put_nowait(data)
        ch.data_event.set()
        done, logs = ch.rec.feed(text)
        if done:
            self._finish_output(ch)
        for line in logs:
            self._emit("log", ch.port, line)

    def _finish_output(self, ch: _Channel) -> None:
        if ch.rec.active:
            self._emit("output", ch.port, summarize_output(ch.rec.take(), self.output_max))

    def flush_output(self, port: int) -> None:
        """结束练习前调用：还没回到提示符的那条命令，按已收到的输出结束采集。"""
        ch = self.channels.get(port)
        if ch is not None:
            self._finish_output(ch)

    # ---- 订阅 ----

    async def subscribe(self, port: int) -> Subscriber:
        ch = self._channel(port)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        await self._ensure_connected(ch)
        sub = Subscriber(port)
        # 新订阅者先看到最近的可见输出（当前提示符等），不含隐藏事务的内容
        if ch.replay:
            sub.queue.put_nowait(b"".join(ch.replay))
        ch.subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        ch = self.channels.get(sub.port)
        if ch is None:
            return
        ch.subs.discard(sub)
        self._maybe_idle(ch)

    def subscriber_count(self, port: int) -> int:
        ch = self.channels.get(port)
        return len(ch.subs) if ch else 0

    def drop_subscribers(self, ports: set[int], message: str = "") -> None:
        """断开这些端口上的订阅者（设备连接保留），例如练习开始时不开放的设备。"""
        for port in ports:
            ch = self.channels.get(port)
            if ch is None:
                continue
            for sub in list(ch.subs):
                if message:
                    sub.queue.put_nowait(message.encode())
                sub.queue.put_nowait(None)
            ch.subs.clear()
            self._maybe_idle(ch)

    def notify_all(self, message: str) -> int:
        """给所有打开着的终端标签发一条系统通知（不经设备、不进回放缓冲、不产生命令记录）。返回收到通知的标签数。"""
        n = 0
        for ch in self.channels.values():
            for sub in ch.subs:
                sub.queue.put_nowait(message.encode())
                n += 1
        return n

    async def disconnect(self, port: int, message: str = "") -> None:
        """主动断开设备连接（设备被停止 / 重启时）：通知订阅者，关闭 connector；之后按需重连。"""
        ch = self.channels.get(port)
        if ch is None:
            return
        for sub in list(ch.subs):
            if message:
                sub.queue.put_nowait(message.encode())
            sub.queue.put_nowait(None)
        ch.subs.clear()
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        async with ch.lock:
            await self._drop_conn(ch)
            ch.eof = True

    def reset_replay(self, port: int) -> None:
        """回放缓冲只保留当前行（提示符）。练习开始时调用：之后打开的标签看不到上一次练习的命令和答案。"""
        ch = self.channels.get(port)
        if ch is None:
            return
        cur = ch.tail.rsplit("\n", 1)[-1]
        ch.tail = cur
        ch.replay.clear()
        ch.replay_size = 0
        if cur:
            ch.replay.append(cur.encode())
            ch.replay_size = len(cur.encode())

    def pin(self, ports) -> None:
        """练习进行中的端口：即使没有订阅者也不空闲关闭（避免结束还原时才冷启动重连）。"""
        for port in ports:
            self.pinned.add(port)
            ch = self.channels.get(port)
            if ch is not None and ch.idle_task is not None:
                ch.idle_task.cancel()
                ch.idle_task = None

    def unpin(self, ports) -> None:
        for port in ports:
            self.pinned.discard(port)
            ch = self.channels.get(port)
            if ch is not None:
                self._maybe_idle(ch)

    def freeze(self, ports, message: str = "") -> None:
        """练习结束、后台复位期间：这些端口上学员的输入一律丢弃（不暂存），已打开的标签收到一条 message 通知。"""
        for port in ports:
            if port in self.frozen:
                continue
            self.frozen.add(port)
            ch = self.channels.get(port)
            if ch is None:
                continue
            ch.pending_input.clear()
            for sub in ch.subs if message else ():
                sub.queue.put_nowait(message.encode())

    def unfreeze(self, ports, message: str = "") -> None:
        for port in ports:
            if port not in self.frozen:
                continue
            self.frozen.discard(port)
            ch = self.channels.get(port)
            for sub in (ch.subs if ch is not None and message else ()):
                sub.queue.put_nowait(message.encode())

    def _maybe_idle(self, ch: _Channel) -> None:
        if ch.subs or ch.conn is None or ch.idle_task is not None or ch.port in self.pinned:
            return
        ch.idle_task = asyncio.create_task(self._idle_close(ch))

    async def _idle_close(self, ch: _Channel) -> None:
        try:
            await asyncio.sleep(self.idle_close)
            async with ch.lock:
                if not ch.subs:
                    await self._drop_conn(ch)
        except asyncio.CancelledError:
            pass
        finally:
            if ch.idle_task is asyncio.current_task():
                ch.idle_task = None

    # ---- 学员输入 ----

    async def write(self, sub: Subscriber, data: bytes) -> None:
        ch = self.channels.get(sub.port)
        if ch is None or sub not in ch.subs:
            return
        # 浏览器终端（xterm.js）的 Backspace 发 0x7f（DEL），VRP console 不认（只回响铃 0x07，不删字符），
        # 它的退格是 0x08（Ctrl-H）。在入口统一转换，输入行跟踪与设备行为才能一致（2026-10-04 真机实测）。
        data = data.replace(b"\x7f", b"\x08")
        if sub.port in self.frozen:
            return                          # 练习已结束、正在复位：不让学员与复位抢终端
        if ch.hidden:
            ch.pending_input.extend(data)   # 事务结束后写回
            return
        await self._send_student(ch, data)

    async def _send_student(self, ch: _Channel, data: bytes) -> None:
        if not ch.connected:
            return
        commands = self._track_input(ch, data.decode("utf-8", "ignore"))
        # 先通知命令、开始采集，再发给设备：保证 output 事件一定在对应的 command 之后，且不漏掉输出开头
        for cmd in commands:
            self._finish_output(ch)
            self._emit("command", ch.port, cmd)
            ch.rec.start()
        await ch.conn.send(data)
        self._emit("input", ch.port, "")

    def _track_input(self, ch: _Channel, text: str) -> list[str]:
        """更新学员当前输入行；返回本次回车提交的命令（只记录在提示符后提交的非空行）。"""
        commands: list[str] = []
        in_more = ends_with_more(ch.tail)
        text = ch.esc + text
        ch.esc = ""
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\x1b":
                m = re.match(r"\x1b(\[[0-9;?]*[@-~]|O.|[^\[O])", text[i:])
                if m is None:
                    ch.esc = text[i:]      # 半截序列，等下次拼接
                    break
                ch.line_dirty = True       # 方向键（历史命令 / 光标移动）：行内容不可知
                i += len(m.group(0))
                ch.last_cr = False
                continue
            if c == "\n" and ch.last_cr:
                ch.last_cr = False
                i += 1
                continue
            ch.last_cr = c == "\r"
            if in_more:
                # 分页中：空格 / 回车 / q 由分页器消费，不进命令行
                if c in "\x03":
                    ch.line, ch.line_dirty = [], False
                i += 1
                continue
            if c in "\r\n":
                cmd = self._submitted(ch)
                if cmd:
                    commands.append(cmd)
                ch.line, ch.line_dirty = [], False
            elif c in "\x7f\x08":
                if ch.line:
                    ch.line.pop()
            elif c in "\x03\x15\x1a":       # Ctrl-C / Ctrl-U / Ctrl-Z 清空当前行
                ch.line, ch.line_dirty = [], False
            elif c == "\t":
                ch.line_dirty = True
            elif c >= " ":
                ch.line.append(c)
            i += 1
        return commands

    def _submitted(self, ch: _Channel) -> str:
        """回车时的命令文本：优先取设备回显的当前行（含 Tab 补全 / 历史命令），否则用跟踪的输入。"""
        echoed = input_after_prompt(ch.tail)
        if echoed is None:
            return ""                       # 不在提示符后（如 [Y/N]、Password:），不算命令
        echoed = echoed.strip()
        typed = "".join(ch.line).strip()
        if ch.line_dirty:
            return echoed
        return typed or echoed

    def input_line(self, port: int) -> tuple[str, bool]:
        ch = self.channels.get(port)
        return ("".join(ch.line), ch.line_dirty) if ch else ("", False)

    def add_listener(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self.listeners:
            self.listeners.remove(fn)

    def _emit(self, event: str, port: int, data: str) -> None:
        for fn in list(self.listeners):
            try:
                r = fn(event, port, data)
                if asyncio.iscoroutine(r):
                    asyncio.create_task(r)
            except Exception:
                pass

    # ---- 隐藏事务 ----

    async def check_ready(self, port: int) -> HiddenResult:
        """Verify a usable console prompt before recording an attempt or changing configuration."""
        ch = self._channel(port)
        try:
            await self._ensure_connected(ch)
        except HubConnectError as exc:
            return HiddenResult("error", f"console 未就绪：{exc}")
        async with ch.lock:
            await self._wait_ready(ch)
            result = self._precheck(ch, "check")
            return result if isinstance(result, HiddenResult) else HiddenResult("ok")

    async def _wait_ready(self, ch: _Channel) -> None:
        """新建的连接：等唤醒后的提示符出现（输出静默 settle 秒），最多 ready_timeout 秒。"""
        if not ch.fresh:
            return
        loop = asyncio.get_running_loop()
        end = loop.time() + self.ready_timeout
        while loop.time() < end and ch.connected:
            ch.data_event.clear()
            if prompt_of(ch.tail):
                try:
                    await asyncio.wait_for(ch.data_event.wait(), self.settle * 2)
                    continue                # 还有输出，接着等
                except asyncio.TimeoutError:
                    break
            if ends_with_more(ch.tail) and not ch.subs:
                # 上一个会话断开时停在分页上的残留输出：没人在看，Ctrl-C 结束它
                await ch.conn.send(b"\x03")
            try:
                await asyncio.wait_for(ch.data_event.wait(), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                break
        ch.fresh = False

    async def _nudge_to_prompt(self, ch: _Channel, tries: int = 2, wait: float = 3.0) -> None:
        """设备输出没停在提示符上（没有输出 / 最后一行为空或残留文字）、且学员输入行为空时，补发回车唤醒。

        常见于新建或刚重连的 console：唤醒用的回车没换回提示符（eNSP 刚断开的会话一段时间内可能不回显）。
        学员正在输入、或停在分页 / [Y/N] 等交互上时不碰，交给调用方按「暂不可执行」重试。
        """
        loop = asyncio.get_running_loop()
        for _ in range(tries):
            if "".join(ch.line) or ch.line_dirty or ch.pending_input or not ch.connected:
                return
            if prompt_of(ch.tail) is not None or ends_with_more(ch.tail):
                return
            tail_line = last_line(ch.tail)
            if is_confirm(tail_line) or LOGIN_PROMPT_RE.search(tail_line):
                return              # 登录提示后补回车 = 提交空密码（算一次失败登录），绝不能发
            ch.data_event.clear()
            await ch.conn.send(b"\r")
            end = loop.time() + wait
            while loop.time() < end and prompt_of(ch.tail) is None:
                try:
                    await asyncio.wait_for(ch.data_event.wait(), max(0.05, end - loop.time()))
                except asyncio.TimeoutError:
                    break
                ch.data_event.clear()

    def _precheck(self, ch: _Channel, kind: str) -> HiddenResult | str:
        line, dirty = "".join(ch.line), ch.line_dirty
        if line or dirty or ch.pending_input:
            return HiddenResult("busy", "学员正在输入命令")
        if not ch.tail:
            return HiddenResult("busy", "设备无响应（console 没有输出）")
        if ends_with_more(ch.tail):
            return HiddenResult("busy", "设备终端停在分页（---- More ----）上")
        p = prompt_of(ch.tail)
        if p is None:
            shown = last_line(ch.tail)[-40:]
            return HiddenResult("busy", f"设备终端不在命令提示符（当前：{shown!r}）")
        if kind == "config" and p.startswith("["):
            return HiddenResult("busy", "学员处于系统视图，暂不注入（请先在终端回到用户视图 <...>）")
        if ch.rec.active and not ch.rec.head:
            # 学员刚回车、设备还没回显：tail 仍是回车前的提示符，这时开始事务会把学员命令的回显吞进事务
            return HiddenResult("busy", "学员命令刚提交，等待设备回显")
        return p

    async def run_hidden(self, port: int, commands: list[str], kind: str = "check",
                         cmd_timeout: float | None = None, tolerate_errors: bool = False,
                         groups: list[tuple[list[str], int]] | None = None,
                         confirm: bool = False) -> HiddenResult:
        """在 port 上执行一组命令，学员终端看不到。kind: "check"（只读）或 "config"。

        config 事务遇到 Error 默认中止并回到用户视图；tolerate_errors=True（还原时用，尽力而为）则记入
        warnings 继续执行后面的命令。

        groups（仅 config，配置复位用）：[(命令列表, guard), ...]，代替 commands。每组从用户视图开始，
        组内前 guard 条是进入视图的命令（如 system-view、interface X），其中任一条报错就跳过本组剩余
        命令——否则后面的 undo 会落在错误的视图里；其余命令报错只记录、继续。每组结束回到用户视图。
        confirm=True：命令停在 [Y/N] 确认上时自动回答 y（如 undo ospf 1）。
        """
        assert kind in ("check", "config")
        assert groups is None or kind == "config"
        ch = self._channel(port)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        try:
            await self._ensure_connected(ch)
        except HubConnectError as exc:
            return HiddenResult("error", f"连不上设备 console：{exc}")
        try:
            async with ch.lock:
                await self._wait_ready(ch)
                pre = self._precheck(ch, kind)
                if isinstance(pre, HiddenResult) and pre.status == "busy":
                    await self._nudge_to_prompt(ch)
                    pre = self._precheck(ch, kind)
                if isinstance(pre, HiddenResult):
                    return pre
                start_prompt = pre
                # 设备已停在提示符上：学员上一条命令的输出采集到此为止。必须在进入隐藏之前结束，否则事务结束时
                # 补发的日志 / 重印的提示符（sysname 改名后）会被当成学员命令的输出。这里同样不能有 await。
                self._finish_output(ch)
                ch.hidden = True              # 与 precheck 之间没有 await，不会漏过设备输出
                ch.capture = ""
                ch.cur_prompt = start_prompt
                timeout = cmd_timeout or self.cmd_timeout
                try:
                    if groups is not None:
                        res = await self._run_groups(ch, groups, timeout, confirm)
                    else:
                        res = await self._run(ch, commands, kind, timeout, tolerate_errors, confirm)
                    res.prompt = ch.cur_prompt
                    return res
                finally:
                    await self._finish(ch, start_prompt, ch.cur_prompt)
        finally:
            self._maybe_idle(ch)

    async def reclaim(self, port: int) -> HiddenResult:
        """强制收回终端（结束练习、复位之前用；只在 freeze 之后调用）：不管学员停在哪里，都把设备带回用户视图的
        空提示符 <...>，之后的隐藏事务不会再因为学员终端的状态返回 busy。

        学员视角的终端状态（tail）只在可见通道里更新，所以收回的按键走可见通道（学员能看到 ^C / return），不进隐藏。
        每一步按当前状态决定：分页 → q（eNSP Pro connector 按 YunShan 真机实测用的键），仍在分页再 Ctrl-C（_wait_ready / _abort）；[Y/N] → n
        （取消学员那条命令；YunShan 未提交配置时 return 问的 Y/N/C，N = 不提交直接退出）；提示符后有未提交的输入 →
        Ctrl-C 丢弃（还在就移到行尾逐个退格）；命令还在执行（不在提示符上，如 ping）→ Ctrl-C；系统视图 / 子视图 →
        有未提交配置（[*...]）先 commit，再 return。登录提示（console 被登出，实验后端的 connector 会自动重登）只等待，
        绝不补回车。没有任何输出的空行交给之后的隐藏事务按原逻辑唤醒。

        返回 ok（已在用户视图，或终端无输出、留给隐藏事务处理）；收不回（连不上、断开、超时）返回 error。"""
        ch = self._channel(port)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        try:
            await self._ensure_connected(ch)
        except HubConnectError as exc:
            return HiddenResult("error", f"连不上设备 console：{exc}")
        try:
            async with ch.lock:
                await self._wait_ready(ch)
                return await self._reclaim(ch)
        finally:
            self._maybe_idle(ch)

    async def _settle_visible(self, ch: _Channel, end: float, first: float = 2.0) -> None:
        """等设备对刚发的按键有反应（最多 first 秒），再等输出静默 settle*2 秒。"""
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(ch.data_event.wait(), max(0.05, min(first, end - loop.time())))
        except asyncio.TimeoutError:
            return
        while loop.time() < end:
            ch.data_event.clear()
            try:
                await asyncio.wait_for(ch.data_event.wait(), self.settle * 2)
            except asyncio.TimeoutError:
                return

    async def _reclaim(self, ch: _Channel) -> HiddenResult:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.reclaim_timeout
        ch.pending_input.clear()
        self._finish_output(ch)
        ch.data_event.clear()
        await self._settle_visible(ch, end, first=self.settle)   # 学员在途的按键先回显完
        cleared = committed = mores = 0
        for _ in range(16):
            if not ch.connected:
                return HiddenResult("error", "设备连接已断开")
            if loop.time() >= end:
                break
            tail = ch.tail
            line = last_line(tail)
            p = prompt_of(tail)
            typed = input_after_prompt(tail)
            nonempty = [x for x in (render_line(y).strip() for y in strip_more(tail).split("\n")) if x]
            if ends_with_more(tail):
                # q：eNSP Pro connector 按 YunShan 真机实测处理分页残留的键；仍停在分页再 Ctrl-C（_wait_ready / _abort 的做法）
                mores += 1
                key = b"q" if mores == 1 else b"\x03"
            elif is_confirm(settled_line(tail)) or CONFIRM_ANY_RE.search(line):
                key = b"n\r"
            elif typed and typed.strip():
                cleared += 1
                key = b"\x03" if cleared == 1 else b"\x05" + b"\x08" * (len(typed) + 2)
            elif p is not None and p.startswith("["):
                if p.startswith("[*") and not committed:
                    committed += 1
                    key = b"commit\r"
                else:
                    key = b"return\r"
            elif p is not None:
                ch.line, ch.line_dirty, ch.esc, ch.last_cr = [], False, "", False
                return HiddenResult("ok", prompt=p)
            elif typed is None and (LOGIN_PROMPT_RE.search(line) or (
                    not line.strip() and nonempty and re.search(r"Press ENTER", nonempty[-1], re.I))):
                # 登录提示（只看当前行 / 最后一个非空行，与 connector 判断一致）：connector 会自动重登，这里只等
                # （补回车 = 提交空密码）
                ch.data_event.clear()
                await self._settle_visible(ch, end, first=3.0)
                continue
            elif not line.strip():
                break                       # 没有输出 / 空行：交给隐藏事务的唤醒逻辑
            else:
                key = b"\x03"               # 命令还在执行（ping 等）
            ch.data_event.clear()
            await ch.conn.send(key)
            await self._settle_visible(ch, end)
        ch.line, ch.line_dirty, ch.esc, ch.last_cr = [], False, "", False
        if prompt_of(ch.tail) is None and last_line(ch.tail).strip():
            shown = last_line(ch.tail)[-40:]
            return HiddenResult("error", f"无法收回终端（当前：{shown!r}）")
        p = prompt_of(ch.tail)
        if p is not None and p.startswith("["):
            return HiddenResult("error", f"无法回到用户视图（当前：{p}）")
        return HiddenResult("ok", prompt=p or "")

    @staticmethod
    def _prompt_ok(line: str, kind: str, start_prompt: str, host: str) -> bool:
        if kind == "check":
            return line == start_prompt
        # YunShan 两阶段生效的型号：[~HUAWEI]（无未提交配置）/ [*HUAWEI]（有未提交配置）；单阶段（如 AR6700 实测）同 VRP5
        return line == f"<{host}>" or re.match(rf"^\[[~*]?{re.escape(host)}(?:-[^\s\[\]]*)?\]$", line) is not None

    def _done(self, seg: str, cmd: str, kind: str, start_prompt: str, host: str) -> bool:
        """一条命令是否执行完：回显（按字符顺序）出现，其后有换行，且输出停在期望的提示符上。"""
        text = clean(seg)
        echo_end = is_subsequence(cmd, text)
        if echo_end < 0:
            return False
        rest = text[echo_end:]
        if "\n" not in rest:
            return False
        line = settled_line(rest)
        if kind == "config" and is_confirm(line):
            return True
        return self._prompt_ok(line, kind, start_prompt, host)

    async def _exec(self, ch: _Channel, cmd: str, kind: str, start_prompt: str, host: str,
                    timeout: float, confirm: bool = False) -> str | None:
        """发一条命令并读到提示符，返回输出；超时 / 断开返回 None（原因记在 ch.error）。"""
        loop = asyncio.get_running_loop()
        start = len(ch.capture)
        ch.data_event.clear()
        await ch.conn.send(cmd.encode() + b"\r")
        end = loop.time() + timeout
        mores = 0
        answered = 0
        ans_pos = start                       # 上次回答 y 时 capture 的位置
        resend_at = loop.time() + self.echo_timeout if kind == "check" else None
        while True:
            seg = ch.capture[start:]
            if resend_at is not None and loop.time() >= resend_at:
                resend_at = None
                # 只读命令发出去后设备一直没回显、又停在提示符上（2026-10-06 实测：eNSP Pro 的 PC 刚经接口改完地址时
                # 命令行会重启，吞掉这时输入的命令，只重印一个 PC>）：重发一次。只对只读的 check 命令这样做
                if is_subsequence(cmd, clean(seg)) < 0 and prompt_of(seg) is not None:
                    log.info("console %s 命令 %r 没有回显，重发一次", ch.port, cmd)
                    start = len(ch.capture)
                    ch.data_event.clear()
                    await ch.conn.send(cmd.encode() + b"\r")
                    continue
            n_more = seg.count(MORE)
            if n_more > mores and ends_with_more(seg):
                mores = n_more
                ch.data_event.clear()
                await ch.conn.send(b" ")     # 分页：空格继续，分页符由 clean() 去掉
            elif self._done(seg, cmd, kind, start_prompt, host):
                line = settled_line(clean(seg))
                asking = is_confirm(line)
                # 已回答过、设备还没回显：仍是同一个 [Y/N]，等新输出，不能再答一次
                # （实测 S5700 回显 y 较慢，曾被答两次，多出的 y 成了一条错误命令）
                stale = asking and answered and not CONFIRM_ANY_RE.search(ch.capture[ans_pos:])
                if not stale:
                    # 短暂静默确认没有后续输出（避免恰好断在一行像提示符的输出处）
                    ch.data_event.clear()
                    try:
                        await asyncio.wait_for(ch.data_event.wait(), self.settle)
                        continue
                    except asyncio.TimeoutError:
                        pass
                    if not (confirm and asking):
                        return self._extract(ch, ch.capture[start:], cmd)
                    if answered < 3:
                        answered += 1
                        ans_pos = len(ch.capture)
                        ch.data_event.clear()
                        await ch.conn.send(b"y\r")
                        continue
                    return self._extract(ch, ch.capture[start:], cmd)
            if ch.eof:
                ch.error = "设备连接已断开"
                return None
            rem = end - loop.time()
            if resend_at is not None:
                rem = min(rem, max(0.05, resend_at - loop.time()))
            if end - loop.time() <= 0:
                ch.error = f"命令执行超时：{cmd}"
                log.warning("console %s 命令执行超时 %r（期望提示符 %s / 主机名 %s）；最后输出 %r", ch.port, cmd,
                            start_prompt, host, ch.capture[start:][-400:])
                await self._abort(ch, start_prompt)
                return None
            ch.data_event.clear()
            try:
                await asyncio.wait_for(ch.data_event.wait(), rem)
            except asyncio.TimeoutError:
                pass

    async def _abort(self, ch: _Channel, start_prompt: str) -> None:
        """超时：Ctrl-C 中断正在执行的命令，等回到提示符，避免剩余输出在事务结束后漏给学员。"""
        if not ch.connected:
            return
        loop = asyncio.get_running_loop()
        mark = len(ch.capture)
        await ch.conn.send(b"\x03")
        end = loop.time() + 3.0
        while loop.time() < end:
            if prompt_of(ch.capture[mark:]):
                break
            ch.data_event.clear()
            try:
                await asyncio.wait_for(ch.data_event.wait(), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                break
        _, logs = split_logs(clean(ch.capture[mark:]))
        ch.logs.extend(logs)

    def _extract(self, ch: _Channel, seg: str, cmd: str) -> str:
        text, logs = split_logs(clean(seg))
        ch.logs.extend(logs)
        echo_end = is_subsequence(cmd, text)
        nl = text.find("\n", max(echo_end, 0))
        lines = text[nl + 1:].split("\n") if nl >= 0 else []
        while lines and not render_line(lines[-1]).strip():
            lines = lines[:-1]                # 日志被拿掉后留下的空行
        if lines and (is_prompt(render_line(lines[-1])) or CONFIRM_ANY_RE.search(lines[-1])):
            lines = lines[:-1]
        out = []
        for line in lines:
            m = _PROMPT_LINE_RE.match(render_line(line))
            # 日志打断回显时 VRP 会重印「提示符 + 半截命令」，去掉这些行
            if m and m.group(2) and cmd.startswith(m.group(2)):
                continue
            out.append(line)
        return "\n".join(out).strip("\n")

    @staticmethod
    def _is_error(out: str) -> bool:
        return "Error:" in out or re.search(r"^\s*\^\s*$", out, re.M) is not None

    def _host_after(self, cmd: str, host: str) -> str:
        """sysname X / undo sysname 会改提示符：返回这条命令执行后的主机名。"""
        m = _SYSNAME_RE.match(cmd)
        if m:
            return m.group(1)
        if re.match(r"^undo\s+sysname\b", cmd):
            return self.default_sysname
        return host

    async def _exec_tracked(self, ch: _Channel, cmd: str, kind: str, start_prompt: str, host: str,
                            timeout: float, confirm: bool) -> tuple[str | None, str]:
        """执行一条 config 命令并跟踪主机名变化；返回 (输出, 执行后的主机名)。"""
        new_host = self._host_after(cmd, host) if kind == "config" else host
        if new_host == host:
            return await self._exec(ch, cmd, kind, start_prompt, host, timeout, confirm), host
        out = await self._exec(ch, cmd, kind, start_prompt, new_host, timeout, confirm)
        if out is None:
            # 改名可能失败（如名字不合法），提示符仍是旧名字：按旧名字再判断一次
            if self._prompt_ok(settled_line(clean(ch.capture)), kind, start_prompt, host):
                return self._extract(ch, ch.capture, cmd), host
            return None, host
        if self._is_error(out):
            return out, host
        ch.cur_prompt = f"<{new_host}>"
        return out, new_host

    async def _run(self, ch: _Channel, commands: list[str], kind: str, timeout: float,
                   tolerate: bool = False, confirm: bool = False) -> HiddenResult:
        start_prompt = ch.cur_prompt
        host = start_prompt[1:-1]
        outputs: list[str] = []
        warnings: list[str] = []
        failed: list[str] = []
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            out, host = await self._exec_tracked(ch, cmd, kind, start_prompt, host, timeout, confirm)
            if out is None:
                if kind == "config":
                    await self._back_to_user_view(ch, host, timeout)
                return HiddenResult("error", ch.error, outputs, failed=failed + [cmd])
            outputs.append(out)
            if kind == "config" and self._is_error(out):
                msg = f"命令执行失败：{cmd}：{out.strip()[:200]}"
                failed.append(cmd)
                if tolerate:
                    warnings.append(msg)
                    continue
                await self._back_to_user_view(ch, host, timeout)
                return HiddenResult("error", msg, outputs, failed=failed)
        if kind == "config" and not await self._back_to_user_view(ch, host, timeout):
            return HiddenResult("error", "未能回到用户视图", outputs, warnings, failed)
        return HiddenResult("ok", "", outputs, warnings, failed)

    async def _run_groups(self, ch: _Channel, groups: list[tuple[list[str], int]], timeout: float,
                          confirm: bool) -> HiddenResult:
        host = ch.cur_prompt[1:-1]
        outputs: list[str] = []
        warnings: list[str] = []
        failed: list[str] = []
        for cmds, guard in groups:
            for i, cmd in enumerate(c.strip() for c in cmds):
                if not cmd:
                    continue
                out, host = await self._exec_tracked(ch, cmd, "config", ch.cur_prompt, host, timeout, confirm)
                if out is None:
                    await self._back_to_user_view(ch, host, timeout)
                    return HiddenResult("error", ch.error, outputs, warnings, failed + [cmd])
                outputs.append(out)
                if self._is_error(out):
                    failed.append(cmd)
                    warnings.append(f"命令执行失败：{cmd}：{out.strip()[:200]}")
                    if i < guard:
                        failed.extend(c.strip() for c in cmds[i + 1:] if c.strip())
                        break             # 没进对视图：本组剩下的命令不执行
            if not await self._back_to_user_view(ch, host, timeout):
                return HiddenResult("error", "未能回到用户视图", outputs, warnings, failed)
        return HiddenResult("ok", "", outputs, warnings, failed)

    async def _back_to_user_view(self, ch: _Channel, host: str, timeout: float) -> bool:
        if not ch.connected:
            return False
        target = f"<{host}>"
        cur = settled_line(clean(ch.capture))
        if cur == target:
            ch.cur_prompt = target
            return True
        if cur.startswith("[*"):
            # 两阶段生效的 YunShan：有未提交的配置时先 commit（否则 return 会问 Y/N/C）
            out = await self._exec(ch, "commit", "config", target, host, timeout, confirm=True)
            if out is None or self._is_error(out):
                ch.error = f"commit 失败：{(out or ch.error).strip()[:200]}"
                return False
        out = await self._exec(ch, "return", "config", target, host, timeout, confirm=True)
        ok = out is not None and settled_line(clean(ch.capture)) == target
        if ok:
            ch.cur_prompt = target
        else:
            log.warning("console %s 未能回到用户视图 %s：%s；最后输出 %r", ch.port, target, ch.error,
                        clean(ch.capture)[-300:])
        return ok

    async def _finish(self, ch: _Channel, start_prompt: str, end_prompt: str | None = None) -> None:
        """恢复转发：补发捕获期间的异步日志（并像 VRP 一样重印提示符），写回暂存的学员输入。

        提示符变了（事务里 sysname 改名）时也重印一次，让学员屏幕与 tail（下次前提检查看的就是它）
        跟上设备当前的提示符。
        """
        logs, ch.logs = ch.logs, []
        ch.hidden = False
        ch.capture = ""
        prompt = end_prompt or start_prompt
        if logs or prompt != start_prompt:
            data = ("\r\n" + "".join(l + "\r\n" for l in logs) + prompt).encode()
            self._visible(ch, data)
        pending = bytes(ch.pending_input)
        ch.pending_input.clear()
        if pending and ch.connected:
            await self._send_student(ch, pending)

    async def reset_all(self, message: str = "", *, default_sysname: str | None = None,
                        lock_timeout: float = 15.0) -> int:
        """切换模拟环境时（spec §5.2）：断开全部终端订阅者（先发 message）、关闭全部设备连接、清空通道与 pin。
        进行中的隐藏事务（Agent 只读命令）最多等 lock_timeout 秒。之后新建的连接走新的实验后端。返回关闭的通道数。"""
        chans = list(self.channels.values())
        self.channels = {}
        self.pinned.clear()
        if default_sysname:
            self.default_sysname = default_sysname
        for ch in chans:
            if ch.idle_task is not None:
                ch.idle_task.cancel()
                ch.idle_task = None
            for sub in list(ch.subs):
                if message:
                    sub.queue.put_nowait(message.encode())
                sub.queue.put_nowait(None)
            ch.subs.clear()
        for ch in chans:
            try:
                await asyncio.wait_for(ch.lock.acquire(), lock_timeout)
                locked = True
            except asyncio.TimeoutError:
                locked = False
            try:
                await self._drop_conn(ch)
                ch.eof = True
            finally:
                if locked:
                    ch.lock.release()
        return len(chans)

    async def close(self) -> None:
        for ch in self.channels.values():
            if ch.idle_task is not None:
                ch.idle_task.cancel()
            for sub in list(ch.subs):
                sub.queue.put_nowait(None)
            ch.subs.clear()
            await self._drop_conn(ch)
        self.channels.clear()
