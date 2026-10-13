"""eNSP Pro 沙箱 ainet_campus 的真机 + 无头浏览器 e2e：YunShan 版题库的练习与考试（不进默认 pytest）。

前置：eNSP Pro 沙箱 ainet_campus（spec §10.4）的 LSW0 / AR0 / AR1 / AR2 / PC1 / R1~R3 已由 ainet 启动；服务以
AINET_LAB_PROVIDER=enspro AINET_ENSPRO_SANDBOX=<沙箱 id>、临时 AINET_DB_PATH 起好（建议 AINET_AI_SCORING=0、AINET_AI_HINTS=0），
并已用 POST /api/lab/baseline 记好各设备的干净基线（或由第一次开题自动记录）。
eNSP Pro 同一账号只能有一个会话：跑脚本期间不要在别处登录 eNSP Pro，也不要另开脚本调它的接口。

用法：python tests/e2e/enspro_campus_e2e.py practice <场景 id> [base_url] [截图目录]
      python tests/e2e/enspro_campus_e2e.py practice-extra <场景 id> [base_url] [截图目录]
      python tests/e2e/enspro_campus_e2e.py exam [base_url] [截图目录]
      python tests/e2e/enspro_campus_e2e.py session [base_url] [截图目录]
- practice：练习页签开始（种子由服务随机取）→ 学员从练习页签的设备按钮打开终端，按参数排查并修复 → 自动判定通关
  → 结束练习 → 复位与开练前快照一致（reset_ok）；开始时打印与干净基线的比对结果（clean / reset / captured）。
- practice-extra：同上，另在 AR0 / AR2 / LSW0 上做场景之外的改动（交换机 sysname、VLAN、trunk、Vlanif、shutdown、stp、
  静态路由；路由器 LoopBack、接口描述与 shutdown、ACL、黑洞路由），结束时由整机复位撤销（只用于 AR0、AR2、LSW0 都是
  本题设备的场景，如 campus-ar2-acl-block-ys）。
- exam：经接口建 3 题模板（固定种子）→ 考试页签开考 → 第 1 题修复后提交通过 → 下一题 → 第 2 题先提交未通过、修复后
  重试通过 → 下一题 → 第 3 题修复后提交通过 → 交卷（二次确认）→ 成绩单 3 题都通过、各题复位成功。
- session：脚本另开一个 eNSP Pro 登录会话把 ainet 挤掉 → 页面状态栏提示、出现「重新登录 eNSP Pro」→ 缺省不自动重登
  → 点按钮重新登录（别处的会话被挤掉）→ AR1 终端恢复。
全程无 JS 异常。
题库扩充题（docs/question-bank-plan.md，2026-10-07）：设备号按当前拓扑取（ainet_test 的 T1 / T2 题也用本脚本，先经
POST /api/lab/prepare 切到 ainet_test）；开始后按 FAULT_WAIT 等（OSPF / DHCP 90 秒）、看一眼现象，再判一次应不通过（2c）。
AINET_E2E_REQUIRE_BASELINE=1：开练时每台本题设备都要与干净基线比对过（clean / reset，2b）；AINET_E2E_LOG=<文件>：记下学员每条
命令的完整回显；AINET_E2E_FAULT_WAIT=<秒> 覆盖等待时间。
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
from browser_e2e import CDP, CHROME  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "practice"
ARGS = sys.argv[2:]
SID = ARGS.pop(0) if MODE.startswith("practice") else None
BASE = ARGS[0] if len(ARGS) > 0 else "http://127.0.0.1:8796"
SHOTS = ARGS[1] if len(ARGS) > 1 else "."
DEBUG_PORT = int(os.environ.get("AINET_E2E_DEBUG_PORT", "9337"))
LOG = os.environ.get("AINET_E2E_LOG")      # 设了就把学员每条命令的完整回显追加到这个文件（核对真机输出格式用）
DEV = {"LSW0": 8, "AR0": 9, "AR2": 10, "AR1": 11, "PC1": 12, "R1": 14, "R2": 15, "R3": 16}   # 练习开始时按当前拓扑刷新
# 注入后先等这么多秒再确认故障（OSPF / DHCP 题注入时邻居 / 地址本来就还没有，开始时的故障确认证明不了注入生效）
FAULT_WAIT = {"ospf-r3-area-mismatch-ys": 90, "ospf-r2-hello-mismatch-ys": 90, "campus-ar0-dhcp-select-ys": 90,
              "campus-ar0-dhcp-exhausted-ys": 90}
# 修复后等判定时，学员隔一会儿再看一眼（触发自动判定）：邻居 / DHCP 地址要几十秒才出来
RETRIGGER = {"ospf-r1-shutdown-ys": ("R1", "display ospf peer brief"),
             "ospf-r3-area-mismatch-ys": ("R1", "display ospf peer brief"),
             "ospf-r2-hello-mismatch-ys": ("R1", "display ospf peer brief"),
             "campus-ar0-dhcp-select-ys": ("R1", "display ip interface brief"),
             "campus-ar0-dhcp-exhausted-ys": ("R1", "display ip interface brief"),
             # AR6700 改完 NAT 规则后紧接着的 ping 可能还不通（几秒后生效）
             "campus-ar2-easyip-acl-ys": ("LSW0", "ping -c 2 203.0.113.1"),
             "campus-ar2-nat-static-ys": ("AR2", "display nat server")}
PROMPT = r"(<[^<>\s]+>|\[[^\[\]\s]+\]|PC>)\s*$"
BUF_JS = """(port => { const t = window.__ainet.state.tabs.get(port); if (!t) return '';
  const b = t.term.buffer.active; let s = '';
  for (let k = 0; k < b.length; k++) { const l = b.getLine(k);
    s += (k && !l.isWrapped ? '\\n' : '') + l.translateToString(true); }
  return s; })"""
EXAM_QUESTIONS = [("campus-ar0-ar2-link-ys", 777), ("campus-ar2-return-route-ys", 4242),
                  ("campus-lsw0-access-vlan-ys", 9)]


def fixes(sid: str, pv: dict) -> list[tuple[str, list[str]]]:
    """学员的排查与修复（按参数取值）。最后一条通常是验证命令（触发自动判定）。"""
    if sid == "campus-ar0-ar2-link-ys":
        link = pv["link"]
        return [("AR0", [f"ping -c 2 10.0.{link}.2", "display ip interface brief"]),
                ("AR2", ["display ip interface brief", "system-view", "interface GE0/0/0",
                         f"ip address 10.0.{link}.2 24", "return"]),
                ("AR0", [f"ping -c 2 10.0.{link}.2"])]
    if sid == "campus-lsw0-access-vlan-ys":
        return [("AR0", ["ping -c 2 10.0.10.254"]), ("PC1", ["ping -c 2 10.0.10.1"]),
                ("LSW0", ["display ip interface brief", "display port vlan", "system-view", "interface GE1/0/1",
                          "port default vlan 10", "return"]),
                ("AR0", ["ping -c 2 10.0.10.254"]), ("PC1", ["ping -c 2 10.0.10.1"])]
    if sid == "campus-ar2-return-route-ys":
        hop = pv["bad_hop"]
        return [("LSW0", ["ping -c 2 192.168.2.1"]), ("AR0", ["ping -c 2 192.168.2.1"]),
                ("AR2", ["display ip routing-table 10.0.10.1", "system-view",
                         f"undo ip route-static 10.0.10.0 24 10.0.12.{hop}", "ip route-static 10.0.10.0 24 10.0.12.1",
                         "return"]),
                ("LSW0", ["ping -c 2 192.168.2.1"])]
    if sid == "campus-ar2-acl-block-ys":
        acl, rule = pv["acl"], pv["rule"]
        return [("LSW0", ["ping -c 2 192.168.2.1"]),
                ("AR2", ["display acl all", "display current-configuration interface GE0/0/0", "system-view",
                         f"acl number {acl}", f"undo rule {rule}",
                         f"rule {rule} deny ip source 10.0.20.0 0.0.0.255 destination 192.168.2.0 0.0.0.255", "return"]),
                ("LSW0", ["ping -c 2 192.168.2.1"])]
    if sid == "ospf-r1-shutdown-ys":
        return [("R1", ["display ospf peer brief", "display ip interface brief", "system-view", "interface GE0/0/0",
                        "undo shutdown", "return", "display ospf peer brief"])]
    # ---- 题库扩充草稿题（docs/question-bank-plan.md，2026-10-07 真机验证） ----
    host = "@host "     # PC 设置气泡（经 api/lab/devices/{console}/host，与页面气泡同一接口）
    if sid == "campus-lsw0-port-shutdown-ys":
        return [("AR0", ["ping -c 2 10.0.10.254", "display ip interface brief"]), ("PC1", ["ping -c 2 10.0.10.1"]),
                ("LSW0", ["display interface brief", "system-view", "interface GE1/0/1", "undo shutdown", "return"]),
                ("AR0", ["ping -c 2 10.0.10.254"]), ("PC1", ["ping -c 2 10.0.10.1"])]
    if sid == "campus-pc1-gateway-ys":
        return [("PC1", ["ping -c 2 10.0.10.1", "ping -c 2 192.168.2.1", "ifconfig"]),
                ("PC1", [host + json.dumps({"ip": "10.0.10.10", "mask": "255.255.255.0", "gateway": "10.0.10.1"})]),
                ("PC1", ["ping -c 2 192.168.2.1"])]
    if sid == "campus-ar0-dot1q-trunk-ys":
        return [("PC1", ["ping -c 2 10.0.20.1"]), ("AR0", ["display ip interface brief"]),
                ("LSW0", ["display port vlan", "system-view", "interface GE1/0/1", "port trunk allow-pass vlan 20",
                          "return"]),
                ("PC1", ["ping -c 2 10.0.20.1", "ping -c 2 10.0.10.254"])]
    if sid == "ospf-r3-area-mismatch-ys":
        return [("R1", ["display ospf peer brief"]),
                ("R3", ["display ospf peer brief", "display current-configuration configuration ospf", "system-view",
                        "ospf 1", "area 1", "undo network 10.0.123.0 0.0.0.255", "return", "system-view", "ospf 1",
                        "area 0", "network 10.0.123.0 0.0.0.255", "return"]),
                ("R1", ["display ospf peer brief"])]
    if sid == "ospf-r2-hello-mismatch-ys":
        return [("R1", ["display ospf peer brief"]),
                ("R2", ["display ospf interface GE0/0/0", "system-view", "interface GE0/0/0", "undo ospf timer hello",
                        "return"]),
                ("R1", ["display ospf peer brief"])]
    if sid == "campus-ar2-acl-telnet-ys":
        return [("LSW0", ["ping -c 2 192.168.2.1"]),
                ("AR2", ["display acl all", "system-view", "acl number 3001", "undo rule 5",
                         "rule 5 deny tcp source 10.0.10.0 0.0.0.255 destination 192.168.2.0 0.0.0.255 "
                         "destination-port eq telnet", "return", "display acl all"]),
                ("LSW0", ["ping -c 2 192.168.2.1"])]
    if sid == "campus-ar0-dhcp-select-ys":
        return [("R1", ["display ip interface brief"]),
                ("AR0", ["display ip pool name campus", "display current-configuration interface GE0/0/0",
                         "system-view", "interface GE0/0/0", "dhcp select global", "return"]),
                ("R1", ["display ip interface brief"])]
    if sid == "campus-ar0-dhcp-exhausted-ys":
        return [("R1", ["display ip interface brief"]),
                ("AR0", ["display ip pool name campus", "system-view", "ip pool campus",
                         "undo excluded-ip-address 10.0.10.10 10.0.10.254", "return"]),
                ("R1", ["display ip interface brief"])]
    if sid == "campus-r1-dns-host-ys":
        return [("R1", ["ping -c 2 files.ainet.lab", "display ip host", "ping -c 2 10.0.123.2", "system-view",
                        "undo ip host files.ainet.lab 10.0.123.20", "ip host files.ainet.lab 10.0.123.2", "return",
                        "ping -c 2 files.ainet.lab"])]
    if sid == "campus-ar2-easyip-acl-ys":
        return [("LSW0", ["ping -c 2 203.0.113.1"]),
                ("AR2", ["display nat-policy rule all", "display nat-policy rule name easyip", "system-view",
                         "nat-policy", "rule name easyip", "source-address 10.0.10.0 24",
                         "undo source-address 10.0.20.0 24", "return"]),
                ("LSW0", ["ping -c 2 203.0.113.1"])]
    if sid == "campus-ar2-nat-static-ys":     # AR6700：接口下没有 nat static，用 nat server（2026-10-07 实测）
        return [("AR2", ["display nat server", "system-view", "undo nat server name mgmt",
                         "nat server mgmt global 198.51.100.10 inside 10.0.10.254", "return", "display nat server"])]
    if sid == "campus-mgmt-two-faults-ys":
        return [("LSW0", ["ping -c 2 10.0.10.1", "display port vlan", "system-view", "interface GE1/0/1",
                          "port default vlan 10", "return", "ping -c 2 10.0.10.1", "ping -c 2 192.168.2.1"]),
                ("AR2", ["display ip routing-table 10.0.10.1", "system-view",
                         "undo ip route-static 10.0.10.0 24 10.0.12.3", "ip route-static 10.0.10.0 24 10.0.12.1",
                         "return"]),
                ("LSW0", ["ping -c 2 192.168.2.1"])]
    # ainet_test（T1 / T2）
    if sid == "enspro-t2-port-shutdown":
        return [("T1", ["ping -c 2 10.0.12.2", "display ip interface brief"]),
                ("T2", ["display ip interface brief", "system-view", "interface GE0/0/1", "undo shutdown", "return"]),
                ("T1", ["ping -c 2 10.0.12.2"])]
    if sid == "enspro-t2-ip-wrong-port":
        return [("T1", ["ping -c 2 10.0.12.2"]),
                ("T2", ["display ip interface brief", "system-view", "interface GE0/0/2", "undo ip address",
                        "interface GE0/0/1", "ip address 10.0.12.2 24", "return"]),
                ("T1", ["ping -c 2 10.0.12.2"])]
    if sid == "enspro-t2-mask-mismatch":
        return [("T1", ["ping -c 2 10.0.12.130"]),
                ("T2", ["display ip interface brief", "system-view", "interface GE0/0/1", "ip address 10.0.12.130 24",
                        "return"]),
                ("T1", ["ping -c 2 10.0.12.130"])]
    if sid == "enspro-t1-static-route-dest":
        return [("T1", ["ping -c 2 -a 1.1.1.1 2.2.2.2", "display ip routing-table", "system-view",
                        "undo ip route-static 22.2.2.2 32 10.0.12.2", "ip route-static 2.2.2.2 32 10.0.12.2", "return",
                        "ping -c 2 -a 1.1.1.1 2.2.2.2"])]
    if sid == "enspro-t1-t2-two-faults":
        return [("T1", ["ping -c 2 10.0.12.2"]),
                ("T2", ["display ip interface brief", "system-view", "interface GE0/0/1", "ip address 10.0.12.2 24",
                        "return"]),
                ("T1", ["ping -c 2 10.0.12.2", "display ip routing-table 2.2.2.2", "system-view",
                        "undo ip route-static 2.2.2.2 32 10.0.12.3", "ip route-static 2.2.2.2 32 10.0.12.2", "return",
                        "ping -c 2 -a 1.1.1.1 2.2.2.2"])]
    raise KeyError(sid)


def evidence(sid: str) -> list[tuple[str, list[str]]]:
    """等待 FAULT_WAIT 之后、修复之前看一眼故障现象（只读命令，佐证故障真的生效）。"""
    return {
        "ospf-r3-area-mismatch-ys": [("R1", ["display ospf peer brief"]), ("R3", ["display ospf peer brief"])],
        "ospf-r2-hello-mismatch-ys": [("R1", ["display ospf peer brief"]), ("R2", ["display ospf peer brief"])],
        "campus-ar0-dhcp-select-ys": [("R1", ["display ip interface brief"])],
        "campus-ar0-dhcp-exhausted-ys": [("R1", ["display ip interface brief"])],
    }.get(sid, [])


EXTRA = [   # 场景之外的改动：只能靠整机复位撤销
    ("LSW0", ["system-view", "sysname SWX", "vlan batch 77 88", "interface GE1/0/6", "port link-type trunk",
              "port trunk allow-pass vlan 77 88", "quit", "interface GE1/0/7", "port link-type access",
              "port default vlan 77", "shutdown", "quit", "interface Vlanif77", "ip address 10.77.0.1 24", "quit",
              "stp mode rstp", "ip route-static 10.99.0.0 16 10.77.0.254", "return"]),
    ("AR2", ["system-view", "interface LoopBack7", "ip address 10.7.7.7 32", "quit", "interface GE0/0/5",
             "description ainet-extra", "shutdown", "return"]),
    ("AR0", ["system-view", "acl number 3999", "rule 5 permit ip", "quit", "ip route-static 10.8.0.0 16 NULL0",
             "return"]),
]
EXTRA_KEYS = {"LSW0": ["SWX", "vlan batch 10 77 88", "Vlanif77", "stp mode rstp", "10.99.0.0"],
              "AR2": ["LoopBack7", "ainet-extra"], "AR0": ["acl number 3999", "10.8.0.0"]}


def step(res: list, n, text: str, ok, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {n}. {text}" + (f"  —— {detail}" if detail else ""), flush=True)
    res.append(bool(ok))


async def shot(c, name: str, full: bool = False) -> None:
    r = await c.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=full)
    path = os.path.join(SHOTS, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    print("  截图:", path, flush=True)


async def buf(c, port: int) -> str:
    return await c.ev(f"({BUF_JS})({port})") or ""


async def type_cmd(c, port: int, cmd: str, timeout: float = 60) -> str:
    """像学员一样在页面终端里敲一条命令（经该标签的 WebSocket 发按键），读到提示符为止；分页按空格。"""
    lines0 = (await buf(c, port)).rstrip("\n").split("\n")
    n0 = len(lines0) - 1
    await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode({json.dumps(cmd + chr(13))}))")
    end = time.time() + timeout
    sent_at = -1
    out = ""
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
    print(f"  ！命令没等到提示符：[{port}] {cmd!r} 末尾 {out[-200:]!r}", flush=True)
    return out


async def open_dev(c, sel: str, port: int) -> bool:
    await c.ev(f"document.querySelector('{sel}[data-port=\"{port}\"]').click()")
    ok = await c.wait_for(f"(t => t && t.ws && t.ws.readyState === 1)(window.__ainet.state.tabs.get({port}))", 20)
    end = time.time() + 40
    while time.time() < end:                       # 等终端里出现提示符（console 首次登录可能要几秒）
        if re.search(PROMPT, (await buf(c, port)).rstrip()):
            return bool(ok)
        await asyncio.sleep(0.5)
    return False


async def run_student(c, sel: str, steps: list[tuple[str, list[str]]], opened: set[int]) -> dict[str, str]:
    outs: dict[str, str] = {}
    for dev, cmds in steps:
        port = DEV[dev]
        if port not in opened and not all(c0.startswith("@host ") for c0 in cmds):
            if not await open_dev(c, sel, port):
                print(f"  ！打不开 {dev} 的终端", flush=True)
            opened.add(port)
        for cmd in cmds:
            if cmd.startswith("@host "):      # PC 设置气泡「应用」（同一接口），等 PC 命令行恢复后返回
                r = await c.ev(f"fetch('api/lab/devices/{port}/host',{{method:'POST',headers:{{'Content-Type':"
                               f"'application/json'}},body:JSON.stringify({cmd[6:]})}}).then(r => r.json())")
                print(f"  [{dev}] PC 设置 {cmd[6:]} → {r}", flush=True)
                continue
            out = await type_cmd(c, port, cmd)
            outs[dev] = outs.get(dev, "") + out
            if LOG:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n===== [{dev}] {cmd}\n{out}\n")
            keep = [l.strip() for l in out.split("\n") if re.search(r"received|Full|Error|PVID|deny|\^", l)]
            if keep:
                print(f"  [{dev}] {cmd} → {keep[:3]}", flush=True)
    return outs


async def practice(c) -> list[bool]:
    res: list[bool] = []
    extra = MODE == "practice-extra"
    pre = f"ys_{SID.replace('campus-', '').replace('-ys', '')}{'_x' if extra else ''}_"
    # 课程页选题后进操作页（?mode=practice&s=课题）：右侧是本题的「开始练习」卡片（2026-10-07）
    await c.cmd("Page.navigate", url=f"{BASE}/?mode=practice&s={SID}")
    await c.wait_for("window.__ainet && window.__ainet.state.topo", 20)
    await c.ev("window.confirm = () => true")
    card = f"document.querySelector('.pr-card[data-id=\"{SID}\"]')"
    ready = await c.wait_for(f"{card} && !{card}.querySelector('.pr-start').disabled", 20)
    title = await c.ev(f"{card} && {card}.querySelector('.t').textContent")
    step(res, 1, f"练习列表里「{title}」就绪（可开始）", ready)
    await shot(c, pre + "01_list.png")
    t0 = time.time()
    await c.ev(f"{card}.querySelector('.pr-start').click()")
    ok = await c.wait_for("!document.getElementById('pr-active-view').hidden", 180)
    st = await c.ev("fetch('api/practice/current').then(r => r.json())")
    pv = st.get("param_values") or {}
    print(f"  开始用时 {time.time() - t0:.1f}s  种子 {st.get('seed')} 参数 {pv}  故障确认 {st.get('last_check')}  "
          f"与干净基线比对 {st.get('pre_reset')}", flush=True)
    step(res, 2, "开始练习：基线比对、快照、baseline、注入、确认故障生效",
         ok and st.get("phase") == "running" and st["last_check"]["all"] is False)
    await shot(c, pre + "02_started.png")
    topo = await c.ev("fetch('api/topology/current').then(r => r.json())")
    DEV.update({d["name"]: d["console"] for d in topo["topology"]["devices"] if d.get("console")})
    if os.environ.get("AINET_E2E_REQUIRE_BASELINE") == "1":     # 开练前与手动记录的干净基线比对过（不是自动记录）
        pr = st.get("pre_reset") or {}
        step(res, "2b", "开练前每台本题设备都与干净基线比对过（clean / reset）",
             all((pr.get(d) or {}).get("status") in ("clean", "reset") for d in st["scenario"]["required_devices"]),
             json.dumps(pr, ensure_ascii=False))

    opened: set[int] = set()
    wait = int(os.environ.get("AINET_E2E_FAULT_WAIT", FAULT_WAIT.get(SID, 0)))
    if wait:                                   # 等协议收敛后再确认故障仍在（并留下现象的回显）
        print(f"  等 {wait}s 后再确认故障……", flush=True)
        await asyncio.sleep(wait)
        await run_student(c, ".pr-dev", evidence(SID), opened)
    chk = await c.ev("fetch('api/practice/check',{method:'POST'}).then(r => r.json()).then(s => s.check)")
    print("  修复前判定:", chk, flush=True)
    step(res, "2c", f"修复前（注入后 {wait}s）判定仍不通过", chk and chk.get("status") == "ok" and chk.get("all") is False,
         str(chk))
    await run_student(c, ".pr-dev", fixes(SID, pv), opened)
    await c.ev("window.__ainet.setSplit && window.__ainet.setSplit(true)")
    if extra:
        await run_student(c, ".pr-dev", EXTRA, opened)
        cfgs = {}
        for dev in EXTRA_KEYS:
            prompt = "<SWX>" if dev == "LSW0" else "<HUAWEI>"
            cfgs[dev] = await type_cmd(c, DEV[dev], "display current-configuration", timeout=90)
        seen = {d: [k for k in ks if k in cfgs[d]] for d, ks in EXTRA_KEYS.items()}
        print("  额外改动已生效：", seen, flush=True)
        step(res, "2x", "学员在 AR0、AR2、LSW0 上做了场景之外的改动（含交换机 sysname / VLAN / trunk / Vlanif）",
             all(len(seen[d]) == len(EXTRA_KEYS[d]) for d in EXTRA_KEYS), str(seen))
    passed = None
    t_fix = time.time()
    for i in range(60):
        passed = await c.ev("fetch('api/practice/current').then(r => r.json()).then(s => s.passed)")
        if passed:
            break
        if SID in RETRIGGER and i % 4 == 3:    # 邻居 / DHCP 地址几十秒后才出来：学员再看一眼（触发判定）
            dev, cmd = RETRIGGER[SID]
            out = await type_cmd(c, DEV[dev], cmd)
            if LOG:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n===== [{dev}] {cmd}（修复后 {time.time() - t_fix:.0f}s）\n{out}\n")
        await asyncio.sleep(3)
    print(f"  修复后 {time.time() - t_fix:.0f}s 判定通过={passed}", flush=True)
    st = await c.ev("fetch('api/practice/current').then(r => r.json())")
    print("  判定:", st.get("last_check"), " 命令数:", st.get("commands"), flush=True)
    step(res, 3, "学员修复后自动判定通关", passed, str(st.get("last_check")))
    await shot(c, pre + "03_passed.png")

    t0 = time.time()
    await c.ev("document.getElementById('pr-end').click()")
    ok = await c.wait_for("!document.getElementById('pr-summary-view').hidden", 300)
    last = await c.ev("fetch('api/practice/current').then(r => r.json()).then(s => s.last_result)")
    reset = (last or {}).get("reset") or {}
    print(f"  结束用时 {time.time() - t0:.1f}s  复位：" +
          json.dumps({d: (r["status"], r["rounds"], r["commands"]) for d, r in reset.items()}, ensure_ascii=False), flush=True)
    ok4 = bool(ok) and bool(last) and last.get("reset_ok") is True and last.get("restored") is True and last.get("passed")
    if extra:
        ok4 = ok4 and all(reset.get(d, {}).get("rounds", 0) >= 1 and reset[d].get("commands", 0) > 0 for d in EXTRA_KEYS)
        tail = (await buf(c, DEV["LSW0"])).rstrip().split("\n")[-1]
        print("  LSW0 终端最后一行（sysname 复原后重印提示符）：", tail, flush=True)
        ok4 = ok4 and tail.strip() == "<HUAWEI>"
    step(res, 4, "结束练习：restore + 整机复位，与开练前快照逐行一致（reset_ok）" + ("，额外改动由整机复位撤销" if extra else ""),
         ok4, json.dumps((last or {}).get("leftovers"), ensure_ascii=False))
    await shot(c, pre + "04_summary.png")
    return res


async def exam(c) -> list[bool]:
    res: list[bool] = []
    await c.wait_for("window.__ainet && window.__ainet.state.topo && window.__ainetExam", 20)
    cfg = {"questions": [{"scenario": sid, "seed": seed} for sid, seed in EXAM_QUESTIONS],
           "duration_s": 3600, "question_duration_s": None, "retries": 1,
           "hints": {"allowed": True, "penalty": 5, "max_level": 2},
           "show_topology": True, "auto_judge": False, "record_replay": True, "shuffle": False}
    name = f"eNSP Pro 真机考试 {time.strftime('%H%M%S')}"
    tid = await c.ev("fetch('api/exam/templates',{method:'POST',headers:{'Content-Type':'application/json'},"
                     f"body:JSON.stringify({{name:{json.dumps(name)},config:{json.dumps(cfg)}}})}}).then(r => r.json()).then(d => d.id)")
    # 多题考试在课程页章节列最下方「考试」里选模板，「开始考试」先按题目准备环境再开考（2026-10-07）
    t0 = time.time()
    from exam_e2e import start_from_courses
    listed = await start_from_courses(c, BASE, tid)
    await shot(c, "ys_exam_01_list.png")

    async def state():
        return await c.ev("fetch('api/exam/current').then(r => r.json())")

    async def wait(pred, timeout):
        end = time.time() + timeout
        st = None
        while time.time() < end:
            st = await state()
            try:
                if pred(st):
                    return st, True
            except (KeyError, TypeError, IndexError):
                pass
            await asyncio.sleep(1)
        return st, False

    st, started = await wait(lambda s: s["active"] and s["question"]["status"] == "active", 240)
    print(f"  开考用时 {time.time() - t0:.1f}s：第 {st['current'] + 1}/{st['total']} 题", flush=True)
    step(res, 1, f"考试页签列出模板（id={tid}）→ 开始考试，第 1 题注入完成",
         listed and started and st["total"] == 3 and st["current"] == 0)
    await shot(c, "ys_exam_02_q1.png")

    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", os.path.join(os.path.dirname(os.path.dirname(HERE)), "scenarios.py"))
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    import config as _cfg
    import scenarios as sc
    loaded, _ = sc.load_dir(_cfg.SCENARIOS_DIR, [])
    pvs = [sc.expand(loaded[sid], seed)["param_values"] for sid, seed in EXAM_QUESTIONS]
    print("  各题参数（按模板固定种子展开）：", pvs, flush=True)

    opened: set[int] = set()
    # 第 1 题：修复 → 提交通过
    await run_student(c, ".ex-dev", fixes(EXAM_QUESTIONS[0][0], pvs[0]), opened)
    await c.ev("document.getElementById('ex-submit').click()")
    st, p1 = await wait(lambda s: s["question"]["passed"], 60)
    await shot(c, "ys_exam_03_q1_passed.png")
    step(res, 2, "第 1 题（AR0–AR2 互联）修复后提交本题一次通过", p1 and len(st["question"]["submissions"]) == 1)

    # 第 2 题：先提交（未修）→ 未通过 → 修复 → 重试通过
    t0 = time.time()
    await c.ev("document.getElementById('ex-next').click()")
    st, ok2 = await wait(lambda s: s["current"] == 1 and s["question"]["status"] == "active", 300)
    print(f"  下一题用时 {time.time() - t0:.1f}s（上一题复位 + 本题注入）", flush=True)
    opened.clear()
    await c.ev("document.getElementById('ex-submit').click()")
    st, f2 = await wait(lambda s: len(s["question"]["submissions"]) == 1, 60)
    first = st["question"]["submissions"][0] if f2 else {}
    await run_student(c, ".ex-dev", fixes(EXAM_QUESTIONS[1][0], pvs[1]), opened)
    await c.ev("document.getElementById('ex-submit').click()")
    st, p2 = await wait(lambda s: s["question"]["passed"], 60)
    await shot(c, "ys_exam_04_q2_passed.png")
    step(res, 3, "第 2 题（回程路由）首次提交未通过 → 修复 → 重试通过",
         ok2 and f2 and not first.get("passed") and p2 and len(st["question"]["submissions"]) == 2)

    # 第 3 题：修复 → 提交通过（含 PC1 的判定）
    await c.ev("document.getElementById('ex-next').click()")
    st, ok3 = await wait(lambda s: s["current"] == 2 and s["question"]["status"] == "active", 300)
    opened.clear()
    await run_student(c, ".ex-dev", fixes(EXAM_QUESTIONS[2][0], pvs[2]), opened)
    await c.ev("document.getElementById('ex-submit').click()")
    st, p3 = await wait(lambda s: s["question"]["passed"], 60)
    await shot(c, "ys_exam_05_q3_passed.png")
    step(res, 4, "第 3 题（接入 VLAN + PC1）修复后提交通过", ok3 and p3)

    # 交卷（二次确认）
    t0 = time.time()
    await c.ev("document.getElementById('ex-finish').click()")
    confirm_shown = await c.wait_for("!document.getElementById('ex-confirm').hidden", 10)
    await c.ev("document.getElementById('ex-confirm-yes').click()")
    fin = await c.wait_for("!document.getElementById('ex-done-view').hidden", 300)
    eid = await c.ev("window.__ainetExam.ex.doneId")
    print(f"  交卷用时 {time.time() - t0:.1f}s（最后一题复位）", flush=True)
    step(res, 5, f"交卷（二次确认）→ 考试结束（考试 {eid}）", confirm_shown and fin and eid)
    await shot(c, "ys_exam_06_done.png")

    await c.cmd("Page.navigate", url=f"{BASE}/exam/report?id={eid}")
    await c.wait_for("document.querySelectorAll('.q').length === 3", 20)
    await c.wait_for("!document.getElementById('summary').textContent.includes('评分中')", 240)
    rep = await c.ev(f"fetch('../api/exam/{eid}/report').then(r => r.json())")
    qs = rep["questions"]
    summary = [(q["scenario"] if "scenario" in q else q.get("title"), q["status"], q.get("passed"), len(q["submissions"]),
                q.get("score_total"), q.get("reset_ok")) for q in qs]
    print(f"  成绩单：总分 {rep['score_total']}（{rep['score_status']}）；各题 {summary}", flush=True)
    await shot(c, "ys_exam_07_report.png", full=True)
    leak = [k for k in ("checks", "solution", "inject", "scoring_points") if f'"{k}"' in json.dumps(rep)]
    step(res, 6, "成绩单：3 题都通过（提交 1 / 2 / 1 次），各题复位成功（与开题前快照一致），无答案字段",
         [s[1:4] for s in summary] == [("done", True, 1), ("done", True, 2), ("done", True, 1)]
         and all(q.get("reset_ok") for q in qs) and not leak, str(leak))
    return res


async def session(c) -> list[bool]:
    """会话被挤掉 → 页面提示与「重新登录 eNSP Pro」按钮 → 重新登录 → 终端恢复。挤会话用脚本自己的第二个登录会话模拟。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from enspro import EnsproClient, EnsproError
    res: list[bool] = []
    port = DEV["AR1"]
    await c.wait_for("window.__ainet && window.__ainet.state.topo", 20)
    await c.ev("window.confirm = () => true")
    ok = await open_dev_topo(c, "AR1", port)
    out = await type_cmd(c, port, "display clock")
    step(res, 1, "AR1 终端可用（display clock 有回显）", ok and "UTC" in out)
    other = EnsproClient(os.environ.get("AINET_ENSPRO_URL", "https://192.168.56.101:8443"))
    await other.ensure()                                    # 第二个会话登录：把 ainet 的会话挤掉
    t0 = time.time()
    lost = await c.wait_for("!document.getElementById('btn-relogin').hidden", 60)
    status = await c.ev("document.getElementById('topo-status').textContent")
    print(f"  被挤掉后 {time.time() - t0:.1f}s 页面提示：{status!r}", flush=True)
    await shot(c, "ys_session_01_lost.png")
    step(res, 2, "会话被挤掉：状态栏提示「在其它地方被重新登录」并出现「重新登录 eNSP Pro」按钮",
         lost and "其它地方" in status, status)
    await asyncio.sleep(3)
    rs = await c.ev(f"(t => t && t.ws ? t.ws.readyState : -1)(window.__ainet.state.tabs.get({port}))")
    tail = ""
    if rs == 1:                                             # 终端连接还开着：敲一条命令看有没有回显
        before = await buf(c, port)
        await c.ev(f"window.__ainet.state.tabs.get({port}).ws.send(new TextEncoder().encode('display clock\\r'))")
        await asyncio.sleep(6)
        tail = (await buf(c, port))[len(before.rstrip()):].strip()
    else:
        tail = (await buf(c, port)).strip().split("\n")[-1]
    print(f"  挤掉期间 AR1 终端：WebSocket readyState={rs}，最后输出 {tail[-200:]!r}", flush=True)
    # 别处的会话照常可用（ainet 不自动抢回会话）
    still = (await other.sandbox(int(os.environ.get("AINET_ENSPRO_SANDBOX", "3"))))["name"]
    step(res, 3, "缺省不自动重登：别处的会话不受影响", bool(still))
    await c.ev("document.getElementById('btn-relogin').click()")
    back = await c.wait_for("document.getElementById('btn-relogin').hidden && "
                            "document.getElementById('topo-status').className === 'st-running'", 30)
    try:
        await other.sandbox(3)
        kicked_other = False
    except EnsproError as exc:
        kicked_other = exc.code in (50070014, 50010005)
    await shot(c, "ys_session_02_relogin.png")
    step(res, 4, "点「重新登录 eNSP Pro」：页面恢复运行状态，别处的会话被挤掉", back and kicked_other)
    ok5, out = False, ""
    reopened = 0
    for _ in range(3):
        rs = await c.ev(f"(t => t && t.ws ? t.ws.readyState : -1)(window.__ainet.state.tabs.get({port}))")
        if rs == 1:
            out = await type_cmd(c, port, "display clock", timeout=30)
            if "UTC" in out:
                ok5 = True
                break
        await c.ev(f"window.__ainet.closeTab({port})")       # 旧连接已断：关掉标签、在拓扑图上重新打开
        await asyncio.sleep(4)
        reopened += 1
        await open_dev_topo(c, "AR1", port)
    print(f"  重新打开终端 {reopened} 次", flush=True)
    print(f"  重新登录后 AR1 终端：{out.strip()[-160:]!r}", flush=True)
    await shot(c, "ys_session_03_terminal_back.png")
    step(res, 5, "重新登录后 AR1 终端恢复（display clock 有回显）", ok5)
    await other._http.aclose()                              # 这个会话已被挤掉，不再调 logout
    return res


async def open_dev_topo(c, name: str, port: int) -> bool:
    """在拓扑图上点设备打开终端，等提示符。"""
    await c.click_node(name)
    ok = await c.wait_for(f"(t => t && t.ws && t.ws.readyState === 1)(window.__ainet.state.tabs.get({port}))", 20)
    end = time.time() + 40
    while time.time() < end:
        if re.search(PROMPT, (await buf(c, port)).rstrip()):
            return bool(ok)
        await asyncio.sleep(0.5)
    return False


async def run() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={DEBUG_PORT}", "--no-sandbox",
                             "--window-size=1600,900", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
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
            res = await (exam(c) if MODE == "exam" else session(c) if MODE == "session" else practice(c))
            await asyncio.sleep(1)
            print("JS 错误:", c.errors or "无", flush=True)
            step(res, len(res) + 1, "全程无 JS 异常 / console error", not c.errors)
            reader.cancel()
            print(f"结果：{sum(res)} pass / {len(res) - sum(res)} fail", flush=True)
            return 0 if all(res) else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
