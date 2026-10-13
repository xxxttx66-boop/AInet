"""手动的真实 DeepSeek 冒烟脚本（会访问网络、消耗 token；不进默认 pytest）。

用一段构造好的练习记录（Campus 场景「AR0 与 AR2 互联不通」）分别调用：
  1. 评分：一份合理的排障记录（通关）+ 一份盲目试错的记录（放弃），走完整的 grading.score_attempt（临时库）；
  2. 三级提示：学员只查了 AR0 时，依次要第 1、2、3 级提示。
打印评分 JSON、提示原文、来源、token 用量和耗时；第 1、2 级会再跑一遍泄露检查。不会打印 API Key。

用法（backend/ 下）：
  python tests/manual/ai_smoke.py            # 全部
  python tests/manual/ai_smoke.py score      # 只评分
  python tests/manual/ai_smoke.py hint       # 只提示
Key 读取顺序同 ai.load_api_key()：DEEPSEEK_API_KEY → AINET_SECRETS_FILE → backend/data/secrets.env → ~/.config/ainet/secrets.env。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ai  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import grading  # noqa: E402
import hints  # noqa: E402
import scenarios as sc  # noqa: E402

PING_FAIL = """  PING 10.0.12.2: 56  data bytes, press CTRL_C to break
    Request time out
    Request time out
    Request time out
    Request time out
    Request time out

  --- 10.0.12.2 ping statistics ---
    5 packet(s) transmitted
    0 packet(s) received
    100.00% packet loss"""

PING_OK = """  PING 10.0.12.2: 56  data bytes, press CTRL_C to break
    Reply from 10.0.12.2: bytes=56 Sequence=1 ttl=255 time=30 ms
    Reply from 10.0.12.2: bytes=56 Sequence=2 ttl=255 time=20 ms
    Reply from 10.0.12.2: bytes=56 Sequence=3 ttl=255 time=20 ms
    Reply from 10.0.12.2: bytes=56 Sequence=4 ttl=255 time=20 ms
    Reply from 10.0.12.2: bytes=56 Sequence=5 ttl=255 time=20 ms

  --- 10.0.12.2 ping statistics ---
    5 packet(s) transmitted
    5 packet(s) received
    0.00% packet loss
    round-trip min/avg/max = 20/22/30 ms"""

BRIEF_HEAD = """*down: administratively down
^down: standby
(l): loopback
(s): spoofing
The number of interface that is UP in Physical is 2
The number of interface that is DOWN in Physical is 2
The number of interface that is UP in Protocol is 2
The number of interface that is DOWN in Protocol is 2

Interface                         IP Address/Mask      Physical   Protocol
"""
AR0_BRIEF = BRIEF_HEAD + """GigabitEthernet0/0/0              unassigned           down       down
GigabitEthernet0/0/1              10.0.12.1/24         up         up
NULL0                             unassigned           up         up(s)"""
AR2_BRIEF = BRIEF_HEAD + """GigabitEthernet0/0/0              10.0.21.2/24         up         up
GigabitEthernet0/0/1              unassigned           down       down
NULL0                             unassigned           up         up(s)"""

# (相对开始的秒数, 设备, 命令, 输出)
GOOD_LOG = [
    (20, "AR0", "ping 10.0.12.2", PING_FAIL),
    (45, "AR0", "display ip interface brief", AR0_BRIEF),
    (80, "AR2", "display ip interface brief", AR2_BRIEF),
    (110, "AR2", "system-view", "Enter system view, return user view with Ctrl+Z."),
    (115, "AR2", "interface GigabitEthernet0/0/0", ""),
    (125, "AR2", "ip address 10.0.12.2 24", ""),
    (130, "AR2", "return", ""),
    (150, "AR0", "ping 10.0.12.2", PING_OK),
]

DOWN_LOG = ("Oct  4 2026 11:06:25-08:00 AR0 %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface "
            "GigabitEthernet0/0/1 has entered the DOWN state.")
BAD_LOG = [
    (15, "AR0", "system-view", "Enter system view, return user view with Ctrl+Z."),
    (20, "AR0", "interface GigabitEthernet0/0/1", ""),
    (25, "AR0", "shutdown", DOWN_LOG),
    (30, "AR0", "undo shutdown", ""),
    (40, "AR0", "ip address 10.0.21.1 24", ""),
    (45, "AR0", "quit", ""),
    (50, "AR0", "ping 10.0.12.2", PING_FAIL),
    (60, "AR0", "ping 10.0.12.2", PING_FAIL),
    (70, "AR0", "ping 10.0.12.2", PING_FAIL),
    (90, "AR0", "undo ip address", ""),
]

# 提示用：学员只在 AR0 上 ping 了一下、看了接口
HINT_LOG = GOOD_LOG[:2]


def scenario() -> dict:
    loaded, errors = sc.load_dir(config.SCENARIOS_DIR)
    return loaded["campus-ar0-ar2-link"]


def _mk_attempt(s: dict, rows: list, passed: bool, hints_used: int) -> int:
    ids = db.sync_scenarios([s])
    att = db.create_attempt(1, ids[s["id"]])
    t0 = datetime.fromisoformat(att["started_at"])
    for off, dev, cmd, out in rows:
        db.add_attempt_log(att["id"], dev, cmd, out, ts=(t0 + timedelta(seconds=off)).isoformat(timespec="seconds"))
    end = t0 + timedelta(seconds=rows[-1][0] + 10)
    db.update_attempt(att["id"], passed=1 if passed else 0, ended_at=end.isoformat(timespec="seconds"),
                      hints_used=hints_used)
    return att["id"]


async def smoke_score(s: dict) -> None:
    for label, rows, passed, hu in (("合理排障（通关，未用提示）", GOOD_LOG, True, 0),
                                    ("盲目试错（放弃，用了 1 级提示）", BAD_LOG, False, 1)):
        aid = _mk_attempt(s, rows, passed, hu)
        t = time.time()
        out = await grading.score_attempt(aid)
        print(f"\n========== 评分：{label}（{time.time() - t:.1f}s）==========")
        print(json.dumps({k: out[k] for k in ("status", "scores", "comment", "standard_path", "ai")
                          if k in out}, ensure_ascii=False, indent=2))
        if out.get("reason"):
            print("原因：", out["reason"])
        print("确定性信号：", json.dumps(out["signals"], ensure_ascii=False))


async def smoke_hints(s: dict) -> None:
    t0 = datetime.now(timezone.utc).timestamp()
    logs = [{"device": d, "command": c, "output": o, "t": t0 + off} for off, d, c, o in HINT_LOG]
    banned = hints.banned_terms(s)
    print(f"\n第 1、2 级禁止内容：{banned}")
    cases = [("学员还没有任何操作", [], 1), ("学员还没有任何操作", [], 2)] + \
            [("学员已在 AR0 上 ping 并查看了接口", logs, lv) for lv in (1, 2, 3)]
    for label, lg, level in cases:
        t = time.time()
        r = await hints.generate(s, level, lg, t0)
        print(f"\n========== 第 {level} 级提示 · {label}（{time.time() - t:.1f}s，来源 {r['source']}）==========")
        print(r["text"])
        if r.get("usage"):
            print("token 用量：", r["usage"])
        if r.get("reason"):
            print("回退原因：", r["reason"])
        if level < 3:
            hit = hints.leaks(r["text"], banned)
            print("泄露检查：", "命中 " + "、".join(hit) if hit else "通过")


async def main() -> None:
    key, source = ai.load_api_key()
    print(f"模型 {config.AI_MODEL} @ {config.AI_BASE_URL}；Key 来源：{source}（{ai.mask_key(key)}）")
    print(f"评分思考={config.AI_SCORE_THINKING} max_tokens={config.AI_SCORE_MAX_TOKENS}；"
          f"提示思考={config.AI_HINT_THINKING} 超时={config.AI_HINT_TIMEOUT}s")
    if not key:
        print("未配置 API Key，退出")
        return
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    tmp = tempfile.mkdtemp(prefix="ainet-smoke-")
    config.DB_PATH = os.path.join(tmp, "smoke.db")
    db.init_db()
    s = scenario()
    if what in ("all", "score"):
        await smoke_score(s)
    if what in ("all", "hint"):
        await smoke_hints(s)


if __name__ == "__main__":
    asyncio.run(main())
