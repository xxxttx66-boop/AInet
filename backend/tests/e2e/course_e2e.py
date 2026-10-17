"""课程选择页 / 操作页单层顶栏 / PC 设置气泡的浏览器 e2e（里程碑 6.9；只用假环境，不进默认 pytest）。

阶段一：假 eNSP Pro（fake_enspro_lab：同进程的假 REST + 假 STOMP console，设备启动有延时；eNSP 经典版离线）
  C1  登录页登录后进课程页
  C2  环境列：eNSP Pro 在线可点；eNSP 离线置灰、写「（离线）」；「↻ 重新检测环境」带 refresh=1
  C3  章节 / 课题 / 预览联动：章节题数、「待补充」、课题卡片（完成状态 / 难度 / 用时）、预览（标签、拓扑预览橙框、准备说明）；
      选择存 localStorage，刷新后保持
  C4  「开始练习」：准备浮层（阶段、各设备状态、已用时间）→ 进入操作页、练习进行中（浮层细节见 prepare_overlay_e2e.py）
  C5  单层顶栏：模式 / 「练习 + 题名 ▾」/ 用时 / 判定 / 按钮 / 学员名 / 退出到课程在同一行；题名气泡（环境、拓扑与运行台数、本题设备、
      章节难度、工单摘要、刷新设备状态、查看完整工单）
  C6  学员菜单：我的档案 / 切换学员 / 登出
  C7  PC 设置气泡（练习中）：读出当前地址 → 改 IP → 等 PC 命令行恢复后「已生效」→ 打开终端
  C8  模式切换：练习 → 实验（确认框，放弃并复位，PC 地址还原；操作记录里有「[PC 配置] …」）→ 练习 → 考试（确认框）
  C9  单题考试：顶栏「考试 第 1 / 1 题 ▾」、题干常驻、其他模式不可切；可以暂离且计时继续，返回后恢复；考试中改 PC 地址；
      修好后提交本题 → 交卷（二次确认）→ 成绩单 → 命令回放里有「[PC 配置] PC1 …」
  C10 退出到课程：练习中确认后按放弃结束；实验中直接回课程页；课题卡片完成状态更新
  C11 窄屏：课程页四列改上下排列、无横向滚动；操作页顶栏换行，按钮都在视口内
  C12 隐藏拓扑的考试：左栏设备按钮里的 PC 同样弹出 PC 设置气泡
阶段二：假 eNSP 经典版（fake_lab：3 台假 VRP，eNSP Pro 离线）
  K1  环境列：eNSP 在线、eNSP Pro 离线
  K2  所需拓扑没在运行：提示「请在 eNSP 中打开 … 拓扑并启动设备」，轮询，可取消
  K3  拓扑在运行：直接进入练习；经典版 PC 气泡只提示在 eNSP 软件中设置、输入框禁用
  X   全程无 JS 异常

用法：python tests/e2e/course_e2e.py [截图目录]
环境变量：AINET_E2E_HTTP_PORT（缺省 8797，阶段二用 +1）、AINET_E2E_DEBUG_PORT（缺省 9357）。
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

from browser_e2e import CDP, CHROME, PROMPT_JS, step, stop_browser  # noqa: E402
from exam_e2e import shot, type_cmd  # noqa: E402

HTTP_PORT = int(os.environ.get("AINET_E2E_HTTP_PORT", "8797"))
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9357"))
TOPIC = "e2e-pc-t1t2"
AR2, PC1 = 7, 12

BOX_JS = """(sel => { const e = document.querySelector(sel); if (!e) return null;
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
          display: cs.display, visibility: cs.visibility}; })"""
CONFIRM_JS = "window.__confirms = window.__confirms || []; window.confirm = (t) => (window.__confirms.push(t), true); 0"
BUF_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) s += b.getLine(k).translateToString(true) + '\\n';
  return s; })"""


def chk(n, text, ok) -> bool:
    return step(n, text, bool(ok))


async def box(c, sel):
    return await c.ev(f"({BOX_JS})({json.dumps(sel)})")


async def shown(c, sel) -> bool:
    b = await box(c, sel)
    return bool(b) and b["display"] != "none" and b["visibility"] != "hidden" and b["w"] > 0


async def text(c, sel) -> str:
    return await c.ev(f"(document.querySelector({json.dumps(sel)}) || {{}}).textContent || ''") or ""


async def click(c, sel):
    await c.ev(f"document.querySelector({json.dumps(sel)}).click(), 0")


async def goto(c, url, ready_js, timeout=20):
    await c.cmd("Page.navigate", url=url)
    await asyncio.sleep(0.5)
    ok = await c.wait_for(ready_js, timeout)
    await c.ev(CONFIRM_JS)
    return ok


async def node_click(c, name):
    await c.ev("window.__ainet.fitView(), 0")
    await asyncio.sleep(0.2)
    await c.click_node(name)
    await asyncio.sleep(0.4)


async def confirms(c) -> list:
    return await c.ev("window.__confirms || []") or []


WB_READY = ("window.__ainet && window.__ainet.state.topo && document.querySelectorAll('#topo-svg .node').length > 0 && "
            "document.getElementById('who').textContent !== '…'")
COURSE_READY = ("window.__ainetCourses && window.__ainetCourses.st.data && "
                "document.querySelectorAll('#c-topics .c-card').length > 0")


async def phase_pro(c, shot_dir, results):
    from fake_enspro_lab import fake_enspro_lab
    async with fake_enspro_lab(os.path.join(shot_dir, "work_pro"), HTTP_PORT) as lab:
        import db                     # 必须在 fake_enspro_lab 设好环境变量（临时库）之后再导入
        base = lab.base
        # ---- C1：登录后进课程页 ----
        await c.cmd("Page.navigate", url=base + "/login")
        await c.wait_for("document.getElementById('login')", 10)
        await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.')).forEach(k => localStorage.removeItem(k)), 0")
        await c.ev("document.getElementById('login').value = 'admin'; document.getElementById('password').value = 'admin';"
                   "document.getElementById('loginForm').requestSubmit(), 0")
        landed = await c.wait_for("location.pathname.endsWith('/courses')", 10)
        ready = await c.wait_for(COURSE_READY, 20)
        await c.ev(CONFIRM_JS)
        results.append(chk("C1", f"登录后默认进课程页（{await c.ev('location.pathname')}）", landed and ready))

        # ---- C2：环境列 ----
        envs = await c.ev("""[...document.querySelectorAll('#c-envs .c-env')].map(b => ({key: b.dataset.key,
          dis: b.disabled, on: b.classList.contains('on'), text: b.innerText.replace(/\\s+/g, ' ')}))""")
        await c.ev("window.__fetched = []; const f0 = window.fetch; window.fetch = (u, o) => (window.__fetched.push(String(u)), f0(u, o)); 0")
        await click(c, "#c-recheck")
        await c.wait_for("window.__fetched.some(u => u.includes('api/lab/environments?refresh=1'))", 10)
        await c.wait_for("window.__fetched.some(u => u.includes('api/scenarios?env='))", 10)   # 重新检测后会重取本环境的题
        await asyncio.sleep(1.0)
        fetched = await c.ev("window.__fetched")
        print(f"  环境：{envs}；重新检测请求：{[u for u in fetched if 'environments' in u]}")
        pro = next((e for e in envs if e["key"] == "enspro"), {})
        ens = next((e for e in envs if e["key"] == "ensp"), {})
        results.append(chk("C2", "环境列：eNSP Pro 在线可点（当前浏览）、eNSP 离线置灰并写「（离线）」；「重新检测环境」带 refresh=1",
                           pro and not pro["dis"] and pro["on"] and "在线" in pro["text"] and ens and ens["dis"]
                           and "（离线）" in ens["text"] and any("refresh=1" in u for u in fetched)))

        # ---- C3：章节 / 课题 / 预览联动 + localStorage ----
        chaps = await c.ev("""[...document.querySelectorAll('#c-chapters .c-chap')].map(b => [b.dataset.id, b.disabled,
          b.querySelector('.c-chap-n').textContent])""")
        await click(c, '.c-chap[data-id="L04"]')
        await asyncio.sleep(0.3)
        l04 = await c.ev("[...document.querySelectorAll('#c-topics .c-card strong')].map(x => x.textContent)")
        await click(c, '.c-chap[data-id="L02"]')
        await asyncio.sleep(0.3)
        cards = await c.ev("""[...document.querySelectorAll('#c-topics .c-card')].map(x => ({id: x.dataset.id,
          prog: x.querySelector('.c-prog').textContent, diff: x.querySelector('.c-diff').textContent,
          mins: (x.querySelector('.c-mins') || {}).textContent, sum: x.querySelector('.c-card-sum').textContent}))""")
        await click(c, f'.c-card[data-id="{TOPIC}"]')
        await asyncio.sleep(0.4)
        pv = {"title": await text(c, "#c-preview h1"), "tags": await c.ev("document.querySelectorAll('#c-preview .c-chip').length"),
              "req": await c.ev("document.querySelectorAll('#c-topo-svg .c-node.req').length"),
              "off": await c.ev("document.querySelectorAll('#c-topo-svg .c-node.off').length"),
              "prep": await text(c, "#c-prep"), "btns": await c.ev("[...document.querySelectorAll('.c-btns button')]"
                                                                   ".filter(b => !b.hidden).map(b => b.textContent)")}
        await shot(c, shot_dir, "01_courses.png")
        await c.cmd("Page.reload")
        await c.wait_for(COURSE_READY, 15)
        await asyncio.sleep(0.5)
        kept = (await c.ev("document.querySelector('.c-chap.on').dataset.id"),
                await c.ev("document.querySelector('.c-card.on').dataset.id"),
                await c.ev("localStorage.getItem('ainet.course.topic')"))
        await c.ev(CONFIRM_JS)
        print(f"  章节：{chaps}\n  L04：{l04}\n  L02 卡片：{cards}\n  预览：{pv}\n  刷新后：{kept}")
        cmap = {x[0]: x for x in chaps}
        results.append(chk("C3", "章节 L01~L12 从易到难 + 末尾「考试」，题数 / 「待补充」；课题卡片（完成状态、难度、用时、一句话症状）；"
                                 "预览（标签、拓扑预览本题设备橙框、未启动虚线、准备说明、三个按钮）；刷新后选择保持",
                           [x[0] for x in chaps][:12] == [f"L{i:02d}" for i in range(1, 13)] and chaps[-1][0] == "__exam__"
                           and cmap["L02"][2] == str(len(cards))
                           and all(cmap[x][1] and cmap[x][2] == "待补充" for x in ("L10", "L11"))
                           and any("VLAN" in t for t in l04) and len(cards) >= 3 and any(x["id"] == TOPIC for x in cards)
                           and all(x["prog"] == "未开始" and x["diff"].startswith("难度 ★") and x["sum"] for x in cards)
                           and pv["title"].startswith("AR1 与 AR2 互联不通（PC 气泡") and pv["tags"] == 2 and pv["req"] == 3
                           and pv["off"] == 3 and "沙箱" in pv["prep"] and "启动本题需要的 3 台设备" in pv["prep"]
                           and pv["btns"] == ["准备环境并进入实验", "准备环境并开始练习", "准备环境并开始考试"]
                           and kept == ("L02", TOPIC, TOPIC)))

        # ---- C4：开始练习 → 准备进度 → 操作页 ----
        await click(c, "#c-practice")
        seen = {"stages": set(), "devs": set(), "elapsed": set()}
        shot_done = False
        t0 = time.time()
        while time.time() - t0 < 90:
            if "mode=practice" in (await c.ev("location.search") or ""):
                break
            try:
                st = await text(c, "#c-prog-stage")
                devs = await c.ev("[...document.querySelectorAll('#c-prog-devs .c-dev')].map(x => x.textContent)") or []
                el = await text(c, "#c-prog-elapsed")
            except RuntimeError:
                await asyncio.sleep(0.3)
                continue
            if st:
                seen["stages"].add(st.split(" · ")[0])
            seen["devs"] |= set(devs)
            if el:
                seen["elapsed"].add(el)
            if not shot_done and any("启动中" in d or "系统初始化" in d for d in devs):
                await shot(c, shot_dir, "02_prepare_progress.png")
                shot_done = True
            await asyncio.sleep(0.4)
        await c.wait_for(WB_READY, 20)
        await c.ev(CONFIRM_JS)
        active = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 20)
        print(f"  准备进度：{seen}；URL={await c.ev('location.search')}")
        results.append(chk("C4", "开始练习：准备浮层显示阶段、各设备状态（启动中 → 就绪）、已用时间；完成后进操作页并开始练习",
                           shot_done and any("等待设备运行" in s or "启动设备" in s for s in seen["stages"])
                           and any("就绪" in d for d in seen["devs"]) and seen["elapsed"] and active))

        # ---- C5：单层顶栏 + 题名气泡 ----
        await asyncio.sleep(1)
        hdr = await box(c, "header#bar")
        mainb = await box(c, "#main")
        strips = await c.ev("[...document.querySelectorAll('.strip > div')].filter(d => d.offsetHeight).reduce((a, d) => a + d.offsetHeight + 0, 0)")
        bar = await c.ev("""[...document.querySelectorAll('#bar > *')].filter(e => e.offsetWidth && getComputedStyle(e).display !== 'none')
          .map(e => e.id || e.className)""")
        controls = await c.ev("['pr-active-view', 'pr-timer', 'pr-check', 'pr-end', 'user-btn', 'btn-exit', 'topic-btn']"
                              ".every(id => { const e=document.getElementById(id), r=e.getBoundingClientRect(); "
                              "return e.offsetParent !== null && r.width > 0 && r.right <= innerWidth; })")
        topic = await text(c, "#topic-btn")
        env_status = await text(c, "#topo-status")
        await click(c, "#topic-btn")
        await asyncio.sleep(0.4)
        pop = {"env": await text(c, "#tp-env"), "topo": await text(c, "#topo-status"), "devs": await text(c, "#tp-devs"),
               "chap": await text(c, "#tp-chapter"),
               "btns": await c.ev("[...document.querySelectorAll('.tp-actions button')].filter(b => !b.hidden).map(b => b.textContent)")}
        await shot(c, shot_dir, "03_topbar_popover.png")
        await click(c, "#btn-refresh")
        await asyncio.sleep(0.6)
        await click(c, "#tp-full")                  # 练习中：查看完整工单 = 打开右侧「工单详情」
        await asyncio.sleep(0.4)
        side_task = await c.ev("window.__ainet.layout.side.tab") == "task" and await shown(c, "#pr-detail")
        ticket = await text(c, "#pr-detail")
        print(f"  顶栏={hdr} main.y={mainb['y']} 通知条高={strips} 元素={bar}; controls={controls}; "
              f"工单页={side_task}, 工单内容={ticket[:80]!r}\n  题名={topic!r} 气泡={pop}")
        results.append(chk("C5", "工作台标题区显示模式和课题；操作控制、学员菜单与退出入口可见；气泡显示环境、拓扑、设备和章节，完整工单切到右侧工单页",
                           hdr["h"] <= 80 and hdr["y"] + hdr["h"] <= mainb["y"] <= hdr["y"] + hdr["h"] + 100 and controls
                           and "练习" in topic and "AR1 与 AR2 互联不通" in topic
                           and "eNSP Pro" in pop["env"] and "YunShan" in pop["env"] and "沙箱 ainet_test" in pop["topo"]
                           and "运行 3/3 台" in pop["topo"] and pop["devs"] == "AR1 · AR2 · PC1"
                           and pop["chap"].startswith("L02 IP 编址") and "难度 ★" in pop["chap"]
                           and pop["btns"] == ["↻ 刷新设备状态", "打开工单页"]
                           and side_task and "工单" in ticket and "AR1 与 AR2" in ticket))

        # ---- C6：学员菜单 ----
        await click(c, "#user-btn")
        await asyncio.sleep(0.3)
        menu = await c.ev("[...document.querySelectorAll('#user-menu [role=menuitem]')].map(x => x.textContent)")
        hrefs = await c.ev("[...document.querySelectorAll('#user-menu a')].map(a => a.getAttribute('href'))")
        await shot(c, shot_dir, "04_user_menu.png")
        await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        await c.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        await asyncio.sleep(0.3)
        closed = await c.ev("document.getElementById('user-menu').hidden")
        nav = await c.ev("[...document.querySelectorAll('#mode-nav button')].map(b => b.textContent)")
        results.append(chk("C6", f"学员菜单：{menu}（我的档案 / 切换学员 / 登出），Esc 关闭；顶栏模式只有实验 / 练习 / 考试（{nav}）",
                           menu == ["我的档案", "AI 设置", "切换学员", "登出"] and hrefs == ["profile", "login"] and closed
                           and nav == ["实验", "练习", "考试"]))

        # ---- C7：PC 设置气泡（练习中） ----
        await node_click(c, "PC1")
        await c.wait_for("document.getElementById('pc-ip').value && !document.getElementById('pc-ip').disabled", 15)
        before = await c.ev("['pc-ip', 'pc-mask', 'pc-gw', 'pc-dns'].map(id => document.getElementById(id).value)")
        st_pc = await text(c, "#pc-status")
        # 提交处理在第一个 await 之前就写好「稍候」提示：同一次求值里读出来
        waiting = await c.ev("document.getElementById('pc-ip').value = '10.0.10.66'; document.getElementById('pc-gw').value = '10.0.10.254';"
                             "document.getElementById('pc-form').requestSubmit(); document.getElementById('pc-msg').textContent")
        done = await c.wait_for("document.getElementById('pc-msg').textContent.startsWith('已生效')", 30)
        await shot(c, shot_dir, "05_pc_bubble.png")
        got = lab.rest.attrs.get(PC1, {})
        await click(c, "#pc-term")
        term = await c.wait_for(f"window.__ainet.state.tabs.has({PC1})", 5)
        await asyncio.sleep(1.0)
        print(f"  PC 气泡：读到 {before} 状态 {st_pc!r} 等待提示 {waiting!r} → {await text(c, '#pc-msg')!r}；假设备属性 ip={got.get('ipv4.config.ip')}")
        results.append(chk("C7", "PC 设置气泡：读出当前 IP / 掩码 / 网关；应用时提示稍候，PC 命令行恢复后「已生效」；「打开终端」打开 PC1 终端",
                           before[:3] == ["10.0.10.10", "255.255.255.0", "10.0.10.1"] and st_pc == "运行中"
                           and "稍候" in waiting and done and got.get("ipv4.config.ip") == "10.0.10.66" and term))

        # ---- C8：模式切换 ----
        aid = await c.ev("window.__ainet.state.practice.attempt_id")
        n0 = len(await confirms(c))
        await click(c, "#vt-topo")
        await c.wait_for("window.__ainet.state.mode === 'lab' && !(window.__ainet.state.practice || {}).active", 60)
        cf = (await confirms(c))[n0:]
        reset_done = await c.wait_for(
            "fetch('api/practice/current').then(r => r.json()).then(j => !j.resetting)", 90)
        reset_state = await c.ev("fetch('api/practice/current').then(r => r.json())")
        logs = [l["command"] for l in db.attempt_logs(aid)]
        host = await c.ev(f"fetch('api/lab/devices/{PC1}/host').then(r => r.json())")
        att = db.get_attempt(aid)
        reset_result = reset_state.get("last_result") or {}
        print(f"  练习 → 实验：确认框 {cf}；复位结果 {reset_result.get('reset_ok')}；操作记录 {logs}；PC1 复位后 {host.get('config')}")
        ok_a = (len(cf) == 1 and "放弃" in cf[0] and any(x.startswith("[PC 配置] PC1 ip=10.0.10.66") for x in logs)
                and reset_done and reset_result.get("reset_ok") and host["config"]["ip"] == ""
                and att["passed"] == 0 and att["ended_at"])
        await click(c, "#vt-practice")                 # 实验 → 练习：直接开始，不用确认
        p2 = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 60)
        cf2 = (await confirms(c))[n0 + 1:]
        await asyncio.sleep(1)
        await click(c, "#vt-exam")                     # 练习 → 考试：确认（会放弃练习）
        ex_on = await c.wait_for("window.__ainet.state.exam && window.__ainet.state.exam.active && "
                                 "window.__ainet.state.exam.question && window.__ainet.state.exam.question.status === 'active'", 60)
        cf3 = (await confirms(c))[n0 + 1:]
        results.append(chk("C8", "模式切换：练习 → 实验要确认（放弃并复位，PC1 地址还原，操作记录有「[PC 配置] PC1 …」，按放弃记录）；"
                                 "实验 → 练习直接开始；练习 → 考试要确认（说明会放弃练习）",
                           ok_a and p2 and not cf2[:-1] and ex_on and len(cf3) == 1 and "单题考试" in cf3[0] and "放弃" in cf3[0]))

        # ---- C9：单题考试 ----
        await asyncio.sleep(1)
        E = {"topic": await text(c, "#topic-btn"), "badge": await text(c, "#topic-badge"),
             "btns": await c.ev("[...document.querySelectorAll('#mode-nav button')].map(b => b.disabled)"),
             "exit": await c.ev("document.getElementById('btn-exit').disabled"),
             "exit_label": await text(c, "#btn-exit"), "question_visible": await shown(c, "#exam-question"),
             "question_title": await text(c, "#exam-question-title"), "question_text": await text(c, "#exam-question-text"),
             "bar_bg": await c.ev("getComputedStyle(document.getElementById('bar')).backgroundColor"),
             "side": await shown(c, "#side"), "progress": await shown(c, "#ex-progress"),
             "agent_tab": await shown(c, "#st-agent"), "agent_rail": await shown(c, "#rail-agent"),
             "timer": await text(c, "#ex-timer")}
        # 导航允许暂离考试：服务端保留当前题，倒计时继续；从考试中心的提示可回到本场。
        exam_id = await c.ev("window.__ainet.state.exam.exam_id")
        await c.ev("window.__leavePrompt = ''; window.confirm = (t) => (window.__leavePrompt = t, false)")
        await c.ev("document.querySelector('#app-sidebar [data-nav=exams]').click(), 0")
        cancelled = await c.wait_for("window.__ainet.state.exam.active && location.pathname.endsWith('/')", 3)
        prompt = await c.ev("window.__leavePrompt")
        target_cleared = await c.ev("!document.getElementById('btn-exit').dataset.uiExitHref")
        await c.ev("window.confirm = (t) => (window.__leavePrompt = t, true)")
        await c.ev("document.querySelector('#app-sidebar [data-nav=exams]').click(), 0")
        away = await c.wait_for("location.pathname.endsWith('/courses') && location.search.includes('view=exams')", 15)
        banner = await c.wait_for("document.getElementById('c-active') && !document.getElementById('c-active').hidden && "
                                  "!document.getElementById('c-active-go').hidden", 15)
        resume_note = await text(c, "#c-active-text")
        resume_href = await c.ev("document.getElementById('c-active-go').getAttribute('href')")
        await click(c, "#c-active-go")
        resumed = await c.wait_for(
            f"location.pathname === '/' && window.__ainet && window.__ainet.state.exam && window.__ainet.state.exam.active && "
            f"window.__ainet.state.exam.exam_id === {exam_id} && "
            f"!!document.getElementById('exam-question') && !document.getElementById('exam-question').hidden", 20)
        await c.ev(CONFIRM_JS)
        E["resume_url"] = await c.ev("location.href")
        E["resume_body"] = await c.ev("document.body.className")
        E["resume_nav"] = await c.ev("!!document.getElementById('mode-nav')")
        E["resume_question"] = await shown(c, "#exam-question")
        E["resume_api"] = await c.ev("fetch('api/exam/current').then(r => r.json()).catch(e => ({error:String(e)}))")
        E["resume_button"] = await c.ev("(() => { const b=document.getElementById('c-active-go'); return b && "
                                            "{text:b.textContent,hidden:b.hidden,disabled:b.disabled,href:b.getAttribute('href'), "
                                            "resumeHref:b.dataset.resumeHref,onclick:b.onclick&&b.onclick.toString()} })()")
        print(f"  恢复后页面：resumed={resumed}, url={E['resume_url']!r}, body={E['resume_body']!r}, "
              f"mode_nav={E['resume_nav']}, question={E['resume_question']}, api={E['resume_api']}, "
              f"button={E['resume_button']}")
        if E["resume_nav"]:
            await click(c, "#vt-topo")
        await asyncio.sleep(0.3)
        E["mode_after"] = await c.ev("window.__ainet && window.__ainet.state.mode || ''")
        if E["resume_nav"] and await c.ev("document.getElementById('topic-pop').hidden"):
            await click(c, "#topic-btn")
        await asyncio.sleep(0.4)
        E["pop_ticket"] = await shown(c, "#ex-ticket") if E["resume_nav"] else False
        E["pop_info"] = await shown(c, "#tp-devs") if E["resume_nav"] else False
        E["pop_env"] = await shown(c, "#tp-env") if E["resume_nav"] else False
        E["symptom"] = (await text(c, "#ex-symptom"))[:20] if E["resume_nav"] else ""
        await shot(c, shot_dir, "06_exam_topbar.png")
        if E["resume_nav"]:
            await c.ev("window.__ainet.setTopicPop(false), 0")
        print(f"  考试：{E}; 暂离/恢复：cancelled={cancelled}, target_cleared={target_cleared}, prompt={prompt!r}, "
              f"away={away}, banner={banner}, note={resume_note!r}, href={resume_href!r}, resumed={resumed}")
        results.append(chk("C9a", "单题考试：题干常驻、其他模式禁用；暂离考试可进入考试中心并恢复同一场考试；气泡只放工单",
                           "第 1 / 1 题" in E["topic"] and E["badge"] == "考试" and E["btns"] == [True, True, False]
                           and not E["exit"] and E["exit_label"] == "暂离考试" and E["question_visible"]
                           and E["question_title"] and E["question_text"] and cancelled and target_cleared
                           and "计时继续" in prompt and away and banner and resumed and "计时继续" in resume_note
                           and f"s={TOPIC}" in resume_href and f"s={TOPIC}" in E["resume_url"]
                           and E["mode_after"] == "exam" and not E["side"] and not E["agent_tab"] and not E["agent_rail"]
                           and E["pop_ticket"]
                           and not E["pop_info"] and E["pop_env"] and E["symptom"].startswith("工单") and E["timer"] != "--:--"))
        if not E["resume_nav"]:
            return
        # 考试中改 PC 地址 → 修好 AR2 → 提交 → 交卷 → 成绩单 → 回放
        await node_click(c, "PC1")
        await c.wait_for("document.getElementById('pc-ip').value && !document.getElementById('pc-ip').disabled", 15)
        await c.ev("document.getElementById('pc-ip').value = '10.0.10.77'; document.getElementById('pc-form').requestSubmit(), 0")
        pc_ok = await c.wait_for("document.getElementById('pc-msg').textContent.startsWith('已生效')", 30)
        await c.ev("document.getElementById('pc-close').click(), 0")
        await node_click(c, "AR2")
        await c.wait_for(f"{PROMPT_JS}(({BUF_JS})({AR2}) || '')", 30)
        for cmd in ("system-view", "interface GE0/0/1", "ip address 10.0.12.2 24", "return"):
            await type_cmd(c, AR2, cmd)
        await click(c, "#ex-submit")
        passed = await c.wait_for("window.__ainet.state.exam.question && window.__ainet.state.exam.question.passed", 40)
        await click(c, "#ex-finish")
        cfm = await c.wait_for("!document.getElementById('ex-confirm').hidden", 5)
        await click(c, "#ex-confirm-yes")
        done = await c.wait_for("!document.getElementById('ex-done-view').hidden && !!document.getElementById('ex-report-link')", 90)
        after = {"btns": await c.ev("[...document.querySelectorAll('#mode-nav button')].map(b => b.disabled)"),
                 "exit": await c.ev("document.getElementById('btn-exit').disabled")}
        await shot(c, shot_dir, "07_exam_done.png")
        href = await c.ev("document.getElementById('ex-report-link').href")
        await c.cmd("Page.navigate", url=href)
        await c.wait_for("document.querySelectorAll('#replay-list .rp').length > 0", 30)
        await asyncio.sleep(1)
        replay = await c.ev("document.getElementById('replay-list').innerText")
        summary = await c.ev("document.body.innerText")
        await shot(c, shot_dir, "08_exam_report.png")
        print(f"  交卷后 {after}；PC={pc_ok} 通过={passed} 确认条={cfm} 已交卷={bool(done)}；回放：{replay[:300]!r}")
        results.append(chk("C9b", "考试中 PC 改地址生效；修好后提交本题通过 → 交卷二次确认 → 已交卷与成绩单链接 → 成绩单命令回放里有「[PC 配置] PC1 ip=10.0.10.77 …」",
                           pc_ok and passed and cfm and done and after == {"btns": [False, False, False], "exit": False}
                           and "[PC 配置] PC1 ip=10.0.10.77" in replay and "单题考试" in summary))

        # ---- C10：退出到课程 ----
        await goto(c, f"{base}/?mode=practice&s={TOPIC}", WB_READY)
        await c.wait_for("document.querySelector('.pr-card .pr-start') && !document.querySelector('.pr-card .pr-start').disabled", 15)
        await click(c, "#vt-practice")
        await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 60)
        await asyncio.sleep(1)
        n1 = len(await confirms(c))
        await click(c, "#btn-exit")
        back = await c.wait_for("location.pathname.endsWith('/courses')", 60)
        await c.wait_for(COURSE_READY, 15)
        await c.ev(CONFIRM_JS)
        cur = await c.ev("fetch('api/practice/current').then(r => r.json()).then(j => j.active)")
        prog = await c.ev(f"(document.querySelector('.c-card[data-id=\"{TOPIC}\"] .c-prog') || {{}}).textContent")
        other = await c.ev("(document.querySelector('.c-card[data-id=\"enspro-t1-t2-link\"] .c-prog') || {}).textContent")
        # 实验模式：直接回课程页，不弹确认
        await goto(c, f"{base}/?mode=lab&s={TOPIC}", WB_READY)
        await click(c, "#btn-exit")
        back2 = await c.wait_for("location.pathname.endsWith('/courses')", 15)
        await c.wait_for(COURSE_READY, 15)
        await c.ev(CONFIRM_JS)
        print(f"  退出：练习中确认 {n1}→（新页面计数清零）回课程页={back} 练习进行中={cur} 进度={prog!r} / 未做过的题={other!r}")
        results.append(chk("C10", "退出到课程：练习中确认后按放弃结束、回课程页（练习已结束）；实验中直接回课程页；卡片完成状态（已通过 ✓ / 未开始）",
                           back and cur is False and prog == "已通过 ✓" and other == "未开始" and back2))

        # ---- C11：窄屏 ----
        await c.cmd("Emulation.setDeviceMetricsOverride", width=760, height=900, deviceScaleFactor=1, mobile=False)
        await asyncio.sleep(0.6)
        nc = {"sw": await c.ev("document.documentElement.scrollWidth"),
              "home": await shown(c, "#c-home"), "envs": await box(c, ".c-envs"),
              "next": await box(c, "#home-next-card")}
        await shot(c, shot_dir, "09_courses_narrow.png", full=True)
        await goto(c, f"{base}/?mode=lab&s={TOPIC}", WB_READY)
        await click(c, "#vt-practice")
        await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 60)
        await asyncio.sleep(0.8)
        ids = ["vt-topo", "vt-practice", "vt-exam", "topic-btn", "pr-check", "pr-end", "user-btn", "btn-exit"]
        boxes = {i: await box(c, f"#{i}") for i in ids}
        sw = await c.ev("document.documentElement.scrollWidth")
        await shot(c, shot_dir, "10_op_narrow.png")
        await click(c, "#topic-btn")
        await asyncio.sleep(0.3)
        popb = await box(c, "#topic-pop")
        await shot(c, shot_dir, "11_op_narrow_popover.png")
        await c.ev("window.__ainet.setTopicPop(false), 0")
        await click(c, "#btn-exit")
        await c.wait_for("location.pathname.endsWith('/courses')", 60)
        await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        inside = all(b and b["w"] > 0 and b["x"] >= 0 and b["x"] + b["w"] <= 760 for b in boxes.values())
        print(f"  窄屏学习首页：{nc}\n  窄屏操作页：sw={sw} {boxes} 气泡={popb}")
        results.append(chk("C11", "窄屏（760px）：学习首页纵向排布且无横向滚动；操作页主要按钮都在视口内，右侧工单折叠为图标栏，课题气泡不出界",
                           nc["sw"] <= 760 and nc["home"] and nc["envs"]["w"] > 0
                           and nc["envs"]["x"] + nc["envs"]["w"] <= 760
                           and nc["next"]["w"] > 0 and nc["next"]["x"] + nc["next"]["w"] <= 760
                           and inside and sw <= 760
                           and popb["x"] >= 0 and popb["x"] + popb["w"] <= 760))

        # ---- C12：隐藏拓扑的考试里也能用 PC 设置气泡（拓扑打码，左栏设备按钮 PC1 → 气泡） ----
        from exam_e2e import start_from_courses
        tid = await c.ev("""fetch('api/exam/templates', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: 'e2e 隐藏拓扑', config: {questions: [{scenario: 'e2e-pc-t1t2', seed: 0}],
          duration_s: 900, retries: 0, show_topology: false}})}).then(r => r.json()).then(t => t.id)""")
        await start_from_courses(c, base, tid)
        await c.ev(CONFIRM_JS)
        await c.wait_for("window.__ainet && window.__ainet.state.exam && window.__ainet.state.exam.active && "
                         "document.querySelectorAll('#topo-devlist button').length >= 3", 60)
        await c.ev("window.__ainet.setTopicPop(false), 0")
        masked = await c.ev("document.getElementById('topo-body').classList.contains('masked')")
        await c.ev("[...document.querySelectorAll('#topo-devlist button')].find(b => b.textContent === 'PC1').click(), 0")
        await c.wait_for("document.getElementById('pc-ip').value && !document.getElementById('pc-ip').disabled", 15)
        await c.ev("document.getElementById('pc-ip').value = '10.0.10.88'; document.getElementById('pc-form').requestSubmit(), 0")
        hid_ok = await c.wait_for("document.getElementById('pc-msg').textContent.startsWith('已生效')", 30)
        await shot(c, shot_dir, "14_hidden_topology_pc_bubble.png")
        await click(c, "#ex-finish")
        await c.wait_for("!document.getElementById('ex-confirm').hidden", 5)
        await click(c, "#ex-confirm-yes")
        fin = await c.wait_for("!document.getElementById('ex-done-view').hidden", 60)
        print(f"  隐藏拓扑考试：打码={masked} PC 气泡生效={hid_ok} 交卷={fin}")
        results.append(chk("C12", "隐藏拓扑的考试：拓扑打码，左栏设备按钮 PC1 同样弹出 PC 设置气泡并生效；交卷",
                           masked and hid_ok and fin))


async def phase_classic(c, shot_dir, results):
    from fake_lab import fake_lab
    for k in ("AINET_SCENARIOS_DIR", "AINET_ENSPRO_SANDBOX"):      # 阶段一在本进程设的，不带进经典版服务
        os.environ.pop(k, None)
    async with fake_lab(os.path.join(shot_dir, "work_classic"), HTTP_PORT + 1) as (base, net):
        await c.cmd("Page.navigate", url=base + "/login")
        await c.wait_for("document.getElementById('login')", 10)
        await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.')).forEach(k => localStorage.removeItem(k)), 0")
        await c.ev("document.getElementById('login').value = 'admin'; document.getElementById('password').value = 'admin';"
                   "document.getElementById('loginForm').requestSubmit(), 0")
        await c.wait_for("location.pathname.endsWith('/courses')", 10)
        await c.wait_for(COURSE_READY, 20)
        await c.ev(CONFIRM_JS)
        envs = await c.ev("[...document.querySelectorAll('#c-envs .c-env')].map(b => [b.dataset.key, b.disabled, b.innerText.replace(/\\s+/g, ' ')])")
        print(f"  经典版环境：{envs}")
        m = {e[0]: e for e in envs}
        # 6.12 起离线后带原因分类：「（离线 · 连不上）」
        results.append(chk("K1", "eNSP 在线（当前）、eNSP Pro 离线置灰（假环境：探测本机关闭的端口，不连真虚机）",
                           not m["ensp"][1] and "在线" in m["ensp"][2] and m["enspro"][1] and "（离线" in m["enspro"][2]))
        # K2：OSPF 题的拓扑没在运行
        await click(c, '.c-chap[data-id="L05"]')
        await asyncio.sleep(0.3)
        await click(c, '.c-card[data-id="ospf-2-1-r1-shutdown"]')
        await asyncio.sleep(0.3)
        prep = await text(c, "#c-prep")
        await click(c, "#c-practice")
        hint = await c.wait_for("document.getElementById('c-prog-stage').textContent.includes('请在 eNSP 中打开')", 20)
        await asyncio.sleep(1.2)
        stage, pmsg = await text(c, "#c-prog-stage"), await text(c, "#c-prog-msg")
        await shot(c, shot_dir, "12_classic_open_topology.png")
        await click(c, "#c-cancel")
        cancelled = await c.wait_for("document.getElementById('c-prog-msg').textContent.includes('已取消')", 10)
        still = await c.ev("location.pathname")
        print(f"  经典版：准备说明 {prep!r}；提示 {stage!r} / {pmsg!r}；取消后 {still}")
        results.append(chk("K2", "经典版、所需拓扑没在运行：提示「请在 eNSP 中打开 2-1Single-Area OSPF 拓扑并启动设备」并倒计时重新检测，可取消",
                           "不能远程打开" in prep and hint and "2-1Single-Area OSPF" in stage and "尚未就绪" in pmsg
                           and "秒后重新检测" in pmsg and cancelled and still.endswith("/courses")))
        # K3：Campus 在运行 → 直接进入练习；经典版 PC 气泡
        await click(c, '.c-chap[data-id="L02"]')
        await asyncio.sleep(0.3)
        await click(c, '.c-card[data-id="campus-ar0-ar2-link"]')
        await asyncio.sleep(0.3)
        req = await c.ev("document.querySelectorAll('#c-topo-svg .c-node.req').length")
        await click(c, "#c-practice")
        await c.wait_for("location.search.includes('mode=practice')", 40)
        await c.wait_for(WB_READY, 20)
        await c.ev(CONFIRM_JS)
        act = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 20)
        pc_nodes = await c.ev("[...document.querySelectorAll('#topo-svg .node')].map(n => n.dataset.name).filter(n => /^\\d+$/.test(n))")
        print(f"  经典版拓扑 PC 节点：{pc_nodes}")
        await node_click(c, pc_nodes[0] if pc_nodes else "1")
        await asyncio.sleep(0.5)
        pc = {"shown": await shown(c, "#pc-pop"), "note": await text(c, "#pc-note"),
              "dis": await c.ev("['pc-ip', 'pc-mask', 'pc-gw', 'pc-dns', 'pc-apply'].every(id => document.getElementById(id).disabled)")}
        await shot(c, shot_dir, "13_classic_pc_bubble.png")
        await c.ev("document.getElementById('pc-close').click(), 0")
        await click(c, "#btn-exit")
        out = await c.wait_for("location.pathname.endsWith('/courses')", 60)
        print(f"  经典版练习：拓扑预览橙框 {req}；进入练习 {act}；PC 气泡 {pc}；退出 {out}")
        results.append(chk("K3", "经典版、拓扑在运行：拓扑预览（本题设备橙框）→ 直接进入练习；PC 气泡只提示「请在 eNSP 软件中双击 … 设置」、输入框禁用；退出到课程",
                           req == 2 and act and pc["shown"] and "请在 eNSP 软件中双击" in pc["note"] and pc["dis"] and out))


async def run(shot_dir: str) -> int:
    results: list[bool] = []
    browser_args = [CHROME]
    if os.name == "nt":
        browser_args += ["--headless=new", f"--user-data-dir={os.path.join(shot_dir, 'browser-profile')}"]
    browser_args += [f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox", "about:blank"]
    chrome = subprocess.Popen(browser_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
            try:
                await phase_pro(c, shot_dir, results)
            except Exception as exc:          # 某步卡住时也跑完阶段二，最后统一计数
                import traceback
                traceback.print_exc()
                results.append(chk("P!", f"阶段一异常：{exc}", False))
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
            try:
                await phase_classic(c, shot_dir, results)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                results.append(chk("K!", f"阶段二异常：{exc}", False))
            print("errors total:", c.errors or "无")
            results.append(chk("X", "全程无 JS 异常 / console error", not c.errors))
            reader.cancel()
    finally:
        stop_browser(chrome, os.path.join(shot_dir, "browser-profile"))
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "course_e2e_shots")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    rc = asyncio.run(run(out))
    print(f"  用时 {int(time.time() - t0)} 秒；截图在 {out}")
    sys.exit(rc)
