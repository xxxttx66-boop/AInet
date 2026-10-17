import os
import re
import shutil
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

import config
import ensp_runtime
import main
import topology

TOPOS = Path(__file__).resolve().parent / "fixtures" / "topos"
OSPF = TOPOS / "2-1Single-Area OSPF.topo"
LAB = TOPOS / "Lab 1-1 Ethernet Interface and Link Configuration (Unconfigured).topo"
ALL = sorted(TOPOS.glob("*.topo"))


@pytest.fixture
def topo_dirs(monkeypatch):
    monkeypatch.setattr(config, "TOPO_DIRS", [str(TOPOS)])
    return TOPOS


@pytest.fixture
def fake_probe(monkeypatch):
    """确认设备状态判定不连 console 端口：一旦尝试连接就让测试失败。

    返回的集合仅为兼容旧用例（往里加减端口不再影响结果）。
    """
    async def no_connect(*a, **k):
        raise AssertionError("resolve_running 不应探测 console 端口")

    monkeypatch.setattr(topology.asyncio, "open_connection", no_connect)
    return {2000, 2001, 2002}


def running(monkeypatch, *ids):
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", ",".join(ids))


def login(client):
    assert client.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200


# ---- 解析器 ----

@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_all_fixtures_parse(path):
    t = topology.parse_topo(str(path))
    text = path.read_text("latin-1")  # 只数标签，编码无关
    assert len(t["devices"]) == text.count("<dev ")
    assert len(t["links"]) == text.count("<interfacePair")
    ids = {d["id"] for d in t["devices"]}
    for link in t["links"]:
        assert link["src_id"] in ids and link["dst_id"] in ids
    assert len(t["notes"]) == len(re.findall(r'<txttip [^>]*content="[^"]*\S', text))
    # 直线全保留；矩形 / 椭圆过滤掉宽或高为 0 的
    shapes = re.findall(r'<shape type="(\d)"[^>]*width="(\d+)" height="(\d+)"', text)
    want = [k for k, w, h in shapes if k == "0" or (int(w) > 0 and int(h) > 0)]
    assert [s["kind"] for s in t["shapes"]] == [{"0": "line", "1": "rect", "2": "ellipse"}[k] for k in want]


def test_campus_old_format_interfaces_follow_real_device():
    """旧格式 Campus_Network.topo：AR1220 主板在文件里是 Ethernet×2 + GE×4 + Serial×4，真机是 GE×2 + Ethernet×8，
    连线序号按真机顺序解释（2026-10-04 真机锚点：AR0–LSW0、AR0–AR2 两端配地址 ping 通）。"""
    t = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    devs = {d["name"]: d for d in t["devices"]}
    names = {d["id"]: d["name"] for d in t["devices"]}
    assert devs["AR0"]["interfaces"] == ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1"] + [
        f"Ethernet0/0/{i}" for i in range(8)]
    assert devs["LSW0"]["interfaces"] == [f"GigabitEthernet0/0/{i}" for i in range(1, 25)]

    def link(a, b):
        for l in t["links"]:
            ends = {names[l["src_id"]]: l["src_label"], names[l["dst_id"]]: l["dst_label"]}
            if set(ends) == {a, b}:
                return ends
        raise AssertionError(f"no link {a}-{b}")
    assert link("AR0", "AR2") == {"AR0": "GigabitEthernet0/0/1", "AR2": "GigabitEthernet0/0/0"}
    assert link("AR0", "LSW0") == {"AR0": "GigabitEthernet0/0/0", "LSW0": "GigabitEthernet0/0/1"}
    # AR2 的其余连线落在 Ethernet 口上（对端 AR1/3/4 未启动时这些口物理 down，与实测一致）
    assert link("AR2", "AR1")["AR2"] == "Ethernet0/0/0"


def test_real_mainboard_only_when_port_count_matches():
    xml = ('<dev model="AR1220"><slot number="slot17" isMainBoard="1">'
           '<interface sztype="Ethernet" interfacename="GE" count="3" /></slot></dev>')
    dev = topology.ET.fromstring(xml)
    # 主板接口数与真机（10）不一致：不套用，仍按文件推断
    assert topology._interfaces(dev, "router", "AR1220") == [
        "GigabitEthernet0/0/0", "GigabitEthernet0/0/1", "GigabitEthernet0/0/2"]
    # 未实测的型号按文件
    assert topology._interfaces(dev, "router", "AR2220") == [
        "GigabitEthernet0/0/0", "GigabitEthernet0/0/1", "GigabitEthernet0/0/2"]


def test_ospf_single_area():
    t = topology.parse_topo(str(OSPF))
    devs = {d["name"]: d for d in t["devices"]}
    assert set(devs) == {"R1", "R2", "R3", "CLOUD1"}
    assert {n: devs[n]["com_port"] for n in devs} == {"R1": 2000, "R2": 2001, "R3": 2002, "CLOUD1": 0}
    assert devs["R1"]["kind"] == "router" and devs["CLOUD1"]["kind"] == "cloud"
    assert devs["R1"]["interfaces"][:3] == ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1", "Ethernet0/0/0"]
    assert len(t["links"]) == 3
    r1_link = next(l for l in t["links"] if l["src_id"] == devs["R1"]["id"])
    assert r1_link["src_if"] == 0 and r1_link["src_label"] == "GigabitEthernet0/0/0"
    assert [n["text"] for n in t["notes"]] == [
        "Loopback:10.0.1.1/24", "Loopback:10.0.2.2/24", "Loopback:10.0.3.3/24"]
    assert devs["R1"]["x"] == 341.0 and devs["R1"]["y"] == 125.0
    assert t["name"] == "2-1Single-Area OSPF" and t["shapes"] == []


def test_ospf_troubleshooting_attribute_order():
    t = topology.parse_topo(str(TOPOS / "OSPF Troubleshooting.topo"))
    ports = {d["name"]: d["com_port"] for d in t["devices"]}
    assert ports == {"AR1": 2000, "AR2": 2001, "AR3": 2002, "AR4": 2003, "AR5": 2004, "LSW1": 2005}
    kinds = {d["name"]: d["kind"] for d in t["devices"]}
    assert kinds["LSW1"] == "switch" and kinds["AR1"] == "router"
    assert len(t["links"]) == 5


def test_campus_counts():
    t = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    assert len(t["devices"]) == 43
    text = (TOPOS / "Campus_Network.topo").read_text("ascii")
    assert text.count("<line ") == 76
    assert len(t["links"]) == 80  # 76 条 <line>，共 80 个 interfacePair（个别 line 有平行链路）
    assert len(topology.console_ports(t)) == 35
    assert {d["kind"] for d in t["devices"]} == {"router", "switch", "pc"}


def test_campus_shapes():
    t = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    text = (TOPOS / "Campus_Network.topo").read_text("ascii")
    assert text.count("<shape ") == 22
    kinds = [s["kind"] for s in t["shapes"]]
    # 22 个 <shape>：5 直线、14 矩形（其中 2 个宽高为 0 被过滤）、3 椭圆（WAN / Internet / PSTN）
    assert (text.count('<shape type="0"'), text.count('<shape type="1"'), text.count('<shape type="2"')) == (5, 14, 3)
    assert (kinds.count("line"), kinds.count("rect"), kinds.count("ellipse")) == (5, 12, 3)
    lines = [s for s in t["shapes"] if s["kind"] == "line"]
    assert (lines[0]["x1"], lines[0]["y1"], lines[0]["x2"], lines[0]["y2"]) == (333, 149, 987, 255)
    assert all(s["color"] == "#000000" and not s["fill"] and s["width"] == 1 for s in lines)
    wan = next(s for s in t["shapes"] if s["kind"] == "ellipse" and s["w"] == 471)
    assert (wan["x"], wan["y"], wan["h"], wan["fill"]) == (652, 5, 223, False)
    branches = next(s for s in t["shapes"] if s["kind"] == "rect" and (s["x"], s["y"]) == (20, 49))
    assert branches["fill"] and (branches["w"], branches["h"]) == (472, 230)
    depts = [s for s in t["shapes"] if s["kind"] == "rect" and not s["fill"]]
    assert len(depts) == 4 and {s["color"] for s in depts} == {"#ff0000"}
    assert {s["color"] for s in t["shapes"] if s["kind"] == "ellipse"} >= {"#50bbfc"}


@pytest.mark.parametrize("raw,rgb", [
    ("16744703", "#ff80ff"), ("255", "#ff0000"), ("16563024", "#50bbfc"),
    ("-7278960", "#90ee90"), ("-16777216", "#000000"), ("0", "#000000"), (None, "#000000"),
])
def test_colorref(raw, rgb):
    assert topology.colorref(raw) == rgb


def test_note_styles_and_multiline():
    t = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    n = next(n for n in t["notes"] if n["text"] == "Branches")
    assert (n["color"], n["bg"], n["size"], n["font"]) == ("#000000", "#90ee90", 100, "Consolas")
    assert (n["x"], n["y"], n["w"], n["h"]) == (385, 245, 72, 17)
    lab = topology.parse_topo(str(LAB))
    sc = next(n for n in lab["notes"] if n["text"].startswith("Scenario:"))
    assert sc["size"] == 90 and len(sc["lines"]) == 5 and sc["lines"][0] == "Scenario:"
    assert sc["text"].split("\n") == sc["lines"] and "\r" not in sc["text"]


def test_utf16_bom(tmp_path):
    p = tmp_path / "u16.topo"
    p.write_bytes(OSPF.read_text("ascii").encode("utf-16"))  # 带 BOM
    t = topology.parse_topo(str(p))
    assert len(t["devices"]) == 4 and len(t["links"]) == 3


def test_list_topologies(topo_dirs):
    items = topology.list_topologies()
    assert len(items) == len(ALL)
    ospf = next(t for t in items if t["name"] == "2-1Single-Area OSPF")
    assert ospf["device_count"] == 4 and ospf["console_count"] == 3
    assert {"path", "mtime", "ports"} <= set(ospf)


def test_gbk_note_decoded():
    t = topology.parse_topo(str(TOPOS / "Security Authentication.topo"))
    assert any("核心交换机" in n["text"] for n in t["notes"])


# ---- 按运行中的设备确定当前拓扑 ----

R1, R2, R3 = ("135C9ACE-B0CE-4a32-8DF2-2B227EBC16BE", "0C7965F9-2170-457e-8FB7-3259A23C33A8",
              "11F07A14-4D1C-4464-B567-D28996D405A1")
CLOUD1 = "A3B3E3D3-1BB3-4017-877D-FFDDD58EAE98"
GHOST = "00000000-1111-2222-3333-444444444444"


def dev_id(path, name):
    return next(d["id"] for d in topology.parse_topo(str(path))["devices"] if d["name"] == name)


def resolve():
    import asyncio
    return asyncio.run(topology.resolve_running())


def test_resolve_all_running(topo_dirs, fake_probe, monkeypatch):
    running(monkeypatch, R1, R2, R3)
    r = resolve()
    assert r["state"] == "running" and r["method"] == "override"
    assert r["topology"]["name"] == "2-1Single-Area OSPF" and r["duplicates"] == []
    st = {d["name"]: d["status"] for d in r["topology"]["devices"]}
    assert st == {"R1": "online", "R2": "online", "R3": "online", "CLOUD1": "na"}
    assert (r["running"], r["total"]) == (3, 3)
    assert r["message"] == "eNSP 当前拓扑：2-1Single-Area OSPF（运行 3 / 3）"


def test_resolve_partial_running(topo_dirs, fake_probe, monkeypatch):
    # 只开 R1、R2（小写 GUID 也能匹配）；不探测端口，运行中即 online
    running(monkeypatch, R1.lower(), R2)
    r = resolve()
    assert r["state"] == "running" and r["topology"]["name"] == "2-1Single-Area OSPF"
    assert r["devices"] == {R1: "online", R2: "online", R3: "stopped", CLOUD1: "na"}
    assert (r["running"], r["total"]) == (2, 3)
    assert "运行 2 / 3" in r["message"]


def test_resolve_none(topo_dirs, monkeypatch):
    running(monkeypatch)  # 已设置但为空：没有运行的设备
    r = resolve()
    assert r["state"] == "none" and r["topology"] is None and r["devices"] == {}
    assert "没有运行的设备" in r["message"]


def test_resolve_unknown(topo_dirs, monkeypatch):
    running(monkeypatch, GHOST)
    r = resolve()
    assert r["state"] == "unknown" and r["unknown_ids"] == [GHOST] and "未保存" in r["message"]
    # 已保存拓扑上新加设备后未保存：没有文件能包含全部，也按 unknown 提示先保存
    running(monkeypatch, R1, GHOST)
    assert resolve()["state"] == "unknown"


def test_resolve_mixed(topo_dirs, monkeypatch):
    bgp = TOPOS / "BGP3-6.topo"
    other = topology.parse_topo(str(bgp))["devices"][0]
    running(monkeypatch, R1, other["id"])
    r = resolve()
    assert r["state"] == "mixed" and r["topology"] is None
    got = {m["name"]: m["devices"] for m in r["mixed"]}
    assert got == {"2-1Single-Area OSPF": ["R1"], "BGP3-6": [other["name"]]}
    assert "分属不同拓扑" in r["message"]


def test_resolve_duplicates_pick_newest(tmp_path, monkeypatch, fake_probe):
    old, new = tmp_path / "Old copy.topo", tmp_path / "New copy.topo"
    shutil.copy(OSPF, old)
    shutil.copy(OSPF, new)
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    monkeypatch.setattr(config, "TOPO_DIRS", [str(tmp_path)])
    running(monkeypatch, R1, R2)
    r = resolve()
    assert r["state"] == "running" and r["topology"]["name"] == "New copy"
    assert r["duplicates"] == [{"name": "Old copy", "path": str(old.resolve())}]
    assert "另有 1 个文件" in r["message"]


def _dev(gid, name, model="AR1220"):
    return {"id": gid, "name": name, "model": model}


def test_match_running_pure():
    t1 = {"name": "A", "devices": [_dev("G1", "x"), _dev("G2", "y"), _dev("S1", "LSW1", "S5700")]}
    t2 = {"name": "B", "devices": [_dev("G3", "z"), _dev("S2", "LSW1", "S5700"), _dev("S3", "LSW2", "S3700")]}
    entries = [("a", 1.0, t1), ("b", 2.0, t2)]
    m = topology.match_running
    assert m(set(), set(), entries)["state"] == "none"
    assert m({"g1"}, set(), entries)["path"] == "a"
    assert m({"G1", "G3"}, set(), entries)["state"] == "mixed"
    assert m({"G9"}, set(), entries)["state"] == "unknown"
    assert m(set(), {"LSW9"}, entries)["state"] == "unknown"
    # 纯交换机：LSW1 两个文件都有 -> 按 recent 先后，其次修改时间新者
    r = m(set(), {"LSW1"}, entries)
    assert r["path"] == "b" and [d["name"] for d in r["duplicates"]] == ["A"]
    assert m(set(), {"LSW1"}, entries, ["a"])["path"] == "a"
    assert m(set(), {"LSW1", "LSW2"}, entries)["path"] == "b"
    # GUID 决定候选，交换机名辅助：G1 只在 a 里，LSW2 对不上也仍选 a，并报告没对上的交换机
    r = m({"G1"}, {"LSW2"}, entries)
    assert r["path"] == "a" and r["unmatched_switches"] == ["LSW2"]


def test_match_running_his_first(tmp_path):
    t1 = {"name": "A", "devices": [_dev("G1", "x"), _dev("S1", "LSW1", "S5700")]}
    t2 = {"name": "B", "devices": [_dev("G1", "x"), _dev("S2", "LSW1", "S5700")]}  # 副本
    entries = [("/x/a.topo", 1.0, t1), ("/x/b.topo", 9.0, t2)]
    m = topology.match_running
    # his 第一个文件核对通过：选它，即使另一个副本更新
    r = m({"G1"}, {"LSW1"}, entries, ["/x/a.topo", "/x/b.topo"])
    assert r["state"] == "running" and r["path"] == "/x/a.topo" and r["source"] == "his"
    # his 第一个文件对不上（没来得及更新）：退回 GUID 索引
    t3 = {"name": "C", "devices": [_dev("G7", "q")]}
    r = m({"G1"}, set(), entries + [("/x/c.topo", 5.0, t3)], ["/x/c.topo", "/x/a.topo"])
    assert r["source"] == "index" and r["path"] == "/x/a.topo"  # his 里排在前面的优先
    assert r["recent"] == {"name": "C", "path": "/x/c.topo"}
    # 没有运行证据：仍返回 eNSP 最近打开的拓扑
    r = m(set(), set(), entries, ["/x/b.topo"])
    assert r["state"] == "none" and r["topo"] is t2 and r["path"] == "/x/b.topo"
    assert m(set(), set(), entries, ["/x/missing.topo"])["topo"] is None


def test_resolve_pure_switch_topology_via_his(tmp_path, monkeypatch, fake_probe):
    # Lab 3-1 STP 全是 S 系列交换机，没有 VBox 设备；topo.his 第一个文件是它，交换机名对得上
    stp = TOPOS / "Lab 3-1 Configuring STP (Unconfigured).topo"
    devs = topology.parse_topo(str(stp))["devices"]
    sw = [d for d in devs if topology.is_process_switch(d["model"])]
    assert sw and all(d["com_port"] for d in sw)
    his = tmp_path / "topo.his"
    his.write_bytes((f"{stp}|{OSPF}|".encode("gbk")))
    monkeypatch.setattr(config, "TOPO_DIRS", [])  # 只靠 topo.his 也能找到文件
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(his))
    monkeypatch.setenv("AINET_ENSP_SWITCH_NAMES", sw[0]["name"])
    running(monkeypatch)
    fake_probe.clear()
    fake_probe.add(sw[0]["com_port"])
    r = resolve()
    assert r["state"] == "running" and r["topology"]["name"] == stp.stem and r["source"] == "his"
    assert r["devices"][sw[0]["id"]] == "online"
    assert all(r["devices"][d["id"]] == "stopped" for d in sw[1:])
    assert (r["running"], r["switch_method"]) == (1, "override")


def test_resolve_none_returns_recent_topology(tmp_path, monkeypatch, topo_dirs):
    his = tmp_path / "topo.his"
    his.write_bytes(f"{TOPOS / 'Campus_Network.topo'}|C:\\不存在\\x.topo|".encode("gbk"))
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(his))
    running(monkeypatch)
    r = resolve()
    assert r["state"] == "none" and r["topology"]["name"] == "Campus_Network"
    assert r["recent"]["name"] == "Campus_Network" and r["running"] == 0
    assert set(r["devices"].values()) == {"stopped", "na"}
    assert r["message"].startswith("eNSP 最近打开：Campus_Network") and "没有运行的设备" in r["message"]
    assert topology.allowed_ports(topology.current_running()) == set()


def test_resolve_campus_routers_and_switches(topo_dirs, fake_probe, monkeypatch):
    campus = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    ars = [d for d in campus["devices"] if d["model"].startswith("AR")]
    sws = [d for d in campus["devices"] if topology.is_process_switch(d["model"])]
    running(monkeypatch, *(d["id"] for d in ars))
    monkeypatch.setenv("AINET_ENSP_SWITCH_NAMES", ",".join(d["name"] for d in sws[:20]))
    fake_probe.update(d["com_port"] for d in ars + sws)
    r = resolve()
    assert r["state"] == "running" and r["topology"]["name"] == "Campus_Network"
    assert (r["running"], r["total"]) == (28, 35)
    st = {d["name"]: d["status"] for d in r["topology"]["devices"]}
    assert all(st[d["name"]] == "online" for d in ars + sws[:20])
    assert all(st[d["name"]] == "stopped" for d in sws[20:])
    assert all(st[d["name"]] == "na" for d in campus["devices"] if d["kind"] == "pc")


# ---- ensp_runtime ----

def test_parse_switch_cmdline():
    line = ('"C:\\Program Files\\Huawei\\eNSP\\VBoxServer\\Devices\\Lsw\\s5700\\eNSP_Switch.exe" '
            '"sim" "system_mac=4C-1F-CC-18-4E-55" "LSW0"')
    assert ensp_runtime.parse_switch_cmdline(line) == "LSW0"
    assert ensp_runtime.parse_switch_cmdline(line.replace('"LSW0"', '"Core SW 1"')) == "Core SW 1"
    assert ensp_runtime.parse_switch_cmdline('"C:\\x\\eNSP_Switch.exe" "sim"') is None
    assert ensp_runtime.parse_switch_cmdline('"C:\\x\\other.exe" "LSW0"') is None
    assert ensp_runtime.parse_switch_cmdline("") is None


def test_parse_process_list():
    text = ('eNSP_Client.exe|"C:\\Program Files\\Huawei\\eNSP\\eNSP_Client.exe"\r\n'
            'eNSP_Switch.exe|"C:\\x\\eNSP_Switch.exe" "sim" "system_mac=4C-1F-CC-18-4E-55" "LSW0"\r\n'
            'eNSP_Switch.exe|"C:\\x\\eNSP_Switch.exe" "sim" "system_mac=4C-1F-CC-18-4E-56" "LSW1"\r\n'
            'eNSP_Switch.exe|\r\n')  # 读不到命令行的进程
    assert ensp_runtime.parse_process_list(text) == ({"LSW0", "LSW1"}, True)
    assert ensp_runtime.parse_process_list("") == (set(), False)


def test_ensp_client_override(monkeypatch):
    for raw, want in (("1", True), ("0", False), ("", False)):
        monkeypatch.setenv("AINET_ENSP_CLIENT", raw)
        assert ensp_runtime.ensp_client_running() is want
    monkeypatch.delenv("AINET_ENSP_CLIENT")
    if os.name != "nt":
        assert ensp_runtime.ensp_client_running() is None


def test_resolve_ensp_not_running(tmp_path, monkeypatch, topo_dirs, client):
    # eNSP 已退出：残留证据（如 VBox 锁未释放）也不算，none + 提示 eNSP 未运行，仍给出最近打开的拓扑
    his = tmp_path / "topo.his"
    his.write_bytes(f"{OSPF}|".encode("gbk"))
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(his))
    monkeypatch.setenv("AINET_ENSP_CLIENT", "0")
    running(monkeypatch, R1)
    r = resolve()
    assert r["state"] == "none" and r["ensp_running"] is False
    assert r["message"] == "eNSP 未运行，请先启动 eNSP 并打开拓扑（最近打开：2-1Single-Area OSPF）"
    assert r["topology"]["name"] == "2-1Single-Area OSPF" and r["running"] == 0
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", "")
    assert resolve()["message"] == "eNSP 未运行，请先启动 eNSP 并打开拓扑"
    # 白名单拒绝并说明 eNSP 未运行
    monkeypatch.setattr(main, "EnspConnector", FailConnector)
    login(client)
    msg = ws_first_text(client, 2000)
    assert "白名单" in msg and "eNSP 未运行" in msg
    # eNSP 在运行：照常按证据判定
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")
    assert resolve()["state"] == "running"


def test_switch_names_override(monkeypatch):
    monkeypatch.setenv("AINET_ENSP_SWITCH_NAMES", "LSW1, LSW2,")
    assert ensp_runtime.running_switch_names() == ({"LSW1", "LSW2"}, "override")
    monkeypatch.delenv("AINET_ENSP_SWITCH_NAMES")
    if os.name != "nt":
        assert ensp_runtime.running_switch_names() == (set(), "unsupported")


def test_topo_his(tmp_path, monkeypatch):
    raw = "C:\\Program Files\\Huawei\\eNSP\\examples\\Campus_Network\\Campus_Network.topo|D:\\Users\\15111\\桌面\\实验 1.topo|  |".encode("gbk")
    assert ensp_runtime.parse_topo_his(raw) == [
        "C:\\Program Files\\Huawei\\eNSP\\examples\\Campus_Network\\Campus_Network.topo",
        "D:\\Users\\15111\\桌面\\实验 1.topo"]
    # 缺省位置：devices 目录往上两级（eNSP 根目录）下的 tmp\topo.his
    root = tmp_path / "eNSP"
    (root / "VBoxServer" / "devices").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "tmp" / "topo.his").write_bytes(raw)
    monkeypatch.delenv("AINET_ENSP_TOPO_HIS")
    monkeypatch.setenv("AINET_ENSP_DEVICES_DIRS", str(root / "VBoxServer" / "devices"))
    assert ensp_runtime.topo_his_path() == str(root / "tmp" / "topo.his")
    assert len(ensp_runtime.recent_topologies()) == 2
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(tmp_path / "missing.his"))
    assert ensp_runtime.recent_topologies() == []


def test_scan_includes_his_files_outside_topo_dirs(tmp_path, monkeypatch):
    outside = tmp_path / "desk" / "m0.topo"
    outside.parent.mkdir()
    shutil.copy(OSPF, outside)
    monkeypatch.setattr(config, "TOPO_DIRS", [str(TOPOS)])
    his = tmp_path / "topo.his"
    his.write_bytes(f"{outside}|{tmp_path / 'gone.topo'}|{tmp_path / 'x.txt'}|".encode("gbk"))
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(his))
    running(monkeypatch, R1, R2)
    cur = topology.current_running()
    # 副本 m0.topo 在 his 第一位且核对通过：选它而不是 TOPO_DIRS 里的原件
    assert cur["state"] == "running" and cur["path"] == str(outside.resolve()) and cur["source"] == "his"

def test_runtime_override_and_unsupported(monkeypatch):
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", " a-b , C-D ,")
    rt = ensp_runtime.running_device_ids()
    assert rt.ids == {"A-B", "C-D"} and rt.method == "override"
    monkeypatch.delenv("AINET_ENSP_RUNNING_IDS")
    if os.name != "nt":
        rt = ensp_runtime.running_device_ids()
        assert rt.ids == set() and rt.method == "unsupported"


def test_parse_runningvms():
    text = ('"AR_Clone_135C9ACE-B0CE-4a32-8DF2-2B227EBC16BE" {abc}\n'
            '"S5700_Clone_0C7965F9-2170-457e-8FB7-3259A23C33A8" {def}\n"other vm" {x}\n')
    assert ensp_runtime.parse_runningvms(text) == {R1.upper(), R2.upper()}


def test_lockfile_ids_branches(tmp_path):
    base = tmp_path / "devices"
    logs = {
        R1: "AR_Clone_" + R1,          # 被占用 -> 运行
        R2: "S5700_Clone_" + R2,       # 型号前缀不同也能匹配
        R3: "AR_Clone_" + R3,          # 能打开 -> 未运行
        CLOUD1: "X_Clone_" + CLOUD1,   # 其他错误码 -> 不算运行
    }
    for gid, sub in logs.items():
        p = base / gid / sub / "Logs"
        p.mkdir(parents=True)
        (p / "VBox.log").write_text("x")
    (base / "not-a-guid").mkdir()
    (base / GHOST).mkdir()  # 没有 VBox.log
    verdict = {R1: ensp_runtime.OPEN_SHARING, R2: ensp_runtime.OPEN_SHARING,
               R3: ensp_runtime.OPEN_OK, CLOUD1: ("error", 5)}
    calls = []

    def probe(path):
        calls.append(path)
        return next(v for g, v in verdict.items() if g in path)

    detail = {}
    ids = ensp_runtime.lockfile_ids([str(base), str(tmp_path / "missing")], probe, detail)
    assert ids == {R1.upper(), R2.upper()}
    assert len(calls) == 4
    assert "无 VBox.log" in detail[GHOST.upper()] and "('error', 5)" in detail[CLOUD1.upper()]


def test_devices_dirs_env(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("AINET_ENSP_DEVICES_DIRS", os.pathsep.join([str(a), str(b), str(a), str(tmp_path / "x")]))
    assert ensp_runtime.devices_dirs() == [str(a), str(b)]


@pytest.mark.skipif(os.name != "nt", reason="CreateFileW 只在 Windows 上可用")
def test_win_open_probe_real(tmp_path):
    p = tmp_path / "VBox.log"
    p.write_text("x")
    assert ensp_runtime._win_open_probe(str(p)) == ensp_runtime.OPEN_OK
    with open(p, "a") as f:  # 有人以写方式打开 -> 只读且拒绝他人写的打开会共享冲突
        f.write("y")
        assert ensp_runtime._win_open_probe(str(p)) == ensp_runtime.OPEN_SHARING


def test_runtime_cache(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return ensp_runtime.Runtime({"G"}, "lockfile")

    monkeypatch.setattr(ensp_runtime, "_detect", fake)
    assert ensp_runtime.running_device_ids().ids == {"G"}
    assert ensp_runtime.running_device_ids().method == "lockfile"
    assert len(calls) == 1


# ---- API ----

def test_topology_api_requires_login(client, topo_dirs):
    assert client.get("/api/topologies").status_code == 401
    assert client.get("/api/topology/current").status_code == 401
    assert client.get("/api/topology/status").status_code == 401


def test_removed_endpoints(client, topo_dirs):
    login(client)
    assert client.put("/api/topology/current", json={"path": str(OSPF)}).status_code in (404, 405)
    assert client.post("/api/topology/detect").status_code in (404, 405)


def test_topology_api_structure(client, topo_dirs, fake_probe, monkeypatch):
    login(client)
    cur = client.get("/api/topology/current").json()
    assert cur["state"] == "none" and cur["topology"] is None and "没有运行的设备" in cur["message"]

    running(monkeypatch, R1, R2)
    cur = client.get("/api/topology/current").json()
    assert cur["state"] == "running" and cur["method"] == "override"
    t = cur["topology"]
    assert set(t) == {"name", "path", "devices", "links", "shapes", "notes"}
    assert {"id", "name", "model", "com_port", "x", "y", "kind", "interfaces", "status"} <= set(t["devices"][0])
    assert {d["name"]: d["status"] for d in t["devices"]} == {
        "R1": "online", "R2": "online", "R3": "stopped", "CLOUD1": "na"}

    st = client.get("/api/topology/status").json()
    assert "topology" not in st and st["path"] == cur["path"] and st["mtime"] == cur["mtime"]
    assert st["devices"] == {d["id"]: d["status"] for d in t["devices"]}
    assert (st["running"], st["total"]) == (2, 3)

    items = client.get("/api/topologies").json()
    assert len(items) == len(ALL)


# ---- 动态白名单 ----

class FailConnector:
    """替身：一定连不上，用来证明请求越过了白名单检查。"""

    def __init__(self, host, port, timeout):
        self.port = port

    async def connect(self):
        raise ConnectionRefusedError("fake")

    async def close(self):
        pass


def ws_first_text(client, port):
    with client.websocket_connect(f"/ws/console/{port}") as ws:
        msg = ws.receive_text()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    return msg


def test_whitelist_follows_running_topology(client, topo_dirs, monkeypatch):
    monkeypatch.setattr(main, "EnspConnector", FailConnector)
    login(client)
    # 没有运行的设备：一个端口都不放行，并说明原因
    for port in (2000, 2002):
        msg = ws_first_text(client, port)
        assert "白名单" in msg and "没有运行的设备" in msg
    # 2-1 OSPF 运行中（只开 R1）：本拓扑 console 端口放行（含已停止的 R3），其他拒绝
    running(monkeypatch, R1)
    assert "连接失败" in ws_first_text(client, 2000)
    assert "连接失败" in ws_first_text(client, 2002)
    msg = ws_first_text(client, 2005)
    assert "白名单" in msg and "2-1Single-Area OSPF" in msg and "[2000, 2001, 2002]" in msg
    # OSPF Troubleshooting 运行中（2000-2005）：2005 放行
    running(monkeypatch, dev_id(TOPOS / "OSPF Troubleshooting.topo", "LSW1"))
    assert "连接失败" in ws_first_text(client, 2005)
    assert "白名单" in ws_first_text(client, 2006)
    # 未保存的拓扑：拒绝并提示保存
    running(monkeypatch, GHOST)
    msg = ws_first_text(client, 2000)
    assert "白名单" in msg and "未保存" in msg


def test_scan_skips_links(tmp_path, monkeypatch):
    # 链接/联接点不跟进；扫描上层目录时也不收录本项目自带的测试样例
    (tmp_path / "real").mkdir()
    shutil.copy(TOPOS / "BGP3-6.topo", tmp_path / "real" / "a.topo")
    try:
        (tmp_path / "link").symlink_to(TOPOS, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account does not have symbolic-link privileges; covered on Linux CI")
        raise
    monkeypatch.setattr(config, "TOPO_DIRS", [str(tmp_path)])
    names = {t["name"] for t in topology.list_topologies()}
    assert names == {"a"}


def test_scan_skips_app_dir(monkeypatch):
    app_parent = str(Path(topology._APP_ROOT).parent)
    monkeypatch.setattr(topology, "_MAX_DEPTH", 2)
    monkeypatch.setattr(config, "TOPO_DIRS", [app_parent])
    assert not any(topology._APP_ROOT in os.path.normcase(t["path"]) for t in topology.list_topologies())


def test_switch_workspace_lock(tmp_path):
    # 交换机：<eNSP>\VBoxServer\workspaces\<GUID>\slot17\flash.dat 被占用即运行（不依赖进程命令行权限）
    devices = tmp_path / "VBoxServer" / "devices"
    devices.mkdir(parents=True)
    ws = ensp_runtime.workspace_dirs([str(devices)])
    assert ws == [str(tmp_path / "VBoxServer" / "workspaces")]
    on, off = "DE880FB4-C0C7-4dc4-848F-7BEB833A9BB4", "11111111-2222-3333-4444-555555555555"
    for gid in (on, off):
        p = tmp_path / "VBoxServer" / "workspaces" / gid / "slot17"
        p.mkdir(parents=True)
        (p / "flash.dat").write_text("x")
        (p / "commands.xml").write_text("x")  # 其他文件不参与判断

    def probe(path):
        assert path.endswith("flash.dat")
        return ensp_runtime.OPEN_SHARING if on in path else ensp_runtime.OPEN_OK

    assert ensp_runtime.lockfile_ids(ws, probe, None, ("*", "flash.dat")) == {on.upper()}


def test_switch_running_by_guid_without_process_names(topo_dirs, fake_probe, monkeypatch):
    # 普通权限读不到 eNSP_Switch 命令行（switches 为空）时，交换机仍可凭 workspace GUID 判为运行
    camp = topology.parse_topo(str(TOPOS / "Campus_Network.topo"))
    lsw0 = next(d for d in camp["devices"] if d["name"] == "LSW0")
    ar0 = next(d for d in camp["devices"] if d["name"] == "AR0")
    his = TOPOS.parent / "campus.his"
    his.write_bytes((str(TOPOS / "Campus_Network.topo") + "|").encode("gbk"))
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", str(his))
    running(monkeypatch, ar0["id"], lsw0["id"])
    monkeypatch.setenv("AINET_ENSP_SWITCH_NAMES", "")
    try:
        r = resolve()
    finally:
        his.unlink()
    assert r["state"] == "running" and r["topology"]["name"] == "Campus_Network"
    assert r["devices"][lsw0["id"]] == "online" and r["devices"][ar0["id"]] == "online"
    assert r["running"] == 2
