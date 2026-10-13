"""参数化场景（里程碑 6）：按种子确定性展开、种子 0 与原题一致、抽样校验（种子 + 边界值）、引用检查、
正则转义、注入值不等于修复值；练习记录种子并可复现、服务重启后按种子恢复。"""
from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path

import pytest

import config
import params as P
import scenarios as sc
from practice_helpers import end_practice

ROOT = Path(__file__).resolve().parent.parent.parent
OLD = Path(__file__).resolve().parent / "fixtures" / "scenarios_m5"
TOPOS = Path(__file__).resolve().parent / "fixtures" / "topos"
PARAM_IDS = ("campus-ar0-ar2-link", "campus-lsw0-access-vlan", "campus-ar2-return-route", "campus-ar2-acl-block")


@pytest.fixture(scope="module")
def lib():
    loaded, errors = sc.load_dir(str(ROOT / "scenarios"), [])
    assert errors == []
    return loaded


def raw(sid: str) -> dict:
    return json.loads((ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))


def base_scenario(**extra) -> dict:
    """最小的参数化场景（基于互联题）。"""
    s = raw("campus-ar0-ar2-link")
    s.update(extra)
    return s


def errs_of(s) -> str:
    with pytest.raises(sc.ScenarioError) as exc:
        sc.validate(s)
    return str(exc.value)


# ---- 展开 ----

def test_at_least_two_campus_scenarios_are_parametrized(lib):
    ps = [k for k, s in lib.items() if s.get("params")]
    assert set(PARAM_IDS) <= set(ps)
    assert all(lib[k]["topology"] == "Campus_Network.topo" for k in ps if lib[k].get("platform", "vrp5") == "vrp5")
    # eNSP Pro（YunShan）版的互联题沿用同样的参数化
    assert lib["enspro-t1-t2-link"]["platform"] == "yunshan" and set(lib["enspro-t1-t2-link"]["params"]) == {"link", "wrong"}


@pytest.mark.parametrize("sid", PARAM_IDS)
def test_expand_is_deterministic(lib, sid):
    s = lib[sid]
    a = sc.expand(s, 12345)
    random.seed(999)                       # 全局随机状态不影响展开
    random.random()
    b = sc.expand(s, 12345)
    assert a == b
    assert a["seed"] == 12345 and a["param_values"]
    assert "params" not in a
    # 不同种子一般给出不同取值
    vals = {json.dumps(sc.expand(s, k)["param_values"], sort_keys=True) for k in range(1, 30)}
    assert len(vals) > 5


@pytest.mark.parametrize("sid", PARAM_IDS)
def test_seed_zero_equals_original(lib, sid):
    """种子 0 = 默认值：命令、判定、工单、评分要点与参数化之前的原题逐字一致（便于回归）。"""
    old = sc.validate(json.loads((OLD / f"{sid}.json").read_text(encoding="utf-8")))
    e0 = sc.expand(lib[sid], 0)
    for k in ("baseline", "inject", "restore", "solution", "checks", "symptom", "reference_path",
              "scoring_points", "title", "required_devices", "accessible_devices"):
        assert e0[k] == old[k], k
    if sid == "campus-ar0-ar2-link":       # 第 3 级提示措辞：「网段写反了」只对默认值成立，改成通用说法
        assert e0["hints"][:2] == old["hints"][:2]
        assert "10.0.21.2/24" in e0["hints"][2] and "ip address 10.0.12.2 24" in e0["hints"][2]
    else:
        assert e0["hints"] == old["hints"]


def test_expanded_fields_have_no_placeholders(lib):
    for sid in PARAM_IDS:
        for seed in (0, 1, 7, 2**31 - 2):
            e = sc.expand(lib[sid], seed)
            text = json.dumps({k: e[k] for k in ("baseline", "inject", "restore", "solution", "checks", "symptom",
                                                  "hints", "reference_path", "scoring_points")}, ensure_ascii=False)
            assert not P.PLACEHOLDER.search(text.replace("\\\\d{", "")), (sid, seed)


@pytest.mark.parametrize("seed", range(0, 300, 7))
def test_injected_fault_always_differs_from_fix(lib, seed):
    """抽样以外的种子也不会出现「注入值 = 正确值」（故障不生效）。"""
    link = sc.expand(lib["campus-ar0-ar2-link"], seed)["param_values"]
    assert link["wrong"] != link["link"] and link["link"] != 20
    vlan = sc.expand(lib["campus-lsw0-access-vlan"], seed)["param_values"]["wrong_vlan"]
    assert vlan not in (1, 10) and 2 <= vlan <= 999
    hop = sc.expand(lib["campus-ar2-return-route"], seed)["param_values"]["bad_hop"]
    assert hop not in (0, 1, 2, 255)
    acl = sc.expand(lib["campus-ar2-acl-block"], seed)
    assert 1 <= acl["param_values"]["rule"] < 10 and 3000 <= acl["param_values"]["acl"] <= 3999
    for sid in PARAM_IDS:
        e = sc.expand(lib[sid], seed)
        for dev, cmds in e["inject"].items():
            fixes = set(e["solution"].get(dev, []))
            assert not [c for c in cmds if P.refs(c) is not None and c in fixes and not P._VIEW_RE.match(c)], sid


def test_expect_values_are_regex_escaped():
    s = base_scenario()
    s["params"]["addr"] = {"type": "format", "value": "10.0.{link}.2"}
    s["checks"] = [{"device": "AR0", "command": "ping -c 2 {addr}",
                    "expect": "from {addr}: bytes", "desc": "ping 10.0.{link}.2"}]
    t = sc.validate(s)
    e = sc.expand(t, 0)
    assert e["checks"][0]["expect"] == r"from 10\.0\.12\.2: bytes"   # 代入的值转义，模板里的其他部分原样
    assert e["checks"][0]["command"] == "ping -c 2 10.0.12.2"     # 命令不转义
    assert re.search(e["checks"][0]["expect"], "Reply from 10.0.12.2: bytes=56")
    assert not re.search(e["checks"][0]["expect"], "Reply from 10.0.1252: bytes=56")


def test_param_types_and_derivation():
    s = base_scenario()
    s["params"] = {
        "link": {"type": "int", "min": 11, "max": 99, "default": 12, "exclude": [20]},
        "wrong": {"type": "int", "min": 11, "max": 99, "default": 21, "exclude": ["{link}"]},
        "net": {"type": "subnet", "within": "10.1.0.0/16", "prefix": 24, "default": "10.1.5.0",
                "exclude": ["10.1.0.0/24"]},
        "gw": {"type": "host", "net": "{net}/24", "index": 1},
        "peer": {"type": "ip", "net": "{net}/24", "default": "10.1.5.9", "exclude": ["{gw}"]},
        "proto": {"type": "choice", "values": ["icmp", "ip"], "default": "icmp"},
        "label": {"type": "format", "value": "L{link}-{proto}"},
    }
    s["note"] = "{gw} {peer} {label}"
    t = sc.validate(s)
    v0 = P.values_for(t, 0)
    assert v0 == {"link": 12, "wrong": 21, "net": "10.1.5.0", "gw": "10.1.5.1", "peer": "10.1.5.9",
                  "proto": "icmp", "label": "L12-icmp"}
    for seed in range(1, 60):
        v = P.values_for(t, seed)
        assert v["net"].startswith("10.1.") and v["net"].endswith(".0") and v["net"] != "10.1.0.0"
        assert v["gw"] == v["net"][:-1] + "1"
        assert v["peer"] != v["gw"] and v["peer"].rsplit(".", 1)[0] == v["net"].rsplit(".", 1)[0]
        assert v["peer"].rsplit(".", 1)[1] not in ("0", "255")
        assert v["wrong"] != v["link"]
        assert v["label"] == f"L{v['link']}-{v['proto']}"


# ---- 抽样校验 ----

def test_sample_plan_includes_defaults_seeds_and_boundaries(lib):
    plan = P.samples(lib["campus-ar0-ar2-link"])
    labels = [p[0] for p in plan]
    assert labels[0].startswith("种子 0")
    assert sum(1 for _, seed, ov in plan if ov is None and seed > 0) == P.SAMPLE_SEEDS
    bounds = [ov for _, _, ov in plan if ov]
    assert {"link": 11} in bounds and {"link": 99} in bounds
    assert {"wrong": 11} in bounds and {"wrong": 99} in bounds
    acl = [ov for _, _, ov in P.samples(lib["campus-ar2-acl-block"]) if ov]
    assert {"acl": 3999} in acl and {"rule": 1} in acl and {"rule": 9} in acl
    # 边界值跳过 exclude：wrong 的下界不能等于 link 的默认值
    s = base_scenario()
    s["params"]["wrong"] = {"type": "int", "min": 12, "max": 99, "default": 21, "exclude": ["{link}"]}
    assert {"wrong": 13} in [ov for _, _, ov in P.samples(s) if ov]


def test_sampling_catches_value_that_breaks_rules_only_at_boundary():
    """只有某个边界取值才违规（静态提示泄露）：抽样能抓到，并指出是哪次展开。"""
    s = base_scenario()
    s["params"]["wrong"] = {"type": "int", "min": 11, "max": 99, "default": 21, "exclude": ["{link}"]}
    # 第 2 级提示里写死 10.0.99.2：只有 wrong=99 时它才是注入的错误地址（泄露）
    s["hints"][1] = s["hints"][1] + " 不要和 10.0.99.2 混淆。"
    e = errs_of(s)
    assert "边界 wrong=99" in e and "第 2 级静态提示泄露答案" in e


def test_sampling_catches_forbidden_command_in_choice():
    s = base_scenario()
    s["params"]["act"] = {"type": "choice", "values": ["shutdown", "reboot"], "default": "shutdown"}
    s["inject"]["AR2"] = s["inject"]["AR2"] + ["{act}"]
    e = errs_of(s)
    assert "inject.AR2 含禁止命令 'reboot'" in e and "重启设备" in e


def test_sampling_catches_inject_equal_to_fix():
    """忘了排除正确值：随机种子很难碰上，碰撞抽样（取 solution 里出现的数字 / 其他参数的默认值）专门试。"""
    s = base_scenario()
    s["params"]["wrong"] = {"type": "int", "min": 11, "max": 99, "default": 21}   # 忘了 exclude link
    e = errs_of(s)
    assert "碰撞 wrong=12" in e and "与参考修复命令相同" in e
    v = raw("campus-lsw0-access-vlan")
    v["params"]["wrong_vlan"].pop("exclude")                                       # 忘了排除 VLAN 10
    e = errs_of(v)
    assert "碰撞 wrong_vlan=10" in e and "port default vlan 10" in e


def test_undeclared_and_unused_params_are_errors():
    s = base_scenario()
    s["symptom"] += "（编号 {ticket}）"
    assert "引用了未声明的参数 ticket" in errs_of(s)
    s = base_scenario()
    s["params"]["unused"] = {"type": "int", "min": 1, "max": 3, "default": 1}
    assert "参数 unused 没有被任何字段引用" in errs_of(s)
    s = raw("ospf-2-1-r1-shutdown")
    s["symptom"] += "{x}"
    assert "未声明的参数：x" in errs_of(s)
    s = base_scenario()
    s["params"]["wrong"]["exclude"] = ["{later}"]
    s["params"]["later"] = {"type": "int", "min": 1, "max": 3, "default": 1}
    s["note"] += "{later}"
    assert "params.wrong 引用了未声明或在其后声明的参数 later" in errs_of(s)


def test_devices_and_interfaces_cannot_be_parametrized():
    s = base_scenario()
    s["params"]["ifx"] = {"type": "choice", "values": ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1"],
                          "default": "GigabitEthernet0/0/0"}
    s["inject"]["AR2"] = ["system-view", "interface {ifx}", "ip address 10.0.{wrong}.2 24"]
    assert "接口名不能参数化" in errs_of(s)
    s = base_scenario()
    s["params"]["dev"] = {"type": "choice", "values": ["AR0", "AR2"], "default": "AR2"}
    s["checks"][0]["device"] = "{dev}"
    assert "checks[0].device 不能参数化" in errs_of(s)
    s = base_scenario()
    s["inject"] = {"{x}": ["shutdown"]}
    assert "不能参数化" in errs_of(s)


def test_bad_specs_are_reported():
    s = base_scenario()
    s["params"]["link"] = {"type": "int", "min": 50, "max": 10, "default": 12}
    assert "min ≤ max" in errs_of(s)
    s = base_scenario()
    s["params"]["link"] = {"type": "int", "min": 11, "max": 99, "default": 5}
    assert "default" in errs_of(s)
    s = base_scenario()
    s["params"]["link"] = {"type": "float"}
    assert "type 必须是" in errs_of(s)


def test_validate_returns_template_and_load_dir_keeps_params(lib):
    t = lib["campus-ar2-return-route"]
    assert t["params"]["bad_hop"]["default"] == 3
    assert "{bad_hop}" in json.dumps(t["inject"])
    # 列表接口按默认值展示，不带占位符
    pub = sc.public(sc.expand(t, 0))
    assert "{" not in pub["symptom"]


# ---- 练习：种子记录、可复现、重启恢复 ----

def test_practice_records_seed_and_recover_reexpands(monkeypatch, db_path):
    from fastapi.testclient import TestClient

    import db
    import main
    from test_practice_api import AR0, AR2, FakeHub, TOPOS as T2

    class AnyHub(FakeHub):
        """任何种子的注入都算故障生效（原 FakeHub 只认默认值 10.0.21.2）。"""
        async def run_hidden(self, port, commands, kind="check", **kw):
            if kind == "config" and kw.get("groups") is None and any(c.startswith("ip address 10.0.") for c in commands):
                self.injected = True
            return await super().run_hidden(port, commands, kind, **kw)

    monkeypatch.setattr(config, "TOPO_DIRS", [str(T2)])
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", f"{AR0},{AR2}")
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")
    monkeypatch.setattr(config, "PRACTICE_SEED", None)      # 不固定：每次随机
    monkeypatch.setattr(main, "ConsoleHub", AnyHub)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        c.app.state.practice.busy_retry = 0.05
        assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200
        r = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link", "seed": 4242})
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["seed"] == 4242 and st["param_values"] == sc.expand(
            c.app.state.practice.scenarios["campus-ar0-ar2-link"], 4242)["param_values"]
        exp = sc.expand(c.app.state.practice.scenarios["campus-ar0-ar2-link"], 4242)
        inj = [cmds for port, kind, cmds in c.app.state.hub.calls if kind == "config"]
        assert exp["inject"]["AR2"] in inj and exp["symptom"] == st["scenario"]["symptom"]
        att = db.get_attempt(st["attempt_id"])
        assert att["seed"] == 4242 and json.loads(att["param_values_json"]) == exp["param_values"]
    # 服务重启：按记录的种子重新展开（判定命令带着本次的参数值）
    with TestClient(main.app) as c:
        a = c.app.state.practice.active
        assert a is not None and a.seed == 4242
        assert a.scenario["checks"] == exp["checks"] and "params" not in a.scenario
        assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200
        end_practice(c)
    # 不给种子：随机，且每次不同
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200
        seeds = set()
        for _ in range(2):
            st = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()
            seeds.add(st.get("seed"))
            end_practice(c)
        assert len(seeds) == 2 and all(isinstance(x, int) and x > 0 for x in seeds)
    main.SESSIONS.clear()
