"""eNSP Pro 替代经典版的补充能力（2026-10-06 真机实测后补的，全部用假设备 / 假服务）：

- console 空闲登出（真机约 5 分钟，输出以 `Please Press ENTER.` + 空行结尾）后连接器立即自动重登；
- PC-CLIENT 终端：`PC>` 提示符、无登录；隐藏事务里 ping（Linux 格式）；地址经设备属性接口读写；
- batchReboot 返回 HTTP 405 时退回「停止 → 启动」；
- ENSP-LSW（S6700）：长接口名被拒、display port vlan、VLAN / access / trunk / Vlanif 的整机复位；
- 场景 hosts（终端主机命令）校验；练习流程里的终端主机配置与复位、干净基线（不一致先复位、首次记录、复位失败拒绝开始）。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

import config_reset as cr
import db
import scenarios as sc
from config_reset import ResetResult
from console_hub import ConsoleHub, HiddenResult
from enspro import EnsproClient, EnsproProvider, host_cfg, mask_dotted
from fake_enspro import FakePC, FakeRest, FakeStompServer, FakeYunShan, YunShanSwitch
from lab import LabError
from practice import PracticeError, PracticeManager, host_snapshot, parse_host_snapshot, same_host

PW = "Fake-Pw#1234"
S6700 = (Path(__file__).parent / "fixtures" / "configs" / "yunshan_s6700_base.cfg").read_text()


def run(coro, timeout=40):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Env:
    def __init__(self, devices: dict[int, Any], pc: bool = False) -> None:
        self.devices = devices
        self.pc = pc

    async def __aenter__(self):
        self.server = await FakeStompServer(self.devices).start()
        self.rest = FakeRest()
        self.rest.pc = self.pc
        if self.pc:
            self.rest.status[12] = 4
            self.rest.pcs = {k: v for k, v in self.devices.items() if isinstance(v, FakePC)}
        client = EnsproClient(f"http://127.0.0.1:{self.server.port}", transport=self.rest.transport())
        self.provider = EnsproProvider(client=client, sandbox=2, password=lambda: PW, cache_ttl=0,
                                       heartbeat=0, quiet=0.15, login_timeout=8)
        return self

    async def __aexit__(self, *a):
        await self.provider.close()
        await self.server.stop()


async def read_until(conn, needle: str, timeout: float = 6) -> str:
    out = ""
    end = asyncio.get_running_loop().time() + timeout
    while needle not in out:
        rem = end - asyncio.get_running_loop().time()
        if rem <= 0:
            raise AssertionError(f"没等到 {needle!r}：{out!r}")
        out += (await asyncio.wait_for(conn.read(), rem)).decode()
    return out


# ---- console：空闲登出后立即自动重登 ----

def test_connector_relogins_on_please_press_enter_without_student_input():
    """真机登出后停在 `Please Press ENTER.` + 两个空行（最后一行为空）：连接器自己回车、输一次密码，学员什么都不用按；
    提示出现之后学员敲的字符被吞掉，不会落到 Password: 上。"""
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            await read_until(conn, "<HUAWEI>")
            dev.logout()
            out = await read_until(conn, "Please Press ENTER.")
            await conn.send(b"display version")              # 提示已出现：学员输入被吞掉
            out += await read_until(conn, "<HUAWEI>", timeout=8)
            for _ in range(50):                               # 提示符已转发，重登任务稍后才收尾
                if not conn._relogin:
                    break
                await asyncio.sleep(0.05)
            assert dev.login_attempts == 2 and dev.empty_submits == 0
            assert "display version" not in dev.received
            assert conn.login_log[-3:] == ["enter", "password", "prompt"]
            await conn.close()
    run(go())


# ---- PC-CLIENT ----

def test_pc_prompt_login_and_hidden_ping_check():
    async def go():
        pc = FakePC(reachable={"10.0.10.1"})
        async with Env({12: pc}, pc=True) as env:
            env.provider.stop_poll = 0.05
            hub = ConsoleHub(env.provider.connector, min_reconnect=0, ready_timeout=2, settle=0.1,
                             default_sysname="HUAWEI")
            hub.echo_timeout = 0.5
            r = await hub.run_hidden(12, ["ping -c 2 10.0.10.1"], kind="check")
            assert r.ok and "Network is unreachable" in r.outputs[0]      # 还没配地址
            assert hub.channels[12].conn.login_log == ["enter", "prompt"]  # PC 没有登录
            got = await env.provider.host_set(12, {"ip": "10.0.10.10", "mask": "24", "gateway": "10.0.10.1"})
            assert got == {"ip": "10.0.10.10", "mask": "255.255.255.0", "gateway": "10.0.10.1", "dns": ""}
            assert pc.attrs["ipv4.type"] == "static"
            # 改地址后 PC 命令行重启、吞掉第一条命令：hub 发现没有回显又停在 PC> 上，重发一次
            r = await hub.run_hidden(12, ["ping -c 2 10.0.10.1", "ping -c 2 10.0.10.99"], kind="check")
            assert pc.dropped == ["ping -c 2 10.0.10.1"]
            assert r.ok and r.prompt == "PC>"
            assert re.search(r"\b[12] received\b", r.outputs[0]) and not re.search(r"\b[12] received\b", r.outputs[1])
            assert await env.provider.host_get(12) == got
            cur = await env.provider.resolve()
            assert env.provider.is_host(12) and not env.provider.is_host(6)
            assert next(d for d in cur["topology"]["devices"] if d["name"] == "PC1")["kind"] == "pc"
            await hub.close()
    run(go())


def test_host_dns_roundtrip_when_attr_never_set():
    """真机（2026-10-07）：PC 从没设过 DNS 时 refreshAttrs 里没有 ipv4.config.dns1。快照要记成 dns=""，
    学员改了 DNS 之后按快照设回去时发 dns1=""（真机实测可清空 resolv.conf），DNS 才能随复位还原。"""
    async def go():
        pc = FakePC(reachable=set())
        async with Env({12: pc}, pc=True) as env:
            env.provider.stop_poll = 0.05
            assert "ipv4.config.dns1" not in env.rest.attrs.get(12, {})
            snap = parse_host_snapshot(host_snapshot(await env.provider.host_get(12)))
            assert snap == {"ip": "", "mask": "", "gateway": "", "dns": ""}
            got = await env.provider.host_set(12, {"ip": "10.0.10.9", "mask": "24", "gateway": "", "dns": "8.8.4.4"})
            assert got["dns"] == "8.8.4.4" and env.rest.attrs[12]["ipv4.config.dns1"] == "8.8.4.4"
            assert not same_host(got, snap)
            back = await env.provider.host_set(12, snap)          # 复位：设回快照
            assert env.rest.attrs[12]["ipv4.config.dns1"] == "" and same_host(back, snap)
    run(go())


def test_host_helpers():
    assert mask_dotted("24") == "255.255.255.0" and mask_dotted("0") == "0.0.0.0"
    assert mask_dotted("255.255.0.0") == "255.255.0.0" and mask_dotted("") == ""
    assert host_cfg({"ipv4.config.ip": "0.0.0.0", "ipv4.config.gateway": "10.0.0.1"}) == \
        {"ip": "", "mask": "", "gateway": "10.0.0.1", "dns": ""}
    # DNS 属性名 2026-10-07 真机核实为 ipv4.config.dns1；没设过时 refreshAttrs 里没有这个键 = 空
    assert host_cfg({"ipv4.config.ip": "10.0.0.2", "ipv4.config.ipmask": "255.0.0.0", "ipv4.config.dns1": "8.8.4.4",
                     "ipv4.config.dns2": "1.1.1.1"}) == {"ip": "10.0.0.2", "mask": "255.0.0.0", "gateway": "", "dns": "8.8.4.4"}
    snap = host_snapshot({"ip": "10.0.10.10", "mask": "255.255.255.0", "gateway": "10.0.10.1"})
    assert parse_host_snapshot(snap)["mask"] == "255.255.255.0" and parse_host_snapshot("#\nsysname X") is None
    assert same_host({"ip": "10.0.10.10", "mask": "24", "gateway": ""}, {"ip": "10.0.10.10", "mask": "255.255.255.0"})
    assert not same_host({"ip": "10.0.10.10", "mask": "24"}, {"ip": "10.0.10.11", "mask": "24"})
    cfg = sc.apply_host_commands({}, ["ip address 10.0.10.10 24", "ip gateway 10.0.10.1"])
    assert cfg == {"ip": "10.0.10.10", "mask": "24", "gateway": "10.0.10.1"}
    assert sc.apply_host_commands(cfg, ["undo ip gateway", "undo ip address"]) == {"ip": "", "mask": "", "gateway": ""}
    with pytest.raises(ValueError):
        sc.apply_host_commands({}, ["ipconfig 10.0.10.10"])


# ---- 重启：batchReboot 405 时退回停止 + 启动 ----

def test_reboot_falls_back_to_stop_start_on_405():
    async def go():
        async with Env({}) as env:
            env.rest.reboot_405 = True
            env.provider.stop_poll = 0.01
            res = await env.provider.power_action("reboot", [7])
            assert res["via"] == "stop+start"
            paths = [p for _, p, _ in env.rest.calls if "/device/batch" in p]
            assert paths == ["/service/ensp/device/batchReboot", "/service/ensp/device/batchStop",
                             "/service/ensp/device/batchStart"]
            assert env.rest.status[7] == 6 and env.provider.pending[7]["action"] == "reboot"
    run(go())


# ---- ENSP-LSW（S6700） ----

def make_hub(dev) -> ConsoleHub:
    return ConsoleHub(dev.factory, idle_close=60, min_reconnect=0, ready_timeout=1.0, settle=0.05,
                      default_sysname="HUAWEI")


def test_switch_long_interface_name_rejected_and_port_vlan():
    async def go():
        sw = YunShanSwitch(S6700, page=40)
        hub = make_hub(sw)
        sub = await hub.subscribe(8)
        r = await hub.run_hidden(8, ["system-view", "interface GigabitEthernet1/0/2"], kind="config")
        assert not r.ok and "Wrong parameter" in r.reason
        r = await hub.run_hidden(8, ["system-view", "vlan batch 10", "interface GE1/0/1", "port link-type access",
                                     "port default vlan 10"], kind="config")
        assert r.ok, r.reason
        r = await hub.run_hidden(8, ["display port vlan"], kind="check")
        rows = {l.split()[0]: l.split()[1:3] for l in r.outputs[0].splitlines() if l.startswith("GE1/0/")}
        assert rows["GE1/0/1"] == ["access", "10"] and rows["GE1/0/2"] == ["desirable", "1"]
        hub.unsubscribe(sub)
        await hub.close()
    run(go())


def test_switch_full_reset_vlan_trunk_access_vlanif():
    """与真机复位实测同一组改动（scratchpad reset_probe：1 轮 31 条命令复位干净）：VLAN、access / trunk / pvid、
    shutdown、Vlanif、LoopBack、stp mode、静态路由、sysname。"""
    async def go():
        sw = YunShanSwitch(S6700, page=40)
        hub = make_hub(sw)
        sub = await hub.subscribe(8)
        reset = cr.ConfigReset(hub, deadline=3, busy_retry=0.1)
        snap = await reset.snapshot(8)
        assert cr.same(S6700, snap) and cr.sysname_of(cr.parse(snap)) == "HUAWEI"
        mut = ["system-view", "vlan batch 10 20 30 40 100 to 103", "vlan 30", "description ainet-users", "quit",
               "interface GE1/0/1", "port link-type access", "port default vlan 20", "description to-AR0", "quit",
               "interface GE1/0/2", "port link-type trunk", "port trunk allow-pass vlan 10 20 30", "quit",
               "interface GE1/0/4", "port link-type trunk", "port trunk pvid vlan 40", "port trunk allow-pass vlan 30 40",
               "quit", "interface GE1/0/6", "shutdown", "quit", "interface Vlanif30", "ip address 10.0.30.254 24", "quit",
               "interface LoopBack1", "ip address 1.1.1.1 32", "quit", "stp mode rstp",
               "ip route-static 10.9.0.0 16 10.0.10.1", "sysname SWX"]
        r = await hub.run_hidden(8, mut, kind="config", confirm=True)
        assert r.ok and r.prompt == "<SWX>", r.reason
        assert len(cr.differences(cr.parse(snap), cr.parse(sw.config_text()))) >= 15
        res = await reset.restore_to(8, snap)
        assert res.ok, (res.reason, res.leftovers)
        assert cr.same(snap, sw.config_text()) and sw.host == "HUAWEI" and not sw.vlans
        hub.unsubscribe(sub)
        await hub.close()
    run(go())


# ---- 场景 hosts 校验 ----

def ys_vlan_scenario() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2] / "scenarios"
    return json.loads((root / "campus-lsw0-access-vlan-ys.json").read_text(encoding="utf-8"))


def test_scenario_hosts_validation():
    s = ys_vlan_scenario()
    out = sc.validate(s)
    assert out["hosts"] == ["PC1"]
    bad = json.loads(json.dumps(s))
    bad["baseline"]["PC1"] = ["ipconfig 10.0.10.10 255.255.255.0"]
    with pytest.raises(sc.ScenarioError, match="终端主机不支持"):
        sc.validate(bad)
    bad = json.loads(json.dumps(s))
    bad["checks"].append({"device": "PC1", "command": "display ip interface brief", "expect": "x"})
    with pytest.raises(sc.ScenarioError, match="只能用 ping"):
        sc.validate(bad)
    bad = json.loads(json.dumps(s))
    bad["hosts"] = ["PC9"]
    with pytest.raises(sc.ScenarioError, match="hosts 中的 PC9"):
        sc.validate(bad)
    # 拓扑里不是 PC：就绪判断拒绝
    exp = sc.expand(sc.validate(s), 0)
    cur = {"state": "running", "lab": {"kind": "enspro", "platform": "yunshan", "power": True, "label": "eNSP Pro"},
           "topology": {"name": "ainet_campus", "path": "enspro://sandbox/3", "devices": [
               {"name": "AR0", "console": 9, "kind": "router", "status": "online"},
               {"name": "LSW0", "console": 8, "kind": "switch", "status": "online"},
               {"name": "PC1", "console": 12, "kind": "router", "status": "online"}]}}
    rd = sc.readiness(exp, cur)
    assert not rd["ready"] and "不是终端主机" in rd["reason"]
    cur["topology"]["devices"][2]["kind"] = "pc"
    assert sc.readiness(exp, cur)["ready"]


# ---- 练习流程：终端主机 + 干净基线 ----

BASE_CFG = "#\nsysname HUAWEI\n#\ninterface GE0/0/0\n#\nreturn"


class StubHub:
    """只记录配置事务、按 configs 返回快照的 hub（练习流程的单元测试用）。"""

    def __init__(self) -> None:
        self.configs: dict[int, str] = {8: BASE_CFG, 9: BASE_CFG}
        self.calls: list[tuple[int, str, list[str]]] = []
        self.ping_ok = False
        self.listeners: list = []
        self.sticky = False                   # True：复位命令执行了但配置不变（模拟撤不掉）

    def add_listener(self, fn):
        self.listeners.append(fn)

    async def run_hidden(self, port, commands, kind="check", cmd_timeout=None, tolerate_errors=False, groups=None,
                         confirm=False):
        if groups is not None:
            commands = [c for cmds, _ in groups for c in cmds]
            if not self.sticky:
                self.configs[port] = BASE_CFG
        self.calls.append((port, kind, list(commands)))
        if commands == ["display current-configuration"]:
            return HiddenResult("ok", outputs=[self.configs[port]])
        if kind == "check":
            return HiddenResult("ok", outputs=["2 packets transmitted, 2 received" if self.ping_ok else
                                               "2 packets transmitted, 0 received"] * len(commands))
        return HiddenResult("ok", outputs=[""] * len(commands))

    def reset_replay(self, p): pass
    def pin(self, ps): pass
    def unpin(self, ps): pass
    def drop_subscribers(self, ps, msg=""): pass
    def flush_output(self, p): pass


class StubLab:
    kind = "enspro"

    def __init__(self) -> None:
        self.hosts: dict[int, dict[str, str]] = {12: {"ip": "", "mask": "", "gateway": ""}}
        self.sets: list[tuple[int, dict[str, str]]] = []

    async def host_get(self, key):
        return dict(self.hosts[key])

    async def host_set(self, key, cfg):
        self.sets.append((key, dict(cfg)))
        self.hosts[key] = {"ip": cfg.get("ip", ""), "mask": mask_dotted(cfg.get("mask", "")),
                           "gateway": cfg.get("gateway", "")}
        return dict(self.hosts[key])


SCEN = {
    "id": "pc-demo", "title": "PC 演示", "topology": "ainet_campus", "platform": "yunshan",
    "required_devices": ["AR0", "LSW0", "PC1"], "accessible_devices": ["AR0", "LSW0", "PC1"], "hosts": ["PC1"],
    "symptom": "PC1 ping 不通网关", "baseline": {"PC1": ["ip address 10.0.10.10 24", "ip gateway 10.0.10.1"],
                                                 "AR0": ["system-view", "interface GE0/0/0", "ip address 10.0.10.1 24"]},
    "inject": {"AR0": ["system-view", "interface GE0/0/0", "shutdown"]},
    "restore": {"PC1": ["undo ip address", "undo ip gateway"],
                "AR0": ["system-view", "interface GE0/0/0", "undo shutdown", "undo ip address"]},
    "checks": [{"device": "PC1", "command": "ping -c 2 10.0.10.1", "expect": "\\b[12] received\\b"}],
    "hints": ["看看接口状态", "display ip interface brief", "undo shutdown"],
}
CUR = {"state": "running", "lab": {"kind": "enspro", "platform": "yunshan", "power": True, "label": "eNSP Pro"},
       "topology": {"name": "ainet_campus", "path": "enspro://sandbox/3", "devices": [
           {"name": "AR0", "console": 9, "kind": "router", "status": "online"},
           {"name": "LSW0", "console": 8, "kind": "switch", "status": "online"},
           {"name": "PC1", "console": 12, "kind": "pc", "status": "online"}]}}


@pytest.fixture
def pm(db_path):
    db.init_db()
    s = sc.validate(json.loads(json.dumps(SCEN)))
    ids = db.sync_scenarios([s])

    async def resolve():
        return json.loads(json.dumps(CUR))
    if not db.list_students():
        db.create_student("stu", "学员", "pw")
    return s, ids, resolve


def _manager(pm) -> tuple[PracticeManager, StubHub, StubLab]:
    s, ids, resolve = pm
    hub, lab = StubHub(), StubLab()
    m = PracticeManager(hub, {s["id"]: s}, ids, resolve, debounce=0.05, min_interval=0, busy_retry=0.01,
                        op_deadline=0.5)
    m.lab = lab
    return m, hub, lab


def _student() -> int:
    return db.list_students()[0]["id"]


def test_practice_hosts_baseline_capture_then_restore(pm):
    async def go():
        m, hub, lab = _manager(pm)
        st = await m.start(_student(), "pc-demo", seed=0)
        # 首次：三台都记下基线；PC1 的快照是地址配置
        assert {d: r["status"] for d, r in st["pre_reset"].items()} == {"AR0": "captured", "LSW0": "captured",
                                                                       "PC1": "captured"}
        assert parse_host_snapshot(m.active.snapshots["PC1"]) == {"ip": "", "mask": "", "gateway": ""}
        assert lab.hosts[12] == {"ip": "10.0.10.10", "mask": "255.255.255.0", "gateway": "10.0.10.1"}
        assert not any(p == 12 and k == "config" for p, k, _ in hub.calls)   # PC 的配置不走 console
        hub.ping_ok = True
        res = await m.check()
        assert res["passed"]
        out = await m.end()
        assert out["reset_ok"] and lab.hosts[12] == {"ip": "", "mask": "", "gateway": ""}
        assert out["reset"]["PC1"]["status"] == "ok"
        b = await m.baselines()
        assert all(d["has_baseline"] for d in b["devices"])
    run(go())


def test_practice_golden_baseline_resets_dirty_device_before_start(pm):
    async def go():
        m, hub, lab = _manager(pm)
        await m.capture_baselines()
        # 上一次没撤销干净（又被自动保存 / 停机保存）的改动：AR0 多了 LoopBack，PC1 地址被改
        hub.configs[9] = BASE_CFG.replace("#\nreturn", "#\ninterface LoopBack55\n ip address 5.5.5.5 255.255.255.255\n#\nreturn")
        lab.hosts[12] = {"ip": "10.9.9.9", "mask": "255.255.255.0", "gateway": ""}
        st = await m.start(_student(), "pc-demo", seed=0)
        assert st["pre_reset"]["AR0"]["status"] == "reset" and st["pre_reset"]["AR0"]["differences"] >= 1
        assert st["pre_reset"]["PC1"]["status"] == "reset" and st["pre_reset"]["LSW0"]["status"] == "clean"
        assert m.active.snapshots["AR0"] == BASE_CFG                    # 开练快照是复位后的干净配置
        assert parse_host_snapshot(m.active.snapshots["PC1"])["ip"] == ""
        assert any(p == 9 and "undo interface LoopBack55" in cmds for p, _, cmds in hub.calls)
        await m.end()
    run(go())


def test_practice_refuses_start_when_baseline_reset_fails(pm):
    async def go():
        m, hub, lab = _manager(pm)
        await m.capture_baselines()
        hub.configs[9] = BASE_CFG.replace("#\nreturn", "#\ninterface LoopBack55\n#\nreturn")
        hub.sticky = True
        with pytest.raises(PracticeError, match="干净基线"):
            await m.start(_student(), "pc-demo", seed=0)
        assert m.active is None
    run(go())


def test_capture_baselines_refused_during_practice(pm):
    async def go():
        m, hub, lab = _manager(pm)
        await m.start(_student(), "pc-demo", seed=0)
        with pytest.raises(PracticeError, match="进行中"):
            await m.capture_baselines()
        await m.end()
    run(go())


def test_async_traffic_filter_error_after_prompt_is_treated_as_log():
    """AR6700 删 ACL / traffic-filter 后数秒，在后面某条命令的提示符之后打印不带时间戳的
    `Error: Failed to apply the traffic-filter ...`、不重印提示符（2026-10-06 真机，曾让复位命令超时）：按日志处理。"""
    from fake_enspro import YunShanConfigVRP
    msg = ("Error: Failed to apply the traffic-filter. To check the cause, run the display traffic-filter "
           "applied-record command. (slot=0)")

    class Dev(YunShanConfigVRP):
        def _execute(self, cmd: str) -> None:
            super()._execute(cmd)
            if cmd.startswith("undo ip route-static"):
                self.emit("\r\n" + msg + "\r\n")

    async def go():
        dev = Dev(S6700.replace("sysname HUAWEI", "sysname HUAWEI\n#\nip route-static 10.0.10.0 255.255.255.0 10.0.12.1"))
        hub = make_hub(dev)
        sub = await hub.subscribe(10)
        r = await hub.run_hidden(10, ["system-view", "undo ip route-static 10.0.10.0 255.255.255.0 10.0.12.1"],
                                 kind="config", cmd_timeout=3)
        assert r.ok and not r.failed, r.reason
        assert r.prompt == "<HUAWEI>"
        hub.unsubscribe(sub)
        await hub.close()
    run(go())
