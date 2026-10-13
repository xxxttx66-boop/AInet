"""场景库：加载、校验（禁止命令 / 只读 checks / 设备引用）、拓扑比对与就绪判断。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import config
import scenarios as sc
import topology

ROOT = Path(__file__).resolve().parent.parent.parent
TOPOS = Path(__file__).resolve().parent / "fixtures" / "topos"
CAMPUS = json.loads((ROOT / "scenarios" / "campus-ar0-ar2-link.json").read_text(encoding="utf-8"))
OSPF = json.loads((ROOT / "scenarios" / "ospf-2-1-r1-shutdown.json").read_text(encoding="utf-8"))


@pytest.fixture
def topos(monkeypatch):
    monkeypatch.setattr(config, "TOPO_DIRS", [str(TOPOS)])


def campus_topo():
    return topology.parse_topo(str(TOPOS / "Campus_Network.topo"))


def errs_of(s, topo=None) -> str:
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate(s, topo)
    return str(exc.value)


# 题库规划（docs/question-bank-plan.md）第一轮新写的 17 道题：先以草稿（"draft": true，课程页缺省不列出）提交，
# 真机验证通过（docs/question-bank-verify.md）后去掉 draft、记进 VERIFIED；其余仍是草稿
BANK_TEST = {"enspro-t2-port-shutdown", "enspro-t2-ip-wrong-port", "enspro-t2-mask-mismatch",
             "enspro-t1-static-route-dest", "enspro-t1-t2-two-faults"}
BANK_CAMPUS = {"campus-lsw0-port-shutdown-ys", "campus-pc1-gateway-ys", "campus-ar0-dot1q-trunk-ys",
               "ospf-r2-hello-mismatch-ys", "ospf-r3-area-mismatch-ys", "campus-ar2-acl-telnet-ys",
               "campus-ar0-dhcp-select-ys", "campus-ar0-dhcp-exhausted-ys", "campus-r1-dns-host-ys",
               "campus-ar2-easyip-acl-ys", "campus-ar2-nat-static-ys", "campus-mgmt-two-faults-ys"}
BANK = BANK_TEST | BANK_CAMPUS
VERIFIED = {  # 2026-10-07 本机 eNSP Pro 真机验证通过、已上线
    "enspro-t1-t2-two-faults",
    "enspro-t1-static-route-dest",
    "enspro-t2-mask-mismatch",
    "enspro-t2-ip-wrong-port",
    "enspro-t2-port-shutdown",
    "campus-ar2-nat-static-ys",
    "campus-ar2-easyip-acl-ys",
    "campus-mgmt-two-faults-ys",
    "campus-r1-dns-host-ys",
    "campus-ar0-dhcp-exhausted-ys",
    "campus-ar0-dhcp-select-ys",
    "campus-ar2-acl-telnet-ys",
    "ospf-r2-hello-mismatch-ys",
    "ospf-r3-area-mismatch-ys",
    "campus-ar0-dot1q-trunk-ys",
    "campus-pc1-gateway-ys",
    "campus-lsw0-port-shutdown-ys",
}
DRAFTS_TEST = BANK_TEST - VERIFIED
DRAFTS_CAMPUS = BANK_CAMPUS - VERIFIED
DRAFTS = DRAFTS_TEST | DRAFTS_CAMPUS


def test_repo_scenarios_load_and_validate(topos):
    loaded, errors = sc.load_dir(str(ROOT / "scenarios"))
    assert errors == []
    vrp5 = {"campus-ar0-ar2-link", "ospf-2-1-r1-shutdown", "campus-lsw0-access-vlan",
            "campus-ar2-return-route", "campus-ar2-acl-block"}
    ys = {"enspro-t1-t2-link", "campus-ar0-ar2-link-ys", "campus-lsw0-access-vlan-ys", "campus-ar2-return-route-ys",
          "campus-ar2-acl-block-ys", "ospf-r1-shutdown-ys"}
    assert set(loaded) == vrp5 | ys | BANK
    assert {k for k, v in loaded.items() if v.get("platform", "vrp5") == "yunshan"} == ys | BANK
    # 题库扩充题（docs/question-bank-plan.md）：YunShan、接口缩写、只用现有两个沙箱；没真机验证过的是 draft（note 写明），
    # 验证过的不是；其他已上线的题都不是草稿
    assert VERIFIED <= BANK
    assert {k for k, v in loaded.items() if v.get("draft")} == DRAFTS
    for sid in BANK:
        s = loaded[sid]
        assert s["platform"] == "yunshan" and s["topology"] in ("ainet_test", "ainet_campus"), sid
        assert ("未真机验证" in s["note"]) == (sid in DRAFTS), sid
        cmds = [c for f in ("baseline", "inject", "restore", "solution") for v in s[f].values() for c in v]
        assert not any("GigabitEthernet" in c for c in cmds), sid
        assert set(s["baseline"]) | set(s["inject"]) <= set(s["restore"]), sid
    # YunShan 版（eNSP Pro 沙箱 ainet_campus）：接口只认缩写（GigabitEthernet 报 Wrong parameter，2026-10-06 实测），参数与原题一致
    for sid in ys - {"enspro-t1-t2-link"}:
        s = loaded[sid]
        assert s["topology"] == "ainet_campus"
        cmds = [c for f in ("baseline", "inject", "restore", "solution") for v in s[f].values() for c in v]
        assert not any("GigabitEthernet" in c for c in cmds), sid
        old = loaded.get(sid.removesuffix("-ys"))
        if old is not None:
            assert s.get("params") == old.get("params"), sid
    v = loaded["campus-lsw0-access-vlan-ys"]
    assert v["hosts"] == ["PC1"] and v["baseline"]["PC1"] == ["ip address 10.0.10.10 24", "ip gateway 10.0.10.1"]
    assert any(c["device"] == "PC1" and c["command"].startswith("ping ") for c in v["checks"])
    assert "interface GE1/0/1" in v["inject"]["LSW0"]
    # Campus 题目共用同一基础拓扑（spec §6.2 第 1 条），按真机连线出题
    for sid in ("campus-lsw0-access-vlan", "campus-ar2-return-route", "campus-ar2-acl-block"):
        s = loaded[sid]
        assert s["topology"] == "Campus_Network.topo" and "LSW0" in s["required_devices"]
        assert s["level"] in (2, 3) and s["knowledge_tags"]
        assert set(s["baseline"]) | set(s["inject"]) <= set(s["restore"])
    o = loaded["ospf-2-1-r1-shutdown"]
    assert o["inject"] == {"R1": ["system-view", "interface GigabitEthernet0/0/0", "shutdown"]}
    assert [c["command"] for c in o["checks"]] == ["display ip interface brief", "display ospf peer brief"]


def test_load_dir_reports_bad_file_without_blocking_others(tmp_path, topos):
    (tmp_path / "a.json").write_text(json.dumps(CAMPUS), encoding="utf-8")
    bad = copy.deepcopy(CAMPUS)
    bad["id"] = "bad"
    bad["inject"]["AR2"].append("reboot")
    (tmp_path / "b.json").write_text(json.dumps(bad), encoding="utf-8")
    (tmp_path / "c.json").write_text("{not json", encoding="utf-8")
    loaded, errors = sc.load_dir(str(tmp_path))
    assert set(loaded) == {"campus-ar0-ar2-link"}
    assert len(errors) == 2 and any("reboot" in e for e in errors)


@pytest.mark.parametrize("cmd", [
    "user-interface console 0", "user-interface con 0", "reboot", "reset saved-configuration",
    "undo info-center enable", "save", "quit", "sysname X", "undo user-interface vty 0 4",
    "  Reboot  fast", "authentication-mode none",
])
def test_forbidden_commands(cmd):
    assert sc.forbidden_reason(cmd)
    s = copy.deepcopy(CAMPUS)
    s["inject"]["AR2"].append(cmd)
    assert "禁止命令" in errs_of(s)


def test_allowed_config_commands_not_forbidden():
    for cmd in ("system-view", "interface GigabitEthernet0/0/0", "shutdown", "undo shutdown",
                "ip address 10.0.12.1 24", "undo ip address", "return", "ospf 1"):
        assert sc.forbidden_reason(cmd) is None, cmd


def test_checks_must_be_readonly():
    for ok in ("display ip interface brief", "dis ospf peer brief", "ping -c 2 10.0.12.2", "tracert 1.1.1.1"):
        assert sc.is_readonly(ok)
    for bad in ("undo shutdown", "system-view", "displayx", "reset counters interface"):
        assert not sc.is_readonly(bad)
        s = copy.deepcopy(CAMPUS)
        s["checks"][0]["command"] = bad
        assert "只读命令" in errs_of(s)


def test_device_reference_rules():
    s = copy.deepcopy(CAMPUS)
    s["inject"]["AR9"] = ["system-view"]
    assert "AR9 不在 required_devices" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    s["accessible_devices"].append("LSW0")
    assert "accessible_devices 中的 LSW0" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    del s["restore"]["AR2"]
    assert "restore 必须给出 AR2" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    s["checks"][0]["device"] = "LSW0"
    assert "checks[0] 的设备 LSW0" in errs_of(s)


def test_shape_rules():
    s = copy.deepcopy(CAMPUS)
    s["hints"] = s["hints"][:2]
    assert "hints" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    s["checks"][0]["expect"] = "([unclosed"
    assert "正则" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    del s["symptom"]
    assert "缺少字段 symptom" in errs_of(s)
    s = copy.deepcopy(CAMPUS)
    s["inject"] = {}
    assert "inject 不能为空" in errs_of(s)


def test_devices_checked_against_topology():
    topo = campus_topo()
    sc.validate(CAMPUS, topo)
    s = copy.deepcopy(CAMPUS)
    s["required_devices"].append("R1")
    assert "设备 R1 不在拓扑" in errs_of(s, topo)
    o = copy.deepcopy(OSPF)
    o["required_devices"].append("CLOUD1")
    assert "CLOUD1 没有 console" in errs_of(o, topology.parse_topo(str(TOPOS / "2-1Single-Area OSPF.topo")))


def test_topo_key_robust():
    assert sc.topo_key(r"C:\Program Files\Huawei\eNSP\examples\Campus_Network\Campus_Network.topo") == "campus_network"
    assert sc.topo_key("Campus_Network") == sc.topo_key("campus_network.TOPO")
    assert sc.topo_key("/x/2-1Single-Area OSPF.topo") == "2-1single-area ospf"


def _cur(state="running", online=("AR0", "AR2"), topo=None):
    t = topo or campus_topo()
    t = {**t, "devices": [{**d, "status": ("online" if d["name"] in online else "stopped")
                           if d["com_port"] > 0 else "na"} for d in t["devices"]]}
    return {"state": state, "message": "eNSP 中没有运行的设备", "topology": t if state == "running" else None}


def test_readiness():
    s = sc.validate(CAMPUS)
    r = sc.readiness(s, _cur())
    assert r == {"ready": True, "reason": "", "ports": {"AR0": 2000, "AR2": 2003}}
    r = sc.readiness(s, _cur(online=("AR0",)))
    assert not r["ready"] and "请在 eNSP 中启动 AR2" in r["reason"]
    r = sc.readiness(s, _cur(state="none"))
    assert not r["ready"] and "请在 eNSP 打开 Campus_Network 并启动 AR0、AR2" in r["reason"]
    o = sc.validate(OSPF)
    r = sc.readiness(o, _cur())
    assert not r["ready"] and "不是本题的 2-1Single-Area OSPF" in r["reason"] and "R1、R2、R3" in r["reason"]


def test_public_hides_answers():
    p = sc.public(sc.validate(CAMPUS))
    text = json.dumps(p, ensure_ascii=False)
    assert "ping -c" not in text and "10.0.21.2" not in text and "undo ip" not in text
    assert p["checks_total"] == 1 and p["hints_total"] == 3
