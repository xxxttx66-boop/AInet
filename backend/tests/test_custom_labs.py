"""自建拓扑使用内存模拟器与临时库，验证归属、端口边界、运行保护及删除清理。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import db
from custom_labs import CustomLabs
from lab import LabError


class Simulator:
    def __init__(self):
        self.user = {"id": 1, "projectId": 1}
        self.boxes = {1: {"id": 1, "name": "course", "maxVms": 50, "devices": [], "links": []}}
        self.next_id = 10
        self.calls = []

    async def sandboxes(self, project_id=None):
        return deepcopy(list(self.boxes.values()))

    async def call(self, method, path, body=None, *, params=None):
        self.calls.append((path, deepcopy(body)))
        if path.endswith("/sandbox/create"):
            self.boxes[2] = {"id": 2, "name": body["name"], "maxVms": body["maxVms"], "devices": [], "links": []}
        elif path.endswith("/device-type/all"):
            return {"devices": [{"id": 1, "name": "ENSP-AR", "superDeviceType": {"name": "router"}}]}
        elif path.endswith("/device/batchCreate"):
            box = self.boxes[body["sandboxId"]]
            for row in body["devices"]:
                self.next_id += 1
                box["devices"].append({**deepcopy(row), "id": self.next_id, "status": 0,
                    "deviceType": {"name": "ENSP-AR"},
                    "deviceIfs": [{"id": self.next_id * 100, "name": "GE1/0/0"}]})
        elif path.endswith("/device/modifyComponent"):
            box = self.boxes[body["sandboxId"]]
            for updated in body["devices"]:
                box["devices"] = [deepcopy(updated) if d["id"] == updated["id"] else d for d in box["devices"]]
        elif path.endswith("/device/batchDelete"):
            box = self.boxes[body["sandboxId"]]
            ports = {p["id"] for d in box["devices"] if d["id"] in body["deviceIds"] for p in d["deviceIfs"]}
            box["devices"] = [d for d in box["devices"] if d["id"] not in body["deviceIds"]]
            box["links"] = [l for l in box["links"] if not ports.intersection({l["leftIfId"], l["rightIfId"]})]
        elif path.endswith("/link/deleteLink"):
            for box in self.boxes.values():
                box["links"] = [l for l in box["links"] if l["id"] != body["linkId"]]
        elif path.endswith("/sandbox/batchDeleteSandbox"):
            for sid in body["sandboxIds"]:
                del self.boxes[sid]
        else:
            raise AssertionError(path)

    async def create_link(self, sandbox, left_dev, left_if, right_dev, right_if):
        self.boxes[sandbox]["links"].append({"id": 501, "leftIfId": left_if, "rightIfId": right_if})


class Provider:
    kind = "enspro"

    def __init__(self, client, sandbox):
        self.client, self.sandbox_id = client, sandbox

    async def sandbox_data(self, force=False):
        return deepcopy(self.client.boxes[self.sandbox_id])

    def invalidate(self):
        pass


class Manager:
    def __init__(self):
        self.key, self.sandbox = "enspro", 1
        self.sim = Simulator()
        self.provider = Provider(self.sim, 1)
        self._lock = asyncio.Lock()
        self.exam = SimpleNamespace(_lock=asyncio.Lock())
        self.pm = SimpleNamespace(_op=asyncio.Lock())
        self.job, self.switching, self.reason = None, False, ""
        self.forgotten = []

    def busy_reason(self, what=""):
        return self.reason

    def touch(self, why):
        pass

    async def switch(self, key, sandbox, *, sandbox_given=False):
        async with self._lock:
            if self.reason:
                raise LabError(409, self.reason)
            self.key, self.sandbox = key, sandbox
            self.provider = Provider(self.sim, sandbox)

    async def forget_sandbox(self, key, sandbox):
        self.forgotten.append((key, sandbox))


def test_topology_lifecycle_preserves_course_sandbox(db_path):
    db.init_db()

    async def go():
        manager = Manager()
        service = CustomLabs(manager)
        lab = await service.create(1, "enspro", "我的双路由实验")
        lid = lab["id"]
        first = await service.add_device(lid, 1, "AR1", 1, -.2, 0)
        first_id = first["devices"][0]["id"]
        second = await service.add_device(lid, 1, "AR2", 1, .2, 0)
        a, b = [d["interfaces"][0]["id"] for d in second["devices"]]
        with pytest.raises(LabError, match="接口不存在"):
            await service.add_link(lid, 1, a, 999999)
        linked = await service.add_link(lid, 1, a, b)
        assert len(linked["links"]) == 1
        with pytest.raises(LabError, match="占用"):
            await service.add_link(lid, 1, a, b)
        moved = await service.move_device(lid, 1, first_id, -.3, .2)
        assert (moved["devices"][0]["x"], moved["devices"][0]["y"]) == (-.3, .2)
        manager.sim.boxes[2]["devices"][0]["status"] = 4
        with pytest.raises(LabError, match="先停止"):
            await service.remove_device(lid, 1, first_id)
        with pytest.raises(LabError, match="先停止"):
            await service.remove_link(lid, 1, 501)
        with pytest.raises(LabError, match="先停止"):
            await service.delete(lid, 1)
        manager.sim.boxes[2]["devices"][0]["status"] = 0
        removed = await service.remove_device(lid, 1, first_id)
        assert len(removed["devices"]) == 1 and not removed["links"]
        await service.delete(lid, 1)
        assert db.get_custom_lab(lid, 1) is None
        assert manager.forgotten == [("enspro", 2)]
        assert manager.sim.boxes == {1: {"id": 1, "name": "course", "maxVms": 50, "devices": [], "links": []}}

    asyncio.run(go())


def test_edits_require_owner_selected_sandbox_and_idle_session(db_path):
    db.init_db()

    async def go():
        manager = Manager()
        service = CustomLabs(manager)
        lab = await service.create(1, "enspro", "隔离实验")
        before = len(manager.sim.calls)
        with pytest.raises(LabError, match="没有这个"):
            await service.add_device(lab["id"], 999, "AR1", 1, 0, 0)
        assert len(manager.sim.calls) == before
        await manager.switch("enspro", 1)
        with pytest.raises(LabError, match="先打开"):
            await service.add_device(lab["id"], 1, "AR1", 1, 0, 0)
        await service.activate(lab["id"], 1)
        manager.reason = "考试进行中"
        with pytest.raises(LabError, match="考试进行中"):
            await service.add_device(lab["id"], 1, "AR1", 1, 0, 0)
        assert not manager.sim.boxes[2]["devices"]

    asyncio.run(go())


def test_create_rechecks_environment_after_switch(db_path):
    db.init_db()

    async def go():
        manager = Manager()
        manager.key = "ensp"

        async def switched_elsewhere(*args, **kwargs):
            manager.key = "ensp"  # 另一个页面在创建请求获取锁之前切走。

        manager.switch = switched_elsewhere
        with pytest.raises(LabError, match="其他页面切换"):
            await CustomLabs(manager).create(1, "enspro", "切换竞态")
        assert not manager.sim.calls

    asyncio.run(go())


def test_forget_sandbox_keeps_other_runtime_and_shared_client(db_path):
    from labcontext import LabContext
    from labenv import LabManager

    db.init_db()

    async def go():
        manager = LabManager()
        shared = object()
        first = LabContext(manager, "enspro", 1, SimpleNamespace(power=False, client=shared))
        removed = LabContext(manager, "enspro", 2, SimpleNamespace(power=False, client=shared))
        manager.key, manager.sandbox, manager._runtime = "enspro", 1, first
        manager._contexts = {("enspro", 1): first, ("enspro", 2): removed}
        db.set_setting("lab:power:enspro:1", "keep")
        db.set_setting("lab:power:enspro:2", "remove")
        await manager.forget_sandbox("enspro", 2)
        assert manager._contexts == {("enspro", 1): first}
        assert removed.power._closed and not first.power._closed
        assert first.provider.client is shared
        assert db.get_setting("lab:power:enspro:1") == "keep"
        assert db.get_setting("lab:power:enspro:2") is None
        await first.close()

    asyncio.run(go())
