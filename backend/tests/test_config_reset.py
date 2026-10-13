"""配置复位（config_reset）：解析、差异、撤销命令生成（纯函数，样例来自 2026-10-04 真机抓取的配置），
以及经真实 ConsoleHub + 带配置树的假 VRP 的整机复位集成测试。

fixtures/configs：
- ar1220_factory / s5700_factory：未配置的 AR1220、S5700（eNSP Campus_Network 的 AR0 / LSW0）。
- ar1220_student1 / s5700_student1：在出厂配置上做了一批学员式改动（接口 IP / sub 地址 / shutdown / description、
  LoopBack、静态路由、OSPF、ACL + traffic-filter、sysname；VLAN、access / trunk、Vlanif、stp mode）。
- ar1220_base2 / s5700_base2：带初始配置的快照；ar1220_student2 / s5700_student2：在它上面改、删
  （改 IP、删 IP、删路由、OSPF 删 / 加 network 与新 area、ACL 换规则、outbound 过滤；删快照里的 VLAN、
  换 access VLAN、改 Vlanif 地址、sysname）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import config_reset as cr
from console_hub import ConsoleHub
from fake_vrp import ConfigVRP

CFG = Path(__file__).resolve().parent / "fixtures" / "configs"


def load(name: str) -> str:
    return (CFG / f"{name}.cfg").read_text(encoding="utf-8")


def plan_of(target: str, current: str, attempts=None) -> cr.Plan:
    return cr.make_plan(cr.parse(load(target)), cr.parse(load(current)), attempts)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ---- 解析 ----

def test_parse_structure_ar1220():
    nodes = cr.parse(load("ar1220_student1"))
    lines = [n.line for n in nodes]
    # AR 的顶层行本身带 1 个空格缩进，仍是顶层；版本行 / return 不算
    assert "sysname R-Student" in lines and "snmp-agent local-engineid 800007DB03000000000000" in lines
    assert "snmp-agent" in lines                         # 行尾空格去掉
    assert not any(l.startswith("[V200") or l == "return" for l in lines)
    ospf = next(n for n in nodes if n.line.startswith("ospf 1"))
    assert ospf.line == "ospf 1 router-id 1.1.1.1"
    assert [a.line for a in ospf.children] == ["area 0.0.0.0"]
    assert [c.line for c in ospf.children[0].children] == ["network 1.1.1.1 0.0.0.0", "network 10.0.12.0 0.0.0.255"]
    ge0 = next(n for n in nodes if n.line == "interface GigabitEthernet0/0/0")
    assert [c.line for c in ge0.children] == ["description to-LSW0", "shutdown",
                                              "ip address 192.168.10.1 255.255.255.0",
                                              "ip address 192.168.11.1 255.255.255.0 sub"]
    ui = [n.line for n in nodes if n.line.startswith("user-interface")]
    assert ui == ["user-interface con 0", "user-interface vty 0 4", "user-interface vty 16 20"]


def test_parse_ignores_echo_prompt_logs_and_inner_hash():
    text = ("display current-configuration\r\n[V200R003C00]\r\n#\r\n sysname R1\r\n#\r\nbgp 100\r\n"
            " peer 1.1.1.1 as-number 200\r\n #\r\n ipv4-family unicast\r\n  peer 1.1.1.1 enable\r\n#\r\n"
            "Oct  4 2026 15:20:16-08:00 Huawei DS/4/DATASYNC_CFGCHANGE:OID 1.3.6.1 configurations changed.\r\n"
            "interface NULL0\r\n#\r\nreturn\r\n<R1>")
    nodes = cr.parse(text)
    assert [n.line for n in nodes] == ["sysname R1", "bgp 100", "interface NULL0"]
    bgp = nodes[1]
    assert [c.line for c in bgp.children] == ["peer 1.1.1.1 as-number 200", "ipv4-family unicast"]
    assert bgp.children[1].children[0].line == "peer 1.1.1.1 enable"


def test_same_config_no_plan_and_captures_are_stable():
    for name in ("ar1220_factory", "s5700_factory", "ar1220_student2", "s5700_base2"):
        p = plan_of(name, name)
        assert p.empty and p.groups() == [] and p.excluded == []
    assert cr.same(load("ar1220_factory"), load("ar1220_factory") + "\n<Huawei>")


# ---- 撤销命令 ----

def test_undo_candidates_rules_and_truncation():
    assert cr.undo_candidates("undo info-center enable") == ["info-center enable"]
    assert cr.undo_candidates("description to-LSW0") == ["undo description"]
    assert cr.undo_candidates("port link-type trunk") == ["undo port link-type"]
    assert cr.undo_candidates("port default vlan 10") == ["undo port default vlan"]
    assert cr.undo_candidates("shutdown") == ["undo shutdown"]
    assert cr.undo_candidates("ospf 1 router-id 1.1.1.1") == ["undo ospf 1"]
    assert cr.undo_candidates("traffic-filter inbound acl 3000") == ["undo traffic-filter inbound"]
    assert cr.undo_candidates("rule 10 deny icmp") == ["undo rule 10"]
    assert cr.undo_candidates("acl number 3000") == ["undo acl number 3000"]
    assert cr.undo_candidates("interface LoopBack0") == ["undo interface LoopBack0"]
    # ip address 不截断（undo ip address 会删掉接口上全部地址）
    assert cr.undo_candidates("ip address 10.0.12.1 255.255.255.0") == ["undo ip address 10.0.12.1 255.255.255.0"]
    # 其余：整行 → 去掉末尾 1 个 → 2 个参数，至少保留 2 个词
    assert cr.undo_candidates("ip route-static 0.0.0.0 0.0.0.0 1.1.1.1 preference 100") == [
        "undo ip route-static 0.0.0.0 0.0.0.0 1.1.1.1 preference 100",
        "undo ip route-static 0.0.0.0 0.0.0.0 1.1.1.1 preference",
        "undo ip route-static 0.0.0.0 0.0.0.0 1.1.1.1"]
    assert cr.undo_candidates("dhcp enable") == ["undo dhcp enable"]


def test_vlan_spec_roundtrip():
    assert cr._vlan_ids("10 20 30 to 35") == {10, 20, 30, 31, 32, 33, 34, 35}
    assert cr.vlan_spec({10, 20, 30, 31, 32, 33}) == "10 20 30 to 33"
    assert cr.vlan_spec({10, 11}) == "10 11"


# ---- 计划（真机样例） ----

def test_plan_ar1220_student1_to_factory():
    p = plan_of("ar1220_factory", "ar1220_student1")
    assert p.groups() == [
        (["system-view", "interface GigabitEthernet0/0/1", "undo traffic-filter inbound",
          "undo ip address 10.0.12.1 255.255.255.0"], 2),
        (["system-view", "interface GigabitEthernet0/0/0", "undo ip address 192.168.11.1 255.255.255.0 sub",
          "undo ip address 192.168.10.1 255.255.255.0", "undo shutdown", "undo description"], 2),
        (["system-view", "undo ip route-static 192.168.2.0 255.255.255.0 10.0.12.2",
          "undo ip route-static 0.0.0.0 0.0.0.0 192.168.10.254 preference 100",
          "undo ospf 1", "undo interface LoopBack0", "undo acl number 3000", "sysname Huawei"], 1),
    ]


def test_plan_s5700_student1_to_factory():
    p = plan_of("s5700_factory", "s5700_student1")
    assert p.groups() == [
        (["system-view", "interface GigabitEthernet0/0/3", "undo shutdown"], 2),
        (["system-view", "interface GigabitEthernet0/0/2", "undo port trunk allow-pass vlan 10 20",
          "undo port link-type"], 2),
        (["system-view", "interface GigabitEthernet0/0/1", "undo port default vlan", "undo port link-type"], 2),
        (["system-view", "undo ip route-static 0.0.0.0 0.0.0.0 10.0.10.1", "undo interface Vlanif10",
          "undo acl number 3001", "undo stp mode", "undo vlan batch 10 20 30", "sysname Huawei"], 1),
    ]


def test_plan_ar1220_student2_back_to_base2_readds_and_orders():
    g = plan_of("ar1220_base2", "ar1220_student2").groups()
    flat = [c for cmds, _ in g for c in cmds]
    # 新 area：先删 network 再删 area（实测 VRP 要求）
    assert flat.index("undo network 10.0.10.0 0.0.0.255") < flat.index("undo area 0.0.0.1")
    assert (["system-view", "ospf 1", "area 0.0.0.1", "undo network 10.0.10.0 0.0.0.255"], 3) in g
    # 删除全部在添加之前；被学员删掉的配置补回（在对应视图里）
    assert (["system-view", "ospf 1", "area 0.0.0.0", "network 10.0.12.0 0.0.0.255"], 3) in g
    assert (["system-view", "interface GigabitEthernet0/0/0", "ip address 10.0.10.1 255.255.255.0"], 2) in g
    assert (["system-view", "acl number 3000", "rule 5 permit ip"], 2) in g
    assert "undo traffic-filter outbound" in flat and "undo rule 10" in flat
    assert flat.index("undo ip address 10.0.13.1 255.255.255.0") < flat.index("ip address 10.0.12.1 255.255.255.0")
    assert flat[-1] == "sysname Huawei"
    assert "ip route-static 192.168.2.0 255.255.255.0 10.0.12.2" in flat
    assert "undo ip route-static 192.168.3.0 255.255.255.0 10.0.12.2" in flat


def test_plan_s5700_student2_vlan_sets():
    g = plan_of("s5700_base2", "s5700_student2").groups()
    flat = [c for cmds, _ in g for c in cmds]
    # VLAN 按集合比较：只删 30、补回 20，不动 10（端口还在用）
    assert "undo vlan batch 30" in flat and "vlan batch 20" in flat
    assert not any(c.startswith("undo vlan batch") and "10" in c.split() for c in flat)
    assert flat.index("undo port default vlan") < flat.index("undo vlan batch 30")
    assert (["system-view", "interface GigabitEthernet0/0/1", "port default vlan 10"], 2) in g
    assert (["system-view", "interface GigabitEthernet0/0/2", "port trunk allow-pass vlan 10 20"], 2) in g
    assert flat[-1] == "sysname Huawei"


def test_vlan_block_kept_when_vlan_in_snapshot():
    tgt = cr.parse("#\nvlan batch 10 30\n#\ninterface NULL0\n#\nreturn")
    cur = cr.parse("#\nvlan batch 10 30\n#\nvlan 30\n description guest\n#\ninterface NULL0\n#\nreturn")
    assert cr.make_plan(tgt, cur).groups() == [(["system-view", "vlan 30", "undo description"], 2)]


def test_sysname_single_set_operation():
    # S5700 的快照显式显示 sysname Huawei：改名后只需一条 sysname Huawei，不会先 undo 再加
    tgt = cr.parse("#\nsysname Huawei\n#\nreturn")
    cur = cr.parse("#\nsysname SW1\n#\nreturn")
    assert cr.make_plan(tgt, cur).groups() == [(["system-view", "sysname Huawei"], 1)]
    # AR 的快照不显示 sysname（默认 Huawei）；改回后显示与否都算一致
    assert cr.make_plan(cr.parse("#\nreturn"), cr.parse("#\n sysname Huawei\n#\nreturn")).empty
    assert cr.differences(cr.parse("#\nreturn"), cr.parse("#\n sysname R1\n#\nreturn")) == ["设备名：当前 R1，应为 Huawei"]


def test_excluded_user_interface_and_local_user():
    base = load("ar1220_factory")
    cur = base.replace("user-interface vty 0 4", "user-interface vty 0 4\n authentication-mode aaa") \
              .replace(" local-user admin service-type http", " local-user admin service-type http\n"
                                                              " local-user test password cipher abc")
    p = cr.make_plan(cr.parse(base), cr.parse(cur))
    assert p.ops == [] and p.empty
    assert sorted(p.excluded) == ["aaa / local-user test password cipher abc", "user-interface vty 0 4"]
    assert cr.differences(cr.parse(base), cr.parse(cur)) == []


def test_attempts_advance_candidates_then_exhaust():
    tgt = cr.parse("#\ninterface NULL0\n#\nreturn")
    cur = cr.parse("#\ninterface NULL0\n#\nfoo bar baz qux\n#\nreturn")
    attempts: dict = {}
    seen = []
    for _ in range(4):
        p = cr.make_plan(tgt, cur, attempts)
        if not p.ops:
            break
        seen.append(p.ops[0].cmds[0])
        cr.note_attempts(p, attempts)
    assert seen == ["undo foo bar baz qux", "undo foo bar baz", "undo foo bar"]
    p = cr.make_plan(tgt, cur, attempts)
    assert not p.ops and [o.line for o in p.exhausted] == ["foo bar baz qux"] and not p.empty


def test_top_block_falls_back_to_children_first():
    tgt = cr.parse("#\ninterface NULL0\n#\nreturn")
    cur = cr.parse("#\nfoo 1\n bar 2\n#\ninterface NULL0\n#\nreturn")
    attempts: dict = {}
    p = cr.make_plan(tgt, cur, attempts)
    assert p.groups() == [(["system-view", "undo foo 1"], 1)]
    cr.note_attempts(p, attempts)
    p = cr.make_plan(tgt, cur, attempts)                 # 块头没删掉：先删子行再删块头
    assert p.groups() == [(["system-view", "foo 1", "undo bar 2"], 2), (["system-view", "undo foo 1"], 1)]


def test_missing_block_readded_whole():
    tgt = cr.parse("#\nospf 1 router-id 1.1.1.1\n silent-interface all\n area 0.0.0.0\n  network 10.0.0.0 0.0.0.255\n"
                   " area 0.0.0.1\n  network 10.0.1.0 0.0.0.255\n#\nreturn")
    p = cr.make_plan(tgt, cr.parse("#\nreturn"))
    # 每个子块各自一组、从用户视图沿完整路径进入，块头都算 guard；不发 quit（块头进不去时 quit 会多退一级甚至登出 console）
    assert p.groups() == [
        (["system-view", "ospf 1 router-id 1.1.1.1", "silent-interface all"], 2),
        (["system-view", "ospf 1 router-id 1.1.1.1", "area 0.0.0.0", "network 10.0.0.0 0.0.0.255"], 3),
        (["system-view", "ospf 1 router-id 1.1.1.1", "area 0.0.0.1", "network 10.0.1.0 0.0.0.255"], 3),
    ]
    flat = [c for g, _ in p.groups() for c in g]
    assert "quit" not in flat


def test_plans_never_send_quit():
    for tgt, cur in (("ar1220_factory", "ar1220_student1"), ("ar1220_student1", "ar1220_factory"),
                     ("ar1220_base2", "ar1220_student2"), ("ar1220_student2", "ar1220_base2"),
                     ("s5700_factory", "s5700_student1"), ("s5700_student1", "s5700_factory"),
                     ("s5700_base2", "s5700_student2"), ("s5700_student2", "s5700_base2")):
        flat = [c for g, _ in plan_of(tgt, cur).groups() for c in g]
        assert "quit" not in flat and not any(c.startswith(("save", "reboot", "reset ")) for c in flat), (tgt, cur)


# ---- 集成：真实 ConsoleHub + 带配置树的假 VRP ----

def make_hub(dev):
    return ConsoleHub(dev.factory, idle_close=60, min_reconnect=0, ready_timeout=1.0, settle=0.05)


def test_restore_to_through_hub_ar1220():
    async def go():
        dev = ConfigVRP(load("ar1220_student1"))
        assert dev.host == "R-Student"
        hub = make_hub(dev)
        sub = await hub.subscribe(2000)
        await asyncio.sleep(0.2)
        reset = cr.ConfigReset(hub, deadline=1, busy_retry=0.05)
        snap = load("ar1220_factory")
        res = await reset.restore_to(2000, snap)
        assert res.status == "ok", res
        assert cr.same(snap, dev.config_text())
        assert dev.host == "Huawei" and dev.view is None
        # [Y/N] 只回答一次（undo ospf 1）；回显慢也不会多答
        assert dev.confirms == 1
        assert "y" not in dev.executed
        # 学员终端：看不到复位命令，只看到改名后的新提示符
        seen = b""
        while not sub.queue.empty():
            seen += sub.queue.get_nowait()
        text = seen.decode()
        assert "undo" not in text and "display current" not in text
        assert text.rstrip().endswith("<Huawei>")
        assert hub.channels[2000].tail.endswith("<Huawei>")
        # 之后的隐藏事务按新提示符执行
        r = await hub.run_hidden(2000, ["display current-configuration"], kind="check")
        assert r.ok and "sysname" not in r.outputs[0]
        await hub.close()
    run(go())


def test_restore_to_through_hub_s5700_with_readds():
    async def go():
        dev = ConfigVRP(load("s5700_student2"))
        hub = make_hub(dev)
        reset = cr.ConfigReset(hub, deadline=1, busy_retry=0.05)
        snap = load("s5700_base2")
        res = await reset.restore_to(2001, snap)
        assert res.status == "ok", res
        assert cr.same(snap, dev.config_text()) and dev.host == "Huawei"
        await hub.close()
    run(go())


def test_restore_to_reports_leftovers_when_undo_refused():
    async def go():
        dev = ConfigVRP(load("ar1220_student1"))
        dev.refuse = {"undo acl number 3000"}
        hub = make_hub(dev)
        reset = cr.ConfigReset(hub, deadline=1, busy_retry=0.05)
        res = await reset.restore_to(2000, load("ar1220_factory"))
        assert res.status == "dirty"
        assert res.leftovers == ["多出：acl number 3000"]
        assert any("undo acl number 3000" in w for w in res.warnings)
        await hub.close()
    run(go())


def test_snapshot_and_busy_when_student_typing():
    async def go():
        dev = ConfigVRP(load("ar1220_factory"))
        hub = make_hub(dev)
        reset = cr.ConfigReset(hub, deadline=0.3, busy_retry=0.05)
        text = await reset.snapshot(2000)
        assert cr.same(load("ar1220_factory"), text)
        sub = await hub.subscribe(2000)
        await hub.write(sub, b"dis")                  # 学员敲了一半
        try:
            await reset.snapshot(2000)
            raise AssertionError("应当 busy")
        except cr.ResetError as e:
            assert e.status == "busy"
        await hub.close()
    run(go())


def test_student_sysname_change_followed_by_hub():
    async def go():
        dev = ConfigVRP(load("s5700_factory"))
        hub = make_hub(dev)
        sub = await hub.subscribe(2001)
        await asyncio.sleep(0.2)
        for line in ("system-view", "sysname SW-X", "return"):
            await hub.write(sub, line.encode() + b"\r")
            await asyncio.sleep(0.1)
        assert dev.host == "SW-X"
        r = await hub.run_hidden(2001, ["display current-configuration"], kind="check")
        assert r.ok and "sysname SW-X" in r.outputs[0] and r.prompt == "<SW-X>"
        res = await cr.ConfigReset(hub, deadline=1, busy_retry=0.05).restore_to(2001, load("s5700_factory"))
        assert res.ok and dev.host == "Huawei"
        await hub.close()
    run(go())
