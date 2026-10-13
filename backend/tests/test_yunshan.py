"""ConsoleHub 与配置复位在 YunShan OS 格式下的行为（AR6700 V600R025 实测格式，假设备模拟）：
`!` 注释行、显式 sysname HUAWEI、ESC[16D 分页擦除、undo sysname 回到 HUAWEI、ping 输出、两阶段生效型号的 commit。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import config_reset as cr
import console_hub as chub
from console_hub import ConsoleHub
from fake_enspro import TwoStageVRP, YunShanConfigVRP, YS_ERASE

BASE = (Path(__file__).parent / "fixtures" / "configs" / "yunshan_ar6700_base.cfg").read_text()


def run(coro, timeout=40):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def make(dev, **kw) -> ConsoleHub:
    return ConsoleHub(dev.factory, idle_close=60, min_reconnect=0, ready_timeout=1.0, settle=0.05,
                      default_sysname="HUAWEI", **kw)


# ---- 纯函数 ----

def test_parse_skips_bang_lines_and_compares_without_timestamps():
    nodes = cr.parse(BASE)
    assert not any(n.line.startswith("!") for n in nodes)
    assert cr.sysname_of(nodes) == "HUAWEI"
    ssl = next(n for n in nodes if n.line == "ssl policy default")
    assert [c.line for c in ssl.children][:2] == ["pki-domain default", "ssl minimum version tls1.2"]
    assert "cipher-suite exclude cipher mode cbc" in [c.line for c in ssl.children]
    na = next(n for n in nodes if n.line == "net-assistant")
    assert [c.line for c in na.children] == ["system-management", "network"]   # 块内缩进的 # 不算段
    later = BASE.replace("2026-10-06 06:15:07+00:00 by SYSTEM automatically", "2026-10-06 09:00:00+00:00")
    assert cr.same(BASE, later)                       # !Last configuration was updated at … 每次都变，不比
    assert cr.make_plan(nodes, cr.parse(later)).empty


def test_yunshan_text_helpers():
    raw = "line1\r\n  ---- More ----" + YS_ERASE + " child\r\n<HUAWEI>"
    assert chub.clean(raw) == "line1\n child\n<HUAWEI>"
    assert chub.ends_with_more("abc\r\n  ---- More ----")
    assert chub.prompt_of("x\r\n[HUAWEI-GE0/0/1]") == "[HUAWEI-GE0/0/1]"
    assert chub.prompt_of("x\r\n[~HUAWEI-GE0/0/1]") == "[~HUAWEI-GE0/0/1]"
    assert chub.prompt_of("x\r\n[*HUAWEI]") == "[*HUAWEI]"
    assert chub.is_confirm("Are you sure to commit them before exiting? [Y(yes)/N(no)/C(cancel)]:")
    assert chub.is_confirm("Continue?[Y/N]:") and not chub.is_confirm("[HUAWEI]")
    log = "Oct  6 2026 07:05:12 HUAWEI %%01UPDATE/4/DOWNLOAD_FAIL(l):CID=0x814f043b;Failed to download the new version."
    assert chub.is_log(log)                           # YunShan 日志不带时区偏移
    assert ConsoleHub._prompt_ok("[~HUAWEI-GE0/0/1]", "config", "<HUAWEI>", "HUAWEI")


# ---- hub：隐藏事务、复位 ----

def test_snapshot_with_yunshan_paging_and_restore():
    async def go():
        dev = YunShanConfigVRP(BASE, page=12)
        hub = make(dev)
        sub = await hub.subscribe(6)
        reset = cr.ConfigReset(hub, deadline=3, busy_retry=0.1)
        snap = await reset.snapshot(6)
        assert cr.same(BASE, snap)                    # 分页（ESC[16D 擦除）去干净，子行缩进不丢
        # 学员的改动：接口地址、新建 LoopBack、改设备名
        r = await hub.run_hidden(6, ["system-view", "interface GE0/0/1", "ip address 10.0.12.1 24", "quit",
                                     "interface LoopBack9", "ip address 10.88.88.1 32", "quit",
                                     "sysname TX"], kind="config")
        assert r.ok, r.reason
        assert r.prompt == "<TX>"
        assert not cr.same(snap, dev.config_text())
        res = await reset.restore_to(6, snap)
        assert res.ok, (res.reason, res.leftovers)
        assert cr.same(snap, dev.config_text()) and dev.host == "HUAWEI"
        assert hub.channels[6].cur_prompt == "<HUAWEI>"
        hub.unsubscribe(sub)
        await hub.close()
    run(go())


def test_undo_sysname_tracks_yunshan_default():
    async def go():
        dev = YunShanConfigVRP(BASE.replace("sysname HUAWEI", "sysname T1"), page=50)
        hub = make(dev)
        await hub.subscribe(6)
        r = await hub.run_hidden(6, ["system-view", "undo sysname"], kind="config")
        assert r.ok, r.reason
        assert r.prompt == "<HUAWEI>" and dev.host == "HUAWEI"
        r = await hub.run_hidden(6, ["display version"], kind="check")
        assert r.ok, r.reason
        await hub.close()
    run(go())


def test_two_stage_commit_before_return():
    async def go():
        dev = TwoStageVRP(BASE, page=50)
        hub = make(dev)
        await hub.subscribe(6)
        r = await hub.run_hidden(6, ["system-view", "interface GE0/0/1", "ip address 10.0.12.1 24"], kind="config")
        assert r.ok, r.reason
        assert dev.commits == 1 and dev.view is None and not dev.dirty     # 先 commit 再 return，不卡在 Y/N/C
        snap = BASE
        res = await cr.ConfigReset(hub, deadline=3, busy_retry=0.1).restore_to(6, snap)
        assert res.ok, (res.reason, res.leftovers)
        assert cr.same(snap, dev.config_text()) and dev.commits >= 2
        await hub.close()
    run(go())


def test_student_ping_output_capture_yunshan():
    async def go():
        dev = YunShanConfigVRP(BASE, page=50)
        dev.state["ping_ok"] = True
        hub = make(dev)
        events = []
        hub.add_listener(lambda e, p, d: events.append((e, d)))
        sub = await hub.subscribe(6)
        await asyncio.sleep(0.1)
        for c in "ping -c 2 10.0.12.2\r":
            await hub.write(sub, c.encode())
        await asyncio.sleep(0.3)
        cmds = [d for e, d in events if e == "command"]
        outs = [d for e, d in events if e == "output"]
        assert cmds == ["ping -c 2 10.0.12.2"]
        assert outs and "2 packet(s) received" in outs[0] and "round-trip" in outs[0]
        assert not outs[0].rstrip().endswith("<HUAWEI>")
        # 判定正则（与 VRP5 相同）在 YunShan 输出上成立
        r = await hub.run_hidden(6, ["ping -c 2 10.0.12.2"], kind="check")
        import re
        assert r.ok and re.search(r"\b[12] packet\(s\) received", r.outputs[0])
        dev.state["ping_ok"] = False
        r = await hub.run_hidden(6, ["ping -c 2 10.0.12.2"], kind="check")
        assert r.ok and not re.search(r"\b[12] packet\(s\) received", r.outputs[0])
        await hub.close()
    run(go())


def test_reclaim_two_stage_uncommitted_system_view():
    """YunShan 两阶段：学员停在 [*HUAWEI-GE0/0/1]（有未提交配置）：reclaim 先 commit 再 return，回到 <HUAWEI>，
    之后整机复位能把学员的改动撤掉。"""
    async def go():
        dev = TwoStageVRP(BASE, page=50)
        hub = make(dev)
        sub = await hub.subscribe(6)
        await asyncio.sleep(0.1)
        for line in ("system-view\r", "interface GE0/0/1\r", "ip address 10.9.9.1 24\r", "desc"):
            for c in line:
                await hub.write(sub, c.encode())
            await asyncio.sleep(0.1)
        assert dev.dirty and dev.view is not None
        hub.freeze([6])
        r = await hub.reclaim(6)
        assert r.ok and r.prompt == "<HUAWEI>", r.reason
        assert dev.view is None and not dev.dirty and dev.typed == ""
        res = await cr.ConfigReset(hub, deadline=3, busy_retry=0.1).restore_to(6, BASE)
        assert res.ok, (res.reason, res.leftovers)
        assert cr.same(BASE, dev.config_text())
        await hub.close()
    run(go())
