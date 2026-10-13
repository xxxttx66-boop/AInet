"""练习 API 全流程（mock 掉 ConsoleHub）：开始 → 注入后确认故障 → 判定 → 通关 → 结束还原；
注入无效报错并还原；单练习约束；未登录 401；自动判定；练习期间终端白名单。
另有一条用假 VRP 设备走真实 ConsoleHub 的 WS 测试：两个标签共享一条设备连接。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

import config
import db
import main
from console_hub import HiddenResult
from fake_vrp import FakeVRP
from practice_helpers import end_practice

TOPOS = Path(__file__).resolve().parent / "fixtures" / "topos"
FACTORY = (Path(__file__).resolve().parent / "fixtures" / "configs" / "ar1220_factory.cfg").read_text(encoding="utf-8")
AR0, AR2 = "B3B8A475-25C8-4a6c-B29D-F49A6F4F9F0E", "14D97C46-57BE-4d7a-BFB6-696F4390C76B"


class FakeHub:
    """记录隐藏事务；按注入 / 修复状态给出 ping 结果。"""

    def __init__(self, *a, **kw) -> None:
        self.calls: list[tuple[int, str, list[str]]] = []
        self.listeners = []
        self.injected = False
        self.fixed = False
        self.always_pass = False
        self.busy = 0
        self.fail_restore = False
        self.dropped: list[set[int]] = []
        self.configs: dict[int, str] = {}           # 端口 -> display current-configuration
        self.extra: dict[int, list[str]] = {}       # 学员额外加的顶层配置行
        self.sticky: set[str] = set()               # 撤销不掉的行
        self.reset_groups: list = []
        self.capture_busy = False                   # 抓配置一直 busy（强制收回也解决不了）
        self.stuck_more = False                     # 学员终端停在分页：抓配置 busy，直到 reclaim 收回终端
        self.reclaimed: list[int] = []
        self.frozen: set[int] = set()
        self.delay = 0.0                            # 每个隐藏事务的耗时（模拟复位慢）

    def student_adds(self, port, line):
        self.extra.setdefault(port, []).append(line)
        self.configs[port] = FACTORY.replace("#\nreturn", "".join(f"#\n{l}\n" for l in self.extra[port])
                                             + "#\nreturn")

    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        self.listeners.remove(fn)

    def emit(self, event, port, data):
        for fn in self.listeners:
            fn(event, port, data)

    async def run_hidden(self, port, commands, kind="check", cmd_timeout=None, tolerate_errors=False,
                         groups=None, confirm=False):
        if groups is not None:
            commands = [c for cmds, _ in groups for c in cmds]
        self.calls.append((port, kind, list(commands)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.busy:
            self.busy -= 1
            return HiddenResult("busy", "学员正在输入命令")
        if commands == ["display current-configuration"]:
            if self.capture_busy or self.stuck_more:
                return HiddenResult("busy", "设备终端停在分页（---- More ----）上")
            return HiddenResult("ok", outputs=[self.configs.get(port, FACTORY)])
        if groups is not None:
            self.reset_groups.append((port, list(commands)))
            for c in commands:                          # 学员额外加的行：可撤销时撤销
                if c.startswith("undo ") and c[5:] in self.extra.get(port, []) and c[5:] not in self.sticky:
                    self.extra[port].remove(c[5:])
            self.configs[port] = FACTORY.replace("#\nreturn", "".join(f"#\n{l}\n" for l in self.extra.get(port, []))
                                                 + "#\nreturn")
            return HiddenResult("ok")
        if kind == "config":
            if any("10.0.21.2" in c for c in commands):
                self.injected = True
            if any("undo ip address" in c for c in commands):
                if self.fail_restore:
                    return HiddenResult("busy", "学员处于系统视图，暂不注入")
                self.injected = False
            return HiddenResult("ok")
        ok = self.always_pass or not self.injected or self.fixed
        return HiddenResult("ok", outputs=[f"  2 packet(s) transmitted\n  {2 if ok else 0} packet(s) received"
                                           for _ in commands])

    def drop_subscribers(self, ports, message=""):
        self.dropped.append(set(ports))

    def freeze(self, ports, message=""):
        self.frozen |= set(ports)

    def unfreeze(self, ports, message=""):
        self.frozen -= set(ports)

    async def reclaim(self, port):
        assert port in self.frozen                  # 只在冻结学员输入之后强制收回
        self.reclaimed.append(port)
        self.stuck_more = False
        return HiddenResult("ok", prompt="<Huawei>")

    def reset_replay(self, port):
        self.reset = getattr(self, "reset", []) + [port]

    def pin(self, ports):
        self.pinned = set(ports)

    def unpin(self, ports):
        self.pinned = set()

    async def close(self):
        pass


@pytest.fixture
def env(monkeypatch, db_path):
    monkeypatch.setattr(config, "TOPO_DIRS", [str(TOPOS)])
    monkeypatch.setenv("AINET_ENSP_RUNNING_IDS", f"{AR0},{AR2}")
    monkeypatch.setenv("AINET_ENSP_CLIENT", "1")
    monkeypatch.setattr(config, "JUDGE_DEBOUNCE", 0.05)
    monkeypatch.setattr(config, "JUDGE_MIN_INTERVAL", 0.0)


@pytest.fixture
def pclient(env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "ConsoleHub", FakeHub)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        c.app.state.practice.busy_retry = 0.05
        yield c
    main.SESSIONS.clear()


def login(c):
    assert c.post("/api/login", json={"login": "admin", "password": "admin"}).status_code == 200


def hub(c) -> FakeHub:
    return c.app.state.hub


def test_requires_login(pclient):
    for method, url, body in [("get", "/api/scenarios", None), ("post", "/api/practice/start", {"scenario_id": "x"}),
                              ("post", "/api/practice/check", None), ("post", "/api/practice/hint", {"level": 1}),
                              ("post", "/api/practice/end", None), ("get", "/api/practice/current", None)]:
        r = getattr(pclient, method)(url, **({"json": body} if body else {}))
        assert r.status_code == 401, url


def test_scenario_list_readiness(pclient):
    login(pclient)
    data = pclient.get("/api/scenarios").json()
    assert data["errors"] == [] and data["active"] is None
    by = {s["id"]: s for s in data["scenarios"]}
    assert by["campus-ar0-ar2-link"]["ready"] is True
    o = by["ospf-2-1-r1-shutdown"]
    assert o["ready"] is False and "2-1Single-Area OSPF" in o["reason"] and "R1、R2、R3" in o["reason"]
    assert "checks" not in by["campus-ar0-ar2-link"] and "inject" not in by["campus-ar0-ar2-link"]


def test_full_flow_start_check_pass_hint_end(pclient):
    login(pclient)
    r = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["active"] and st["phase"] == "running"
    assert "10.0.12.2" in st["scenario"]["symptom"]
    assert st["devices"] == [{"name": "AR0", "port": 2000}, {"name": "AR2", "port": 2003}]
    assert st["last_check"]["passed"] == 0 and st["last_check"]["total"] == 1
    h = hub(pclient)
    # 顺序：先给各设备拍整机配置快照，再各设备 baseline、inject（各自一个配置事务），最后确认故障（判定事务）
    assert [(p, k) for p, k, _ in h.calls] == [(2000, "check"), (2003, "check"), (2000, "config"),
                                                (2003, "config"), (2003, "config"), (2000, "check")]
    assert h.calls[0][2] == h.calls[1][2] == ["display current-configuration"]
    assert set(db.get_configs(st["attempt_id"])) == {"AR0", "AR2"}       # 快照已落库
    assert h.calls[3][2][-1] == "ip address 10.0.12.2 24"
    assert h.calls[4][2][-1] == "ip address 10.0.21.2 24"
    assert h.calls[5][2] == ["ping -c 2 10.0.12.2"]
    assert 2001 in h.dropped[0] and 2000 not in h.dropped[0]     # 不开放设备的终端被断开
    # 单练习约束
    r2 = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r2.status_code == 409 and "进行中" in r2.json()["detail"]
    # 未修复：检查不通过
    c = pclient.post("/api/practice/check").json()
    assert c["check"]["passed"] == 0 and c["phase"] == "running"
    # 提示：按级别依次
    assert pclient.post("/api/practice/hint", json={"level": 2}).status_code == 400
    h1 = pclient.post("/api/practice/hint", json={"level": 1}).json()
    assert h1["hints_used"] == 1 and "网段" in h1["text"]
    # 修复后立即检查：通关
    h.fixed = True
    c = pclient.post("/api/practice/check").json()
    assert c["phase"] == "passed" and c["passed"] and c["last_check"]["all"]
    cur = pclient.get("/api/practice/current").json()
    assert cur["passed"] and cur["passed_at"]
    # 结束：执行 restore
    n = len(h.calls)
    e = end_practice(pclient)
    assert e["restored"] and e["passed"] is True and e["hints_used"] == 1 and e["duration_s"] >= 0
    assert e["reset_ok"] and e["leftovers"] == {} and "额外做的配置更改都已撤销" in e["note"]
    assert set(e["reset"]) == {"AR0", "AR2"} and all(r["status"] == "ok" for r in e["reset"].values())
    restores = [c for c in h.calls[n:] if c[1] == "config"]
    assert {p for p, k, _ in restores} == {2000, 2003}
    assert all("undo ip address" in cmds for _, _, cmds in restores)
    # 场景 restore 之后再整机复位：抓配置比对（一致则不需要再下命令）
    assert [c for c in h.calls[n:] if c[1] == "check"] == [(2000, "check", ["display current-configuration"]),
                                                          (2003, "check", ["display current-configuration"])]
    assert pclient.get("/api/practice/current").json()["active"] is False
    # 档案：最近记录 passed=1，带场景标题
    me = pclient.get("/api/me").json()
    rec = me["recent_attempts"][0]
    assert rec["passed"] == 1 and rec["mode"] == "practice" and rec["scenario_title"] == "AR0 与 AR2 互联不通"
    assert me["progress"]["completed_scenarios"] == 1


def test_give_up_restores_and_records_failure(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    e = end_practice(pclient)
    assert e["restored"] and e["passed"] is False
    assert hub(pclient).injected is False
    me = pclient.get("/api/me").json()
    assert me["recent_attempts"][0]["passed"] == 0
    assert me["wrong_scenarios"][0]["scenario_key"] == "campus-ar0-ar2-link"


def test_ineffective_inject_is_rejected_and_restored(pclient):
    login(pclient)
    hub(pclient).always_pass = True
    r = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 409 and "没有生效" in r.json()["detail"]
    kinds = [(p, k, c[-1]) for p, k, c in hub(pclient).calls]
    assert (2000, "config", "undo ip address") in kinds and (2003, "config", "undo ip address") in kinds
    assert pclient.get("/api/practice/current").json()["active"] is False
    assert pclient.get("/api/me").json()["recent_attempts"] == []     # 失败的开始不留记录


def test_busy_device_retried_during_start(pclient):
    login(pclient)
    hub(pclient).busy = 2
    r = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 200, r.text


def test_not_ready_scenario_rejected(pclient):
    login(pclient)
    r = pclient.post("/api/practice/start", json={"scenario_id": "ospf-2-1-r1-shutdown"})
    assert r.status_code == 409 and "R1" in r.json()["detail"]
    assert pclient.post("/api/practice/start", json={"scenario_id": "nope"}).status_code == 404


def test_auto_judge_after_command(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    h.fixed = True

    async def student_enter():
        h.emit("input", 2003, "")
        h.emit("command", 2003, "ip address 10.0.12.2 24")
    pclient.portal.call(student_enter)
    end = time.time() + 5
    st = None
    while time.time() < end:
        st = pclient.get("/api/practice/current").json()
        if st.get("passed"):
            break
        time.sleep(0.05)
    assert st["passed"] and st["last_check"]["source"] == "auto"
    logs = db.attempt_logs(st["attempt_id"])
    assert [(l["device"], l["command"]) for l in logs] == [("AR2", "ip address 10.0.12.2 24")]
    e = end_practice(pclient)
    assert e["commands"] == 1 and e["passed"]


def test_restore_failure_after_retries_still_ends_practice(pclient):
    """restore 一直不成功（重试几轮后）：练习照样结束（不再要求学员回用户视图重试），note 列出没复位的设备。"""
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    pm = pclient.app.state.practice
    pm.end_retry_wait = 0.01
    h.fail_restore = True
    n = len(h.calls)
    e = end_practice(pclient)
    assert e["active"] is False and e["restored"] is False and e["reset_ok"] is False and e["passed"] is False
    assert set(e["reset_failed"]) == {"AR0", "AR2"} and "系统视图" in e["reset_failed"]["AR0"]
    assert "没能复位" in e["note"] and "重载拓扑" in e["note"] and "用户视图" not in e["note"]
    restores = [c for _, k, c in h.calls[n:] if k == "config" and any("undo ip address" in x for x in c)]
    assert len(restores) >= 2 * pm.end_retries                       # 每台设备重试了 end_retries 轮（每轮内部还按 busy 重试）
    assert pclient.get("/api/practice/current").json()["active"] is False
    assert h.frozen == set()                                         # 结束后解冻
    h.fail_restore = False
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200


class PowerLab:
    """能启停的实验后端桩：device_states 返回给定状态，或抛错。"""
    power = True
    kind = "ensp"

    def __init__(self, states=None, error=None) -> None:
        self.states, self.error, self.calls = states or {}, error, 0

    async def device_states(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {k: {"name": str(k), "status": v} for k, v in self.states.items()}


def test_powered_off_device_skipped_on_end(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    pm = pclient.app.state.practice
    pm.lab = PowerLab({2000: "online", 2003: "stopped"})
    n = len(h.calls)
    e = end_practice(pclient)
    assert e["active"] is False and e["reset_ok"] is False and e["powered_off"] == ["AR2"], e
    assert e["passed"] is False and list(e["reset"]) == ["AR0"] and "AR2" in e["note"]
    assert {p for p, _, _ in h.calls[n:]} == {2000}  # 没有碰 AR2（2003）


def test_power_state_unknown_keeps_old_behavior(pclient):
    from lab import LabError

    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    h.fail_restore = True
    pm = pclient.app.state.practice
    pm.lab = PowerLab(error=LabError(409, "会话被挤掉"))
    pm.end_retry_wait = 0.01
    e = end_practice(pclient)
    assert e["restored"] is False and pm.lab.calls == 1 and e["powered_off"] == []   # 读不到电源：照常去复位
    assert set(e["reset_failed"]) == {"AR0", "AR2"}
    assert pclient.get("/api/practice/current").json()["active"] is False
    h.fail_restore = False
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    pm.lab = PowerLab({2003: "stopped"})
    pm.lab.power = False                             # 不支持启停的后端：不判断电源
    e = end_practice(pclient)
    assert e["active"] is False and e["reset_ok"] and pm.lab.calls == 0 and e["powered_off"] == []


def test_student_extra_config_is_reset(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    h.student_adds(2000, "ip route-static 192.168.9.0 255.255.255.0 10.0.12.2")
    e = end_practice(pclient)
    assert e["active"] is False and e["restored"] and e["reset_ok"]
    assert e["reset"]["AR0"]["status"] == "ok" and e["reset"]["AR0"]["rounds"] == 1
    assert h.reset_groups == [(2000, ["system-view", "undo ip route-static 192.168.9.0 255.255.255.0 10.0.12.2"])]
    assert h.extra[2000] == []


def test_unremovable_config_reported_with_reload_hint(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    h.student_adds(2003, "dhcp enable")
    h.sticky.add("dhcp enable")
    e = end_practice(pclient)
    # 不静默当作成功：练习结束，但 restored=False、列出撤不掉的行、提示重载拓扑
    assert e["active"] is False and e["restored"] is False and e["reset_ok"] is False
    assert e["leftovers"] == {"AR2": ["多出：dhcp enable"]}
    assert "重载拓扑" in e["note"] and "多出：dhcp enable" in e["note"]
    assert pclient.get("/api/practice/current").json()["active"] is False


def test_end_reclaims_terminal_stuck_on_more(pclient):
    """学员终端停在分页（以前复位 busy、要学员按 q 重试）：结束时先冻结输入、强制收回终端，一次复位成功。"""
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    h = hub(pclient)
    h.stuck_more = True
    e = end_practice(pclient)
    assert e["active"] is False and e["restored"] and e["reset_ok"] and e["reset_failed"] == {}, e
    assert sorted(h.reclaimed) == [2000, 2003]


def test_end_returns_immediately_and_resets_in_background(pclient):
    """结束接口立即返回（resetting）；复位在后台，期间开始新练习 / 考试 409；完成后 current 非 active、last_result 有复位结果。"""
    login(pclient)
    st = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()
    h = hub(pclient)
    h.delay = 0.3
    t0 = time.time()
    r = pclient.post("/api/practice/end")
    assert r.status_code == 200 and time.time() - t0 < 1.0
    e = r.json()
    assert e["active"] is False and e["resetting"] is True and e["reset_ok"] is None
    assert e["attempt_id"] == st["attempt_id"] and e["passed"] is False and "score" not in e
    assert db.get_attempt(st["attempt_id"])["ended_at"] is not None       # 放弃已落库
    assert h.frozen == {2000, 2003}
    cur = pclient.get("/api/practice/current").json()
    assert cur["active"] is False and cur["resetting"] is True and cur["last_result"]["resetting"] is True
    again = pclient.post("/api/practice/end")                              # 重复点击：不再起第二次复位
    assert again.status_code == 200 and again.json()["resetting"] is True
    r = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 409 and "后台复位" in r.json()["detail"]
    r = pclient.post("/api/exam/start_single", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 409 and "后台复位" in r.json()["detail"]
    r = pclient.post("/api/lab/environment", json={"key": "enspro"})
    assert r.status_code == 409 and "后台复位" in r.json()["detail"]
    end = time.time() + 20
    while time.time() < end and pclient.get("/api/practice/current").json().get("resetting"):
        time.sleep(0.05)
    cur = pclient.get("/api/practice/current").json()
    last = cur["last_result"]
    assert cur["active"] is False and not cur.get("resetting")
    assert last["attempt_id"] == st["attempt_id"] and last["resetting"] is False and last["reset_ok"] is True
    assert set(last["reset"]) == {"AR0", "AR2"} and "score" in last
    assert json.loads(db.get_attempt(st["attempt_id"])["reset_json"])["ok"] is True
    assert h.frozen == set() and db.get_setting("practice_active") is None
    h.delay = 0
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200


def test_restart_resumes_background_reset(env, monkeypatch):
    """后台复位中服务重启：phase=ending 已持久化，重启后接着复位，练习按结束处理。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "ConsoleHub", FakeHub)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        login(c)
        aid = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()["attempt_id"]
        hub(c).delay = 5                              # 复位很慢：还没做完服务就关了
        r = c.post("/api/practice/end")
        assert r.json().get("resetting") is True, r.text
    assert json.loads(db.get_setting("practice_active"))["phase"] == "ending"
    with TestClient(main.app) as c:                    # 「重启」
        login(c)
        end = time.time() + 20
        while time.time() < end and c.get("/api/practice/current").json().get("resetting"):
            time.sleep(0.05)
        last = c.get("/api/practice/current").json()["last_result"]
        assert last["attempt_id"] == aid and last["reset_ok"] is True
        assert db.get_setting("practice_active") is None
    main.SESSIONS.clear()


def test_snapshots_survive_restart(env, monkeypatch):
    """服务重启后恢复进行中的练习：快照从 db 读回，结束时仍能整机复位。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "ConsoleHub", FakeHub)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        c.app.state.practice.op_deadline = 0.5
        login(c)
        assert c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    with TestClient(main.app) as c:                    # 「重启」：新的 hub / 练习管理器
        c.app.state.practice.op_deadline = 0.5
        login(c)
        pm = c.app.state.practice
        assert pm.active is not None and set(pm.active.snapshots) == {"AR0", "AR2"}
        h = hub(c)
        h.student_adds(2000, "ip route-static 192.168.9.0 255.255.255.0 10.0.12.2")
        e = end_practice(c)
        assert e["reset_ok"] and h.extra[2000] == []
    main.SESSIONS.clear()


def test_ws_whitelist_limited_during_practice(pclient):
    login(pclient)
    assert pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).status_code == 200
    with pclient.websocket_connect("/ws/console/2001") as ws:
        assert "练习进行中" in ws.receive_text()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_ws_tabs_share_one_device_connection(env, monkeypatch):
    """真实 ConsoleHub + 假 VRP：两个标签只建一条设备连接，关一个不影响另一个。"""
    from fastapi.testclient import TestClient

    dev = FakeVRP()
    monkeypatch.setattr(main, "connector_factory", dev.factory)
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        login(c)
        with c.websocket_connect("/ws/console/2000") as a:
            assert "[已连接]" in a.receive_text()
            assert "<Huawei>" in a.receive_bytes().decode()
            with c.websocket_connect("/ws/console/2000") as b:
                assert "[已连接]" in b.receive_text()
                assert b.receive_bytes().decode().endswith("<Huawei>")   # 回放
                b.send_bytes(b"display clock\r")
                got = ""
                while "display clock" not in got:
                    got += a.receive_bytes().decode()
            a.send_bytes(b"x")              # 另一个标签关闭后，A 仍然连着同一条设备连接
            got = ""
            while not got.endswith("<Huawei>x"):
                got += a.receive_bytes().decode()
        assert dev.connects == 1 and not dev.conns[0].closed
    main.SESSIONS.clear()


def test_end_to_end_with_real_hub_and_fake_devices(env, monkeypatch):
    """真实 ConsoleHub + 两台假 VRP：注入 → 学员经 WS 修复 → 自动判定通关 → 结束还原，学员看不到隐藏命令。"""
    from fastapi.testclient import TestClient

    ar0, ar2 = FakeVRP(), FakeVRP()
    ar0.responses["ping -c 2 10.0.12.2"] = lambda: (
        "  2 packet(s) transmitted\r\n  {} packet(s) received".format(
            2 if ar0.ips.get("GigabitEthernet0/0/1") == "10.0.12.1"
            and ar2.ips.get("GigabitEthernet0/0/0") == "10.0.12.2" else 0))
    devs = {2000: ar0, 2003: ar2}
    monkeypatch.setattr(main, "connector_factory", lambda port: devs[port].factory(port))
    main.SESSIONS.clear()
    with TestClient(main.app) as c:
        login(c)
        with c.websocket_connect("/ws/console/2003") as ws:
            assert "[已连接]" in ws.receive_text()
            seen = ws.receive_bytes().decode()
            r = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
            assert r.status_code == 200, r.text
            assert r.json()["last_check"]["passed"] == 0
            assert ar0.ips == {"GigabitEthernet0/0/1": "10.0.12.1"}
            assert ar2.ips == {"GigabitEthernet0/0/0": "10.0.21.2"} and ar2.view is None
            for line in ("system-view", "interface GigabitEthernet0/0/0", "ip address 10.0.12.2 24", "return"):
                ws.send_bytes(line.encode() + b"\r")
                while not seen.rstrip().endswith((">", "]")) or line not in seen:
                    seen += ws.receive_bytes().decode()
            end = time.time() + 5
            st = {}
            while time.time() < end and not st.get("passed"):
                st = c.get("/api/practice/current").json()
                time.sleep(0.05)
            assert st["passed"] and st["last_check"]["source"] == "auto"
            e = end_practice(c)
            assert e["restored"] and e["passed"] and e["commands"] == 4
            assert ar0.ips == {} and ar2.ips == {} and ar0.view is None and ar2.view is None
            assert "10.0.21.2" not in seen and "undo ip" not in seen and "ping" not in seen
    main.SESSIONS.clear()


def test_start_busy_until_deadline_does_not_restore_untouched(pclient):
    # 第一台设备一直「暂不可执行」：一条命令都没执行，不应再去还原（以前会报「开始失败且还原未完成」）
    login(pclient)
    hub(pclient).busy = 10 ** 6
    r = pclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 409
    assert "还原未完成" not in r.json()["detail"]
    assert not any(kind == "config" and any("undo" in c for c in cmds) for _, kind, cmds in hub(pclient).calls)
    assert pclient.get("/api/practice/current").json().get("active") in (None, False)


def test_command_outputs_recorded_without_hidden_transactions(env, monkeypatch):
    """真实 ConsoleHub + 假 VRP：学员命令的输出摘要写进 attempt_logs.output，判定 / 注入 / 还原的输出不记；
    学员改配置后的异步日志追加到该设备最近一条命令上。"""
    from fastapi.testclient import TestClient

    ar0, ar2 = FakeVRP(), FakeVRP()
    ar0.responses["ping -c 2 10.0.12.2"] = lambda: (
        "  2 packet(s) transmitted\r\n  {} packet(s) received".format(
            2 if ar2.ips.get("GigabitEthernet0/0/0") == "10.0.12.2" else 0))
    ar2.responses["display ip interface brief"] = "GigabitEthernet0/0/0  10.0.21.2/24  up  up"
    devs = {2000: ar0, 2003: ar2}
    monkeypatch.setattr(main, "connector_factory", lambda port: devs[port].factory(port))
    monkeypatch.setattr(config, "JUDGE_DEBOUNCE", 60.0)     # 不自动判定：命令全部在练习进行中提交
    main.SESSIONS.clear()
    log_line = ("Oct  4 2026 11:02:03-08:00 Huawei %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the "
                "interface GigabitEthernet0/0/0 has entered the DOWN state.")
    with TestClient(main.app) as c:
        login(c)
        with c.websocket_connect("/ws/console/2003") as ws:
            assert "[已连接]" in ws.receive_text()
            seen = ws.receive_bytes().decode()
            r = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
            assert r.status_code == 200, r.text
            aid = r.json()["attempt_id"]
            for line in ("display ip interface brief", "system-view", "interface GigabitEthernet0/0/0",
                         "ip address 10.0.12.2 24"):
                ws.send_bytes(line.encode() + b"\r")
                while not seen.rstrip().endswith((">", "]")) or line not in seen:
                    seen += ws.receive_bytes().decode()
            ar2.emit(f"\r\n{log_line}\r\n{ar2.prompt}")
            seen2 = ""
            while "DOWN" not in seen2:
                seen2 += ws.receive_bytes().decode()
            ws.send_bytes(b"return\r")
            while not seen2.rstrip().endswith("<Huawei>"):
                seen2 += ws.receive_bytes().decode()
            e = end_practice(c)
            assert e["restored"] and e["commands"] == 5
        logs = db.attempt_logs(aid)
        by = {l["command"]: l["output"] for l in logs}
        assert list(by) == ["display ip interface brief", "system-view", "interface GigabitEthernet0/0/0",
                            "ip address 10.0.12.2 24", "return"]
        assert by["display ip interface brief"] == "GigabitEthernet0/0/0  10.0.21.2/24  up  up"
        assert "Enter system view" in by["system-view"]
        assert by["ip address 10.0.12.2 24"] == log_line          # 改配置后的 DOWN 日志
        joined = "\n".join(str(l["output"]) for l in logs)
        assert "packet(s)" not in joined and "undo ip" not in joined and "10.0.21.2 24" not in joined
    main.SESSIONS.clear()


def test_end_with_real_hub_reclaims_stuck_terminals(env, monkeypatch):
    """真实 ConsoleHub + 假 VRP：学员 AR0 停在分页、AR2 停在接口视图且敲了半行，直接结束：强制收回终端、复位成功，
    结束过程中学员的输入被丢弃。"""
    from fastapi.testclient import TestClient

    ar0, ar2 = FakeVRP(), FakeVRP()
    ar0.responses["ping -c 2 10.0.12.2"] = "  2 packet(s) transmitted\r\n  0 packet(s) received"
    ar0.responses["display long"] = "\r\n".join(f"line {i}" for i in range(30))
    devs = {2000: ar0, 2003: ar2}
    monkeypatch.setattr(main, "connector_factory", lambda port: devs[port].factory(port))
    main.SESSIONS.clear()

    def until(ws, seen, cond):
        while not cond(seen):
            seen += ws.receive_bytes().decode()
        return seen

    with TestClient(main.app) as c:
        login(c)
        c.app.state.hub.settle = 0.05
        r = c.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
        assert r.status_code == 200, r.text
        assert ar2.ips == {"GigabitEthernet0/0/0": "10.0.21.2"}
        with c.websocket_connect("/ws/console/2000") as w0, c.websocket_connect("/ws/console/2003") as w2:
            assert "[已连接]" in w0.receive_text() and "[已连接]" in w2.receive_text()
            w0.send_bytes(b"display long\r")
            until(w0, "", lambda s: "---- More ----" in s)
            seen = ""
            for line in ("system-view", "interface GigabitEthernet0/0/0"):
                w2.send_bytes(line.encode() + b"\r")
                seen = until(w2, seen, lambda s, line=line: line in s and s.rstrip().endswith("]"))
            w2.send_bytes(b"ip addr")
            until(w2, "", lambda s: "ip addr" in s)
            assert ar0.pending_pages and ar2.view == "GigabitEthernet0/0/0" and ar2.typed == "ip addr"
            r = c.post("/api/practice/end")
            assert r.status_code == 200 and r.json()["resetting"] is True
            w2.send_bytes(b"ess 1.1.1.1 24\r")                   # 结束过程中学员的输入：丢弃
            end = time.time() + 30
            while time.time() < end and c.get("/api/practice/current").json().get("resetting"):
                time.sleep(0.05)
            e = c.get("/api/practice/current").json()["last_result"]
            assert e["restored"] and e["reset_failed"] == {}, e
            assert ar0.view is None and not ar0.pending_pages
            assert ar2.view is None and ar2.typed == "" and ar2.ips == {}
            assert "1.1.1.1" not in ar2.received
            got = until(w2, "", lambda s: "[复位完成]" in s)
            assert "[练习已结束]" in got
    main.SESSIONS.clear()
