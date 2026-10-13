"""工作台布局 e2e：分栏滑块、隐藏式滚动条下的列数、窄宽度标签栏不换行（不进默认 pytest）。

前置同 browser_e2e.py（服务已起，eNSP 中运行着 2-1 OSPF 的 R1、R2）。只发 display 类命令。
用法：python tests/e2e/layout_e2e.py [base_url] [截图目录]
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_e2e import BUF_JS, CDP, CHROME, PROMPT_JS, step  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
SHOT_DIR = sys.argv[2] if len(sys.argv) > 2 else "."
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9335"))
LIVE = (2000, 2001)  # R3(2002) 可能离线：只要求 R1/R2 保持连接

WAIT_TOPO = ("window.__ainet && window.__ainet.state.topo && "
             "document.querySelectorAll('#topo-svg .node').length > 0")

# 每个可见终端：列数、按 fit 公式应得的列数、画布与视口宽度
TERMS_JS = """(() => [...window.__ainet.state.tabs.values()]
  .filter(t => t.opened && t.host.parentElement.id !== 'parking').map(t => {
    const core = t.term._core, cell = core._renderService.dimensions.css.cell.width;
    const hs = getComputedStyle(t.host);
    const avail = t.host.clientWidth - parseFloat(hs.paddingLeft) - parseFloat(hs.paddingRight);
    const sb = core.viewport.scrollBarWidth;
    const vp = t.host.querySelector('.xterm-viewport'), scr = t.host.querySelector('.xterm-screen');
    return {name: t.name, cols: t.term.cols, expect: Math.max(2, Math.floor((avail - sb) / cell)),
            cell: +cell.toFixed(2), avail: Math.round(avail), sb,
            screenW: Math.round(scr.getBoundingClientRect().width),
            viewportW: Math.round(vp.getBoundingClientRect().width), ws: t.ws.readyState};
  }))()"""

MEASURE_JS = """(() => {
  const w = id => Math.round(document.getElementById(id).getBoundingClientRect().width);
  const panes = [...document.querySelectorAll('.pane')].map(p => Math.round(p.getBoundingClientRect().width));
  return {topo: w('topo-panel'), term: w('term-area'), panes,
          ls: {topo: localStorage.getItem('ainet.ui.topoW'), panes: localStorage.getItem('ainet.ui.panes')}};
})()"""


async def center_of(c, sel):
    return await c.ev(f"""(() => {{ const r = document.querySelector({json.dumps(sel)}).getBoundingClientRect();
      return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, display: getComputedStyle(document.querySelector({json.dumps(sel)})).display}}; }})()""")


async def drag(c, sel, dx):
    p = await center_of(c, sel)
    x, y = p["x"], p["y"]
    await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    await c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
    steps = 10
    for i in range(1, steps + 1):
        await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x + dx * i / steps, y=y,
                    button="left", buttons=1)
        await asyncio.sleep(0.03)
    await c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x + dx, y=y, button="left", buttons=0, clickCount=1)
    await asyncio.sleep(0.6)


async def shot(c, name):
    path = os.path.abspath(os.path.join(SHOT_DIR, name))
    data = await c.cmd("Page.captureScreenshot", format="png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(data["data"]))
    print("  screenshot:", path)
    return path


def cols_ok(terms):
    return all(t["cols"] == t["expect"] and t["screenW"] <= t["viewportW"] for t in terms)


async def open_devices(c, names):
    for n in names:
        if n in ("R1", "R2"):
            await c.click_node(n)
        else:  # 已停止的设备不可点，直接打开标签以保持三个标签的布局断言
            await c.ev(f"window.__ainet.openTab(window.__ainet.state.topo.devices.find(d => d.name === {json.dumps(n)})), 0")
    ports = {"R1": 2000, "R2": 2001, "R3": 2002}
    cond = " && ".join(f"{PROMPT_JS}(({BUF_JS})({ports[n]}) || '')" for n in names if ports[n] in LIVE)
    return await c.wait_for(cond, 15)


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

            async def viewport(w):
                await c.cmd("Emulation.setDeviceMetricsOverride", width=w, height=900,
                            deviceScaleFactor=1, mobile=False)
                await asyncio.sleep(0.6)

            await viewport(1600)
            results = []
            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            st = await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                            "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
            await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.')).forEach(k => localStorage.removeItem(k))")
            print("login status:", st)
            # 当前拓扑由 eNSP 中运行的设备决定（Linux 上用 AINET_ENSP_RUNNING_IDS 覆盖为 R1、R2）
            sel = await c.ev("fetch('api/topology/current').then(r=>r.json()).then(t=>t.state + ':' + (t.topology && t.topology.name))")
            print("topology:", sel)
            await c.cmd("Page.navigate", url=f"{BASE}/")
            await c.wait_for(WAIT_TOPO, 15)
            await asyncio.sleep(1)

            # a. 三个标签 + 三种宽度下标签栏 / 工具栏 / 屏头高度不变
            got = await open_devices(c, ["R1", "R2", "R3"])
            st3 = await c.ev("[...window.__ainet.state.tabs.values()].map(t => [t.name, t.port, t.ws.readyState, t.status])")
            print("  R1/R2 提示符均出现:", bool(got), " 标签:", st3)
            rows = []
            for w in (1600, 900, 700):
                await viewport(w)
                m = await c.ev("""(() => {
                  const h = s => +document.querySelector(s).getBoundingClientRect().height.toFixed(2);
                  const tabs = [...document.querySelectorAll('#tab-list .tab')].map(t => {
                    const r = t.getBoundingClientRect(), nm = t.querySelector('.nm');
                    return {top: +r.top.toFixed(2), w: Math.round(r.width), h: Math.round(r.height),
                            btns: [...t.querySelectorAll('button')].every(b => b.getBoundingClientRect().width > 0
                                   && b.getBoundingClientRect().right <= r.right + 0.5),
                            ellipsis: nm.scrollWidth > nm.clientWidth}; });
                  const list = document.getElementById('tab-list');
                  return {tabsH: h('#tabs'), barH: h('#bar'), tabs,
                          listScroll: [list.scrollWidth, list.clientWidth],
                          term: Math.round(document.getElementById('term-area').getBoundingClientRect().width)};
                })()""")
                terms = await c.ev(TERMS_JS)
                print(f"  width={w}: #tabs.height={m['tabsH']} #bar.height={m['barH']} term-area={m['term']} "
                      f"tab-list scrollWidth/clientWidth={m['listScroll']}")
                print(f"    tabs={m['tabs']}")
                print(f"    terms={terms}")
                rows.append((m, terms))
                if w == 700:
                    await c.ev("window.__ainet.focusTab(2000)")
                    await asyncio.sleep(0.6)
                    await shot(c, "layout_narrow_700.png")
            hs = [m["tabsH"] for m, _ in rows]
            bars = [m["barH"] for m, _ in rows]
            one_row = all(len({t["top"] for t in m["tabs"]}) == 1 and len(m["tabs"]) == 3
                          and all(t["btns"] for t in m["tabs"]) for m, _ in rows)
            # 2026-10 布局调整：标签栏 40px、顶栏 52px（docs/design/ui-layout.html）
            results.append(step("a", f"标签栏高度 {hs} 三次相等且为单行(40)，顶栏高度 {bars} 不变，标签 top 一致",
                                len(set(hs)) == 1 and hs[0] == 40 and len(set(bars)) == 1 and one_row))
            results.append(step("a2", "三种宽度下 fit 列数 = floor((可视宽 - 滚动条宽) / 字宽)，画布不超出视口",
                                all(cols_ok(t) for _, t in rows)))

            # b. 拖动拓扑 / 终端滑块
            await viewport(1600)
            await c.ev("window.__ainet.focusTab(2000)")
            await asyncio.sleep(0.5)
            before = await c.ev(MEASURE_JS)
            t_before = await c.ev(TERMS_JS)
            await drag(c, "#split-main", 200)
            after = await c.ev(MEASURE_JS)
            t_after = await c.ev(TERMS_JS)
            wsall = await c.ev(f"{list(LIVE)}.map(p => window.__ainet.state.tabs.get(p).ws.readyState)")
            print(f"  拖动前 topo={before['topo']} term={before['term']} cols={[t['cols'] for t in t_before]}")
            print(f"  拖动后 topo={after['topo']} term={after['term']} cols={[t['cols'] for t in t_after]} ws={wsall} ls={after['ls']}")
            dt, dterm = after["topo"] - before["topo"], before["term"] - after["term"]
            results.append(step("b", f"拓扑 +{dt}px，终端区 -{dterm}px，列数变化且 fit 正确，ws 全为 1",
                                abs(dt - 200) <= 6 and abs(dterm - 200) <= 6
                                and t_after[0]["cols"] < t_before[0]["cols"] and cols_ok(t_after)
                                and all(s == 1 for s in wsall)))

            # c. 左右分屏后拖动中间滑块
            await c.ev("document.getElementById('btn-split').click()")
            await c.ev("""[[0, '2000'], [1, '2001']].forEach(([i, v]) => {
              const s = document.querySelectorAll('.pane-select')[i];
              s.value = v; s.dispatchEvent(new Event('change')); })""")
            await asyncio.sleep(0.8)
            pb = await c.ev(MEASURE_JS)
            tb = await c.ev(TERMS_JS)
            sp = await center_of(c, "#split-panes")
            await drag(c, "#split-panes", -80)
            pa = await c.ev(MEASURE_JS)
            ta = await c.ev(TERMS_JS)
            print(f"  分屏拖动前 panes={pb['panes']} cols={[(t['name'], t['cols']) for t in tb]} 滑块={sp}")
            print(f"  分屏拖动后 panes={pa['panes']} cols={[(t['name'], t['cols']) for t in ta]} ls={pa['ls']}")
            print(f"    terms={ta}")
            for port in (2000, 2001):
                await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode('display clock\\r'))")
            await asyncio.sleep(2)
            clocks = [await c.ev(f"({BUF_JS})({p})?.includes('display clock')") for p in (2000, 2001)]
            hh = await c.ev("[...document.querySelectorAll('.pane-head')].map(h => +h.getBoundingClientRect().height.toFixed(2))")
            print(f"  display clock 回显={clocks} 屏头高度={hh}")
            dl, dr = pb["panes"][0] - pa["panes"][0], pa["panes"][1] - pb["panes"][1]
            bycols = {t["name"]: t["cols"] for t in tb}, {t["name"]: t["cols"] for t in ta}
            results.append(step("c", f"左屏 -{dl}px 右屏 +{dr}px，左列数减少右列数增加，两边连接且有回显",
                                abs(dl - 80) <= 6 and abs(dr - 80) <= 6
                                and bycols[1]["R1"] < bycols[0]["R1"] and bycols[1]["R2"] > bycols[0]["R2"]
                                and cols_ok(ta) and all(t["ws"] == 1 for t in ta) and all(clocks)
                                and len(set(hh)) == 1))
            await shot(c, "layout_split_dragged.png")

            # d. 刷新后比例保持
            await c.cmd("Page.reload")
            await c.wait_for(WAIT_TOPO, 15)
            await asyncio.sleep(0.8)
            await open_devices(c, ["R1", "R2"])
            await c.ev("document.getElementById('btn-split').click()")
            await asyncio.sleep(0.8)
            pr = await c.ev(MEASURE_JS)
            print(f"  刷新后 topo={pr['topo']} panes={pr['panes']} ls={pr['ls']}（刷新前 topo={pa['topo']} panes={pa['panes']}）")
            results.append(step("d", "刷新后拓扑宽度与分屏比例保持（localStorage）",
                                abs(pr["topo"] - pa["topo"]) <= 2 and abs(pr["panes"][0] - pa["panes"][0]) <= 2
                                and bool(pr["ls"]["topo"]) and bool(pr["ls"]["panes"])))

            # 折叠隐藏滑块、展开恢复；双击恢复默认
            await c.ev("document.getElementById('btn-collapse').click()")
            await asyncio.sleep(0.5)
            col = await center_of(c, "#split-main")
            await c.ev("document.getElementById('btn-expand-topo').click()")
            await asyncio.sleep(0.5)
            exp = await c.ev(MEASURE_JS)
            p = await center_of(c, "#split-main")
            await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=p["x"], y=p["y"])
            for n in (1, 2):
                await c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=p["x"], y=p["y"], button="left", buttons=1, clickCount=n)
                await c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=p["x"], y=p["y"], button="left", clickCount=n)
            await asyncio.sleep(0.6)
            dbl = await c.ev(MEASURE_JS)
            main_w = await c.ev("document.getElementById('main').getBoundingClientRect().width")
            print(f"  折叠时滑块 display={col['display']}；展开后 topo={exp['topo']}；双击后 topo={dbl['topo']} "
                  f"(默认 300px，主区 {main_w}) ls={dbl['ls']}")
            results.append(step("d2", "折叠隐藏滑块、展开恢复宽度、双击恢复默认（300px）",
                                col["display"] == "none" and abs(exp["topo"] - pr["topo"]) <= 2
                                and abs(dbl["topo"] - 300) <= 3 and dbl["ls"]["topo"] is None))

            # e. 全程无 JS 异常
            print("errors total:", c.errors or "无")
            results.append(step("e", "全程无 JS 异常 / console error", not c.errors))
            reader.cancel()
            print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
            return 0 if all(results) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
