"""A retained runtime for one environment and sandbox.

Device identifiers are local to their simulator. Each context has its own watchdog,
idle timer and background console hub, while sandboxes in one environment share
the REST session and stop timestamps.
"""
from __future__ import annotations

from typing import Any

import config
from console_hub import ConsoleHub
from labpower import IdleStop, PowerWatch


class LabContext:
    def __init__(self, root: Any, key: str, sandbox: int | None, provider: Any,
                 *, power: PowerWatch | None = None, idle: IdleStop | None = None) -> None:
        self.root, self.key, self.sandbox, self.provider = root, key, sandbox, provider
        self.power_setting = f"lab:power:{key}:{sandbox}"
        self.power = power or PowerWatch(self)
        self.idle = idle or IdleStop(self)
        self.power.mgr = self.idle.mgr = self
        self.power.stopped_at = root._stop_times.setdefault(key, self.power.stopped_at)
        self._background_hub: ConsoleHub | None = None
        self._started = False
        if getattr(provider, "power", False):
            provider.watch = self.power

    @property
    def active(self) -> bool:
        return self.root._runtime is self

    @property
    def hub(self) -> Any:
        if self.active:
            return self.root.hub
        if self._background_hub is None:
            self._background_hub = ConsoleHub(self.provider.connector, idle_close=config.HUB_IDLE_CLOSE,
                min_reconnect=config.HUB_MIN_RECONNECT, default_sysname="HUAWEI")
        return self._background_hub

    @property
    def pm(self) -> Any:
        return self.root.pm

    @property
    def exam(self) -> Any:
        return self.root.exam

    @property
    def _lock(self) -> Any:
        return self.root._lock

    @property
    def switching(self) -> bool:
        return self.root.switching

    @property
    def job(self) -> Any:
        return self.root.job if self.active else None

    def busy_reason(self) -> str:
        return self.root.busy_reason() if self.active else ""

    def start(self) -> None:
        if not self._started:
            self._started = True
            self.power.resume()
            self.idle.start()

    async def reset_background_hub(self) -> None:
        if self._background_hub is not None:
            await self._background_hub.close()
            self._background_hub = None

    async def close(self) -> None:
        await self.power.close()
        await self.idle.close()
        await self.reset_background_hub()
