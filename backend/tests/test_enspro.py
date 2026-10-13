"""eNSP Pro 后端：STOMP 帧解析、REST 登录与会话冲突、沙箱 -> 拓扑、provider 状态与启停、console 登录状态机
（首次设密码、正常登录、密码错误只试一次、心跳、断线重连、空闲登出后自动重登、会话失效）。全部用假服务，不连真机。"""
from __future__ import annotations

import asyncio

import pytest

import enspro
from connectors.enspro import ConsoleAuthError, EnsproConnector, StompParser, frame
from console_hub import ConsoleHub
from enspro import EnsproClient, EnsproProvider, EnsproSessionLost, sandbox_to_topology, topo_signature
from fake_enspro import FakeRest, FakeStompServer, FakeYunShan, sandbox_json

PW = "Fake-Pw#1234"


def run(coro, timeout=30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---- STOMP 帧 ----

def test_stomp_parser_multi_split_heartbeat_content_length():
    p = StompParser()
    two = frame("MESSAGE", {"destination": "/a"}, "x") + frame("MESSAGE", {"destination": "/a"}, "y")
    assert [b for _, _, b in p.feed(two)] == ["x", "y"]
    whole = frame("MESSAGE", {"destination": "/a"}, "hello")
    assert p.feed(whole[:7]) == []                       # 半帧等下一条消息
    assert p.feed(whole[7:]) == [("MESSAGE", {"destination": "/a"}, "hello")]
    assert p.feed("\n") == []                            # 心跳
    body = "a\x00b"                                      # content-length：body 里可以有 NUL
    f = "MESSAGE\ndestination:/a\ncontent-length:3\n\n" + body + "\x00\n"
    assert p.feed(f) == [("MESSAGE", {"destination": "/a", "content-length": "3"}, "a\x00b")]
    assert p.feed("中") == [] and p.buf                  # 多字节字符按字节缓存
    assert frame("SEND", {"destination": "/x"}, "ab") == "SEND\ndestination:/x\n\nab\x00"


# ---- REST 客户端 ----

def test_client_login_csrf_and_session_conflict():
    async def go():
        rest = FakeRest()
        c = EnsproClient("http://enspro.test", transport=rest.transport())
        data = await c.sandbox(2)
        assert data["name"] == "ainet_test" and c.logged_in and rest.logins == 1
        assert c.csrf() == "tok1" and "__Host-SESSION=s1" in c.cookie_header()
        rest.kick()                                      # 别处登录了同一账号
        with pytest.raises(EnsproSessionLost) as ei:
            await c.sandbox(2)
        assert "其它地方" in ei.value.message and ei.value.code == 50070014
        with pytest.raises(EnsproSessionLost):           # 没开自动重登：不再自己登录
            await c.sandbox(2)
        assert rest.logins == 1
        await c.relogin()                                # 用户点「重新登录」
        assert (await c.sandbox(2))["id"] == 2 and rest.logins == 2
        await c.aclose()
        assert rest.logouts == 1
    run(go())


def test_client_auto_relogin_respects_interval():
    async def go():
        rest = FakeRest()
        now = [100.0]
        c = EnsproClient("http://enspro.test", transport=rest.transport(), auto_relogin=True,
                         relogin_interval=30, clock=lambda: now[0])
        await c.sandbox(2)
        now[0] += 40
        rest.kick()
        assert (await c.sandbox(2))["id"] == 2           # 自动重登后重试成功
        assert rest.logins == 2
        rest.kick()                                       # 刚登录过：间隔未到，不重登（避免和网页互相挤）
        with pytest.raises(EnsproSessionLost) as ei:
            await c.sandbox(2)
        assert "秒后自动重新登录" in ei.value.message and rest.logins == 2
        now[0] += 31
        assert (await c.sandbox(2))["id"] == 2 and rest.logins == 3
        await c.aclose()
    run(go())


def test_client_not_logged_in_code():
    async def go():
        rest = FakeRest()
        c = EnsproClient("http://enspro.test", transport=rest.transport())
        await c.sandbox(2)
        rest.session = None                               # 会话过期（50010005）
        with pytest.raises(EnsproSessionLost) as ei:
            await c.sandbox(2)
        assert ei.value.code == 50010005
        await c.aclose()
    run(go())


def test_relogin_does_not_reuse_stale_gateway_cookies():
    import httpx

    async def go():
        rest = FakeRest()
        login_cookies = []

        def handle(req):
            if req.url.path == "/api/login":
                login_cookies.append(req.headers.get("cookie", ""))
                if "__Host-SESSION=" in login_cookies[-1]:
                    return httpx.Response(200, text="SSO reused the invalid session")
            return rest.handle(req)

        c = EnsproClient("http://enspro.test", transport=httpx.MockTransport(handle))
        await c.sandbox(2)
        rest.session = None
        with pytest.raises(EnsproSessionLost):
            await c.sandbox(2)
        await c.relogin()
        assert (await c.sandbox(2))["id"] == 2
        assert login_cookies == ["", ""] and rest.logins == 2
        await c.aclose()
    run(go())


def test_response_from_old_session_does_not_invalidate_new_login():
    import httpx

    async def go():
        rest = FakeRest()
        entered, release = asyncio.Event(), asyncio.Event()
        blocked = [False]

        async def handle(req):
            response = rest.handle(req)
            if blocked[0] and req.url.path.endswith("getOneSandbox"):
                blocked[0] = False
                entered.set()
                await release.wait()
            return response

        c = EnsproClient("http://enspro.test", transport=httpx.MockTransport(handle))
        await c.sandbox(2)
        rest.kick()
        blocked[0] = True
        stale = asyncio.create_task(c.sandbox(2))
        await entered.wait()
        await c.relogin()
        release.set()
        assert (await stale)["id"] == 2 and c.logged_in and not c.lost_reason
        await c.aclose()
    run(go())


def test_relogin_reports_simulator_service_failure():
    import httpx
    from lab import LabError

    async def go():
        rest = FakeRest()

        def handle(req):
            if req.url.path.endswith("getByPage"):
                return httpx.Response(200, json={"code": 50010005, "message": "simulator session expired"})
            return rest.handle(req)

        client = EnsproClient("http://enspro.test", transport=httpx.MockTransport(handle))
        provider = EnsproProvider(client=client, sandbox=2, password=lambda: PW)
        with pytest.raises(LabError) as error:
            await provider.relogin()
        assert error.value.status == 502 and not client.logged_in
        await provider.close()
    run(go())


# ---- 沙箱 -> 拓扑 ----

def test_sandbox_to_topology_devices_links_status():
    sb = sandbox_json(2, {6: 4, 7: 0}, [{"id": 1, "leftDeviceId": 6, "leftIfId": 58, "rightDeviceId": 7, "rightIfId": 69}])
    sb["textBoxs"] = [{"content": "测试\n第二行", "positionX": 0.1, "positionY": 0.2}]
    t = sandbox_to_topology(sb)
    assert t["name"] == "ainet_test"
    t1, t2 = t["devices"]
    assert (t1["id"], t1["name"], t1["console"], t1["kind"], t1["model"]) == ("6", "T1", 6, "router", "ENSP-AR")
    assert t1["interfaces"][:3] == ["MEth0/0/0", "GE0/0/0", "GE0/0/1"] and len(t1["interfaces"]) == 11
    assert (t1["x"], t1["y"], t1["anchor"]) == (-750.0, -500.0, "center")
    assert t["links"] == [{"id": 1, "src_id": "6", "src_if": 2, "src_label": "GE0/0/1",
                           "dst_id": "7", "dst_if": 2, "dst_label": "GE0/0/1"}]
    assert t["notes"][0]["lines"] == ["测试", "第二行"] and t["notes"][0]["x"] == 250.0
    assert [enspro.device_status(c) for c in (0, 1, 2, 3, 4, 5, 6, 17, 99, None)] == [
        "stopped", "online", "starting", "error", "online", "error", "starting", "error", "unknown", "unknown"]
    # 指纹只随结构变（状态变化不触发前端重画）
    t_stopped = sandbox_to_topology(sandbox_json(2, {6: 0, 7: 0}, sb["links"]) | {"textBoxs": sb["textBoxs"]})
    assert topo_signature(t) == topo_signature(t_stopped)
    assert topo_signature(t) != topo_signature(sandbox_to_topology(sandbox_json(2)))
    rev = dict(sb, devices=list(reversed(sb["devices"])))       # 接口返回顺序变了：结果与指纹不变
    assert sandbox_to_topology(rev)["devices"][0]["name"] == "T1"
    assert topo_signature(sandbox_to_topology(rev)) == topo_signature(t)


def provider_with(rest: FakeRest, port: int = 1, **kw) -> EnsproProvider:
    client = EnsproClient(f"http://127.0.0.1:{port}", transport=rest.transport())
    return EnsproProvider(client=client, sandbox=2, password=kw.pop("password", lambda: PW), cache_ttl=0, **kw)


def test_provider_resolve_power_and_session_lost():
    async def go():
        rest = FakeRest()
        rest.status = {6: 4, 7: 0}
        p = provider_with(rest)
        cur = await p.resolve()
        assert cur["state"] == "running" and cur["lab"]["kind"] == "enspro" and cur["lab"]["power"]
        assert cur["devices"] == {"6": "online", "7": "stopped"} and (cur["running"], cur["total"]) == (1, 2)
        assert cur["path"] == "enspro://sandbox/2" and isinstance(cur["mtime"], int)
        assert p.allowed(cur) == {6}                       # 只有运行中的设备能开终端
        assert "T2" in p.deny_reason(cur, 7)
        r = await p.power_action("start", [7])
        assert r["ok"] and rest.calls[-1] == ("POST", "/service/ensp/device/batchStart", {"deviceIds": [7], "sandboxId": 2})
        cur = await p.resolve()
        assert cur["devices"]["7"] == "starting" and cur["progress"]["7"].startswith("启动中 40% 0s")
        rest.status[7] = 4
        assert (await p.resolve())["devices"]["7"] == "online"
        await p.power_action("stop", [7])
        rest.status[7] = 4                                 # 停止请求已发出、状态还没变：显示「停止中」
        assert (await p.resolve())["devices"]["7"] == "stopping"
        rest.status[7] = 0
        assert (await p.resolve())["devices"]["7"] == "stopped"
        from lab import LabError
        with pytest.raises(LabError):
            await p.power_action("start", [99])
        rest.kick()
        cur = await p.resolve()
        assert cur["state"] == "session_lost" and cur["relogin"] and "重新登录" in cur["message"]
        assert cur["topology"]["devices"][0]["name"] == "T1"   # 仍画出上次的拓扑
        assert p.allowed(cur) == set()
        await p.relogin()
        assert (await p.resolve())["state"] == "running"
        await p.close()
    run(go())


# ---- console 连接器 ----

class Env:
    def __init__(self, devices: dict[int, FakeYunShan], **kw) -> None:
        self.devices = devices
        self.kw = kw

    async def __aenter__(self):
        self.server = await FakeStompServer(self.devices, char_frames=self.kw.pop("char_frames", True)).start()
        self.rest = FakeRest()
        self.provider = provider_with(self.rest, self.server.port, password=self.kw.pop("password", lambda: PW),
                                      heartbeat=self.kw.pop("heartbeat", 0), quiet=0.15, login_timeout=8)
        return self

    async def __aexit__(self, *a):
        await self.provider.close()
        await self.server.stop()


async def read_until(conn, needle: str, timeout: float = 5) -> str:
    out = ""
    end = asyncio.get_running_loop().time() + timeout
    while needle not in out:
        rem = end - asyncio.get_running_loop().time()
        if rem <= 0:
            raise AssertionError(f"没等到 {needle!r}：{out!r}")
        out += (await asyncio.wait_for(conn.read(), rem)).decode()
    return out


def test_connector_normal_login_then_commands():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            assert conn.login_log == ["enter", "password", "prompt"]
            assert dev.login_attempts == 1 and dev.empty_submits == 0
            assert (await read_until(conn, "<HUAWEI>")).strip().endswith("<HUAWEI>")
            await conn.send(b"display version\r")
            out = await read_until(conn, "<HUAWEI>")
            assert "display version" in out
            sess = env.server.sessions[0]
            assert "__Host-SESSION=" in sess["cookie"] and sess["origin"] == env.provider.client.origin
            assert sess["subs"] == ["/user/topic/console/message/2/6", "/user/topic/console/heartbeat/2/6"]
            await conn.close()
            await asyncio.sleep(0.1)
            assert env.server.closes == 1
            assert await conn.read() == b""
    run(go())


def test_connector_first_login_sets_password():
    async def go():
        dev = FakeYunShan(password="", fresh=True)
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            assert conn.login_log == ["enter", "set-password", "confirm-password", "prompt"]
            assert dev.password == PW and not dev.fresh and dev.auth == "shell"
            await conn.close()
    run(go())


def test_connector_wrong_password_tries_once_and_blocks():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}, password=lambda: "wrong-pass") as env:
            conn = env.provider.connector(6)
            with pytest.raises(ConsoleAuthError):
                await conn.connect()
            assert dev.login_attempts == 1 and dev.empty_submits == 0      # 只试一次，不补空回车
            assert env.provider.blocked(6)
            frames = env.server.connect_frames
            with pytest.raises(ConsoleAuthError) as ei:                    # 之后直接拒绝，不联网
                await env.provider.connector(6).connect()
            assert "不再自动尝试" in str(ei.value) and env.server.connect_frames == frames
            assert dev.login_attempts == 1
            cur = await env.provider.resolve()
            assert "console 登录失败：T1" in cur["message"] and "6" in cur["console_errors"]
            await env.provider.power_action("reboot", [6])                 # 由 ainet 重启设备后允许再试
            assert not env.provider.blocked(6)
    run(go())


def test_connector_no_password_configured():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}, password=lambda: None) as env:
            from connectors.enspro import ConsoleLoginError
            with pytest.raises(ConsoleLoginError) as ei:
                await env.provider.connector(6).connect()
            assert "ENSPRO_DEVICE_PASSWORD" in str(ei.value)
            assert dev.login_attempts == 0 and not env.provider.blocked(6)
    run(go())


def test_connector_already_logged_in_and_heartbeat_and_frames():
    async def go():
        dev = FakeYunShan(PW)
        dev.auth = "shell"                                  # 上个会话没登出：回车直接出提示符
        async with Env({6: dev}, heartbeat=0.1, char_frames=False) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            assert conn.login_log == ["enter", "prompt"] and dev.login_attempts == 0
            await asyncio.sleep(0.35)
            assert env.server.heartbeats >= 2
            await conn.close()
    run(go())


def test_connector_session_lost_before_connect():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            await env.provider.resolve()
            env.rest.kick()
            with pytest.raises(EnsproSessionLost):
                await env.provider.connector(6).connect()
            assert env.server.connect_frames == 0 and dev.wakeups == 0
    run(go())


def test_connector_idle_logout_relogin_once():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            await read_until(conn, "<HUAWEI>")
            dev.logout()                                    # console 空闲超时
            await conn.send(b"\r")                          # 学员按回车：设备问 Password:
            await read_until(conn, "Password:")
            await conn.send(b"display version\r")           # 登录成功后完整转发，不能混进密码
            out = await read_until(conn, "display version", timeout=6)
            assert dev.login_attempts == 2 and dev.empty_submits == 0
            assert "display version" in dev.received
            assert "Authentication failed" not in out
            await conn.close()
    run(go())


def test_buffer_replay_failure_closes_console_without_leaking_input():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            await read_until(conn, "<HUAWEI>")
            original = conn._type

            async def disconnected(text):
                if text == "display version\r":
                    raise RuntimeError("fake disconnection")
                await original(text)
            conn._type = disconnected
            dev.logout()
            await read_until(conn, "Please Press ENTER.")
            await conn.send(b"display version\r")
            await read_until(conn, "console 重登或转发失败")
            await conn._watch
            assert conn._closed and not conn._relogin_input and conn._watch.exception() is None
            assert "display version" not in dev.received
    run(go())


def test_hub_over_enspro_reconnects_after_server_drop():
    async def go():
        dev = FakeYunShan(PW)
        async with Env({6: dev}) as env:
            hub = ConsoleHub(env.provider.connector, min_reconnect=0.3, ready_timeout=3, settle=0.1)
            sub = await hub.subscribe(6)
            r = await hub.run_hidden(6, ["display version"], kind="check")
            assert r.ok, r.reason
            await env.server.kill_all()                     # 服务端断开
            for _ in range(50):
                if not hub.channels[6].connected:
                    break
                await asyncio.sleep(0.05)
            assert not hub.channels[6].connected
            t0 = asyncio.get_running_loop().time()
            r = await hub.run_hidden(6, ["display version"], kind="check")   # 按需重连 + 再次登录
            assert r.ok, r.reason
            # 设备侧会话没登出：重连后回车直接是提示符，不再输密码
            assert dev.connects == 2 and dev.login_attempts == 1 and dev.empty_submits == 0
            assert hub.channels[6].conn.login_log == ["enter", "prompt"]
            assert asyncio.get_running_loop().time() - t0 >= 0.25        # hub 的重连最小间隔
            hub.unsubscribe(sub)
            await hub.close()
    run(go())


def test_connector_skips_enter_when_replay_shows_password_prompt():
    """服务端回放显示设备已停在 Password: 上：不发初始回车（那会提交空密码），直接输一次密码。"""
    async def go():
        dev = FakeYunShan(PW)
        dev.auth = "login"

        class Conns(list):
            def append(self, c):
                super().append(c)
                c.q.put_nowait(b"\r\nPassword:")            # connect 时回放最后一屏
        dev.conns = Conns()
        async with Env({6: dev}) as env:
            conn = env.provider.connector(6)
            await conn.connect()
            assert conn.login_log[0] == "at-password" and "enter" not in conn.login_log
            assert dev.login_attempts == 1 and dev.empty_submits == 0
            await conn.close()
    run(go())
