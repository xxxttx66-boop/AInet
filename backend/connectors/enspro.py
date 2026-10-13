"""EnsproConnector：eNSP Pro 设备 console（STOMP over 原生 WebSocket，spec §10.4）。

协议（实测）：wss://<host>/web.socket/websocket，握手带 Cookie（REST 登录得到的 __Host-SESSION / csrfToken）与 Origin。
1. CONNECT{accept-version:1.2,1.1,1.0, host, heart-beat:0,0} → CONNECTED；
2. SUBSCRIBE /user/topic/console/message/{sb}/{dev} 与 /user/topic/console/heartbeat/{sb}/{dev}；
3. SEND /app/console/connect/{sb}/{dev}（body 空）；
4. 输入：SEND /app/console/message/{sb}/{dev}，body 就是按键文本；设备输出以 MESSAGE 帧的 body 到达（有时一个字符一帧）；
5. 应用层心跳：每 30 秒 SEND /app/console/heartbeat/{sb}/{dev}；关闭：SEND /app/console/close/{sb}/{dev}。

设备登录状态机（connect() 内完成，登录成功后才把通道交给 ConsoleHub）：
- 连上后发**一次**回车；之后只按设备提示应答：
  - `Please configure the login password` / `Enter Password:`（全新设备首次登录）→ 输入密码；`Confirm Password:` → 再输一次；
  - `Password:` → 输入密码，**只试一次**：之后再出现 `Password:`、或出现错误 / 锁定提示，即判定失败——记到 provider
    （provider.block），关闭连接并抛 ConsoleAuthError；之后对该设备的连接一律直接拒绝（不联网、不再输密码），
    直到由 ainet 重新启动该设备或服务重启；
  - 提示符 `<…>` / `[…]` → 登录成功；`[Y/N]`（如登录后询问是否修改密码）→ 回答 N；`---- More ----`（上个会话
    停在分页上的残留）→ q；`Press ENTER` → 回车。
  - 绝不在 `Password:` 后面补空回车（会被设备当成一次失败的登录）。
- 交给 hub 之后，若设备又回到登录提示（console 空闲超时把会话登出、学员在用户视图敲了 quit）：连接器先吞掉学员输入，
  自己按同样的规则登录一次（同样只试一次），成功后恢复转发；失败则 block 并断开。
- 设备 console 密码由 provider.password() 提供（secrets.env 的 ENSPRO_DEVICE_PASSWORD），不进日志、不进异常信息。
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
from typing import Any

from .base import DeviceConnector

log = logging.getLogger("ainet.enspro.console")

# ---- STOMP 帧 ----


def frame(cmd: str, headers: dict[str, str] | None = None, body: str = "") -> str:
    return cmd + "\n" + "".join(f"{k}:{v}\n" for k, v in (headers or {}).items()) + "\n" + body + "\x00"


class StompParser:
    """把 WebSocket 消息流拆成 STOMP 帧：一条消息可含多帧、一帧可跨消息；只有换行的消息是心跳；
    带 content-length 时按字节数取 body（body 里可以有 NUL）。"""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: str | bytes) -> list[tuple[str, dict[str, str], str]]:
        self.buf += data.encode("utf-8") if isinstance(data, str) else data
        out: list[tuple[str, dict[str, str], str]] = []
        while True:
            b = self.buf.lstrip(b"\r\n")           # 帧之间 / 心跳的换行
            self.buf = b
            if not b:
                break
            sep = b.find(b"\n\n")
            if sep < 0:
                break
            head = b[:sep].decode("utf-8", "replace").replace("\r", "").split("\n")
            headers: dict[str, str] = {}
            for line in head[1:]:
                k, c, v = line.partition(":")
                if c and k not in headers:          # STOMP：重复头以第一个为准
                    headers[k] = v
            start = sep + 2
            clen = headers.get("content-length")
            if clen is not None and clen.strip().isdigit():
                end = start + int(clen)
                if len(b) < end + 1:
                    break
                body = b[start:end]
                rest = b[end + 1:] if b[end:end + 1] == b"\x00" else b[end:]
            else:
                nul = b.find(b"\x00", start)
                if nul < 0:
                    break
                body = b[start:nul]
                rest = b[nul + 1:]
            out.append((head[0].strip(), headers, body.decode("utf-8", "replace")))
            self.buf = rest
        return out


# ---- 设备输出识别（登录阶段） ----

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 设备提示符 <…> / […]；eNSP Pro 的 PC（PC-CLIENT 等终端）是 `PC>`（2026-10-06 实测，无需登录）
PROMPT_RE = re.compile(r"^(?:[<\[][^\s<>\[\]]+[>\]]|[A-Za-z][\w-]{0,31}>)\s*$")
FIRST_PW_RE = re.compile(r"configure the login password|Enter Password:\s*$", re.I)
CONFIRM_PW_RE = re.compile(r"Confirm Password:\s*$", re.I)
PASSWORD_RE = re.compile(r"Password:\s*$", re.I)
USERNAME_RE = re.compile(r"(Username|login):\s*$", re.I)
YN_RE = re.compile(r"\[\s*Y\s*(\(yes\))?\s*/\s*N\s*(\(no\))?.*\]\s*:?\s*$", re.I)
MORE_RE = re.compile(r"---- More ----\s*$")
ENTER_RE = re.compile(r"Press ENTER", re.I)
AUTH_FAIL_RE = re.compile(r"(authentication fail|password.*(incorrect|wrong|invalid|error)|"
                          r"\block(ed)?\b|too many|Error:)", re.I)


def last_line(text: str) -> str:
    t = _ANSI.sub("", text).replace("\r\n", "\n")
    line = t.rsplit("\n", 1)[-1]
    if "\r" in line:                                # \r 回到行首：取最后一段（覆盖写）
        line = line.rsplit("\r", 1)[-1] or line
    return line.rstrip(" ") if line.strip() else ""


def last_nonempty(text: str) -> str:
    t = _ANSI.sub("", text).replace("\r", "")
    for line in reversed(t.split("\n")):
        if line.strip():
            return line.rstrip()
    return ""


class ConsoleAuthError(Exception):
    """console 登录失败（密码错误 / 设备被锁 / 已判定失败不再尝试）。"""


class ConsoleLoginError(Exception):
    """console 登录没走完（超时、需要用户名、没有配置密码等），不算密码错误。"""


# ---- 连接器 ----

class EnsproConnector(DeviceConnector):
    def __init__(self, provider: Any, device_id: int, *, ws_connect: Any = None, ssl_ctx: ssl.SSLContext | None = None,
                 heartbeat: float | None = None, login_timeout: float | None = None, quiet: float = 0.4,
                 open_timeout: float = 15.0) -> None:
        import config
        self.provider = provider
        self.device_id = int(device_id)
        self.sandbox_id = provider.sandbox_id
        self.heartbeat = config.ENSPRO_HEARTBEAT if heartbeat is None else heartbeat
        self.login_timeout = config.ENSPRO_LOGIN_TIMEOUT if login_timeout is None else login_timeout
        self.quiet = quiet
        self.open_timeout = open_timeout
        self._ws_connect = ws_connect
        self._ssl = ssl_ctx
        self.ws: Any = None
        self._parser = StompParser()
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()   # 交给 hub 的设备输出；None = EOF
        self._login_buf = ""                     # 登录阶段收到的输出
        self._login_event = asyncio.Event()
        self._handed = False                     # 已交给 hub
        self._relogin = False                    # 交给 hub 之后的自动重登进行中：吞掉学员输入
        self._tail = ""                          # 交给 hub 之后的最近输出（识别又回到登录提示）
        self._watch: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self.error = ""
        self._closed = False
        self.login_log: list[str] = []          # 登录过程的动作（测试 / 诊断；不含密码）

    # ---- 目的地 ----
    def _dest(self, kind: str, app: bool = True) -> str:
        return (f"/app/console/{kind}/{self.sandbox_id}/{self.device_id}" if app
                else f"/user/topic/console/{kind}/{self.sandbox_id}/{self.device_id}")

    async def _send_frame(self, cmd: str, headers: dict[str, str], body: str = "") -> None:
        if self.ws is None:
            raise ConnectionError("console 未连接")
        await self.ws.send(frame(cmd, headers, body))

    async def _type(self, text: str) -> None:
        await self._send_frame("SEND", {"destination": self._dest("message")}, text)

    # ---- 连接 ----

    async def connect(self) -> None:
        why = self.provider.blocked(self.device_id)
        if why:
            raise ConsoleAuthError(f"该设备 console 登录已失败过，不再自动尝试（{why}）。"
                                   f"请确认设备 console 密码 {getattr(self.provider, 'secret_name', 'ENSPRO_DEVICE_PASSWORD')} 后重启 ainet，"
                                   "或在拓扑图上重新启动该设备")
        headers = await self.provider.ws_headers()      # 会话失效时抛 EnsproSessionLost
        url = self.provider.client.ws_url + "/web.socket/websocket"
        connect = self._ws_connect
        kw: dict[str, Any] = {}
        if connect is None:
            import websockets
            connect = websockets.connect
            if url.startswith("wss://"):
                ctx = self._ssl or ssl.create_default_context()
                if self._ssl is None:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                kw["ssl"] = ctx
        try:
            self.ws = await asyncio.wait_for(connect(url, additional_headers=headers, max_size=None, **kw),
                                             self.open_timeout)
            host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
            await self._send_frame("CONNECT", {"accept-version": "1.2,1.1,1.0", "host": host, "heart-beat": "0,0"})
            self._recv_task = asyncio.create_task(self._recv_loop())
            await asyncio.wait_for(self._connected.wait(), self.open_timeout)
            if self.error:
                raise ConsoleLoginError(self.error)
            await self._send_frame("SUBSCRIBE", {"id": "msg-0", "destination": self._dest("message", False)})
            await self._send_frame("SUBSCRIBE", {"id": "hb-0", "destination": self._dest("heartbeat", False)})
            await self._send_frame("SEND", {"destination": self._dest("connect")})
            if self.heartbeat > 0:
                self._hb_task = asyncio.create_task(self._heartbeat_loop())
            await self._login(first=True)
        except BaseException:
            await self.close()
            raise
        self._handed = True
        # 登录步骤（enter / set-password / confirm-password / password / prompt …，不含密码）便于确认首次设密码等流程
        log.info("设备 %s console 登录完成：%s", self.device_id, " → ".join(self.login_log))

    async def _recv_loop(self) -> None:
        try:
            async for msg in self.ws:
                for cmd, headers, body in self._parser.feed(msg):
                    if cmd == "CONNECTED":
                        self._connected.set()
                    elif cmd == "ERROR":
                        self.error = f"STOMP ERROR：{headers.get('message', '')} {body[:200]}".strip()
                        log.warning("设备 %s console：%s", self.device_id, self.error)
                        self._connected.set()
                    elif cmd == "MESSAGE" and "/console/message/" in headers.get("destination", ""):
                        self._on_output(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                    # 连接断开
            if not self.error:
                self.error = f"console 连接断开：{type(exc).__name__}"
        finally:
            self._connected.set()
            self._login_event.set()
            self._out.put_nowait(None)

    def _on_output(self, body: str) -> None:
        if not body:
            return
        if not self._handed or self._relogin:
            self._login_buf += body
            self._login_event.set()
        if self._handed:
            self._tail = (self._tail + body)[-2000:]
            self._out.put_nowait(body.encode("utf-8"))
            if not self._relogin and not self._closed and self._login_prompt_tail(self._tail):
                self._relogin = True                # 从这一刻起吞掉学员输入
                self._watch = asyncio.get_running_loop().create_task(self._relogin_task())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat)
                await self._send_frame("SEND", {"destination": self._dest("heartbeat")})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ---- 登录状态机 ----

    async def _settle(self, mark: int, deadline: float, idle: float = 3.0) -> str:
        """等 mark 之后出现新输出并静默 quiet 秒，返回 mark 之后的输出；idle 秒内没有任何新输出返回空串。"""
        loop = asyncio.get_running_loop()
        idle_end = min(deadline, loop.time() + idle)
        while True:
            if self.ws is None or (self._recv_task is not None and self._recv_task.done()):
                break
            self._login_event.clear()
            seg = self._login_buf[mark:]
            limit = deadline if seg else idle_end
            if loop.time() >= limit:
                break
            try:
                await asyncio.wait_for(self._login_event.wait(),
                                       min(self.quiet, limit - loop.time()) if seg else limit - loop.time())
                continue
            except asyncio.TimeoutError:
                if seg:
                    return seg
        return self._login_buf[mark:]

    async def _login(self, first: bool) -> None:
        """见模块说明。first=True：刚连上（先发一次回车）；False：交给 hub 之后又回到登录提示。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.login_timeout
        pw_sent = False             # 已在 Password: 后输过一次密码
        set_state = ""              # "" / "enter"（首次设密码已输第一遍）/ "confirm"（已确认）
        nudges = 0
        mark = len(self._login_buf)
        if first:
            # 订阅后服务端可能先吐出缓冲的输出：稍等再看，然后按约定发一次回车
            await asyncio.sleep(self.quiet)
            cur = last_line(self._login_buf)
            if PASSWORD_RE.search(cur) or FIRST_PW_RE.search(cur) or CONFIRM_PW_RE.search(cur):
                # 服务端回放显示设备已经停在密码提示上：此时回车 = 提交空密码，跳过，直接按提示应答
                mark = max(0, len(self._login_buf) - 200)
                self.login_log.append("at-password")
            else:
                mark = len(self._login_buf)
                await self._type("\r")
                self.login_log.append("enter")
        else:
            mark = max(0, len(self._login_buf) - 200)
        while True:
            if loop.time() >= deadline:
                shown = last_nonempty(self._login_buf)[-80:]
                log.warning("设备 %s console 登录超时，最后一行：%r", self.device_id, shown)
                raise ConsoleLoginError("console 登录超时（设备没有出现提示符）"
                                        + (f"，最后一行：{shown!r}" if shown else "，设备没有任何输出（刚启动的设备 console 可能要几分钟才响应，请稍后重试）"))
            if self.error and (self._recv_task is None or self._recv_task.done()):
                raise ConsoleLoginError(self.error)
            seg = await self._settle(mark, deadline)
            line = last_line(seg) or last_line(self._login_buf)
            text = _ANSI.sub("", seg)
            if not seg.strip():
                # 没有任何新输出：最后一行不是登录提示时补一次回车唤醒（绝不在 Password: 后补空回车）
                cur = last_line(self._login_buf)
                if nudges < 2 and not (PASSWORD_RE.search(cur) or USERNAME_RE.search(cur)):
                    nudges += 1
                    mark = len(self._login_buf)
                    await self._type("\r")
                    self.login_log.append("enter")
                    continue
                continue
            mark = len(self._login_buf)
            if PROMPT_RE.match(line):
                self.login_log.append("prompt")
                if first:
                    # 只把提示符交给 hub（登录过程的输出不进学员终端）
                    self._out.put_nowait(("\r\n" + line).encode("utf-8"))
                return
            if MORE_RE.search(line):
                await self._type("q")
                self.login_log.append("more")
                continue
            if YN_RE.search(line):
                # 登录后询问（如是否修改密码）：回答 N
                await self._type("N\r")
                self.login_log.append("no")
                continue
            if CONFIRM_PW_RE.search(line):
                if set_state != "enter":
                    raise self._fail("设备要求确认密码，但首次设置密码的流程不完整")
                await self._type(self._password() + "\r")
                set_state = "confirm"
                self.login_log.append("confirm-password")
                continue
            if FIRST_PW_RE.search(line) or (FIRST_PW_RE.search(text) and PASSWORD_RE.search(line)):
                if set_state or pw_sent:
                    raise self._fail("设置 / 确认的密码未被设备接受（可能不满足复杂度要求）：" + last_nonempty(text)[:120])
                await self._type(self._password() + "\r")
                set_state = "enter"
                self.login_log.append("set-password")
                continue
            if PASSWORD_RE.search(line):
                if pw_sent or set_state:
                    raise self._fail("密码错误（设备再次要求输入密码）")
                await self._type(self._password() + "\r")
                pw_sent = True
                self.login_log.append("password")
                continue
            if (pw_sent or set_state) and AUTH_FAIL_RE.search(text):
                raise self._fail("设备拒绝登录：" + last_nonempty(text)[:120])
            if USERNAME_RE.search(line):
                raise ConsoleLoginError("设备要求输入用户名（console 配置了 AAA 认证），ainet 暂不支持")
            if ENTER_RE.search(text) and not (pw_sent or set_state):
                await self._type("\r")
                self.login_log.append("enter")
                continue
            # 其他输出（横幅、日志……）：继续等

    def _password(self) -> str:
        pw = self.provider.password()
        if not pw:
            raise ConsoleLoginError(f"没有配置设备 console 密码（{getattr(self.provider, 'secret_name', 'ENSPRO_DEVICE_PASSWORD')}："
                                    "secrets.env、环境变量，或课程页「⚙ 接入设置」）")
        return pw

    def _fail(self, reason: str) -> ConsoleAuthError:
        self.provider.block(self.device_id, reason)
        self.login_log.append("auth-failed")
        return ConsoleAuthError(f"console 登录失败：{reason}（只尝试一次，不再重试）")

    @staticmethod
    def _login_prompt_tail(tail: str) -> bool:
        """输出停在登录提示上：Press ENTER / 首次设密码 / 单独一行的 Password:（前一行不是「提示符 + 命令」——
        那是命令自己在要密码，不是登录）。

        真机（2026-10-06，S6700 / AR6700）登出后输出 `User interface con0 is available` + 空行 + `Please Press ENTER.`
        + 两个空行，最后一行是空的：Press ENTER 按最后一个非空行判断，提示一出现就接管（自己回车、输密码），
        学员此时敲的字符不会落到之后的 Password: 上。"""
        line = last_line(tail)
        if ENTER_RE.search(line) or FIRST_PW_RE.search(line):
            return True
        if not line and ENTER_RE.search(last_nonempty(tail)):
            return True
        if not re.fullmatch(r"\s*Password:\s*", line, re.I):
            return False
        lines = [l for l in _ANSI.sub("", tail).replace("\r", "").split("\n")[:-1] if l.strip()]
        prev = lines[-1].strip() if lines else ""
        return not re.match(r"^[<\[][^\s<>\[\]]+[>\]]\s*\S", prev)

    async def _relogin_task(self) -> None:
        """交给 hub 之后设备回到登录提示：自动登录一次（同样只试一次）。学员输入从提示出现那一刻起就被吞掉
        （_on_output 里同步置位 _relogin），不会混进密码。"""
        try:
            await asyncio.sleep(self.quiet)
            if not self._login_prompt_tail(self._tail):     # 只是输出里出现过、随后又有别的输出：不是登录提示
                self._relogin = False
                return
            self._login_buf = self._tail
            self._tail = ""
            try:
                mark = len(self.login_log)
                await self._login(first=False)
                log.info("设备 %s 回到登录提示，已自动重新登录：%s", self.device_id,
                         " → ".join(self.login_log[mark:]))
            except (ConsoleAuthError, ConsoleLoginError) as exc:
                self.error = str(exc)
                self._out.put_nowait(f"\r\n[ainet] {exc}\r\n".encode("utf-8"))
                await self.close()
                return
            finally:
                self._relogin = False
        except asyncio.CancelledError:
            pass

    # ---- DeviceConnector ----

    async def send(self, data: bytes) -> None:
        if self.ws is None:
            raise RuntimeError("connector not connected")
        if self._relogin:
            return                                  # 自动重登中：学员输入不能落到 Password: 上
        await self._type(data.decode("utf-8", "replace"))

    async def read(self) -> bytes:
        item = await self._out.get()
        if item is None:
            self._out.put_nowait(None)              # 之后的 read 也返回 EOF
            return b""
        chunks = [item]
        while not self._out.empty():                # 一个字符一帧时合并成一块
            nxt = self._out.get_nowait()
            if nxt is None:
                self._out.put_nowait(None)
                break
            chunks.append(nxt)
        return b"".join(chunks)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for t in (self._watch, self._hb_task):
            if t is not None and t is not asyncio.current_task():
                t.cancel()
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.send(frame("SEND", {"destination": self._dest("close")}))
                await ws.send(frame("DISCONNECT", {}))
            except Exception:
                pass
            try:
                await asyncio.wait_for(ws.close(), 3)
            except Exception:
                pass
        if self._recv_task is not None and self._recv_task is not asyncio.current_task():
            self._recv_task.cancel()
        self._out.put_nowait(None)
