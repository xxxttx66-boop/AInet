"""课程页「准备环境浮层」的浏览器 e2e + 截图（里程碑 6.13；只用假环境，不进默认 pytest）。

假 eNSP Pro（fake_enspro_lab：同进程假 REST + 假 STOMP console；设备启动 start_delay 秒），题目 e2e-pc-t1t2（AR1 / AR2 / PC1）。
AR2 模拟真机的启动卡住（lab.delayed.stuck：停止 2 次后才正常启动）：
  O1  开始练习：遮罩 + 居中卡片（role=dialog、aria-modal），标题「正在准备练习环境」+ 题名 + 环境 / 沙箱；
      步骤条（切换环境 / 查找沙箱 / 启动设备 / 等待运行 / 确认就绪 / 完成，进行中的有 aria-current）、
      进度条（role=progressbar、aria-valuenow 只增不减）、已用时间、预计还需、设备卡片（启动中）、提示、本题工单  → 截图 starting
  O2  不自动重试（retries=0）：AR2 启动超时 → 浮层错误态（后端中文原因、「重试」「返回课程」）               → 截图 error
  O3  点「重试」（retries=1）：AR2 先停后启仍卡住 → 看门狗自动重启，警示「AR2 启动卡住，正在自动重启（第 1 次…）」，
      设备卡片「重启中」、「等待运行」步骤警示色                                                               → 截图 restart
  O4  完成态「环境已就绪」（doneHold 调大便于截图）→ 自动进入操作页、练习进行中                              → 截图 done
  O5  390px：浮层卡片在视口内、无横向滚动（每个状态都在 1300px 与 390px 各截一张）
  X   全程无 JS 异常

用法：python tests/e2e/prepare_overlay_e2e.py [截图目录]
环境变量：AINET_E2E_HTTP_PORT（缺省 8799）、AINET_E2E_DEBUG_PORT（缺省 9359）。只绑定 127.0.0.1，跑完关掉服务与浏览器。
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from browser_e2e import CDP, CHROME, step  # noqa: E402
from exam_e2e import shot  # noqa: E402

HTTP_PORT = int(os.environ.get("AINET_E2E_HTTP_PORT", "8799"))
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9359"))
TOPIC = "e2e-pc-t1t2"
AR2 = 7
WIDE, NARROW = (1300, 860), (390, 844)
COURSE_READY = ("window.__ainetCourses && window.__ainetCourses.st.data && "
                "document.querySelectorAll('#c-topics .c-card').length > 0")
# 浮层状态快照
OV_JS = """(() => { const ov = document.getElementById('c-prep-ov'), card = document.getElementById('c-progress');
  const r = card.getBoundingClientRect(), bar = document.getElementById('pv-bar');
  return {shown: !ov.hidden, state: ov.dataset.state, role: card.getAttribute('role'), modal: card.getAttribute('aria-modal'),
    title: document.getElementById('c-prog-title').textContent, sub: document.getElementById('pv-sub').textContent,
    steps: [...document.querySelectorAll('#pv-steps .pv-step')].map(li => [li.dataset.key, li.className.replace('pv-step ', ''),
      li.querySelector('.pv-ss').textContent, li.hasAttribute('aria-current')]),
    pct: +bar.getAttribute('aria-valuenow'), prole: bar.getAttribute('role'),
    elapsed: document.getElementById('c-prog-elapsed').textContent, eta: document.getElementById('pv-eta').textContent,
    stage: document.getElementById('c-prog-stage').textContent,
    alert: document.getElementById('pv-alert').hidden ? '' : document.getElementById('pv-alert').className + '|' + document.getElementById('pv-alert').textContent,
    devs: [...document.querySelectorAll('#c-prog-devs .pv-dev')].map(d => [d.dataset.name, d.dataset.st, d.textContent]),
    tip: document.getElementById('pv-tip-t').textContent, ticket: document.getElementById('pv-ticket-body').textContent.slice(0, 20),
    btns: ['c-cancel', 'pv-retry', 'pv-back'].filter(id => !document.getElementById(id).hidden),
    live: document.getElementById('pv-live').textContent, inert: document.getElementById('c-main').hasAttribute('inert'),
    box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    vw: innerWidth, sw: document.documentElement.scrollWidth}; })()"""


def chk(n, text, ok) -> bool:
    return step(n, text, bool(ok))


async def ov(c):
    return await c.ev(OV_JS)


async def size(c, wh):
    w, h = wh
    await c.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=w < 600)
    await asyncio.sleep(0.6)


async def both(c, shot_dir, name):
    """当前状态在 1300px 与 390px 各截一张；返回 390px 时的浮层快照（检查不溢出）。"""
    await shot(c, shot_dir, f"{name}_1300.png")
    await size(c, NARROW)
    o = await ov(c)
    await shot(c, shot_dir, f"{name}_390.png")
    await size(c, WIDE)
    return o


def fits(o) -> bool:
    x, y, w, h = o["box"]
    return o["sw"] <= o["vw"] and x >= 16 and x + w <= o["vw"] - 16


async def run(shot_dir: str) -> int:
    results: list[bool] = []
    from fake_enspro_lab import fake_enspro_lab
    chrome = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
                               "--no-sandbox", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with fake_enspro_lab(os.path.join(shot_dir, "work"), HTTP_PORT, start_delay=25.0, stuck={AR2: 2}) as lab:
            import main
            power = main.app.state.labmgr.power
            power.start_timeout, power.early_check, power.retries = 35.0, 0.0, 0
            tabs = None
            for _ in range(50):
                try:
                    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json"))
                    break
                except OSError:
                    await asyncio.sleep(0.2)
            ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
            async with websockets.connect(ws_url, max_size=None) as ws:
                c = CDP(ws)
                reader = asyncio.create_task(c.reader())
                for d in ("Runtime", "Log", "Page"):
                    await c.cmd(f"{d}.enable")
                await size(c, WIDE)
                await c.cmd("Page.navigate", url=lab.base + "/login")
                await c.wait_for("document.getElementById('login')", 10)
                await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.')).forEach(k => localStorage.removeItem(k)), 0")
                await c.ev("document.getElementById('login').value = 'admin'; document.getElementById('password').value = 'admin';"
                           "document.getElementById('loginForm').requestSubmit(), 0")
                await c.wait_for("location.pathname.endsWith('/courses')", 10)
                await c.wait_for(COURSE_READY, 20)
                await c.ev("document.querySelector('.c-chap[data-id=\"L02\"]').click(), 0")
                await asyncio.sleep(0.3)
                await c.ev(f"document.querySelector('.c-card[data-id=\"{TOPIC}\"]').click(), 0")
                await asyncio.sleep(0.4)
                await c.ev("window.__ainetCourses.st.doneHold = 6000, 0")

                # ---- O1：准备中（启动设备）----
                await c.ev("document.getElementById('c-practice').click(), 0")
                await c.wait_for("[...document.querySelectorAll('#c-prog-devs .pv-dev')].filter(d => d.dataset.st === 'starting').length >= 2", 30)
                await asyncio.sleep(3.5)                    # 让进度条与「预计还需」走一点
                pcts = []
                for _ in range(4):
                    pcts.append((await ov(c))["pct"])
                    await asyncio.sleep(1.0)
                o1 = await ov(c)
                o1n = await both(c, shot_dir, "01_starting")
                print(f"  O1：{json.dumps(o1, ensure_ascii=False)}\n  进度采样 {pcts}")
                keys = [s[0] for s in o1["steps"]]
                results.append(chk("O1", "准备中：遮罩 + 居中卡片（dialog / aria-modal、背景 inert），标题 + 题名 + 环境 / 沙箱；"
                                         "步骤条六步、当前步骤 aria-current；进度条 progressbar 只增不减；已用 / 预计还需；设备卡片启动中；提示与工单",
                                   o1["shown"] and o1["role"] == "dialog" and o1["modal"] == "true" and o1["inert"]
                                   and o1["title"] == "正在准备练习环境" and "AR1 与 AR2 互联不通" in o1["sub"]
                                   and "eNSP Pro" in o1["sub"] and "沙箱 ainet_test" in o1["sub"]
                                   and keys == ["switch", "sandbox", "start", "wait", "probe", "done"]
                                   and sum(s[3] for s in o1["steps"]) == 1 and any("s-done" in s[1] for s in o1["steps"])
                                   and o1["prole"] == "progressbar" and 0 < o1["pct"] < 60 and pcts == sorted(pcts)
                                   and o1["elapsed"].startswith("已用 ") and "预计还需" in o1["eta"]
                                   and any(d[1] == "starting" and "启动中" in d[2] for d in o1["devs"])
                                   and o1["tip"] and o1["ticket"].startswith("工单") and o1["btns"] == ["c-cancel"]))

                # ---- O2：错误态（retries=0，AR2 启动超时 → start_failed）----
                await c.wait_for("document.getElementById('c-prep-ov').dataset.state === 'error'", 120)
                await asyncio.sleep(0.5)
                o2 = await ov(c)
                o2n = await both(c, shot_dir, "03_error")
                print(f"  O2：{json.dumps(o2, ensure_ascii=False)}")
                results.append(chk("O2", "错误态：浮层内显示后端中文原因（AR2 启动失败…）、出错步骤标红，「重试」「返回课程」按钮",
                                   o2["shown"] and o2["title"] == "没能开始练习" and o2["alert"].startswith("pv-alert err|")
                                   and "AR2 启动失败" in o2["alert"] and any("s-error" in s[1] for s in o2["steps"])
                                   and o2["btns"] == ["pv-retry", "pv-back"] and o2["devs"]
                                   and any(d[0] == "AR2" and d[1] == "start_failed" for d in o2["devs"])))

                # ---- O3：重试 → 看门狗自动重启 AR2 ----
                power.retries = 1
                await c.ev("document.getElementById('pv-retry').click(), 0")
                await c.wait_for("!document.getElementById('pv-alert').hidden && "
                                 "document.getElementById('pv-alert').classList.contains('warn')", 120)
                await asyncio.sleep(1.0)
                o3 = await ov(c)
                o3n = await both(c, shot_dir, "02_restart")
                print(f"  O3：{json.dumps(o3, ensure_ascii=False)}")
                wait = next((s for s in o3["steps"] if s[0] == "wait"), ["", "", ""])
                results.append(chk("O3", "重启阶段：警示「AR2 启动卡住，正在自动重启（第 1 次，最多 1 次）」，AR2 卡片「重启中」，「等待运行」步骤警示色",
                                   "AR2 启动卡住，正在自动重启（第 1 次，最多 1 次）" in o3["alert"]
                                   and any(d[0] == "AR2" and d[1] == "restarting" and "重启中" in d[2] for d in o3["devs"])
                                   and "warn" in wait[1] and wait[2] == "自动重启" and o3["state"] == "busy"))

                # ---- O4：完成态 → 自动进入操作页 ----
                await c.wait_for("document.getElementById('c-prep-ov').dataset.state === 'done'", 180)
                await asyncio.sleep(0.3)
                o4 = await ov(c)
                o4n = await both(c, shot_dir, "04_done")
                print(f"  O4：{json.dumps(o4, ensure_ascii=False)}")
                went = await c.wait_for("location.search.includes('mode=practice')", 20)
                await asyncio.sleep(2.0)
                active = await c.wait_for("window.__ainet && window.__ainet.state.practice && window.__ainet.state.practice.active", 30)
                results.append(chk("O4", "完成态：「环境已就绪」、进度 100%、步骤都已完成（或已跳过）→ 停留后自动进操作页、练习进行中",
                                   o4["title"] == "环境已就绪" and o4["pct"] == 100
                                   and all(s[1] in ("s-done", "s-skipped") for s in o4["steps"])
                                   and o4["btns"] == [] and went and active))
                results.append(chk("O5", "390px：四个状态的浮层卡片都在视口内（左右各留 16px）、页面无横向滚动",
                                   all(fits(o) for o in (o1n, o2n, o3n, o4n))))
                print("errors total:", c.errors or "无")
                results.append(chk("X", "全程无 JS 异常 / console error", not c.errors))
                reader.cancel()
    finally:
        chrome.terminate()
        try:
            chrome.wait(10)
        except subprocess.TimeoutExpired:
            chrome.kill()
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "prepare_overlay_shots")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    rc = asyncio.run(run(out))
    print(f"  用时 {int(time.time() - t0)} 秒；截图在 {out}")
    sys.exit(rc)
