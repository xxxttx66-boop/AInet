"""ConsoleHub：扇出、退订不断开、隐藏事务、输入行跟踪、分页、异步日志、非提示符拒绝。"""
from __future__ import annotations

import asyncio
import time

import console_hub as ch
from console_hub import ConsoleHub
from fake_vrp import FakeVRP

LOG = "Oct  4 2026 11:02:03-08:00 Huawei %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface GigabitEthernet0/0/0 has entered the UP state."


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_boot_output_is_not_a_ready_console_prompt():
    class BootConsole:
        def __init__(self):
            self.output = asyncio.Queue()
            self.sent = []

        async def connect(self):
            self.output.put_nowait(b"#### booting ####")

        async def send(self, data):
            self.sent.append(data)

        async def read(self):
            return await self.output.get()

        async def close(self):
            self.output.put_nowait(b"")

    async def go():
        device = BootConsole()
        hub = ConsoleHub(lambda port: device, ready_timeout=0.05, settle=0.01, min_reconnect=0)
        ready = await hub.check_ready(2000)
        assert ready.status == "busy" and device.sent == [b"\r"]
        device.output.put_nowait(b"\r\n<Huawei>")
        await asyncio.sleep(0.02)
        assert (await hub.check_ready(2000)).ok
        assert device.sent == [b"\r"]
        await hub.close()
    asyncio.run(go())


def make(**kw) -> tuple[FakeVRP, ConsoleHub]:
    dev = FakeVRP()
    hub = ConsoleHub(dev.factory, idle_close=kw.pop("idle_close", 60), min_reconnect=kw.pop("min_reconnect", 0),
                     ready_timeout=1.0, settle=0.05, **kw)
    return dev, hub


async def drain(sub, wait=0.1) -> str:
    out = b""
    await asyncio.sleep(wait)
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        if d is None:
            out += b"<EOF>"
            break
        out += d
    return out.decode()


async def type_(hub, sub, text):
    for c in text:
        await hub.write(sub, c.encode())


# ---- 纯函数 ----

def test_text_helpers():
    raw = "line1\r\n  ---- More ----" + "\x1b[42D" + " " * 42 + "\x1b[42Dline2\r\n<Huawei>"
    assert ch.clean(raw) == "line1\nline2\n<Huawei>"
    assert ch.prompt_of("x\r\n<Huawei>") == "<Huawei>"
    assert ch.prompt_of("x\r\n[Huawei-GigabitEthernet0/0/0]") == "[Huawei-GigabitEthernet0/0/0]"
    assert ch.prompt_of("[V200R003C00]\r\n#") is None
    assert ch.prompt_of("Are you sure? [Y/N]:") is None
    assert ch.prompt_of("Password:") is None
    assert ch.ends_with_more("abc\r\n  ---- More ----")
    assert ch.render_line("<H>disx\b \bplay") == "<H>display"
    assert ch.is_subsequence("return", "[H]ret\nlog\n[H]return\n") > 0
    assert ch.is_subsequence("display", "<H>\n<H>") == -1
    text, logs = ch.split_logs("a\n" + LOG + "\nb")
    assert text == "a\nb" and logs == [LOG]


# ---- 扇出 / 退订 ----

def test_fanout_shared_connection_and_input():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        b = await hub.subscribe(2000)
        assert dev.connects == 1
        await drain(a), await drain(b)
        await type_(hub, a, "display clock\r")
        oa, ob = await drain(a), await drain(b)
        assert "display clock" in oa and "display clock" in ob
        await type_(hub, b, "x")
        assert "x" in await drain(a)
        await hub.close()
    run(go())


def test_wake_only_on_fresh_connection_and_replay():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await asyncio.sleep(0.05)
        assert dev.received == "\r"          # 新建连接唤醒一次
        await type_(hub, a, "dis")            # A 敲了一半
        b = await hub.subscribe(2000)         # B 加入：不发任何字节，不提交 A 的半截命令
        assert dev.received == "\rdis"
        first = await drain(b)
        assert first.endswith("<Huawei>dis")  # 回放最近输出
        await hub.close()
    run(go())


def test_unsubscribe_keeps_connection_then_idle_close():
    async def go():
        dev, hub = make(idle_close=0.3)
        a = await hub.subscribe(2000)
        hub.unsubscribe(a)
        await asyncio.sleep(0.1)
        conn = dev.conns[0]
        assert not conn.closed                # 退订不断开设备连接
        b = await hub.subscribe(2000)         # 空闲期内重开：复用同一连接
        assert dev.connects == 1
        hub.unsubscribe(b)
        await asyncio.sleep(0.5)
        assert conn.closed                    # 空闲超时后才关闭
        await hub.close()
    run(go())


def test_reconnect_after_eof_respects_min_interval():
    async def go():
        dev, hub = make(min_reconnect=0.4)
        a = await hub.subscribe(2000)
        t0 = time.monotonic()                 # 间隔从上一次连接算起（与 hub 一致），不含读输出的耗时
        dev.conns[0].hangup()
        assert "<EOF>" in await drain(a)      # 设备断开：通知订阅者
        await hub.subscribe(2000)
        assert dev.connects == 2
        assert time.monotonic() - t0 >= 0.38  # 重连间隔下限（留出计时精度余量）
        await hub.close()
    run(go())


# ---- 隐藏事务 ----

def test_hidden_check_invisible_and_output_extracted():
    async def go():
        dev, hub = make()
        dev.responses["display ip interface brief"] = "Interface  IP\r\nGE0/0/0  10.0.12.1/24  up  up"
        a = await hub.subscribe(2000)
        await drain(a)
        r = await hub.run_hidden(2000, ["display ip interface brief"])
        assert r.ok, r.reason
        assert r.outputs == ["Interface  IP\nGE0/0/0  10.0.12.1/24  up  up"]
        assert await drain(a) == ""           # 学员什么也看不到
        c = await hub.subscribe(2000)         # 之后打开的标签：回放里也没有隐藏命令
        assert "display ip" not in await drain(c)
        await hub.close()
    run(go())


def test_input_line_tracking_backspace_and_arrows():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        await type_(hub, a, "dis")
        assert hub.input_line(2000) == ("dis", False)
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy" and "输入" in r.reason
        await type_(hub, a, "\x7f\x7f\x7f")   # 退格删光
        assert hub.input_line(2000) == ("", False)
        await asyncio.sleep(0.05)             # 等设备回显退格
        assert (await hub.run_hidden(2000, ["display clock"])).ok
        await hub.write(a, b"\x1b[A")        # 方向键调出历史：内容不可知
        assert hub.input_line(2000)[1] is True
        assert (await hub.run_hidden(2000, ["display clock"])).status == "busy"
        await hub.write(a, b"\x03")          # Ctrl-C 清空
        await asyncio.sleep(0.05)
        assert (await hub.run_hidden(2000, ["display clock"])).ok
        await hub.close()
    run(go())


def test_student_input_during_hidden_is_replayed_after():
    async def go():
        dev, hub = make()
        gate = asyncio.Event()

        async def slow():
            a2 = sub
            await gate.wait()
            await type_(hub, a2, "dis")       # 事务进行中敲字
        dev.responses["display slow"] = lambda: "slow output"
        sub = await hub.subscribe(2000)
        await drain(sub)
        orig = dev.feed

        def feed(data):                       # 事务命令送到设备时放行学员输入
            orig(data)
            if b"\r" in data and dev.executed and dev.executed[-1] == "display slow":
                gate.set()
        dev.feed = feed
        t = asyncio.create_task(slow())
        r = await hub.run_hidden(2000, ["display slow"])
        await t
        assert r.ok and r.outputs == ["slow output"]
        await asyncio.sleep(0.05)
        assert dev.typed == "dis"             # 暂存的输入在事务后写回设备
        out = await drain(sub)
        assert out == "dis" and "slow" not in out
        await hub.close()
    run(go())


def test_more_paging_handled():
    async def go():
        dev, hub = make()
        dev.responses["display current-configuration"] = "\r\n".join(f"line{i}" for i in range(20))
        a = await hub.subscribe(2000)
        await drain(a)
        r = await hub.run_hidden(2000, ["display current-configuration"])
        assert r.ok, r.reason
        assert r.outputs[0].split("\n") == [f"line{i}" for i in range(20)]
        assert "More" not in r.outputs[0] and "\x1b" not in r.outputs[0]
        assert await drain(a) == ""
        await hub.close()
    run(go())


def test_async_log_during_hidden_forwarded_after():
    async def go():
        dev, hub = make()
        dev.responses["display version"] = "VRP V200R003C00"
        a = await hub.subscribe(2000)
        await drain(a)
        dev.log_after = (4, LOG)              # 回显到 "disp" 时插入一条日志
        r = await hub.run_hidden(2000, ["display version"])
        assert r.ok, r.reason
        assert r.outputs == ["VRP V200R003C00"]
        out = await drain(a)
        assert LOG in out                     # 日志照常转发
        assert "display" not in out and "disp" not in out.replace(LOG, "")
        assert out.endswith("<Huawei>")       # 像 VRP 一样重印提示符
        await hub.close()
    run(go())


def test_refuses_when_not_at_prompt():
    async def go():
        dev, hub = make()
        dev.responses["reset counters"] = "Are you sure? [Y/N]:"
        a = await hub.subscribe(2000)
        await drain(a)
        # 停在 [Y/N]
        dev.emit("\r\nAre you sure? [Y/N]:")
        await asyncio.sleep(0.05)
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy" and "提示符" in r.reason
        # 停在分页
        dev.emit("\r\n<Huawei>")
        dev.responses["display long"] = "\r\n".join(str(i) for i in range(20))
        await type_(hub, a, "display long\r")
        await asyncio.sleep(0.05)
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy" and "分页" in r.reason
        await type_(hub, a, "   ")           # 分页中的空格不算输入
        assert hub.input_line(2000) == ("", False)
        await asyncio.sleep(0.05)
        assert (await hub.run_hidden(2000, ["display clock"])).ok
        await hub.close()
    run(go())


def test_config_transaction_returns_to_user_view_and_refuses_in_system_view():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        r = await hub.run_hidden(2000, ["system-view", "interface GigabitEthernet0/0/1",
                                        "ip address 10.0.12.1 24"], kind="config")
        assert r.ok, r.reason
        assert dev.view is None and dev.executed[-1] == "return"   # 自动回到用户视图
        assert await drain(a) == ""
        # 学员在系统视图：配置事务不执行
        await type_(hub, a, "system-view\r")
        await asyncio.sleep(0.05)
        r = await hub.run_hidden(2000, ["system-view"], kind="config")
        assert r.status == "busy" and "系统视图" in r.reason
        # 判定事务在任何视图都能跑，结束时提示符不变
        dev.responses["display clock"] = "2026-10-04"
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.ok and r.outputs == ["2026-10-04"] and dev.view == ""
        await hub.close()
    run(go())


def test_config_error_aborts_and_returns():
    async def go():
        dev, hub = make()
        await hub.subscribe(2000)
        r = await hub.run_hidden(2000, ["system-view", "bogus command", "ip address 1.1.1.1 24"], kind="config")
        assert r.status == "error" and "bogus command" in r.reason
        assert dev.view is None
        assert "ip address 1.1.1.1 24" not in dev.executed
        await hub.close()
    run(go())


def test_hidden_without_subscribers_connects_and_waits_prompt():
    async def go():
        dev, hub = make()
        dev.responses["display clock"] = "now"
        r = await hub.run_hidden(2003, ["display clock"])
        assert r.ok and r.outputs == ["now"] and dev.connects == 1
        await hub.close()
    run(go())


def test_silent_device_reports_no_response():
    async def go():
        dev, hub = make()
        dev.silent = True
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy" and "无响应" in r.reason
        await hub.close()
    run(go())


def test_command_listener_records_submitted_lines():
    async def go():
        dev, hub = make()
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, port, data)))
        a = await hub.subscribe(2000)
        await drain(a)
        await type_(hub, a, "display  vresion")
        await type_(hub, a, "\x7f" * 7 + "version\r")
        await type_(hub, a, "\r")             # 空行不记录
        cmds = [d for ev, p, d in events if ev == "command"]
        assert cmds == ["display  version"]
        assert any(ev == "input" for ev, _, _ in events)
        await hub.close()
    run(go())


def test_config_tolerate_errors_continues():
    async def go():
        dev, hub = make()
        await hub.subscribe(2000)
        r = await hub.run_hidden(2000, ["system-view", "bogus", "ip address 1.1.1.1 24"], kind="config",
                                 tolerate_errors=True)
        assert r.ok and len(r.warnings) == 1 and "bogus" in r.warnings[0]
        assert "ip address 1.1.1.1 24" in dev.executed and dev.view is None
        await hub.close()
    run(go())


DS_LOG = ("Oct  4 2026 15:20:16-08:00 Huawei DS/4/DATASYNC_CFGCHANGE:OID 1.3.6.1.4.1.2011.5.25.191.3.1 "
          "configurations have been changed. The current change number is 4")


def test_switch_log_without_percent_and_settled_line():
    assert ch.LOG_RE.match(DS_LOG) and ch.LOG_RE.match(LOG)
    assert not ch.LOG_RE.match("Oct  4 2026 15:20:16-08:00 Huawei interface GigabitEthernet0/0/1")
    # S5700 在提示符后打印日志、不重印提示符：设备仍停在日志前的提示符上
    assert ch.settled_line("x\r\n[Huawei-Vlanif1]\r\n" + DS_LOG + "\r\n") == "[Huawei-Vlanif1]"
    assert ch.settled_line("x\r\n[Huawei]") == "[Huawei]"
    assert ch.settled_line("x\r\n[Huawei]\r\n") == ""        # 只是换行（输出还在继续）：不往前找
    text, logs = ch.split_logs("a\n" + DS_LOG + "\nb")
    assert text == "a\nb" and logs == [DS_LOG]
    # 停在分页符时插进来的日志：仍算停在分页上（真机 S5700，否则不会按空格，抓配置超时）
    assert ch.ends_with_more("interface GigabitEthernet0/0/20\r\n  ---- More ----\r\n" + DS_LOG)
    assert not ch.ends_with_more("x\r\n" + DS_LOG)


def test_config_done_when_switch_log_follows_prompt():
    async def go():
        dev, hub = make()
        await hub.subscribe(2000)
        dev.responses["ip address 9.9.9.9 24"] = lambda: ""
        orig = dev._execute

        def ex(cmd):
            orig(cmd)
            if cmd.startswith("ip address"):
                dev.emit("\r\n" + DS_LOG + "\r\n")          # 提示符之后来一条日志，不重印提示符
        dev._execute = ex
        t = time.monotonic()
        r = await hub.run_hidden(2000, ["system-view", "interface Vlanif1", "ip address 9.9.9.9 24"],
                                 kind="config", cmd_timeout=3)
        assert r.ok, r.reason
        assert time.monotonic() - t < 2.5                     # 没有等到超时
        await hub.close()
    run(go())


def test_config_groups_skip_rest_when_view_command_fails():
    async def go():
        dev, hub = make()
        await hub.subscribe(2000)
        r = await hub.run_hidden(2000, [], kind="config", groups=[
            (["system-view", "bogus-view", "undo ip address"], 2),       # 进视图失败：后面不执行
            (["system-view", "interface GigabitEthernet0/0/1", "bogus", "ip address 1.1.1.1 24"], 2),
        ])
        assert r.ok
        assert r.failed == ["bogus-view", "undo ip address", "bogus"]
        assert "undo ip address" not in dev.executed and "ip address 1.1.1.1 24" in dev.executed
        assert dev.view is None
        await hub.close()
    run(go())


def test_reset_replay_keeps_only_current_prompt():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await type_(hub, a, "display answer\r")
        await asyncio.sleep(0.05)
        hub.reset_replay(2000)                # 练习开始：之后打开的标签看不到上一次的命令
        b = await hub.subscribe(2000)
        assert await drain(b) == "<Huawei>"
        assert ch.prompt_of(hub.channels[2000].tail) == "<Huawei>"
        assert (await hub.run_hidden(2000, ["display clock"])).ok   # 前提检查仍能识别提示符
        await hub.close()
    run(go())


def test_pinned_port_not_idle_closed():
    async def go():
        dev, hub = make(idle_close=0.1)
        hub.pin([2000])
        a = await hub.subscribe(2000)
        hub.unsubscribe(a)
        await asyncio.sleep(0.3)
        assert not dev.conns[0].closed        # 练习进行中：没有订阅者也保持连接
        hub.unpin([2000])
        await asyncio.sleep(0.3)
        assert dev.conns[0].closed
        await hub.close()
    run(go())


def test_nudge_when_wake_returned_no_prompt():
    # 真机现象：新连接的唤醒回车只换回空行、没出提示符（最后一行为 ''），之前直接报「不在命令提示符」。
    # 现在学员输入行为空时补发回车，拿到提示符后正常执行。
    async def go():
        dev, hub = make()
        dev.silent = True
        await hub._ensure_connected(hub._channel(2000))   # 唤醒的回车被吞
        await asyncio.sleep(0.05)
        dev.silent = False
        dev.emit("\r\n")                                   # 只来了一个空行
        await asyncio.sleep(0.05)
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.ok, r.reason
        assert dev.executed[-1] == "display clock"
        await hub.close()
    run(go())


def test_no_nudge_while_student_typing():
    # 学员输入行非空时绝不补发回车（会提交学员敲了一半的命令）
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        await hub.write(a, b"disp")
        await asyncio.sleep(0.05)
        sent_before = dev.received
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy"
        assert dev.received == sent_before                 # 没有多发任何字节
        await hub.close()
    run(go())


def test_backspace_del_translated_to_ctrl_h():
    # xterm.js 的 Backspace 发 0x7f；VRP console 只认 0x08，真机上 0x7f 只响铃不删字符
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        await hub.write(a, b"disx\x7fplay clock\r")
        await asyncio.sleep(0.1)
        assert "\x7f" not in dev.received and "disx\x08play clock" in dev.received
        assert dev.executed[-1] == "display clock"
        assert hub.input_line(2000) == ("", False)     # 回车后输入行清空，跟踪与设备一致
        await hub.close()
    run(go())


# ---- 结束练习：冻结学员输入、强制收回终端 ----

def test_reclaim_from_more_system_view_and_half_line():
    """学员停在分页 / 子视图里敲了半行：freeze 后输入被丢弃，reclaim 把设备带回用户视图空提示符，配置事务不再 busy。"""
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        # 停在分页
        dev.responses["display long"] = "\r\n".join(str(i) for i in range(20))
        await type_(hub, a, "display long\r")
        await asyncio.sleep(0.05)
        assert (await hub.run_hidden(2000, ["system-view"], kind="config")).status == "busy"
        hub.freeze([2000], "\r\n[练习结束] 正在复位\r\n")
        assert "[练习结束]" in await drain(a)
        r = await hub.reclaim(2000)
        assert r.ok and r.prompt == "<Huawei>", r.reason
        assert not dev.pending_pages and dev.view is None
        # 子视图 + 半行输入
        hub.unfreeze([2000])
        await type_(hub, a, "system-view\r")
        await type_(hub, a, "interface GigabitEthernet0/0/1\r")
        await type_(hub, a, "ip addr")
        await asyncio.sleep(0.05)
        assert dev.view == "GigabitEthernet0/0/1" and dev.typed == "ip addr"
        hub.freeze([2000])
        before = dev.received
        await type_(hub, a, "ess 1.1.1.1 24\r")      # 冻结期间学员的输入一律丢弃
        assert dev.received == before
        r = await hub.reclaim(2000)
        assert r.ok, r.reason
        assert dev.view is None and dev.typed == "" and "ip address" not in " ".join(dev.executed)
        assert hub.input_line(2000) == ("", False)
        r = await hub.run_hidden(2000, ["system-view", "interface GigabitEthernet0/0/1",
                                        "ip address 10.0.12.1 24"], kind="config")
        assert r.ok, r.reason
        await hub.close()
    run(go())


def test_reclaim_answers_confirm_with_no_and_stops_running_command():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        dev.emit("\r\nAre you sure? [Y/N]:")     # 学员停在 [Y/N]
        await asyncio.sleep(0.05)
        hub.freeze([2000])
        r = await hub.reclaim(2000)
        assert r.ok, r.reason
        assert "n\r" in dev.received and "y" not in dev.received.replace("display", "")
        dev.emit("\r\n  PING 10.0.0.2: 56  data bytes, press CTRL_C to break\r\n    Request time out")   # 命令还在执行
        await asyncio.sleep(0.05)
        r = await hub.reclaim(2000)
        assert r.ok and dev.received.endswith("\x03"), r.reason
        await hub.close()
    run(go())


def test_reclaim_error_when_device_silent():
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        dev.emit("\r\nsome stuck output")
        dev.silent = True                       # 设备不再有任何输出
        hub.reclaim_timeout = 1.0
        r = await hub.reclaim(2000)
        assert r.status == "error" and "无法收回" in r.reason
        await hub.close()
    run(go())


def test_reclaim_after_relogin_text_does_not_wait():
    """console 曾被登出又自动重登（tail 里还留着 Please Press ENTER）、学员随后进了系统视图：
    只看最后一个非空行判断登录态，照常 return 回用户视图，不白等。"""
    async def go():
        dev, hub = make()
        a = await hub.subscribe(2000)
        await drain(a)
        dev.emit("\r\nUser interface con0 is available\r\n\r\nPlease Press ENTER.\r\n\r\n\r\nPassword:\r\n\r\n<Huawei>")
        await asyncio.sleep(0.05)
        await type_(hub, a, "system-view\r")
        await asyncio.sleep(0.05)
        assert dev.view == ""
        hub.freeze([2000])
        t0 = time.monotonic()
        r = await hub.reclaim(2000)
        assert r.ok and dev.view is None, r.reason
        assert time.monotonic() - t0 < 2
        await hub.close()
    run(go())
