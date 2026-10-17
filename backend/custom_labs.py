"""学员自建 eNSP Pro 沙箱。只允许修改在 AInet 中登记且属于当前学员的沙箱。"""
from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any

import db
import lab as lab_lib
from enspro import EnsproError, EnsproSessionLost, device_kind, device_status


class CustomLabs:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._create_lock = asyncio.Lock()

    @staticmethod
    def _owned(lab_id: int, student_id: int) -> dict[str, Any]:
        row = db.get_custom_lab(lab_id, student_id)
        if row is None:
            raise lab_lib.LabError(404, "没有这个自定义实验")
        return row

    @staticmethod
    def _vendor_error(exc: EnsproError) -> lab_lib.LabError:
        return lab_lib.LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message)

    def _ready(self, *, selected: dict[str, Any] | None = None) -> Any:
        mgr = self.manager
        reason = mgr.busy_reason("编辑自定义实验")
        if reason:
            raise lab_lib.LabError(409, reason)
        if mgr.switching or (mgr.job is not None and not mgr.job.get("done")):
            raise lab_lib.LabError(409, "环境正在切换或准备，请稍后操作")
        if selected is not None and (mgr.key != selected["env_key"] or mgr.sandbox != selected["sandbox_id"]):
            raise lab_lib.LabError(409, "请先打开这个自定义实验，再编辑拓扑")
        provider = mgr.provider
        if getattr(provider, "kind", "") != "enspro" or not hasattr(provider, "client"):
            raise lab_lib.LabError(409, "自定义拓扑编辑需要 eNSP Pro 环境")
        return provider

    @asynccontextmanager
    async def _editing(self, row: dict[str, Any] | None = None, *, env_key: str | None = None,
                       empty: bool = False):
        mgr = self.manager
        # 与环境切换一致的锁顺序；练习 / 考试不能在校验后、拓扑修改前启动。
        async with mgr._lock:
            async with mgr.exam._lock:
                async with mgr.pm._op:
                    provider = self._ready(selected=row)
                    if env_key is not None and (mgr.key != env_key or (empty and mgr.sandbox is not None)):
                        raise lab_lib.LabError(409, "环境已在其他页面切换，请重新打开实验后操作")
                    yield provider

    @staticmethod
    def _view(row: dict[str, Any], mgr: Any) -> dict[str, Any]:
        try:
            env = lab_lib.get_env(row["env_key"])
            label = env.label
        except ValueError:
            label = row["env_key"]
        return {"id": row["id"], "name": row["name"], "environment": row["env_key"],
                "environment_label": label, "sandbox": row["sandbox_id"],
                "selected": mgr.key == row["env_key"] and mgr.sandbox == row["sandbox_id"],
                "created_at": row["created_at"]}

    def list(self, student_id: int) -> dict[str, Any]:
        return {"labs": [self._view(r, self.manager) for r in db.list_custom_labs(student_id)]}

    async def create(self, student_id: int, env_key: str, title: str) -> dict[str, Any]:
        title = title.strip()
        if not (1 <= len(title) <= 60):
            raise lab_lib.LabError(422, "实验名称须为 1～60 个字符")
        try:
            env = lab_lib.get_env(env_key)
        except ValueError as exc:
            raise lab_lib.LabError(422, str(exc)) from None
        if (env.type or env.key) != "enspro":
            raise lab_lib.LabError(422, "只有 eNSP Pro 环境支持新建自定义拓扑")
        async with self._create_lock:
            if self.manager.key != env_key:
                await self.manager.switch(env_key, None, sandbox_given=True)
            async with self._editing(env_key=env_key) as provider:
                client = provider.client
                machine_name = f"ainet_custom_{student_id}_{secrets.token_hex(5)}"
                project_id = int((client.user or {}).get("projectId") or 1)
                user_id = int((client.user or {}).get("userId") or (client.user or {}).get("id") or 1)
                try:
                    await client.call("POST", "/service/ensp/sandbox/create", {
                        "name": machine_name, "isPublic": 0, "maxVms": 50, "keepTime": 0,
                        "projectId": project_id, "type": 0, "userId": user_id})
                    rows = await client.sandboxes(project_id)
                except EnsproError as exc:
                    raise self._vendor_error(exc) from None
                match = next((r for r in rows if r.get("name") == machine_name), None)
                if match is None:
                    raise lab_lib.LabError(502, "模拟器已收到创建请求，但未返回新沙箱；请刷新后重试")
                row = await asyncio.to_thread(db.create_custom_lab, student_id, env_key, int(match["id"]), title)
            await self.manager.switch(env_key, row["sandbox_id"], sandbox_given=True)
            return self._view(row, self.manager)

    async def activate(self, lab_id: int, student_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)
        await self.manager.switch(row["env_key"], row["sandbox_id"], sandbox_given=True)
        return self._view(row, self.manager)

    @staticmethod
    def _topology(sb: dict[str, Any]) -> dict[str, Any]:
        devices = []
        for d in sb.get("devices") or []:
            typ = d.get("deviceType") or {}
            pos = d.get("devicePosition") or {}
            devices.append({"id": int(d["id"]), "name": d.get("name") or "",
                            "model": typ.get("displayName") or typ.get("name") or "设备",
                            "kind": device_kind(d), "powerable": device_kind(d) != "cloud",
                            "status": device_status(d.get("status")),
                            "x": float(pos.get("positionX") or 0), "y": float(pos.get("positionY") or 0),
                            "interfaces": [{"id": int(i["id"]), "name": i.get("name") or "接口"}
                                           for i in d.get("deviceIfs") or []]})
        links = [{"id": int(l["id"]), "left_if": int(l["leftIfId"]), "right_if": int(l["rightIfId"])}
                 for l in sb.get("links") or []]
        return {"devices": devices, "links": links, "limit": int(sb.get("maxVms") or 50)}

    async def detail(self, lab_id: int, student_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)
        provider = self._ready(selected=row)
        try:
            sb = await provider.sandbox_data(force=True)
        except EnsproError as exc:
            raise self._vendor_error(exc) from None
        return {**self._view(row, self.manager), **self._topology(sb),
                "max_running": lab_lib.max_devices(row["env_key"])}

    async def models(self, lab_id: int, student_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)
        provider = self._ready(selected=row)
        try:
            data = await provider.client.call("GET", "/service/ensp/device-type/all",
                                              params={"sandboxId": row["sandbox_id"]})
        except EnsproError as exc:
            raise self._vendor_error(exc) from None
        models = [{"id": int(t["id"]), "name": t.get("name") or "",
                   "label": t.get("displayName") or t.get("name") or "设备",
                   "category": (t.get("superDeviceType") or {}).get("displayName") or
                               (t.get("superDeviceType") or {}).get("name") or "其他"}
                  for t in (data or {}).get("devices") or [] if t.get("id") is not None]
        return {"models": models}

    async def _mutate(self, row: dict[str, Any], callback) -> dict[str, Any]:
        async with self._editing(row) as provider:
            try:
                sb = await provider.sandbox_data(force=True)
                await callback(provider.client, sb)
                provider.invalidate()
                self.manager.touch("custom-lab")
                sb = await provider.sandbox_data(force=True)
            except EnsproError as exc:
                raise self._vendor_error(exc) from None
            return {**self._view(row, self.manager), **self._topology(sb),
                    "max_running": lab_lib.max_devices(row["env_key"])}

    async def add_device(self, lab_id: int, student_id: int, name: str, model_id: int,
                         x: float, y: float) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name):
            raise lab_lib.LabError(422, "设备名须以英文字母开头，只能包含字母、数字、下划线和连字符（最多 32 位）")

        async def act(client, sb):
            if any(d.get("name", "").lower() == name.lower() for d in sb.get("devices") or []):
                raise lab_lib.LabError(409, f"设备名 {name} 已存在")
            if len(sb.get("devices") or []) >= int(sb.get("maxVms") or 50):
                raise lab_lib.LabError(409, "已达到沙箱设备数量上限")
            catalog = await client.call("GET", "/service/ensp/device-type/all",
                                        params={"sandboxId": row["sandbox_id"]})
            if not any(int(t["id"]) == model_id for t in (catalog or {}).get("devices") or []):
                raise lab_lib.LabError(422, "设备型号在当前环境中不可用")
            pos = {"positionX": x, "positionY": y}
            await client.call("POST", "/service/ensp/device/batchCreate", {
                "sandboxId": row["sandbox_id"], "devices": [{"name": name, "deviceTypeId": model_id,
                "devicePosition": pos, "namePosition": pos}]})

        return await self._mutate(row, act)

    async def move_device(self, lab_id: int, student_id: int, device_id: int,
                          x: float, y: float) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)

        async def act(client, sb):
            dev = next((d for d in sb.get("devices") or [] if int(d["id"]) == device_id), None)
            if dev is None:
                raise lab_lib.LabError(404, "设备不存在")
            moved = dict(dev)
            moved["devicePosition"] = {**(dev.get("devicePosition") or {}), "positionX": x, "positionY": y}
            moved["namePosition"] = {**(dev.get("namePosition") or {}), "positionX": x, "positionY": y}
            await client.call("POST", "/service/ensp/device/modifyComponent",
                              {"sandboxId": row["sandbox_id"], "devices": [moved]})

        return await self._mutate(row, act)

    async def remove_device(self, lab_id: int, student_id: int, device_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)

        async def act(client, sb):
            dev = next((d for d in sb.get("devices") or [] if int(d["id"]) == device_id), None)
            if dev is None:
                raise lab_lib.LabError(404, "设备不存在")
            if device_status(dev.get("status")) != "stopped":
                raise lab_lib.LabError(409, "请先停止设备，再删除")
            await client.call("POST", "/service/ensp/device/batchDelete",
                              {"sandboxId": row["sandbox_id"], "deviceIds": [device_id]})

        return await self._mutate(row, act)

    async def add_link(self, lab_id: int, student_id: int, left_if: int, right_if: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)

        async def act(client, sb):
            ports = {int(i["id"]): (d, i) for d in sb.get("devices") or [] for i in d.get("deviceIfs") or []}
            if left_if not in ports or right_if not in ports:
                raise lab_lib.LabError(404, "接口不存在")
            left, right = ports[left_if][0], ports[right_if][0]
            if left["id"] == right["id"]:
                raise lab_lib.LabError(422, "连线两端须是不同设备")
            used = {int(l[k]) for l in sb.get("links") or [] for k in ("leftIfId", "rightIfId")}
            if left_if in used or right_if in used:
                raise lab_lib.LabError(409, "接口已被其他连线占用")
            if any(device_status(d.get("status")) != "stopped" for d in (left, right)):
                raise lab_lib.LabError(409, "请先停止连线两端的设备")
            await client.create_link(row["sandbox_id"], int(left["id"]), left_if, int(right["id"]), right_if)

        return await self._mutate(row, act)

    async def remove_link(self, lab_id: int, student_id: int, link_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)

        async def act(client, sb):
            link = next((l for l in sb.get("links") or [] if int(l["id"]) == link_id), None)
            if link is None:
                raise lab_lib.LabError(404, "连线不存在")
            ids = {int(link["leftIfId"]), int(link["rightIfId"])}
            attached = [d for d in sb.get("devices") or []
                        if any(int(i["id"]) in ids for i in d.get("deviceIfs") or [])]
            if any(device_status(d.get("status")) != "stopped" for d in attached):
                raise lab_lib.LabError(409, "请先停止连线两端的设备")
            await client.call("POST", "/service/ensp/link/deleteLink", {"linkId": link_id})

        return await self._mutate(row, act)

    async def delete(self, lab_id: int, student_id: int) -> dict[str, Any]:
        row = self._owned(lab_id, student_id)
        async with self._create_lock:
            async with self._editing(row) as provider:
                try:
                    sb = await provider.sandbox_data(force=True)
                except EnsproError as exc:
                    raise self._vendor_error(exc) from None
                if any(device_status(d.get("status")) != "stopped" for d in sb.get("devices") or []):
                    raise lab_lib.LabError(409, "请先停止沙箱中的全部设备，再删除实验")
            await self.manager.switch(row["env_key"], None, sandbox_given=True)
            async with self._editing(env_key=row["env_key"], empty=True) as provider:
                try:
                    await provider.client.call("POST", "/service/ensp/sandbox/batchDeleteSandbox",
                                               {"sandboxIds": [row["sandbox_id"]]})
                except EnsproError as exc:
                    raise self._vendor_error(exc) from None
                await self.manager.forget_sandbox(row["env_key"], row["sandbox_id"])
                await asyncio.to_thread(db.delete_custom_lab, lab_id, student_id)
            return {"deleted": lab_id}
