"""考试模式 e2e（不进默认 pytest）：Campus_Network 上一场 3 道题的考试，全程经无头浏览器操作页面。

前置（真机）：eNSP 打开 Campus_Network 并启动 AR0、AR2、LSW0（未配置设备）；服务已用
AINET_TOPO_DIRS=tests/fixtures/topos、ENSP_HOST=<设备主机>、临时 AINET_DB_PATH 起好，Linux 上用
AINET_ENSP_RUNNING_IDS=<AR0 GUID>,<AR2 GUID>,<LSW0 GUID> 覆盖运行中设备。建议 AINET_EXAM_TICK=1（缺省即 1）。
模拟设备：tests/e2e/exam_fake_e2e.py 会起假 VRP（127.0.0.1:2000/2001/2003）和服务，再调用本脚本的 run()。

步骤：
  T  在「考试模板管理」页用 JSON 新建模板：3 题、固定种子（参数化题按种子展开，脚本据此算出修复命令）、
     允许提示（每级扣 5 分）、每题可重试 1 次、单题时长 QTIME 秒（缺省 120，环境变量 AINET_E2E_QTIME）
  1  课程页章节列最下方「考试」列出模板 → 开始考试（准备环境 → 第 1 题注入故障 → 进操作页）；学员终端看不到注入 / 判定命令
  2  第 1 题（互联网段，种子 777）：要一级提示 → 在 AR2 上改对接口地址 → 提交本题 → 通过
  3  下一题 → 第 2 题（回程路由，种子 4242）：不修直接提交 → 未通过（还剩 1 次）→ 在 AR2 上改对下一跳 → 再提交 → 通过
  4  下一题 → 第 3 题（接入 VLAN，种子 9）：不作答，等单题超时 → 系统代为提交、复位 → 最后一题 → 自动交卷
  5  成绩单：3 题状态 / 提交次数 / 超时标记 / 得分；各题复位成功
  6  命令回放：第 1 题能逐条播放学员敲过的命令（含修复命令）
  7  档案页「考试记录」有这场考试和总分，能点进成绩单
  全程无 JS 异常。
用法：python tests/e2e/exam_e2e.py [base_url] [截图目录]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # backend/
from browser_e2e import CDP, CHROME, step, stop_browser  # noqa: E402

AR0, LSW0, AR2 = 2000, 2001, 2003
QUESTIONS = [("campus-ar0-ar2-link", 777), ("campus-ar2-return-route", 4242), ("campus-lsw0-access-vlan", 9)]
# 判定 / 注入只走隐藏通道：学员终端里不该出现这些字样（学员自己没敲过）
HIDDEN = ["ping -c 2", "undo ip address"]

JOIN_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s + '\\n'; })"""
EXAM_JS = "fetch('api/exam/current').then(r => r.json())"


def expanded(sid: str, seed: int) -> dict:
    import config
    import scenarios as sc
    config.TOPO_DIRS = [os.path.join(os.path.dirname(HERE), "fixtures", "topos")]
    loaded, errors = sc.load_dir(config.SCENARIOS_DIR, [])
    return sc.expand(loaded[sid], seed)


async def shot(c, shot_dir, name, full=False):
    data = await c.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=full)
    path = os.path.join(shot_dir, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data["data"]))
    print(f"  screenshot: {path}")
    return path


async def buf(c, port) -> str:
    return await c.ev(f"({JOIN_JS})({port})") or ""


async def type_cmd(c, port, text, timeout=25):
    """经页面终端敲一条命令（聚焦 xterm，insertText + 回车键事件），等回到提示符。"""
    before = await buf(c, port)
    await c.ev(f"window.__ainet.focusTab({port}), window.__ainet.state.tabs.get({port}).term.focus(), 0")
    await c.cmd("Input.insertText", text=text)
    for typ in ("keyDown", "keyUp"):
        await c.cmd("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter",
                    windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, **({"text": "\r"} if typ == "keyDown" else {}))
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(0.4)
        b = await buf(c, port)
        lines = b.rstrip("\n").split("\n")
        if text in b[max(0, len(before.rstrip()) - 100):] and re.match(r"^[<\[][^\s<>\[\]]+[>\]]\s*$", lines[-1]):
            return b
    return await buf(c, port)


async def start_from_courses(c, base, tid, shot_dir=None, name=None):
    """课程页：章节列最下方「考试」→ 模板卡片 → 「开始考试」（确认框自动同意）→ 准备环境后进操作页。返回模板是否列出。"""
    await c.cmd("Page.navigate", url=base + "/courses")
    await c.wait_for("window.__ainetCourses && window.__ainetCourses.st.data", 15)
    await asyncio.sleep(0.5)
    await c.ev("window.confirm = () => true")
    await c.ev("document.querySelector('.c-chap[data-id=\"__exam__\"]').click()")
    card = f"document.querySelector('.c-card[data-id=\"tpl:{tid}\"]')"
    listed = await c.wait_for(f"!!{card}", 10)
    await c.ev(f"{card}.click()")
    await asyncio.sleep(0.3)
    if shot_dir:
        await shot(c, shot_dir, name)
    await c.ev("document.getElementById('c-exam-tpl').click()")
    await c.wait_for("location.search.includes('mode=exam')", 120)
    await c.wait_for("window.__ainet && window.__ainet.state.topo && window.__ainetExam", 20)
    await c.ev("window.confirm = () => true")
    return listed


async def open_dev(c, port):
    await c.ev(f"document.querySelector('.ex-dev[data-port=\"{port}\"]').click()")
    ok = await c.wait_for(f"window.__ainet.state.tabs.get({port}) && window.__ainet.state.tabs.get({port}).ws "
                          f"&& window.__ainet.state.tabs.get({port}).ws.readyState === 1", 15)
    await asyncio.sleep(1.5)
    return ok


async def exam_state(c):
    return await c.ev(EXAM_JS)


async def wait_exam(c, pred, timeout):
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = await exam_state(c)
        try:
            if pred(st):
                return st, True
        except (KeyError, TypeError, IndexError):
            pass
        await asyncio.sleep(0.7)
    return st, False


async def click(c, el_id):
    await c.ev(f"document.getElementById('{el_id}').click()")


async def run(base: str, shot_dir: str, debug_port: int = 9341, qtime: int | None = None) -> int:
    qtime = qtime or int(os.environ.get("AINET_E2E_QTIME", "120"))
    os.makedirs(shot_dir, exist_ok=True)
    results: list[bool] = []
    browser_args = [CHROME]
    if os.name == "nt":
        browser_args += ["--headless=new", f"--user-data-dir={os.path.join(shot_dir, 'browser-profile')}"]
    browser_args += [f"--remote-debugging-port={debug_port}", "--no-sandbox", "about:blank"]
    chrome = subprocess.Popen(browser_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json"))
                break
            except OSError:
                await asyncio.sleep(0.2)
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=None) as ws:
            c = CDP(ws)
            reader = asyncio.create_task(c.reader())
            for d in ("Runtime", "Log", "Page"):
                await c.cmd(f"{d}.enable")
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1500, height=950, deviceScaleFactor=1, mobile=False)
            await c.cmd("Page.navigate", url=base + "/login")
            await asyncio.sleep(1)
            await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                       "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
            exps = [expanded(sid, seed) for sid, seed in QUESTIONS]
            pv = [e["param_values"] for e in exps]
            print(f"  题目参数（按模板固定种子展开）：{pv}")

            # ---- T：模板管理页新建模板 ----
            cfg = {"questions": [{"scenario": sid, "seed": seed} for sid, seed in QUESTIONS],
                   "duration_s": 3600, "question_duration_s": qtime, "retries": 1,
                   "hints": {"allowed": True, "penalty": 5, "max_level": 2},
                   "show_topology": True, "auto_judge": False, "record_replay": True, "shuffle": False}
            name = f"e2e 考试 {time.strftime('%H%M%S')}"
            await c.cmd("Page.navigate", url=base + "/exam/templates")
            await c.wait_for("document.getElementById('btn-new') && window.__admin", 10)
            await asyncio.sleep(0.8)
            await c.ev("document.getElementById('btn-new').click()")
            await c.ev(f"document.getElementById('f-name').value = {json.dumps(name)}")
            await c.ev("document.getElementById('json-box').open = true")
            await c.ev(f"(t => {{ t.value = {json.dumps(json.dumps(cfg, ensure_ascii=False, indent=2))}; "
                       "t.dispatchEvent(new Event('input')); })(document.getElementById('f-json'))")
            await c.ev("document.getElementById('btn-json-apply').click()")      # JSON 填回表单（核对表单渲染）
            await asyncio.sleep(0.3)
            await shot(c, shot_dir, "00a_template_editor.png", full=True)
            await c.ev("document.getElementById('btn-save').click()")
            ok = await c.wait_for(f"[...document.querySelectorAll('#list td')].some(td => td.textContent === {json.dumps(name)})", 10)
            tid = await c.ev(f"fetch('../api/exam/templates').then(r => r.json()).then(d => d.templates.find(t => t.name === {json.dumps(name)}).id)")
            await shot(c, shot_dir, "00_template_admin.png")
            results.append(step("T", f"模板管理页新建模板「{name}」（id={tid}）", bool(ok and tid)))

            # ---- 1：课程页「考试」列出模板 → 开始（准备环境后进操作页） ----
            listed = await start_from_courses(c, base, tid, shot_dir, "01_exam_list.png")
            st, started = await wait_exam(c, lambda s: s["active"] and s["question"]["status"] == "active", 120)
            await c.wait_for("!document.getElementById('ex-active-view').hidden", 10)
            await shot(c, shot_dir, "02_q1_started.png")
            print(f"  开考：第 {st['current'] + 1}/{st['total']} 题，剩余 {st['remaining_s']}s，本题剩余 {st['question']['remaining_s']}s")
            results.append(step(1, "课程页「考试」列出模板 → 开始考试（准备环境、进操作页），第 1 题注入完成、计时开始",
                                bool(listed and started and st["total"] == 3 and st["current"] == 0)))

            # ---- 2：第 1 题：提示 → 修复 → 提交通过 ----
            link = pv[0]["link"]
            await click(c, "ex-hint")
            hint_ok = await c.wait_for("document.querySelectorAll('#ex-hints .hint').length >= 1", 25)
            await open_dev(c, AR0)
            await type_cmd(c, AR0, f"ping 10.0.{link}.2")
            await open_dev(c, AR2)
            await type_cmd(c, AR2, "display ip interface brief")
            for cmd in ("system-view", "interface GigabitEthernet0/0/0", f"ip address 10.0.{link}.2 24", "return"):
                await type_cmd(c, AR2, cmd)
            await click(c, "ex-submit")
            st, passed = await wait_exam(c, lambda s: s["question"]["passed"], 40)
            await c.wait_for("document.getElementById('ex-banner').classList.contains('ok')", 10)
            terms = (await buf(c, AR0)) + (await buf(c, AR2))
            leaks = [w for w in HIDDEN if w in terms]
            await shot(c, shot_dir, "03_q1_passed.png")
            subs = st["question"]["submissions"]
            hint_text = await c.ev("document.getElementById('ex-hints').innerText")
            print(f"  第 1 题提交：{subs}；提示：{hint_text!r}")
            results.append(step(2, f"第 1 题：一级提示 → 改 AR2 接口为 10.0.{link}.2/24 → 提交本题一次通过；终端无隐藏命令 {leaks}",
                                bool(hint_ok and passed and len(subs) == 1 and not leaks)))

            # ---- 3：第 2 题：提交失败 → 修复 → 重试通过 ----
            await click(c, "ex-next")
            st, ok2 = await wait_exam(c, lambda s: s["current"] == 1 and s["question"]["status"] == "active", 150)
            hop = pv[1]["bad_hop"]
            await click(c, "ex-submit")
            st, failed = await wait_exam(c, lambda s: len(s["question"]["submissions"]) == 1, 40)
            first = st["question"]["submissions"][0] if failed else {}
            await c.wait_for("document.getElementById('ex-msg').textContent.includes('未通过')", 10)
            await shot(c, shot_dir, "04_q2_failed_submit.png")
            await open_dev(c, AR2)
            for cmd in ("system-view", f"undo ip route-static 10.0.10.0 24 10.0.12.{hop}",
                        "ip route-static 10.0.10.0 24 10.0.12.1", "return"):
                await type_cmd(c, AR2, cmd)
            await open_dev(c, LSW0)
            await type_cmd(c, LSW0, "ping 192.168.2.1")
            await click(c, "ex-submit")
            st, passed2 = await wait_exam(c, lambda s: s["question"]["passed"], 40)
            await c.wait_for("document.getElementById('ex-banner').classList.contains('ok')", 10)
            await shot(c, shot_dir, "05_q2_passed_after_retry.png")
            subs2 = st["question"]["submissions"]
            print(f"  第 2 题提交：{subs2}")
            results.append(step(3, f"第 2 题：首次提交未通过（剩 1 次）→ 改回程下一跳（错误 10.0.12.{hop}）→ 重试通过",
                                bool(ok2 and failed and not first.get("passed") and passed2 and len(subs2) == 2)))

            # ---- 4：第 3 题：不作答，单题超时 → 自动交卷 ----
            await click(c, "ex-next")
            st, ok3 = await wait_exam(c, lambda s: s["current"] == 2 and s["question"]["status"] == "active", 150)
            print(f"  第 3 题开始，本题剩余 {st['question']['remaining_s'] if ok3 else '?'}s，等超时…")
            await asyncio.sleep(3)
            await shot(c, shot_dir, "06a_q3_waiting.png")
            done = await c.wait_for("!document.getElementById('ex-done-view').hidden", qtime + 120)
            st = await exam_state(c)
            eid = await c.ev("window.__ainetExam.ex.doneId")
            await shot(c, shot_dir, "06_q3_timeout_finished.png")
            results.append(step(4, f"第 3 题不作答 → 单题超时后系统代为提交、复位 → 自动交卷（考试 {eid}）",
                                bool(ok3 and done and not st.get("active") and eid)))

            # ---- 5：成绩单 ----
            await c.cmd("Page.navigate", url=f"{base}/exam/report?id={eid}")
            await c.wait_for("document.querySelectorAll('.q').length === 3", 15)
            await c.wait_for("!document.getElementById('summary').textContent.includes('评分中')", 240)
            await c.wait_for("document.querySelector('#questions .report-rescore')", 15)
            await asyncio.sleep(1)
            rep = await c.ev(f"fetch('../api/exam/{eid}/report').then(r => r.json())")
            qs = rep["questions"]
            summary = [(q["status"], q.get("passed"), len(q["submissions"]), q["timed_out"], q["auto_submitted"],
                        q.get("score_total"), q.get("reset_ok")) for q in qs]
            print(f"  成绩单：总分 {rep['score_total']}（{rep['score_status']}）；各题（状态, 通过, 提交次数, 超时, 代提交, 得分, 复位）={summary}")
            await shot(c, shot_dir, "07_report.png", full=True)
            replay_style = await c.ev("(() => { const b=document.getElementById('rp-play'),s=getComputedStyle(b); "
                                      "return {background:s.backgroundColor,color:s.color,disabled:b.disabled}; })()")
            rescore_before = await c.ev("!!document.querySelector('#questions .report-rescore')")
            await c.ev("document.querySelector('#questions .report-rescore').click(), 0")
            rescore_after = await c.wait_for("document.querySelector('#questions .report-rescore') && "
                                              "!document.querySelector('#questions .report-rescore').disabled", 25)
            leak_keys = [k for k in ("checks", "solution", "inject", "scoring_points") if f'"{k}"' in json.dumps(rep)]
            results.append(step(5, "成绩单：第 1、2 题通过（1 次 / 2 次提交），第 3 题超时代提交未通过；各题复位成功；无 checks / solution 原文",
                                [s[:5] for s in summary] == [("done", True, 1, False, False), ("done", True, 2, False, False),
                                                              ("done", False, 1, True, True)]
                                and all(q.get("reset_ok") for q in qs) and not leak_keys
                                and rep["score_status"] != "scoring"))

            results.append(step(5.1, "AI 点评不可用时成绩单提供重新评分；命令回放控件使用主题色且文字可读",
                                rescore_before and rescore_after and replay_style["background"] != "rgb(15, 20, 25)"
                                and replay_style["color"] != "rgb(15, 20, 25)"))

            # ---- 6：命令回放 ----
            await c.ev("document.getElementById('replay-q').value = '0'; document.getElementById('replay-q').dispatchEvent(new Event('change'))")
            await click(c, "rp-play")
            await asyncio.sleep(4)
            pos = await c.ev("window.__report.rp.pos")
            text = await c.ev("document.getElementById('replay-list').innerText")
            await c.ev("document.getElementById('replay-h').scrollIntoView()")
            await shot(c, shot_dir, "08_replay.png")
            rp = await c.ev(f"fetch('../api/exam/{eid}/replay').then(r => r.json())")
            cmds = [l["command"] for l in rp["questions"][0]["logs"]]
            print(f"  回放第 1 题命令：{cmds}；播放到第 {pos + 1} 条")
            results.append(step(6, "命令回放：第 1 题按时间顺序列出学员命令（含修复命令），逐条播放推进",
                                f"ip address 10.0.{link}.2 24" in cmds and cmds[0].startswith("ping") and pos >= 1
                                and f"ip address 10.0.{link}.2 24" in text))

            # ---- 7：档案页 ----
            await c.cmd("Page.navigate", url=base + "/profile")
            await c.wait_for("document.querySelector('#exams tr.exam-row')", 10)
            row = await c.ev(f"(document.querySelector('#exams tr.exam-row[data-id=\"{eid}\"]') || {{}}).innerText")
            await shot(c, shot_dir, "09_profile.png", full=True)
            await c.ev(f"document.querySelector('#exams tr.exam-row[data-id=\"{eid}\"] a').click()")
            nav = await c.wait_for(f"location.pathname.endsWith('/exam/report') && location.search.includes('id={eid}')", 10)
            print(f"  档案考试记录：{row!r}")
            # 页面按 Math.round(x * 10) / 10 显示（88.0 显示成 88），这里按同样的规则比
            shown = f"{round(rep['score_total'] * 10) / 10:g}" if rep["score_total"] is not None else "-"
            results.append(step(7, "档案页「考试记录」显示总分与日期，点进成绩单",
                                bool(row and nav and re.search(rf"(?<![\d.]){re.escape(shown)}(?![\d.])", row))))

            # ---- 9：不可看拓扑的考试 + 交卷二次确认 ----
            cfg2 = {"questions": [{"scenario": QUESTIONS[0][0], "seed": None}], "duration_s": 1800,
                    "question_duration_s": None, "retries": 0, "hints": {"allowed": False},
                    "show_topology": False}
            await c.cmd("Page.navigate", url=base + "/")
            await c.wait_for("window.__ainet && window.__ainet.state.topo && window.__ainetExam", 15)
            tid2 = await c.ev("fetch('api/exam/templates',{method:'POST',headers:{'Content-Type':'application/json'},"
                              f"body:JSON.stringify({{name:'e2e 隐藏拓扑 {time.strftime('%H%M%S')}',config:{json.dumps(cfg2)}}})}})"
                              ".then(r => r.json()).then(d => d.id)")
            await asyncio.sleep(1)
            await start_from_courses(c, base, tid2)
            st, ok9 = await wait_exam(c, lambda s: s["active"] and s["question"]["status"] == "active", 120)
            hint_hidden = await c.wait_for("document.getElementById('ex-hint').hidden", 5)
            await c.ev("document.getElementById('vt-topo').click()")
            masked = await c.wait_for("document.getElementById('topo-body').classList.contains('masked') && "
                                      "document.getElementById('topo-empty').textContent.includes('不提供拓扑图') && "
                                      "document.querySelectorAll('#topo-svg .node').length === 0", 10)
            await shot(c, shot_dir, "10_topology_hidden.png")
            await c.ev("document.getElementById('vt-exam').click()")
            term_ok = await open_dev(c, AR0)
            await click(c, "ex-finish")
            confirm_shown = await c.wait_for("!document.getElementById('ex-confirm').hidden", 5)
            await shot(c, shot_dir, "11_finish_confirm.png")
            still = (await exam_state(c))["active"]                  # 只点一次「交卷」不会交卷
            await click(c, "ex-confirm-yes")
            fin = await c.wait_for("!document.getElementById('ex-done-view').hidden", 60)
            await c.ev("document.getElementById('vt-topo').click()")
            unmasked = await c.wait_for("!document.getElementById('topo-body').classList.contains('masked') && "
                                        "document.querySelectorAll('#topo-svg .node').length > 0", 15)
            results.append(step(9, "模板设定不看拓扑：拓扑图打码、提示按钮隐藏、终端仍可打开本题设备；交卷需二次确认；交卷后拓扑恢复",
                                bool(ok9 and hint_hidden and masked and term_ok and confirm_shown and still and fin
                                     and unmasked)))

            await asyncio.sleep(0.5)
            errs = [e for e in c.errors]
            print(f"  JS 异常 / console.error：{errs or '无'}")
            results.append(step(8, "全程无 JS 异常", not errs))
            reader.cancel()
    finally:
        stop_browser(chrome, os.path.join(shot_dir, "browser-profile"))
    print(f"\n{sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


if __name__ == "__main__":
    BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
    SHOT_DIR = sys.argv[2] if len(sys.argv) > 2 else "."
    sys.exit(asyncio.run(run(BASE, SHOT_DIR, int(os.environ.get("AINET_E2E_DEBUG_PORT", "9341")))))
