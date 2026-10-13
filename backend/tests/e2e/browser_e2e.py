"""真实浏览器 e2e（不进默认 pytest：文件名不以 test_ 开头）。

前置：已用 AINET_TOPO_DIRS=tests/fixtures/topos、ENSP_HOST=<设备主机>、临时 AINET_DB_PATH
起好 uvicorn；eNSP 中运行着 2-1Single-Area OSPF 的 R1、R2（R3 已停止）。Linux 上用
AINET_ENSP_RUNNING_IDS=<R1 GUID>,<R2 GUID> 覆盖运行中设备。只发 display 类命令。

用法：python tests/e2e/browser_e2e.py [base_url] [screenshot.png] [none|campus]
第三个参数为 none 时改为验证「没有运行的设备」（服务以 AINET_ENSP_RUNNING_IDS= 空值启动；
另加 AINET_ENSP_CLIENT=0 时验证「eNSP 未运行」）；
为 campus 时验证 eNSP 中运行着 Campus_Network 的部分设备（运行中的设备由服务端 state 给出，
脚本只要求它们 online、其余有 console 的 stopped、PC 为 na，并打开两台在线设备的终端）。
依赖：websockets；无头 Chrome 路径见 CHROME（可用环境变量 AINET_CHROME 覆盖）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

CHROME = os.environ.get(
    "AINET_CHROME",
    "/home/xixi/.cache/ms-playwright/chromium_headless_shell-1228/"
    "chrome-headless-shell-linux64/chrome-headless-shell",
)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
SHOT = sys.argv[2] if len(sys.argv) > 2 else "ainet_e2e.png"
MODE = sys.argv[3] if len(sys.argv) > 3 else "running"
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9334"))

# 任一 VRP 提示符 <主机名> 或 [主机名]（设备 sysname 未必是拓扑里的设备名，如未配置的 <Huawei>）
PROMPT_JS = r"/[<\[][A-Za-z0-9_.~\-]+[>\]]/.test"
PROMPT_NAME_JS = r"""(port => { const m = (window.__ainet.state.tabs.get(port)?.term.buffer.active || null)
  && [...Array(window.__ainet.state.tabs.get(port).term.buffer.active.length).keys()]
     .map(k => window.__ainet.state.tabs.get(port).term.buffer.active.getLine(k).translateToString(true))
     .join('\n').match(/[<\[][A-Za-z0-9_.~\-]+[>\]]/g); return m ? m[m.length - 1] : null; })"""

BUF_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) s += b.getLine(k).translateToString(true) + '\\n';
  return s; })"""


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.errors: list[str] = []

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                desc = (d.get("exception") or {}).get("description") or d.get("text")
                self.errors.append(f"exception: {desc}")
            elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
                self.errors.append("console.error: " + " ".join(
                    str(a.get("value", a.get("description"))) for a in m["params"]["args"]))
            elif m.get("method") == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
                e = m["params"]["entry"]
                self.errors.append(f"log.error: {e.get('text')} {e.get('url', '')}")

    async def cmd(self, method, **params):
        self.i += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.i] = fut
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        r = await asyncio.wait_for(fut, 30)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    async def ev(self, js):
        r = await self.cmd("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"evaluate 失败: {r['exceptionDetails']}")
        return r["result"].get("value")

    async def click_node(self, name):
        rect = await self.ev(f"""(() => {{
          const g = [...document.querySelectorAll('#topo-svg .node')].find(n => n.dataset.name === {json.dumps(name)});
          const r = g.querySelector('.shape').getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2}}; }})()""")
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.cmd("Input.dispatchMouseEvent", type=typ, x=rect["x"], y=rect["y"],
                           button="left", clickCount=1)
        return rect

    async def wait_for(self, js, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            v = await self.ev(js)
            if v:
                return v
            await asyncio.sleep(0.3)
        return await self.ev(js)


async def run_none(c, reader) -> int:
    """服务以 AINET_ENSP_RUNNING_IDS= 空值启动：提示没有运行的设备，终端连接被拒并给出原因。"""
    results = []
    await c.wait_for("window.__ainet && window.__ainet.state.cur", 15)
    await asyncio.sleep(1)
    text = await c.ev("document.getElementById('topo-status').textContent")
    empty = await c.ev("getComputedStyle(document.getElementById('topo-empty')).display + '|' + document.getElementById('topo-empty').textContent")
    nodes = await c.ev("document.querySelectorAll('#topo-svg .node').length")
    print(f"  状态文字={text!r}  画布提示={empty!r}  节点数={nodes}")
    key = "eNSP 未运行" if "eNSP 未运行" in text else "没有运行的设备"
    results.append(step("n1", f"状态文字与画布提示「{key}」，不画拓扑",
                        key in text and empty.startswith("flex|") and key in empty and nodes == 0))
    await c.ev("window.__ainet.openTab({name: 'R1', com_port: 2000}), 0")
    got = await c.wait_for(f"({BUF_JS})(2000)?.includes('[拒绝]')", 10)
    buf = await c.ev(f"({BUF_JS})(2000)")
    line = next((l for l in (buf or "").splitlines() if "拒绝" in l), "")
    dot = await c.ev("window.__ainet.state.tabs.get(2000).el.querySelector('.dot').className")
    print(f"  终端输出={line!r}  标签状态点={dot!r}")
    results.append(step("n2", "终端连接被拒并说明原因，标签不是绿点",
                        bool(got) and key in line and dot != "dot open"))
    print("errors total:", c.errors or "无")
    results.append(step("n3", "全程无 JS 异常", not c.errors))
    reader.cancel()
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


async def run_campus(c, reader) -> int:
    """真实场景：eNSP 中运行着 Campus_Network 的部分设备。"""
    results = []
    # 拓扑面板放宽，便于和 eNSP 截图对照
    await c.ev("localStorage.setItem('ainet.ui.topoW', '720')")
    await c.cmd("Page.reload")
    await c.wait_for("window.__ainet && window.__ainet.state.topo && document.querySelectorAll('#topo-svg .node').length > 0", 15)
    await asyncio.sleep(1.5)
    api = await c.ev("fetch('api/topology/status').then(r => r.json())")
    devs = await c.ev("window.__ainet.state.topo.devices.map(d => ({id: d.id, name: d.name, model: d.model, com_port: d.com_port, kind: d.kind}))")
    cls = await c.ev("Object.fromEntries([...document.querySelectorAll('#topo-svg .node')].map(n => [n.dataset.name, n.getAttribute('class')]))")
    text = await c.ev("document.getElementById('topo-status').textContent")
    running = sorted(d["name"] for d in devs if api["devices"][d["id"]] == "online")
    online = [d for d in devs if "online" in cls[d["name"]].split()]
    total = sum(1 for d in devs if d["com_port"] > 0)
    print(f"  状态文字={text!r}  method={api['method']}")
    print(f"  运行中({len(running)})={running}")
    print(f"  online 节点={[(d['name'], d['com_port']) for d in online]}")
    bad = [n for d in devs for n in [d["name"]] if (
        (d["com_port"] <= 0 and cls[n].split()[1] != "na")
        or (d["com_port"] > 0 and api["devices"][d["id"]] == "online" and "clickable" not in cls[n])
        or (d["com_port"] > 0 and api["devices"][d["id"]] == "stopped" and ("stopped" not in cls[n] or "clickable" in cls[n])))]
    pcs = [d["name"] for d in devs if d["kind"] == "pc"]
    print(f"  PC={[(n, cls[n]) for n in pcs]}  不一致节点={bad}")
    results.append(step("c1", f"状态文字 Campus_Network（运行 {len(running)} / {total}），节点样式与状态一致",
                        text == f"eNSP · 拓扑 Campus_Network · 运行 {len(running)}/{total} 台"
                        and api["state"] == "running" and not bad and len(online) >= 2
                        and all(cls[n].split()[1] == "na" for n in pcs)))
    # 可用 AINET_E2E_PICK=AR5,LSW1 指定要打开的两台（eNSP console 刚断开的会话需要一段时间才再出回显）
    want = [n for n in os.environ.get("AINET_E2E_PICK", "").split(",") if n]
    picks = [d for d in online if d["name"] in want] if want else online[:2]
    for d in picks:
        await c.click_node(d["name"])
    cond = " && ".join(f"{PROMPT_JS}(({BUF_JS})({d['com_port']}) || '')" for d in picks)
    got = await c.wait_for(cond, 15)
    print("  提示符等待结果:", got)
    for d in picks:
        await c.ev(f"window.__ainet.state.tabs.get({d['com_port']}).ws.send(new TextEncoder().encode('display clock\\r'))")
    await asyncio.sleep(2)
    echo = {}
    for d in picks:
        port = d["com_port"]
        buf = await c.ev(f"({BUF_JS})({port})") or ""
        lines = [l for l in buf.splitlines() if l.strip()]
        echo[d["name"]] = [l for l in lines if "display clock" in l][-1:] + lines[-3:]
        print(f"  {d['name']}({d['com_port']}) 提示符={await c.ev(f'({PROMPT_NAME_JS})({port})')}  末尾={echo[d['name']]}")
    dots = await c.ev("Object.fromEntries([...window.__ainet.state.tabs.values()].map(t => [t.name, t.el.querySelector('.dot').className]))")
    print("  标签状态点:", dots)
    results.append(step("c2", "两台在线设备终端有 VRP 提示符回显、display clock 有输出、标签绿点",
                        bool(got) and all(any("display clock" in l for l in v) for v in echo.values())
                        and all(v == "dot open" for v in dots.values()) and len(dots) == 2))
    await c.ev("window.__ainet.fitView()")
    await asyncio.sleep(0.5)
    shot = await c.cmd("Page.captureScreenshot", format="png")
    with open(SHOT, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
    print("  screenshot:", os.path.abspath(SHOT))
    print("errors total:", c.errors or "无")
    results.append(step("c3", "全程无 JS 异常", not c.errors))
    reader.cancel()
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


def step(n, text, ok):
    print(f"[{'PASS' if ok else 'FAIL'}] {n}. {text}")
    return ok


async def run() -> int:
    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
         "--window-size=1600,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=900,
                        deviceScaleFactor=1, mobile=False)
            results = []

            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            st = await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                            "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
            print("login status:", st)
            await c.cmd("Page.navigate", url=f"{BASE}/")
            if MODE == "none":
                return await run_none(c, reader)
            if MODE == "campus":
                return await run_campus(c, reader)
            await c.wait_for("window.__ainet && window.__ainet.state.topo && document.querySelectorAll('#topo-svg .node').length > 0", 15)
            await asyncio.sleep(1)

            # 1. JS 异常
            print("errors after load:", c.errors or "无")
            results.append(step(1, "页面加载无 JS 异常 / console error", not c.errors))

            # 2. 状态文字：按 eNSP 中运行的设备确定（无下拉、无自动识别按钮）
            name = await c.ev("window.__ainet.state.topo.name")
            text = await c.ev("document.getElementById('topo-status').textContent")
            gone = await c.ev("!document.getElementById('topo-select') && !document.getElementById('btn-detect')")
            await c.ev("document.getElementById('btn-refresh').click()")
            await asyncio.sleep(1)
            text2 = await c.ev("document.getElementById('topo-status').textContent")
            print(f"  topo={name!r}  状态文字={text!r}  刷新后={text2!r}  下拉/识别按钮已删除={gone}")
            results.append(step(2, "状态文字显示 2-1Single-Area OSPF（运行 2 / 3）",
                                name == "2-1Single-Area OSPF" and gone
                                and text == text2 == "eNSP · 拓扑 2-1Single-Area OSPF · 运行 2/3 台"))

            # 3. SVG 节点与连线
            nodes = await c.ev("[...document.querySelectorAll('#topo-svg .node')].map(n => n.dataset.name + ':' + n.getAttribute('class'))")
            nlinks = await c.ev("document.querySelectorAll('#topo-svg .link line').length")
            iflabels = await c.ev("[...document.querySelectorAll('#topo-svg .if-label')].map(t => t.textContent)")
            notes = await c.ev("[...document.querySelectorAll('#topo-svg .note')].map(t => t.textContent)")
            print("  nodes:", nodes)
            print("  links:", nlinks, " if-labels:", iflabels)
            print("  notes:", notes)
            cls = {k: v.split() for k, v in (n.split(":", 1) for n in nodes)}
            await c.click_node("R3")
            await c.click_node("CLOUD1")
            await asyncio.sleep(0.5)
            ntabs0 = await c.ev("window.__ainet.state.tabs.size")
            print(f"  点 R3（已停止）/ CLOUD1 后标签数={ntabs0}")
            ok3 = (len(nodes) == 4 and nlinks == 3
                   and all("online" in cls[r] and "clickable" in cls[r] for r in ("R1", "R2"))
                   and "stopped" in cls["R3"] and "clickable" not in cls["R3"]
                   and "na" in cls["CLOUD1"] and "clickable" not in cls["CLOUD1"] and ntabs0 == 0)
            results.append(step(3, "4 个节点（R1/R2 online 可点，R3 stopped 不可点，CLOUD1 na 不可点）+ 3 条线", ok3))

            # 4. 点 R2、R1，各自发 display ip interface brief
            print("  click R2 at", await c.click_node("R2"))
            r2_prompt = await c.wait_for(f"{PROMPT_JS}(({BUF_JS})(2001) || '')", 10)
            print("  R2 缓冲区出现 VRP 提示符:", r2_prompt, await c.ev(f"({PROMPT_NAME_JS})(2001)"))
            print("  click R1 at", await c.click_node("R1"))
            await c.wait_for(f"{PROMPT_JS}(({BUF_JS})(2000) || '')", 10)
            print("  R1 提示符:", await c.ev(f"({PROMPT_NAME_JS})(2000)"))
            tabs_state = await c.ev("[...window.__ainet.state.tabs.values()].map(t => [t.name, t.port, t.ws.readyState, t.status])")
            print("  tabs:", tabs_state)
            # 再点一次 R2：只聚焦，不新开
            await c.click_node("R2")
            await asyncio.sleep(0.5)
            ntabs = await c.ev("window.__ainet.state.tabs.size")
            active = await c.ev("window.__ainet.state.active")
            print(f"  重复点 R2 后标签数={ntabs} 当前={active}")
            # 两个标签各发不同的 display 命令，验证输出互不串台
            await c.ev("window.__ainet.state.tabs.get(2000).ws.send(new TextEncoder().encode('display ip interface brief\\r'))")
            await c.ev("window.__ainet.state.tabs.get(2001).ws.send(new TextEncoder().encode('display version\\r'))")
            await c.wait_for(f"({BUF_JS})(2000)?.includes('Interface') && ({BUF_JS})(2001)?.includes('VRP')", 10)
            b1 = await c.ev(f"({BUF_JS})(2000)")
            b2 = await c.ev(f"({BUF_JS})(2001)")
            g1 = [l for l in b1.splitlines() if "GigabitEthernet0/0/0" in l or "LoopBack0" in l or "display" in l]
            g2 = [l for l in b2.splitlines() if "VRP" in l or "display" in l][:3]
            print("  R1 buffer:", g1)
            print("  R2 buffer:", g2)
            ok4 = (bool(r2_prompt) and all(s[2] == 1 for s in tabs_state) and len(tabs_state) == 2
                   and ntabs == 2 and active == 2001
                   and "display ip interface brief" in b1 and "display version" not in b1
                   and "display version" in b2 and "display ip interface brief" not in b2)
            results.append(step(4, "R2/R1 两个标签同时连接，各自输出正确", ok4))

            # 5. 左右分屏
            await c.ev("document.getElementById('btn-split').click()")
            # 经 UI 下拉指定每屏显示的标签
            await c.ev("""[[0, '2000'], [1, '2001']].forEach(([i, v]) => {
              const s = document.querySelectorAll('.pane-select')[i];
              s.value = v; s.dispatchEvent(new Event('change')); })""")
            await asyncio.sleep(1)
            panes = await c.ev("""[0, 1].map(i => {
              const pane = document.querySelectorAll('.pane')[i];
              const port = window.__ainet.state.panes[i];
              const t = window.__ainet.state.tabs.get(port);
              const r = t.host.getBoundingClientRect();
              return {pane: i, port, name: t.name, inPane: pane.contains(t.host),
                      w: Math.round(r.width), h: Math.round(r.height), left: Math.round(r.left),
                      cols: t.term.cols, rows: t.term.rows, ws: t.ws.readyState}; })""")
            for p in panes:
                print("  pane:", p)
            ok5 = (panes[0]["name"] == "R1" and panes[1]["name"] == "R2"
                   and all(p["inPane"] and p["w"] > 200 and p["h"] > 200 and p["ws"] == 1 for p in panes)
                   and panes[1]["left"] > panes[0]["left"])
            # 分屏下再发一次命令，两边都能收
            for port in (2000, 2001):
                await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode('display clock\\r'))")
            await asyncio.sleep(2)
            clocks = [await c.ev(f"({BUF_JS})({p})?.includes('display clock')") for p in (2000, 2001)]
            print("  分屏后 display clock 回显:", clocks)
            results.append(step(5, "左右分屏分别显示 R1、R2，均可见且已连接", ok5 and all(clocks)))

            # 6. 截图
            shot = await c.cmd("Page.captureScreenshot", format="png")
            with open(SHOT, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("  screenshot:", os.path.abspath(SHOT))
            results.append(step(6, "截图已保存", os.path.getsize(SHOT) > 10000))

            # 6b. 标签状态点：R1/R2 绿；强行打开已停止的 R3 也不能是绿点
            await c.ev("window.__ainet.openTab(window.__ainet.state.topo.devices.find(d => d.name === 'R3')), 0")
            await asyncio.sleep(4)
            dots = await c.ev("Object.fromEntries([...window.__ainet.state.tabs.values()].map(t => [t.name, t.el.querySelector('.dot').className]))")
            print("  标签状态点:", dots)
            results.append(step("6b", "R1/R2 标签绿点，已停止的 R3 标签不是绿点",
                                dots.get("R1") == dots.get("R2") == "dot open" and dots.get("R3") != "dot open"))

            # 7. ?port= 兼容：新页面直接打开对应设备。eNSP console 释放上一条会话要一点时间，
            # 先关掉本页标签再等几秒，否则新连接可能收不到输出
            await c.ev("[...window.__ainet.state.tabs.keys()].forEach(p => window.__ainet.closeTab(p)), 0")
            await asyncio.sleep(3)
            await c.cmd("Page.navigate", url=f"{BASE}/?port=2001")
            got = await c.wait_for(f"window.__ainet && window.__ainet.state.tabs.get(2001) && {PROMPT_JS}(({BUF_JS})(2001) || '')", 15)
            names = await c.ev("[...window.__ainet.state.tabs.values()].map(t => t.name)")
            print(f"  ?port=2001 打开标签={names} 缓冲区出现提示符={got} {await c.ev(f'({PROMPT_NAME_JS})(2001)')}")
            print("  2001 缓冲区:", repr((await c.ev(f"({BUF_JS})(2001)") or "").strip()[-200:]))
            results.append(step(7, "?port=2001 自动打开 R2", names == ["R2"] and bool(got)))

            print("errors total:", c.errors or "无")
            if c.errors:
                results.append(step(8, "全程无 JS 异常", False))
            reader.cancel()
            print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
            return 0 if all(results) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
