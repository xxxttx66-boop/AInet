"""AINET_LAB_PROVIDER=enspro 时的整条链路（全部假服务）：拓扑接口、场景就绪（按平台）、终端 WebSocket（STOMP console）、
练习开始（快照 / baseline / 注入 / 确认故障）→ 学员经终端修复 → 判定通关 → 结束（restore + 整机复位，与开练前快照一致）、
练习中不能停止本题设备、设备启停接口、会话被挤掉后的状态与重新登录。"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

import config
import config_reset as cr
import lab as lab_lib
import main
from enspro import EnsproClient, EnsproProvider
from fake_enspro import FakeRest, FakeStompServer, NetYunShan
from fake_vrp import FakeNet
from practice_helpers import end_practice

BASE = (Path(__file__).parent / "fixtures" / "configs" / "yunshan_ar6700_base.cfg").read_text()
PW = "Fake-Pw#1234"


class ServerThread:
    """假 STOMP 服务跑在独立线程的事件循环里（TestClient 的应用在另一个线程）。"""

    def __init__(self, devices) -> None:
        self.devices = devices
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(FakeStompServer(self.devices).start())
        self.ready.set()
        self.loop.run_forever()

    def __enter__(self):
        self.t.start()
        self.ready.wait(5)
        return self.server

    def __exit__(self, *a):
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)


@pytest.fixture
def env(db_path, monkeypatch):
    from fastapi.testclient import TestClient

    net = FakeNet([(("T1", "GE0/0/1"), ("T2", "GE0/0/1"))])
    t1 = net.add("T1", NetYunShan(BASE, password=PW))
    t2 = net.add("T2", NetYunShan(BASE, password=PW))
    t1.auth = t2.auth = "idle"                 # 首次连接要输密码
    rest = FakeRest()
    rest.links = [{"id": 5, "leftDeviceId": 6, "leftIfId": 58, "rightDeviceId": 7, "rightIfId": 69}]
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "JUDGE_DEBOUNCE", 60.0)          # 只用「立即检查」，不自动判定
    monkeypatch.setattr(config, "HUB_MIN_RECONNECT", 0.2)
    with ServerThread({6: t1, 7: t2}) as server:
        def make(kind=None, **kw):
            client = EnsproClient(f"http://127.0.0.1:{server.port}", transport=rest.transport())
            return EnsproProvider(client=client, sandbox=2, password=lambda: PW, cache_ttl=0,
                                  quiet=0.15, login_timeout=8, heartbeat=0)
        monkeypatch.setattr(lab_lib, "make_provider", make)
        main.SESSIONS.clear()
        with TestClient(main.app) as c:
            r = c.post("/api/login", json={"login": "admin", "password": "admin"})
            assert r.status_code == 200
            yield {"c": c, "t1": t1, "t2": t2, "rest": rest, "server": server}
        main.SESSIONS.clear()
    assert rest.logouts >= 1                     # 服务退出时登出 eNSP Pro，释放单会话


def recv_until(ws, needle: str, limit: int = 200) -> str:
    out = ""
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes"):
            out += msg["bytes"].decode("utf-8", "replace")
        elif msg.get("text"):
            out += msg["text"]
        if needle in out:
            return out
    raise AssertionError(f"没等到 {needle!r}：{out[-300:]!r}")


def test_enspro_topology_practice_power_flow(env):
    c, t1, t2, rest = env["c"], env["t1"], env["t2"], env["rest"]
    cur = c.get("/api/topology/current").json()
    assert cur["state"] == "running" and cur["lab"] == {"kind": "enspro", "platform": "yunshan", "power": True,
                                                         "label": "eNSP Pro"}
    t = cur["topology"]
    assert [(d["name"], d["console"], d["status"]) for d in t["devices"]] == [("T1", 6, "online"), ("T2", 7, "online")]
    assert t["links"][0]["src_label"] == "GE0/0/1" and t["links"][0]["dst_label"] == "GE0/0/1"
    sc = {s["id"]: s for s in c.get("/api/scenarios?all=1").json()["scenarios"]}   # all=1：不按平台过滤
    assert sc["enspro-t1-t2-link"]["ready"], sc["enspro-t1-t2-link"]["reason"]
    assert not sc["campus-ar0-ar2-link"]["ready"] and "eNSP Pro" in sc["campus-ar0-ar2-link"]["reason"]

    snap1, snap2 = t1.config_text(), t2.config_text()
    r = c.post("/api/practice/start", json={"scenario_id": "enspro-t1-t2-link", "seed": 0})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["phase"] == "running" and st["devices"] == [{"name": "T1", "port": 6}, {"name": "T2", "port": 7}]
    assert st["last_check"]["all"] is False                       # 故障已生效
    assert t1.login_attempts == 1 and t2.login_attempts == 1 and t1.empty_submits == t2.empty_submits == 0
    assert "ip address 10.0.21.2 255.255.255.0" in t2.config_text()

    # 练习中不能停止本题设备
    r = c.post("/api/lab/devices/stop", json={"devices": [7]})
    assert r.status_code == 409 and "T2" in r.json()["detail"]

    # 学员经终端（WebSocket -> hub -> STOMP console）修复 T2
    with c.websocket_connect("/ws/console/7") as ws:
        recv_until(ws, "[已连接] eNSP Pro T2")
        for line in ("system-view", "interface GE0/0/1", "ip address 10.0.12.2 24", "return"):
            ws.send_bytes((line + "\r").encode())
            recv_until(ws, "]" if line != "return" else "<HUAWEI>")
        ws.send_bytes(b"display ip interface brief\r")
        out = recv_until(ws, "<HUAWEI>")
        assert "10.0.12.2/24" in out
    r = c.post("/api/practice/check").json()
    assert r["check"]["all"] is True and r["passed"] is True

    res = end_practice(c)
    assert res["passed"] and res["reset_ok"] and res["restored"], res
    assert cr.same(snap1, t1.config_text()) and cr.same(snap2, t2.config_text())
    assert t1.empty_submits == t2.empty_submits == 0

    # 练习结束后可以启停
    r = c.post("/api/lab/devices/stop", json={"devices": [7]})
    assert r.status_code == 200 and rest.status[7] == 0
    assert c.get("/api/topology/status").json()["devices"]["7"] == "stopped"
    with c.websocket_connect("/ws/console/7") as ws:                 # 已停止的设备不放行终端
        assert "不在白名单" in recv_until(ws, "白名单")
    r = c.post("/api/lab/devices/start", json={"devices": [7]})
    assert r.status_code == 200 and c.get("/api/topology/status").json()["devices"]["7"] == "starting"
    r = c.post("/api/lab/devices/start", json={"devices": [99]})
    assert r.status_code == 404


def _record_hidden(c) -> list[int]:
    """记录之后经 hub 隐藏事务访问过的 console 键（还原 / 复位都走 run_hidden）。"""
    hub, ports = c.app.state.practice.hub, []
    orig = hub.run_hidden

    async def spy(port, *a, **kw):
        ports.append(port)
        return await orig(port, *a, **kw)
    hub.run_hidden = spy
    return ports


def test_end_with_all_devices_powered_off(env):
    """练习中本题设备被关机：结束时不去连 console，练习正常结束（放弃），reset_ok=False，note 说明。"""
    c, t2, rest = env["c"], env["t2"], env["rest"]
    r = c.post("/api/practice/start", json={"scenario_id": "enspro-t1-t2-link", "seed": 0})
    assert r.status_code == 200, r.text
    rest.status[6] = rest.status[7] = 0          # 在 eNSP Pro 界面上停了 T1、T2
    ports = _record_hidden(c)
    res = end_practice(c)
    assert res["active"] is False and res["passed"] is False and res["reset_ok"] is False, res
    assert res["restored"] is False and res["powered_off"] == ["T1", "T2"] and res["reset"] == {}
    assert "T1、T2" in res["note"] and "已关机" in res["note"] and "干净基线" in res["note"]
    assert ports == []                               # 没有去连已关机设备的 console
    assert "ip address 10.0.21.2 255.255.255.0" in t2.config_text()   # restore 没执行
    assert c.get("/api/practice/current").json()["active"] is False
    assert c.post("/api/practice/end").status_code == 409             # 已结束，不再锁住


def test_end_with_some_devices_powered_off(env):
    """只有 T2 关机：T2 跳过，T1 照常复位到开练前快照。"""
    c, t1, t2, rest = env["c"], env["t1"], env["t2"], env["rest"]
    snap1 = t1.config_text()
    r = c.post("/api/practice/start", json={"scenario_id": "enspro-t1-t2-link", "seed": 0})
    assert r.status_code == 200, r.text
    rest.status[7] = 0
    ports = _record_hidden(c)
    res = end_practice(c)
    assert res["active"] is False and res["reset_ok"] is False and res["powered_off"] == ["T2"], res
    assert list(res["reset"]) == ["T1"] and res["reset"]["T1"]["status"] == "ok"
    assert "T2" in res["note"] and "其余设备已复位" in res["note"]
    assert ports and set(ports) == {6}               # 只连了开着的 T1
    assert cr.same(snap1, t1.config_text())
    assert "ip address 10.0.21.2 255.255.255.0" in t2.config_text()


def test_enspro_session_lost_and_relogin(env):
    c, rest = env["c"], env["rest"]
    assert c.get("/api/topology/status").json()["state"] == "running"
    rest.kick()
    st = c.get("/api/topology/status").json()
    assert st["state"] == "session_lost" and st["relogin"] and "其它地方" in st["message"]
    with c.websocket_connect("/ws/console/6") as ws:
        assert "不在白名单" in recv_until(ws, "白名单")
    assert c.post("/api/lab/relogin").json() == {"ok": True}
    assert c.get("/api/topology/status").json()["state"] == "running"


def test_relogin_rebuilds_console_connections_without_clearing_practice_locks(env):
    c, rest = env["c"], env["rest"]
    hub = c.app.state.hub
    with c.websocket_connect("/ws/console/6") as ws:
        recv_until(ws, "<HUAWEI>")
        old = hub.channels[6].conn
        c.portal.call(hub.pin, [6])
        c.portal.call(hub.freeze, [7])
        rest.kick()
        assert c.get("/api/topology/status").json()["state"] == "session_lost"
        result = c.post("/api/lab/relogin")
        assert result.status_code == 200 and result.json() == {"ok": True}
        assert hub.channels[6].conn is None
        assert 6 in hub.pinned and 7 in hub.frozen
    with c.websocket_connect("/ws/console/6") as ws:
        recv_until(ws, "<HUAWEI>")
        assert hub.channels[6].conn is not old
    c.portal.call(hub.unfreeze, [7])


def test_ensp_provider_rejects_power(client):
    client.post("/api/login", json={"login": "admin", "password": "admin"})
    assert client.get("/api/lab").json()["kind"] == "ensp"
    r = client.post("/api/lab/devices/start", json={"devices": [2000]})
    assert r.status_code == 400 and "不支持" in r.json()["detail"]
