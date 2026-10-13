"""EnspConnector：asyncio 连 eNSP 设备 console（裸 telnet）。

- 用 asyncio.open_connection 直连 host:port（禁用 pexpect / netmiko，Windows 无 pty）。
- 处理 telnet IAC 协商：对收到的 DO 回 WONT，对 WILL 回 DONT，保持裸管道。
- 把 IAC 控制序列从数据流中剥离，只把纯净字节交给上层。
- IAC 可能跨 read 边界，内部用状态机缓存半截序列。
"""
from __future__ import annotations

import asyncio

from .base import DeviceConnector

# telnet 控制字节
IAC = 255   # 0xFF 命令引导
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250    # 子协商开始
SE = 240    # 子协商结束

_READ_CHUNK = 4096


class EnspConnector(DeviceConnector):
    def __init__(self, host: str, port: int, connect_timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # IAC 状态机缓存（跨 read 边界的半截序列）
        self._pending = bytearray()
        self._in_subneg = False

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("connector not connected")
        # 数据中的裸 0xFF 需转义为 IAC IAC，避免被设备当成命令引导
        if b"\xff" in data:
            data = data.replace(b"\xff", b"\xff\xff")
        self._writer.write(data)
        await self._writer.drain()

    async def read(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("connector not connected")
        raw = await self._reader.read(_READ_CHUNK)
        if not raw:
            return b""  # EOF
        clean, responses = self._process(raw)
        if responses and self._writer is not None:
            self._writer.write(responses)
            await self._writer.drain()
        # read 返回空但连接未断（整块都是协商）时，继续读直到拿到数据或 EOF
        while not clean:
            raw = await self._reader.read(_READ_CHUNK)
            if not raw:
                return b""
            clean, responses = self._process(raw)
            if responses and self._writer is not None:
                self._writer.write(responses)
                await self._writer.drain()
        return clean

    def _process(self, data: bytes) -> tuple[bytes, bytes]:
        """消费一段原始字节，返回 (纯净数据, 要回发的协商响应)。

        对 DO 回 WONT，对 WILL 回 DONT；子协商整体丢弃；IAC IAC 还原为 0xFF。
        半截序列（结尾处不完整的 IAC 命令）存入 self._pending 待下次拼接。
        """
        buf = self._pending + data
        self._pending = bytearray()
        out = bytearray()
        resp = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            b = buf[i]
            if self._in_subneg:
                # 子协商中：等待 IAC SE 结束，期间字节全丢
                if b == IAC:
                    if i + 1 >= n:
                        self._pending = bytearray(buf[i:])
                        break
                    if buf[i + 1] == SE:
                        self._in_subneg = False
                        i += 2
                        continue
                    # IAC IAC 在子协商内，跳过两字节
                    i += 2
                    continue
                i += 1
                continue
            if b == IAC:
                if i + 1 >= n:
                    self._pending = bytearray(buf[i:])
                    break
                cmd = buf[i + 1]
                if cmd == IAC:
                    out.append(IAC)  # 转义的字面 0xFF
                    i += 2
                    continue
                if cmd == SB:
                    self._in_subneg = True
                    i += 2
                    continue
                if cmd in (DO, DONT, WILL, WONT):
                    if i + 2 >= n:
                        self._pending = bytearray(buf[i:])
                        break
                    opt = buf[i + 2]
                    if cmd == DO:
                        resp += bytes([IAC, WONT, opt])
                    elif cmd == WILL:
                        resp += bytes([IAC, DONT, opt])
                    # 对设备主动发来的 DONT/WONT 不必回应
                    i += 3
                    continue
                # 其它单字节命令（NOP/GA 等），跳过 IAC+cmd
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out), bytes(resp)

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            finally:
                self._writer = None
                self._reader = None
