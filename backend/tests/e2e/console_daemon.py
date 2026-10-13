"""真机实验 / e2e 独立核对用的常驻 console 进程（不进默认 pytest）。

内嵌生产代码的 ConsoleHub + config_reset（config_reset 每次请求重新加载），对每个 console 端口只保持一条连接，
避免实验时反复连接 / 断开 console（spec §3）。通过 unix socket 收请求（一行 JSON，回一行 JSON）：

  {"op": "snapshot", "port": P, "save": 文件}        抓 display current-configuration 存到文件（隐藏 check 事务）
  {"op": "restore", "port": P, "snapshot": 文件}     restore_to 到快照文件（隐藏 config 事务）
  {"op": "diff", "port": P, "snapshot": 文件}        当前配置与快照的结构差异
  {"op": "hidden", "port": P, "cmds": [...], "kind": "check"|"config", "tolerate": bool, "confirm": bool}
  {"op": "student", "port": P, "cmds": [...], "yn": "y"}   像学员一样经订阅者逐条敲命令（遇分页按空格）

注意：同一 console 端口的多个连接共享输入输出（spec §3）。本进程与服务同时连着设备时，**不要在服务执行隐藏事务（开始 / 判定 /
结束练习）的同时发请求**，否则两边的输入会在设备上交错（实测出现过 `ndo acl number 3000` 这类被打乱的命令）。只在没有练习进行、
或练习已结束后使用；本进程发的命令和输出也会出现在服务的学员终端里。

用法：
  AINET_E2E_HUBSOCK=/path/hub.sock ENSP_HOST=192.168.31.72 python tests/e2e/console_daemon.py serve
  AINET_E2E_HUBSOCK=/path/hub.sock python tests/e2e/console_daemon.py snapshot 2000 out.txt
  AINET_E2E_HUBSOCK=/path/hub.sock python tests/e2e/console_daemon.py restore 2000 snap.txt
可选 AINET_E2E_HUBLOG：把设备原始输出追加写到这个文件（排查用）。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

SOCK = os.environ.get("AINET_E2E_HUBSOCK", "")
LOG = os.environ.get("AINET_E2E_HUBLOG", "")


def request(req: dict) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    s.sendall((json.dumps(req) + "\n").encode())
    data = b""
    while True:
        d = s.recv(65536)
        if not d:
            break
        data += d
    return json.loads(data)


async def serve() -> None:
    import config_reset
    from connectors import EnspConnector
    from console_hub import ConsoleHub, MORE, clean, ends_with_more, last_line, prompt_of

    host = os.environ.get("ENSP_HOST", "192.168.31.72")
    hub = ConsoleHub(lambda p: EnspConnector(host, p, 8), idle_close=36000, min_reconnect=3)
    if LOG:
        orig = hub._on_device

        def tap(ch, data):
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(data.decode("utf-8", "replace"))
            orig(ch, data)
        hub._on_device = tap
    subs: dict[int, object] = {}
    bufs: dict[int, str] = {}

    async def pump(port, sub):
        while True:
            d = await sub.queue.get()
            if d is None:
                subs.pop(port, None)
                return
            bufs[port] = bufs.get(port, "") + d.decode("utf-8", "replace")

    async def student(port, cmds, yn, timeout=30):
        if port not in subs:
            subs[port] = await hub.subscribe(port)
            bufs.setdefault(port, "")
            asyncio.create_task(pump(port, subs[port]))
            await asyncio.sleep(1.5)
        sub = subs[port]
        outs = []
        for cmd in cmds:
            start = len(bufs[port])
            await hub.write(sub, cmd.encode() + b"\r")
            end = time.monotonic() + timeout
            mores = answered = 0
            while time.monotonic() < end:
                await asyncio.sleep(0.3)
                seg = bufs[port][start:]
                n = seg.count(MORE)
                if n > mores and ends_with_more(seg):
                    mores = n
                    await hub.write(sub, b" ")
                    continue
                if "[Y/N]" in last_line(seg) and yn and answered < 2:
                    answered += 1
                    await hub.write(sub, yn.encode() + b"\r")
                    continue
                if "\n" in seg and prompt_of(seg):
                    await asyncio.sleep(0.6)
                    if len(bufs[port]) == start + len(seg):
                        break
            outs.append(clean(bufs[port][start:]))
        return outs

    async def handle(r, w):
        try:
            req = json.loads(await r.readline())
            importlib.reload(config_reset)
            op, port = req["op"], int(req["port"])
            reset = config_reset.ConfigReset(hub, deadline=15)
            t0 = time.monotonic()
            if op == "student":
                res = {"out": await student(port, req["cmds"], req.get("yn", ""))}
            elif op == "hidden":
                hr = await hub.run_hidden(port, req["cmds"], kind=req.get("kind", "check"),
                                          tolerate_errors=req.get("tolerate", False), confirm=req.get("confirm", False))
                res = {"status": hr.status, "reason": hr.reason, "out": hr.outputs, "warnings": hr.warnings,
                       "failed": hr.failed, "prompt": hr.prompt}
            elif op == "snapshot":
                text = await reset.snapshot(port)
                with open(req["save"], "w", encoding="utf-8") as f:
                    f.write(text)
                res = {"saved": req["save"], "chars": len(text)}
            elif op == "restore":
                with open(req["snapshot"], encoding="utf-8") as f:
                    rr = await reset.restore_to(port, f.read())
                res = {**rr.as_dict(), "warnings": rr.warnings}
            elif op == "diff":
                cap = await reset.capture(port)
                with open(req["snapshot"], encoding="utf-8") as f:
                    snap = f.read()
                res = {"diff": config_reset.differences(config_reset.parse(snap), config_reset.parse(cap.outputs[0]))}
            else:
                res = {"error": f"unknown op {op}"}
            res["secs"] = round(time.monotonic() - t0, 1)
        except Exception:
            res = {"error": traceback.format_exc()}
        w.write((json.dumps(res, ensure_ascii=False) + "\n").encode())
        await w.drain()
        w.close()

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = await asyncio.start_unix_server(handle, SOCK)
    async with srv:
        await srv.serve_forever()


def main() -> int:
    if not SOCK:
        print("需要环境变量 AINET_E2E_HUBSOCK（unix socket 路径）")
        return 2
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        asyncio.run(serve())
        return 0
    op, port = args[0], int(args[1])
    req: dict = {"op": op, "port": port}
    if op == "snapshot":
        req["save"] = args[2]
    elif op in ("restore", "diff"):
        req["snapshot"] = args[2]
    else:
        req["cmds"] = args[2:] or [l.rstrip("\n") for l in sys.stdin if l.strip()]
        if op == "hidden" and os.environ.get("AINET_E2E_CONFIG"):
            req.update(kind="config", tolerate=True, confirm=True)
        if op == "student":
            req["yn"] = os.environ.get("AINET_E2E_YN", "y")     # [Y/N] 的回答（空串表示不回答）
    res = request(req)
    if op == "student" and "out" in res:
        print("\n".join(res["out"]))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main())
