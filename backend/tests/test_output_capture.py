"""命令输出采集（里程碑 5，过程评分用）：摘要清洗与截断、真实 ConsoleHub + 假设备的 output / log 事件、
隐藏事务的输出不记录。"""
from __future__ import annotations

import asyncio

import console_hub as ch
from console_hub import ConsoleHub
from fake_vrp import ERASE, FakeVRP

LOG = ("Oct  4 2026 11:02:03-08:00 Huawei %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the "
       "interface GigabitEthernet0/0/0 has entered the DOWN state.")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def make() -> tuple[FakeVRP, ConsoleHub]:
    dev = FakeVRP()
    hub = ConsoleHub(dev.factory, idle_close=60, min_reconnect=0, ready_timeout=1.0, settle=0.05,
                     output_max=300)
    return dev, hub


async def type_(hub, sub, text):
    for c in text:
        await hub.write(sub, c.encode())


async def settle(t=0.1):
    await asyncio.sleep(t)


# ---- 纯函数 ----

def test_summarize_strips_more_ansi_control_and_prompt():
    raw = ("\r\nline1\r\n  ---- More ----" + ERASE + "line2\x07\r\n\x1b[32mgreen\x1b[0m\r\n\r\n\r\n\r\nend\r\n<AR0>")
    out = ch.summarize_output(raw)
    assert out == "line1\nline2\ngreen\n\nend"


def test_summarize_drops_echo_line_and_truncates_head_tail():
    raw = "display x\r\n" + "\r\n".join(f"row{i:04d}" for i in range(500)) + "\r\n[AR0]"
    out = ch.summarize_output(raw, limit=200)
    assert not out.startswith("display x")
    assert out.startswith("row0000") and out.rstrip().endswith("row0499")
    assert "省略" in out and len(out) < 260


def test_summarize_backspace_rendering():
    assert ch.summarize_output("\r\nabcX\bd\r\n<R1>") == "abcd"


# ---- 真实 hub + 假设备 ----

def test_hub_emits_output_after_command_with_pages_and_no_hidden_output():
    async def go():
        dev, hub = make()
        dev.responses["display ip interface brief"] = "\r\n".join(f"GE0/0/{i}  10.0.{i}.1/24  up  up"
                                                               for i in range(9))
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        await type_(hub, sub, "display ip interface brief\r")
        await settle()
        await hub.write(sub, b" ")              # 翻页
        await settle()
        evs = [(e, d) for e, d in events if e in ("command", "output")]
        assert evs[0] == ("command", "display ip interface brief")
        assert evs[1][0] == "output"
        out = evs[1][1]
        assert "GE0/0/0" in out and "GE0/0/8" in out and "More" not in out and "\x1b" not in out
        assert "<Huawei>" not in out
        # 隐藏事务（判定）不产生 command / output 事件
        n = len(events)
        dev.responses["display clock"] = "2026-10-04 12:00:00"
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.ok and r.outputs == ["2026-10-04 12:00:00"]
        await settle()
        assert not [e for e in events[n:] if e[0] in ("command", "output")]
        await hub.close()
    run(go())


def test_hub_next_command_closes_previous_and_flush():
    async def go():
        dev, hub = make()
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        dev.silent = True                       # 设备不回提示符：采集一直挂着
        await type_(hub, sub, "ping 1.1.1.1\r")
        await settle()
        assert not [e for e in events if e[0] == "output"]
        hub.flush_output(2000)                  # 结束练习前强制收尾
        assert [e for e in events if e[0] == "output"] == [("output", "")]
        hub.flush_output(2000)                  # 幂等：没有进行中的采集就不再发
        assert len([e for e in events if e[0] == "output"]) == 1
        await hub.close()
    run(go())


def test_hub_log_event_outside_capture():
    async def go():
        dev, hub = make()
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        await type_(hub, sub, "display clock\r")
        await settle()
        dev.emit(f"\r\n{LOG}\r\n<Huawei>")      # 命令结束后才到的异步日志
        await settle()
        logs = [d for e, d in events if e == "log"]
        assert logs == [LOG]
        await hub.close()
    run(go())
