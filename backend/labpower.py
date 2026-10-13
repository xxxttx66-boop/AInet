"""设备启动可靠性（spec §10.5「启动可靠性」，eNSP Pro）：启动看门狗、错开启动、就绪探测、空闲自动停机。

所有由 ainet 发起的设备启动（拓扑图「启动」按钮 / POST /api/lab/devices/start、按题目准备环境、重启的「停止 → 启动」）
都经过 PowerWatch：
- **停机间隔**：同一台设备距 ainet 上次发出停止不到 restart_gap 秒（AINET_ENSPRO_RESTART_GAP，缺省 10）不发启动，
  排队等到够了、确认接口已是「已停止」再发。只是安全余量，不是根治：卡住（spec §14 第 21 项：新容器早期服务 sfed 以状态
  17 退出 → 请求重启系统，容器里被忽略 → 板卡每 15 秒复位一次的死循环，接口一直停在状态 6）与停机间隔无关，
  真正的恢复手段是看门狗的「停止 → 启动」（停掉卡住的实例后再启动，实测都正常）。
  重启（拓扑图「重启」、看门狗的自动重启）一律是「停止 → 等间隔、确认已停止 → 启动」，不用 batchReboot。
- **错开启动**：同一时刻只发一台的启动请求，相邻两次至少隔 stagger 秒（AINET_ENSPRO_START_STAGGER，缺省 5；
  0 = 一次批量启动）。排队中的设备显示「启动中（排队）」。
- **看门狗**：后台每 poll 秒查一次接口状态。启动超过 start_timeout 秒（AINET_ENSPRO_START_TIMEOUT，缺省 150）仍在启动阶段
  （状态 2 / 6），或接口报错误状态（3 / 5 / 17），自动「停止 → 启动」retries 次（AINET_ENSPRO_START_RETRIES，缺省 3）；
  仍不行就标为 **start_failed**（启动失败），原因写在拓扑的 start_errors 与进度文字里。最后一轮不停设备：之后接口报运行中
  并通过就绪探测的（超时误判的慢设备）恢复为 online。
- **按环境配置**：start_timeout / retries / early_check / probe_timeout 按模拟环境单独配置（lab.WatchParam，库里
  lab:watch:<环境> 优先，其次环境变量、内置缺省），LabManager 用 configure() 换成当前环境的值，改了立即生效。
- **就绪探测**：接口报运行中（状态 1 / 4）之后，设备状态先是 **initializing**（系统初始化中），经 ConsoleHub 的隐藏事务
  执行只读的探测命令（PROBE_COMMANDS），设备不再报 busy 才算就绪（状态回到 online）。隐藏事务的输出不进学员终端、
  不产生命令记录。probe_timeout 秒内一直没通过，按卡住处理（停止再启动 / 启动失败）。
- **重启恢复**：跟踪状态存 app_settings（lab:power），服务重启后接着跟踪；中途停在「停止 → 启动」之间的设备补发启动。

IdleStop：当前沙箱的设备超过 N 分钟（AINET_ENSPRO_IDLE_STOP_MIN，缺省 30，0 = 关闭）没有任何活动（终端输入 / 命令 /
命令输出、练习、考试、Agent 命令、启停、准备环境），自动停掉当前沙箱里运行着的设备；练习 / 考试进行中不停；
停之前提前 warn 秒给打开着的终端发通知（终端里按任意键 = 有活动，取消这次停机）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import config
import db
from lab import LabError

log = logging.getLogger("ainet.labpower")

SETTING_POWER = "lab:power"
# 就绪探测命令（只读）：刚冷启动的 YunShan 设备在「构建配置」期间读配置也报
# `Error: The system is busy in building configuration. Please wait for a moment...`，与改配置报 busy 的时间窗一致
# （2026-10-07 T2 实测：接口报运行中后 0~27 秒两者都 busy，34 秒起都正常）；`system-view` / `quit` 全程不报 busy，不能用。
PROBE_COMMANDS = ["display current-configuration | include sysname"]
BUSY_RE = re.compile(r"\bbusy\b|building configuration|not ready|initializ|please wait|try again later", re.I)
START_PHASE = {2: "分配虚机", 6: "启动系统"}
ERROR_CODES = {3: "分配虚机失败", 5: "创建容器失败", 17: "设备丢失"}


@dataclass
class Track:
    sandbox: int
    dev: int
    name: str
    phase: str                    # queued / starting / initializing / ready / failed
    since: float                  # 本轮启动请求发出的时刻（clock）；排队中为入队时刻
    started: float                # 第一次进入跟踪的时刻
    restarts: int = 0
    reason: str = ""              # 卡住 / 失败的原因
    init_at: float | None = None  # 接口报运行中的时刻
    last_probe: float = 0.0
    probe_note: str = ""
    restarting: bool = False      # 卡住后正在「停止 → 等待 → 启动」
    not_before: float = 0.0       # 排队中：最早什么时候发启动（距上次停机至少 restart_gap 秒）
    after_stop: bool = False      # 排队中：刚发过停止，发启动前确认接口已是「已停止」
    early_checks: int = 0         # 已做过几次「设备日志判断板卡复位循环」的早期检查
    source: str = ""
    recovered: bool = False       # 标过 start_failed 后接口报运行中（超时误判的慢设备），正在 / 已经恢复


class PowerWatch:
    def __init__(self, mgr: Any, *, clock: Callable[[], float] = time.time,
                 prober: Callable[[int], Awaitable[tuple[bool | None, str]]] | None = None) -> None:
        self.mgr = mgr
        self.clock = clock
        self.prober = prober or self._probe_console
        self.start_timeout = config.ENSPRO_START_TIMEOUT
        self.retries = config.ENSPRO_START_RETRIES
        self.stagger = config.ENSPRO_START_STAGGER
        self.restart_gap = config.ENSPRO_RESTART_GAP
        self.early_check = config.ENSPRO_EARLY_CHECK    # 启动这么多秒（及 2 倍时）还在「启动系统」：查设备日志看是否已卡死
        self.poll = config.ENSPRO_WATCH_POLL
        self.probe_enabled = config.ENSPRO_PROBE
        self.probe_interval = config.ENSPRO_PROBE_INTERVAL
        self.probe_timeout = config.ENSPRO_PROBE_TIMEOUT
        self.stop_grace = 30.0            # 发出启动后这么久接口仍是「已停止」= 被别处停掉了，不再跟踪
        self.stop_wait = 90.0             # 重启时等接口报「已停止」的上限
        self.tracks: dict[int, Track] = {}
        self.stopped_at: dict[tuple[Any, int], float] = {}   # (沙箱, 设备) -> ainet 最近一次发停止的时刻
        self._queue: list[int] = []
        self._last_start = 0.0
        self._task: asyncio.Task | None = None
        self._queue_task: asyncio.Task | None = None
        self._jobs: dict[int, asyncio.Task] = {}      # 设备 -> 进行中的探测 / 重启任务
        self._closed = False
        self.events: list[dict[str, Any]] = []        # 最近的看门狗动作（诊断 / 测试）
        self.configured_for: str | None = None        # 按哪个环境的参数配置的（lab.watch_values，见 configure）

    # 按环境单独配置的参数（lab.WatchParam.name -> 属性名，spec §5.2「接入设置」）
    PARAM_ATTRS = {"start_timeout": "start_timeout", "start_retries": "retries", "early_check": "early_check",
                   "probe_timeout": "probe_timeout"}

    def configure(self, values: dict[str, Any], env: str | None = None) -> dict[str, Any]:
        """换成某个环境的看门狗参数（立即生效：tick 每轮现读属性，进行中的启动按新值继续计时）。返回改动 {参数: (旧, 新)}。"""
        changed = {}
        for name, v in values.items():
            attr = self.PARAM_ATTRS.get(name)
            if attr is None:
                continue
            old = getattr(self, attr)
            if old != v:
                changed[name] = (old, v)
                setattr(self, attr, v)
        if env is not None:
            self.configured_for = env
        if changed:
            log.info("启动看门狗参数%s：%s", f"（{env}）" if env else "",
                     "，".join(f"{k} {a:g} -> {b:g}" for k, (a, b) in changed.items()))
        return changed

    # ---- 状态叠加（provider.resolve 调用） ----

    def overlay(self, sandbox: Any, dev: int, st: str) -> tuple[str, str | None]:
        """接口状态 st -> (显示状态, 进度文字或 None)。"""
        t = self.tracks.get(dev)
        if t is None or t.sandbox != sandbox:
            return st, None
        now = self.clock()
        if t.phase == "queued":
            if t.restarting:
                return "starting", f"启动卡住，正在停止后重新启动（{t.reason}）"
            left = t.not_before - now
            if left > 0:
                return "starting", f"等上次停机清理完再启动（{int(left) + 1}s）"
            return "starting", f"排队启动（每 {self.stagger:g} 秒启动一台）"
        if t.phase == "failed":
            if st in ("stopped", "stopping"):
                return st, None
            return "start_failed", f"启动失败：{t.reason}"
        if t.restarting:
            return "starting", f"启动卡住，正在停止后重新启动（{t.reason}）"
        if t.phase == "starting":
            if st == "online" and self.probe_enabled:
                return "initializing", "系统初始化中"
            if st in ("stopped", "unknown"):
                return "starting", None
            return st, (f"已自动重启 {t.restarts} 次 · " if t.restarts else None)
        if t.phase == "initializing":
            if st == "online":
                secs = int(now - (t.init_at or now))
                return "initializing", f"系统初始化中 {secs}s" + (f"（{t.probe_note}）" if t.probe_note else "")
            return st, None
        return st, None

    def start_errors(self, sandbox: Any) -> dict[str, str]:
        return {str(t.dev): t.reason for t in self.tracks.values() if t.sandbox == sandbox and t.phase == "failed"}

    def busy(self) -> bool:
        """有设备在排队 / 启动 / 初始化 / 重启中（空闲停机不在这时停）。"""
        return any(t.phase in ("queued", "starting", "initializing") or t.restarting for t in self.tracks.values())

    def view(self) -> dict[str, Any]:
        now = self.clock()
        return {"stagger": self.stagger, "start_timeout": self.start_timeout, "retries": self.retries,
                "restart_gap": self.restart_gap, "probe": self.probe_enabled, "probe_timeout": self.probe_timeout,
                "early_check": self.early_check, "configured_for": self.configured_for,
                "devices": {t.name: {"phase": t.phase, "restarts": t.restarts, "reason": t.reason,
                                     "elapsed_s": round(now - t.started, 1), "restarting": t.restarting}
                            for t in self.tracks.values()},
                "events": self.events[-10:]}

    # ---- 启停入口 ----

    def _provider(self) -> Any:
        p = getattr(self.mgr, "provider", None)
        if p is None or not getattr(p, "power", False):
            raise LabError(400, "当前实验后端不支持由 ainet 启停设备，请在模拟器界面上操作")
        return p

    def note_stopped(self, sandbox: Any, keys) -> None:
        """记下 ainet 发出停止的时刻：同一台设备距上次停机不到 restart_gap 秒不发启动（spec §10.5 根因）。"""
        now = self.clock()
        for k in keys:
            self.stopped_at[(sandbox, int(k))] = now

    def _not_before(self, sandbox: Any, k: int) -> float:
        return self.stopped_at.get((sandbox, k), -1e18) + self.restart_gap

    async def start(self, keys: list[int], *, source: str = "api") -> dict[str, Any]:
        """启动设备：排队、错开发出（stagger）、距上次停机不足 restart_gap 秒的推迟到够了再发。
        已启动失败 / 接口报错误状态的设备先停止再启动（看门狗计数清零）。能立即发的第一台同步发出（会话失效等错误
        直接报给调用方），其余由后台队列发出。"""
        p = self._provider()
        keys = list(dict.fromkeys(int(k) for k in keys))
        states = await self._states(p)
        bad = [k for k in keys if k not in states]
        if bad or not keys:
            raise LabError(404, f"沙箱 {p.sandbox_id} 中没有这些设备：{bad or '（空）'}")
        sb, now = p.sandbox_id, self.clock()
        # 标过 start_failed、但接口已报运行中的（超时误判的慢设备）：不再停止重启，交给看门狗走就绪探测恢复
        late = [k for k in keys if self.tracks.get(k) is not None and self.tracks[k].phase == "failed"
                and states[k]["status"] == "online"]
        for k in late:
            self.tracks[k].phase = "starting"
        keys = [k for k in keys if k not in late]
        if late and not keys:
            self._ensure_loop()
            await self._persist()
            return {"ok": True, "action": "start", "devices": late, "started": [], "queued": [],
                    "stagger": self.stagger, "restarted": [], "recovering": late}
        redo = [k for k in keys if (self.tracks.get(k) is not None and self.tracks[k].phase == "failed")
                or states[k]["status"] == "error"]
        if redo:
            await p.power_action("stop", redo)
            self.note_stopped(sb, redo)
        for k in keys:
            old = self.tracks.get(k)
            if old is not None and old.phase == "queued":
                continue
            self._cancel_job(k)
            self.tracks[k] = Track(sb, k, states[k]["name"], "queued", now, now, source=source,
                                   not_before=self._not_before(sb, k), after_stop=k in redo)
            if k not in self._queue:
                self._queue.append(k)
        started: list[int] = []
        k0 = self._eligible()
        if k0 is not None and not self.tracks[k0].after_stop:
            batch = [k for k in self._queue if self.tracks[k].not_before <= self.clock()
                     and not self.tracks[k].after_stop] if self.stagger <= 0 else [k0]
            for k in batch:
                self._queue.remove(k)
            try:
                await self._send_start(p, batch)
            except LabError:
                for k in batch:
                    self.tracks.pop(k, None)
                raise
            started += batch
        self._kick_queue()
        self._ensure_loop()
        await self._persist()
        queued = [k for k in keys if self.tracks.get(k) is not None and self.tracks[k].phase == "queued"]
        return {"ok": True, "action": "start", "devices": keys, "started": started, "queued": queued,
                "stagger": self.stagger, "restarted": redo, "recovering": late}

    def _eligible(self) -> int | None:
        """队列里现在就能发启动的第一台（间隔与停机间隔都满足）；没有返回 None。"""
        now = self.clock()
        if self.stagger > 0 and now - self._last_start < self.stagger:
            return None
        return next((k for k in self._queue if k in self.tracks and self.tracks[k].not_before <= now), None)

    async def _send_start(self, p: Any, batch: list[int]) -> None:
        await p.power_action("start", batch)
        self._last_start = now = self.clock()
        for k in batch:
            t = self.tracks.get(k)
            if t is not None:
                t.phase, t.since, t.restarting, t.after_stop, t.early_checks = "starting", now, False, False, 0
                t.init_at, t.probe_note = None, ""
        log.info("启动设备：%s", "、".join(self.tracks[k].name if k in self.tracks else str(k) for k in batch))

    async def reboot(self, keys: list[int], *, source: str = "api") -> dict[str, Any]:
        """重启 = 停止 → 等 restart_gap 秒（上一个容器清理完）→ 启动（后台）。不用 batchReboot（本机返回 405，
        且立即重新启动正是卡住的诱因）。"""
        p = self._provider()
        keys = list(dict.fromkeys(int(k) for k in keys))
        names = getattr(p, "device_name", lambda k: str(k))
        for k in keys:
            self._cancel_job(k)
            self._dequeue(k)
        res = await p.power_action("stop", keys)
        sb, now = p.sandbox_id, self.clock()
        self.note_stopped(sb, keys)
        for k in keys:
            self.tracks[k] = Track(sb, k, names(k), "queued", now, now, source=source,
                                   not_before=self._not_before(sb, k), after_stop=True)
            self._queue.append(k)
        self._kick_queue()
        self._ensure_loop()
        await self._persist()
        return {**res, "action": "reboot", "via": "stop+start", "start_after_s": self.restart_gap}

    async def stop(self, keys: list[int]) -> dict[str, Any]:
        p = self._provider()
        keys = [int(k) for k in keys]
        for k in keys:
            self._cancel_job(k)
            self._dequeue(k)
        res = await p.power_action("stop", keys)
        self.note_stopped(p.sandbox_id, keys)
        self.forget(keys)
        await self._persist()
        return res

    def adopt(self, keys: list[int], names: dict[int, str]) -> None:
        """别处发起、正在启动的设备也纳入看门狗（按题目准备环境时）。"""
        p = getattr(self.mgr, "provider", None)
        now = self.clock()
        for k in keys:
            if k not in self.tracks:
                self.tracks[k] = Track(p.sandbox_id, k, names.get(k, str(k)), "starting", now, now, source="adopt")
        if keys:
            self._ensure_loop()

    def forget(self, keys) -> None:
        for k in keys:
            self._cancel_job(k)
            self._dequeue(k)
            self.tracks.pop(int(k), None)

    def _dequeue(self, k: int) -> None:
        if k in self._queue:
            self._queue.remove(k)

    def _cancel_job(self, k: int) -> None:
        j = self._jobs.pop(int(k), None)
        if j is not None and not j.done() and j is not asyncio.current_task():
            j.cancel()

    def reset(self) -> None:
        """切换环境 / 沙箱：不再跟踪旧沙箱的设备（停机时刻保留，切回来时照样遵守停机间隔）。"""
        for k in list(self._jobs):
            self._cancel_job(k)
        if self._queue_task is not None and not self._queue_task.done():
            self._queue_task.cancel()
        self._queue_task = None
        self._queue.clear()
        self.tracks.clear()

    # ---- 队列 ----

    def _kick_queue(self) -> None:
        if self._queue and not self._closed and (self._queue_task is None or self._queue_task.done()):
            self._queue_task = asyncio.create_task(self._run_queue())

    async def _run_queue(self) -> None:
        try:
            while self._queue and not self._closed:
                p = getattr(self.mgr, "provider", None)
                for k in list(self._queue):           # 被遗忘 / 换了沙箱的不再发
                    t = self.tracks.get(k)
                    if t is None or t.phase != "queued" or p is None or p.sandbox_id != t.sandbox:
                        self._queue.remove(k)
                if not self._queue:
                    break
                now = self.clock()
                k = self._eligible()
                if k is None:
                    nb = min(self.tracks[q].not_before for q in self._queue)
                    gap = self._last_start + self.stagger - now if self.stagger > 0 else 0.0
                    await asyncio.sleep(min(1.0, max(0.05, nb - now, gap)))
                    continue
                t = self.tracks[k]
                if t.after_stop:                      # 刚发过停止：确认接口已是「已停止」再发启动
                    st = (await self._states(p)).get(k, {}).get("status")
                    if st not in ("stopped", "unknown", None):
                        if now - t.since > self.stop_wait:
                            self._queue.remove(k)
                            t.phase, t.restarting = "failed", False
                            t.reason = f"{t.reason + '；' if t.reason else ''}{int(self.stop_wait)} 秒内没有停下来（接口状态 {st}）"
                            self._event(t, "failed", t.reason)
                            await self._persist()
                        else:
                            t.not_before = now + 2.0
                        continue
                self._queue.remove(k)
                try:
                    await self._send_start(p, [k])
                except Exception as exc:
                    detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
                    t.phase, t.restarting = "failed", False
                    t.reason = f"{t.reason + '；' if t.reason else ''}发出启动请求失败：{detail}"
                    self._event(t, "failed", t.reason)
                    self._last_start = self.clock()
                await self._persist()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("启动队列异常")

    # ---- 看门狗 ----

    def _ensure_loop(self) -> None:
        if self._closed:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._closed and any(t.phase != "ready" for t in self.tracks.values()):
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:          # 读不到状态（会话被挤掉等）：下一轮再试
                    log.warning("启动看门狗本轮失败：%s", getattr(exc, "detail", None) or exc)
                await asyncio.sleep(self.poll)
            for k in [k for k, t in self.tracks.items() if t.phase == "ready"]:
                self.tracks.pop(k, None)          # 就绪的不再叠加状态
            await self._persist()
        except asyncio.CancelledError:
            pass

    async def _states(self, p: Any) -> dict[int, dict[str, Any]]:
        try:
            return await p.device_states()
        except LabError:
            raise
        except Exception as exc:
            raise LabError(502, getattr(exc, "message", None) or str(exc)) from None

    async def tick(self) -> None:
        p = getattr(self.mgr, "provider", None)
        if p is None or not getattr(p, "power", False) or not self.tracks:
            return
        states = await self._states(p)
        now = self.clock()
        changed = False
        for k, t in list(self.tracks.items()):
            if t.sandbox != p.sandbox_id:
                self.tracks.pop(k, None)
                changed = True
                continue
            if t.phase in ("queued", "ready") or t.restarting or (k in self._jobs and not self._jobs[k].done()
                                                                   and t.phase != "initializing"):
                continue
            s = states.get(k)
            if s is None:
                self.tracks.pop(k, None)
                changed = True
                continue
            st, code = s["status"], s.get("code")
            if st == "online":
                if t.phase in ("starting", "failed"):
                    # 已标 start_failed 的设备（超时误判：设备只是慢，最后一轮没有停它）接口报运行中后照样走就绪探测，
                    # 通过就恢复为 online，start_errors 里不再有它（2026-10-07：笔记本上冷启动 187~202 秒）
                    late = "（此前判为启动失败，设备随后自行启动完成）" if t.phase == "failed" else ""
                    t.recovered = bool(late)
                    if not self.probe_enabled:
                        t.phase, t.reason = "ready", ""
                        self._event(t, "ready", f"接口报运行中（启动用时 {int(now - t.since)} 秒）{late}")
                        changed = True
                        continue
                    t.phase, t.init_at, t.reason, t.probe_note, t.last_probe = "initializing", now, "", "", 0.0
                    self._event(t, "initializing", f"接口报运行中（启动用时 {int(now - t.since)} 秒）{late}")
                    changed = True
                if t.phase == "initializing":
                    if now - (t.init_at or now) >= self.probe_timeout:
                        self._cancel_job(k)
                        self._stuck(p, t, f"接口报运行中 {int(now - (t.init_at or now))} 秒后设备仍未就绪"
                                          f"（{t.probe_note or 'console 无响应'}）")
                        changed = True
                    elif (k not in self._jobs or self._jobs[k].done()) and now - t.last_probe >= self.probe_interval:
                        t.last_probe = now
                        self._spawn(k, self._probe(t))
            elif st == "stopped":
                if t.phase == "failed" or now - t.since > self.stop_grace:
                    self._event(t, "forget", "设备已停止")
                    self.tracks.pop(k, None)
                    getattr(p, "pending", {}).pop(k, None)     # 别处停掉的：不再显示「启动中」
                    changed = True
            elif st == "error":
                if t.phase in ("starting", "initializing"):
                    self._stuck(p, t, f"eNSP Pro 报{ERROR_CODES.get(code, '错误状态')}（状态 {code}）")
                    changed = True
            elif st == "starting":
                if t.phase == "initializing":
                    t.phase = "starting"
                if t.phase == "starting" and now - t.since >= self.start_timeout:
                    self._stuck(p, t, f"启动 {int(now - t.since)} 秒仍停在「{START_PHASE.get(code, '启动中')}」"
                                      f"（状态 {code}）")
                    changed = True
                elif (t.phase == "starting" and code == 6 and self.early_check > 0 and t.early_checks < 2
                      and now - t.since >= self.early_check * (t.early_checks + 1) and hasattr(p, "boot_failure")):
                    t.early_checks += 1
                    self._spawn(k, self._early(p, t))
        if changed:
            await self._persist()

    def _spawn(self, k: int, coro) -> None:
        self._cancel_job(k)
        self._jobs[k] = asyncio.create_task(coro)

    def _event(self, t: Track, what: str, detail: str) -> None:
        self.events.append({"at": self.clock(), "device": t.name, "event": what, "detail": detail})
        del self.events[:-50]
        log.info("启动看门狗：%s %s（%s）", t.name, what, detail)

    def _stuck(self, p: Any, t: Track, why: str) -> None:
        if t.restarts < self.retries:
            t.restarts += 1
            t.reason, t.restarting = why, True
            self._event(t, "restart", why)
            self._spawn(t.dev, self._restart(p, t))
        else:
            t.phase = "failed"
            t.reason = (f"{why}；已自动停止再启动 {t.restarts} 次仍未就绪。请在拓扑图上再点一次「启动」"
                        f"（会先停止再启动），仍不行请检查 eNSP Pro" if t.restarts else
                        f"{why}。请在拓扑图上再点一次「启动」（会先停止再启动）")
            self._event(t, "failed", t.reason)

    async def _restart(self, p: Any, t: Track) -> None:
        """卡住的设备：断开终端 → 停止 → 排队，等 restart_gap 秒、确认已停止后再启动。"""
        hub = getattr(self.mgr, "hub", None)
        if hub is not None and hasattr(hub, "disconnect"):
            try:
                await hub.disconnect(t.dev, "\r\n[设备启动卡住，ainet 正在自动停止后重新启动，终端已断开]\r\n")
            except Exception:
                pass
        try:
            await p.power_action("stop", [t.dev])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            t.phase, t.restarting = "failed", False
            t.reason = f"{t.reason}；自动停止失败：{getattr(exc, 'detail', None) or exc}"
            self._event(t, "failed", t.reason)
        else:
            self.note_stopped(t.sandbox, [t.dev])
            now = self.clock()
            t.phase, t.since, t.not_before, t.after_stop = "queued", now, self._not_before(t.sandbox, t.dev), True
            t.init_at, t.probe_note = None, ""
            if t.dev not in self._queue:
                self._queue.append(t.dev)
            self._kick_queue()
        await self._persist()

    async def _early(self, p: Any, t: Track) -> None:
        """早期卡死检查：设备日志里已出现板卡复位循环（spec §14 第 21 项）就不等 start_timeout，立即按卡住处理。"""
        try:
            why = await p.boot_failure(t.dev)
        except asyncio.CancelledError:
            raise
        except Exception:
            why = None
        if why and self.tracks.get(t.dev) is t and t.phase == "starting" and not t.restarting:
            self._stuck(p, t, f"启动 {int(self.clock() - t.since)} 秒：{why}")
            await self._persist()

    async def _probe(self, t: Track) -> None:
        try:
            ok, note = await self.prober(t.dev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ok, note = None, f"{type(exc).__name__}: {exc}"
        if self.tracks.get(t.dev) is not t or t.phase != "initializing":
            return
        if ok:
            t.phase, t.probe_note, t.reason = "ready", "", ""
            late = "（此前判为启动失败，已恢复）" if t.recovered else ""
            self._event(t, "ready", f"就绪探测通过（接口报运行中后 {int(self.clock() - (t.init_at or 0))} 秒）{late}")
            await self._persist()
        else:
            t.probe_note = note

    async def _probe_console(self, dev: int) -> tuple[bool | None, str]:
        """经 hub 隐藏事务执行 PROBE_COMMANDS：True = 就绪；False = 设备报 busy；None = 暂时判断不了（连不上 / 学员在输入）。"""
        hub = getattr(self.mgr, "hub", None)
        if hub is None:
            return True, ""
        r = await hub.run_hidden(dev, PROBE_COMMANDS, kind="check", cmd_timeout=20.0)
        text = "\n".join([r.reason or "", *r.outputs])
        m = BUSY_RE.search(text) if r.status != "busy" else None
        if m:
            line = next((ln.strip() for ln in text.splitlines() if m.group(0) in ln), m.group(0))
            return False, f"设备忙：{line[:80]}"
        if r.status == "ok":
            return True, ""
        if r.status == "busy":
            return None, r.reason            # 学员正在输入 / 停在分页等交互上：稍后再试
        return None, (r.reason or "console 暂无响应")[:120]

    # ---- 持久化 / 重启恢复 ----

    async def _persist(self) -> None:
        p = getattr(self.mgr, "provider", None)
        data = {"env": getattr(self.mgr, "key", ""), "sandbox": getattr(p, "sandbox_id", None),
                "tracks": [asdict(t) for t in self.tracks.values() if t.phase != "ready"],
                "stopped_at": [[sb, k, at] for (sb, k), at in self.stopped_at.items()
                               if self.clock() - at < self.restart_gap]}
        try:
            await asyncio.to_thread(db.set_setting, SETTING_POWER, json.dumps(data, ensure_ascii=False))
        except Exception as exc:              # 库不可写不影响看门狗本身
            log.warning("保存启动跟踪状态失败：%s", exc)

    def resume(self) -> int:
        """服务启动时：接着跟踪上次没完成的启动（只认当前环境与沙箱的记录）。返回恢复的设备数。
        排队中（含卡住后停在「停止 → 启动」之间）的照样排队发出；启动中 / 初始化中的接着计时、接着探测；
        启动失败的照样显示失败（接口变成运行中会再探测一次）。"""
        try:
            raw = db.get_setting(SETTING_POWER)
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        p = getattr(self.mgr, "provider", None)
        if not data or p is None or not getattr(p, "power", False):
            return 0
        for sb, k, at in data.get("stopped_at") or []:
            self.stopped_at[(sb, int(k))] = float(at)
        if data.get("env") != getattr(self.mgr, "key", None) or data.get("sandbox") != getattr(p, "sandbox_id", None):
            return 0
        n = 0
        for row in data.get("tracks") or []:
            try:
                t = Track(**row)
            except TypeError:
                continue
            if t.phase == "queued":
                self._queue.append(t.dev)
            t.last_probe = 0.0
            self.tracks[t.dev] = t
            n += 1
        if n:
            log.info("服务重启：接着跟踪 %d 台设备的启动（%s）", n,
                     "、".join(f"{t.name}:{t.phase}" for t in self.tracks.values()))
            self._kick_queue()
            self._ensure_loop()
        return n

    async def close(self) -> None:
        self._closed = True
        tasks = [t for t in (self._task, self._queue_task, *self._jobs.values()) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass


class IdleStop:
    """空闲自动停机（见模块说明）。clock 可换成假时钟（测试）。"""

    def __init__(self, mgr: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.mgr = mgr
        self.clock = clock
        self.minutes = config.ENSPRO_IDLE_STOP_MIN
        self.warn = config.ENSPRO_IDLE_WARN
        self.interval = config.ENSPRO_IDLE_CHECK
        self.last = clock()
        self.last_why = "start"
        self.warned = False
        self.last_stop: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def touch(self, why: str = "") -> None:
        self.last = self.clock()
        self.last_why = why
        self.warned = False

    def on_hub(self, event: str, port: int, data: str) -> None:
        # 学员按键、提交的命令、命令输出算活动；异步日志、空闲登出后的自动重登、看门狗探测（隐藏事务）不算
        if event in ("input", "command", "output"):
            self.touch("terminal")

    def view(self) -> dict[str, Any]:
        return {"minutes": self.minutes, "idle_s": round(self.clock() - self.last, 1), "last_activity": self.last_why,
                "warned": self.warned, "last_stop": self.last_stop}

    def start(self) -> None:
        if self.minutes > 0 and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await self.check()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("空闲停机检查失败：%s", getattr(exc, "detail", None) or exc)
        except asyncio.CancelledError:
            pass

    def _blocked(self) -> str:
        m = self.mgr
        busy = m.busy_reason()
        if busy:
            return busy
        if m.switching:
            return "切换中"
        if m.job is not None and not m.job.get("done"):
            return "准备环境中"
        if m.power.busy():
            return "设备启动中"
        return ""

    async def check(self) -> dict[str, Any] | None:
        """评估一次；到点就停机。返回停机记录（没停返回 None）。"""
        m = self.mgr
        p = m.provider
        if self.minutes <= 0 or p is None or not getattr(p, "power", False) or getattr(p, "sandbox_id", None) is None:
            return None
        if self._blocked():
            self.touch(self._blocked())        # 练习 / 考试 / 启动进行中都算活动
            return None
        limit = self.minutes * 60.0
        idle = self.clock() - self.last
        if idle < limit - self.warn:
            return None
        states = await p.device_states()
        running = sorted(k for k, s in states.items() if s["status"] in ("online", "starting") and s.get("console", True))
        if not running:
            self.touch("idle-none-running")   # 没有运行的设备：重新计时
            return None
        names = "、".join(states[k]["name"] for k in running)
        hub = m.hub
        if idle < limit:
            if not self.warned:
                self.warned = True
                left = int(limit - idle)
                n = hub.notify_all(f"\r\n[ainet] 设备已空闲 {self.minutes:g} 分钟，约 {left} 秒后将自动停止当前沙箱的设备"
                                   f"（{names}）以释放资源；在终端里按任意键可取消。\r\n") if hub is not None else 0
                log.info("空闲停机预告：%s（%d 个终端收到通知）", names, n)
            return None
        async with m._lock:
            async with m.exam._lock:
                async with m.pm._op:
                    if self._blocked() or self.clock() - self.last < limit or m.provider is not p:
                        return None
                    if hub is not None:
                        hub.notify_all(f"\r\n[ainet] 设备空闲超过 {self.minutes:g} 分钟，已自动停止：{names}。"
                                       f"需要时在拓扑图上点「启动」。\r\n")
                    await m.power.stop(running)
                    if hub is not None:
                        for k in running:
                            await hub.disconnect(k, "\r\n[设备已因空闲自动停止，终端已断开]\r\n")
        self.last_stop = {"at": time.time(), "sandbox": p.sandbox_id, "devices": [states[k]["name"] for k in running],
                          "idle_min": round(idle / 60, 1)}
        log.info("空闲 %.1f 分钟，已自动停止沙箱 %s 的设备：%s", idle / 60, p.sandbox_id, names)
        self.touch("idle-stop")
        return self.last_stop

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
