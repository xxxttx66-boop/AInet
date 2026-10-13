"""eNSP Pro 真机 + 无头浏览器 e2e（不进默认 pytest：文件名不以 test_ 开头）。

前置：本机能访问 eNSP Pro（AINET_ENSPRO_URL，缺省 https://127.0.0.1:8443），沙箱里有 T1、T2（ENSP-AR，YunShan），
T1 GE0/0/1 <-> T2 GE0/0/1 已连线，两台已启动；设备 console 密码在 secrets.env 的 ENSPRO_DEVICE_PASSWORD。
服务以 AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=<沙箱 id> 和临时 AINET_DB_PATH 起好（建议 AINET_AI_SCORING=0）。
eNSP Pro 同一账号只能有一个会话：跑脚本期间不要在别处登录 eNSP Pro 网页，也不要另开脚本调它的接口。

用法：python tests/e2e/enspro_e2e.py basic|practice|practice-extra [base_url] [截图目录]
- basic：拓扑图显示 T1、T2 与状态、连线接口名 → 打开 T1、T2 终端执行 display version、display ip interface brief
  → 在网页上停止 T2（确认框）看状态 停止中 → 已停止 → 再启动（确认框）看 启动中 + 进度 → 运行中 → 再开 T2 终端
  → 全程无 JS 异常。
- practice-extra：同 practice，另外学员在 T2 上做场景之外的改动（sysname、LoopBack、接口描述与 shutdown、静态路由），
  结束时由整机复位（config_reset 差异回滚）撤销，T2 复位轮数 ≥ 1。
- practice：练习页签开始「T1 与 T2 互联不通」（YunShan）→ 学员在 T2 终端排查并修复、在 T1 上 ping → 自动判定通关
  → 结束练习 → 复位成功（与开练前快照逐行一致）→ 全程无 JS 异常。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import base64

import websockets

CHROME = os.environ.get(
    "AINET_CHROME",
    os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell"))
MODE = sys.argv[1] if len(sys.argv) > 1 else "basic"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8790"
SHOTS = sys.argv[3] if len(sys.argv) > 3 else "."
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9336"))
T1, T2 = 6, 7          # console 键 = deviceId

BUF_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return '';
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) s += b.getLine(k).translateToString(true) + '\\n';
  return s; })"""


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.errors: list[str] = []
        self.dialogs: list[str] = []

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                self.errors.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
            elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
                self.errors.append("console.error: " + " ".join(
                    str(a.get("value", a.get("description"))) for a in m["params"]["args"]))
            elif m.get("method") == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
                e = m["params"]["entry"]
                self.errors.append(f"log.error: {e.get('text')} {e.get('url', '')}")
            elif m.get("method") == "Page.javascriptDialogOpening":
                self.dialogs.append(m["params"]["message"])
                self.i += 1
                await self.ws.send(json.dumps({"id": self.i, "method": "Page.handleJavaScriptDialog",
                                               "params": {"accept": True}}))

    async def cmd(self, method, **params):
        self.i += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.i] = fut
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        r = await asyncio.wait_for(fut, 60)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    async def ev(self, js):
        r = await self.cmd("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"evaluate 失败: {r['exceptionDetails']}")
        return r["result"].get("value")

    async def click_at(self, rect):
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.cmd("Input.dispatchMouseEvent", type=typ, x=rect["x"], y=rect["y"], button="left", clickCount=1)

    async def rect(self, js_el):
        return await self.ev(f"""(() => {{ const e = {js_el}; if (!e) return null;
          const r = e.getBoundingClientRect(); return {{x: r.x + r.width / 2, y: r.y + r.height / 2}}; }})()""")

    async def click_node(self, name):
        r = await self.rect(f"[...document.querySelectorAll('#topo-svg .node')].find(n => n.dataset.name === "
                            f"{json.dumps(name)}).querySelector('.shape')")
        await self.click_at(r)

    async def click_power(self, name):
        r = await self.rect(f"[...document.querySelectorAll('#topo-svg .node')].find(n => n.dataset.name === "
                            f"{json.dumps(name)}).querySelector('.power rect')")
        await self.click_at(r)

    async def wait_for(self, js, timeout=10.0, every=0.3):
        end = time.time() + timeout
        while time.time() < end:
            v = await self.ev(js)
            if v:
                return v
            await asyncio.sleep(every)
        return await self.ev(js)

    async def shot(self, name):
        r = await self.cmd("Page.captureScreenshot", format="png")
        path = os.path.join(SHOTS, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("  截图:", path)

    async def type_(self, port, text):
        await self.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode({json.dumps(text)}))")


def step(results, n, text, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {n}. {text}" + (f"  —— {detail}" if detail else ""))
    results.append(ok)


def node_status(name):
    return (f"(() => {{ const n = [...document.querySelectorAll('#topo-svg .node')].find(n => n.dataset.name === "
            f"{json.dumps(name)}); return n && n.dataset.status; }})()")


def power_text(name):
    return (f"(() => {{ const n = [...document.querySelectorAll('#topo-svg .node')].find(n => n.dataset.name === "
            f"{json.dumps(name)}); return n && n.querySelector('.power text').textContent; }})()")


async def wait_prompt(c, port, after_len=0, timeout=30):
    return await c.wait_for(f"(({BUF_JS})({port})).slice({after_len}).trimEnd().endsWith('<HUAWEI>')", timeout)


async def run_cmd(c, port, cmd, timeout=40, prompt="<HUAWEI>"):
    """像学员一样在终端里敲命令：从当前提示符所在行开始取输出；遇到 ---- More ---- 按空格翻页。"""
    lines0 = (await c.ev(f"(({BUF_JS})({port}))")).rstrip("\n").split("\n")
    n0 = len(lines0) - 1                       # 当前（提示符）行：命令回显会写在这一行
    await c.type_(port, cmd + "\r")
    end = time.time() + timeout
    sent_at = -1                               # 上次按空格时的输出长度（xterm 会把擦掉的分页符覆盖掉，不能按出现次数数）
    out = ""
    while time.time() < end:
        await asyncio.sleep(0.4)
        out = "\n".join((await c.ev(f"(({BUF_JS})({port}))")).rstrip("\n").split("\n")[n0:])
        tail = out.rstrip().split("\n")[-1]
        if cmd in out and tail.rstrip().endswith(prompt) and len(out.rstrip().split("\n")) > 1:
            return True, out
        if "---- More ----" in tail and len(out) != sent_at:
            sent_at = len(out)
            await c.type_(port, " ")
    return False, out


async def basic(c) -> list[bool]:
    res: list[bool] = []
    await c.wait_for("window.__ainet && window.__ainet.state.topo && document.querySelectorAll('#topo-svg .node').length >= 2", 20)
    await asyncio.sleep(1.5)
    status = await c.ev("document.getElementById('topo-status').textContent")
    nodes = await c.ev("[...document.querySelectorAll('#topo-svg .node')].map(n => [n.dataset.name, n.dataset.status, n.querySelector('.power text').textContent])")
    links = await c.ev("[...document.querySelectorAll('#topo-svg .if-label')].map(t => t.firstChild.nodeValue)")
    print("  状态文字:", status, " 节点:", nodes, " 接口标注:", links)
    step(res, 1, "拓扑图显示 T1、T2（运行中、有「停止」按钮）与连线 GE0/0/1 — GE0/0/1",
         sorted(nodes) == [["T1", "online", "■ 停止"], ["T2", "online", "■ 停止"]] and links == ["GE0/0/1", "GE0/0/1"]
         and "运行 2/2 台" in status, status)
    await c.shot("enspro_01_topology.png")

    await c.click_node("T1")
    await wait_prompt(c, T1)
    await c.click_node("T2")
    await wait_prompt(c, T2)
    await c.ev("window.__ainet.setSplit(true)")
    outs = {}
    for port in (T1, T2):
        for cmd in ("display version", "display ip interface brief"):
            ok, out = await run_cmd(c, port, cmd)
            outs[(port, cmd)] = (ok, out)
            lines = [l for l in out.splitlines() if l.strip()]
            print(f"  [{port}] {cmd}: ok={ok}  {lines[1:3]} … {lines[-2:]}")
    ok2 = all(v[0] for v in outs.values()) \
        and all("YunShan" in outs[(p, "display version")][1] for p in (T1, T2)) \
        and all("GE0/0/1" in outs[(p, "display ip interface brief")][1] for p in (T1, T2))
    step(res, 2, "T1、T2 终端执行 display version / display ip interface brief 都有回显", ok2)
    await c.shot("enspro_02_terminals.png")

    # 3. 停止 T2
    await c.click_power("T2")
    seen = []
    t0 = time.time()
    while time.time() - t0 < 180:
        st = await c.ev(node_status("T2"))
        if not seen or seen[-1] != st:
            seen.append(st)
            print(f"  +{time.time() - t0:.0f}s T2 状态 {st}（按钮：{await c.ev(power_text('T2'))}）")
            if st == "stopping":
                await c.shot("enspro_03_t2_stopping.png")
        if st == "stopped":
            break
        await asyncio.sleep(1)
    tab2 = await c.ev(f"window.__ainet.state.tabs.get({T2}).status")
    step(res, 3, "网页停止 T2：确认框 → 停止中 → 已停止（T2 终端断开）",
         seen[-1] == "stopped" and any("停止设备 T2" in d for d in c.dialogs), f"状态序列 {seen}，终端 {tab2}")
    await c.shot("enspro_04_t2_stopped.png")

    # 4. 再启动 T2
    await c.click_power("T2")
    seen2, progress = [], []
    t0 = time.time()
    while time.time() - t0 < 400:
        st = await c.ev(node_status("T2"))
        pt = await c.ev(power_text("T2"))
        if not seen2 or seen2[-1] != st:
            seen2.append(st)
            print(f"  +{time.time() - t0:.0f}s T2 状态 {st}（按钮：{pt}）")
            if st == "starting":
                await c.shot("enspro_05_t2_starting.png")
        if pt and pt not in progress and st == "starting":
            progress.append(pt)
        if st == "online":
            break
        await asyncio.sleep(2)
    print("  启动进度显示:", progress)
    step(res, 4, "网页启动 T2：确认框 → 启动中（显示进度）→ 运行中",
         seen2[-1] == "online" and "starting" in seen2 and any("启动设备 T2" in d for d in c.dialogs),
         f"状态序列 {seen2}，用时 {time.time() - t0:.0f}s")
    await c.shot("enspro_06_t2_online.png")

    # 5. 重启后的 T2 终端（重新登录）
    await c.ev(f"window.__ainet.closeTab({T2})")
    await asyncio.sleep(1)
    ok5 = False
    for _ in range(10):                      # console 可能比状态晚几十秒就绪
        await c.click_node("T2")
        if await wait_prompt(c, T2, timeout=40):
            ok, out = await run_cmd(c, T2, "display version")
            ok5 = ok and "YunShan" in out
            break
        await c.ev(f"window.__ainet.closeTab({T2})")
        await asyncio.sleep(10)
    step(res, 5, "T2 重新启动后终端可登录并执行 display version", ok5)
    await c.shot("enspro_07_t2_terminal_after_restart.png")
    return res


async def practice(c) -> list[bool]:
    res: list[bool] = []
    # 课程页选题后进操作页（?mode=practice&s=课题）：右侧是本题的「开始练习」卡片（2026-10-07）
    await c.cmd("Page.navigate", url=f"{BASE}/?mode=practice&s=enspro-t1-t2-link")
    await c.wait_for("window.__ainet && window.__ainet.state.topo", 20)
    await c.wait_for("[...document.querySelectorAll('#pr-list .pr-item')].length > 0 || document.getElementById('pr-list').textContent.includes('YunShan')", 15)
    item = await c.ev("""(() => { const el = [...document.querySelectorAll('#pr-list > *')].find(e => e.textContent.includes('T1 与 T2'));
      return el ? el.textContent : null; })()""")
    print("  场景条目:", (item or "")[:160])
    btn = await c.ev("""(() => { const el = [...document.querySelectorAll('#pr-list > *')].find(e => e.textContent.includes('T1 与 T2'));
      const b = el && el.querySelector('button'); return b ? [b.textContent, b.disabled] : null; })()""")
    step(res, 1, "练习列表里 YunShan 互联题就绪（可开始）", bool(btn) and not btn[1], str(btn))
    pre = "enspro_px" if MODE == "practice-extra" else "enspro_p"
    await c.shot(f"{pre}01_list.png")
    t0 = time.time()
    await c.ev("""(() => { const el = [...document.querySelectorAll('#pr-list > *')].find(e => e.textContent.includes('T1 与 T2'));
      el.querySelector('button').click(); })()""")
    ok = await c.wait_for("!document.getElementById('pr-active-view').hidden && /T1/.test(document.getElementById('pr-symptom').textContent)", 120, 1)
    st = await c.ev("fetch('api/practice/current').then(r => r.json())")
    link = st.get("param_values", {}).get("link")
    print(f"  开始用时 {time.time() - t0:.1f}s  种子 {st.get('seed')} 参数 {st.get('param_values')}  last_check {st.get('last_check')}")
    step(res, 2, "开始练习：快照、baseline、注入、确认故障生效", bool(ok) and st.get("phase") == "running"
         and st["last_check"]["all"] is False and link is not None)
    await c.shot(f"{pre}02_started.png")

    # 学员：从练习页签的设备按钮打开 T1、T2 终端，先看 T1，再看 T2，修复 T2，回 T1 ping
    for port in (T1, T2):
        await c.click_at(await c.rect(f"document.querySelector('#pr-devices .pr-dev[data-port=\"{port}\"]')"))
        await wait_prompt(c, port)
    await c.ev("window.__ainet.setSplit(true)")
    _, o1 = await run_cmd(c, T1, f"ping -c 2 10.0.{link}.2")
    _, o2 = await run_cmd(c, T2, "display ip interface brief")
    wrong = [l for l in o2.splitlines() if l.startswith("GE0/0/1")]
    print("  T1 ping:", [l.strip() for l in o1.splitlines() if "received" in l], " T2 GE0/0/1:", wrong)
    for cmd in ("system-view", "interface GE0/0/1", f"ip address 10.0.{link}.2 24", "return"):
        n0 = await c.ev(f"(({BUF_JS})({T2})).length")
        await c.type_(T2, cmd + "\r")
        await c.wait_for(f"/[\\]>]\\s*$/.test((({BUF_JS})({T2})).slice({n0}).trimEnd())", 15)
    if MODE == "practice-extra":
        # 学员额外的改动（场景 restore 不管，只能靠整机复位撤销）：改设备名、建 LoopBack、另一个口加描述并 shutdown、静态路由
        extra = ["system-view", "sysname T2X", "interface LoopBack7", "ip address 10.7.7.7 32", "quit",
                 "interface GE0/0/2", "description ainet-extra", "shutdown", "quit",
                 f"ip route-static 10.8.0.0 16 10.0.{link}.1", "return"]
        for cmd in extra:
            n0 = await c.ev(f"(({BUF_JS})({T2})).length")
            await c.type_(T2, cmd + "\r")
            await c.wait_for(f"/[\\]>]\\s*$/.test((({BUF_JS})({T2})).slice({n0}).trimEnd())", 15)
        keys = ("T2X", "LoopBack7", "ainet-extra", "10.8.0.0")
        _, ox = await run_cmd(c, T2, "display current-configuration", prompt="<T2X>", timeout=60)
        print("  T2 额外改动：", [l.strip() for l in ox.splitlines() if any(k in l for k in keys)])
    ok_ping, o3 = await run_cmd(c, T1, f"ping -c 2 10.0.{link}.2")
    print("  修复后 T1 ping:", [l.strip() for l in o3.splitlines() if "received" in l])
    passed = await c.wait_for("fetch('api/practice/current').then(r => r.json()).then(s => s.passed)", 60, 2)
    st = await c.ev("fetch('api/practice/current').then(r => r.json())")
    print("  判定:", st.get("last_check"), " 命令数:", st.get("commands"))
    import re as _re
    step(res, 3, "学员修复后自动判定通关", bool(passed) and bool(_re.search(r"\b[12] packet\(s\) received", o3)),
         str(st.get("last_check")))
    await c.shot(f"{pre}03_passed.png")

    t0 = time.time()
    await c.ev("document.getElementById('pr-end').click()")
    ok = await c.wait_for("!document.getElementById('pr-summary-view').hidden", 180, 1)
    summary = await c.ev("document.getElementById('pr-summary').textContent")
    last = await c.ev("fetch('api/practice/current').then(r => r.json()).then(s => s.last_result)")
    print(f"  结束用时 {time.time() - t0:.1f}s  总结：{summary[:200]!r}")
    print("  复位:", json.dumps(last and last.get("reset"), ensure_ascii=False))
    ok4 = bool(ok) and bool(last) and last.get("reset_ok") is True and last.get("restored") is True and last.get("passed")
    if MODE == "practice-extra":
        r2 = (last or {}).get("reset", {}).get("T2", {})
        ok4 = ok4 and r2.get("rounds", 0) >= 1 and r2.get("commands", 0) > 0     # 额外改动确实由差异复位撤销
        tail = await c.ev(f"(({BUF_JS})({T2})).trimEnd().split('\\n').slice(-2).join(' | ')")
        print("  T2 终端最后两行（sysname 复原后重印提示符）：", tail)
    step(res, 4, "结束练习：restore + 整机复位，与开练前快照一致（reset_ok）", ok4)
    await c.shot(f"{pre}04_summary.png")
    return res


async def run() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
                             "--window-size=1600,900", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json"))
                break
            except OSError:
                time.sleep(0.2)
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=None) as ws:
            c = CDP(ws)
            reader = asyncio.create_task(c.reader())
            for dom in ("Runtime", "Log", "Page"):
                await c.cmd(f"{dom}.enable")
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=900, deviceScaleFactor=1, mobile=False)
            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            print("login:", await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                                       "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)"))
            await c.cmd("Page.navigate", url=f"{BASE}/")
            res = await (basic(c) if MODE == "basic" else practice(c))
            await asyncio.sleep(1)
            print("JS 错误:", c.errors or "无")
            step(res, len(res) + 1, "全程无 JS 异常 / console error", not c.errors)
            reader.cancel()
            print(f"结果：{sum(res)} pass / {len(res) - sum(res)} fail")
            return 0 if all(res) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
