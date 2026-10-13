"""工作台布局与 Agent 面板的浏览器 e2e（模拟设备版，不进默认 pytest，不连任何真实设备 / eNSP Pro）。

用 fake_lab 起 3 台假 VRP 与临时库的服务，无头 Chrome 依次检查（布局稿 docs/design/ui-layout.html 的 4 个场景）：
  L  实验模式（课题从课程页带入 ?s=）：单层顶栏（模式、题名 ▾，环境与拓扑在气泡里）、拓扑 | 终端 | 右侧只有 Agent
  A  Agent 对话（自由问答）：流式回复、「已在 X 执行（只读）：命令」、只读命令不进学员终端
  C  收起状态：两侧收成 48px 图标栏，终端加宽；面板收起时有新回复 → 图标圆点
  D  拖动宽度：拓扑 / 右侧面板可拖宽，刷新后保持（localStorage）；双击恢复默认
  P  练习：顶栏同一行（「练习 + 题名」、用时、判定、立即检查、结束练习）、切到实验要确认（取消则留在练习）、工单详情 / Agent 导师两个页签、
     导师模式提示条、三级提示按钮（3 级标「扣分」）、提示进对话流；收起状态截图；结束练习
  E  考试（课程页「考试」里开模板）：顶栏变红、右侧面板与图标栏都不出现、终端加宽、Agent 接口 403、打码拓扑只列设备、
     题名气泡里的工单、交卷二次确认
  N  窄屏（< 1100px）：右侧面板改抽屉，图标栏打开 / 遮罩关闭
  G  独立页面（档案）用同一套顶栏
  X  全程无 JS 异常
用法：python tests/e2e/ui_e2e.py [截图目录] [--ai]
  --ai：Agent 用本机配置的真 DeepSeek（少量调用）；不加则屏蔽 Key，只检查「未配置」的友好提示。
环境变量：AINET_E2E_HTTP_PORT（缺省 8783）、AINET_E2E_DEBUG_PORT（缺省 9343）。
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

from browser_e2e import CDP, CHROME, PROMPT_JS, step  # noqa: E402
from exam_e2e import buf, shot, start_from_courses, type_cmd  # noqa: E402
from fake_lab import fake_lab  # noqa: E402

HTTP_PORT = int(os.environ.get("AINET_E2E_HTTP_PORT", "8783"))
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9343"))
AR0, AR2 = 2000, 2003
SID = "campus-ar0-ar2-link"

BOX_JS = """(sel => { const e = document.querySelector(sel); if (!e) return null;
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
          display: cs.display, visibility: cs.visibility}; })"""


def chk(n, text, ok) -> bool:
    return step(n, text, bool(ok))


async def box(c, sel):
    return await c.ev(f"({BOX_JS})({json.dumps(sel)})")


async def shown(c, sel) -> bool:
    b = await box(c, sel)
    return bool(b) and b["display"] != "none" and b["visibility"] != "hidden" and b["w"] > 0


async def drag(c, sel, dx):
    p = await box(c, sel)
    x, y = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
    await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    await c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
    for i in range(1, 11):
        await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x + dx * i / 10, y=y, button="left", buttons=1)
        await asyncio.sleep(0.03)
    await c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x + dx, y=y, button="left", clickCount=1)
    await asyncio.sleep(0.4)


async def dblclick(c, sel):
    p = await box(c, sel)
    x, y = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
    for n in (1, 2):
        await c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=n)
        await c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=n)
    await asyncio.sleep(0.4)


async def ask(c, text, timeout=150):
    """经 Agent 输入框提问，等回答结束。返回最后一条回复的信息。"""
    await c.ev(f"(() => {{ const i = document.getElementById('agent-input'); i.value = {json.dumps(text)};"
               " document.getElementById('agent-form').requestSubmit(); })()")
    await asyncio.sleep(0.5)
    await c.wait_for("!window.__ainetAgent.ag.busy", timeout)
    return await c.ev("""(() => { const bots = document.querySelectorAll('#agent-log .ag-bot');
      const b = bots[bots.length - 1]; if (!b) return null;
      return {text: [...b.querySelectorAll('.md')].map(x => x.innerText).join('\\n').trim(),
              tools: [...b.querySelectorAll('.ag-tool summary')].map(x => x.innerText),
              cmds: [...b.querySelectorAll('.ag-tool .cmd')].map(x => x.innerText),
              ok_tools: [...b.querySelectorAll('details.ag-tool .cmd')].map(x => x.innerText),
              error: (b.querySelector('.ag-error') || {}).innerText || '',
              penalty: (b.querySelector('.ag-penalty') || {}).innerText || ''}; })()""")


async def width(c, sel):
    b = await box(c, sel)
    return b["w"] if b else 0


async def run(shot_dir: str, use_ai: bool) -> int:
    results: list[bool] = []
    async with fake_lab(shot_dir, HTTP_PORT, ai=use_ai) as (base, net):
        chrome = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
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
                await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1,
                            mobile=False)
                await c.cmd("Page.navigate", url=base + "/login")
                await asyncio.sleep(1)
                await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                           "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
                await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.'))"
                           ".forEach(k => localStorage.removeItem(k)), 0")
                await c.cmd("Page.navigate", url=f"{base}/?mode=lab&s={SID}")
                await c.wait_for("window.__ainet && window.__ainetAgent && window.__ainet.state.topo && "
                                 "document.querySelectorAll('#topo-svg .node').length > 0", 15)
                await asyncio.sleep(1)

                # ---- L：自由实验布局 ----
                body = await c.ev("document.body.className")
                env = await c.ev("document.getElementById('tp-env').textContent + ' · ' + "
                                 "document.getElementById('topo-status').textContent")
                env_hidden = not await shown(c, "#topo-status")          # 环境与拓扑不常驻顶栏，在题名气泡里
                L = {k: await shown(c, s) for k, s in (("side", "#side"), ("task_tab", "#st-task"),
                                                       ("agent_tab", "#st-agent"), ("pr_bar", "#pr-active-view"),
                                                       ("ex_bar", "#ex-active-view"), ("rail", "#side-rail"))}
                wl = {k: await width(c, s) for k, s in (("topo", "#topo-panel"), ("side", "#side"),
                                                        ("term", "#term-area"))}
                print(f"  body={body!r} env={env!r} 可见={L} 宽度={wl}")
                await c.click_node("AR0")
                await c.wait_for(f"{PROMPT_JS}(({await _buf_js()})({AR0}) || '')", 20)
                await type_cmd(c, AR0, "display ip interface brief")
                await shot(c, shot_dir, "01_lab.png")
                results.append(chk("L", "实验模式：题名气泡里是「eNSP · 拓扑 · 运行 x/y 台」（不常驻顶栏）；拓扑 | 终端 | 右侧只有 Agent；无练习 / 考试控制",
                                    "mode-lab" in body and "运行" in env and "eNSP" in env and env_hidden and L["side"]
                                    and not L["task_tab"] and L["agent_tab"] and not L["pr_bar"]
                                    and not L["ex_bar"] and not L["rail"] and wl["topo"] == 300 and wl["side"] == 380))

                # ---- A：Agent 对话（自由问答） ----
                b_before = await buf(c, AR0)
                r = await ask(c, "请在 AR0 上执行 display ip interface brief 和 display version，"
                                 "告诉我 GE0/0/1 的地址和状态，以及设备版本。")
                print(f"  Agent 回复：{json.dumps(r, ensure_ascii=False)[:600]}")
                await asyncio.sleep(1)
                b_after = await buf(c, AR0)
                term_clean = all(b_after.count(cmd) == b_before.count(cmd) for cmd in (r or {}).get("ok_tools", []))
                await shot(c, shot_dir, "02_agent_lab.png")
                if use_ai:
                    ok = bool(r and r["text"] and not r["error"] and r["ok_tools"]
                              and all("已在 AR0 执行（只读）" in t for t in r["tools"] if "已在" in t) and term_clean)
                    results.append(chk("A", f"Agent 流式回答，显示「已在 AR0 执行（只读）：{r and r['ok_tools']}」，"
                                             "这些命令不出现在学员的 AR0 终端里", ok))
                else:
                    results.append(chk("A", "无 Key：Agent 给出友好提示（未配置），界面不卡住",
                                        bool(r and "没有 DeepSeek API Key" in r["error"])))
                # 学员命令进了快照（服务端 TermLog），Agent 的只读命令没有
                hist = await c.ev("fetch('api/agent/history').then(r => r.json())")
                print(f"  history: {[(m['role'], m['content'][:30]) for m in hist['messages']]}")

                # ---- C：收起状态 + 新回复圆点 ----
                await c.ev("document.getElementById('btn-side-collapse').click(), "
                           "document.getElementById('btn-collapse').click(), 0")
                await asyncio.sleep(0.5)
                rails = (await shown(c, "#topo-rail"), await shown(c, "#side-rail"))
                tw = await width(c, "#term-area")
                if use_ai:
                    await c.ev("window.__ainetAgent.send('用一句话说说 AR0 和 AR2 之间怎么连的'), 0")
                else:
                    await c.ev("window.__ainetAgent.send('在吗'), 0")
                await asyncio.sleep(0.5)
                await c.wait_for("!window.__ainetAgent.ag.busy", 120)
                await asyncio.sleep(0.3)
                dot = await c.ev("!document.getElementById('agent-dot').hidden")
                label = await c.ev("document.getElementById('rail-label').textContent")
                print(f"  收起：rails={rails} 终端宽={tw} 圆点={dot} 标签={label!r} ls={await c.ev('localStorage.getItem(\"ainet.ui.sideCollapsed\")')}")
                await shot(c, shot_dir, "03_collapsed_lab.png")
                await c.ev("document.getElementById('rail-agent').click(), 0")
                await asyncio.sleep(0.4)
                dot2 = await c.ev("!document.getElementById('agent-dot').hidden")
                await c.ev("document.getElementById('btn-expand-topo').click(), 0")
                await asyncio.sleep(0.4)
                back = (await width(c, "#topo-panel"), await width(c, "#side"))
                results.append(chk("C", f"两侧收起为 48px 图标栏（终端宽 {tw}px），收起时新回复在图标上显示圆点；展开后圆点消失、宽度恢复 {back}",
                                    rails == (True, True) and tw >= 1440 - 2 * 48 - 2 and dot and "新回复" in label
                                    and not dot2 and back == (300, 380)))

                # ---- D：拖动宽度、刷新保持、双击恢复 ----
                await drag(c, "#split-side", -120)
                await drag(c, "#split-main", 80)
                d1 = (await width(c, "#topo-panel"), await width(c, "#side"))
                ls = await c.ev("[localStorage.getItem('ainet.ui.topoW'), localStorage.getItem('ainet.ui.sideW')]")
                await c.cmd("Page.reload")
                await c.wait_for("window.__ainet && window.__ainet.state.topo", 15)
                await asyncio.sleep(1)
                d2 = (await width(c, "#topo-panel"), await width(c, "#side"))
                await dblclick(c, "#split-side")
                d3 = await width(c, "#side")
                print(f"  拖动后 {d1} ls={ls}；刷新后 {d2}；双击右侧滑块后 side={d3}")
                await shot(c, shot_dir, "04_dragged.png")
                results.append(chk("D", "拓扑 +80px、右侧 +120px，存 localStorage，刷新后保持；双击滑块恢复默认 380",
                                    abs(d1[0] - 380) <= 3 and abs(d1[1] - 500) <= 3 and d2 == d1 and ls[0] and ls[1]
                                    and d3 == 380))
                await dblclick(c, "#split-main")

                # ---- P：练习（实验 → 练习：直接开始本题练习） ----
                await c.ev("document.getElementById('vt-practice').click(), 0")
                lst = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 60)
                await asyncio.sleep(1.5)
                P = {
                    "bar": await shown(c, "#pr-active-view"), "title": await c.ev("document.getElementById('topic-title').textContent"),
                    "brief": await c.ev("document.getElementById('tp-ticket').textContent"),
                    "timer": await c.ev("document.getElementById('pr-timer').textContent"),
                    "check": await c.ev("document.getElementById('pr-check-result').textContent"),
                    "ticket": await c.ev("document.getElementById('pr-symptom').textContent"),
                    "btns": await c.ev("[...document.querySelectorAll('#mode-nav button')].map(b => b.id + ':' + b.disabled)"),
                    "inbar": await c.ev("!!document.getElementById('pr-active-view').closest('#bar')"),
                    "tabs": await c.ev("[...document.querySelectorAll('#side-tabs button')].filter(b => getComputedStyle(b).display !== 'none').map(b => b.textContent)"),
                    "endbtn": await shown(c, "#pr-end"), "checkbtn": await shown(c, "#pr-check"),
                }
                # 练习中切到实验要确认（放弃练习）；这里点「取消」，留在练习
                await c.ev("window.__cf = []; window.confirm = (t) => (window.__cf.push(t), false); "
                           "document.getElementById('vt-topo').click(), 0")
                await asyncio.sleep(0.3)
                P["mode_after_click"] = await c.ev("window.__ainet.state.mode")
                P["confirm"] = await c.ev("window.__cf")
                print(f"  练习：{json.dumps(P, ensure_ascii=False)}")
                await shot(c, shot_dir, "05_practice_ticket.png")
                results.append(chk("P1", "练习控制在单层顶栏里（题名、工单摘要在气泡、用时、判定、立即检查、结束练习）；切到实验要确认，"
                                          "取消则留在练习；右侧「工单详情 / Agent 导师」",
                                    lst and P["bar"] and P["inbar"] and P["title"] and P["brief"].startswith("工单：") and P["ticket"]
                                    and P["check"].startswith("通过 0/1") and P["endbtn"] and P["checkbtn"]
                                    and P["btns"] == ["vt-topo:false", "vt-practice:false", "vt-exam:false"]
                                    and len(P["confirm"]) == 1 and "放弃本次练习" in P["confirm"][0]
                                    and P["tabs"] == ["工单详情", "Agent 导师"] and P["mode_after_click"] == "practice"))
                await c.ev("document.getElementById('st-agent').click(), 0")
                await asyncio.sleep(0.3)
                hb = await c.ev("[...document.querySelectorAll('#agent-hints .hint-lv')].map(b => [b.textContent, b.disabled])")
                note = await shown(c, "#agent-note")
                # 导师：学员先从工单里的设备按钮打开 AR0（刷新页面后标签已关）、看一眼，再问导师
                await c.ev(f"document.querySelector('.pr-dev[data-port=\"{AR0}\"]').click(), 0")
                await c.wait_for(f"{PROMPT_JS}(({await _buf_js()})({AR0}) || '')", 20)
                await type_cmd(c, AR0, "ping 10.0.12.2")
                r = await ask(c, "AR0 ping 不通 10.0.12.2，我该先查什么？")
                print(f"  导师回复：{json.dumps(r, ensure_ascii=False)[:500]}")
                hu = await c.ev("window.__ainet.state.practice.hints_used")
                await c.ev("window.__ainet.requestHint(1), 0")
                await c.wait_for("document.querySelectorAll('#agent-log .ag-hint').length >= 1 && "
                                 "document.querySelectorAll('#pr-hints .hint').length >= 1", 30)
                await asyncio.sleep(0.5)
                await shot(c, shot_dir, "06_practice_mentor.png")
                mentor_ok = bool(r and r["text"] and not r["error"]) if use_ai else bool(r and r["error"])
                penalty_consistent = (not r or not r["penalty"]) or hu >= 3
                print(f"  提示按钮={hb} 导师提示条={note} 回复后 hints_used={hu} 扣分提示={bool(r and r['penalty'])}")
                results.append(chk("P2", "Agent 导师页签：导师模式提示条、三级提示按钮（3 级标「扣分」，2/3 级未解锁前禁用）；"
                                          "提问本身不扣分，回复判定泄露才按 3 级记扣分；提示进对话流",
                                    note and len(hb) == 3 and "扣分" in hb[2][0] and hb[0][1] is False and hb[1][1] is True
                                    and hb[2][1] is True and mentor_ok and penalty_consistent))
                # 收起状态（布局稿 ④）：练习中两侧收起，取提示 → 导师图标出圆点
                await c.ev("document.getElementById('btn-side-collapse').click(), "
                           "document.getElementById('btn-collapse').click(), 0")
                await asyncio.sleep(0.4)
                await c.ev("window.__ainet.requestHint(2), 0")
                await c.wait_for("document.querySelectorAll('#agent-log .ag-hint').length >= 2", 30)
                await asyncio.sleep(0.4)
                dotp = await c.ev("!document.getElementById('agent-dot').hidden")
                lbl = await c.ev("document.getElementById('rail-label').textContent")
                await c.ev("window.__ainet.split || window.__ainet.setSplit(true), 0")
                await shot(c, shot_dir, "07_collapsed_practice.png")
                results.append(chk("P3", f"练习中收起两侧（布局稿 ④）：顶栏练习控制保留，导师图标有新回复圆点（{lbl}）",
                                    dotp and lbl == "导师有新回复" and await shown(c, "#pr-active-view")))
                await c.ev("window.__ainet.setSplit(false), document.getElementById('btn-expand-topo').click(), "
                           "document.getElementById('rail-task').click(), 0")
                await c.ev("window.confirm = () => true")
                await c.ev("document.getElementById('pr-end').click(), 0")
                ok_sum = await c.wait_for("!document.getElementById('pr-summary-view').hidden", 60)
                unlocked = await c.ev("[...document.querySelectorAll('#mode-nav button')].every(b => !b.disabled)")
                bar_gone = not await shown(c, "#pr-active-view")
                results.append(chk("P4", "结束练习：顶栏练习控制消失、模式都可点、右侧显示练习总结",
                                    bool(ok_sum) and unlocked and bar_gone))

                # ---- E：考试 ----
                tid = await c.ev("""fetch('api/exam/templates', {method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({name: 'ui e2e 考试', config: {questions: [{scenario: 'campus-ar0-ar2-link', seed: 0}],
                  duration_s: 1800, question_duration_s: 900, retries: 1, hints: {allowed: true, penalty: 2, max_level: 2},
                  show_topology: false}})}).then(r => r.json()).then(t => t.id)""")
                tw_before = await width(c, "#term-area")
                await start_from_courses(c, base, tid)           # 多题考试模板在课程页「考试」里开
                await c.wait_for("window.__ainet.state.exam && window.__ainet.state.exam.active && "
                                 "window.__ainet.state.exam.question && window.__ainet.state.exam.question.status === 'active'", 60)
                await asyncio.sleep(1.5)
                E = {
                    "bar": await shown(c, "#ex-active-view"), "side": await shown(c, "#side"),
                    "rail": await shown(c, "#side-rail"), "split": await shown(c, "#split-side"),
                    "term": await width(c, "#term-area"),
                    "chat": await c.ev("fetch('api/agent/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                                       " body: JSON.stringify({message: '这题怎么做'})}).then(r => r.status)"),
                    "hist": await c.ev("fetch('api/agent/history').then(r => r.status)"),
                    "devlist": await c.ev("[...document.querySelectorAll('#topo-devlist button')].map(b => b.textContent + ':' + b.disabled)"),
                    "masked": await c.ev("document.getElementById('topo-body').classList.contains('masked')"),
                    "ticket": await shown(c, "#ex-ticket"),
                    "timer": await c.ev("document.getElementById('ex-timer').textContent"),
                    "dots": await c.ev("document.querySelectorAll('#ex-progress .ex-dot').length"),
                    "hint": await shown(c, "#ex-hint"), "submit": await shown(c, "#ex-submit"),
                    "finish": await shown(c, "#ex-finish"), "env": await c.ev("document.getElementById('topo-status').textContent"),
                }
                await c.ev("document.querySelector('#topo-devlist button:not(:disabled)').click(), 0")
                await asyncio.sleep(1.5)
                await c.ev("window.__ainet.setTopicPop(true), 0")
                await asyncio.sleep(0.3)
                print(f"  考试：{json.dumps(E, ensure_ascii=False)}")
                await shot(c, shot_dir, "08_exam.png")
                results.append(chk("E1", f"考试：顶栏变红（倒计时 / 题号 / 题名气泡里的工单 / 提交 / 交卷）；右侧面板与图标栏都不出现，"
                                          f"终端加宽（{tw_before}→{E['term']}px）；Agent 接口 403；打码拓扑只列可登录设备",
                                    E["bar"] and not E["side"] and not E["rail"] and not E["split"]
                                    and E["term"] > tw_before + 300 and E["chat"] == 403 and E["hist"] == 403
                                    and E["masked"] and len(E["devlist"]) >= 2 and E["ticket"] and E["dots"] == 1
                                    and E["hint"] and E["submit"] and E["finish"] and "Campus" not in E["env"]))
                await c.ev("document.getElementById('ex-finish').click(), 0")
                confirm = await c.wait_for("!document.getElementById('ex-confirm').hidden", 5)
                await shot(c, shot_dir, "09_exam_confirm.png")
                await c.ev("document.getElementById('ex-confirm-yes').click(), 0")
                done = await c.wait_for("!document.getElementById('ex-done-view').hidden", 90)
                await asyncio.sleep(0.5)
                after = (await shown(c, "#side"), await shown(c, "#ex-active-view"))
                results.append(chk("E2", "交卷二次确认后：考试控制消失、右侧面板回来显示「已交卷」与成绩单链接",
                                    bool(confirm) and bool(done) and after == (True, False)
                                    and bool(await c.ev("!!document.getElementById('ex-report-link')"))))

                # ---- N：窄屏抽屉 ----
                await c.ev("document.getElementById('vt-topo').click(), 0")
                await c.cmd("Emulation.setDeviceMetricsOverride", width=1000, height=760, deviceScaleFactor=1, mobile=False)
                await asyncio.sleep(0.6)
                n0 = {"narrow": await c.ev("document.body.classList.contains('narrow')"),
                      "side": await shown(c, "#side"), "rail": await shown(c, "#side-rail"),
                      "term": await width(c, "#term-area")}
                await c.ev("document.getElementById('rail-agent').click(), 0")
                await asyncio.sleep(0.5)
                sb = await box(c, "#side")
                n1 = {"open": await c.ev("document.body.classList.contains('drawer-open')"), "side": await shown(c, "#side"),
                      "box": sb, "mask": await shown(c, "#drawer-mask")}
                await shot(c, shot_dir, "10_narrow_drawer.png")
                await c.ev("document.getElementById('drawer-mask').click(), 0")
                await asyncio.sleep(0.5)
                n2 = await c.ev("document.body.classList.contains('drawer-open')")
                print(f"  窄屏：{n0} 打开 {n1} 关闭后 drawer-open={n2}")
                results.append(chk("N", "窄屏（1000px）：右侧面板改抽屉，图标栏打开、遮罩关闭，终端不被挤压",
                                    n0["narrow"] and not n0["side"] and n0["rail"] and n1["open"] and n1["side"]
                                    and n1["mask"] and sb["x"] + sb["w"] <= 1000 - 48 + 1 and not n2
                                    and n0["term"] >= 1000 - 300 - 48 - 4))
                await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)

                # ---- G：独立页面顶栏 ----
                await c.cmd("Page.navigate", url=base + "/profile")
                await c.wait_for("document.getElementById('who') && document.getElementById('who').textContent.includes('admin')", 10)
                await asyncio.sleep(0.8)
                g = {"bar": await box(c, "header.topbar"), "on": await c.ev("document.querySelector('.mode-nav a.on').textContent")}
                await shot(c, shot_dir, "11_profile_topbar.png")
                results.append(chk("G", "档案页使用同一套顶栏（56px，档案高亮，显示学生）",
                                    g["bar"] and g["bar"]["h"] == 56 and g["on"] == "档案"))

                # 考试中脚本自己探测 Agent 接口（期望 403）会在控制台留两条资源加载失败，单独计数、不算页面异常
                probes = [e for e in c.errors if "403 (Forbidden)" in e and "/api/agent/" in e]
                others = [e for e in c.errors if e not in probes]
                print("errors total:", others or "无", f"（考试中探测 Agent 接口的 403：{len(probes)} 条，预期 2）")
                results.append(chk("X", "全程无 JS 异常 / console error（页面自身没有请求被拒的 Agent 接口）",
                                   not others and len(probes) == 2))
                reader.cancel()
        finally:
            chrome.terminate()
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


async def _buf_js():
    from browser_e2e import BUF_JS
    return BUF_JS


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else os.path.join(os.getcwd(), "ui_e2e_shots")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    rc = asyncio.run(run(out, "--ai" in sys.argv))
    print(f"  用时 {int(time.time() - t0)} 秒；截图在 {out}")
    sys.exit(rc)
