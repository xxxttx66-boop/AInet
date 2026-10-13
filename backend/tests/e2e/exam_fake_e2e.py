"""考试 e2e 的模拟设备版（不连任何真实设备，不进默认 pytest）。

在本机起 3 台假 VRP（tests/fake_vrp.py 的 NetVRP：带配置树，ping 按各设备配置计算）监听
127.0.0.1:2000（AR0）/ 2001（LSW0）/ 2003（AR2），再以 ENSP_HOST=127.0.0.1、AINET_ENSP_RUNNING_IDS=三台 GUID
起服务（临时库、屏蔽 DeepSeek Key：评分降级为只给结果分），然后跑 exam_e2e.run()（同一套浏览器步骤）。

用法：python tests/e2e/exam_fake_e2e.py [截图目录] [单题时长秒，缺省 45]
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BACKEND, "tests"))
sys.path.insert(0, BACKEND)

from fake_vrp import FakeNet, NetVRP  # noqa: E402
import exam_e2e  # noqa: E402

GUIDS = {"AR0": "B3B8A475-25C8-4a6c-B29D-F49A6F4F9F0E", "AR2": "14D97C46-57BE-4d7a-BFB6-696F4390C76B",
         "LSW0": "DE880FB4-C0C7-4dc4-848F-7BEB833A9BB4"}
PORTS = {"AR0": 2000, "LSW0": 2001, "AR2": 2003}
CFG = os.path.join(BACKEND, "tests", "fixtures", "configs")
HTTP_PORT = int(os.environ.get("AINET_E2E_HTTP_PORT", "8781"))


class _TcpConn:
    def __init__(self) -> None:
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False


async def serve(dev: NetVRP, port: int):
    async def handle(reader, writer):
        conn = _TcpConn()
        dev.conns.append(conn)

        async def pump():
            while True:
                data = await conn.q.get()
                writer.write(data)
                await writer.drain()
        t = asyncio.create_task(pump())
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                dev.feed(data)
        finally:
            t.cancel()
            conn.closed = True
    return await asyncio.start_server(handle, "127.0.0.1", port)


def build_net() -> FakeNet:
    ar = open(os.path.join(CFG, "ar1220_factory.cfg"), encoding="utf-8").read()
    sw = open(os.path.join(CFG, "s5700_factory.cfg"), encoding="utf-8").read()
    net = FakeNet([(("AR0", "GigabitEthernet0/0/1"), ("AR2", "GigabitEthernet0/0/0")),
                   (("AR0", "GigabitEthernet0/0/0"), ("LSW0", "GigabitEthernet0/0/1"))])
    net.add("AR0", NetVRP("#\nsysname AR0\n" + ar))
    net.add("AR2", NetVRP("#\nsysname AR2\n" + ar))
    net.add("LSW0", NetVRP(sw.replace("sysname Huawei", "sysname LSW0")))
    return net


async def main() -> int:
    shot_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "exam_fake_shots")
    qtime = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    os.makedirs(shot_dir, exist_ok=True)
    net = build_net()
    servers = [await serve(net.devs[n], p) for n, p in PORTS.items()]
    db_path = os.path.join(shot_dir, "exam_fake.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env.update(ENSP_HOST="127.0.0.1", AINET_TOPO_DIRS=os.path.join(BACKEND, "tests", "fixtures", "topos"),
               AINET_ENSP_RUNNING_IDS=",".join(GUIDS.values()), AINET_ENSP_CLIENT="1", AINET_ENSP_TOPO_HIS="",
               AINET_ENSP_DEVICES_DIRS="", AINET_DB_PATH=db_path, AINET_HUB_MIN_RECONNECT="0",
               AINET_SECRETS_FILE=os.path.join(shot_dir, "no-such-secrets.env"), AINET_EXAM_TICK="0.5",
               AINET_EXAM_RESET_RETRY="2",
               # 课程页会探测全部环境：eNSP Pro 指向本机关闭的端口（离线），绝不连真 eNSP Pro 虚机
               AINET_ENSPRO_URL="http://127.0.0.1:9")
    log = open(os.path.join(shot_dir, "server.log"), "w")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(HTTP_PORT)], cwd=BACKEND,
                           env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{HTTP_PORT}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/login")
                break
            except OSError:
                await asyncio.sleep(0.2)
        t0 = time.time()
        rc = await exam_e2e.run(base, shot_dir, qtime=qtime)
        print(f"  用时 {int(time.time() - t0)} 秒；服务日志：{os.path.join(shot_dir, 'server.log')}")
        for n in ("AR0", "AR2", "LSW0"):
            d = net.devs[n]
            print(f"  {n} 执行过的命令数：{len(d.executed)}；当前提示符 {d.prompt}")
        return rc
    finally:
        srv.terminate()
        for s in servers:
            s.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
