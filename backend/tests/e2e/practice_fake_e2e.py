"""练习 e2e 的模拟设备版（不连任何真实设备，不进默认 pytest）。

用 fake_lab（3 台假 VRP：AR0 / LSW0 / AR2，临时库，屏蔽 DeepSeek Key）起服务，再跑 practice_e2e.run()
（同一套浏览器步骤：场景列表 → 开始练习 → 学员经页面终端修复 → 自动判定通关 → 结束复位并核对 → 放弃路径）。

用法：python tests/e2e/practice_fake_e2e.py [截图目录]
环境变量：AINET_E2E_HTTP_PORT（缺省 8782）、AINET_E2E_DEBUG_PORT（无头 Chrome 调试端口，缺省 9336）。
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_lab import fake_lab  # noqa: E402

HTTP_PORT = int(os.environ.get("AINET_E2E_HTTP_PORT", "8782"))


async def main() -> int:
    shot_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "practice_fake_shots")
    os.makedirs(shot_dir, exist_ok=True)
    sys.argv = sys.argv[:1]                    # practice_e2e 在导入时读 argv
    import practice_e2e
    async with fake_lab(shot_dir, HTTP_PORT) as (base, net):
        practice_e2e.BASE = base
        practice_e2e.SHOT_DIR = shot_dir
        rc = await practice_e2e.run()
        for n in ("AR0", "AR2", "LSW0"):
            d = net.devs[n]
            print(f"  {n} 执行过的命令数：{len(d.executed)}；当前提示符 {d.prompt}")
        # practice_e2e 的步骤 5 / G 经终端用 display ip interface brief 核对，期望真机的「unassigned」行；
        # 假设备只列出配了地址的接口，那两步在假设备上必然不通过（基线 873a121 同样 9 pass / 2 fail）。
        # 这里改从假设备的配置树直接核对：两次结束练习后互联接口都没有遗留地址。
        l3 = {n: net.l3(n) for n in ("AR0", "AR2")}
        clean = not l3["AR0"] and not l3["AR2"]
        print(f"[{'PASS' if clean else 'FAIL'}] F. 假设备配置树核对：复位后 AR0 / AR2 没有遗留的接口地址 {l3}")
        return rc



if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
