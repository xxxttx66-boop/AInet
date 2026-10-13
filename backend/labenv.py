"""模拟环境的运行时切换（spec §5.2「模拟环境注册与切换」）。

LabManager 是「实验上下文」：当前模拟环境、当前沙箱、当前 LabProvider、切换代数都只在这里（main.LAB 只是它的镜像，
供旧代码读取）；干净基线按「环境 + 沙箱名 + 设备」存在 app_settings。目前按单人使用设计（spec §2），全进程一个
LabManager；以后做「每用户独立沙箱」时改成按用户各建一个上下文（spec §5.2「多人模式（未来）」）。

LabManager 负责：
- 启动时选定环境：数据库 app_settings（lab:env、lab:sandbox:<环境>）优先，没有记录时用环境变量
  AINET_LAB_PROVIDER / AINET_ENSPRO_SANDBOX 作缺省值。
- 切换（switch）：串行加锁；有练习 / 考试进行中返回 409；目标环境不可用返回 409；
  关掉旧后端的全部 console 连接与 hub 通道（终端收到「环境已切换」）→ 退出旧后端的登录（eNSP Pro 单会话）→
  建新后端（登录是懒的，第一次调用接口时才登录）→ 清空拓扑 / 状态缓存 → 记库。
- 接入设置（endpoint_info / set_endpoint / test_endpoint）：每种环境的接入地址可在运行时改（app_settings
  lab:endpoint:<环境>，优先级与 lab:env 相同：库里的记录优先，AINET_LAB_FORCE=1 时环境变量优先）；改当前环境的地址时
  与切换同一套锁和 409 判断，断开终端并重建该环境的后端。凭据只报有无（spec §5.2「接入设置」）。
- 按题目准备环境（prepare）：切到题目平台对应的环境与沙箱（按题目 topology 名 = 沙箱名匹配），启动本题需要的设备，
  等到全部运行中；后台任务 + 进度（供课程页调用）。

锁的顺序与 exam.start 一致：先 ExamManager._lock，再 PracticeManager._op，避免死锁；拿到锁后再检查练习 / 考试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

import config
import db
import lab as lab_lib
import scenarios as scenario_lib
from lab import LabError, LabProvider, console_of

log = logging.getLogger("ainet.labenv")

SETTING_ENV = "lab:env"
SETTING_SANDBOX = "lab:sandbox:{}"          # 每种有沙箱的环境各记一个
PREPARE_POLL = 3.0
SYSNAME = {"yunshan": "HUAWEI", "vrp5": "Huawei"}
ENDPOINT_SOURCES = {"db": "数据库记录（页面上设置）", "force": "环境变量（AINET_LAB_FORCE=1，已写回数据库）",
                    "env": "环境变量", "default": "内置缺省值"}
CLOSE_TIMEOUT = 5.0                  # 改接入地址时关闭旧后端（eNSP Pro 登出）的等待上限：旧地址往往已经连不上
# 按题目准备环境的步骤条（课程页浮层，spec §16.3）：stage -> 步骤；restart 是「等待运行」步骤里的警示状态，不单列
STEP_LABELS = {"switch": "切换环境", "sandbox": "查找沙箱", "start": "启动设备", "wait": "等待运行", "probe": "确认就绪",
               "check": "核对设备", "done": "完成"}
STEP_OF = {"switch": "switch", "sandbox": "sandbox", "start": "start", "wait": "wait", "restart": "wait",
           "probe": "probe", "check": "check", "done": "done"}
# 预计用时的经验值（eNSP Pro 真机，spec §3 / §10.5）：接口报运行中 AR 110~125 秒、LSW 55~70 秒；就绪探测再约 30~40 秒；
# 多台一起冷启动互相拖慢（8 台 207 秒全部运行中），每多一台按 +10 秒算。只用来给进度条与「预计还需」，不影响超时判断。
BOOT_S = {"router": 120.0, "switch": 65.0, "pc": 20.0}
BOOT_DEFAULT = 100.0
PROBE_S = 35.0
CROWD_S = 10.0


def _norm(name: str) -> str:
    return re.split(r"[\\/]", str(name or ""))[-1].removesuffix(".topo").strip().lower()


class LabManager:
    def __init__(self, *, make: Callable[..., LabProvider] | None = None) -> None:
        self._make = make                    # None = lab_lib.make_provider（测试会 monkeypatch 它，按调用时取）
        self.key = ""
        self.sandbox: int | None = None
        self.provider: LabProvider | None = None
        self.gen = 0                         # 每切换一次 +1（前端 / Agent 据此发现环境变了）
        self.switching = False
        self._lock = asyncio.Lock()
        self.hub: Any = None
        self.pm: Any = None
        self.exam: Any = None
        self.agent: Any = None
        self.on_install: Callable[[LabProvider], None] | None = None
        self.job: dict[str, Any] | None = None   # 最近一次「按题目准备环境」
        self._job_task: asyncio.Task | None = None
        self._job_seq = 0
        self.prepare_timeout: float | None = None   # None = 按看门狗参数推算（见 _prepare_deadline）
        self.prepare_poll = PREPARE_POLL
        self.source = ""                     # 启动时环境来自哪里：db / env（AINET_LAB_FORCE）/ env-default（库里没有记录）
        self.last_stopped_old: dict[str, Any] | None = None
        # 同时运行设备上限（里程碑 6.17）：见过的拓扑 {环境: {拓扑键: topology}}，计数时用其中的设备类型；判断不了的题只记一次日志
        self._topos: dict[str, dict[str, dict[str, Any]]] = {}
        self._cap_logged: set[str] = set()
        # 启动可靠性（spec §10.5）：所有启动经看门狗；空闲自动停机
        from labpower import IdleStop, PowerWatch
        self.power = PowerWatch(self)
        self.idle = IdleStop(self)

    # ---- 同时运行设备上限（里程碑 6.17）----

    def note_topology(self, cur: dict[str, Any] | None, key: str | None = None) -> None:
        """记下 resolve() 看到的拓扑（各环境、各沙箱最近一次），供计数时取设备类型（eNSP Pro 的 deviceType）。"""
        t = (cur or {}).get("topology")
        if not t or not t.get("devices"):
            return
        key = key or ((cur or {}).get("env") or {}).get("key") or self.key
        box = self._topos.setdefault(key, {})
        for name in {t.get("name") or "", t.get("path") or ""} - {""}:
            box[scenario_lib.topo_key(name)] = t

    def target_env(self, s: dict[str, Any], key: str | None = None) -> lab_lib.LabEnv | None:
        """题目要在哪个环境里做：给了 key（正在浏览 / 指定的环境）且平台相同就是它；否则当前环境平台相同就用当前环境，
        再否则该平台注册顺序第一个（同一平台可能有几台 eNSP Pro，里程碑 6.19）。"""
        plat = scenario_lib.platform_of(s)
        env = lab_lib.REGISTRY.get((key or "").strip().lower()) if key else None
        if env is not None and env.platform == plat:
            return env
        return lab_lib.env_of_platform(plat, prefer=self.key)

    def capacity(self, s: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        """本题与题目所属环境（target_env）的同时运行设备上限比：{limit, count, over}。limit 0 = 不限；count None = 判断不了
        （不隐藏，记一条日志）。计数口径见 scenarios.device_load（PC 类不计）。"""
        env = self.target_env(s, key)
        limit = lab_lib.max_devices(env.key) if env is not None else 0
        if limit <= 0:
            return {"limit": 0, "count": None, "over": False}
        topo = self._topos.get(env.key, {}).get(scenario_lib.topo_key(s["topology"]))
        n = scenario_lib.device_load(s, topo)
        if n is None:
            if s["id"] not in self._cap_logged:
                self._cap_logged.add(s["id"])
                log.warning("题目 %s 的设备数判断不了（拓扑 %s 中缺少本题设备），不按同时运行设备上限隐藏",
                            s["id"], s["topology"])
            return {"limit": limit, "count": None, "over": False}
        return {"limit": limit, "count": n, "over": n > limit}

    def capacity_reason(self, s: dict[str, Any], key: str | None = None) -> str | None:
        """超过上限时的中文原因（按 id 开练 / 单题考试 / 准备环境返回 409 用）；没超返回 None。"""
        c = self.capacity(s, key)
        if not c["over"]:
            return None
        return (f"「{s.get('title', s['id'])}」需要同时运行 {c['count']} 台路由器 / 交换机（不含 PC），"
                f"超过本机上限 {c['limit']} 台，无法开始（上限在课程页「⚙ 接入设置」里改）")

    # ---- 启动 ----

    @staticmethod
    def saved() -> tuple[str, dict[str, int | None]]:
        """(环境 key, {环境: 沙箱})：库里的记录优先，没有时用环境变量缺省值。"""
        key = (db.get_setting(SETTING_ENV) or config.LAB_PROVIDER or "ensp").strip().lower()
        if key not in lab_lib.REGISTRY:
            log.warning("库里记录的模拟环境 %r 未注册，改用 ensp", key)
            key = "ensp"
        sbs: dict[str, int | None] = {}
        for env in lab_lib.environments():
            if not env.sandboxes:
                continue
            raw = db.get_setting(SETTING_SANDBOX.format(env.key))
            if raw is not None:
                sbs[env.key] = int(raw) if raw.strip() else None
            elif env.key == "enspro":
                sbs[env.key] = config.ENSPRO_SANDBOX
            else:
                sbs[env.key] = None
        return key, sbs

    def _new(self, key: str, sandbox: int | None) -> LabProvider:
        make = self._make or lab_lib.make_provider
        env = lab_lib.get_env(key)
        p = make(key, **({"sandbox": sandbox} if env.sandboxes else {}))
        if getattr(p, "power", False):
            try:
                p.watch = self.power          # resolve() 叠加 initializing / start_failed
            except AttributeError:
                pass
        return p

    def boot(self) -> LabProvider:
        """选定启动时的环境。AINET_LAB_FORCE=1：以环境变量为准（并写回库）；否则库里的记录优先。日志写明来源。
        最先注册用户添加的环境实例（lab:envs，里程碑 6.19），否则库里记的当前环境是 enspro-N 时会被当成未注册。"""
        lab_lib.load_custom_envs()
        if config.LAB_FORCE:
            key = (config.LAB_PROVIDER or "ensp").strip().lower()
            if key not in lab_lib.REGISTRY:
                log.warning("AINET_LAB_PROVIDER=%r 未注册，改用 ensp", key)
                key = "ensp"
            _, sbs = self.saved()
            if config.ENSPRO_SANDBOX is not None:      # 没设 AINET_ENSPRO_SANDBOX 时沿用库里的沙箱
                sbs["enspro"] = config.ENSPRO_SANDBOX
            db.set_setting(SETTING_ENV, key)
            if lab_lib.get_env(key).sandboxes and sbs.get(key) is not None:
                db.set_setting(SETTING_SANDBOX.format(key), str(sbs[key]))
            self.source = "env"
        else:
            self.source = "db" if db.get_setting(SETTING_ENV) else "env-default"
            key, sbs = self.saved()
        lab_lib.load_endpoints(force=config.LAB_FORCE)     # 各环境的接入地址（库里的记录 / 环境变量）
        lab_lib.load_watch(force=config.LAB_FORCE)         # 各环境的看门狗参数（同一套优先级）
        self.key, self.sandbox = key, sbs.get(key)
        self.provider = self._new(key, self.sandbox)
        self._apply_watch(key)
        where = {"db": "数据库记录（app_settings lab:env，改环境变量不生效；要以环境变量为准请设 AINET_LAB_FORCE=1）",
                 "env": "环境变量（AINET_LAB_FORCE=1，已覆盖并写回数据库）",
                 "env-default": "环境变量缺省值（数据库里还没有记录）"}[self.source]
        log.info("当前模拟环境：%s%s，来源：%s", self.env.label,
                 f"（沙箱 {self.sandbox}）" if self.env.sandboxes else "", where)
        for e in lab_lib.environments():
            if e.endpoint is not None:
                log.info("%s %s：%s（来源：%s）", e.label, e.endpoint.label, lab_lib.endpoint_value(e.key),
                         ENDPOINT_SOURCES.get(lab_lib.endpoint_source(e.key), "?"))
            if e.watch:
                log.info("%s 启动看门狗：%s", e.label, "，".join(
                    f"{p.label} {lab_lib.watch_value(e.key, p.name):g}（{ENDPOINT_SOURCES.get(lab_lib.watch_source(e.key, p.name), '?')}）"
                    for p in e.watch))
        return self.provider

    def _apply_watch(self, key: str, *, force: bool = False) -> None:
        """看门狗换成环境 key 的参数（没有看门狗的环境不动）。同一环境内换沙箱不重设（force 除外），
        测试 / e2e 直接改过的 PowerWatch 属性不会被覆盖。"""
        env = lab_lib.REGISTRY.get(key)
        if env is None or not env.watch or (self.power.configured_for == key and not force):
            return
        self.power.configure(lab_lib.watch_values(key), env=key)

    def bind(self, hub: Any, pm: Any, exam: Any, agent: Any = None) -> None:
        self.hub, self.pm, self.exam, self.agent = hub, pm, exam, agent
        if hub is not None and hasattr(hub, "add_listener"):
            hub.add_listener(self.idle.on_hub)   # 终端输入 / 命令 / 输出 = 有活动（空闲停机计时）

    def start_background(self) -> None:
        """lifespan 里调用（需要事件循环）：接着跟踪上次没完成的启动、开始空闲停机检查。"""
        self.power.resume()
        self.idle.start()

    def touch(self, why: str = "") -> None:
        """有活动（练习 / 考试 / Agent 命令 / 启停 / 准备环境）：空闲停机重新计时。"""
        self.idle.touch(why)

    @property
    def env(self) -> lab_lib.LabEnv:
        return lab_lib.get_env(self.key)

    # ---- 状态 ----

    def busy_reason(self, what: str = "切换模拟环境") -> str:
        """有练习 / 考试进行中时不能切换（返回原因；空串 = 可以切换）。what：被拦下的操作（改接入地址也用这套判断）。"""
        ex = getattr(self.exam, "exam", None)
        a = getattr(self.pm, "active", None)
        if ex is not None or (a is not None and a.mode == "exam"):
            return f"考试进行中，交卷后才能{what}"
        if a is not None and getattr(a, "phase", None) == "ending":
            return f"上一次练习正在后台复位设备，请稍候（通常几十秒）再{what}"
        if a is not None:
            return f"练习进行中，结束练习后才能{what}"
        return ""

    def info(self) -> dict[str, Any]:
        """GET /api/lab：provider.info() 加当前环境 key、沙箱、切换代数。"""
        p = self.provider
        base = p.info() if p is not None else {}
        return {**base, "key": self.key, "sandbox": self.sandbox, "gen": self.gen, "switching": self.switching,
                "source": self.source, "watch": self.power.view(), "idle_stop": self.idle.view(),
                "stop_old": config.ENSPRO_STOP_OLD, "last_stopped_old": self.last_stopped_old}

    def env_state(self) -> dict[str, Any]:
        """拼进 /api/topology/status|current 的 env 字段（不改原有 lab 字段）。"""
        return {"key": self.key, "sandbox": self.sandbox, "gen": self.gen, "switching": self.switching,
                "label": self.env.label, "platform": self.env.platform}

    def _access(self, env: lab_lib.LabEnv) -> dict[str, Any]:
        """环境列表 / 接入设置里每个环境的接入信息：endpoint（地址与来源）、secrets（凭据有无，绝不含值）、runtime_local。"""
        ep = lab_lib.endpoint_view(env.key)
        out = {"endpoint": ep, "secrets": lab_lib.secrets_view(env),
               "runtime_local": ep["runtime_local"] if ep is not None else None,
               "watchdog": lab_lib.watch_view(env.key), "device_password": None}
        if (env.type or env.key) == "enspro":
            # 设备 console 密码（按实例，里程碑 6.19）：只报有无与来源；页面上填写时写进 secrets 文件的 name 行
            out["device_password"] = {"name": lab_lib.device_secret_name(env.key),
                                      "configured": bool(lab_lib.device_password(env.key)),
                                      "source": lab_lib.device_password_source(env.key)}
        return out

    def _overlay(self, env: lab_lib.LabEnv, pr: dict[str, Any]) -> dict[str, Any]:
        """当前环境：叠加后端自己知道的问题（eNSP Pro 登录被拒 / 会话被挤掉）。端口能连上时仍算在线（还能浏览题目、
        在操作页「重新登录」），只把原因分类与说明带上，warning=true。"""
        fn = getattr(self.provider, "health", None) if env.key == self.key else None
        h = fn() if callable(fn) else None
        if h and pr.get("available"):
            return {**pr, "reason_code": h[0], "reason": h[1], "warning": True}
        return pr

    async def environments(self, *, force: bool = False) -> dict[str, Any]:
        envs = lab_lib.environments()
        probes = await asyncio.gather(*(lab_lib.available(e.key, force=force) for e in envs))
        _, sbs = await asyncio.to_thread(self.saved)
        access = await asyncio.to_thread(lambda: [self._access(e) for e in envs])
        topo = getattr(self.provider, "_last_topo", None) or {}
        busy = self.busy_reason()
        return {
            "current": self.key, "label": self.env.label, "platform": self.env.platform,
            "sandbox": self.sandbox, "sandbox_name": topo.get("name") if self.env.sandboxes else None,
            "gen": self.gen, "switching": self.switching, "locked": busy,
            "can_switch": not busy and not self.switching, "saved_sandboxes": sbs,
            "environments": [{**e.view(), **self._overlay(e, pr), **ac, "current": e.key == self.key}
                             for e, pr, ac in zip(envs, probes, access)],
            "types": [t.view() for t in lab_lib.ENV_TYPES.values()],      # 课程页「+」可添加的环境类型
        }

    # ---- 接入设置（spec §5.2「接入设置」） ----

    @staticmethod
    def _endpoint_env(key: str) -> lab_lib.LabEnv:
        try:
            env = lab_lib.get_env(key)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        if env.endpoint is None:
            raise LabError(400, f"{env.label} 没有可配置的接入地址")
        return env

    @staticmethod
    async def _detect(kind: str, value: str) -> dict[str, Any]:
        """校验 + 自动识别 https（lab.detect_scheme）：{value, scheme_check, normalized_from?, notice?, warning?}；格式不对 422。"""
        try:
            return await lab_lib.detect_scheme(kind, value)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None

    def endpoint_info(self, key: str) -> dict[str, Any]:
        """GET /api/lab/environments/{key}/endpoint：当前地址与来源、缺省值、凭据有无、runtime_local、能否修改。"""
        env = self._endpoint_env(key)
        busy = self.busy_reason("修改接入地址")
        return {"environment": env.key, "label": env.label, "current": env.key == self.key, **self._access(env),
                "locked": busy, "can_edit": not busy and not self.switching}

    async def test_endpoint(self, key: str, value: str | None = None) -> dict[str, Any]:
        """「测试连接」：用给定地址（缺省 = 当前地址）探测一次，不缓存、不改设置；与在线探测一样不登录、不连设备 console。"""
        env = self._endpoint_env(key)
        extra: dict[str, Any] = {}
        if value is not None and value.strip():
            det = await self._detect(env.endpoint.kind, value)
            value = det.pop("value")
            extra = det
        else:
            value = lab_lib.endpoint_value(env.key)
            # 已存的 http 地址不自动迁移；测试时发现该端口其实是 https，就给 suggest（⚙ 气泡显示「建议改为 https」一键改）
            if env.endpoint.kind == "url" and value.startswith("http://"):
                try:
                    det = await lab_lib.detect_scheme("url", value)
                except ValueError:
                    det = {}
                if det.get("scheme_check") == "https":
                    extra["suggest"] = det["value"]
        res = await lab_lib.probe_endpoint(env.key, value)
        res.update(extra)
        if env.local_runtime:
            res["runtime_local"] = await asyncio.to_thread(lab_lib.is_local_host, value)
        return res

    async def set_endpoint(self, key: str, value: str | None) -> dict[str, Any]:
        """POST /api/lab/environments/{key}/endpoint：改接入地址（空 = 删掉库里的记录，回到环境变量 / 缺省值）。
        与切换同一套锁与判断：练习 / 考试进行中、准备环境任务进行中 409。改的是当前环境且地址变了：断开全部终端
        （提示「接入地址已改为 …」）、关闭旧后端、按新地址重建（gen + 1，前端据此重连），不停旧沙箱的设备。"""
        env = self._endpoint_env(key)
        norm: str | None = None
        det: dict[str, Any] = {}
        if value is not None and value.strip():
            det = await self._detect(env.endpoint.kind, value)    # 自动识别 https：拿锁之前做（有网络 I/O）
            norm = det.pop("value")
        elif env.custom:
            raise LabError(422, f"{env.label} 是页面上添加的环境，没有缺省地址，地址不能为空")
        async with self._lock:
            self.switching = True
            try:
                async with self.exam._lock:
                    async with self.pm._op:
                        busy = self.busy_reason("修改接入地址")
                        if busy:
                            raise LabError(409, busy)
                        if self.job is not None and not self.job["done"]:
                            raise LabError(409, f"正在为「{self.job['title']}」准备环境（{self.job['message']}），"
                                                "完成后再修改接入地址")
                        old, old_src = lab_lib.endpoint_value(env.key), lab_lib.endpoint_source(env.key)
                        await asyncio.to_thread(db.set_setting, lab_lib.SETTING_ENDPOINT.format(env.key), norm)
                        lab_lib.set_endpoint_override(env.key, norm, "db")
                        new = lab_lib.endpoint_value(env.key)
                        rebuilt = False
                        if new != old and env.key == self.key:
                            await self._replace(env, self.sandbox, notice=f"{env.label} 接入地址已改为 {new}",
                                                stop_old=False, close_timeout=CLOSE_TIMEOUT)
                            rebuilt = True
                        if new != old:
                            log.info("%s %s已改：%s -> %s%s", env.label, env.endpoint.label, old, new,
                                     "（已重建当前后端）" if rebuilt else "")
                        self.touch("endpoint")
                        return {"changed": new != old or lab_lib.endpoint_source(env.key) != old_src,
                                "rebuilt": rebuilt, "environment": env.key, **self._access(env),
                                "env": self.env_state(), **det}
            finally:
                self.switching = False

    # ---- 启动看门狗参数（按环境单独配置，spec §5.2「接入设置」/ §10.5） ----

    @staticmethod
    def _watch_env(key: str) -> lab_lib.LabEnv:
        try:
            env = lab_lib.get_env(key)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        if not env.watch:
            raise LabError(400, f"{env.label} 没有启动看门狗（设备只能在模拟器界面上启停），没有可配置的参数")
        return env

    def watch_info(self, key: str) -> dict[str, Any]:
        """GET /api/lab/environments/{key}/watchdog：各参数的当前值、来源、缺省值、范围，能否修改。"""
        env = self._watch_env(key)
        busy = self.busy_reason("修改看门狗参数")
        return {"environment": env.key, "label": env.label, "current": env.key == self.key,
                "watchdog": lab_lib.watch_view(env.key), "locked": busy, "can_edit": not busy and not self.switching}

    async def set_watch(self, key: str, values: dict[str, Any]) -> dict[str, Any]:
        """POST /api/lab/environments/{key}/watchdog {参数: 值 | null}：只改给出的参数，null = 删掉库里的记录（回到环境变量 /
        缺省值）。范围不对 422；练习 / 考试 / 准备环境进行中 409（同接入地址）。改的是当前环境（或看门狗正用着的环境）时
        立即生效：不重建后端、不断开终端，进行中的启动按新值继续计时。"""
        env = self._watch_env(key)
        updates: dict[str, Any] = {}
        try:
            for name, raw in values.items():
                p = lab_lib._watch_param(env, name)
                updates[name] = None if raw is None or (isinstance(raw, str) and not raw.strip()) \
                    else lab_lib.normalize_watch(p, raw)
            merged = {**lab_lib.watch_values(env.key)}
            for name, v in updates.items():
                merged[name] = lab_lib.watch_default(env.key, name) if v is None else v
            lab_lib.check_watch(merged)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        async with self._lock:
            async with self.exam._lock:
                async with self.pm._op:
                    busy = self.busy_reason("修改看门狗参数")
                    if busy:
                        raise LabError(409, busy)
                    if self.job is not None and not self.job["done"]:
                        raise LabError(409, f"正在为「{self.job['title']}」准备环境（{self.job['message']}），"
                                            "完成后再修改看门狗参数")
                    before = {p.name: (lab_lib.watch_value(env.key, p.name), lab_lib.watch_source(env.key, p.name))
                              for p in env.watch}
                    saved = lab_lib.watch_saved(env.key)
                    for name, v in updates.items():
                        if v is None:
                            saved.pop(name, None)
                        else:
                            saved[name] = v
                    await asyncio.to_thread(db.set_setting, lab_lib.SETTING_WATCH.format(env.key),
                                            json.dumps(saved) if saved else None)
                    for name, v in updates.items():
                        lab_lib.set_watch_override(env.key, name, v, "db")
                    after = {p.name: (lab_lib.watch_value(env.key, p.name), lab_lib.watch_source(env.key, p.name))
                             for p in env.watch}
                    applied = env.key == self.key or self.power.configured_for == env.key
                    if applied:
                        self.power.configure(lab_lib.watch_values(env.key), env=env.key)
                    if after != before:
                        log.info("%s 启动看门狗参数已改：%s%s", env.label, "，".join(
                            f"{n} {before[n][0]:g} -> {after[n][0]:g}" for n in after if after[n] != before[n]),
                            "（已立即生效）" if applied else "（下次切到该环境时生效）")
                    return {"changed": after != before, "applied": applied, "environment": env.key,
                            "watchdog": lab_lib.watch_view(env.key)}

    # ---- 用户添加的环境实例（里程碑 6.19，课程页环境列「+」）----

    def _no_exam(self, what: str) -> None:
        ex = getattr(self.exam, "exam", None)
        a = getattr(self.pm, "active", None)
        if ex is not None or (a is not None and a.mode == "exam"):
            raise LabError(409, f"考试进行中，交卷后才能{what}")

    def _env_view(self, env: lab_lib.LabEnv) -> dict[str, Any]:
        return {**env.view(), **self._access(env), "current": env.key == self.key}

    @staticmethod
    def _type_watch(t: lab_lib.EnvType, values: dict[str, Any] | None) -> dict[str, Any]:
        """添加时给的看门狗参数（null / 空串 = 不设）；不合法抛 ValueError。"""
        updates: dict[str, Any] = {}
        for name, raw in (values or {}).items():
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            p = next((x for x in t.watch if x.name == name), None)
            if p is None:
                raise ValueError(f"{t.label} 没有参数 {name!r}（可选 {' / '.join(x.name for x in t.watch) or '无'}）")
            updates[name] = lab_lib.normalize_watch(p, raw)
        lab_lib.check_watch({**{p.name: lab_lib._num(p, p.builtin) for p in t.watch}, **updates})
        return updates

    @staticmethod
    def _env_type(type_: str) -> lab_lib.EnvType:
        t = lab_lib.ENV_TYPES.get(str(type_ or "").strip().lower())
        if t is None:
            raise LabError(422, f"不支持的环境类型 {type_!r}（可选 {' / '.join(lab_lib.ENV_TYPES)}）")
        return t

    async def test_new_env(self, type_: str, url: str) -> dict[str, Any]:
        """添加前的「测试连接」（POST /api/lab/environments/probe）：只连端口 / TLS 握手，不登录、不保存。"""
        t = self._env_type(type_)
        det = await self._detect(t.endpoint_kind, url)
        return {**await lab_lib.probe_type(t.key, det.pop("value")), **det}

    async def add_env(self, type_: str, label: str, url: str, *, password: str | None = None,
                      watch: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST /api/lab/environments：添加一个环境实例。先校验（422），再做与「测试连接」相同的探测（不登录）；探测失败
        也保存，返回 warning。存 lab:envs、lab:endpoint:<key>、lab:watch:<key>（给了参数时）；密码写 secrets 文件的
        ENSPRO_DEVICE_PASSWORD__<KEY> 行（原子写入、保留其他行），接口与日志都不出现密码。不切换当前环境。
        考试进行中 409（课程页考试中也不显示「+」）。"""
        self._no_exam("添加模拟环境")
        t = self._env_type(type_)
        try:
            label = lab_lib.normalize_label(label)
            lab_lib.normalize_endpoint(t.endpoint_kind, url)  # 先只校验格式（不连），其他字段都对了再做网络探测
            pw = lab_lib.normalize_device_password(password) if password else None
            if pw and not t.device_password:
                raise ValueError(f"{t.label} 没有设备 console 密码")
            updates = self._type_watch(t, watch)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        det = await self._detect(t.endpoint_kind, url)        # 自动识别 https（写了 http:// 或没写协议时）
        url = det.pop("value")
        probe = {**await lab_lib.probe_type(t.key, url), **det}   # 添加前探测（与「测试连接」相同，不登录）
        import ai
        async with self._lock:
            try:
                label = lab_lib.normalize_label(label)        # 拿锁后再查一次重名（并发添加）
            except ValueError as exc:
                raise LabError(422, str(exc)) from None
            key = await asyncio.to_thread(lab_lib.next_custom_key, t.key)
            env = lab_lib.register_custom(key, t.key, label)
            try:
                await asyncio.to_thread(db.set_setting, lab_lib.SETTING_ENDPOINT.format(key), url)
                lab_lib.set_endpoint_override(key, url, "db")
                if updates:
                    await asyncio.to_thread(db.set_setting, lab_lib.SETTING_WATCH.format(key), json.dumps(updates))
                    for name, v in updates.items():
                        lab_lib.set_watch_override(key, name, v, "db")
                if pw:
                    await asyncio.to_thread(ai.write_key_file, ai.key_file(), pw, lab_lib.device_secret_name(key))
                await asyncio.to_thread(lab_lib.save_custom_envs)
            except Exception:
                lab_lib.unregister(key)
                for k in (lab_lib.SETTING_ENDPOINT.format(key), lab_lib.SETTING_WATCH.format(key)):
                    await asyncio.to_thread(db.set_setting, k, None)
                raise
        log.info("已添加模拟环境 %s（%s，%s，%s）：%s%s", key, label, t.label, url,
                 "设备密码已写入 secrets 文件" if pw else "未填设备密码",
                 "" if probe["available"] else f"；探测未通过：{probe['reason']}")
        warning = "" if probe["available"] else f"已保存，但{lab_lib.REASON_CODES.get(probe['reason_code'], '')}：{probe['reason']}"
        if not warning and t.device_password and not await asyncio.to_thread(lab_lib.device_password, key):
            warning = (f"已保存，但还没有设备 console 密码（{lab_lib.device_secret_name(key)}），该环境显示为离线；"
                       "在「⚙ 接入设置」里填写")
        if det.get("warning"):                               # 该端口是明文 HTTP：保留 http，另给警告
            warning = f"{warning}；{det['warning']}" if warning else f"已保存，但{det['warning']}"
        return {"key": key, "environment": await asyncio.to_thread(self._env_view, env), "probe": probe,
                "warning": warning, **{k: det[k] for k in ("normalized_from", "notice") if k in det}}

    async def edit_env(self, key: str, *, label: str | None = None, password: str | None = None,
                       clear_password: bool = False) -> dict[str, Any]:
        """PATCH|POST /api/lab/environments/{key}：改显示名（只限用户添加的环境）、设置 / 清除设备 console 密码（eNSP Pro
        类的环境，内置的也可以：写 ENSPRO_DEVICE_PASSWORD__ENSPRO，优先于 ENSPRO_DEVICE_PASSWORD）。不断开终端、不重建
        后端；改了当前环境的密码时清掉「console 登录失败过、不再自动尝试」的记录，下次连接用新密码。考试进行中 409。"""
        self._no_exam("修改模拟环境")
        env = lab_lib.REGISTRY.get((key or "").strip().lower())
        if env is None:
            raise LabError(404, f"没有模拟环境 {key!r}")
        is_pro = (env.type or env.key) == "enspro"
        try:
            if label is not None:
                if not env.custom:
                    raise ValueError(f"{env.label} 是内置环境，不能改名")
                label = lab_lib.normalize_label(label, exclude=env.key)
            if (password or clear_password) and not is_pro:
                raise ValueError(f"{env.label} 没有设备 console 密码")
            pw = lab_lib.normalize_device_password(password) if password else None
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        import ai
        changed: list[str] = []
        async with self._lock:
            if label is not None and label != env.label:
                old = env.label
                env.label = label
                try:
                    await asyncio.to_thread(lab_lib.save_custom_envs)
                except Exception:
                    env.label = old
                    raise
                changed.append("label")
                log.info("模拟环境 %s 已改名：%s -> %s", env.key, old, label)
            name = lab_lib.device_secret_name(env.key)
            if pw is not None:
                old_pw = await asyncio.to_thread(ai.read_secrets_file, ai.key_file(), name)
                await asyncio.to_thread(ai.write_key_file, ai.key_file(), pw, name)
                if old_pw != pw:
                    changed.append("device_password")
            elif clear_password:
                if await asyncio.to_thread(ai.read_secrets_file, ai.key_file(), name) is not None:
                    await asyncio.to_thread(ai.write_key_file, ai.key_file(), None, name)
                    changed.append("device_password")
            if "device_password" in changed:
                lab_lib.clear_probe_cache(env.key)
                if env.key == self.key and isinstance(getattr(self.provider, "auth_blocked", None), dict):
                    self.provider.auth_blocked.clear()
                log.info("模拟环境 %s 的设备 console 密码已%s（%s，secrets 文件）", env.key,
                         "清除" if pw is None else "更新", name)
        return {"changed": changed, "environment": await asyncio.to_thread(self._env_view, env)}

    async def delete_env(self, key: str) -> dict[str, Any]:
        """DELETE /api/lab/environments/{key}：只能删用户添加的环境（内置 400）；是当前环境、练习 / 考试 / 准备环境进行中
        409（与切换同一套锁）。清理：lab:endpoint / lab:watch / lab:sandbox:<key>、干净基线 baseline:<key>:…、secrets 文件
        （ai.key_file()）里的 ENSPRO_DEVICE_PASSWORD__<KEY> 行（环境变量与其他候选 secrets 文件不动）、内存里的覆盖与缓存。"""
        env = lab_lib.REGISTRY.get((key or "").strip().lower())
        if env is None:
            raise LabError(404, f"没有模拟环境 {key!r}")
        if not env.custom:
            raise LabError(400, f"{env.label} 是内置环境，不能删除")
        import ai
        async with self._lock:
            async with self.exam._lock:
                async with self.pm._op:
                    busy = self.busy_reason("删除模拟环境")
                    if busy:
                        raise LabError(409, busy)
                    if self.job is not None and not self.job["done"]:
                        raise LabError(409, f"正在为「{self.job['title']}」准备环境（{self.job['message']}），"
                                            "完成后再删除环境")
                    if env.key == self.key:
                        raise LabError(409, f"{env.label} 是当前环境，先切换到其他环境再删除")
                    name = lab_lib.device_secret_name(env.key)

                    def cleanup() -> tuple[int, bool]:
                        for k in (lab_lib.SETTING_ENDPOINT, lab_lib.SETTING_WATCH, SETTING_SANDBOX):
                            db.set_setting(k.format(env.key), None)
                        n = db.delete_settings_prefix(f"baseline:{env.key}:")
                        had = ai.read_secrets_file(ai.key_file(), name) is not None
                        if had:
                            ai.write_key_file(ai.key_file(), None, name)
                        return n, had
                    n_base, had_secret = await asyncio.to_thread(cleanup)
                    lab_lib.unregister(env.key)
                    await asyncio.to_thread(lab_lib.save_custom_envs)
                    self._topos.pop(env.key, None)
        import os
        log.info("已删除模拟环境 %s（%s）：清理基线 %d 条%s", env.key, env.label, n_base,
                 "，已删除 secrets 文件里的设备密码行" if had_secret else "")
        return {"deleted": env.key, "label": env.label, "baselines_removed": n_base, "secret_removed": had_secret,
                "secret_in_env": bool(os.environ.get(name, "").strip())}

    async def sandboxes(self) -> dict[str, Any]:
        p = self.provider
        if not self.env.sandboxes or not hasattr(p, "list_sandboxes"):
            raise LabError(409, f"当前模拟环境 {self.env.label} 没有沙箱")
        return {"environment": self.key, "current": self.sandbox, "sandboxes": await p.list_sandboxes()}

    # ---- 切换 ----

    async def switch(self, key: str, sandbox: int | None = None, *, sandbox_given: bool = False,
                     _job: bool = False) -> dict[str, Any]:
        """切换模拟环境 / 沙箱。sandbox_given=False 时沿用该环境上次记录的沙箱。返回 {changed, ...env_state}。"""
        try:
            env = lab_lib.get_env(key)
        except ValueError as exc:
            raise LabError(422, str(exc)) from None
        async with self._lock:
            self.switching = True
            try:
                async with self.exam._lock:
                    async with self.pm._op:
                        busy = self.busy_reason()
                        if busy:
                            raise LabError(409, busy)
                        if not _job and self.job is not None and not self.job["done"]:
                            raise LabError(409, f"正在为「{self.job['title']}」准备环境（{self.job['message']}），"
                                                "完成后再切换")
                        if env.sandboxes:
                            if not sandbox_given:
                                sandbox = (await asyncio.to_thread(self.saved))[1].get(env.key)
                        else:
                            sandbox = None
                        if env.key == self.key and sandbox == self.sandbox:
                            return {"changed": False, **self.env_state(), "switching": False}
                        if env.key != self.key:
                            pr = await lab_lib.available(env.key, force=True)
                            if not pr["available"]:
                                raise LabError(409, f"{env.label} 不可用：{pr['reason']}")
                        elif sandbox is not None and hasattr(self.provider, "list_sandboxes"):
                            ids = {s["id"] for s in await self.provider.list_sandboxes()}
                            if sandbox not in ids:
                                raise LabError(404, f"{env.label} 中没有沙箱 {sandbox}")
                        stopped = await self._replace(env, sandbox)
                        self.touch("switch")
                        return {"changed": True, **self.env_state(), "switching": False, "stopped_old": stopped}
            finally:
                self.switching = False

    async def _stop_old(self, old: LabProvider) -> dict[str, Any] | None:
        """切走之前停掉旧沙箱里运行着的设备（AINET_ENSPRO_STOP_OLD，单人使用；spec §10.5）。

        调用时已拿着切换锁、考试锁、练习锁并确认没有练习 / 考试进行中：上一题结束时已复位干净，eNSP Pro 停机时保存的
        就是干净配置。必须在旧后端退出登录之前做（同一账号只能有一个会话，新后端一登录旧会话就失效）。"""
        if not config.ENSPRO_STOP_OLD or not getattr(old, "power", False) or getattr(old, "sandbox_id", None) is None:
            return None
        sb = old.sandbox_id
        name = (getattr(old, "_last_topo", None) or {}).get("name") or f"沙箱 {sb}"
        try:
            states = await old.device_states()
            name = getattr(old, "sandbox_name", None) or name
            ids = sorted(k for k, s in states.items() if s["status"] in ("online", "starting", "error"))
            if ids:
                await old.power_action("stop", ids)
                self.power.note_stopped(sb, ids)   # 切回来时同样遵守停机间隔
            out = {"ok": True, "sandbox": sb, "sandbox_name": name, "devices": [states[k]["name"] for k in ids]}
            if ids:
                log.info("已停止旧沙箱 %s 的设备：%s", name, "、".join(out["devices"]))
        except Exception as exc:              # 停不掉不影响切换（设备照常运行，只是多占资源）
            detail = getattr(exc, "detail", None) or str(exc)
            log.warning("停止旧沙箱 %s 的设备失败：%s", name, detail)
            out = {"ok": False, "sandbox": sb, "sandbox_name": name, "devices": [], "error": detail}
        self.last_stopped_old = {**out, "at": time.time()}
        return out

    async def _replace(self, env: lab_lib.LabEnv, sandbox: int | None, *, notice: str | None = None,
                       stop_old: bool = True, close_timeout: float | None = None) -> dict[str, Any] | None:
        """换后端。notice：终端里显示的提示（缺省「模拟环境已切换为 X」）；stop_old=False 不停旧沙箱的设备（改接入地址时）；
        close_timeout：关闭旧后端的等待上限（None = 不限）。"""
        old, old_label = self.provider, self.env.label
        new_desc = env.label + (f"（沙箱 {sandbox}）" if env.sandboxes and sandbox is not None else "")
        self.gen += 1
        self.power.reset()                    # 不再跟踪旧沙箱的启动（也不会再去重启它们）
        self._apply_watch(env.key)            # 换环境时看门狗换成新环境的参数（同环境换沙箱不变）
        # 1) 关掉旧后端的全部 console 连接与 hub 通道，终端标签收到提示
        if self.hub is not None:
            await self.hub.reset_all(f"\r\n[{notice or f'模拟环境已切换为 {new_desc}'}，终端已断开]\r\n",
                                     default_sysname=SYSNAME.get(env.platform, "Huawei"))
        # 1.5) 停掉旧沙箱里运行着的设备（单人使用，省内存；要在旧后端退出登录之前）
        stopped = await self._stop_old(old) if old is not None and stop_old else None
        # 2) 退出旧后端的登录（eNSP Pro 同一账号只能有一个会话）
        if old is not None:
            try:
                if close_timeout is None:
                    await old.close()
                else:
                    await asyncio.wait_for(old.close(), close_timeout)
            except Exception as exc:          # 登出失败 / 超时不影响切换
                log.warning("关闭旧实验后端 %s 失败：%s", old_label, f"{type(exc).__name__}: {exc}")
        # 3) 建新后端（eNSP Pro 的登录是懒的），清缓存
        new = self._new(env.key, sandbox)
        self.key, self.sandbox, self.provider = env.key, sandbox, new
        import ensp_runtime
        import topology
        topology.clear_scan_cache()
        ensp_runtime.clear_cache()
        if self.pm is not None:
            self.pm.lab = new
        if self.agent is not None and hasattr(self.agent, "on_env_switch"):
            self.agent.on_env_switch(new_desc)
        if self.on_install is not None:
            self.on_install(new)
        # 4) 记库（下次启动优先用它）
        await asyncio.to_thread(db.set_setting, SETTING_ENV, env.key)
        if env.sandboxes:
            await asyncio.to_thread(db.set_setting, SETTING_SANDBOX.format(env.key),
                                    "" if sandbox is None else str(sandbox))
        log.info("模拟环境已切换：%s -> %s", old_label, new_desc)
        return stopped

    # ---- 按题目准备环境（课程页用） ----

    def job_view(self) -> dict[str, Any] | None:
        j = self.job
        if j is None:
            return None
        view = {**{k: v for k, v in j.items() if not k.startswith("_")},
                "elapsed_s": round((j.get("ended") or time.time()) - j["started"], 1)}
        view["progress"], view["estimate"] = self._progress(j)
        return view

    def _progress(self, j: dict[str, Any]) -> tuple[float, dict[str, Any] | None]:
        """整体进度（0~1）与剩余时间估计 {remaining_s, lo_s, hi_s, max_s}（没有可估的设备时为 None）。

        切换 / 查沙箱占前 8%，设备占 8%~98%：每台按「接口报运行中（经验用时）+ 就绪探测」两段计份额，用看门狗跟踪的本轮
        启动时刻推算；卡住重启的设备份额会回落（前端只增不减）。只是估计，真正的完成以 done 为准。"""
        stage = j.get("stage")
        if stage == "done":
            return 1.0, None
        if j.get("done"):                       # 出错结束：进度停在原处，不再给预计时间
            return self._progress({**j, "done": False})[0], None
        info = j.get("device_info") or {}
        if not info or not j.get("power"):
            base = {"queued": 0.0, "switch": 0.04, "sandbox": 0.06, "check": 0.5}.get(stage, 0.08)
            return base, None
        w = self.power
        now = w.clock()
        n = max(1, int(j.get("_n_start") or len(info)))
        scores, rems = [], []
        for name, d in info.items():
            boot = BOOT_S.get(d.get("kind") or "", BOOT_DEFAULT) + CROWD_S * (n - 1)
            probe = PROBE_S if w.probe_enabled and d.get("kind") in ("router", "switch") else 0.0
            total = boot + probe
            t = w.tracks.get((j.get("_keys") or {}).get(name, -1))
            st = d.get("status")
            if st == "online":
                scores.append(1.0)
                continue
            if st == "start_failed":
                scores.append(0.0)
                continue
            if (t is not None and t.phase == "initializing") or st == "initializing":
                e = now - t.init_at if t is not None and t.init_at else 0.0
                scores.append((boot + min(e, probe * 0.95)) / total)
                rems.append(max(probe - e, 5.0))
            elif t is not None and t.phase == "starting" and not t.restarting:
                e = now - t.since
                scores.append(min(e, boot * 0.95) / total)
                rems.append(max(boot - e, 10.0) + probe)
            elif t is not None and (t.phase == "queued" or t.restarting):
                scores.append(0.02)
                rems.append(total + max(0.0, t.not_before - now) + (w.restart_gap if t.restarting else 0.0))
            else:                                   # 还没发启动 / 状态未知
                scores.append(0.0)
                rems.append(total)
        prog = min(0.08 + 0.9 * sum(scores) / len(scores), 0.98)
        if not rems:
            return prog, None
        r = max(rems)
        left = j.get("_deadline")
        est = {"remaining_s": round(r), "lo_s": round(r * 0.8), "hi_s": round(r * 1.3),
               "max_s": round(max(0.0, left - time.time())) if left else None}
        return prog, est

    async def start_prepare(self, scenario_id: str, environment: str | None = None) -> dict[str, Any]:
        """开始「切到这道题的环境 / 沙箱并启动本题设备」的后台任务；同一时刻只跑一个。返回任务状态。

        课程页「开始练习」调用：条件是当前没有任何练习 / 考试进行中、目标环境在线；显式切换（switch）另有接口。
        environment：在哪个环境里做（课程页传正在浏览的环境；同一平台可能有几台 eNSP Pro，里程碑 6.19），平台不符 409；
        不给时当前环境平台相同就用当前环境，否则该平台注册顺序第一个。"""
        s = self.pm.scenarios.get(scenario_id)
        if s is None:
            raise LabError(404, f"场景不存在：{scenario_id}")
        if self.job is not None and not self.job["done"]:
            raise LabError(409, f"正在准备「{self.job['title']}」的环境，请稍候")
        busy = self.busy_reason()
        if busy:
            raise LabError(409, busy)
        if environment:
            try:
                want = lab_lib.get_env(environment)
            except ValueError as exc:
                raise LabError(422, str(exc)) from None
            if want.platform != scenario_lib.platform_of(s):
                raise LabError(409, f"「{s.get('title', scenario_id)}」是 "
                                    f"{scenario_lib.PLATFORMS.get(scenario_lib.platform_of(s), scenario_lib.platform_of(s))}"
                                    f" 的题目，不能在 {want.label} 里做")
        env = self.target_env(s, environment)
        if env is None:
            raise LabError(409, f"没有注册平台为 {s.get('platform')} 的模拟环境")
        over = self.capacity_reason(s, env.key)
        if over:
            raise LabError(409, over)
        if env.key != self.key:
            pr = await lab_lib.available(env.key)
            if not pr["available"]:
                raise LabError(409, f"本题需要 {env.label}，但它当前离线：{pr['reason']}")
        # 上面有 await：重新检查一次（两次点击并发时只跑一个任务）
        if self.job is not None and not self.job["done"]:
            raise LabError(409, f"正在准备「{self.job['title']}」的环境，请稍候")
        busy = self.busy_reason()
        if busy:
            raise LabError(409, busy)
        self._job_seq += 1
        # 步骤条：有沙箱的环境才有「查找沙箱」；不能启停设备的环境（eNSP 经典版）只「核对设备」；关掉就绪探测时没有「确认就绪」
        keys = (["switch"] + (["sandbox"] if env.sandboxes else []) +
                ((["start", "wait"] + (["probe"] if self.power.probe_enabled else [])) if env.power else ["check"]) +
                ["done"])
        self.job = {"id": self._job_seq, "scenario": scenario_id, "title": s.get("title", scenario_id),
                    "environment": env.key, "sandbox": None, "stage": "queued", "message": "排队中",
                    "devices": {}, "done": False, "ok": None, "error": "", "error_status": None, "started": time.time(), "ended": None,
                    "stopped_old": [], "stopped_extra": [], "stopped_other": [], "notes": {},
                    # 课程页准备浮层用（2026-10-07，spec §16.3）：环境名、沙箱名、步骤条、每台设备详情、自动重启上限
                    "env_label": env.label, "power": env.power,
                    "sandbox_name": s.get("topology") if env.sandboxes else None,
                    "steps": [{"key": k, "label": STEP_LABELS[k], "state": "pending"} for k in keys],
                    "device_info": {}, "retries": self._retries_of(env)}
        self._job_task = asyncio.create_task(self._prepare(self.job, s, env))
        return self.job_view()

    def _retries_of(self, env: lab_lib.LabEnv) -> int:
        """该环境的自动重启上限：看门狗正用着它的参数时取 PowerWatch 的值，否则取该环境配置的值（切换前就要显示）。"""
        if not env.power:
            return 0
        if env.watch and self.power.configured_for != env.key:
            return int(lab_lib.watch_value(env.key, "start_retries"))
        return int(self.power.retries)

    def _stage(self, job: dict[str, Any], stage: str, message: str, *, step: str | None = None) -> None:
        job["stage"], job["message"] = stage, message
        self._step(job, step or STEP_OF.get(stage), failed=stage == "error")
        log.info("准备环境 #%s：%s %s", job["id"], stage, message)

    @staticmethod
    def _step(job: dict[str, Any], key: str | None, *, failed: bool = False) -> None:
        """步骤条推进：进入 key 时，前面进行中的标 done；前面没进入过的标 skipped（已在目标环境 / 设备已在运行），
        但启动过设备时没单独停留的「等待运行 / 确认就绪」算 done（设备一轮询就已到下一状态）。出错时当前步骤标 error。"""
        steps = job.get("steps")
        if not steps:
            return
        if failed:
            for x in steps:
                if x["state"] == "active":
                    x["state"] = "error"
            return
        keys = [x["key"] for x in steps]
        if key not in keys:
            return
        started = any(x["key"] == "start" and x["state"] in ("active", "done") for x in steps)
        for x in steps[:keys.index(key)]:
            if x["state"] == "active":
                x["state"] = "done"
            elif x["state"] == "pending":
                x["state"] = "done" if started and x["key"] in ("wait", "probe") else "skipped"
        cur = steps[keys.index(key)]
        cur["state"] = "done" if key == "done" else "active"

    async def _prepare(self, job: dict[str, Any], s: dict[str, Any], env: lab_lib.LabEnv) -> None:
        try:
            sandbox = None
            if env.key != self.key:
                self._stage(job, "switch", f"切换模拟环境到 {env.label}")
                self._note_stopped(job, await self.switch(env.key, _job=True))
            if env.sandboxes:
                self._stage(job, "sandbox", f"查找沙箱 {s['topology']}")
                rows = (await self.sandboxes())["sandboxes"]
                want = _norm(s["topology"])
                hits = [r for r in rows if _norm(r["name"]) == want]
                if not hits:
                    raise LabError(404, f"{env.label} 中没有名为 {s['topology']} 的沙箱（题目 topology 应写沙箱名）")
                if len(hits) > 1:
                    raise LabError(409, f"{env.label} 中有 {len(hits)} 个同名沙箱 {s['topology']}："
                                        f"{'、'.join(str(h['id']) for h in hits)}，无法自动选择")
                sandbox = job["sandbox"] = hits[0]["id"]
                if sandbox != self.sandbox:
                    self._stage(job, "switch", f"切换到沙箱 {s['topology']}（id {sandbox}）", step="sandbox")
                    self._note_stopped(job, await self.switch(env.key, sandbox, sandbox_given=True, _job=True),
                                       step="sandbox")
            await self._start_devices(job, s)
            job["ok"] = True
            self._stage(job, "done", "环境已就绪" + ("" if env.key == "ensp" else
                                                    "（设备刚启动时 console 可能还要几分钟才响应）"))
        except LabError as exc:
            job["ok"], job["error"], job["error_status"] = False, exc.detail, exc.status
            self._stage(job, "error", exc.detail)
        except Exception as exc:              # 不让后台任务静默死掉
            log.exception("准备环境失败")
            job["ok"], job["error"], job["error_status"] = False, f"{type(exc).__name__}: {exc}", 500
            self._stage(job, "error", job["error"])
        finally:
            job["done"], job["ended"] = True, time.time()

    def _note_stopped(self, job: dict[str, Any], res: dict[str, Any], *, step: str | None = None) -> None:
        """切换时停掉的旧沙箱设备记进任务（接口返回里可见）。"""
        st = (res or {}).get("stopped_old")
        if st:
            job["stopped_old"].append(st)
            if st.get("devices"):
                self._stage(job, "switch", f"已停止旧沙箱 {st.get('sandbox_name')} 的设备：{'、'.join(st['devices'])}",
                            step=step)

    def _prepare_deadline(self, n: int) -> float:
        """等设备就绪的上限：错开启动 + 每轮（启动超时 + 停止再启动约 60 秒）×（重试次数 + 1）+ 就绪探测 + 余量。"""
        if self.prepare_timeout is not None:
            return self.prepare_timeout
        w = self.power
        rounds = (w.start_timeout + 60.0) * (w.retries + 1)
        return w.stagger * max(0, n - 1) + rounds + (w.probe_timeout if w.probe_enabled else 0.0) + 60.0

    async def _start_devices(self, job: dict[str, Any], s: dict[str, Any]) -> None:
        """启动本题需要的设备并等到真正就绪（online）。启动经 PowerWatch：错开发出、看门狗（卡住自动停止再启动、仍失败标
        start_failed）、就绪探测（initializing → online，阶段 probe）。"""
        req = list(s["required_devices"])
        loop = asyncio.get_running_loop()
        started = False
        gen0 = self.gen
        if self.env.power:
            job["retries"] = int(self.power.retries)   # 跨环境准备时切换后才换成该环境的参数
        limit = self._prepare_deadline(len(req))
        end = loop.time() + limit
        job["_deadline"] = time.time() + limit
        if not self.env.power:
            self._stage(job, "check", f"核对 {self.env.label} 中的拓扑与设备：{'、'.join(req)}")
        while True:
            if self.gen != gen0:              # 等待期间环境被切走了（不应发生：任务进行中拒绝显式切换）
                raise LabError(409, "等待设备期间模拟环境被切换，已停止准备")
            p = self.provider
            cur = await p.resolve()
            self.note_topology(cur, self.key)
            t = cur.get("topology")
            if cur.get("state") not in ("running",) or t is None:
                if p.power:
                    raise LabError(409, cur.get("message") or "读取拓扑失败")
                raise LabError(409, f"{cur.get('message') or '没有运行的拓扑'}；请在 {self.env.label} 中打开本题拓扑并启动"
                                    f" {'、'.join(req)}")
            by = {d["name"]: d for d in t["devices"]}
            missing = [n for n in req if n not in by or console_of(by[n]) <= 0]
            if missing:
                raise LabError(409, f"拓扑 {t['name']} 中缺少本题设备：{'、'.join(missing)}")
            st = {n: by[n].get("status") for n in req}
            job["devices"] = st
            prog = cur.get("progress") or {}
            job["notes"] = {n: prog[by[n]["id"]] for n in req if by[n]["id"] in prog}
            job["_keys"] = {n: console_of(by[n]) for n in req}
            job["device_info"] = self._device_info(job, by, st)
            offline = [n for n in req if st[n] != "online"]
            if not offline:
                return
            if not p.power:
                raise LabError(409, f"请在 {self.env.label} 中启动 {'、'.join(offline)}（ainet 不能启停该环境的设备）")
            self.touch("prepare")
            if not started:
                await self._stop_extra(job, t, req)
                todo = [n for n in offline if st[n] in ("stopped", "error", "unknown", "start_failed")]
                if todo:
                    job["_n_start"] = len(todo)
                    self._stage(job, "start", f"启动 {'、'.join(todo)}" + (
                        f"（每 {self.power.stagger:g} 秒启动一台）" if len(todo) > 1 and self.power.stagger > 0 else ""))
                    await self.power.start([console_of(by[n]) for n in todo], source="prepare")
                # 别处发起、正在启动的设备也交给看门狗
                self.power.adopt([console_of(by[n]) for n in offline if n not in todo and st[n] == "starting"],
                                 {console_of(by[n]): n for n in offline})
                started = True
                await asyncio.sleep(self.prepare_poll)
                continue
            failed = [n for n in offline if st[n] == "start_failed"]
            if failed:
                errs = cur.get("start_errors") or {}
                raise LabError(504, "；".join(f"{n} 启动失败：{errs.get(by[n]['id'], '未知原因')}" for n in failed))
            tracks = self.power.tracks
            restarting = [n for n in offline if getattr(tracks.get(console_of(by[n])), "restarting", False)]
            if restarting:
                nth = max(getattr(tracks.get(console_of(by[n])), "restarts", 0) for n in restarting)
                self._stage(job, "restart", f"{'、'.join(restarting)} 启动卡住，正在自动停止后重新启动"
                                            f"（第 {nth} 次，最多 {self.power.retries} 次）")
            elif all(st[n] in ("initializing", "online") for n in req):
                self._stage(job, "probe", "确认设备系统初始化完成（不再报 busy）：" +
                            "、".join(f"{n}（{job['notes'].get(n) or st[n]}）" for n in offline))
            else:
                pending = "、".join(f"{n}（{st[n]}）" for n in offline)
                self._stage(job, "wait", f"等待设备运行：{pending}")
            if loop.time() >= end:
                raise LabError(504, f"等待超时，设备没有全部就绪：{'、'.join(f'{n}（{st[n]}）' for n in offline)}")
            await asyncio.sleep(self.prepare_poll)

    # 占着资源的状态：这些状态的非本题设备算「在运行」（同时运行设备上限，里程碑 6.17）
    ACTIVE_STATUSES = ("online", "starting", "initializing", "error", "start_failed")

    async def _stop_extra(self, job: dict[str, Any], t: dict[str, Any], req: list[str]) -> None:
        """同时运行设备上限（里程碑 6.17）：要启动本题设备之前，按**整个 eNSP Pro** 计数——本题的非 PC 设备 + 本沙箱里在运行的
        非本题非 PC 设备 + 其他沙箱里在运行的非 PC 设备（provider.running_elsewhere，getByPage 一次取全）。超过上限时先停掉
        全部这些非本题设备（其他沙箱的、本沙箱非本题的；PC 很轻，不停也不计），再启动本题设备；准备浮层的阶段文字写明停了哪些。
        起因（真机）：服务刚启动时没有「上一个沙箱」记录，切换时的 stop_old 不会触发，别的沙箱留着 4 台设备在跑，T1 / T2
        资源不足一直卡在启动。上限为 0（不限）时不做，仍只按 stop_old 规则。停不掉不中断准备（照常启动，只是可能更慢 / 卡住）。"""
        limit = lab_lib.max_devices(self.key)
        if limit <= 0 or not self.env.power:
            return
        skip = scenario_lib.UNCOUNTED_KINDS
        mine = [d for d in t["devices"] if d["name"] in req and d.get("kind") not in skip]
        others = [d for d in t["devices"] if d["name"] not in req and d.get("kind") not in skip
                  and d.get("status") in self.ACTIVE_STATUSES and console_of(d) > 0]
        p = self.provider
        elsewhere: list[dict[str, Any]] = []
        if hasattr(p, "running_elsewhere"):
            try:
                elsewhere = await p.running_elsewhere()
            except LabError as exc:
                log.warning("读取其他沙箱的运行设备失败（只按本沙箱计数）：%s", exc.detail)
        total = len(mine) + len(others) + len(elsewhere)
        if (not others and not elsewhere) or total <= limit:
            return
        groups: dict[int, dict[str, Any]] = {}
        for d in elsewhere:
            g = groups.setdefault(d["sandbox"], {"sandbox": d["sandbox"], "sandbox_name": d["sandbox_name"],
                                                 "ids": [], "devices": []})
            g["ids"].append(d["id"])
            g["devices"].append(d["name"])
        parts = [f"沙箱 {g['sandbox_name']} 的 {'、'.join(g['devices'])}" for g in groups.values()]
        if others:
            parts.append(f"本沙箱非本题设备 {'、'.join(d['name'] for d in others)}")
        self._stage(job, "start", f"同时运行设备上限 {limit} 台（现在共 {total} 台）：先停止 {'；'.join(parts)}")
        job["stopped_other"] = []
        for g in groups.values():
            try:
                await p.stop_in_sandbox(g["sandbox"], g["ids"])
            except LabError as exc:
                log.warning("停止沙箱 %s 的设备 %s 失败：%s", g["sandbox_name"], "、".join(g["devices"]), exc.detail)
                continue
            self.power.note_stopped(g["sandbox"], g["ids"])      # 切回那个沙箱时同样遵守停机间隔
            job["stopped_other"].append({"sandbox": g["sandbox"], "sandbox_name": g["sandbox_name"],
                                         "devices": g["devices"]})
            log.info("准备环境 #%s：同时运行设备上限 %s 台，已停止沙箱 %s 的设备 %s", job["id"], limit,
                     g["sandbox_name"], "、".join(g["devices"]))
        if not others:
            return
        names = [d["name"] for d in others]
        keys = [console_of(d) for d in others]
        try:
            await self.power.stop(keys)
        except LabError as exc:
            log.warning("停止非本题设备 %s 失败：%s", "、".join(names), exc.detail)
            return
        if self.hub is not None and hasattr(self.hub, "disconnect"):
            for k in keys:
                await self.hub.disconnect(k, "\r\n[设备正在停止（同时运行设备上限），终端已断开]\r\n")
        job["stopped_extra"] = names
        log.info("准备环境 #%s：同时运行设备上限 %s 台，已停止非本题设备 %s", job["id"], limit, "、".join(names))

    def _device_info(self, job: dict[str, Any], by: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        """每台设备的详情（浮层的设备卡片）：显示状态（看门狗重启中的另报 restarting，接口状态里它显示为 starting）、
        进度文字、类型、已自动重启次数、看门狗阶段、本轮已用秒数。"""
        tracks, now = self.power.tracks, self.power.clock()
        out = {}
        for n, status in st.items():
            t = tracks.get(console_of(by[n]))
            if t is not None and t.sandbox != self.sandbox:
                t = None
            out[n] = {"status": status, "note": job["notes"].get(n) or "", "kind": by[n].get("kind") or "",
                      "restarting": bool(t and t.restarting), "restarts": t.restarts if t else 0,
                      "phase": t.phase if t else None, "round_s": round(now - t.since, 1) if t else None}
        return out

    async def close(self) -> None:
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()
        await self.power.close()
        await self.idle.close()
        if self.provider is not None:
            await self.provider.close()
