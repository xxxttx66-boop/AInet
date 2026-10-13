"""里程碑 4（配置复位）与里程碑 5（输出采集 / AI 评分）的集成点：

- 结束顺序：场景 restore → 整机复位并比对 → 复位结果落库（attempts.reset_json）→ 再触发评分；
  评分把复位结果交给 AI（复位失败作为系统问题告知、不扣学员分），重新评分也拿得到；
- 隐藏事务（复位的按组执行 / 自动 [Y/N] / sysname 改名）不产生 command / output 事件，事务结束后重印的提示符
  不进学员命令的输出；进入隐藏前先结束学员上一条命令的采集；学员刚回车、设备未回显时事务 busy；
- 学员自己改 sysname：提示符变化不打断「命令结束」判定，每条命令都有对应的输出；
- S5700 的 DATASYNC 配置同步日志：不作为 log 事件、不进输出摘要；它之后的新行（交换机不重印提示符）上提交的命令
  照样记录并采集输出；评分的 Down 信号不把它算进去。
AI 客户端一律 mock。
"""
from __future__ import annotations

import asyncio
import json

import ai
import config
import console_hub as chm
import db
import grading
import scenarios as sc
from console_hub import ConsoleHub
from fake_vrp import ConfigVRP, FakeVRP
from test_grading import GOOD, FakeAI, _poll, aclient  # noqa: F401  (aclient 是 fixture)
from test_practice_api import env, hub, login  # noqa: F401
from practice_helpers import end_practice

DS_LOG = ("Oct  4 2026 15:20:16-08:00 Huawei DS/4/DATASYNC_CFGCHANGE:OID 1.3.6.1.4.1.2011.5.25.191.3.1 "
          "configurations changed. The current change number is 3, the change loop count is 0, and the "
          "maximum number of records is 4095.")
DOWN_LOG = ("Oct  4 2026 11:02:03-08:00 Huawei %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the "
            "interface GigabitEthernet0/0/0 has entered the DOWN state.")
CAMPUS = sc.load_dir(config.SCENARIOS_DIR)[0]["campus-ar0-ar2-link"]

CFG = """!Software Version V200R003C00SPC200
#
sysname Huawei
#
interface GigabitEthernet0/0/0
#
ospf 1
 area 0.0.0.0
#
return"""


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def type_(hub, sub, text):
    for c in text:
        await hub.write(sub, c.encode())


async def settle(t=0.15):
    await asyncio.sleep(t)


def _hub(dev) -> ConsoleHub:
    return ConsoleHub(dev.factory, idle_close=60, min_reconnect=0, ready_timeout=1.0, settle=0.05)


def _cmd_out(events):
    return [(e, d) for e, d in events if e in ("command", "output")]


# ---- hub：隐藏事务与输出采集 ----

def test_reset_style_transaction_not_recorded_and_reprinted_prompt_not_in_output():
    """按组执行 + 自动 [Y/N] + sysname 改名的隐藏事务：不产生 command / output；结束后重印的新提示符
    不会被算进学员任何一条命令的输出，学员之后的命令照常采集。"""
    async def go():
        dev = ConfigVRP(CFG)
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        await type_(hub, sub, "display current-configuration\r")
        await settle()
        evs = _cmd_out(events)
        assert evs[0] == ("command", "display current-configuration") and "ospf 1" in evs[1][1]
        n = len(events)
        r = await hub.run_hidden(2000, [], kind="config", confirm=True, groups=[
            (["system-view", "undo ospf 1"], 1),          # 会问 [Y/N]，自动回答
            (["system-view", "sysname R9"], 1),           # 改提示符
        ])
        assert r.ok, r
        assert r.prompt == "<R9>" and dev.confirms == 1
        await settle()
        assert _cmd_out(events[n:]) == []                  # 隐藏事务不记成学员命令 / 输出
        await type_(hub, sub, "display current-configuration\r")
        await settle()
        evs = _cmd_out(events[n:])
        assert [e for e, _ in evs] == ["command", "output"] and evs[0][1] == "display current-configuration"
        assert "sysname R9" in evs[1][1] and "ospf 1" not in evs[1][1]
        assert not any("<R9>" in d or "<Huawei>" in d for e, d in events if e == "output")
        await hub.close()
    run(go())


def test_pending_capture_finished_before_hidden_and_not_polluted():
    """进入隐藏事务前，学员上一条命令的采集先结束：事务结束时补发的日志与重印的提示符不进它的输出。"""
    async def go():
        dev = ConfigVRP(CFG)
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        await type_(hub, sub, "display clock\r")
        await settle()
        ch = hub.channels[2000]
        # 模拟采集器没认出命令结束（例如输出里夹着奇怪的行）：仍处于采集中、已有部分输出
        ch.rec.start()
        ch.rec.head = "\r\npartial output"
        n = len(events)
        dev.log_after = None
        r = await hub.run_hidden(2000, ["system-view", "sysname R9"], kind="config")
        assert r.ok and r.prompt == "<R9>"
        await settle()
        outs = [d for e, d in events[n:] if e == "output"]
        assert outs == ["partial output"]                  # 在事务开始前就交出，没有带上 <R9>
        assert not ch.rec.active
        await hub.close()
    run(go())


def test_logs_reprinted_after_hidden_go_to_log_event_not_student_output():
    """隐藏事务期间设备吐出的异步日志，事务结束后补发给学员终端：它们作为 log 事件交出，
    不会拼进学员上一条（采集器还没收尾的）命令的输出。"""
    async def go():
        dev = FakeVRP()
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        ch = hub.channels[2000]
        ch.rec.start()
        ch.rec.head = "\r\npartial output"
        dev.responses["display clock"] = "2026-10-04 12:00:00"
        dev.log_after = (3, DOWN_LOG)                     # 事务命令回显到一半时设备打印日志
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.ok and r.outputs == ["2026-10-04 12:00:00"]
        await settle()
        assert [d for e, d in events if e == "output"] == ["partial output"]
        assert [d for e, d in events if e == "log"] == [DOWN_LOG]
        await hub.close()
        del sub
    run(go())


def test_hidden_busy_while_student_command_just_submitted():
    async def go():
        dev = FakeVRP()
        hub = _hub(dev)
        sub = await hub.subscribe(2000)
        await settle()
        ch = hub.channels[2000]
        ch.rec.start()                                     # 学员刚回车，设备还没回显
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.status == "busy" and "回显" in r.reason
        hub.flush_output(2000)
        r = await hub.run_hidden(2000, ["display clock"])
        assert r.ok
        await hub.close()
        del sub
    run(go())


def test_student_sysname_change_keeps_output_capture():
    """学员自己改 sysname：提示符从 [Huawei] 变成 [R9]，「命令结束」照常判定，每条命令都有自己的输出。"""
    async def go():
        dev = ConfigVRP(CFG)
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        for line in ("system-view", "sysname R9", "display current-configuration", "return",
                     "display current-configuration"):
            await type_(hub, sub, line + "\r")
            await settle()
        evs = _cmd_out(events)
        assert [d for e, d in evs if e == "command"] == ["system-view", "sysname R9", "display current-configuration",
                                                         "return", "display current-configuration"]
        # command / output 严格交替：改名那条命令也在新提示符 [R9] 处结束
        assert [e for e, _ in evs] == ["command", "output"] * 5
        outs = [d for e, d in evs if e == "output"]
        assert outs[1] == "" and "sysname R9" in outs[2] and "sysname R9" in outs[4]
        assert not any("[R9]" in o or "<R9>" in o for o in outs)
        assert dev.host == "R9"
        # 隐藏事务按新提示符执行
        r = await hub.run_hidden(2000, ["display current-configuration"])
        assert r.ok and "sysname R9" in r.outputs[0]
        await hub.close()
    run(go())


def test_datasync_log_not_emitted_and_next_command_still_recorded():
    """S5700：改配置后约 7 秒打印 DATASYNC 日志且不重印提示符。它不作为 log 事件；学员接着在新行上敲的命令
    仍按命令记录、输出照常采集；真正的 Down 日志仍作为 log 事件。"""
    async def go():
        dev = FakeVRP()
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        dev.responses["display vlan"] = "VID  Type    Ports\r\n10   common  GE0/0/1"
        dev.emit(f"\r\n{DS_LOG}\r\n")                       # 不重印提示符
        await settle()
        assert [d for e, d in events if e == "log"] == []
        await type_(hub, sub, "display vlan\r")
        await settle()
        assert _cmd_out(events) == [("command", "display vlan"), ("output", "VID  Type    Ports\n10   common  GE0/0/1")]
        dev.emit(f"\r\n{DOWN_LOG}\r\n{dev.prompt}")
        await settle()
        assert [d for e, d in events if e == "log"] == [DOWN_LOG]
        await hub.close()
    run(go())


def test_datasync_inside_command_output_stripped_and_command_ends_at_prompt():
    async def go():
        dev = FakeVRP()
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2000)
        await settle()
        dev.responses["display clock"] = "2026-10-04 12:00:00"
        dev.silent = False
        await type_(hub, sub, "display clock\r")
        await settle()
        # 下一条命令：输出停在提示符后，紧跟一条 DATASYNC（同一批数据里，交换机不重印提示符）
        orig = dev._execute

        def execute(cmd):
            if cmd == "display vlan":
                dev.executed.append(cmd)
                dev.emit(f"\r\n10 common\r\n{dev.prompt}\r\n{DS_LOG}\r\n")
            else:
                orig(cmd)
        dev._execute = execute
        await type_(hub, sub, "display vlan\r")
        await settle()
        # 不等下一条命令：设备停在（日志之前的）提示符上，这条命令的采集就此结束
        outs = [d for e, d in events if e == "output"]
        assert outs == ["2026-10-04 12:00:00", "10 common"]
        await hub.close()
    run(go())


def test_log_between_more_and_erase_does_not_eat_config_line():
    """2026-10-04 真机（S5700，集成 e2e 的 C 题开练快照）：DATASYNC 日志插在 ---- More ---- 之后，擦除序列和下一行配置
    紧跟在日志行末尾、不换行。以前整行被当成日志拿掉，快照丢了 interface GigabitEthernet0/0/24。"""
    erase = "\x1b[42D" + " " * 42 + "\x1b[42D"
    raw = ("#\r\ninterface GigabitEthernet0/0/23\r\n#\r\n  ---- More ----\r\n" + DS_LOG + erase
           + "interface GigabitEthernet0/0/24\r\n#\r\ninterface NULL0\r\n#\r\nreturn\r\n<Huawei>")
    text, logs = chm.split_logs(chm.clean(raw))
    assert logs == [DS_LOG]
    assert "interface GigabitEthernet0/0/24\n#\ninterface NULL0" in text
    # 学员侧摘要同样不丢行、也不带日志
    assert "interface GigabitEthernet0/0/24" in chm.summarize_output("display cur\r\n" + raw)
    # 学员自己的行编辑（不在日志行上）里的擦除序列不受影响
    assert chm.strip_more("<Huawei>dis" + erase + "display") == "<Huawei>dis" + erase + "display"


def test_hidden_capture_with_log_at_page_break_keeps_all_lines():
    class LogAtMore(FakeVRP):
        def _next_page(self):
            self.emit("\r\n" + DS_LOG)                    # 日志插在分页符之后、擦除序列之前，不换行
            super()._next_page()

    async def go():
        dev = LogAtMore(page=4)
        lines = [f"interface GigabitEthernet0/0/{i}\r\n#" for i in range(1, 7)]
        dev.responses["display current-configuration"] = "\r\n".join(lines)
        hub = _hub(dev)
        sub = await hub.subscribe(2001)
        await settle()
        r = await hub.run_hidden(2001, ["display current-configuration"])
        assert r.ok
        got = [l for l in r.outputs[0].split("\n") if l.startswith("interface")]
        assert got == [f"interface GigabitEthernet0/0/{i}" for i in range(1, 7)]
        await hub.close()
        del sub
    run(go())


def test_summarize_drops_noise_logs():
    raw = f"display vlan\r\nVID 10\r\n{DS_LOG}\r\n\r\n<Huawei>"
    assert chm.summarize_output(raw) == "VID 10"
    assert chm.is_noise_log(DS_LOG) and not chm.is_noise_log(DOWN_LOG) and chm.is_log(DOWN_LOG)
    assert chm.input_after_prompt(f"<Huawei>\r\n{DS_LOG}\r\ndisplay vl") == "display vl"
    assert chm.input_after_prompt("Warning: continue? [Y/N]:y") is None
    assert chm.input_after_prompt(f"some output\r\n{DS_LOG}\r\nfoo") is None
    # 日志行末尾不换行、学员的回显直接接在日志后面（真机 S5700 改名后）：在提示符后，但回显分不出来
    assert chm.input_after_prompt(f"[SW-TEST]\r\n{DS_LOG}display th") == ""
    assert chm.input_after_prompt(f"some output\r\n{DS_LOG}display th") is None


def test_command_echoed_onto_unterminated_log_line_still_recorded():
    """2026-10-04 真机（集成 AI e2e）：LSW0 上 sysname SW-TEST 后打印 `[SW-TEST]` + 换行 + DATASYNC 日志（不换行），
    学员接着敲的 display this 回显在日志行末尾。以前这条命令没被记录（也不触发自动判定）。"""
    async def go():
        dev = FakeVRP(host="SW-TEST")
        dev.view = ""
        hub = _hub(dev)
        events = []
        hub.add_listener(lambda ev, port, data: events.append((ev, data)))
        sub = await hub.subscribe(2001)
        await settle()
        dev.responses["display this"] = "#\r\nsysname SW-TEST\r\n#\r\nreturn"
        dev.emit(f"\r\n{DS_LOG}")                           # 日志后不换行、不重印提示符
        await settle()
        await type_(hub, sub, "display this\r")
        await settle()
        evs = _cmd_out(events)
        assert evs[0] == ("command", "display this")
        assert evs[1][0] == "output" and "sysname SW-TEST" in evs[1][1] and "DATASYNC" not in evs[1][1]
        assert [d for e, d in events if e == "log"] == []
        await hub.close()
        del sub
    run(go())


def test_grading_down_signal_ignores_datasync():
    logs = [{"command": "display ip interface brief", "output": "x", "device": "LSW0"},
            {"command": "port default vlan 10", "output": f"{DS_LOG}", "device": "LSW0"},
            {"command": "ip address 10.0.1.1 24", "output": DOWN_LOG, "device": "LSW0"}]
    sig = grading.extract_signals(CAMPUS, logs)
    assert [d["log"] for d in sig["down_logs_after_student_config"]] == [DOWN_LOG[:200]]


# ---- 结束顺序：复位 → 复位结果落库 → 评分 ----

def test_end_resets_then_records_reset_then_schedules_scoring(aclient, monkeypatch):  # noqa: F811
    login(aclient)
    r = aclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"})
    assert r.status_code == 200, r.text
    aid = r.json()["attempt_id"]
    h = hub(aclient)
    h.student_adds(2003, "ip route-static 192.168.9.0 255.255.255.0 10.0.12.1")
    seen = {}
    n0 = len(h.calls)

    class SpyScorer:
        async def schedule(self, attempt_id, scenario=None, opts=None):
            seen["groups"] = list(h.reset_groups)
            seen["att"] = db.get_attempt(attempt_id)
            seen["calls"] = len(h.calls)
            return {"status": "scoring"}
    aclient.app.state.practice.scorer = SpyScorer()
    e = end_practice(aclient)
    assert e["active"] is False and e["reset_ok"] and e["score"] == {"status": "scoring"}
    # 评分触发时复位已经做完（撤销命令已执行），且复位结果已在库里
    assert seen["groups"] == [(2003, ["system-view", "undo ip route-static 192.168.9.0 255.255.255.0 10.0.12.1"])]
    assert seen["calls"] == len(h.calls)                  # 评分之后没有再执行任何事务
    assert seen["att"]["attempt_id" if "attempt_id" in seen["att"] else "id"] == aid
    info = json.loads(seen["att"]["reset_json"])
    assert info["ok"] is True and info["leftovers"] == {} and set(info["devices"]) == {"AR0", "AR2"}
    # 结束顺序：场景 restore 的配置事务全部在复位（抓配置）之前
    end_calls = h.calls[n0:]
    restores = [CAMPUS["restore"][d] for d in CAMPUS["restore"]]
    last_restore = max(i for i, (_, k, c) in enumerate(end_calls) if k == "config" and c in restores)
    first_capture = min(i for i, (_, k, c) in enumerate(end_calls) if c == ["display current-configuration"])
    assert last_restore < first_capture


def test_reset_failure_passed_to_ai_without_penalty(aclient, monkeypatch):  # noqa: F811
    fake = FakeAI(GOOD)
    monkeypatch.setattr(ai, "make_client", lambda: fake)
    login(aclient)
    aid = aclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()["attempt_id"]
    h = hub(aclient)
    h.fixed = True
    assert aclient.post("/api/practice/check").json()["passed"]
    h.student_adds(2003, "dhcp enable")
    h.sticky.add("dhcp enable")
    e = end_practice(aclient)
    assert e["reset_ok"] is False and e["leftovers"] == {"AR2": ["多出：dhcp enable"]}
    res = _poll(aclient, aid)
    assert res["status"] == "done"
    # 分数只由 AI 的得分率与结果决定：复位失败不额外扣分（GOOD + 通关 = 93）
    assert res["score_total"] == 93
    assert res["detail"]["reset"] == {"ok": False, "snapshot": True, "leftovers": {"AR2": ["多出：dhcp enable"]},
                                      "excluded": {}}
    user, system = fake.calls[0]["user"], fake.calls[0]["system"]
    assert "复位未完全成功（系统问题，与学员无关，不扣分）" in user and "多出：dhcp enable" in user
    assert "不能因此扣学员的分" in system
    # 重新评分仍能拿到复位结果（读库）
    r = aclient.post(f"/api/practice/rescore/{aid}")
    assert r.status_code == 200, r.text
    _poll(aclient, aid)
    assert "多出：dhcp enable" in fake.calls[-1]["user"]


def test_reset_ok_told_to_ai(aclient, monkeypatch):  # noqa: F811
    fake = FakeAI(GOOD)
    monkeypatch.setattr(ai, "make_client", lambda: fake)
    login(aclient)
    aid = aclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()["attempt_id"]
    end_practice(aclient)
    res = _poll(aclient, aid)
    assert res["detail"]["reset"]["ok"] is True
    assert "已全部复位到开练前配置" in fake.calls[0]["user"]


def test_reset_failure_still_ends_and_scores(aclient, monkeypatch):  # noqa: F811
    """复位重试几轮仍做不成（抓配置一直 busy）：练习照样结束，复位失败记进 reset_json（ok=False、failed）后再评分。"""
    login(aclient)
    aid = aclient.post("/api/practice/start", json={"scenario_id": "campus-ar0-ar2-link"}).json()["attempt_id"]
    h = hub(aclient)
    h.capture_busy = True
    aclient.app.state.practice.end_retry_wait = 0.01
    e = end_practice(aclient)
    assert e["active"] is False and e["reset_ok"] is False and e["score"]["status"] == "scoring"
    info = json.loads(db.get_attempt(aid)["reset_json"])
    assert info["ok"] is False and set(info["failed"]) == {"AR0", "AR2"} and "分页" in info["failed"]["AR0"]


def test_campus_scenarios_have_scoring_points_in_prompt():
    import prompts
    loaded, errors = sc.load_dir(config.SCENARIOS_DIR)
    assert errors == []
    for sid in ("campus-ar0-ar2-link", "campus-lsw0-access-vlan", "campus-ar2-return-route", "campus-ar2-acl-block"):
        s = loaded[sid]
        assert len(s["scoring_points"]) >= 4, sid
        user = prompts.build_score_user(s, passed=True, duration_s=60, hints_used=0, logs_text="", signals={},
                                        reset={"ok": True, "snapshot": True, "leftovers": {}, "excluded": {}})
        assert s["scoring_points"][0] in user and "已全部复位到开练前配置" in user
