"""课程页全流程的真机 e2e（eNSP Pro + 无头浏览器，里程碑 6.11；不进默认 pytest）。

前置：本机能访问 eNSP Pro（沙箱 ainet_test id 2：AR1=6、AR2=7；ainet_campus id 3：LSW0=8、AR0=9、PC1=12）；
服务以 AINET_LAB_PROVIDER=enspro、临时 AINET_DB_PATH 起好（本脚本读同一个库拿考试种子与操作记录，所以要把
AINET_DB_PATH 也传给本脚本），建议 AINET_AI_SCORING=0 AINET_AI_HINTS=0 AINET_AI_AGENT=0。eNSP Pro 单会话：跑脚本期间
不要在别处登录 eNSP Pro，也不要同时跑别的直连 eNSP Pro 的脚本。只启停本题设备、开 / 结束练习与考试（结束时复位），
不 save、不删设备；结束时停掉三个沙箱里运行着的设备。

  R0  预置：切到沙箱 3、启动 PC1（当作「旧沙箱里运行着的设备」）
  R1  登录后进课程页；eNSP 离线（置灰）、eNSP Pro 在线
  R2  L02 →「AR1 与 AR2 互联不通」（题名不带平台后缀）→ 开始练习：准备进度（切换沙箱、启动设备、等待运行、检测终端）
      → 旧沙箱的 PC1 被自动停掉（stopped_old）→ 进操作页、练习进行中
  R3  操作页：单层顶栏（56px）、题名气泡里有环境（eNSP Pro / YunShan）与拓扑（沙箱 ainet_test、运行台数）、AR2 终端可用
  R4  在 AR2 上修复 → 自动判定通关 → 退出到课程 → 复位与开练前快照一致（reset_ok）→ 卡片「已通过」
  R5  同一题单题考试：开始（确认框）→ 修复 → 提交本题通过 → 交卷（二次确认）→ 成绩单（带平台标签、各题复位成功）
  R6  L04 →「交换机管理地址 ping 不通（接入 VLAN 配错）」→ 开始练习：切到沙箱 3（停掉 AR1、AR2）、启动 AR0 / LSW0 / PC1，
      LSW0 卡住时看门狗自动停止再启动（记录 restart 阶段与设备进度文字）→ 就绪探测 → 练习进行中
  R7  PC 设置气泡（练习中）：读出 PC1 当前地址 → 改 IP / 掩码 / 网关 / DNS →「已生效」→ 接口读回（refreshAttrs）一致 →
      PC1 终端里查看地址（ipconfig / ifconfig）
  R8  在 LSW0 上修复 → 自动判定通关 → 退出到课程 → reset_ok、PC1 地址（含 DNS）还原、操作记录里有「[PC 配置] PC1 …」
  R9  档案页：练习 / 考试记录带平台标签
  R10 收尾：停掉全部设备（经切换沙箱 + 停止），三个沙箱运行数为 0
  X   全程无 JS 异常

用法：AINET_DB_PATH=<服务用的库> python tests/e2e/course_real_e2e.py [base_url] [截图目录] [vlan]
（第三个参数 vlan：只跑 R0、R1、R6~R10，用来多碰几次 LSW0 卡住 → 看门狗自动重启）
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
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from browser_e2e import CDP, CHROME  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799").rstrip("/")
SHOTS = sys.argv[2] if len(sys.argv) > 2 else "."
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9361"))
VLAN_ONLY = len(sys.argv) > 3 and sys.argv[3] == "vlan"     # 只跑 R0、R1、R6~R10（再碰一次 LSW0 卡住 + 看门狗）
AR1, AR2, LSW0, AR0, PC1 = 6, 7, 8, 9, 12
TOPIC1, TOPIC2 = "enspro-t1-t2-link", "campus-lsw0-access-vlan-ys"
PROMPT = r"(<[^<>\s]+>|\[[^\[\]\s]+\]|PC>)\s*$"
BUF_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return '';
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s; })"""
CONFIRM_JS = "window.__confirms = window.__confirms || []; window.confirm = (t) => (window.__confirms.push(t), true); 0"
WB_READY = ("window.__ainet && window.__ainet.state.topo && document.querySelectorAll('#topo-svg .node').length > 0 && "
            "document.getElementById('who').textContent !== '…'")
COURSE_READY = ("window.__ainetCourses && window.__ainetCourses.st.data && "
                "document.querySelectorAll('#c-topics .c-card').length > 0")
RESULTS: list[bool] = []
T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} +{time.time() - T0:5.0f}s] {msg}", flush=True)


def chk(n: str, text: str, ok, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    log(f"[{'PASS' if ok else 'FAIL'}] {n} {text}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


async def shot(c, name: str, full: bool = False) -> None:
    r = await c.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=full)
    path = os.path.join(SHOTS, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    log(f"  截图 {path}")


async def api(c, method: str, url: str, body=None):
    """在页面里调接口（带登录 cookie）：返回 {status, data}。"""
    js = (f"fetch({json.dumps(url)}, {{method: {json.dumps(method)}, headers: {{'Content-Type': 'application/json'}}"
          + (f", body: JSON.stringify({json.dumps(body)})" if body is not None else "")
          + "}).then(async r => ({status: r.status, data: await r.json().catch(() => null)}))")
    return await c.ev(js)


async def text(c, sel: str) -> str:
    return await c.ev(f"(document.querySelector({json.dumps(sel)}) || {{}}).textContent || ''") or ""


async def click(c, sel: str) -> None:
    await c.ev(f"document.querySelector({json.dumps(sel)}).click(), 0")


async def buf(c, port: int) -> str:
    return await c.ev(f"({BUF_JS})({port})") or ""


async def type_cmd(c, port: int, cmd: str, timeout: float = 60) -> str:
    """像学员一样在页面终端里敲一条命令，读到提示符为止；分页按空格。"""
    n0 = len((await buf(c, port)).rstrip("\n").split("\n")) - 1
    await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode({json.dumps(cmd + chr(13))}))")
    end, sent_at, out = time.time() + timeout, -1, ""
    while time.time() < end:
        await asyncio.sleep(0.4)
        out = "\n".join((await buf(c, port)).rstrip("\n").split("\n")[n0:])
        tail = out.rstrip().split("\n")[-1] if out.strip() else ""
        if "---- More ----" in tail and len(out) != sent_at:
            sent_at = len(out)
            await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode(' '))")
            continue
        if cmd in out and len(out.rstrip().split("\n")) > 1 and re.search(PROMPT, tail):
            return out
    log(f"  ！命令没等到提示符：[{port}] {cmd!r} 末尾 {out[-200:]!r}")
    return out


async def node_click(c, name: str) -> None:
    await c.ev("window.__ainet.fitView(), 0")
    await asyncio.sleep(0.2)
    await c.click_node(name)
    await asyncio.sleep(0.5)


async def open_term(c, name: str, port: int, timeout: float = 90) -> bool:
    """点拓扑节点打开终端，等到提示符（刚就绪的设备 console 首次登录要几秒）。"""
    await node_click(c, name)
    ok = await c.wait_for(f"(t => t && t.ws && t.ws.readyState === 1)(window.__ainet.state.tabs.get({port}))", 20)
    end = time.time() + timeout
    while time.time() < end:
        if re.search(PROMPT, (await buf(c, port)).rstrip()):
            return bool(ok)
        await asyncio.sleep(0.5)
        if time.time() > end - timeout + 15 and not (await buf(c, port)).strip():
            await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode('\\r'))")
    return False


async def goto_courses(c) -> bool:
    await c.cmd("Page.navigate", url=BASE + "/courses")
    ok = await c.wait_for(COURSE_READY, 30)
    await c.ev(CONFIRM_JS)
    return bool(ok)


async def pick(c, chapter: str, topic: str) -> None:
    await click(c, '.c-env[data-key="enspro"]')
    await asyncio.sleep(0.8)
    await click(c, f'.c-chap[data-id="{chapter}"]')
    await asyncio.sleep(0.4)
    await click(c, f'.c-card[data-id="{topic}"]')
    await asyncio.sleep(0.5)


async def follow_prepare(c, tag: str, want_mode: str, timeout: float = 1200) -> dict:
    """点了开始之后：跟着课程页的准备进度（阶段、设备状态、已用时间），直到进入操作页。"""
    seen = {"stages": [], "devs": set(), "elapsed": "", "msgs": set()}
    shot_done = False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if f"mode={want_mode}" in (await c.ev("location.search") or ""):
            break
        try:
            st = await text(c, "#c-prog-stage")
            devs = await c.ev("[...document.querySelectorAll('#c-prog-devs .c-dev')].map(x => x.textContent)") or []
            el = await text(c, "#c-prog-elapsed")
            msg = await text(c, "#c-prog-msg")
        except RuntimeError:
            await asyncio.sleep(0.5)
            continue
        stage = (st or "").split(" · ")[0]
        if stage and (not seen["stages"] or seen["stages"][-1] != stage):
            seen["stages"].append(stage)
            log(f"  [{tag}] 阶段 {st} | {devs} | {el}")
        seen["devs"] |= set(devs)
        if msg:
            seen["msgs"].add(msg[:160])
        seen["elapsed"] = el or seen["elapsed"]
        if not shot_done and any(("启动中" in d or "初始化" in d) for d in devs):
            await shot(c, f"{tag}_prepare_progress.png")
            shot_done = True
        job = ((await api(c, "GET", "api/lab/prepare"))["data"] or {}).get("job") or {}
        if job.get("done") and job.get("ok") is False and time.time() - t0 > 5:
            seen["failed"] = job.get("error")
            break
        await asyncio.sleep(1.0)
    seen["seconds"] = round(time.time() - t0)
    return seen


async def wait_practice_passed(c, poke=None, timeout: float = 150) -> bool:
    end = time.time() + timeout
    i = 0
    while time.time() < end:
        st = (await api(c, "GET", "api/practice/current"))["data"] or {}
        if st.get("passed"):
            return True
        i += 1
        if poke and i % 6 == 0:
            await poke()
        await asyncio.sleep(3)
    return False


def db_rows(sql: str, args=()) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(os.environ["AINET_DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


async def stop_all(c) -> dict:
    """停掉三个沙箱里运行着的设备：在当前沙箱停止，再切到其他沙箱（切换时停掉旧沙箱）……最后读各沙箱运行数。"""
    out = {}
    for sb in (2, 1, 3):
        r = await api(c, "POST", "api/lab/environment", {"key": "enspro", "sandbox": sb})
        out[f"switch{sb}"] = (r["status"], ((r["data"] or {}).get("stopped_old") or {}).get("devices"))
        cur = (await api(c, "GET", "api/topology/current"))["data"] or {}
        run = [d["console"] for d in ((cur.get("topology") or {}).get("devices") or [])
               if d.get("status") not in ("stopped", "na", None)]
        if run:
            r = await api(c, "POST", "api/lab/devices/stop", {"devices": run})
            out[f"stop{sb}"] = (r["status"], run)
    await asyncio.sleep(15)
    sbs = (await api(c, "GET", "api/lab/sandboxes"))["data"] or {}
    out["running"] = {s["id"]: s["running"] for s in sbs.get("sandboxes", [])}
    return out


async def main(c) -> None:
    from scenarios import expand, load_dir  # noqa: E402
    import config  # noqa: E402
    loaded, _ = load_dir(config.SCENARIOS_DIR)

    # ---- R1：登录 → 课程页 ----
    await c.cmd("Page.navigate", url=BASE + "/login")
    await c.wait_for("document.getElementById('login')", 15)
    await c.ev("Object.keys(localStorage).filter(k => k.startsWith('ainet.')).forEach(k => localStorage.removeItem(k)), 0")
    await c.ev("document.getElementById('login').value = 'admin'; document.getElementById('password').value = 'admin';"
               "document.getElementById('loginForm').requestSubmit(), 0")
    landed = await c.wait_for("location.pathname.endsWith('/courses')", 15)
    await c.wait_for(COURSE_READY, 30)
    await c.ev(CONFIRM_JS)

    # ---- R0：预置旧沙箱里运行着的设备（沙箱 3 的 PC1）----
    r = await api(c, "POST", "api/lab/environment", {"key": "enspro", "sandbox": 3})
    r2 = await api(c, "POST", "api/lab/devices/start", {"devices": [PC1]})
    pc_on = False
    for _ in range(60):
        cur = (await api(c, "GET", "api/topology/current"))["data"] or {}
        pc = next((d for d in cur["topology"]["devices"] if d["console"] == PC1), {})
        if pc.get("status") == "online":
            pc_on = True
            break
        await asyncio.sleep(3)
    chk("R0", "预置：切到沙箱 3、启动 PC1（旧沙箱里运行着的设备）", r["status"] == 200 and r2["status"] == 200 and pc_on,
        f"switch={r['status']} start={r2['status']} {r2['data']}")

    await goto_courses(c)
    envs = await c.ev("""[...document.querySelectorAll('#c-envs .c-env')].map(b => ({key: b.dataset.key, dis: b.disabled,
      text: b.innerText.replace(/\\s+/g, ' ')}))""")
    pro = next((e for e in envs if e["key"] == "enspro"), {})
    ens = next((e for e in envs if e["key"] == "ensp"), {})
    await shot(c, "r01_courses.png")
    chk("R1", "登录后进课程页；eNSP 离线置灰、eNSP Pro 在线可点", landed and pro and not pro["dis"] and "在线" in pro["text"]
        and ens and ens["dis"] and "离线" in ens["text"], json.dumps(envs, ensure_ascii=False))

    if not VLAN_ONLY:
        # ---- R2：AR1/AR2 题 开始练习 ----
        await pick(c, "L02", TOPIC1)
        title = await text(c, "#c-preview h1")
        card_t = await c.ev(f"(document.querySelector('.c-card[data-id=\"{TOPIC1}\"] strong') || {{}}).textContent")
        await shot(c, "r02_preview_t1t2.png")
        await click(c, "#c-practice")
        seen = await follow_prepare(c, "r02", "practice")
        await c.wait_for(WB_READY, 40)
        await c.ev(CONFIRM_JS)
        active = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 120)
        info = (await api(c, "GET", "api/lab"))["data"] or {}
        so = info.get("last_stopped_old") or {}
        job = ((await api(c, "GET", "api/lab/prepare"))["data"] or {}).get("job") or {}
        log(f"  准备：{json.dumps({k: (sorted(v) if isinstance(v, set) else v) for k, v in seen.items()}, ensure_ascii=False)[:900]}")
        log(f"  job.stopped_old={job.get('stopped_old')} last_stopped_old={so}")
        stopped_pc = (any("PC1" in (x.get("devices") or []) for x in (job.get("stopped_old") or []))
                      or "PC1" in (so.get("devices") or []))
        chk("R2", "AR1/AR2 题：题名不带「（YunShan）」；开始练习有准备进度（阶段 / 设备 / 用时）、走到检测终端；旧沙箱的 PC1 被自动停掉；进入练习",
            title == "AR1 与 AR2 互联不通" and card_t == "AR1 与 AR2 互联不通" and "检测设备终端" in "".join(seen["stages"])
            and seen["elapsed"] and active and stopped_pc, f"title={title!r} stages={seen['stages']} stopped_pc={stopped_pc}")

        # ---- R3：操作页 ----
        await asyncio.sleep(1.5)
        hdr = await c.ev("(e => e && Math.round(e.getBoundingClientRect().height))(document.querySelector('header#bar'))")
        topic = await text(c, "#topic-btn")
        await click(c, "#topic-btn")
        await asyncio.sleep(0.6)
        pop = {"env": await text(c, "#tp-env"), "topo": await text(c, "#topo-status"), "devs": await text(c, "#tp-devs")}
        await shot(c, "r03_topbar_popover.png")
        await c.ev("window.__ainet.setTopicPop && window.__ainet.setTopicPop(false), 0")
        t2_ok = await open_term(c, "AR2", AR2)
        out = await type_cmd(c, AR2, "display ip interface brief") if t2_ok else ""
        await shot(c, "r03_t2_terminal.png")
        chk("R3", "操作页单层顶栏（56px）、「练习 + 题名」；气泡：环境 eNSP Pro / YunShan、拓扑沙箱 ainet_test 与运行台数；AR2 终端可用",
            hdr == 56 and "练习" in topic and "AR1 与 AR2 互联不通" in topic and "eNSP Pro" in pop["env"] and "YunShan" in pop["env"]
            and "ainet_test" in pop["topo"] and "运行" in pop["topo"] and t2_ok and "GE0/0/1" in out,
            f"hdr={hdr} topic={topic!r} pop={pop}")

        # ---- R4：修复 → 通关 → 退出到课程 → 复位一致 ----
        st = (await api(c, "GET", "api/practice/current"))["data"] or {}
        link = (st.get("param_values") or {}).get("link")
        aid1 = st.get("attempt_id")
        for cmd in ("system-view", "interface GE0/0/1", f"ip address 10.0.{link}.2 24", "return"):
            await type_cmd(c, AR2, cmd)
        t1_ok = await open_term(c, "AR1", AR1)

        async def poke():
            await type_cmd(c, AR1, f"ping -c 2 10.0.{link}.2")
        await poke()
        passed = await wait_practice_passed(c, poke)
        await shot(c, "r04_passed.png")
        n_cf = len(await c.ev("window.__confirms || []") or [])
        await click(c, "#btn-exit")
        await asyncio.sleep(0.3)
        cf4 = (await c.ev("window.__confirms || []") or [])[n_cf:]
        back = await c.wait_for("location.pathname.endsWith('/courses')", 300)
        await c.wait_for(COURSE_READY, 30)
        await c.ev(CONFIRM_JS)
        last = ((await api(c, "GET", "api/practice/current"))["data"] or {}).get("last_result") or {}
        await pick(c, "L02", TOPIC1)
        prog = await c.ev(f"(document.querySelector('.c-card[data-id=\"{TOPIC1}\"] .c-prog') || {{}}).textContent")
        await shot(c, "r04_back_to_courses.png")
        chk("R4", "AR2 上修复 → 自动判定通关 → 退出到课程 → 复位与开练前快照一致（reset_ok）→ 卡片「已通过」",
            t1_ok and passed and back and last.get("reset_ok") is True and "已通过" in (prog or "")
            and cf4 and "已通关" in cf4[0],
            f"link={link} attempt={aid1} reset_ok={last.get('reset_ok')} leftovers={last.get('leftovers')} prog={prog!r} "
            f"确认框={cf4}")

        # ---- R5：单题考试 ----
        await click(c, "#c-exam")
        seen5 = await follow_prepare(c, "r05", "exam", timeout=600)
        await c.wait_for("window.__ainet && window.__ainet.state.topo && window.__ainetExam", 40)
        await c.ev(CONFIRM_JS)
        ex_on = await c.wait_for("window.__ainet.state.exam && window.__ainet.state.exam.active && "
                                 "window.__ainet.state.exam.question && window.__ainet.state.exam.question.status === 'active'", 300)
        ex = await c.ev("window.__ainet.state.exam")
        exam_id = ex and ex.get("exam_id")
        row = db_rows("SELECT state_json FROM exams WHERE id = ?", (exam_id,))
        seed = json.loads(row[0]["state_json"])["questions"][0]["seed"] if row else 0
        link5 = expand(loaded[TOPIC1], seed)["param_values"]["link"]
        await shot(c, "r05_exam_started.png")
        ok_t2 = await open_term(c, "AR2", AR2)
        for cmd in ("system-view", "interface GE0/0/1", f"ip address 10.0.{link5}.2 24", "return"):
            await type_cmd(c, AR2, cmd)
        await click(c, "#ex-submit")
        q_passed = await c.wait_for("window.__ainet.state.exam.question && window.__ainet.state.exam.question.passed", 90)
        await click(c, "#ex-finish")
        cfm = await c.wait_for("!document.getElementById('ex-confirm').hidden", 10)
        await click(c, "#ex-confirm-yes")
        done = await c.wait_for("!document.getElementById('ex-done-view').hidden && !!document.getElementById('ex-report-link')", 300)
        href = await c.ev("document.getElementById('ex-report-link') && document.getElementById('ex-report-link').href")
        rep = (await api(c, "GET", f"api/exam/{exam_id}/report"))["data"] or {}
        if href:
            await c.cmd("Page.navigate", url=href)
            await c.wait_for("document.querySelectorAll('.qh').length > 0", 60)
            await asyncio.sleep(1.5)
        tags = await c.ev("[...document.querySelectorAll('.qh .plat-tag')].map(x => x.textContent)")
        await shot(c, "r05_exam_report.png", full=True)
        qs = rep.get("questions") or []
        chk("R5", "单题考试：开始 → 修复 → 提交本题通过 → 交卷二次确认 → 成绩单（名称带平台、题目带 YunShan 标签、复位成功）",
            ex_on and ok_t2 and q_passed and cfm and done and rep.get("name", "").endswith(" · YunShan")
            and qs and qs[0].get("platform") == "yunshan" and tags == ["YunShan"] and all(q.get("reset_ok") for q in qs),
            f"exam={exam_id} seed={seed} link={link5} name={rep.get('name')!r} tags={tags} "
            f"q={[(q.get('status'), q.get('passed'), q.get('reset_ok')) for q in qs]} prep_stages={seen5['stages']}")

    # ---- R6：VLAN 题（沙箱 3，AR0 / LSW0 / PC1）----
    await goto_courses(c)
    await pick(c, "L04", TOPIC2)
    await shot(c, "r06_preview_vlan.png")
    await click(c, "#c-practice")
    seen6 = await follow_prepare(c, "r06", "practice", timeout=1500)
    tries6 = 1
    while "mode=practice" not in (await c.ev("location.search") or "") and tries6 < 3:
        # LSW0 自动重启 START_RETRIES 次后仍卡住 → 任务失败（start_failed）；像学员一样再点一次「开始练习」
        # （对 start_failed 的设备再启动 = 先停止再启动、计数清零，spec §10.5）
        job_f = ((await api(c, "GET", "api/lab/prepare"))["data"] or {}).get("job") or {}
        log(f"  准备失败（第 {tries6} 次）：{job_f.get('error')}；重新开始练习")
        await shot(c, f"r06_prepare_failed_{tries6}.png")
        await goto_courses(c)
        await pick(c, "L04", TOPIC2)
        await click(c, "#c-practice")
        more = await follow_prepare(c, f"r06_retry{tries6}", "practice", timeout=1500)
        seen6["stages"] += ["(重试)"] + more["stages"]
        seen6["msgs"] |= more["msgs"]
        seen6["seconds"] += more["seconds"]
        tries6 += 1
    await c.wait_for(WB_READY, 40)
    await c.ev(CONFIRM_JS)
    active6 = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 300)
    job6 = ((await api(c, "GET", "api/lab/prepare"))["data"] or {}).get("job") or {}
    watch = ((await api(c, "GET", "api/lab"))["data"] or {}).get("watch") or {}
    log(f"  VLAN 题准备：stages={seen6['stages']} {seen6['seconds']}s msgs={sorted(seen6['msgs'])[:6]}")
    log(f"  job.notes={job6.get('notes')} stopped_old={job6.get('stopped_old')}")
    log(f"  看门狗事件：{json.dumps(watch.get('events'), ensure_ascii=False)[:1500]}")
    stopped_t = any({"AR1", "AR2"} <= set(x.get("devices") or []) for x in (job6.get("stopped_old") or []))
    restarted = any(e.get("event") == "restart" and e.get("device") == "LSW0" for e in (watch.get("events") or []))
    chk("R6", "VLAN 题：开始练习 → 切到沙箱 3（停掉 AR1、AR2）→ 启动 AR0 / LSW0 / PC1 → 就绪探测 → 练习进行中"
              f"（LSW0 本次{'卡住、看门狗自动重启后就绪' if restarted else '没有卡住'}）",
        active6 and (stopped_t or VLAN_ONLY) and "检测设备终端" in "".join(seen6["stages"]),
        f"stages={seen6['stages']} stopped_AR1AR2={stopped_t} restarted={restarted}")

    # ---- R7：PC 设置气泡 ----
    st6 = (await api(c, "GET", "api/practice/current"))["data"] or {}
    aid2 = st6.get("attempt_id")
    before_api = (await api(c, "GET", f"api/lab/devices/{PC1}/host"))["data"] or {}
    await node_click(c, "PC1")
    await c.wait_for("document.getElementById('pc-ip').value && !document.getElementById('pc-ip').disabled", 20)
    before = await c.ev("['pc-ip', 'pc-mask', 'pc-gw', 'pc-dns'].map(id => document.getElementById(id).value)")
    waiting = await c.ev("document.getElementById('pc-ip').value = '10.0.10.66'; document.getElementById('pc-mask').value = '24';"
                         "document.getElementById('pc-gw').value = '10.0.10.1'; document.getElementById('pc-dns').value = '8.8.4.4';"
                         "document.getElementById('pc-form').requestSubmit(); document.getElementById('pc-msg').textContent")
    applied = await c.wait_for("document.getElementById('pc-msg').textContent.startsWith('已生效')", 60)
    msg = await text(c, "#pc-msg")
    await shot(c, "r07_pc_bubble.png")
    after_api = (await api(c, "GET", f"api/lab/devices/{PC1}/host"))["data"] or {}
    await click(c, "#pc-term")
    pc_term = await c.wait_for(f"(t => t && t.ws && t.ws.readyState === 1)(window.__ainet.state.tabs.get({PC1}))", 20)
    await asyncio.sleep(3)
    shows = {}
    for cmd in ("ipconfig", "ifconfig"):
        shows[cmd] = await type_cmd(c, PC1, cmd, timeout=20)
    await shot(c, "r07_pc_terminal.png")
    log(f"  PC 气泡：页面读到 {before}（接口 {before_api.get('config')}）；稍候提示 {waiting!r} → {msg!r}；接口读回 {after_api.get('config')}")
    for k, v in shows.items():
        log(f"  PC1 终端 {k}: {v[-600:]!r}")
    seen_ip = any("10.0.10.66" in v for v in shows.values())
    chk("R7", "PC 设置气泡：读出 PC1 当前地址；改 IP / 掩码 / 网关 / DNS →「已生效」；接口读回（refreshAttrs）一致；"
              "PC1 终端里查得到新地址",
        before[:3] == ["10.0.10.10", "255.255.255.0", "10.0.10.1"] and "稍候" in (waiting or "") and applied
        and after_api.get("config") == {"ip": "10.0.10.66", "mask": "255.255.255.0", "gateway": "10.0.10.1", "dns": "8.8.4.4"}
        and after_api.get("dns_supported") and pc_term and seen_ip,
        f"终端里看得到新地址={seen_ip}")

    # ---- R8：修 VLAN → 通关 → 退出 → 复位、PC 地址还原、操作记录 ----
    await c.ev(f"document.getElementById('pc-close') && document.getElementById('pc-close').click(), 0")
    ok_sw = await open_term(c, "LSW0", LSW0)
    for cmd in ("display port vlan", "system-view", "interface GE1/0/1", "port default vlan 10", "return"):
        await type_cmd(c, LSW0, cmd)
    ok_ar = await open_term(c, "AR0", AR0)

    async def poke2():
        await type_cmd(c, AR0, "ping -c 2 10.0.10.254")
        await type_cmd(c, PC1, "ping -c 2 10.0.10.1", timeout=30)
    await poke2()
    passed2 = await wait_practice_passed(c, poke2, timeout=180)
    await shot(c, "r08_vlan_passed.png")
    await click(c, "#btn-exit")
    back2 = await c.wait_for("location.pathname.endsWith('/courses')", 400)
    await c.wait_for(COURSE_READY, 30)
    await c.ev(CONFIRM_JS)
    last2 = ((await api(c, "GET", "api/practice/current"))["data"] or {}).get("last_result") or {}
    restored = (await api(c, "GET", f"api/lab/devices/{PC1}/host"))["data"] or {}
    snap_rows = db_rows("SELECT config FROM attempt_configs WHERE attempt_id = ? AND device = 'PC1' AND kind = 'before'", (aid2,))
    snap = json.loads(snap_rows[0]["config"][len("!ainet-host "):]) if snap_rows else None
    want = {k: (snap or {}).get(k, "") for k in ("ip", "mask", "gateway", "dns")}
    got = dict(restored.get("config") or {})
    if want.get("ip") and want.get("mask", "").isdigit():
        got["mask"] = str(sum(bin(int(x)).count("1") for x in got.get("mask", "0.0.0.0").split(".")))
    logs = [r["command"] for r in db_rows("SELECT command FROM attempt_logs WHERE attempt_id = ? ORDER BY id", (aid2,))]
    pc_logs = [x for x in logs if x.startswith("[PC 配置]")]
    chk("R8", "LSW0 上修复 → 自动判定通关 → 退出到课程 → reset_ok；PC1 地址（含 DNS）还原；操作记录里有「[PC 配置] PC1 …」",
        ok_sw and ok_ar and passed2 and back2 and last2.get("reset_ok") is True
        and snap is not None and got == want and got.get("dns") == ""
        and any("ip=10.0.10.66" in x and "dns=8.8.4.4" in x for x in pc_logs),
        f"reset_ok={last2.get('reset_ok')} leftovers={last2.get('leftovers')} 开练快照={snap} 复位后={restored.get('config')} "
        f"记录={pc_logs}")

    # ---- R9：档案页平台标签 ----
    await c.cmd("Page.navigate", url=BASE + "/profile")
    await c.wait_for("document.querySelectorAll('#recent table tr').length > 1", 20)
    await asyncio.sleep(1)
    ptags = await c.ev("[...document.querySelectorAll('#recent .plat-tag')].map(x => x.textContent)")
    exams = await c.ev("[...document.querySelectorAll('#exams td')].map(x => x.textContent).filter(t => t.startsWith('单题考试'))")
    await shot(c, "r09_profile.png", full=True)
    chk("R9", "档案页：练习 / 考试记录的场景带平台标签，单题考试名带平台", ptags and all(t == "YunShan" for t in ptags)
        and (VLAN_ONLY or (exams and exams[0].endswith("· YunShan"))), f"tags={ptags} exams={exams}")

    # ---- R10：收尾 ----
    await goto_courses(c)
    res = await stop_all(c)
    chk("R10", "收尾：三个沙箱的设备全部停掉（运行数为 0）", res.get("running") and all(v == 0 for v in res["running"].values()),
        json.dumps(res, ensure_ascii=False))


async def run() -> int:
    os.makedirs(SHOTS, exist_ok=True)
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
            try:
                await main(c)
            except Exception as exc:       # 某步卡住：记失败，仍尽量收尾停设备
                import traceback
                traceback.print_exc()
                chk("!", f"异常：{exc}", False)
                try:
                    await goto_courses(c)
                    log(f"  收尾：{await stop_all(c)}")
                except Exception as exc2:  # noqa: BLE001
                    log(f"  收尾失败：{exc2}")
            log(f"errors total: {c.errors or '无'}")
            chk("X", "全程无 JS 异常 / console error", not c.errors)
            reader.cancel()
    finally:
        chrome.terminate()
    print(f"SUMMARY: {sum(RESULTS)} pass / {len(RESULTS) - sum(RESULTS)} fail", flush=True)
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
