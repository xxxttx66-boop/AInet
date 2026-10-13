"""测试用的假 eNSP Pro：REST（httpx.MockTransport）+ STOMP over WebSocket 服务（websockets）+ 带登录流程的假 YunShan console。

不连任何真实设备。行为按 2026-10 实测的 eNSP Pro / YunShan OS 1.25 模拟：
- REST：离线登录（/ → /api/login → /api/getUserInfo）、{code, message, data}；kick() 后所有接口返回 50070014
  （同一账号在别处登录），直到重新走一遍登录；getOneSandbox / batchStart / batchStop / link/create。
- STOMP：CONNECT → CONNECTED；SUBSCRIBE；SEND /app/console/connect|message|heartbeat|close/{sb}/{dev}；
  设备输出以 MESSAGE 帧下发，可选一个字符一帧、一条 WebSocket 消息里塞多帧。
- FakeYunShan：全新设备首次登录要设置密码（Please configure the login password (8-16) / Enter Password: /
  Confirm Password:），之后登录只问 Password:；密码错误再问一次 Password:（记录尝试次数）；登录后是 FakeVRP 的
  命令行（提示符 <HUAWEI>，分页 `  ---- More ----`）。logout() 模拟 console 空闲超时登出（真机格式：
  `User interface con0 is available` + `Please Press ENTER.` + 空行，2026-10-06 S6700 / AR6700 实测，约 5 分钟无操作）。
- FakePC：eNSP Pro 的 PC-CLIENT 命令行（`PC>`，无登录；只有 ping / ifconfig 等，地址只能经设备属性接口配置；
  ping 为 Linux iputils 格式 `2 packets transmitted, 2 received, 0% packet loss`）。
- FakeRest 另有设备属性（refreshAttrs / updateAttrs，data 是 JSON 字符串）与 reboot_405（本机 eNSP Pro 的
  batchReboot 返回 HTTP 405）。
- YunShanSwitch：ENSP-LSW（S6700 V600R025）的配置树：接口 GE1/0/x（GigabitEthernet1/0/x 报 Wrong parameter），
  端口缺省 link-type desirable / PVID 1，display port vlan 按配置计算。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import websockets

import re

from connectors.enspro import StompParser, frame
from fake_vrp import ERASE, FakeVRP, NetVRP


class LoginMixin:
    """console 登录流程（放在设备类前面）：回车 → [首次设置密码] / Password: → 命令行。"""

    def __init__(self, *args, password: str = "Fake-Pw#1234", fresh: bool = False, **kw) -> None:
        super().__init__(*args, **kw)
        self.password = password
        self.fresh = fresh              # True：首次登录要设置密码
        self.auth = "idle"              # idle（等回车）/ set1 / set2 / login / shell
        self.pw_buf = ""
        self.new_pw = ""
        self.login_attempts = 0         # 在 Password: 后提交过几次（含空）
        self.empty_submits = 0
        self.wakeups = 0

    def logout(self) -> None:
        """console 空闲超时 / 用户视图 quit：会话登出，回到「回车开始」（真机格式，最后一行是空的）。"""
        self.auth = "idle"
        self.view = None
        self.emit("\r\nInfo: The max number of VTY users is 21, and the number of current VTY users on line is 0.\r\n"
                  "User interface con0 is available\r\n\r\n\r\nPlease Press ENTER.\r\n\r\n")

    def _prompt_login(self) -> None:
        if self.fresh:
            self.auth = "set1"
            self.emit("\r\nPlease configure the login password (8-16)\r\nEnter Password:")
        else:
            self.auth = "login"
            self.emit("\r\nPassword:")

    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", "ignore")
        if self.auth == "shell":
            return super().feed(data)
        self.received += text
        for i, ch in enumerate(text):
            if self.auth == "shell":                 # 登录成功后剩下的字符照常处理
                return super().feed(text[i:].encode())
            if self.auth == "idle":
                if ch in "\r\n ":
                    self.wakeups += 1
                    self._prompt_login()
                continue
            if ch == "\n":
                continue
            if ch != "\r":
                self.pw_buf += ch                    # 密码不回显
                continue
            pw, self.pw_buf = self.pw_buf, ""
            if self.auth == "set1":
                self.new_pw = pw
                self.auth = "set2"
                self.emit("\r\nConfirm Password:")
            elif self.auth == "set2":
                if pw == self.new_pw and len(pw) >= 8:
                    self.password, self.fresh = pw, False
                    self._enter_shell()
                else:
                    self.auth = "set1"
                    self.emit("\r\nError: The two passwords are different.\r\nEnter Password:")
            elif self.auth == "login":
                self.login_attempts += 1
                if not pw:
                    self.empty_submits += 1
                if pw == self.password:
                    self._enter_shell()
                else:
                    self.emit("\r\nError: Authentication failed.\r\nPassword:")

    def _enter_shell(self) -> None:
        self.auth = "shell"
        self.view = None
        self.emit("\r\n\r\nInfo: The max number of VTY users is 21, the number of current VTY users online is 0.\r\n"
                  f"{self.prompt}")


class FakePC(FakeVRP):
    """eNSP Pro 的 PC-CLIENT 终端（2026-10-06 实测）：提示符 `PC> `，没有登录；help 列出 ping / traceroute / arp /
    ifconfig 等；不能用命令配地址（ipconfig: command not found），地址由设备属性接口（FakeRest.attrs）配置。
    reachable：能 ping 通的地址集合（或 callable(ip) -> bool）。"""

    def __init__(self, reachable=None) -> None:
        super().__init__(host="PC", page=1000)
        self.attrs: dict[str, str] = {}
        self.reachable = reachable if reachable is not None else set()
        self.deaf = 0                       # 接下来吞掉几次回车（命令行重启期间的输入丢失）
        self.dropped: list[str] = []

    def restart_cli(self, drops: int = 1) -> None:
        """经接口改地址后 PC 的命令行重启（2026-10-06 真机：这时输入的命令被吞掉，只重印一个 PC>）。"""
        self.deaf = drops

    def feed(self, data: bytes) -> None:
        if self.deaf:
            text = data.decode("utf-8", "ignore")
            self.typed += text.replace("\r", "")
            if "\r" in text:
                self.deaf -= 1
                self.dropped.append(self.typed)
                self.typed = ""
                self.emit("\r\n" + self.prompt)
            return
        super().feed(data)

    @property
    def prompt(self) -> str:
        return "PC> "

    def _ok(self, ip: str) -> bool:
        if not self.attrs.get("ipv4.config.ip"):
            return False
        r = self.reachable
        return r(ip) if callable(r) else ip in r

    def _execute(self, cmd: str) -> None:
        if cmd:
            self.executed.append(cmd)
        m = re.match(r"^ping(?: -c (\d+))?(?: -W \d+)? (\d+\.\d+\.\d+\.\d+)$", cmd)
        if not cmd:
            out = ""
        elif m:
            n, ip = int(m.group(1) or 4), m.group(2)
            if not self.attrs.get("ipv4.config.ip"):
                out = "/usr/sbin/ping: connect: Network is unreachable"
            else:
                ok = self._ok(ip)
                rows = "".join(f"64 bytes from {ip}: icmp_seq={i + 1} ttl=255 time=9.8 ms\r\n" for i in range(n)) if ok else ""
                out = (f"PING {ip} ({ip}) 56(84) bytes of data.\r\n{rows}\r\n--- {ip} ping statistics ---\r\n"
                       f"{n} packets transmitted, {n if ok else 0} received, {0 if ok else 100}% packet loss, time 1002ms"
                       + ("\r\nrtt min/avg/max/mdev = 9.670/11.753/13.837/2.083 ms" if ok else "\r\n"))
        elif cmd == "ifconfig":
            # 格式照 2026-10-07 真机 PC-CLIENT 的 ifconfig（含 DNS server 行，DNS 来自属性 ipv4.config.dns1）
            out = ("Local IPv6 address................:  fe80::12ca:38ff:feeb:c29f\r\n"
                   "IPv6 address......................:  ::\r\n"
                   "IPv6 gateway......................:  ::\r\n"
                   f"IPv4 address......................:  {self.attrs.get('ipv4.config.ip') or '0.0.0.0'}\r\n"
                   f"Subnet mask.......................:  {self.attrs.get('ipv4.config.ipmask') or '0.0.0.0'}\r\n"
                   f"Gateway...........................:  {self.attrs.get('ipv4.config.gateway') or '0.0.0.0'}\r\n"
                   "Physical address..................:  10:ca:38:eb:c2:9f\r\n"
                   f"DNS server........................:  {self.attrs.get('ipv4.config.dns1') or ''}  ")
        else:
            out = f"bash: {cmd.split()[0]}: command not found"
        self.emit("\r\n" + (out + "\r\n" if out else "") + self.prompt)


class FakeYunShan(LoginMixin, FakeVRP):
    """带登录流程的假 YunShan console（命令行部分是 FakeVRP）。"""

    def __init__(self, password: str = "Fake-Pw#1234", *, fresh: bool = False, host: str = "HUAWEI",
                 page: int = 6) -> None:
        super().__init__(host=host, page=page, password=password, fresh=fresh)


class FakeStompServer:
    """假 eNSP Pro 的 WebSocket 端：/web.socket/websocket，按 deviceId 接到 FakeYunShan。"""

    def __init__(self, devices: dict[int, FakeYunShan], sandbox: int = 2, *, char_frames: bool = True,
                 require_cookie: str = "__Host-SESSION") -> None:
        self.devices = devices
        self.sandbox = sandbox
        self.char_frames = char_frames
        self.require_cookie = require_cookie
        self.server: Any = None
        self.port = 0
        self.sessions: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.closes = 0
        self.connect_frames = 0
        self.rejected = 0
        self.valid_cookie: str | None = None      # 只接受这个会话 cookie（被挤掉后旧 cookie 收不到输出）

    async def start(self) -> "FakeStompServer":
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, ws) -> None:
        cookie = ws.request.headers.get("Cookie", "")
        sess = {"ws": ws, "subs": [], "dev": None, "cookie": cookie, "origin": ws.request.headers.get("Origin")}
        self.sessions.append(sess)
        parser = StompParser()
        conn = _DevConn(self, ws)
        try:
            async for msg in ws:
                for cmd, headers, body in parser.feed(msg):
                    dest = headers.get("destination", "")
                    if cmd == "CONNECT":
                        self.connect_frames += 1
                        if self.require_cookie and self.require_cookie not in cookie:
                            await ws.send(frame("ERROR", {"message": "Unauthorized"}))
                            self.rejected += 1
                            return
                        await ws.send(frame("CONNECTED", {"version": "1.2", "heart-beat": "0,0"}))
                    elif cmd == "SUBSCRIBE":
                        sess["subs"].append(dest)
                    elif cmd == "SEND":
                        parts = dest.strip("/").split("/")     # app/console/<kind>/<sb>/<dev>
                        if len(parts) != 5:
                            continue
                        kind, sb, dev = parts[2], int(parts[3]), int(parts[4])
                        d = self.devices.get(dev) if sb == self.sandbox else None
                        if kind == "connect" and d is not None:
                            if self.valid_cookie is not None and self.valid_cookie not in cookie:
                                continue                    # 会话已被挤掉：静默（收不到任何输出）
                            sess["dev"] = dev
                            conn.dest = f"/user/topic/console/message/{sb}/{dev}"
                            conn.dev = d
                            d.conns.append(conn)
                            d.connects += 1
                        elif kind == "message" and d is not None and sess["dev"] == dev:
                            d.feed(body.encode("utf-8"))
                        elif kind == "heartbeat":
                            self.heartbeats += 1
                        elif kind == "close":
                            self.closes += 1
                            conn.closed = True
                    elif cmd == "DISCONNECT":
                        return
        except websockets.ConnectionClosed:
            pass
        finally:
            conn.closed = True
            if conn.task is not None:
                conn.task.cancel()

    async def kill_all(self) -> None:
        """服务端断开所有 console WebSocket（模拟 eNSP Pro 重启 / 网络断开）。"""
        for s in list(self.sessions):
            try:
                await s["ws"].close()
            except Exception:
                pass


class _DevConn:
    """FakeVRP 的「连接」：emit 的输出排队、由后台任务以 MESSAGE 帧发出。"""

    def __init__(self, server: FakeStompServer, ws) -> None:
        self.server = server
        self.ws = ws
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.dest = ""
        self.dev: FakeYunShan | None = None
        self.task = asyncio.get_running_loop().create_task(self._pump())
        self.n = 0

    async def _pump(self) -> None:
        try:
            while True:
                data = (await self.q.get()).decode("utf-8")
                if self.closed:
                    continue
                if self.server.char_frames:
                    # 一个字符一帧；每 3 帧拼成一条 WebSocket 消息（一条消息多帧）
                    frames = [frame("MESSAGE", {"destination": self.dest, "subscription": "msg-0",
                                                "message-id": str(self.n + i)}, c) for i, c in enumerate(data)]
                    self.n += len(frames)
                    for i in range(0, len(frames), 3):
                        await self.ws.send("".join(frames[i:i + 3]))
                else:
                    self.n += 1
                    body = data.encode("utf-8")
                    await self.ws.send(frame("MESSAGE", {"destination": self.dest, "subscription": "msg-0",
                                                         "content-length": str(len(body)),
                                                         "message-id": str(self.n)}, data))
                    await self.ws.send("\n")            # 夹一个心跳换行
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass


PC_DEVICE = {"id": 12, "name": "PC1", "deviceType": {"id": 26, "name": "PC-CLIENT", "iconName": "Client",
                                                    "superDeviceType": {"id": 5, "name": "terminate"}},
             "devicePosition": {"positionX": -0.35, "positionY": -0.05},
             "deviceIfs": [{"id": 122, "name": "GE1/0/0", "ifType": {"id": 2, "name": "GE"}, "status": 1, "serialId": 0}]}


def sandbox_json(sandbox: int = 2, status: dict[int, int] | None = None,
                 links: list[dict[str, Any]] | None = None, pc: bool = False,
                 more: tuple[tuple[int, str], ...] = ()) -> dict[str, Any]:
    """与实测 getOneSandbox 同形的沙箱：T1(6)、T2(7)，接口 MEth0/0/0 + GE0/0/0-9；pc=True 另有 PC1(12，PC-CLIENT)；
    more：另加的 ENSP-AR（(deviceId, 名)，同时运行设备上限的测试用）。"""
    status = status or {6: 4, 7: 4}
    devs = []
    extra = tuple((did, name, 0.1 + 0.2 * i, 200 + 20 * i) for i, (did, name) in enumerate(more))
    for did, name, x, base in ((6, "T1", -0.3, 56), (7, "T2", -0.1, 67)) + extra:
        ifs = [{"id": base, "name": "MEth0/0/0", "ifType": {"id": 4, "name": "MEth"}, "status": 1, "serialId": 0}]
        ifs += [{"id": base + 1 + i, "name": f"GE0/0/{i}", "ifType": {"id": 2, "name": "GE"}, "status": 1,
                 "serialId": i + 1} for i in range(10)]
        devs.append({"id": did, "name": name, "sandboxId": sandbox,
                     "deviceType": {"id": 25, "name": "ENSP-AR", "superDeviceType": {"id": 2, "name": "route"},
                                    "iconName": "AR", "displayName": "AR"},
                     "devicePosition": {"positionX": x, "positionY": -0.2}, "status": status.get(did, 0),
                     "displayStatus": 12, "deviceStartProgress": 100 if status.get(did) in (1, 4) else 40,
                     "deviceIfs": ifs})
    if pc:
        devs.append({**PC_DEVICE, "sandboxId": sandbox, "status": status.get(12, 0), "displayStatus": 12,
                     "deviceStartProgress": 100})
    return {"id": sandbox, "name": "ainet_test", "status": 1, "devices": devs, "links": links or [],
            "graphicsVOList": [], "frames": []}


class FakeRest:
    """httpx.MockTransport 的处理函数：离线登录、会话冲突、沙箱与启停。"""

    def __init__(self, sandbox: int = 2) -> None:
        self.sandbox = sandbox
        self.status = {6: 4, 7: 4}
        self.links: list[dict[str, Any]] = []
        self.session: str | None = None
        self.n = 0
        self.kicked = False
        self.calls: list[tuple[str, str, Any]] = []
        self.logins = 0
        self.logouts = 0
        self.pc = False                      # 沙箱里有 PC1（deviceId 12）
        self.more: tuple[tuple[int, str], ...] = ()   # 另加的 ENSP-AR（deviceId, 名）
        self.attrs: dict[int, dict[str, str]] = {}   # 设备属性（PC 地址配置）
        self.pcs: dict[int, Any] = {}        # deviceId -> FakePC（属性改动同步到假 PC 的命令行）
        self.reboot_405 = False              # batchReboot 返回 HTTP 405（本机 eNSP Pro 实测）
        self.extra: dict[int, dict[str, Any]] = {}   # 其他沙箱（getOneSandbox / getByPage 原样返回）
        self.instant_start = False           # batchStart 后设备直接变成运行中（状态 4）
        # 启动卡住（spec §14 第 21 项）：{deviceId: 次数}——接下来这么多次启动一直停在状态 6（或 stuck_code）
        self.stuck: dict[int, int] = {}
        self.stuck_code = 6
        self.times: list[tuple[float, str, Any]] = []   # (time.monotonic(), path, body)：错开启动 / 停机间隔的测试用

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def kick(self) -> None:
        """同一账号在别处登录：当前会话作废。"""
        self.kicked = True
        self.n += 1
        self.session = f"other-{self.n}"

    def _ok(self, data: Any = None, message: str = "ok") -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "message": message, "data": data})

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, body))
        cookie = req.headers.get("cookie", "")
        if path == "/":
            return httpx.Response(200, text="<html></html>")
        if path == "/api/login":
            self.n += 1
            self.session = f"s{self.n}"
            self.kicked = False
            self.logins += 1
            return httpx.Response(200, text="ok", headers=[("Set-Cookie", f"__Host-SESSION={self.session}; Path=/"),
                                                          ("Set-Cookie", f"csrfToken=tok{self.n}; Path=/")])
        if path == "/api/logout":
            self.logouts += 1
            self.session = None
            return httpx.Response(200, text="bye")
        mine = self.session is not None and f"__Host-SESSION={self.session}" in cookie
        if not mine:
            if self.kicked:
                return httpx.Response(200, json={"code": 50070014, "message": "该账号在其它地方被重新登录", "data": None})
            return httpx.Response(200, json={"code": 50010005, "message": "您尚未登录", "data": None})
        if req.headers.get("X-CSRF-TOKEN") != f"tok{self.n}":
            return httpx.Response(200, json={"code": 403, "message": "csrf", "data": None})
        if path == "/api/getUserInfo":
            return self._ok({"userId": 1, "username": "offline-user"})
        if path == "/service/ensp/sandbox/getByPage":
            # 真机：不带 projectId 返回 code 10000「参数缺失」
            if not body or body.get("projectId") is None:
                return httpx.Response(200, json={"code": 10000, "message": "参数缺失", "data": None})
            rows = [sandbox_json(self.sandbox, self.status, self.links, pc=self.pc, more=self.more), *self.extra.values()]
            return self._ok([{**r, "projectId": 1, "totalPages": 1, "totalElements": len(rows)} for r in rows],
                            "Get ensp sandboxes successfully.")
        if path == "/service/ensp/sandbox/getOneSandbox":
            sid = int(req.url.params.get("sandboxId", 0))
            if sid in self.extra:
                return self._ok(self.extra[sid])
            if sid != self.sandbox:
                return httpx.Response(200, json={"code": 500, "message": "sandbox not found", "data": None})
            return self._ok(sandbox_json(self.sandbox, self.status, self.links, pc=self.pc, more=self.more))
        if path == "/service/ensp/device/batchReboot" and self.reboot_405:
            return httpx.Response(405, text="<html>405 Not Allowed</html>")
        if path in ("/service/ensp/device/property/refreshAttrs", "/service/ensp/device/property/updateAttrs"):
            did = int(req.url.params.get("deviceId", 0))
            cur = self.attrs.setdefault(did, {"device.innerId": "5", "basic.mac": "10:ca:38:eb:c2:9f",
                                              "basic.status": "Ready"})
            if path.endswith("updateAttrs"):
                cur.update({k: str(v) for k, v in (body or {}).items()})
                cur["ipv4.status"] = "PendingUpdate"          # 真机：改完先 PendingUpdate，稍后 Ready
                if did in self.pcs:
                    self.pcs[did].attrs = dict(cur)
                    self.pcs[did].restart_cli()
                return self._ok(json.dumps(cur))
            out = json.dumps(cur)
            cur["ipv4.status"] = "Ready"
            return self._ok(out)
        if path in ("/service/ensp/device/batchStart", "/service/ensp/device/batchStop",
                    "/service/ensp/device/batchReboot"):
            import time as _time
            self.times.append((_time.monotonic(), path, body))
            for d in body["deviceIds"]:
                if path.endswith("Start") or path.endswith("Reboot"):
                    if self.stuck.get(d, 0) > 0:
                        self.stuck[d] -= 1
                        self.status[d] = self.stuck_code
                    else:
                        self.status[d] = 4 if self.instant_start else 6
                else:
                    self.status[d] = 0
            sb = self.extra.get(body.get("sandboxId"))     # 其他沙箱：同步改它设备的状态（同时运行设备上限的测试用）
            if sb is not None:
                for dev in sb.get("devices") or []:
                    if dev.get("id") in body["deviceIds"]:
                        dev["status"] = 0 if path.endswith("Stop") else 4
            return self._ok(None)
        if path == "/service/ensp/link/create":
            self.links.append({"id": len(self.links) + 1, **{k: body[k] for k in
                                                              ("leftDeviceId", "leftIfId", "rightDeviceId", "rightIfId")}})
            return self._ok(None)
        return httpx.Response(200, json={"code": 404, "message": f"no route {path}", "data": None})


# ---- YunShan 命令行（带配置树）：hub / 复位在 YunShan 格式下的测试用 ----

from fake_vrp import ConfigVRP  # noqa: E402

YS_ERASE = "\x1b[16D" + " " * 16 + "\x1b[16D"       # YunShan（AR6700）擦分页符用 16 列（实测）


class YunShanConfigVRP(ConfigVRP):
    """YunShan 风格的 ConfigVRP（按 2026-10 AR6700 V600R025 实测）：设备名缺省 HUAWEI 且配置里显式显示 sysname；
    配置开头是 `!` 注释行，其中 `!Last configuration was updated at …` 每次改配置都会变；分页擦除 ESC[16D；
    `Enter system view, return user view with return command.`；ping 输出带 round-trip 行与只有一个空格的空行。"""

    def __init__(self, config_text: str, host: str = "HUAWEI", page: int = 20) -> None:
        super().__init__(config_text, host=host, page=page)
        self.changes = 0

    def _set_sysname(self, name: str) -> None:
        self.host = name
        self.cfg = [n for n in self.cfg if not n.line.startswith("sysname ")]
        self.cfg.insert(0, self.cr.Node(f"sysname {name}"))

    def config_text(self) -> str:
        out = ["!Software Version V600R025C10SPC100",
               f"!Last configuration was updated at 2026-10-06 07:{self.changes:02d}:00+00:00",
               "!Last configuration was saved at 2026-10-06 06:20:08+00:00"]
        for n in self.cfg:
            out.append("#")
            out += self.cr.render([n])
        out += ["#", "return"]
        return "\r\n".join(out)

    def _next_page(self) -> None:
        page = self.pending_pages.pop(0)
        if self.pending_pages:
            self.emit(YS_ERASE + page + "\r\n  ---- More ----")
        else:
            self.emit(YS_ERASE + page + "\r\n" + self.prompt)

    def _apply(self, cmd: str) -> str | None:
        if cmd == "system-view" and self.view is None:
            self.view = ""
            return "Enter system view, return user view with return command."
        if cmd == "undo sysname" and self.view == "" and not self.path:
            self._set_sysname("HUAWEI")
            self.changes += 1
            return ""
        if cmd.startswith("ping "):
            dst = cmd.split()[-1]
            if self.state.get("ping_ok", False):
                return (f"  PING {dst}: 56  data bytes, press CTRL_C to break\r\n"
                        f"    Reply from {dst}: bytes=56 Sequence=1 ttl=255 time=9 ms\r\n"
                        f"    Reply from {dst}: bytes=56 Sequence=2 ttl=255 time=8 ms\r\n\r\n"
                        f"  --- {dst} ping statistics ---\r\n    2 packet(s) transmitted\r\n"
                        "    2 packet(s) received\r\n    0.00% packet loss\r\n    round-trip min/avg/max = 8/8/9 ms\r\n ")
            return (f"  PING {dst}: 56  data bytes, press CTRL_C to break\r\n    Request time out\r\n"
                    f"    Request time out\r\n\r\n  --- {dst} ping statistics ---\r\n    2 packet(s) transmitted\r\n"
                    "    0 packet(s) received\r\n    100.00% packet loss")
        before = self.config_text()
        out = super()._apply(cmd)
        if self.config_text() != before:
            self.changes += 1
        return out


class TwoStageVRP(YunShanConfigVRP):
    """两阶段生效的 YunShan（CE 交换机等；AR6700 实测为单阶段）：系统视图提示符 [~HUAWEI]，有未提交配置时 [*HUAWEI…]；
    有未提交配置时 return 问 [Y(yes)/N(no)/C(cancel)]。"""

    def __init__(self, config_text: str, **kw) -> None:
        super().__init__(config_text, **kw)
        self.dirty = False
        self.commits = 0

    @property
    def prompt(self) -> str:
        p = super().prompt
        if self.view is None:
            return p
        return "[" + ("*" if self.dirty else "~") + p[1:]

    def _apply(self, cmd: str) -> str | None:
        if cmd == "commit" and self.view is not None:
            self.dirty = False
            self.commits += 1
            return ""
        if cmd == "return" and self.dirty:
            def leave():
                self.dirty = False
                self.view, self.path = None, []
            self.confirm = leave
            self.emit("\r\nWarning: Uncommitted configurations found. Are you sure to commit them before exiting? "
                      "[Y(yes)/N(no)/C(cancel)]:")
            return None
        before = self.config_text()
        out = super()._apply(cmd)
        if self.view is not None and self.config_text() != before:
            self.dirty = True
        return out



class NetYunShan(LoginMixin, NetVRP, YunShanConfigVRP):
    """FakeNet 里的一台 YunShan 设备：带登录流程、配置树，ping 按两端配置计算；ip address x 24 存成点分掩码（与真机一致）。"""

    def __init__(self, config_text: str, **kw) -> None:
        super().__init__(config_text, **kw)
        self.auth = "shell"                 # 缺省已登录过（测试需要时改成 idle）

    def _apply(self, cmd: str) -> str | None:
        m = re.match(r"^ip address (\S+) (\d{1,2})$", cmd)
        if m:
            n = int(m.group(2))
            mask = (0xFFFFFFFF << (32 - n)) & 0xFFFFFFFF
            cmd = f"ip address {m.group(1)} " + ".".join(str((mask >> s) & 255) for s in (24, 16, 8, 0))
        return super()._apply(cmd)


class YunShanSwitch(YunShanConfigVRP):
    """ENSP-LSW（S6700 V600R025C10SPC500，2026-10-06 实测）：
    - 接口只认缩写 GE1/0/x，`interface GigabitEthernet1/0/2` 报 `Error: Wrong parameter found at '^' position.`；
    - 端口缺省 link-type 为 desirable、PVID 1、trunk 列表 1-4094（配置里不显示）；undo port link-type 回到缺省；
    - display port vlan 按配置树计算；配置里没有 configuration file auto-save（交换机不自动保存）。"""

    PORTS = [f"GE1/0/{i}" for i in range(1, 11)]

    def config_text(self) -> str:
        text = super().config_text()
        if not self.vlans:
            return text
        head, sep, rest = text.partition("\r\n#\r\nsysname ")
        line, nl, tail = rest.partition("\r\n")
        return f"{head}{sep}{line}\r\n#\r\nvlan batch {self.cr.vlan_spec(self.vlans)}{nl}{tail}"

    def _apply(self, cmd: str) -> str | None:
        if cmd.startswith("undo vlan batch ") and not self.path:
            ids = self.cr._vlan_ids(cmd[len("undo vlan batch "):])

            def drop() -> None:          # 删 VLAN 时连同 vlan N 视图里的配置一起删（真机如此）
                self.vlans -= ids
                self.cfg = [n for n in self.cfg if self.cr._vlan_block_id(n.line) not in ids]
            self.confirm = drop
            self.emit("\r\nWarning: The configurations of the VLAN will be deleted. Continue? [Y/N]:")
            return None
        if re.match(r"^interface GigabitEthernet", cmd) and self.view is not None:
            return "                  ^\r\nError: Wrong parameter found at '^' position."
        if cmd == "display port vlan":
            rows = ["Port                    Link Type    PVID  Trunk VLAN List                      Port Description",
                    "-" * 111]
            for p in self.PORTS:
                kids = next((n.children for n in self.cfg if n.line == f"interface {p}"), [])
                lines = [k.line for k in kids]
                lt = next((l.split()[-1] for l in lines if l.startswith("port link-type ")), "desirable")
                pvid = next((l.split()[-1] for l in lines if l.startswith(("port default vlan ", "port trunk pvid vlan "))), "1")
                trunk = " ".join(l.split("allow-pass vlan ", 1)[1] for l in lines if "allow-pass vlan" in l)
                tl = "--" if lt == "access" else ("1 " + trunk if lt == "trunk" else "1-4094").strip()
                rows.append(f"{p:<24}{lt:<13}{pvid:>4}  {tl}")
            return "\r\n".join(rows)
        return super()._apply(cmd)
