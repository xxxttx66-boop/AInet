"""测试用的假 VRP 设备 + 假 connector：不依赖真设备，模拟 console 的关键行为。

- 共用一根「串口」：同一 FakeVRP 的所有连接共享输入输出（spec §3 实测）。
- 回显输入字符，回车执行命令；提示符 <host> / [host] / [host-view]。
- system-view / interface X / return / quit 切换视图；未知命令在系统视图下报 Error。
- 输出超过 page 行时分页：`  ---- More ----`，收到空格继续（VRP 用 ESC[42D 擦除分页符）。
- 可在回显第 k 个字符后插入一条异步日志（VRP 会重印提示符与半截命令）。
- ConfigVRP：带配置树，配置复位（config_reset）的集成测试用。
- NetVRP + FakeNet（里程碑 6）：多台 ConfigVRP 组成的小网络，ping 结果按各设备的配置树计算（接口地址、直连、
  静态路由、交换机 access 口的 PVID 与 Vlanif），用于考试 / 练习的模拟设备 e2e：学员真改对了配置才 ping 得通。
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable

ERASE = "\x1b[42D" + " " * 42 + "\x1b[42D"


class FakeVRP:
    def __init__(self, host: str = "Huawei", page: int = 6) -> None:
        self.host = host
        self.page = page
        self.view: str | None = None          # None=用户视图，""=系统视图，其余=子视图名
        self.conns: list[FakeConn] = []
        self.connects = 0
        self.typed = ""                        # 当前输入行
        self.received = ""                     # 收到的全部原始输入（断言用）
        self.executed: list[str] = []          # 执行过的命令
        self.responses: dict[str, str | Callable[[], str]] = {}
        self.pending_pages: list[str] = []
        self.log_after: tuple[int, str] | None = None   # (回显第 k 个字符后, 日志行)
        self.state: dict = {}
        self.silent = False                    # True：不输出任何东西（模拟刚断开后重连无输出）
        self.ips: dict[str, str] = {}          # 接口名 -> IP（ip address / undo ip address）

    @property
    def prompt(self) -> str:
        if self.view is None:
            return f"<{self.host}>"
        return f"[{self.host}{'-' + self.view if self.view else ''}]"

    def emit(self, text: str) -> None:
        if self.silent:
            return
        for c in self.conns:
            if not c.closed:
                c.q.put_nowait(text.encode())

    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", "ignore")
        self.received += text
        for ch in text:
            if self.pending_pages:
                if ch == " ":
                    self._next_page()
                else:   # 分页时其他键结束输出（真机吞掉这个字符）
                    self.pending_pages = []
                    self.emit(ERASE + "\r\n" + self.prompt)
                continue
            if ch == "\r":
                cmd, self.typed = self.typed, ""
                self._execute(cmd.strip())
            elif ch == "\n":
                continue
            elif ch in "\x7f\x08":
                if self.typed:
                    self.typed = self.typed[:-1]
                    self.emit("\b \b")
            elif ch == "\x03":
                self.typed = ""
                self.emit("^C\r\n" + self.prompt)
            elif ch >= " ":
                self.typed += ch
                self.emit(ch)
                if self.log_after and len(self.typed) == self.log_after[0]:
                    _, log = self.log_after
                    self.log_after = None
                    self.emit(f"\r\n{log}\r\n{self.prompt}{self.typed}")

    def _execute(self, cmd: str) -> None:
        if cmd:
            self.executed.append(cmd)
        out: str | None
        if not cmd:
            out = ""
        elif cmd == "system-view" and self.view is None:
            self.view = ""
            out = "Enter system view, return user view with Ctrl+Z."
        elif cmd.startswith("interface ") and self.view is not None:
            self.view = cmd.split(None, 1)[1]
            out = ""
        elif cmd == "return":
            self.view = None
            out = ""
        elif cmd == "quit":
            if self.view:
                self.view = ""
            elif self.view == "":
                self.view = None
            out = ""
        elif cmd.startswith("ip address ") and self.view:
            self.ips[self.view] = cmd.split()[2]
            out = ""
        elif cmd == "undo ip address" and self.view:
            out = "" if self.ips.pop(self.view, None) else "Error: The address does not exist."
        elif cmd in self.responses:
            r = self.responses[cmd]
            out = r() if callable(r) else r
        elif self.view is not None and not cmd.startswith(("ip ", "undo ", "shutdown", "display")):
            out = "        ^\r\nError: Unrecognized command found at '^' position."
        elif cmd.startswith("display"):
            out = f"Info: no output for {cmd}"
        else:
            out = ""
        lines = out.split("\r\n") if out else []
        if len(lines) > self.page:
            pages = [lines[i:i + self.page] for i in range(0, len(lines), self.page)]
            self.emit("\r\n" + "\r\n".join(pages[0]) + "\r\n  ---- More ----")
            self.pending_pages = ["\r\n".join(p) for p in pages[1:]]
            return
        self.emit("\r\n" + ("\r\n".join(lines) + "\r\n" if lines else "") + self.prompt)

    def _next_page(self) -> None:
        page = self.pending_pages.pop(0)
        if self.pending_pages:
            self.emit(ERASE + page + "\r\n  ---- More ----")
        else:
            self.emit(ERASE + page + "\r\n" + self.prompt)

    def factory(self, port: int) -> "FakeConn":
        return FakeConn(self)


class FakeConn:
    def __init__(self, dev: FakeVRP) -> None:
        self.dev = dev
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    async def connect(self) -> None:
        self.dev.connects += 1
        self.dev.conns.append(self)

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.dev.feed(data)

    async def read(self) -> bytes:
        if self.closed:
            return b""
        return await self.q.get()

    def hangup(self) -> None:
        """模拟设备侧断开（EOF）。"""
        self.q.put_nowait(b"")


    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.q.put_nowait(b"")


class ConfigVRP(FakeVRP):
    """带配置树的假 VRP：进入 / 创建视图、加行、undo 删行、sysname 改提示符、undo ospf / vlan batch 要 [Y/N]
    （回答前延迟回显，复现 S5700 回显慢导致重复回答的问题），display current-configuration 按 VRP 的样子
    输出（# 分段、缩进子行、分页）。

    refuse：这些 undo 命令一律报错（模拟删不掉的配置）。
    """

    HEADERS = re.compile(r"^(interface \S+|ospf \d+(?: router-id \S+)?|area \S+|acl number \d+|vlan \d+|aaa)$")

    def __init__(self, config_text: str, host: str = "Huawei", page: int = 20) -> None:
        super().__init__(host=host, page=page)
        import config_reset as cr
        self.cr = cr
        self.cfg = cr.parse(config_text)
        self.path: list = []                   # 当前所在的块（Node）链
        self.confirm = None                    # 等 [Y/N] 时的回调
        self.refuse: set[str] = set()
        self.confirms = 0
        self.vlans = cr.vlans_of(self.cfg)     # VLAN 集合（显示为一行 vlan batch）
        self.cfg = [n for n in self.cfg if not n.line.startswith("vlan batch ")]
        for n in self.cfg:
            if n.line.startswith("sysname "):
                self.host = n.line.split(" ", 1)[1]

    @property
    def prompt(self) -> str:
        if self.view is None:
            return f"<{self.host}>"
        tail = "-" + self.path[-1].line.replace(" ", "-") if self.path else ""
        return f"[{self.host}{tail}]"

    def _container(self) -> list:
        return self.path[-1].children if self.path else self.cfg

    def _set_sysname(self, name: str) -> None:
        self.host = name
        self.cfg = [n for n in self.cfg if not n.line.startswith("sysname ")]
        if name != "Huawei":
            self.cfg.insert(0, self.cr.Node(f"sysname {name}"))

    def config_text(self) -> str:
        out = ["[V200R003C00]"]
        cfg = list(self.cfg)
        if self.vlans:
            k = 1 if cfg and cfg[0].line.startswith("sysname ") else 0
            cfg.insert(k, self.cr.Node(f"vlan batch {self.cr.vlan_spec(self.vlans)}"))
        for n in cfg:
            out.append("#")
            out += self.cr.render([n])
        out += ["#", "return"]
        return "\r\n".join(out)

    def feed(self, data: bytes) -> None:
        if self.confirm is not None:
            # 等 [Y/N]：y 的回显推迟到下一个事件循环周期之后（真机 S5700 回显慢）
            text = data.decode("utf-8", "ignore")
            self.received += text
            if "\r" in text:
                cb, self.confirm = self.confirm, None
                ans = (self.typed + text.split("\r")[0]).strip().lower()
                self.typed = ""
                self.confirms += 1

                async def later():
                    await asyncio.sleep(0.12)
                    self.emit(ans + "\r\n")
                    if ans == "y":
                        cb()
                    self.emit(self.prompt)
                asyncio.get_running_loop().create_task(later())
            else:
                self.typed += text
            return
        super().feed(data)

    def _execute(self, cmd: str) -> None:
        if cmd:
            self.executed.append(cmd)
        out = self._apply(cmd)
        if out is None:
            return                              # 已自行输出（[Y/N]）
        lines = out.split("\r\n") if out else []
        if len(lines) > self.page:
            pages = [lines[i:i + self.page] for i in range(0, len(lines), self.page)]
            self.emit("\r\n" + "\r\n".join(pages[0]) + "\r\n  ---- More ----")
            self.pending_pages = ["\r\n".join(p) for p in pages[1:]]
            return
        self.emit("\r\n" + ("\r\n".join(lines) + "\r\n" if lines else "") + self.prompt)

    def _apply(self, cmd: str) -> str | None:
        err = "        ^\r\nError: Unrecognized command found at '^' position."
        if not cmd:
            return ""
        if cmd == "display current-configuration":
            return self.config_text()
        if self.view is None:
            if cmd == "system-view":
                self.view = ""
                return "Enter system view, return user view with Ctrl+Z."
            return err
        if cmd == "return":
            self.view, self.path = None, []
            return ""
        if cmd == "quit":
            if self.path:
                self.path.pop()
                self.view = self.path[-1].line if self.path else ""
            else:
                self.view = None
            return ""
        m = re.match(r"^sysname (\S+)$", cmd)
        if m and not self.path:
            self._set_sysname(m.group(1))
            return ""
        cont = self._container()
        if cmd.startswith("vlan batch ") and not self.path:
            self.vlans |= self.cr._vlan_ids(cmd[len("vlan batch "):])
            return ""
        if cmd.startswith("undo vlan batch ") and not self.path:
            ids = self.cr._vlan_ids(cmd[len("undo vlan batch "):])

            def drop_vlans():
                self.vlans -= ids
            self.confirm = drop_vlans
            self.emit("\r\nWarning: The configurations of the VLAN will be deleted. Continue?[Y/N]:")
            return None
        if cmd.startswith("undo "):
            if cmd in self.refuse:
                return "Error: The configuration cannot be deleted."
            rest = cmd[5:]
            if rest == "sysname" and not self.path:
                self._set_sysname("Huawei")
                return ""
            for n in cont:
                if n.line == rest or n.line.startswith(rest + " "):
                    def drop(n=n):
                        cont.remove(n)
                    if rest.startswith(("ospf ", "vlan batch")):
                        self.confirm = drop
                        self.emit("\r\nWarning: The configurations will be deleted. Continue?[Y/N]:")
                        return None
                    drop()
                    return ""
            return "Error: The configuration does not exist."
        if self.HEADERS.match(cmd):
            key = cmd.split(" router-id")[0]
            for n in cont:
                if n.line == cmd or n.line.split(" router-id")[0] == key:
                    break
            else:
                n = self.cr.Node(cmd)
                cont.append(n)
            self.path.append(n)
            self.view = n.line
            return ""
        if not any(n.line == cmd for n in cont):
            cont.append(self.cr.Node(cmd))
        return ""


# ---------------- 小网络模型（里程碑 6：模拟设备 e2e） ----------------

def _mask_len(m: str) -> int:
    if m.isdigit():
        return int(m)
    return sum(bin(int(x)).count("1") for x in m.split("."))


def _ip_int(ip: str) -> int:
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def _same_net(a: str, b: str, plen: int) -> bool:
    m = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
    return (_ip_int(a) & m) == (_ip_int(b) & m)


class FakeNet:
    """links: [((设备, 接口), (设备, 接口))]。交换机的物理口按 access PVID 落到 Vlanif<PVID> 上。"""

    def __init__(self, links: list[tuple[tuple[str, str], tuple[str, str]]]) -> None:
        self.devs: dict[str, "NetVRP"] = {}
        self.links = links

    def add(self, name: str, dev: "NetVRP") -> "NetVRP":
        self.devs[name] = dev
        dev.net, dev.name = self, name
        return dev

    def shut(self, name: str, port: str) -> bool:
        """接口下有 shutdown（管理性关闭）。"""
        if name not in self.devs:
            return False
        return any(n.line == f"interface {port}" and any(ch.line == "shutdown" for ch in n.children)
                   for n in self.devs[name].cfg)

    # 某台设备的三层接口：{接口名: (ip, 前缀长度)}；关闭的接口不算（不进路由表）
    def l3(self, name: str) -> dict[str, tuple[str, int]]:
        out = {}
        for n in self.devs[name].cfg:
            if n.line.startswith("interface "):
                ifn = n.line.split(" ", 1)[1]
                if any(ch.line == "shutdown" for ch in n.children):
                    continue
                for ch in n.children:
                    m = re.match(r"^ip address (\S+) (\S+)$", ch.line)
                    if m:
                        out[ifn] = (m.group(1), _mask_len(m.group(2)))
        return out

    def pvid(self, name: str, port: str) -> int:
        for n in self.devs[name].cfg:
            if n.line == f"interface {port}":
                for ch in n.children:
                    m = re.match(r"^port default vlan (\d+)$", ch.line)
                    if m:
                        return int(m.group(1))
        return 1

    def _l3_if(self, name: str, port: str) -> str:
        """物理口对应的三层接口（交换机：Vlanif<PVID>）。"""
        if name.startswith("LSW") and port.startswith("GigabitEthernet"):
            return f"Vlanif{self.pvid(name, port)}"
        return port

    def neighbors(self, name: str, ifn: str) -> list[tuple[str, str]]:
        """从 name 的三层接口 ifn 出去能到达的（设备, 三层接口）。"""
        out = []
        for (d1, p1), (d2, p2) in self.links:
            if self.shut(d1, p1) or self.shut(d2, p2):
                continue                    # 任一端口关闭 = 链路不通（真机两端都 down）
            for (a, pa), (b, pb) in (((d1, p1), (d2, p2)), ((d2, p2), (d1, p1))):
                if a == name and self._l3_if(a, pa) == ifn:
                    out.append((b, self._l3_if(b, pb)))
        return out

    def routes(self, name: str) -> list[tuple[str, int, str | None, str | None]]:
        """(网络, 前缀, 出接口 | None, 下一跳 | None)。"""
        rs = [(ip, plen, ifn, None) for ifn, (ip, plen) in self.l3(name).items()]
        for n in self.devs[name].cfg:
            m = re.match(r"^ip route-static (\S+) (\S+) (\S+)$", n.line)
            if m:
                rs.append((m.group(1), _mask_len(m.group(2)), None, m.group(3)))
        return rs

    def forward(self, name: str, dst: str, ttl: int = 8) -> tuple[bool, str | None]:
        """(能否到达, 首跳出接口地址)。"""
        if ttl <= 0:
            return False, None
        mine = self.l3(name)
        if any(ip == dst for ip, _ in mine.values()):
            return True, dst
        best = None
        for net, plen, ifn, nh in self.routes(name):
            if _same_net(net, dst, plen) and (best is None or plen > best[1]):
                best = (net, plen, ifn, nh)
        if best is None:
            return False, None
        net, plen, ifn, nh = best
        target = dst if ifn is not None else nh
        # 找到下一跳所在的直连接口
        for my_if, (my_ip, my_plen) in mine.items():
            if _same_net(my_ip, target, my_plen):
                for peer, pif in self.neighbors(name, my_if):
                    pip = self.l3(peer).get(pif)
                    if pip and pip[0] == target:
                        if target == dst:
                            return True, my_ip
                        ok, _ = self.forward(peer, dst, ttl - 1)
                        return ok, my_ip
                return False, my_ip
        return False, None

    def owner(self, ip: str) -> str | None:
        for name in self.devs:
            if any(x == ip for x, _ in self.l3(name).values()):
                return name
        return None

    def ping(self, name: str, dst: str) -> bool:
        ok, src = self.forward(name, dst)
        if not ok or src is None:
            return False
        if src == dst:
            return True
        back = self.owner(dst)
        return back is not None and self.forward(back, src)[0]


class NetVRP(ConfigVRP):
    """FakeNet 里的一台设备：ping 按网络模型计算；display ip interface brief 列出三层接口。"""

    net: FakeNet
    name: str

    def _apply(self, cmd: str) -> str | None:
        m = re.match(r"^ping(?: -c (\d+))?(?: -a \S+)? (\d+\.\d+\.\d+\.\d+)$", cmd)
        if m:
            n = int(m.group(1) or 5)
            ok = self.net.ping(self.name, m.group(2))
            return (f"  PING {m.group(2)}: 56  data bytes, press CTRL_C to break\r\n"
                    + ("".join(f"    Reply from {m.group(2)}: bytes=56 Sequence={i + 1} ttl=255 time=10 ms\r\n"
                               for i in range(n)) if ok else
                       "".join("    Request time out\r\n" for _ in range(n)))
                    + f"\r\n  --- {m.group(2)} ping statistics ---\r\n"
                    + f"    {n} packet(s) transmitted\r\n    {n if ok else 0} packet(s) received\r\n"
                    + f"    {0 if ok else 100}.00% packet loss")
        if cmd in ("display ip interface brief", "dis ip int br"):
            rows = [f"{ifn:<34}{ip}/{plen:<16}up         up" for ifn, (ip, plen) in sorted(self.net.l3(self.name).items())]
            return "Interface                         IP Address/Mask      Physical   Protocol\r\n" + "\r\n".join(rows)
        if cmd.startswith(("display ip routing-table", "dis ip rou")):
            rows = [f"{net}/{plen:<4} {'Direct' if ifn else 'Static'}  {nh or '-'}  {ifn or ''}"
                    for net, plen, ifn, nh in self.net.routes(self.name)]
            return "Route Flags: R - relay, D - download to fib\r\n" + "\r\n".join(rows)
        if self.view is None and cmd.startswith(("display", "dis ", "tracert")) and cmd != "display current-configuration":
            return f"Info: no output for {cmd}"
        if self.view is not None and self.path and re.match(r"^(interface \S+|acl number \d+|vlan \d+)$", cmd):
            self.path = []          # 真机：在子视图里输入 interface / acl 直接切到这个顶层视图（不是嵌套）
        m = re.match(r"^(port default vlan|port link-type|ip address|description) ", cmd)
        if self.path and m and " sub" not in cmd:
            # 单值配置：真机是覆盖，不是追加一行
            kids = self.path[-1].children
            kids[:] = [n for n in kids if not n.line.startswith(m.group(1) + " ")]
        return super()._apply(cmd)
