"""模拟设备版实验环境（不连任何真实设备，不进默认 pytest）：3 台假 VRP + 临时库的 ainet 服务。

复用 exam_fake_e2e 的假网络（tests/fake_vrp.py 的 NetVRP：AR0 / AR2 / LSW0，ping 按各设备配置计算），
监听 127.0.0.1:2000 / 2001 / 2003，再以 AINET_LAB_PROVIDER=ensp、ENSP_HOST=127.0.0.1 起服务。
- ai=False（缺省）：屏蔽 DeepSeek Key（评分降级为只给结果分、提示用静态、Agent 返回「未配置」）；
- ai=True：沿用本机的 Key（~/.config/ainet/secrets.env），用于真 AI 联调（少量调用）。

用法：
  python tests/e2e/fake_lab.py [HTTP 端口，缺省 8790] [--ai]     # 起好后一直运行，Ctrl-C 结束
  其他脚本：async with fake_lab(dir, port) as (base, net): ...
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BACKEND, "tests"))
sys.path.insert(0, BACKEND)

from exam_fake_e2e import GUIDS, PORTS, build_net, serve  # noqa: E402


@contextlib.asynccontextmanager
async def fake_lab(work_dir: str, http_port: int, *, ai: bool = False, env_extra: dict | None = None):
    os.makedirs(work_dir, exist_ok=True)
    net = build_net()
    servers = [await serve(net.devs[n], p) for n, p in PORTS.items()]
    db_path = os.path.join(work_dir, "fake_lab.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    env = dict(os.environ)
    if not ai:
        env.pop("DEEPSEEK_API_KEY", None)
        env["AINET_SECRETS_FILE"] = os.path.join(work_dir, "no-such-secrets.env")
    env.update(AINET_LAB_PROVIDER="ensp", ENSP_HOST="127.0.0.1",
               AINET_TOPO_DIRS=os.path.join(BACKEND, "tests", "fixtures", "topos"),
               AINET_ENSP_RUNNING_IDS=",".join(GUIDS.values()), AINET_ENSP_CLIENT="1", AINET_ENSP_TOPO_HIS="",
               AINET_ENSP_DEVICES_DIRS="", AINET_DB_PATH=db_path, AINET_HUB_MIN_RECONNECT="0",
               AINET_PRACTICE_SEED="0", AINET_EXAM_TICK="0.5", AINET_EXAM_RESET_RETRY="2",
               # 课程页会探测全部环境：eNSP Pro 指向本机关闭的端口（离线），绝不连真 eNSP Pro 虚机
               AINET_ENSPRO_URL="http://127.0.0.1:9")
    env.update(env_extra or {})
    log = open(os.path.join(work_dir, f"server_{http_port}.log"), "w")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(http_port)], cwd=BACKEND,
                           env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{http_port}"
    try:
        for _ in range(150):
            if srv.poll() is not None:
                raise RuntimeError(f"ainet 服务没起来（端口 {http_port} 被占用？），见 {log.name}")
            try:
                urllib.request.urlopen(base + "/api/students")
                break
            except OSError:
                await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
        if srv.poll() is not None:
            raise RuntimeError(f"ainet 服务没起来（端口 {http_port} 被占用？），见 {log.name}")
        yield base, net
    finally:
        srv.terminate()
        for s in servers:
            s.close()


async def _main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8790
    work = os.path.join(os.getcwd(), f"fake_lab_{port}")
    async with fake_lab(work, port, ai="--ai" in sys.argv) as (base, _):
        print(f"fake lab: {base}（库与日志在 {work}）", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
