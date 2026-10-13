"""里程碑 4 真机 e2e（不进默认 pytest）：Campus 新题跑通 + 整机配置复位（含学员额外改动、sysname 改名、放弃路径）。

前置：eNSP 打开 Campus_Network 并启动 AR0、AR2、LSW0（未配置设备）；服务已起好（参数同 practice_e2e.py，
AINET_ENSP_RUNNING_IDS 再加上 LSW0 的 GUID），AINET_DB_PATH 指向的库与本脚本的 --db 相同。
独立核对：另起 tests/e2e/console_daemon.py（AINET_E2E_HUBSOCK 指向它的 unix socket），每道题结束后用它抓
display current-configuration，与库里开练前的快照逐行比对。它是同一 console 端口上的第二条连接（输入输出共享），
所以只在练习结束、服务没有隐藏事务时才请求，否则两边的输入会在设备上交错。
学员命令全部经页面终端（xterm 输入框 + 键盘事件）发出。

步骤：
T1 拓扑图：Campus 的接口标注与真机一致（AR0 GE0/0/0–LSW0 GE0/0/1，AR0 GE0/0/1–AR2 GE0/0/0）。
A  campus-lsw0-access-vlan：开始 → LSW0 上修复 → 自动判定通关 → 学员在 AR0、LSW0 上做一批额外改动（接口 IP 增 / 改 / 删、shutdown、
   LoopBack、静态路由、OSPF、ACL + traffic-filter、VLAN、trunk、stp、sysname）→ 结束 → 设备配置与开练前快照逐行一致。
B  campus-ar2-return-route：开始 → AR2 上修复 → 通关 → 删掉 baseline 里的配置、改 IP、改名 → 结束 → 逐行一致。
C  campus-ar2-acl-block：放弃路径。学员改 ACL、取消接口过滤、改名，并停在系统视图点结束 → 立即进入总结页（后台复位中），
   终端被强制收回到用户视图、复位完成后三台设备逐行一致（2026-10-08 起结束练习改为后台复位，不再要求学员回用户视图重试）。
C2 campus-ar2-acl-block：通关路径。按参考答案改写 rule 5（保留接口过滤）→ 自动判定 3/3 通关 → 结束 → 逐行一致。
F  三台设备回到开工前的未配置状态（与 --archive 目录里的存档逐行一致）。
用法：python tests/e2e/reset_e2e.py BASE_URL SHOT_DIR --db DB_PATH --archive DIR
里程碑 6 起场景参数化、练习缺省随机种子：服务必须加 AINET_PRACTICE_SEED=0（本脚本按各题默认值写死故障与修复命令）。
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
import config_reset as cr  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("shots")
ap.add_argument("--db", required=True)
ap.add_argument("--archive", required=True)
ap.add_argument("--only", default="T1,A,B,C,C2,F")
ARGS = ap.parse_args()
BASE, SHOT_DIR = ARGS.base, ARGS.shots
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9337"))
HUBSOCK = os.environ["AINET_E2E_HUBSOCK"]
AR0, LSW0, AR2 = 2000, 2001, 2003
NAMES = {AR0: "AR0", LSW0: "LSW0", AR2: "AR2"}
STATE_JS = "fetch('api/practice/current').then(r => r.json())"
PROMPT_LINE = re.compile(r"^[<\[][^\s<>\[\]]+[>\]]")

JOIN_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s + '\\n'; })"""


async def shot(c, name):
    data = await c.cmd("Page.captureScreenshot", format="png")
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


async def term(c, port, text, timeout=25, yn="y"):
    """经页面终端敲一条命令，等到回显之后出现提示符行且缓冲区静止；遇分页按空格，遇 [Y/N] 回答 yn。返回新增内容。"""
    before = (await buf(c, port)).rstrip("\n")
    start = max(0, len(before) - 1)
    await send_keys(c, port, text)
    end = time.time() + timeout
    last, stable_since = None, time.time()
    while time.time() < end:
        await asyncio.sleep(0.4)
        b = await buf(c, port)
        seg = b[start:]
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
        outs.append(o)
        if errs:
            print(f"    {NAMES[port]}> {l}  ->  {errs[0]}")
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


def db_snapshots(attempt_id: int) -> dict[str, str]:
    conn = sqlite3.connect(ARGS.db)
    rows = conn.execute("SELECT device, config FROM attempt_configs WHERE attempt_id = ? AND kind = 'before'",
                        (attempt_id,)).fetchall()
    conn.close()
    return dict(rows)


def compare(dev: str, port: int, want: str, tag: str) -> bool:
    got = hub_snapshot(port)
    a, b = norm_lines(want), norm_lines(got)
    same = a == b
    diff = cr.differences(cr.parse(want), cr.parse(got))
    print(f"  [{tag}] {dev}: 逐行一致={same}（{len(b)} 行）  结构差异={diff or '无'}")
    if not same:
        import difflib
        for l in list(difflib.unified_diff(a, b, "快照", "当前", lineterm=""))[:30]:
            print("     ", l)
    return same and not diff


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
                          "&& window.__ainet.state.practice.phase === 'running') || document.querySelector('.pr-start-err')", 120)
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


async def end_practice(c, give_up=False, timeout=180):
    """点结束：接口立即返回、立即进总结页（复位在后台进行，总结页显示「后台复位中…」），再等复位结果出来。
    返回 (复位完成后的总结文本, 提示信息, 进入总结页耗时)。"""
    if give_up:
        await c.ev("window.confirm = () => true")
    t = time.time()
    await c.ev("document.getElementById('pr-end').click()")
    await c.wait_for("!document.getElementById('pr-summary-view').hidden || "
                     "(!document.getElementById('pr-end').disabled && "
                     "/结束失败/.test(document.getElementById('pr-msg').textContent))", 30)
    t_summary = time.time() - t
    await c.wait_for("document.getElementById('pr-summary-view').hidden || (document.getElementById('pr-reset') && "
                     "!/后台复位中/.test(document.getElementById('pr-reset').textContent))", timeout)
    summary = await c.ev("document.getElementById('pr-summary-view').hidden ? '' : document.getElementById('pr-summary').innerText")
    msg = await c.ev("document.getElementById('pr-msg') ? document.getElementById('pr-msg').textContent : ''")
    print(f"  进入总结页 {t_summary:.1f}s，复位完成共 {time.time() - t:.1f}s")
    return summary, msg, t_summary


async def check_labels(c):
    """拓扑图上 Campus 的接口标注（每条连线两端 <title> 为全名）。"""
    info = await c.ev("""(() => {
      const t = window.__ainet.state.topo; const name = id => (t.devices.find(d => d.id === id) || {}).name;
      const gs = [...document.querySelectorAll('#topo-svg .link')];
      return gs.map(g => [...g.querySelectorAll('.if-label')].map(e => [e.textContent, e.querySelector('title').textContent]))
        .map((labs, i) => ({labs, link: t.links[i] && [name(t.links[i].src_id), t.links[i].src_label,
                                                     name(t.links[i].dst_id), t.links[i].dst_label]}));
    })()""")
    want = {("AR0", "AR2"): {"AR0": "GigabitEthernet0/0/1", "AR2": "GigabitEthernet0/0/0"},
            ("AR0", "LSW0"): {"AR0": "GigabitEthernet0/0/0", "LSW0": "GigabitEthernet0/0/1"}}
    ok = True
    for pair, ends in want.items():
        hit = [x for x in info if x["link"] and {x["link"][0], x["link"][2]} == set(pair)]
        if not hit:
            print(f"  连线 {pair} 未找到")
            ok = False
            continue
        x = hit[0]
        got = {x["link"][0]: x["labs"][0][1], x["link"][2]: x["labs"][1][1]}
        shown = [l[0] for l in x["labs"]]
        print(f"  拓扑图 {pair[0]}–{pair[1]}：标注 {shown}（全名 {got}）")
        ok = ok and got == ends
    return ok


async def run() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    only = set(ARGS.only.split(","))
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
                             "--window-size=1600,900", "about:blank"],
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=900, deviceScaleFactor=1, mobile=False)
            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                       "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
            await c.cmd("Page.navigate", url=f"{BASE}/")
            await c.wait_for("window.__ainet && window.__ainet.state.topo && "
                             "document.querySelectorAll('#topo-svg .node').length > 0", 15)
            await asyncio.sleep(1.5)

            if "T1" in only:
                ok = await check_labels(c)
                await shot(c, "t1_campus_labels.png")
                results.append(step("T1", "拓扑图接口标注与真机一致（AR0 GE0/0/0–LSW0 GE0/0/1、AR0 GE0/0/1–AR2 GE0/0/0）", ok))

            if "A" in only:
                print("== A campus-lsw0-access-vlan：通关 + 学员额外改动 ==")
                st = await start(c, "campus-lsw0-access-vlan")
                ok_start = bool(st) and st["last_check"]["passed"] == 0
                snaps = db_snapshots(st["attempt_id"]) if st else {}
                await open_tabs(c, [AR0, LSW0])
                await shot(c, "a1_started.png")
                o = await term(c, LSW0, "display port vlan")
                pvid = [l.strip() for l in o.split("\n") if l.strip().startswith("GigabitEthernet0/0/1 ")]
                print(f"  LSW0 display port vlan（注入后）：{pvid}")
                await run_lines(c, LSW0, ["system-view", "interface GigabitEthernet0/0/1", "port default vlan 10", "return"])
                t = time.time()
                st2 = await wait_pass(c)
                print(f"  修复到通关 {time.time() - t:.1f}s  last_check={st2.get('last_check')}")
                await shot(c, "a2_passed.png")
                results.append(step("A1", "接入 VLAN 题：注入生效（0/1、GE0/0/1 PVID 20）→ 学员修复 → 自动判定通关",
                                    ok_start and bool(pvid) and " 20 " in f" {pvid[0]} " and bool(st2.get("passed"))))
                # 学员额外改动（通关后继续在终端里改）
                await run_lines(c, LSW0, [
                    "system-view", "vlan batch 30", "interface GigabitEthernet0/0/2", "port link-type trunk",
                    "port trunk allow-pass vlan 30", "interface GigabitEthernet0/0/3", "shutdown",
                    "interface Vlanif10", "undo ip address", "quit", "stp mode rstp", "sysname SW-Stu", "return"])
                await run_lines(c, AR0, [
                    "system-view", "interface GigabitEthernet0/0/0", "ip address 10.0.10.9 24",
                    "interface GigabitEthernet0/0/1", "ip address 10.9.9.1 24", "interface LoopBack1",
                    "ip address 1.1.1.1 32", "quit", "ip route-static 192.168.50.0 24 10.9.9.2",
                    "acl number 2000", "rule 5 deny source 10.9.9.0 0.0.0.255", "quit",
                    "interface GigabitEthernet0/0/1", "traffic-filter outbound acl 2000", "quit",
                    "ospf 1 router-id 1.1.1.1", "area 0", "network 10.9.9.0 0.0.0.255", "quit", "quit",
                    "sysname AR0-Stu", "return"])
                tails = {p: (await buf(c, p)).rstrip().rsplit("\n", 1)[-1].strip() for p in (AR0, LSW0)}
                print(f"  改动后提示符：{tails}")
                await shot(c, "a3_student_changes.png")
                summary, msg, _ = await end_practice(c)
                print("  总结:", summary.replace("\n", " | "), msg)
                await asyncio.sleep(1)
                tails = {p: (await buf(c, p)).rstrip().rsplit("\n", 1)[-1].strip() for p in (AR0, LSW0)}
                print(f"  结束后终端提示符：{tails}")
                await shot(c, "a4_summary.png")
                same = all([compare("AR0", AR0, snaps["AR0"], "A"), compare("LSW0", LSW0, snaps["LSW0"], "A")])
                results.append(step("A2", "结束：额外改动（含 sysname）全部撤销，AR0、LSW0 与开练前快照逐行一致，提示符回到 <Huawei>",
                                    "已复位到开练前配置" in summary and same
                                    and tails == {AR0: "<Huawei>", LSW0: "<Huawei>"}))

            if "B" in only:
                print("== B campus-ar2-return-route：通关 + 删改 baseline 配置 ==")
                st = await start(c, "campus-ar2-return-route")
                ok_start = bool(st) and st["last_check"]["passed"] == 0
                snaps = db_snapshots(st["attempt_id"]) if st else {}
                await open_tabs(c, [AR2, LSW0])
                o = await term(c, LSW0, "ping -c 2 192.168.2.1")
                lost = "0 packet(s) received" in o
                o = await term(c, AR2, "display ip routing-table 10.0.10.1")
                rt = [l.strip() for l in o.split("\n") if "10.0.10.0/24" in l]
                print(f"  LSW0 ping 不通={lost}；AR2 路由：{rt}")
                await run_lines(c, AR2, ["system-view", "undo ip route-static 10.0.10.0 24 10.0.12.3",
                                         "ip route-static 10.0.10.0 24 10.0.12.1", "return"])
                t = time.time()
                st2 = await wait_pass(c)
                print(f"  修复到通关 {time.time() - t:.1f}s  last_check={st2.get('last_check')}")
                await shot(c, "b1_passed.png")
                results.append(step("B1", "回程路由题：注入生效（LSW0 ping 不通、AR2 下一跳 10.0.12.3）→ 修复 → 自动判定通关",
                                    ok_start and lost and bool(rt) and "10.0.12.3" in rt[0] and bool(st2.get("passed"))))
                await run_lines(c, AR2, [
                    "system-view", "undo interface LoopBack0", "interface GigabitEthernet0/0/0", "ip address 10.0.12.20 24",
                    "interface GigabitEthernet0/0/1", "shutdown", "quit", "ip route-static 0.0.0.0 0 10.0.12.1",
                    "sysname AR2-Stu", "return"])
                await run_lines(c, LSW0, ["system-view", "undo ip route-static 0.0.0.0 0 10.0.10.1",
                                          "interface Vlanif10", "ip address 10.0.11.254 24", "return"])
                summary, msg, _ = await end_practice(c)
                print("  总结:", summary.replace("\n", " | "), msg)
                await shot(c, "b2_summary.png")
                same = all([compare("AR0", AR0, snaps["AR0"], "B"), compare("AR2", AR2, snaps["AR2"], "B"),
                            compare("LSW0", LSW0, snaps["LSW0"], "B")])
                results.append(step("B2", "结束：被删的 baseline 配置补回再随场景撤销，三台设备与开练前快照逐行一致",
                                    "已复位到开练前配置" in summary and same))

            if "C" in only:
                print("== C campus-ar2-acl-block：放弃路径 + 停在系统视图 ==")
                st = await start(c, "campus-ar2-acl-block")
                # 故障态下「接口仍应用过滤」这一项本来就满足：1/3
                ok_start = bool(st) and st["last_check"]["passed"] == 1 and st["last_check"]["total"] == 3
                snaps = db_snapshots(st["attempt_id"]) if st else {}
                await open_tabs(c, [AR2])
                o = await term(c, AR2, "display acl all")
                rule5 = [l.strip() for l in o.split("\n") if l.strip().startswith("rule 5")]
                print(f"  AR2 ACL（注入后）：{rule5}")
                await run_lines(c, AR2, ["system-view", "acl number 3000", "undo rule 10", "rule 15 permit icmp",
                                         "quit", "interface GigabitEthernet0/0/0", "undo traffic-filter inbound",
                                         "quit", "sysname AR2-X"])
                # 停在系统视图点结束：立即进总结页，终端被强制收回（return）后后台复位
                summary, msg, t_summary = await end_practice(c, give_up=True)
                print("  总结:", summary.replace("\n", " | "), msg)
                await shot(c, "c2_giveup_summary.png")
                tail = (await buf(c, AR2)).rstrip().rsplit("\n", 1)[-1].strip()
                print(f"  复位完成后 AR2 终端最后一行：{tail!r}")
                ok_reclaim = t_summary < 5 and "复位完成" in tail
                same = all([compare("AR0", AR0, snaps["AR0"], "C"), compare("AR2", AR2, snaps["AR2"], "C"),
                            compare("LSW0", LSW0, snaps["LSW0"], "C")])
                me = await c.ev("fetch('api/me').then(r => r.json())")
                rec = me["recent_attempts"][0]
                results.append(step("C1", "ACL 题放弃路径：注入生效（rule 5 deny 10.0.10.0、1/3）；停在系统视图点结束，立即进总结页，"
                                          "终端被强制收回、后台复位后三台设备与开练前快照逐行一致，档案记 passed=0",
                                    ok_start and bool(rule5) and "10.0.10.0" in rule5[0] and ok_reclaim
                                    and "未通关" in summary and "已复位到开练前配置" in summary and same
                                    and rec["passed"] == 0 and rec["scenario_key"] == "campus-ar2-acl-block"))

            if "C2" in only:
                print("== C2 campus-ar2-acl-block：通关路径（按参考答案改 ACL，保留过滤）==")
                st = await start(c, "campus-ar2-acl-block")
                ok_start = bool(st) and st["last_check"]["passed"] == 1 and st["last_check"]["total"] == 3
                snaps = db_snapshots(st["attempt_id"]) if st else {}
                await open_tabs(c, [AR2, LSW0])
                o = await term(c, LSW0, "ping -c 2 192.168.2.1")
                lost = "0 packet(s) received" in o
                await run_lines(c, AR2, ["system-view", "acl number 3000", "undo rule 5",
                                         "rule 5 deny ip source 10.0.20.0 0.0.0.255 destination 192.168.2.0 0.0.0.255",
                                         "return"])
                t = time.time()
                st2 = await wait_pass(c)
                print(f"  修复到通关 {time.time() - t:.1f}s  last_check={st2.get('last_check')}")
                o = await term(c, LSW0, "ping -c 2 192.168.2.1")
                ok_ping = "2 packet(s) received" in o or "1 packet(s) received" in o
                o = await term(c, AR2, "display acl all")
                rules = [l.strip() for l in o.split("\n") if l.strip().startswith("rule ")]
                print(f"  修复后 LSW0 ping 通={ok_ping}；AR2 ACL：{rules}")
                await shot(c, "c3_acl_passed.png")
                summary, msg, _ = await end_practice(c)
                print("  总结:", summary.replace("\n", " | "), msg)
                await shot(c, "c4_acl_summary.png")
                same = all([compare("AR0", AR0, snaps["AR0"], "C2"), compare("AR2", AR2, snaps["AR2"], "C2"),
                            compare("LSW0", LSW0, snaps["LSW0"], "C2")])
                results.append(step("C2", "ACL 题通关路径：故障态 LSW0 ping 不通 → 按参考答案改写 rule 5（保留过滤）→ 自动判定 3/3 通关 → "
                                          "结束后三台设备与开练前快照逐行一致",
                                    ok_start and lost and bool(st2.get("passed")) and st2["last_check"]["passed"] == 3
                                    and ok_ping and any("10.0.20.0" in r for r in rules)
                                    and "已复位到开练前配置" in summary and same))

            if "F" in only:
                print("== F 设备回到开工前状态 ==")
                ok = True
                for p, name in NAMES.items():
                    want = open(os.path.join(ARGS.archive, f"{name}_before.txt"), encoding="utf-8").read()
                    ok = compare(name, p, want, "F") and ok
                results.append(step("F", "三台设备与开工前存档逐行一致（未配置状态）", ok))

            print("errors total:", c.errors or "无")
            results.append(step("J", "全程无 JS 异常", not c.errors))
            reader.cancel()
            print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
            return 0 if all(results) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
