"""Regression checks for independent simulator runtimes; HTTP is always mocked."""
import asyncio
import json
import time
from types import SimpleNamespace

import config
import db
import lab
from labenv import LabManager
from enspro import EnsproClient, EnsproProvider
from fake_enspro import FakeRest, sandbox_json


class Hub:
    def add_listener(self, listener):
        self.listener = listener

    async def reset_all(self, *args, **kwargs):
        return 0


def manager(monkeypatch, rests):
    monkeypatch.setattr(config, "LAB_PROVIDER", "enspro")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", 2)
    monkeypatch.setattr(config, "ENSPRO_STOP_OLD", False)
    db.set_setting("lab:envs", json.dumps([{"key": "enspro-2", "type": "enspro", "label": "Second fake lab"}]))

    async def available(*args, **kwargs):
        return {"available": True, "reason": ""}
    monkeypatch.setattr(lab, "available", available)

    def make(key, sandbox=None, client=None):
        client = client or EnsproClient(f"http://{key}.test", transport=rests[key].transport())
        return EnsproProvider(client=client, sandbox=sandbox, env_key=key, cache_ttl=0, password=lambda: "fake")
    m = LabManager(make=make)
    m.boot()
    m.bind(Hub(), SimpleNamespace(active=None, _op=asyncio.Lock(), lab=m.provider),
           SimpleNamespace(exam=None, _lock=asyncio.Lock()))
    return m


async def until(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("fake runtime did not settle")


def test_identical_sandbox_and_device_ids_do_not_share_stop_timers(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}
    rests["enspro-2"].status = {6: 0, 7: 0}
    rests["enspro-2"].instant_start = True

    async def run():
        m = manager(monkeypatch, rests)
        first = m._runtime
        first.power.restart_gap = 60
        first.power.note_stopped(2, [6])
        await m.switch("enspro-2", 2, sandbox_given=True)
        second = m._runtime
        assert first.power.stopped_at is not second.power.stopped_at
        second.power.restart_gap = 60
        result = await second.power.start([6])
        assert result["started"] == [6] and result["queued"] == []
        assert json.loads(db.get_setting(first.power_setting))["env"] == "enspro"
        assert json.loads(db.get_setting(second.power_setting))["env"] == "enspro-2"
        await m.close()
    asyncio.run(run())


def test_watchdog_keeps_monitoring_environment_after_selection_changes(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}
    rest = rests["enspro"]
    rest.status, rest.stuck, rest.instant_start = {6: 0, 7: 0}, {6: 1}, True

    async def run():
        m = manager(monkeypatch, rests)
        old = m._runtime
        old.power.poll, old.power.start_timeout, old.power.retries = 0.02, 0.15, 1
        await old.power.start([6])
        await m.switch("enspro-2", 2, sandbox_given=True)
        assert not old.active and old.provider is not m.provider
        await until(lambda: not old.power.tracks)
        assert any(p.endswith("batchStop") for _, p, _ in rest.calls)
        assert not any(p.endswith("batchStop") for _, p, _ in rests["enspro-2"].calls)
        assert rest.logouts == 0
        await m.close()
        assert rest.logouts == 1
    asyncio.run(run())


def test_inactive_environment_idle_stop_targets_its_own_devices(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}

    async def run():
        m = manager(monkeypatch, rests)
        old = m._runtime
        await m.switch("enspro-2", 2, sandbox_given=True)
        old.idle.clock, old.idle.last = lambda: 1000.0, 0.0
        old.idle.minutes, old.idle.warn = 1, 0
        result = await old.idle.check()
        assert result is not None and result["sandbox"] == 2
        assert rests["enspro"].status == {6: 0, 7: 0}
        assert rests["enspro-2"].status == {6: 4, 7: 4}
        assert m.hub is not old.hub  # Device IDs must not touch the selected environment's console channels.
        await m.close()
    asyncio.run(run())


def test_hub_activity_updates_only_selected_environment_idle_timer(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}

    async def run():
        m = manager(monkeypatch, rests)
        old = m._runtime
        old.idle.clock = lambda: 10
        old.idle.touch("old")
        await m.switch("enspro-2", 2, sandbox_given=True)
        m.idle.clock = lambda: 20
        m.hub.listener("input", 6, "d")
        assert old.idle.last == 10 and m.idle.last == 20
        await m.close()
    asyncio.run(run())


def test_runtime_recovery_restores_unselected_watchdog(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}
    rest = rests["enspro"]
    rest.status, rest.stuck, rest.instant_start = {6: 0, 7: 0}, {6: 1}, True

    async def run():
        m = manager(monkeypatch, rests)
        old = m._runtime
        await old.power.start([6])
        await m.switch("enspro-2", 2, sandbox_given=True)
        await m.close()
        saved = json.loads(db.get_setting(old.power_setting))
        saved["tracks"][0]["since"] -= 1000
        db.set_setting(old.power_setting, json.dumps(saved))
        m = manager(monkeypatch, rests)
        m.start_background()
        restored = m._contexts[("enspro", 2)]
        restored.power.poll = 0.02
        assert m.key == "enspro-2" and 6 in restored.power.tracks
        await until(lambda: not restored.power.tracks)
        assert any(p.endswith("batchStop") for _, p, _ in rest.calls)
        await m.close()
    asyncio.run(run())


def test_retiring_endpoint_cancels_all_its_background_tasks(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}

    async def run():
        m = manager(monkeypatch, rests)
        old = m._runtime
        await old.provider.resolve()
        await m.switch("enspro-2", 2, sandbox_given=True)
        await m._retire_contexts("enspro")
        assert old.power._closed and ("enspro", 2) not in m._contexts
        assert not db.get_settings_prefix("lab:power:enspro:")
        assert rests["enspro"].logouts == 1
        await m.close()
    asyncio.run(run())


def test_changing_inactive_endpoint_releases_old_runtime(db_path, monkeypatch):
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}

    async def run():
        m = manager(monkeypatch, rests)
        await m.switch("enspro-2", 2, sandbox_given=True)
        old = m._runtime
        await old.provider.resolve()
        await m.switch("enspro", 2, sandbox_given=True)
        result = await m.set_endpoint("enspro-2", "https://127.0.0.1:9")
        assert result["changed"] and old.power._closed
        assert ("enspro-2", 2) not in m._contexts and rests["enspro-2"].logouts == 1
        await m.close()
    asyncio.run(run())


def test_new_device_password_clears_blocks_in_all_sandboxes(db_path, monkeypatch, tmp_path):
    import ai
    monkeypatch.setattr(ai, "key_file", lambda: tmp_path / "secrets.env")
    db.init_db()
    rests = {key: FakeRest() for key in ("enspro", "enspro-2")}
    rests["enspro-2"].extra[3] = sandbox_json(3, {6: 4, 7: 4})

    async def run():
        m = manager(monkeypatch, rests)
        await m.switch("enspro-2", 2, sandbox_given=True)
        a = m._runtime
        await m.switch("enspro-2", 3, sandbox_given=True)
        b = m._runtime
        await m.switch("enspro", 2, sandbox_given=True)
        for ctx in (a, b):
            ctx.provider.auth_blocked[6] = "old credential rejected"
            assert ctx.hub is not m.hub
        await m.edit_env("enspro-2", password="Fake-New#123")
        assert not a.provider.auth_blocked and not b.provider.auth_blocked
        assert a._background_hub is None and b._background_hub is None
        await m.close()
    asyncio.run(run())
