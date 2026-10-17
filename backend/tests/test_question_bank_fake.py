"""题库规划（docs/question-bank-plan.md）里沙箱 ainet_test 的草稿题，在假 eNSP Pro 上跑一遍完整练习：
开始（baseline → 注入 → 确认故障生效）→ 学员经终端（WebSocket → hub → STOMP console）执行参考修复 → 判定通关 →
结束（restore + 整机复位，与开练前快照一致）。

假设备（tests/fake_enspro.NetYunShan + fake_vrp.FakeNet）按配置计算 ping：接口地址 / 掩码、静态路由、接口 shutdown；
不模拟 ping -a 的源地址（按首跳出接口地址回包）。ainet_campus 的草稿题（DHCP / DNS / NAT / 子接口 / OSPF 定时器等）
假设备不模拟。17 道题 2026-10-07 都已在真机验证通过并上线（docs/question-bank-verify.md）；这里通过不代表真机能通过。
"""
from __future__ import annotations

import pytest

import config_reset as cr
from fake_enspro import NetYunShan
from fake_vrp import FakeNet
from test_enspro_api import BASE, PW, env, recv_until  # noqa: F401  （env 是 fixture）
from test_scenarios import BANK_TEST, DRAFTS_TEST
from practice_helpers import end_practice

PORTS = {"AR1": 6, "AR2": 7}


def _run_solution(c, dev: str, cmds: list[str]) -> None:
    with c.websocket_connect(f"/ws/console/{PORTS[dev]}") as ws:
        recv_until(ws, f"[已连接] eNSP Pro {dev}")
        for line in cmds:
            ws.send_bytes((line + "\r").encode())
            recv_until(ws, "<HUAWEI>" if line == "return" else "]")


@pytest.mark.parametrize("sid", sorted(BANK_TEST))
def test_draft_scenario_inject_fix_judge_reset(env, sid):  # noqa: F811
    c, t1, t2 = env["c"], env["t1"], env["t2"]
    sc = {s["id"]: s for s in c.get("/api/scenarios?all=1").json()["scenarios"]}
    draft = sid in DRAFTS_TEST
    assert (sid not in sc) == draft                       # 草稿题缺省不列出（AINET_SHOW_DRAFTS 未开）
    pm = c.app.state.practice
    tpl = pm.scenarios[sid]
    assert tpl.get("draft", False) is draft and tpl["topology"] == "ainet_test"
    snap = {"AR1": t1.config_text(), "AR2": t2.config_text()}

    r = c.post("/api/practice/start", json={"scenario_id": sid, "seed": 0})   # 按 id 开始不受 draft 限制
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["phase"] == "running" and st["last_check"]["all"] is False, st["last_check"]   # 故障确实生效
    assert sid in {s["id"] for s in c.get("/api/scenarios?all=1").json()["scenarios"]}     # 进行中的（草稿）题照列

    for dev, cmds in tpl["solution"].items():
        _run_solution(c, dev, cmds)
    r = c.post("/api/practice/check").json()
    assert r["check"]["all"] is True and r["passed"] is True, r

    res = end_practice(c)
    assert res["passed"] and res["reset_ok"] and res["restored"], res
    assert cr.same(snap["AR1"], t1.config_text()) and cr.same(snap["AR2"], t2.config_text())


def test_shutdown_breaks_link_in_fake_net():
    """假网络模型：任一端接口 shutdown 链路即不通、该接口不进路由表（L01 草稿题依赖这一点）。"""
    net = FakeNet([(("AR1", "GE0/0/1"), ("AR2", "GE0/0/1"))])
    t1 = net.add("AR1", NetYunShan(BASE, password=PW))
    t2 = net.add("AR2", NetYunShan(BASE, password=PW))
    for dev, ip in ((t1, "10.0.12.1"), (t2, "10.0.12.2")):
        for cmd in ("system-view", "interface GE0/0/1", f"ip address {ip} 24", "return"):
            dev._apply(cmd)
    assert net.ping("AR1", "10.0.12.2")
    for cmd in ("system-view", "interface GE0/0/1", "shutdown"):
        t2._apply(cmd)
    assert not net.ping("AR1", "10.0.12.2") and "GE0/0/1" not in net.l3("AR2") and "GE0/0/1" in net.l3("AR1")
    t2._apply("undo shutdown")
    assert net.ping("AR1", "10.0.12.2")
