"""练习闭环真机 e2e（不进默认 pytest）：Campus 场景「AR0 与 AR2 互联不通」。

前置：eNSP 打开 Campus_Network 并启动 AR0、AR2（未配置设备）；服务已用 AINET_TOPO_DIRS=tests/fixtures/topos、
ENSP_HOST=<设备主机>、临时 AINET_DB_PATH 起好，Linux 上用 AINET_ENSP_RUNNING_IDS=<AR0 GUID>,<AR2 GUID> 覆盖运行中设备。
里程碑 6 起场景参数化、练习缺省随机种子：服务必须加 AINET_PRACTICE_SEED=0（本脚本按默认值 10.0.21.2 等写死）。
学员命令全部经页面终端（xterm 输入框 + 键盘事件）发出。

步骤（主路径）：场景列表显示就绪 → 开始练习（注入；学员终端看不到注入 / 判定命令）→ 学员在 AR2 上排查修复
→ 系统 3~10 秒内自动判定通关 → 结束练习（restore）→ 用 display 核对设备回到开练前状态 → 档案里有 passed=1 的记录。
放弃路径：再开始一次，不修复直接结束，restore 照样还原干净，档案记 passed=0。
用法：python tests/e2e/practice_e2e.py [base_url] [截图目录]
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_e2e import BUF_JS, CDP, CHROME, PROMPT_JS, step  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
SHOT_DIR = sys.argv[2] if len(sys.argv) > 2 else "."
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9336"))
SID = "campus-ar0-ar2-link"
AR0, AR2 = 2000, 2003
# 注入 / 判定 / 还原命令里才有、学员此时还没敲过的字样
SENTINELS = {AR0: ["ping -c 2", "packet(s)", "system-view", "undo ip", "ip address", "10.0.21.2"],
             AR2: ["ping -c 2", "packet(s)", "system-view", "undo ip", "ip address", "10.0.21.2", "10.0.12.2"]}
STATE_JS = "fetch('api/practice/current').then(r => r.json())"


async def shot(c, name):
    data = await c.cmd("Page.captureScreenshot", format="png")
    path = os.path.join(SHOT_DIR, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data["data"]))
    print(f"  screenshot: {path}")
    return path


# 终端缓冲区：xterm 按列宽软换行的行（isWrapped）拼回一行，避免字样被折行拆开而漏检
JOIN_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return null;
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s + '\\n'; })"""


async def buf(c, port) -> str:
    return await c.ev(f"({JOIN_JS})({port})") or ""


async def type_cmd(c, port, text, wait_prompt=True, timeout=20):
    """经页面终端敲一条命令：聚焦该标签的 xterm，insertText + 回车键事件。返回本次新增的缓冲区内容。"""
    before = await buf(c, port)
    await c.ev(f"window.__ainet.focusTab({port}), window.__ainet.state.tabs.get({port}).term.focus(), 0")
    await c.cmd("Input.insertText", text=text)
    for typ in ("keyDown", "keyUp"):
        await c.cmd("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter",
                    windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, **({"text": "\r"} if typ == "keyDown" else {}))
    if not wait_prompt:
        return ""
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(0.4)
        b = await buf(c, port)
        new = b[len(before.rstrip("\n")) - 200:] if len(b) > len(before) else ""
        lines = [l for l in b.rstrip("\n").split("\n")]
        if text in b[max(0, len(before.rstrip()) - 100):] and re.match(r"^[<\[][^\s<>\[\]]+[>\]]\s*$", lines[-1]):
            return new
    return await buf(c, port)


def leaked(text: str, port: int) -> list[str]:
    return [s for s in SENTINELS[port] if s in text]


async def wait_state(c, pred_js, timeout):
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = await c.ev(STATE_JS)
        if await c.ev(f"(s => {pred_js})({json.dumps(st)})"):
            return st, True
        await asyncio.sleep(0.5)
    return st, False


async def verify_clean(c, results, tag):
    """经页面终端用 display 核对两台设备：互联接口没有遗留 IP / 配置，与开练前一致。"""
    out0 = await type_cmd(c, AR0, "display ip interface brief")
    cfg0 = await type_cmd(c, AR0, "display current-configuration interface GigabitEthernet0/0/1")
    out2 = await type_cmd(c, AR2, "display ip interface brief")
    cfg2 = await type_cmd(c, AR2, "display current-configuration interface GigabitEthernet0/0/0")
    b0, b2 = await buf(c, AR0), await buf(c, AR2)
    # 取各自最后一次输出
    def last_block(b, cmd):
        i = b.rfind(cmd)
        return b[i:] if i >= 0 else ""
    ib0 = last_block(b0, "display ip interface brief")
    ib0 = ib0[:ib0.find("display current-configuration")] if "display current-configuration" in ib0 else ib0
    cf0 = last_block(b0, "display current-configuration interface GigabitEthernet0/0/1")
    ib2 = last_block(b2, "display ip interface brief")
    ib2 = ib2[:ib2.find("display current-configuration")] if "display current-configuration" in ib2 else ib2
    cf2 = last_block(b2, "display current-configuration interface GigabitEthernet0/0/0")
    rows0 = [l.strip() for l in ib0.split("\n") if l.startswith(("GigabitEthernet", "NULL0", "Vlanif"))]
    rows2 = [l.strip() for l in ib2.split("\n") if l.startswith(("GigabitEthernet", "NULL0", "Vlanif"))]
    print(f"  [{tag}] AR0 ip int brief: {rows0}")
    print(f"  [{tag}] AR0 GE0/0/1 配置: {[l for l in cf0.split(chr(10)) if l.strip()][:6]}")
    print(f"  [{tag}] AR2 ip int brief: {rows2}")
    print(f"  [{tag}] AR2 GE0/0/0 配置: {[l for l in cf2.split(chr(10)) if l.strip()][:6]}")
    want0 = ["GigabitEthernet0/0/0              unassigned           up         down",
             "GigabitEthernet0/0/1              unassigned           up         down",
             "NULL0                             unassigned           up         up(s)"]
    want2 = ["GigabitEthernet0/0/0              unassigned           up         down",
             "GigabitEthernet0/0/1              unassigned           down       down",
             "NULL0                             unassigned           up         up(s)"]
    norm = lambda rows: [re.sub(r"\s+", " ", r) for r in rows]
    l0 = [l.strip() for l in cf0.split("\n")]
    l2 = [l.strip() for l in cf2.split("\n")]
    ok = (norm(rows0) == norm(want0) and norm(rows2) == norm(want2)
          and "ip address" not in cf0 and "ip address" not in cf2
          and "Error" not in cf0 and "Error" not in cf2
          and "interface GigabitEthernet0/0/1" in l0 and "interface GigabitEthernet0/0/0" in l2)
    return ok


async def run() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
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
            await c.cmd("Emulation.setDeviceMetricsOverride", width=1600, height=900,
                        deviceScaleFactor=1, mobile=False)
            await c.cmd("Page.navigate", url=f"{BASE}/login")
            await asyncio.sleep(1.5)
            st = await c.ev("fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                            "body:JSON.stringify({login:'admin',password:'admin'})}).then(r=>r.status)")
            print("login status:", st)
            # 课程页选好课题后进操作页（?s=课题 &mode=practice）：右侧显示本题的「开始练习」卡片
            await c.cmd("Page.navigate", url=f"{BASE}/?mode=practice&s={SID}")
            await c.wait_for("window.__ainet && window.__ainet.state.topo && "
                             "document.querySelectorAll('#topo-svg .node').length > 0", 15)
            await asyncio.sleep(1)

            # 1. 本题卡片：Campus 场景就绪；OSPF 场景未就绪并给出原因（课程页只在操作页显示本题，其他题看接口）
            await c.wait_for(f"document.querySelector('.pr-card[data-id=\"{SID}\"] .pr-state')", 10)
            cards = await c.ev("[...document.querySelectorAll('.pr-card')].map(e => [e.dataset.id, "
                               "e.querySelector('.t').textContent, e.querySelector('.pr-state').textContent, "
                               "e.querySelector('.pr-start').disabled])")
            for cd in cards:
                print("  card:", cd)
            by = {cd[0]: cd for cd in cards}
            api_by = {x["id"]: x for x in (await c.ev("fetch('api/scenarios?all=1').then(r => r.json())"))["scenarios"]}
            await shot(c, "01_scenarios.png")
            results.append(step(1, "本题卡片显示 Campus 场景「就绪」，OSPF 场景未就绪并给出原因",
                                by[SID][2] == "● 就绪" and by[SID][3] is False
                                and "请在 eNSP 打开 2-1Single-Area OSPF 并启动 R1、R2、R3" in api_by["ospf-2-1-r1-shutdown"]["reason"]))

            # 开练前先打开 AR0、AR2 的终端（验证注入期间学员终端看不到命令）
            await c.click_node("AR0")
            await c.wait_for(f"{PROMPT_JS}(({BUF_JS})({AR0}) || '')", 20)
            await c.click_node("AR2")
            await c.wait_for(f"{PROMPT_JS}(({BUF_JS})({AR2}) || '')", 20)
            print("  AR0 缓冲区:", repr((await buf(c, AR0)).strip()[-80:]))
            print("  AR2 缓冲区:", repr((await buf(c, AR2)).strip()[-80:]))
            pre0, pre2 = await buf(c, AR0), await buf(c, AR2)

            # 2. 开始练习
            t_start = time.time()
            await c.ev(f"document.querySelector('.pr-card[data-id=\"{SID}\"] .pr-start').click()")
            ok_active = await c.wait_for("(!document.getElementById('pr-active-view').hidden && "
                                         "window.__ainet.state.practice && window.__ainet.state.practice.active)"
                                         " || document.querySelector('.pr-start-err')", 60)
            ok_active = ok_active and not await c.ev("!!document.querySelector('.pr-start-err')")
            print(f"  开始耗时 {time.time() - t_start:.1f}s")
            st = await c.ev(STATE_JS)
            print("  state:", {k: st.get(k) for k in ("phase", "devices", "last_check", "hints_used")})
            err_txt = await c.ev("[...document.querySelectorAll('.pr-start-err')].map(e => e.textContent)")
            if err_txt:
                print("  开始失败：", err_txt)
            await asyncio.sleep(1.5)
            b0, b2 = await buf(c, AR0), await buf(c, AR2)
            leak = leaked(b0, AR0) + leaked(b2, AR2)
            print(f"  开始后 AR0 缓冲区: {b0.strip()!r}")
            print(f"  开始后 AR2 缓冲区: {b2.strip()!r}")
            print(f"  开始后 AR0 新增: {b0[len(pre0.rstrip()):]!r}")
            print(f"  开始后 AR2 新增: {b2[len(pre2.rstrip()):]!r}")
            print(f"  泄漏字样: {leak or '无'}")
            symptom = await c.ev("document.getElementById('pr-symptom').textContent")
            # 任务条上只显示「通过 x/y」，详细文字在右侧「工单详情」（2026-10 布局调整）
            check_txt = await c.ev("document.getElementById('pr-check-detail').textContent")
            print(f"  工单: {symptom[:40]}…  检查结果: {check_txt}")
            await shot(c, "02_started.png")
            results.append(step(2, "开始练习：注入完成并确认故障生效（0/1），学员终端里看不到注入 / 判定命令",
                                bool(ok_active) and st.get("phase") == "running"
                                and (st.get("last_check") or {}).get("passed") == 0
                                and st["last_check"]["total"] == 1 and not leak and "通过 0 / 1 项" in check_txt))
            if not results[-1]:
                print("SUMMARY: 开始练习失败，中止")
                return 1

            # 2a. 练习期间不开放的设备（LSW0 在运行）不能打开终端
            lsw = await c.ev("(() => { const g = [...document.querySelectorAll('#topo-svg .node')]"
                             ".find(n => n.dataset.name === 'LSW0'); return g ? g.getAttribute('class') : null; })()")
            opts = await c.ev("[...document.getElementById('open-select').options].map(o => o.textContent)")
            print(f"  LSW0 节点 class={lsw!r}  打开终端下拉={opts}")
            results.append(step("2a", "练习期间不开放的 LSW0 在拓扑上不可点、不在「打开终端」下拉中",
                                lsw is not None and "online" in lsw and "clickable" not in lsw
                                and not any("LSW0" in o for o in opts) and any("AR2" in o for o in opts)))

            # 2b. 学员先在 AR0 上看本端：触发一次自动判定，判定后仍看不到判定命令
            out = await type_cmd(c, AR0, "display ip interface brief")
            st, ok_auto = await wait_state(c, "s.last_check && s.last_check.source === 'auto'", 30)
            print("  首次自动判定:", st["last_check"])
            await asyncio.sleep(1)
            b0, b2 = await buf(c, AR0), await buf(c, AR2)
            ar0_rows = [l for l in b0.split("\n") if l.startswith("GigabitEthernet0/0/1")]
            leak = leaked(b0, AR0) + leaked(b2, AR2)
            print(f"  AR0 本端接口: {ar0_rows[-1:] }  泄漏字样: {leak or '无'}")
            results.append(step("2b", "baseline 生效（AR0 GE0/0/1 = 10.0.12.1/24）；自动判定执行后学员终端仍无判定命令",
                                ok_auto and st["last_check"]["passed"] == 0 and not leak
                                and ar0_rows and "10.0.12.1/24" in ar0_rows[-1]))

            # 3. 学员在 AR2 上排查并修复（全部经页面终端）
            await type_cmd(c, AR2, "display ip interface brief")
            b2 = await buf(c, AR2)
            ar2_rows = [l for l in b2.split("\n") if l.startswith("GigabitEthernet0/0/0")]
            print(f"  AR2 互联接口（注入后）: {ar2_rows[-1:]}")
            await type_cmd(c, AR2, "system-view")
            await type_cmd(c, AR2, "interface GigabitEthernet0/0/0")
            await type_cmd(c, AR2, "ip address 10.0.12.2 24")
            t_fix = time.time()
            await type_cmd(c, AR2, "return")
            print("  AR2 修复后缓冲区末尾:", [l for l in (await buf(c, AR2)).split("\n") if l.strip()][-5:])
            results.append(step(3, "inject 生效（AR2 GE0/0/0 = 10.0.21.2/24），学员经页面终端完成修复",
                                bool(ar2_rows) and "10.0.21.2/24" in ar2_rows[-1]))

            # 4. 自动判定通关
            st, ok_pass = await wait_state(c, "s.passed === true", 40)
            dt = time.time() - t_fix
            # 页面每 2 秒轮询一次：等通关横幅出现（也保证随后点「结束练习」不会弹放弃确认框）
            await c.wait_for("!document.getElementById('pr-banner').hidden && "
                             "window.__ainet.state.practice && window.__ainet.state.practice.passed", 8)
            dt_page = time.time() - t_fix
            banner = await c.ev("document.getElementById('pr-banner').hidden ? '' : document.getElementById('pr-banner').textContent")
            print(f"  修复命令到后端判定通关 {dt:.1f}s，到页面出现横幅 {dt_page:.1f}s；last_check={st['last_check']}；横幅={banner!r}")
            await asyncio.sleep(0.5)
            await shot(c, "03_passed.png")
            results.append(step(4, "系统约 3~10 秒内自动判定通关，页面出现通关提示",
                                ok_pass and st["last_check"]["source"] == "auto" and "通关" in banner and dt <= 15))

            # 5. 结束练习：restore，并经终端用 display 核对设备
            await c.ev("document.getElementById('pr-end').click()")
            ok_sum = await c.wait_for("!document.getElementById('pr-summary-view').hidden", 60)
            summary = await c.ev("document.getElementById('pr-summary').innerText")
            print("  总结:", summary.replace("\n", " | "))
            await shot(c, "04_summary.png")
            clean_ok = await verify_clean(c, results, "通关后")
            await shot(c, "05_verify_restore.png")
            results.append(step(5, "结束练习：restore + 整机复位成功，设备回到开练前状态（互联接口无 IP、无遗留配置）",
                                bool(ok_sum) and "通关" in summary and "已复位到开练前配置" in summary and clean_ok))

            # 5b. 通关那次自动判定（隐藏 ping）和结束时的 restore 也没有进学员终端（学员从没敲过这些字样）
            b0, b2 = await buf(c, AR0), await buf(c, AR2)
            hidden_words = ["ping -c 2", "packet(s)", "undo ip"]
            leak = [w for w in hidden_words if w in b0] + [w for w in hidden_words if w in b2]
            print(f"  通关路径全程泄漏字样: {leak or '无'}")
            results.append(step("5b", "通关路径全程（含通关判定与 restore）学员终端里没有判定 / 还原命令", not leak))

            # 6. 档案
            me = await c.ev("fetch('api/me').then(r => r.json())")
            rec = me["recent_attempts"][0]
            print("  档案最近记录:", {k: rec[k] for k in ("scenario_title", "mode", "passed", "hints_used", "commands",
                                                       "started_at", "ended_at")})
            results.append(step(6, "档案接口能看到这次练习，passed=1",
                                rec["passed"] == 1 and rec["scenario_key"] == SID and rec["mode"] == "practice"))

            # 放弃路径：总结页「再练一次」（同一道题重新开始），不修复直接结束
            p0, p2 = await buf(c, AR0), await buf(c, AR2)
            await c.ev("document.getElementById('pr-back').click()")
            ok_a2 = await c.wait_for("window.__ainet.state.practice && window.__ainet.state.practice.active", 60)
            st = await c.ev(STATE_JS)
            print("  放弃路径 state:", {k: st.get(k) for k in ("phase", "last_check")})
            # 关掉 AR2 标签再用练习面板的设备按钮重开：回放里不应有上一次练习的命令和答案
            await c.ev(f"window.__ainet.closeTab({AR2}), 0")
            await asyncio.sleep(0.5)
            await c.ev(f"document.querySelector('.pr-dev[data-port=\"{AR2}\"]').click()")
            await c.wait_for(f"{PROMPT_JS}(({BUF_JS})({AR2}) || '')", 20)
            reopened = await buf(c, AR2)
            stale = [w for w in ("ip address", "10.0.21.2", "10.0.12.2", "system-view") if w in reopened]
            print(f"  重开 AR2 标签的缓冲区: {reopened.strip()!r}  上一次练习的残留: {stale or '无'}")
            await c.ev("window.confirm = () => true")
            await c.ev("document.getElementById('pr-end').click()")
            ok_sum2 = await c.wait_for("!document.getElementById('pr-summary-view').hidden", 60)
            summary2 = await c.ev("document.getElementById('pr-summary').innerText")
            print("  放弃总结:", summary2.replace("\n", " | "))
            b0, b2 = await buf(c, AR0), await buf(c, AR2)
            leak = leaked(b0, AR0) + leaked(b2, AR2)
            print(f"  放弃路径泄漏字样: {leak or '无'}")
            await shot(c, "06_giveup_summary.png")
            clean2 = await verify_clean(c, results, "放弃后")
            me = await c.ev("fetch('api/me').then(r => r.json())")
            rec2 = me["recent_attempts"][0]
            print("  档案最近记录:", {k: rec2[k] for k in ("scenario_title", "passed", "commands")},
                  " 错题本:", [(w["scenario_title"], w["fail_count"]) for w in me["wrong_scenarios"]])
            await shot(c, "07_giveup_verify.png")
            results.append(step("G", "放弃路径：开始后不修复直接结束，restore 还原干净，档案记 passed=0",
                                bool(ok_a2) and st["phase"] == "running" and bool(ok_sum2) and "未通关" in summary2
                                and clean2 and rec2["passed"] == 0 and not leak and not stale))

            # 7. JS 异常
            print("errors total:", c.errors or "无")
            results.append(step(7, "全程无 JS 异常", not c.errors))
            reader.cancel()
            print(f"SUMMARY: {sum(results)} pass / {len(results) - sum(results)} fail")
            return 0 if all(results) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
