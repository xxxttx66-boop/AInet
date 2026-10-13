"""DeviceConnector 抽象接口（spec §5）。

对上层屏蔽模拟器差异：eNSP 驱动连本机 2000+，未来 EVE-NG 驱动连
EVE 服务器 telnet 端口，二者都实现同一组异步方法。上层（终端桥接、
出题、评分、教学）只对本接口编程。
"""
from __future__ import annotations

import abc


class DeviceConnector(abc.ABC):
    """设备 console 连接的异步抽象。

    典型用法（全双工终端桥接）：
        await conn.connect()
        # 任务 A：while True: data = await conn.read(); ws.send(data)
        # 任务 B：ws 收到字节 -> await conn.send(data)
        await conn.close()
    """

    @abc.abstractmethod
    async def connect(self) -> None:
        """建立到设备 console 的连接，完成必要的协议协商。"""

    @abc.abstractmethod
    async def send(self, data: bytes) -> None:
        """向设备写入原始字节（如键盘输入、命令）。"""

    @abc.abstractmethod
    async def read(self) -> bytes:
        """读取设备输出的一段原始字节（已剥离协议控制字节）。

        返回空 bytes 表示连接已关闭 / EOF。
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。可重复调用。"""
