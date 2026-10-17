"""eNSP Pro 设备启动可靠性的真机 e2e（spec §10.5；只走 HTTP / WebSocket 接口，不进默认 pytest）。

前置：本机能访问 eNSP Pro；沙箱 ainet_test（id 2：AR1=6、AR2=7）与 ainet_campus（id 3：LSW0=8）；服务以
AINET_LAB_PROVIDER=enspro、临时 AINET_DB_PATH 起好（建议 AINET_AI_SCORING=0）。eNSP Pro 单会话：跑脚本期间不要在别处登录，
也不要同时跑别的直连 eNSP Pro 的脚本。脚本只启停设备、开 / 结束练习（结束时复位），不 save、不删设备。

模式（每个模式对服务的环境变量要求不同）：
  probe   冷启动 AR1、AR2（按题目准备环境）→ 记录 start / wait / probe 阶段与用时 → 任务完成后立即开练，期望 200（不再 busy）→ 结束练习。
  switch  AR1、AR2 运行中切到沙箱 3 → 返回 stopped_old = [AR1, AR2]，沙箱 2 运行数变 0。
  stuck   缺省配置即可（LSW0 实测约一半的启动会卡住，与停机间隔无关）：沙箱 3 启动 LSW0 → 就绪 → 重启 →
          期望卡在「启动系统」→ 看门狗超时（AINET_ENSPRO_START_TIMEOUT，缺省 150）后自动停止再启动 → 就绪。
  gap     缺省停机间隔：LSW0 运行中重启 → 启动请求在停止后 ≥ 间隔才发出 → 正常就绪（不卡）。
  idle    服务设 AINET_ENSPRO_IDLE_STOP_MIN=2（AINET_ENSPRO_IDLE_WARN=60）：LSW0 就绪后开一个终端不操作 → 收到空闲预告与
          停机通知 → LSW0 被自动停止。
  stopall 停掉沙箱 2、3 的全部设备（收尾）。

用法：python tests/e2e/reliability_e2e.py MODE [base_url]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"
BASE = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765").rstrip("/")
AR1, AR2, LSW0 = 6, 7, 8
RESULTS: list[tuple[str, bool, str]] = []
T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} +{time.time() - T0:5.0f}s] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    return ok


async def login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/login", json={"login": "admin", "password": "admin"})
    r.raise_for_status()


async def topo(c):
    t = (await c.get("/api/topology/current")).json()
    devs = {d["console"]: d for d in (t.get("topology") or {}).get("devices", [])}
    return t, devs


async def status(c, dev):
    t, devs = await topo(c)
    d = devs.get(dev) or {}
    return d.get("status"), (t.get("progress") or {}).get(str(dev)), t


async def ensure_sandbox(c, sb):
    r = await c.post("/api/lab/environment", json={"key": "enspro", "sandbox": sb})
    assert r.status_code == 200, r.text
    return r.json()


async def wait_status(c, dev, want, timeout, every=3.0, trace=True):
    end, last = time.time() + timeout, None
    while time.time() < end:
        st, prog, _ = await status(c, dev)
        if trace and (st, prog) != last:
            log(f"  设备 {dev}: {st} {prog or ''}")
            last = (st, prog)
        if st in want:
            return st
        await asyncio.sleep(every)
    return None


async def mode_probe(c):
    await ensure_sandbox(c, 2)
    for d in (AR1, AR2):
        st, _, _ = await status(c, d)
        if st not in ("stopped",):
            await c.post("/api/lab/devices/stop", json={"devices": [d]})
    await wait_status(c, AR1, {"stopped"}, 60, trace=False)
    await wait_status(c, AR2, {"stopped"}, 60, trace=False)
    r = await c.post("/api/lab/prepare", json={"scenario_id": "enspro-t1-t2-link"})
    check("prepare 202", r.status_code == 202, r.text[:200])
    stages, t_probe, job = [], None, None
    end = time.time() + 900
    while time.time() < end:
        job = (await c.get("/api/lab/prepare")).json()["job"]
        if not stages or stages[-1][0] != job["stage"]:
            stages.append((job["stage"], round(job["elapsed_s"])))
            log(f"  job {job['stage']}: {job['message'][:160]}")
            if job["stage"] == "probe" and t_probe is None:
                t_probe = job["elapsed_s"]
        if job["done"]:
            break
        await asyncio.sleep(2)
    check("prepare ok", bool(job and job["ok"]), f"stages={stages} devices={job and job['devices']} "
                                                 f"error={job and job['error']}")
    check("出现 probe 阶段", any(s == "probe" for s, _ in stages), f"probe 开始于 {t_probe}s，完成于 {job['elapsed_s']}s")
    info = (await c.get("/api/lab")).json()
    log("  看门狗事件：" + json.dumps(info["watch"]["events"], ensure_ascii=False)[:1500])
    r = await c.post("/api/practice/start", json={"scenario_id": "enspro-t1-t2-link"})
    check("就绪后立即开练 200（不 busy）", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        r2 = await c.post("/api/practice/end")
        check("结束练习（复位）", r2.status_code == 200, r2.text[:200])


async def mode_switch(c):
    await ensure_sandbox(c, 2)
    _, devs = await topo(c)
    running = [n for n in (AR1, AR2) if devs[n]["status"] in ("online", "initializing", "starting")]
    log(f"  沙箱 2 运行中：{running}")
    r = await c.post("/api/lab/environment", json={"key": "enspro", "sandbox": 3})
    body = r.json()
    check("切到沙箱 3", r.status_code == 200, json.dumps(body.get("stopped_old"), ensure_ascii=False))
    st = body.get("stopped_old") or {}
    check("返回停掉的旧沙箱设备", st.get("ok") and set(st.get("devices") or []) >= {"AR1", "AR2"} and st.get("sandbox") == 2,
          str(st))
    await asyncio.sleep(8)
    sbs = (await c.get("/api/lab/sandboxes")).json()["sandboxes"]
    sb2 = next(s for s in sbs if s["id"] == 2)
    check("沙箱 2 运行数为 0", sb2["running"] == 0, str(sb2))
    info = (await c.get("/api/lab")).json()
    check("GET /api/lab 有 last_stopped_old", bool(info.get("last_stopped_old")), str(info.get("last_stopped_old"))[:200])


async def start_ready(c, dev, timeout=600):
    st, _, _ = await status(c, dev)
    if st != "online":
        r = await c.post("/api/lab/devices/start", json={"devices": [dev]})
        log(f"  start → {r.status_code} {r.text[:200]}")
    return await wait_status(c, dev, {"online", "start_failed"}, timeout)


async def mode_stuck(c):
    await ensure_sandbox(c, 3)
    st = await start_ready(c, LSW0)
    check("LSW0 先正常就绪", st == "online", str(st))
    t_reboot = time.time()
    r = await c.post("/api/lab/devices/reboot", json={"devices": [LSW0]})
    log(f"  reboot → {r.status_code} {r.text[:200]}")
    restart_at = None
    end = time.time() + 900
    last = None
    while time.time() < end:
        st, prog, _ = await status(c, LSW0)
        ev = (await c.get("/api/lab")).json()["watch"]["events"]
        if (st, prog) != last:
            log(f"  LSW0: {st} {prog or ''}")
            last = (st, prog)
        if restart_at is None and any(e["event"] == "restart" and e["device"] == "LSW0" for e in ev):
            restart_at = time.time() - t_reboot
            log(f"  看门狗自动重启（重启后 {restart_at:.0f}s）：" +
                json.dumps([e for e in ev if e["event"] == "restart"][-1], ensure_ascii=False))
        if st in ("online", "start_failed") and time.time() - t_reboot > 20:
            break
        await asyncio.sleep(3)
    ev = (await c.get("/api/lab")).json()["watch"]["events"]
    log("  看门狗事件：" + json.dumps([e for e in ev if e["device"] == "LSW0"], ensure_ascii=False)[:2000])
    check("复现卡住并由看门狗自动停止再启动", restart_at is not None, f"restart_at={restart_at}")
    check("最终就绪", st == "online", f"{st}，用时 {time.time() - t_reboot:.0f}s")


async def mode_gap(c):
    await ensure_sandbox(c, 3)
    st = await start_ready(c, LSW0)
    check("LSW0 就绪", st == "online", str(st))
    t_reboot = time.time()
    r = await c.post("/api/lab/devices/reboot", json={"devices": [LSW0]})
    check("reboot 返回停止 + 等间隔", r.status_code == 200 and r.json().get("via") == "stop+start", r.text[:200])
    st = await wait_status(c, LSW0, {"online", "start_failed"}, 600)
    ev = [e for e in (await c.get("/api/lab")).json()["watch"]["events"] if e["device"] == "LSW0"]
    log("  看门狗事件：" + json.dumps(ev, ensure_ascii=False)[:1500])
    check("没有卡住（无自动重启）且就绪", st == "online" and not any(e["event"] == "restart" for e in ev),
          f"{st}，用时 {time.time() - t_reboot:.0f}s")


async def mode_idle(c):
    await ensure_sandbox(c, 3)
    st = await start_ready(c, LSW0)
    check("LSW0 就绪", st == "online", str(st))
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    url = BASE.replace("http", "ws", 1) + f"/ws/console/{LSW0}"
    got = b""
    t0 = time.time()
    async with websockets.connect(url, additional_headers={"Cookie": cookie}, max_size=None) as ws:
        log("  终端已连接，之后不做任何操作")
        while time.time() - t0 < 600:
            try:
                m = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                st, _, _ = await status(c, LSW0)
                if st == "stopped":
                    break
                continue
            except websockets.ConnectionClosed:
                break
            got += m if isinstance(m, bytes) else m.encode()
            for key in ("设备已空闲", "已自动停止", "终端已断开"):
                if key.encode() in got and f"seen:{key}" not in [r[0] for r in RESULTS]:
                    RESULTS.append((f"seen:{key}", True, ""))
                    log(f"  终端收到：{key}（连接后 {time.time() - t0:.0f}s）")
    text = got.decode("utf-8", "ignore")
    check("终端收到空闲预告", "设备已空闲" in text)
    check("终端收到停机通知", "已自动停止" in text)
    st = await wait_status(c, LSW0, {"stopped"}, 60, trace=False)
    check("LSW0 被自动停止", st == "stopped", f"连接后 {time.time() - t0:.0f}s")
    info = (await c.get("/api/lab")).json()["idle_stop"]
    check("GET /api/lab idle_stop.last_stop", bool(info.get("last_stop")), json.dumps(info, ensure_ascii=False)[:300])


async def mode_stopall(c):
    for sb, devs in ((3, [LSW0]), (2, [AR1, AR2])):
        await ensure_sandbox(c, sb)
        _, d = await topo(c)
        run = [k for k in devs if d.get(k, {}).get("status") not in ("stopped", None)]
        if run:
            r = await c.post("/api/lab/devices/stop", json={"devices": run})
            log(f"  停止沙箱 {sb} 的 {run} → {r.status_code}")


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
        await login(c)
        await {"probe": mode_probe, "switch": mode_switch, "stuck": mode_stuck, "gap": mode_gap,
               "idle": mode_idle, "stopall": mode_stopall}[MODE](c)
    res = [r for r in RESULTS if not r[0].startswith("seen:")]
    n_ok = sum(ok for _, ok, _ in res)
    print(f"\n{MODE}: {n_ok} pass / {len(res) - n_ok} fail", flush=True)


asyncio.run(main())
