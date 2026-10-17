"""模拟环境 / 沙箱切换的真机 + 无头浏览器 e2e（spec §5.2；不进默认 pytest）。

前置：本机能访问 eNSP Pro；沙箱 ainet_test（id 2：AR1=6、AR2=7）与 ainet_campus（id 3：LSW0=8 …）；服务以
AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=2 和临时 AINET_DB_PATH 起好（建议 AINET_AI_SCORING=0）。
eNSP Pro 单会话：跑脚本期间不要在别处登录。脚本会启动 AR1、AR2、LSW0，结束时停掉它们（不 save、不删设备）。

步骤（2026-10-07 起环境切换只在课程页进行，操作页不再有环境 / 沙箱下拉；本脚本同步了选择器，未在真机上重跑）：
 1. 课程页环境列：eNSP Pro 在线（当前）；eNSP 按真实探测结果（本机没有 eNSP 经典版 → 置灰 + 「（离线）」）；沙箱接口列出 2、3。
 2. 按题目准备环境（LabSwitch.prepare：AR1–AR2 互联题）→ 沙箱 2、启动 AR1 / AR2 → 运行中；AR1 终端 display version。
 3. 切到沙箱 ainet_campus（LabSwitch.switchTo）→ 操作页旧终端标签关闭、拓扑重画为 ainet_campus。
 4. 启动 LSW0，打开终端执行 display version。
 5. 切回沙箱 ainet_test → 拓扑重画为 AR1、AR2。
 6. 开始练习（AR1–AR2 互联题）→ POST /api/lab/environment 返回 409；课程页提示进行中的练习、进入按钮禁用 → 结束练习（复位）。
 7. 另一个（非管理员）账号：课程页环境列同样可操作（单人使用，不区分角色）；档案页顶栏只读显示当前环境。
 8. 停掉 AR1、AR2、LSW0；全程无 JS 异常。

用法：python tests/e2e/labswitch_e2e.py [base_url] [截图目录]
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

sys.argv = [sys.argv[0], "basic", *sys.argv[1:]]          # enspro_e2e 按 argv 解析 MODE / BASE / SHOTS
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enspro_e2e as E  # noqa: E402

BASE, SHOTS = E.BASE, E.SHOTS
AR1, LSW0 = 6, 8


async def api(c, method, url, body=None):
    js = (f"fetch({json.dumps(url)}, {{method: {json.dumps(method)}, headers: {{'Content-Type': 'application/json'}}"
          + (f", body: JSON.stringify({json.dumps(body)})" if body is not None else "")
          + "}).then(async r => ({status: r.status, body: await r.json().catch(() => null)}))")
    return await c.ev(js)


async def wait_online(c, ids, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        st = (await api(c, "GET", "api/topology/current"))["body"]
        devs = {d["console"]: d.get("status") for d in (st.get("topology") or {}).get("devices", [])}
        if all(devs.get(i) == "online" for i in ids):
            return True
        await asyncio.sleep(3)
    return False


async def sel_state(c):
    """操作页的状态（环境切换只在课程页进行，操作页不再有环境 / 沙箱下拉）。"""
    return await c.ev("""(() => ({
        topo: document.getElementById('topo-status').textContent,
        env: document.getElementById('tp-env').textContent,
        tabs: window.__ainet.state.tabs.size,
        nodes: [...document.querySelectorAll('#topo-svg .node')].map(n => n.dataset.name).sort()}))()""")


async def course_envs(c):
    """课程页环境列：[(key, 禁用, 文字)]。"""
    await c.cmd("Page.navigate", url=f"{BASE}/courses")
    await c.wait_for("window.__ainetCourses && window.__ainetCourses.st.envs && "
                     "document.querySelectorAll('#c-envs .c-env').length >= 2", 30)
    await asyncio.sleep(1)
    return await c.ev("[...document.querySelectorAll('#c-envs .c-env')].map(b => [b.dataset.key, b.disabled, "
                      "b.innerText.replace(/\\s+/g, ' ')])")


async def switch_sandbox(c, sid):
    """环境 / 沙箱切换接口（课程页「进入」时由按题目准备环境调用；这里直接调 LabSwitch.switchTo）。"""
    return await c.ev(f"window.LabSwitch.switchTo('', 'enspro', {sid}).then(r => r.changed, e => 'error: ' + e.message)")


async def run_scenario(c) -> list[bool]:
    res: list[bool] = []
    envs_ui = await course_envs(c)
    envs = (await api(c, "GET", "api/lab/environments?refresh=1"))["body"]
    sbs = (await api(c, "GET", "api/lab/sandboxes"))["body"]
    print("  环境:", [(e["key"], e["available"], e["reason"][:60]) for e in envs["environments"]])
    print("  课程页环境列:", envs_ui, "沙箱:", [(x["id"], x["name"]) for x in sbs["sandboxes"]])
    ensp = next(o for o in envs_ui if o[0] == "ensp")
    pro = next(o for o in envs_ui if o[0] == "enspro")
    ensp_env = next(e for e in envs["environments"] if e["key"] == "ensp")
    ok1 = envs["current"] == "enspro" and not pro[1] and "在线" in pro[2] and {2, 3} <= {x["id"] for x in sbs["sandboxes"]} \
        and ensp[1] == (not ensp_env["available"]) and (ensp_env["available"] or "（离线）" in ensp[2])
    E.step(res, 1, "课程页环境列：eNSP Pro 在线（当前）；eNSP 按探测结果置灰并写「（离线）」；沙箱接口列出 2、3", ok1,
           f"ensp={ensp[2]}")
    await c.shot("ls_01_course_envs.png")

    # 2. 按题目准备环境（操作页上调用 LabSwitch.prepare，与课程页「进入」同一个接口）
    await c.cmd("Page.navigate", url=f"{BASE}/?mode=lab")
    await c.wait_for("window.__ainet && window.__ainet.state.cur", 30)
    t0 = time.time()
    await c.ev("""window.__job = null; window.LabSwitch.prepare('', 'enspro-t1-t2-link', j => {
      window.__prep = (window.__prep || []); window.__prep.push(j.stage + ':' + j.message); })
      .then(j => { window.__job = j; }, e => { window.__job = {ok: false, error: e.message, stage: 'reject'}; }); 1""")
    job = await c.wait_for("window.__job", 420, 2)
    print(f"  prepare: ok={job['ok']} stage={job['stage']} sandbox={job['sandbox']} devices={job['devices']}"
          f" {time.time() - t0:.0f}s；阶段：{sorted(set(await c.ev('window.__prep')))[:6]}")
    await c.wait_for("window.__ainet.state.topo && window.__ainet.state.topo.name === 'ainet_test'", 10)
    await asyncio.sleep(6)       # 冷启动后 console 可能还要一会儿才响应
    await c.click_node("AR1")
    await c.wait_for(f"(({E.BUF_JS})({AR1})).trimEnd().endsWith('<HUAWEI>')", 240, 1)
    ok, out = await E.run_cmd(c, AR1, "display version")
    E.step(res, 2, "按题目准备环境：切到沙箱 2、启动 AR1/AR2 到运行中；AR1 终端 display version", job["ok"] and ok and
           "YunShan" in out, f"{time.time() - t0:.0f}s")
    await c.shot("ls_02_prepared_ar1_terminal.png")

    # 3. 切到沙箱 3：操作页发现切换代数变化，关闭旧终端、重画拓扑
    r = await switch_sandbox(c, 3)
    okc = await c.wait_for("window.__ainet.state.topo && window.__ainet.state.topo.name === 'ainet_campus'", 30)
    await asyncio.sleep(1.5)
    s = await sel_state(c)
    print("  切换后:", r, s["topo"], s["nodes"], "tabs", s["tabs"])
    E.step(res, 3, "切到沙箱 ainet_campus：操作页旧终端关闭、拓扑重画（题名气泡里显示新沙箱）", r is True and bool(okc)
           and s["tabs"] == 0 and "LSW0" in s["nodes"] and "AR1" not in s["nodes"] and "ainet_campus" in s["topo"], s["topo"])
    await c.shot("ls_03_sandbox3.png")

    # 4. LSW0 终端
    r = await api(c, "POST", "api/lab/devices/start", {"devices": [LSW0]})
    print("  启动 LSW0:", r["status"])
    t0 = time.time()
    on = await wait_online(c, [LSW0], 150)
    if not on:
        # eNSP Pro 偶发：其他沙箱有设备运行时启动 LSW0，会一直停在状态 6（spec §14 第 21 项）；同用户操作，停止再启动一次
        r = await api(c, "POST", "api/lab/devices/reboot", {"devices": [LSW0]})
        print(f"  LSW0 150 秒未就绪，重启（停止 → 启动）：{r['status']} {r['body']}")
        on = await wait_online(c, [LSW0], 240)
    await asyncio.sleep(5)
    await c.click_node("LSW0")
    got = await c.wait_for(f"(({E.BUF_JS})({LSW0})).trimEnd().endsWith('<HUAWEI>')", 300, 2)
    ok, out = await E.run_cmd(c, LSW0, "display version") if got else (False, "")
    E.step(res, 4, "沙箱 3：启动 LSW0，终端 display version", on and ok and "YunShan" in out, f"{time.time() - t0:.0f}s")
    await c.shot("ls_04_lsw0_terminal.png")

    # 5. 切回沙箱 2
    r = await switch_sandbox(c, 2)
    okc = await c.wait_for("window.__ainet.state.topo && window.__ainet.state.topo.name === 'ainet_test'", 30)
    await asyncio.sleep(1.5)
    s = await sel_state(c)
    E.step(res, 5, "切回沙箱 ainet_test：拓扑重画为 AR1、AR2，旧终端关闭", r is True and bool(okc) and s["nodes"] == ["AR1", "AR2"]
           and s["tabs"] == 0, s["topo"])
    await c.shot("ls_05_back_to_sandbox2.png")

    # 6. 练习中切换被拒；课程页提示「练习进行中」、进入按钮禁用
    for _ in range(8):      # 刚冷启动的 YunShan 设备会报 Error: The system is busy in building configuration，稍后重试
        r = await api(c, "POST", "api/practice/start", {"scenario_id": "enspro-t1-t2-link", "seed": 0})
        if r["status"] == 200 or "busy" not in str((r["body"] or {}).get("detail", "")):
            break
        print("  开始练习暂不可行（设备仍在初始化），20 秒后重试:", r["body"]["detail"][:60])
        await asyncio.sleep(20)
    print("  开始练习:", r["status"], (r["body"] or {}).get("detail", "")[:80])
    r409 = await api(c, "POST", "api/lab/environment", {"key": "enspro", "sandbox": 3})
    await c.cmd("Page.navigate", url=f"{BASE}/courses")
    await c.wait_for("window.__ainetCourses && !document.getElementById('c-active').hidden", 20)
    await asyncio.sleep(1)
    banner = await c.ev("document.getElementById('c-active-text').textContent")
    locked = await c.ev("document.getElementById('c-practice').disabled")
    print("  练习中切换:", r409["status"], r409["body"], "课程页:", banner, "按钮禁用", locked)
    await c.shot("ls_06_practice_locked.png")
    end = await api(c, "POST", "api/practice/end")
    print("  结束练习:", end["status"], ((end["body"] or {}).get("note") or "")[:80])
    E.step(res, 6, "练习中：切换接口 409「练习进行中」；课程页提示进行中的练习、进入按钮禁用；结束练习后复位",
           r["status"] == 200 and r409["status"] == 409 and "练习进行中" in r409["body"]["detail"]
           and "进行中" in banner and locked and end["status"] == 200)
    return res


async def other_user(c) -> list[bool]:
    res: list[bool] = []
    r = await api(c, "POST", "api/students", {"login": "e2e_stu", "name": "测试学员", "password": "pw"})
    print("  建学员:", r["status"])
    await api(c, "POST", "api/login", {"login": "e2e_stu", "password": "pw"})
    envs_ui = await course_envs(c)
    who = await c.ev("document.getElementById('who').textContent")
    await c.shot("ls_07_other_user.png")
    pro = next(o for o in envs_ui if o[0] == "enspro")
    E.step(res, 7, "非管理员账号的课程页环境列同样可操作（单人使用，不区分角色）", "测试学员" in who and not pro[1], who)
    await c.cmd("Page.navigate", url=f"{BASE}/profile")
    ro = await c.wait_for("(() => { const e = document.getElementById('lab-ro'); return e && e.textContent.includes('eNSP Pro') && e.textContent; })()", 15)
    await c.shot("ls_08_profile_readonly.png")
    E.step(res, 8, "档案页顶栏只读显示当前环境", bool(ro) and "ainet_test" in str(ro), str(ro))
    return res


async def run() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    proc = subprocess.Popen([E.CHROME, f"--remote-debugging-port={E.DEBUG_PORT}", "--no-sandbox",
                             "--window-size=1600,900", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res: list[bool] = []
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{E.DEBUG_PORT}/json"))
                break
            except OSError:
                time.sleep(0.2)
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=None) as ws:
            c = E.CDP(ws)
            reader = asyncio.create_task(c.reader())
            for dom in ("Runtime", "Log", "Page"):
                await c.cmd(f"{dom}.enable")
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=900, deviceScaleFactor=1, mobile=False)
            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            print("login:", (await api(c, "POST", "api/login", {"login": "admin", "password": "admin"}))["status"])
            try:
                res += await run_scenario(c)
                res += await other_user(c)
            finally:
                # 停掉本次启动的设备（不 save、不删）：先切回 admin，按沙箱分别停
                await api(c, "POST", "api/login", {"login": "admin", "password": "admin"})
                for sb, ids in ((3, [LSW0]), (2, [6, 7])):
                    sw = await api(c, "POST", "api/lab/environment", {"key": "enspro", "sandbox": sb})
                    st = await api(c, "POST", "api/lab/devices/stop", {"devices": ids})
                    print(f"  清理：沙箱 {sb} 切换 {sw['status']}，停止 {ids} -> {st['status']}")
            await asyncio.sleep(1)
            # 步骤 6 故意在练习中调切换接口，浏览器会记一条「409 Failed to load resource」：这是预期的网络日志，不算 JS 错误
            errs = [e for e in c.errors if not ("status of 409" in e and ("/api/lab/environment" in e
                                                                           or "/api/practice/start" in e or "/api/students" in e))]
            print("JS 错误:", errs or "无", f"（已忽略预期的 409 网络日志 {len(c.errors) - len(errs)} 条）")
            E.step(res, len(res) + 1, "全程无 JS 异常 / console error", not errs)
            reader.cancel()
            print(f"结果：{sum(res)} pass / {len(res) - sum(res)} fail")
            return 0 if all(res) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
