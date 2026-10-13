"""里程碑 4 + 5 集成真机 e2e（不进默认 pytest）：真实 DeepSeek 的 AI 提示与评分，与整机复位联调。

前置：同 reset_e2e.py（eNSP 打开 Campus_Network 并启动 AR0、AR2、LSW0；服务已起好、能读到 DeepSeek Key；
另起 tests/e2e/console_daemon.py，AINET_E2E_HUBSOCK 指向它；只在练习结束后才用它抓配置核对）。
学员命令全部经页面终端（xterm 输入框 + 键盘事件）发出。题目：campus-ar2-acl-block（ACL 误拦截）。

P  通关路径：开始 → 学员先排查，再依次请求第 1、2 级提示（来源必须是 AI，且不含禁止内容 / 答案）→ 继续排查，并做一些额外改动
   （AR2 上加一个无关的 LoopBack、LSW0 改 sysname 再看配置）→ 按思路修复 rule 5 → 自动判定 3/3 通关 → 结束 → 复位成功、三台设备与开练前
   快照逐行一致 → 评分卡出现（总分、三个维度、点评；评分全文里带着复位结果）→ 命令记录有输出、没有隐藏事务的命令 → 档案页显示总分。
G  盲目试错后放弃（缺省用回程路由题，--g-scenario 可换）：开始 → 不看就改（shutdown / 乱加路由 / 删正确路由 / 乱改 VLAN）→ 放弃 → 复位干净 → 分数明显偏低、点评指出盲目试错 / 副作用。
用法：python tests/e2e/ai_e2e.py BASE_URL SHOT_DIR --db DB_PATH [--only P,G]
里程碑 6 起场景参数化、练习缺省随机种子：服务必须加 AINET_PRACTICE_SEED=0（本脚本按各题默认值写死修复命令）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from browser_e2e import CDP, CHROME, step  # noqa: E402
import config  # noqa: E402
import config_reset as cr  # noqa: E402
import hints  # noqa: E402
import scenarios as sc  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("shots")
ap.add_argument("--db", required=True)
ap.add_argument("--only", default="P,G")
# G 用回程路由题：eNSP 的 AR1220 在同一接口上反复挂 / 撤 traffic-filter 后会报 Add rule failed（模拟器资源问题，
# 只能在 eNSP 里重启设备恢复），ACL 题不适合连续跑多轮
ap.add_argument("--g-scenario", default="campus-ar2-return-route")
ARGS = ap.parse_args()
BASE, SHOT_DIR = ARGS.base, ARGS.shots
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9338"))
HUBSOCK = os.environ["AINET_E2E_HUBSOCK"]
AR0, LSW0, AR2 = 2000, 2001, 2003
NAMES = {AR0: "AR0", LSW0: "LSW0", AR2: "AR2"}
SID = "campus-ar2-acl-block"
SCEN = sc.load_dir(config.SCENARIOS_DIR)[0][SID]
STATE_JS = "fetch('api/practice/current').then(r => r.json())"
PROMPT_LINE = re.compile(r"^[<\[][^\s<>\[\]]+[>\]]")
# 场景 restore / 整机复位才会发、学员在这两条路径里都不会敲的命令：出现在命令记录里就说明隐藏事务被记成了学员命令
HIDDEN_ONLY = ("undo vlan batch", "undo interface Vlanif10", "undo port link-type", "undo port default vlan",
               "display current-configuration\n", "undo sysname", "undo traffic-filter inbound",
               "undo acl number 3000", "undo interface LoopBack")

JOIN_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s + '\\n'; })"""


async def shot(c, name):
    data = await c.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=False)
    path = os.path.join(SHOT_DIR, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data["data"]))
    print(f"  screenshot: {path}")


async def buf(c, port) -> str:
    return await c.ev(f"({JOIN_JS})({port})") or ""


async def send_keys(c, port, text, enter=True):
    await c.ev(f"window.__ainet.focusTab({port}), window.__ainet.state.tabs.get({port}).term.focus(), 0")
    if text:
        await c.cmd("Input.insertText", text=text)
    if enter:
        for typ in ("keyDown", "keyUp"):
            await c.cmd("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter", windowsVirtualKeyCode=13,
                        nativeVirtualKeyCode=13, **({"text": "\r"} if typ == "keyDown" else {}))


async def term(c, port, text, timeout=30, yn="y"):
    """经页面终端敲一条命令，等到回显之后出现提示符行且缓冲区静止；遇分页按空格，遇 [Y/N] 回答 yn。"""
    before = (await buf(c, port)).rstrip("\n")
    start = max(0, len(before) - 1)
    await send_keys(c, port, text)
    end = time.time() + timeout
    last, stable_since = None, time.time()
    while time.time() < end:
        await asyncio.sleep(0.4)
        seg = (await buf(c, port))[start:]
        tail = seg.rstrip("\n").rsplit("\n", 1)[-1]
        if "---- More ----" in tail:
            await send_keys(c, port, " ", enter=False)
            continue
        if tail.rstrip().endswith(("[Y/N]:", "[Y/N]", "[y/n]:")):
            await send_keys(c, port, yn)
            continue
        if seg != last:
            last, stable_since = seg, time.time()
            continue
        pos = seg.find(text) if text else 0
        if pos >= 0:
            after = seg[pos:].split("\n")[1:]
            if any(PROMPT_LINE.match(l.strip()) for l in after) and time.time() - stable_since >= 0.8:
                return seg
    print(f"    !! 等待 {NAMES[port]} 上「{text}」超时")
    return (await buf(c, port))[start:]


async def run_lines(c, port, lines):
    outs = []
    for l in lines:
        o = await term(c, port, l)
        errs = [x.strip() for x in o.split("\n") if "Error" in x]
        print(f"    {NAMES[port]}> {l}" + (f"  ->  {errs[0]}" if errs else ""))
        outs.append(o)
    return outs


def hub_snapshot(port: int) -> str:
    path = os.path.join(SHOT_DIR, f"_cap_{port}.txt")
    s = socket.socket(socket.AF_UNIX)
    s.connect(HUBSOCK)
    s.sendall((json.dumps({"op": "snapshot", "port": port, "save": path}) + "\n").encode())
    data = b""
    while True:
        d = s.recv(65536)
        if not d:
            break
        data += d
    res = json.loads(data)
    if "error" in res:
        raise RuntimeError(res["error"])
    return open(path, encoding="utf-8").read()


def norm_lines(text: str) -> list[str]:
    out = []
    for l in text.replace("\r", "").split("\n"):
        l = l.rstrip()
        if not l.strip() or l.strip() == "display current-configuration" or re.match(r"^<[^\s<>]+>$", l.strip()):
            continue
        out.append(l)
    return out


def db_rows(sql: str, *args):
    conn = sqlite3.connect(ARGS.db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def compare(dev: str, port: int, want: str, tag: str) -> bool:
    got = hub_snapshot(port)
    a, b = norm_lines(want), norm_lines(got)
    diff = cr.differences(cr.parse(want), cr.parse(got))
    print(f"  [{tag}] {dev}: 逐行一致={a == b}（{len(b)} 行）  结构差异={diff or '无'}")
    if a != b:
        import difflib
        for l in list(difflib.unified_diff(a, b, "快照", "当前", lineterm=""))[:30]:
            print("     ", l)
    return a == b and not diff


async def back_to_list(c):
    if not await c.ev("document.getElementById('pr-summary-view').hidden"):
        await c.ev("document.getElementById('pr-back').click()")
    await asyncio.sleep(1)


async def start(c, sid):
    # 课程页选题后进操作页（?mode=practice&s=课题）：右侧是本题的「开始练习」卡片（操作页内不能换题，2026-10-07）
    await c.cmd("Page.navigate", url=f"{BASE}/?mode=practice&s={sid}")
    await c.wait_for("window.__ainet && window.__ainet.state.topo", 30)
    await asyncio.sleep(1)
    await c.ev("window.confirm = () => true")
    await c.wait_for(f"document.querySelector('.pr-card[data-id=\"{sid}\"] .pr-start') && "
                     f"!document.querySelector('.pr-card[data-id=\"{sid}\"] .pr-start').disabled", 20)
    t = time.time()
    await c.ev(f"document.querySelector('.pr-card[data-id=\"{sid}\"] .pr-start').click()")
    ok = await c.wait_for("(window.__ainet.state.practice && window.__ainet.state.practice.active "
                          "&& window.__ainet.state.practice.phase === 'running') || document.querySelector('.pr-start-err')",
                          120)
    err = await c.ev("[...document.querySelectorAll('.pr-start-err')].map(e => e.textContent)")
    st = await c.ev(STATE_JS)
    print(f"  开始 {sid}：{time.time() - t:.1f}s  phase={st.get('phase')}  last_check={st.get('last_check')}  err={err}")
    return st if ok and not err else None


async def open_tabs(c, ports):
    for p in ports:
        await c.ev(f"document.querySelector('.pr-dev[data-port=\"{p}\"]').click()")
        await c.wait_for(f"(({JOIN_JS})({p}) || '').trim().split('\\n').pop().match(/^[<\\[]/)", 25)
        await asyncio.sleep(0.5)


async def wait_pass(c, timeout=60):
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = await c.ev(STATE_JS)
        if st.get("passed"):
            return st
        await asyncio.sleep(0.5)
    return st


async def ask_hint(c, level):
    """点「提示」按钮（2026-10 起在 Agent 导师页签底部，按级别各一个），等第 level 级提示出现在工单详情里。
    返回 (text, source, 耗时)。"""
    t = time.time()
    await c.ev("window.confirm = () => true")          # 第 3 级会弹扣分确认
    await c.ev(f"document.getElementById('pr-hint-{level}').click()")
    ok = await c.wait_for(f"document.querySelectorAll('#pr-hints .hint').length >= {level} && "
                          f"!/生成中/.test(document.getElementById('pr-hint-{level}').textContent)", 40)
    info = await c.ev(f"""(() => {{ const h = document.querySelectorAll('#pr-hints .hint')[{level - 1}];
      if (!h) return null; const tag = h.querySelector('.hint-src');
      return {{source: tag.classList.contains('ai') ? 'ai' : 'static', title: tag.title,
              text: h.innerText.split('\\n').slice(1).join('\\n')}}; }})()""")
    if not ok or not info:
        return "", "none", time.time() - t
    return info["text"].strip(), info["source"], time.time() - t


async def end_practice(c, give_up=False, timeout=180):
    if give_up:
        await c.ev("window.confirm = () => true")
    t = time.time()
    await c.ev("document.getElementById('pr-end').click()")
    await asyncio.sleep(0.3)
    await c.wait_for("!document.getElementById('pr-summary-view').hidden || "
                     "(!document.getElementById('pr-end').disabled && "
                     "/还原未完成|结束失败/.test(document.getElementById('pr-msg').textContent))", timeout)
    summary = await c.ev("document.getElementById('pr-summary-view').hidden ? '' : document.getElementById('pr-summary').innerText")
    msg = await c.ev("document.getElementById('pr-msg') ? document.getElementById('pr-msg').textContent : ''")
    print(f"  结束耗时 {time.time() - t:.1f}s")
    return summary, msg


async def wait_score(c, aid, timeout=180):
    t = time.time()
    end = t + timeout
    res = None
    while time.time() < end:
        res = await c.ev(f"fetch('api/practice/result/{aid}').then(r => r.json())")
        if res.get("status") in ("done", "degraded", "failed"):
            break
        await asyncio.sleep(1.5)
    card = await c.wait_for("!!document.querySelector('#pr-score .sc-big')", 15)
    print(f"  评分完成 {time.time() - t:.1f}s  status={res.get('status')}  评分卡={bool(card)}")
    return res


def show_score(res) -> dict:
    d = res.get("detail") or {}
    s = d.get("scores") or {}
    dims = s.get("dimensions") or {}
    brief = {
        "status": res.get("status"), "total": s.get("total"),
        "result": dims.get("result", {}).get("score"), "process": dims.get("process", {}).get("score"),
        "process_rate": dims.get("process", {}).get("rate"), "side": dims.get("side", {}).get("score"),
        "side_rate": dims.get("side", {}).get("rate"), "discount": s.get("discount"),
        "reset": d.get("reset"), "ai": {k: (d.get("ai") or {}).get(k) for k in ("model", "thinking", "usage",
                                                                                "attempts", "elapsed_s")},
        "signals": {k: (d.get("signals") or {}).get(k) for k in (
            "commands_total", "readonly_before_first_config", "config_without_prior_inspection",
            "dangerous_or_extra_commands", "error_commands", "down_logs_after_student_config")},
    }
    print("  评分摘要:", json.dumps(brief, ensure_ascii=False))
    print("  过程分理由:", dims.get("process", {}).get("reason"))
    print("  规范分理由:", dims.get("side", {}).get("reason"))
    c = d.get("comment") or {}
    print("  点评 summary:", c.get("summary"))
    for k in ("good", "problems", "suggestions"):
        for x in c.get(k) or []:
            print(f"    [{k}] {x}")
    print("  标准思路:", (d.get("standard_path") or "")[:400].replace("\n", " / "))
    return brief


def check_logs(aid) -> tuple[bool, list]:
    rows = db_rows("SELECT device, command, output FROM attempt_logs WHERE attempt_id = ? ORDER BY id", aid)
    bad = [r for r in rows if any(h.strip() == (r[1] or "").strip() or (r[1] or "").startswith(h.strip())
                                  for h in HIDDEN_ONLY if h.strip())]
    print(f"  命令记录 {len(rows)} 条；疑似隐藏事务命令：{bad or '无'}")
    for d, cmd, out in rows:
        o = (out or "").replace("\n", " ⏎ ")
        print(f"    {d}> {cmd}  ||  {o[:140]}")
    return not bad, rows


async def profile_score(c, aid_total):
    await c.cmd("Page.navigate", url=f"{BASE}/profile")
    await c.wait_for("document.querySelectorAll('#recent tr').length > 1", 15)
    await asyncio.sleep(0.5)
    row = await c.ev("""(() => { const hs = [...document.querySelectorAll('#recent th')].map(e => e.textContent);
      const r = document.querySelectorAll('#recent tr')[1]; const cs = [...r.querySelectorAll('td')].map(e => e.textContent);
      return Object.fromEntries(hs.map((h, i) => [h, cs[i]])); })()""")
    print(f"  档案页最近一条：{row}")
    return row


async def run() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    only = set(ARGS.only.split(","))
    banned = hints.banned_terms(SCEN)
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
                             "--window-size=1600,1000", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results: list[bool] = []
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=1000, deviceScaleFactor=1, mobile=False)

            async def home():
                await c.cmd("Page.navigate", url=f"{BASE}/login")
                await asyncio.sleep(1.5)
                await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                           "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
                await c.cmd("Page.navigate", url=f"{BASE}/")
                await c.wait_for("window.__ainet && window.__ainet.state.topo && "
                                 "document.querySelectorAll('#topo-svg .node').length > 0", 15)
                await asyncio.sleep(1.5)
            await home()

            if "P" in only:
                print("== P ACL 题：AI 提示 → 额外改动与排障 → 修复通关 → 复位 → AI 评分 → 档案 ==")
                st = await start(c, SID)
                ok_start = bool(st) and st["last_check"]["passed"] == 1 and st["last_check"]["total"] == 3
                aid = st["attempt_id"] if st else None
                snaps = dict(db_rows("SELECT device, config FROM attempt_configs WHERE attempt_id = ?", aid))
                await open_tabs(c, [LSW0, AR0, AR2])
                await run_lines(c, LSW0, ["ping -c 2 192.168.2.1"])
                await run_lines(c, AR0, ["ping -c 2 192.168.2.1", "display ip routing-table 192.168.2.1"])
                h1, s1, t1 = await ask_hint(c, 1)
                print(f"  第 1 级提示（{s1}，{t1:.1f}s）：{h1}")
                await run_lines(c, AR0, ["ping -c 2 -a 10.0.10.1 192.168.2.1"])
                h2, s2, t2 = await ask_hint(c, 2)
                print(f"  第 2 级提示（{s2}，{t2:.1f}s）：{h2}")
                await shot(c, "p1_hints.png")
                leak1, leak2 = hints.leaks(h1, banned), hints.leaks(h2, banned)
                answer = re.compile(r"10\.0\.20\.0|undo rule|rule 5 deny|改成.*访客")
                print(f"  泄露检查：L1={leak1 or '无'}  L2={leak2 or '无'}  答案字样：L1={bool(answer.search(h1))} "
                      f"L2={bool(answer.search(h2))}")
                results.append(step("P1", "ACL 题开始（1/3）后，第 1、2 级提示均来自 AI，且不含禁止内容 / 答案",
                                    ok_start and s1 == "ai" and s2 == "ai" and h1 and h2 and not leak1 and not leak2
                                    and not answer.search(h1) and not answer.search(h2)))
                # 继续排查 + 额外改动（与故障无关）：AR2 加 LoopBack1，LSW0 改名后再看配置（输出采集要跟上新提示符）
                await run_lines(c, AR2, ["display acl 3000", "display traffic-filter applied-record"])
                await run_lines(c, AR2, ["system-view", "interface LoopBack1", "ip address 172.16.9.9 32", "return"])
                await run_lines(c, LSW0, ["system-view", "sysname SW-TEST", "display this", "return",
                                          "display ip interface brief"])
                # 按提示思路修复（保留接口过滤，只改 rule 5 的源）
                await run_lines(c, AR2, ["system-view", "acl number 3000", "undo rule 5",
                                         "rule 5 deny ip source 10.0.20.0 0.0.0.255 destination 192.168.2.0 0.0.0.255",
                                         "return"])
                t = time.time()
                st2 = await wait_pass(c)
                print(f"  修复到通关 {time.time() - t:.1f}s  last_check={st2.get('last_check')}")
                o = await term(c, LSW0, "ping -c 2 192.168.2.1")
                ok_ping = "packet(s) received" in o and "0 packet(s) received" not in o
                await shot(c, "p2_passed.png")
                results.append(step("P2", "额外改动（LoopBack1、sysname SW-TEST）后按思路修复 rule 5，自动判定 3/3 通关，LSW0 ping 通",
                                    bool(st2.get("passed")) and st2["last_check"]["passed"] == 3 and ok_ping))
                summary, msg = await end_practice(c)
                print("  总结:", summary.replace("\n", " | ")[:600], msg)
                same = all([compare("AR0", AR0, snaps["AR0"], "P"), compare("AR2", AR2, snaps["AR2"], "P"),
                            compare("LSW0", LSW0, snaps["LSW0"], "P")])
                results.append(step("P3", "结束：复位成功（总结显示已复位），三台设备与开练前快照逐行一致（含 sysname、LoopBack1 撤销）",
                                    "已复位到开练前配置" in summary and same))
                res = await wait_score(c, aid)
                await c.ev("document.getElementById('pr-score').scrollIntoView()")
                await shot(c, "p3_score_card.png")
                brief = show_score(res)
                card = await c.ev("document.getElementById('pr-score').innerText")
                dims_ok = all(isinstance(brief.get(k), (int, float)) for k in ("total", "result", "process", "side"))
                comment = (res.get("detail") or {}).get("comment") or {}
                results.append(step("P4", "评分卡出现：status=done，总分与结果 / 过程 / 规范三维分数齐全，有点评；评分全文带着复位结果（ok）",
                                    res.get("status") == "done" and dims_ok and brief["result"] == 50
                                    and bool(comment.get("summary")) and (brief.get("reset") or {}).get("ok") is True
                                    and "结果" in card and "过程" in card and "规范" in card))
                ok_logs, rows = check_logs(aid)
                cmds = [r[1] for r in rows]
                outs = {r[1]: r[2] for r in rows}
                after_rename = outs.get("display ip interface brief") or ""
                # 改名后 S5700 的 DATASYNC 日志可能不换行、学员回显接在日志行末尾：display this 也必须记上
                ok_rename = ("sysname SW-TEST" in cmds and "display this" in cmds
                             and "sysname SW-TEST" in (outs.get("display this") or "")
                             and "Vlanif10" in after_rename and "SW-TEST" not in after_rename)
                results.append(step("P5", "命令记录：学员命令都有输出摘要，改名后命令的输出照常采集；没有隐藏事务（注入 / 判定 / 复位）的命令",
                                    ok_logs and ok_rename and "display acl 3000" in cmds
                                    and "rule 5 deny ip source 10.0.10.0" in (outs.get("display acl 3000") or "")))
                row = await profile_score(c, brief["total"])
                await shot(c, "p4_profile.png")
                want = str(round(brief["total"] * 10) / 10).removesuffix(".0") if brief["total"] is not None else "?"
                results.append(step("P6", f"档案页最近一条显示总分 {want}", row.get("得分") == want))
                await home()

            if "G" in only:
                print(f"== G {ARGS.g_scenario}：盲目试错后放弃 ==")
                st = await start(c, ARGS.g_scenario)
                aid = st["attempt_id"] if st else None
                snaps = dict(db_rows("SELECT device, config FROM attempt_configs WHERE attempt_id = ?", aid))
                await open_tabs(c, [AR0, AR2, LSW0])
                # 不看就改：关 / 开互联接口、乱加路由、删掉 LSW0 正确的缺省路由、在交换机上乱改接入 VLAN
                await run_lines(c, AR0, ["system-view", "interface GigabitEthernet0/0/1", "shutdown", "undo shutdown",
                                         "quit", "ip route-static 192.168.2.0 24 10.0.12.9", "return"])
                await run_lines(c, AR2, ["system-view", "ip route-static 10.0.10.0 24 10.0.12.9",
                                         "undo ip route-static 192.168.9.0 24 10.0.12.1", "return"])
                await run_lines(c, LSW0, ["system-view", "undo ip route-static 0.0.0.0 0 10.0.10.1",
                                          "ip route-static 0.0.0.0 0 10.0.10.9", "vlan batch 30",
                                          "interface GigabitEthernet0/0/1", "port default vlan 30",
                                          "port default vlan 10", "return"])
                await asyncio.sleep(12)                       # 让自动判定跑一轮（应仍未通关）
                stg = await c.ev(STATE_JS)
                print(f"  放弃前 last_check={stg.get('last_check')}  passed={stg.get('passed')}")
                summary, msg = await end_practice(c, give_up=True)
                print("  总结:", summary.replace("\n", " | ")[:600], msg)
                same = all([compare("AR0", AR0, snaps["AR0"], "G"), compare("AR2", AR2, snaps["AR2"], "G"),
                            compare("LSW0", LSW0, snaps["LSW0"], "G")])
                results.append(step("G1", "盲目试错后放弃：未通关，结束后复位成功，三台设备与开练前快照逐行一致",
                                    not stg.get("passed") and "未通关" in summary and "已复位到开练前配置" in summary and same))
                res = await wait_score(c, aid)
                await c.ev("document.getElementById('pr-score').scrollIntoView()")
                await shot(c, "g1_score_card.png")
                brief = show_score(res)
                check_logs(aid)
                comment = (res.get("detail") or {}).get("comment") or {}
                text = json.dumps(comment, ensure_ascii=False) + str((res.get("detail") or {}).get("scores"))
                points = re.search(r"盲目|试错|没有(先)?(查看|确认|定位)|未(先)?(查看|定位)|直接(修改|改)|删除.*ACL|破坏|副作用|shutdown",
                                   text)
                results.append(step("G2", "盲目试错的评分明显偏低（结果 0、过程打折，总分 < 30），点评指出盲目试错 / 副作用",
                                    res.get("status") == "done" and brief["result"] == 0 and brief["total"] is not None
                                    and brief["total"] < 30 and bool(points)))
                await home()

            print("errors total:", c.errors or "无")
            results.append(step("J", "全程无 JS 异常", not c.errors))
            reader.cancel()
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
