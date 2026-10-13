"""假 eNSP Pro 版实验环境（不连任何真实设备 / eNSP Pro 虚机，不进默认 pytest）：同一个进程里跑
假 STOMP console 服务（tests/fake_enspro.py）+ 假 REST（httpx.MockTransport，设备启动有延时）+ ainet 服务（uvicorn）。

- 沙箱 ainet_test（id 2）：T1（6）、T2（7）两台 YunShan AR（NetYunShan，GE0/0/1 互联，ping 按配置计算）+ PC1（12，PC-CLIENT）。
  设备开始时全部停止；batchStart 后状态 2（分配资源）→ 6（启动系统）→ 4（运行），用时 start_delay 秒，便于看准备进度。
- 场景目录：仓库 scenarios/ 的副本 + 一道只给 e2e 用的题 e2e-pc-t1t2（T1 / T2 / PC1 都开放，PC1 在 hosts 里），
  用于在浏览器里验证 PC 设置气泡（练习 / 考试中的修改进操作记录、结束时随复位还原）。
- eNSP 经典版：AINET_ENSP_CLIENT=0（离线），课程页显示「（离线）」。
- AINET_ENSPRO_URL 指向假服务的端口（课程页的在线探测只做 TCP 连接）。

用法：
  python tests/e2e/fake_enspro_lab.py [HTTP 端口，缺省 8796]          # 起好后一直运行，Ctrl-C 结束
  其他脚本：async with fake_enspro_lab(dir, port) as lab: ...（lab.base、lab.rest、lab.devices、lab.delayed.stuck）
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shutil
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
REPO = os.path.dirname(BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))
sys.path.insert(0, BACKEND)

PW = "Fake-Pw#1234"
E2E_SCENARIO = {
    "id": "e2e-pc-t1t2", "title": "T1 与 T2 互联不通（PC 气泡 e2e）", "level": 1, "platform": "yunshan",
    "chapter": "L02", "difficulty": 1, "est_minutes": 10, "summary": "T1 ping 不通 T2，PC1 也在同一个沙箱里",
    "tags": ["IP 编址", "PC 设置"], "knowledge_tags": ["IP 地址规划"],
    "topology": "ainet_test", "required_devices": ["T1", "T2", "PC1"], "accessible_devices": ["T1", "T2", "PC1"],
    "hosts": ["PC1"],
    "symptom": "工单：T1 与 T2 之间有一条直连链路（T1 GE0/0/1 — T2 GE0/0/1），规划网段为 10.0.12.0/24，T1 一端为 10.0.12.1，"
               "T2 一端为 10.0.12.2。现在从 T1 上 ping 10.0.12.2 不通。PC1 是同一沙箱里的用户电脑（10.0.10.10/24）。",
    "baseline": {"T1": ["system-view", "interface GE0/0/1", "ip address 10.0.12.1 24"],
                 "T2": ["system-view", "interface GE0/0/1", "ip address 10.0.12.2 24"],
                 "PC1": ["ip address 10.0.10.10 24", "ip gateway 10.0.10.1"]},
    "inject": {"T2": ["system-view", "interface GE0/0/1", "ip address 10.0.21.2 24"]},
    "restore": {"T1": ["system-view", "interface GE0/0/1", "undo ip address"],
                "T2": ["system-view", "interface GE0/0/1", "undo ip address"],
                "PC1": ["undo ip address", "undo ip gateway"]},
    "solution": {"T2": ["system-view", "interface GE0/0/1", "ip address 10.0.12.2 24"]},
    "checks": [{"device": "T1", "command": "ping -c 2 10.0.12.2", "expect": "\\b[12] packet\\(s\\) received",
                "desc": "T1 能 ping 通 T2"}],
    "hints": ["先确认两端接口地址是否在同一网段", "display ip interface brief", "把 T2 的地址改回规划值"],
    "reference_path": "两端 display ip interface brief 对照规划。",
}


class DelayedRest:
    """在 FakeRest 外面包一层：batchStart 之后按时间推进状态 2 → 6 → 4（真机：分配资源 → 启动系统 → 运行）。

    stuck {设备: n}：模拟真机的启动卡住（spec §14 第 21 项），该设备一直停在 6，直到被停止 n 次之后才正常启动
    （看门狗「停止 → 启动」n 次后恢复；n 很大 = 一直卡住）。可在运行中改。"""

    def __init__(self, rest, delay: float, stuck: dict[int, int] | None = None) -> None:
        self.rest = rest
        self.delay = delay
        self.stuck: dict[int, int] = dict(stuck or {})
        self.started: dict[int, float] = {}
        inner = rest.handle

        def handle(req):
            self._tick()
            path = req.url.path
            if path in ("/service/ensp/device/batchStart", "/service/ensp/device/batchReboot"):
                body = json.loads(req.content) if req.content else {}
                for d in body.get("deviceIds", []):
                    self.started[d] = time.time()
            if path == "/service/ensp/device/batchStop":
                body = json.loads(req.content) if req.content else {}
                for d in body.get("deviceIds", []):
                    if self.stuck.get(d, 0) > 0:
                        self.stuck[d] -= 1
            r = inner(req)
            if path in ("/service/ensp/device/batchStart", "/service/ensp/device/batchReboot"):
                for d in self.started:
                    if rest.status.get(d) == 6 and time.time() - self.started[d] < 0.1:
                        rest.status[d] = 2
            return r
        rest.handle = handle

    def _tick(self) -> None:
        now = time.time()
        for d, t0 in list(self.started.items()):
            st = self.rest.status.get(d)
            if st in (2, 6):
                el = now - t0
                self.rest.status[d] = 4 if el >= self.delay and self.stuck.get(d, 0) <= 0 else (
                    6 if el >= min(2.0, self.delay / 3) else 2)
                if self.rest.status[d] == 4:
                    self.started.pop(d, None)


def _scenarios_dir(work: str) -> str:
    d = os.path.join(work, "scenarios")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for f in glob.glob(os.path.join(REPO, "scenarios", "*.json")):
        shutil.copy(f, d)
    with open(os.path.join(d, "e2e-pc-t1t2.json"), "w", encoding="utf-8") as f:
        json.dump(E2E_SCENARIO, f, ensure_ascii=False, indent=2)
    return d


@contextlib.asynccontextmanager
async def fake_enspro_lab(work_dir: str, http_port: int, *, start_delay: float = 8.0, running: bool = False,
                          stuck: dict[int, int] | None = None):
    os.makedirs(work_dir, exist_ok=True)
    db_path = os.path.join(work_dir, "fake_enspro_lab.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    # config 在导入时读环境变量：先设好再导入任何后端模块（AINET_ENSPRO_URL 等假服务起来后直接改 config）
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.update(
        AINET_SECRETS_FILE=os.path.join(work_dir, "no-such-secrets.env"), AINET_DB_PATH=db_path,
        AINET_SCENARIOS_DIR=_scenarios_dir(work_dir), AINET_LAB_PROVIDER="enspro", AINET_ENSPRO_SANDBOX="2",
        AINET_ENSPRO_URL="http://127.0.0.1:9", AINET_ENSP_CLIENT="0", AINET_ENSP_TOPO_HIS="",
        AINET_ENSP_DEVICES_DIRS="", AINET_TOPO_DIRS=os.path.join(BACKEND, "tests", "fixtures", "topos"),
        AINET_HUB_MIN_RECONNECT="0", AINET_PRACTICE_SEED="0", AINET_EXAM_TICK="0.5", AINET_EXAM_RESET_RETRY="2",
        # 在线探测要求配置了设备 console 密码（只看有无，spec §5.2「接入设置」）：给假设备的假密码
        ENSPRO_DEVICE_PASSWORD=PW)
    from fake_enspro import FakePC, FakeRest, FakeStompServer, NetYunShan
    from fake_vrp import FakeNet
    base_cfg = open(os.path.join(BACKEND, "tests", "fixtures", "configs", "yunshan_ar6700_base.cfg"),
                    encoding="utf-8").read()
    net = FakeNet([(("T1", "GE0/0/1"), ("T2", "GE0/0/1"))])
    t1 = net.add("T1", NetYunShan(base_cfg, password=PW))
    t2 = net.add("T2", NetYunShan(base_cfg, password=PW))
    pc1 = FakePC(reachable=set())
    devices = {6: t1, 7: t2, 12: pc1}
    server = await FakeStompServer(devices).start()
    rest = FakeRest()
    rest.pc = True
    rest.pcs = {12: pc1}
    rest.links = [{"id": 5, "leftDeviceId": 6, "leftIfId": 58, "rightDeviceId": 7, "rightIfId": 69}]
    rest.status = {6: 4, 7: 4, 12: 4} if running else {6: 0, 7: 0, 12: 0}
    delayed = DelayedRest(rest, start_delay, stuck)

    import config
    config.ENSPRO_URL = f"http://127.0.0.1:{server.port}"      # 课程页在线探测：对假服务端口做 TCP 连接
    import uvicorn
    import lab as lab_lib
    import main
    from enspro import EnsproClient, EnsproProvider

    def make(kind=None, **kw):
        kind = kind or "enspro"
        if kind != "enspro":
            return lab_lib.EnspProvider()
        client = EnsproClient(f"http://127.0.0.1:{server.port}", transport=rest.transport())
        p = EnsproProvider(client=client, sandbox=kw.get("sandbox", 2), password=lambda: PW, cache_ttl=0,
                           quiet=0.15, login_timeout=8, heartbeat=0)
        p.stop_poll = 0.2
        return p
    lab_lib.make_provider = make
    cfg = uvicorn.Config(main.app, host="127.0.0.1", port=http_port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(100):
            if srv.started:
                break
            if task.done():
                raise RuntimeError(f"ainet 服务没起来（端口 {http_port} 被占用？）：{task.exception()}")
            await asyncio.sleep(0.1)
        main.app.state.labmgr.prepare_poll = 1.0
        yield SimpleNamespace(base=f"http://127.0.0.1:{http_port}", rest=rest, devices=devices, net=net, server=server,
                              delayed=delayed)
    finally:
        srv.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
        await server.stop()


async def _main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8796
    work = os.path.join(os.getcwd(), f"fake_enspro_lab_{port}")
    async with fake_enspro_lab(work, port) as lab:
        print(f"fake eNSP Pro lab: {lab.base}（库在 {work}）", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
