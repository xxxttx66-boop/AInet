"""练习流程（spec §7 闯关模式的第一版）：注入故障 → 学员排障 → 自动判定通关 → 结束并还原。

- 同一时间只允许一个进行中的练习（系统单人使用，spec §2）。
- 注入、判定、还原都走 ConsoleHub 的隐藏事务，学员终端看不到这些命令。
- 自动判定：练习中学员每次回车提交命令后，静默 debounce 秒（默认 3）没有新输入就跑一遍 checks；
  两次判定至少间隔 min_interval 秒（默认 10，开始时的故障确认也算一次）；设备暂不可检查（学员正在
  输入、停在分页 / [Y/N] 等）时跳过，稍后重试。全部通过即通关。
- 结束时两步还原：先执行场景 restore（撤销场景自己的改动），再做整机复位（config_reset，spec §10 / 里程碑 4）：
  开练前在 baseline、inject 之前给每台 required 设备拍 display current-configuration 快照（存 db 的
  attempt_configs），结束时按快照差异回滚，学员额外改的配置也一并撤销，最后比对确认一致。复位后仍不一致的
  配置行原样列出，提示在 eNSP 中重载拓扑（spec §10 兜底），不当作成功。
- 进行中的练习写入 app_settings（practice_active），快照在 db 里，服务重启后可恢复，仍能「结束并还原」。
- 参数化场景（里程碑 6）：开始时生成种子（或由调用方 / AINET_PRACTICE_SEED 指定），按种子展开场景，种子与取值
  记入 attempts；之后注入、判定、提示、评分、重启恢复都用展开后的场景。
- 考试（里程碑 6，exam.py）复用这里的一题底层能力（mode="exam"）：同一个 active 槽位、快照、注入、隐藏事务、
  命令采集、复位。考试题默认不自动判定（auto_judge=False），由学员「提交本题」（submit）验收；练习接口
  （check / hint / end）对考试题一律拒绝，避免绕过重试次数与提示设置。
- 终端主机（场景 hosts，eNSP Pro 的 PC）：命令行只能 ping，地址由实验后端接口配置。快照 = 地址配置（HOST_SNAPSHOT
  前缀 + JSON），baseline / inject / restore 里主机的命令（ip address / ip gateway / undo …）翻译成 lab.host_set，
  复位 = 把地址配置设回快照；判定照常走 ConsoleHub 的隐藏事务（PC> 提示符下 ping）。
- 干净基线（spec §10.4，config.LAB_BASELINE_AUTO）：YunShan 设备自动保存、停机必保存，上一次没撤销干净的改动会留在
  设备上。每台设备在 app_settings 里记一份基线（键 baseline:<后端>:<拓扑>:<设备>）；每题开始前先比对，不一致就先复位到
  基线（结果记在 state 的 pre_reset），复位不干净则拒绝开始；没有基线时把这次读到的配置记为基线。
- 结束时本题设备已关机（能启停的实验后端，lab.power；如 eNSP Pro 里学员停了设备）：这些设备跳过 restore 与整机复位
  （不去连 console，否则登录超时、练习永远结束不了），其余设备照常复位，练习正常结束，reset_ok=False 并在 note 里说明。
  读不到电源状态时按原流程尝试复位。eNSP 经典版（power=False）不判断。
- 结束在后台复位（begin_end）：接口立即返回（放弃 / 通关、ended_at 已落库，phase=ending），复位放进后台任务；
  复位前冻结本题终端（学员输入丢弃）、强制收回终端（hub.reclaim：分页 / [Y/N] / 系统视图 / 半行输入都不再 busy）。
  没复位成功的设备（连不上等）隔几秒重试几轮，仍不行就按结束处理（reset_ok=False，note 列出设备）。复位完成后
  reset_json 落库、调度评分、last_result 更新、释放槽位。后台复位期间 active 仍占着槽位：开始新练习 / 考试、切换环境
  一律 409（RESETTING_BUSY）。考试的题间切换用 end()（同样后台任务，但等它完成）。服务重启后 ending 状态由
  resume_ending 接着复位。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import config
import db
import scenarios as sc
import config_reset as cr
from config_reset import ConfigReset, ResetError, ResetResult
from console_hub import HiddenResult
from lab import LabError, console_of

RESTORE_NOTE = "设备已复位到开练前的配置（场景的改动和你额外做的配置更改都已撤销）。"
NO_SNAPSHOT_NOTE = "未找到开练前的配置快照，只还原了场景自身的改动；你额外做的配置更改未还原（需要时请手动撤销）。"
RELOAD_NOTE = ("以下配置无法自动撤销，设备与开练前不一致。请在 eNSP 中重载拓扑（停止并重新启动这些设备）"
               "后再开始下一次练习：")
EXCLUDED_NOTE = "以下配置不在自动复位范围内（用户界面 / 本地用户），未处理："
POWERED_OFF_NOTE = ("以下设备已关机，未还原、未复位（练习中的改动可能已随停机保存留在设备上）：{devices}。"
                    "下一题开始前需先启动这些设备；开始时会先与干净基线比对，不一致先复位到基线。")
RESETTING_NOTE = "设备正在后台复位到开练前的配置（撤销场景改动和你额外做的更改），完成前终端不接受输入。"
RESET_FAILED_NOTE = "以下设备重试几次后仍没能复位，练习已按结束处理：{items}。"
BASELINE_HINT = "设备上可能还留有本题的改动；下一题开始前会先与干净基线比对，不一致自动复位到基线。"
MANUAL_HINT = "设备上可能还留有本题的改动：请确认设备已启动、console 能连上，必要时在模拟器中重载拓扑（停止并重新启动这些设备）后再开始下一次练习。"
RESETTING_BUSY = "上一次练习正在后台复位设备，请稍候（通常几十秒）再{what}"
log = logging.getLogger(__name__)
_SETTING = "practice_active"
HOST_SNAPSHOT = "!ainet-host "           # 终端主机快照的前缀（其后是地址配置 JSON）
BASELINE_LABS = {"enspro"}               # 需要干净基线的实验后端（YunShan 自动保存配置）


def host_snapshot(cfg: dict[str, str]) -> str:
    """终端主机快照：ip / mask / gateway；实验后端读得到 DNS 时（cfg 里有 dns 键）连 dns 一起记。"""
    keys = ("ip", "mask", "gateway") + (("dns",) if "dns" in cfg else ())
    return HOST_SNAPSHOT + json.dumps({k: cfg.get(k, "") for k in keys}, sort_keys=True)


def parse_host_snapshot(text: str) -> dict[str, str] | None:
    if not text.startswith(HOST_SNAPSHOT):
        return None
    try:
        d = json.loads(text[len(HOST_SNAPSHOT):])
    except ValueError:
        return None
    out = {k: str(d.get(k, "")) for k in ("ip", "mask", "gateway")}
    if "dns" in d:
        out["dns"] = str(d.get("dns") or "")
    return out


def host_command(dev: str, cfg: dict[str, str]) -> str:
    """PC 设置气泡的改动写进操作记录的伪命令（命令采集 / 回放 / 评分可见）。"""
    return (f"[PC 配置] {dev} ip={cfg.get('ip') or '-'} mask={cfg.get('mask') or '-'} "
            f"gw={cfg.get('gateway') or '-'} dns={cfg.get('dns') or '-'}")


def _mask_len(m: str) -> str:
    """掩码规范成前缀长度字符串（比较用）：255.255.255.0 / 24 -> 24。"""
    m = (m or "").strip()
    if m.isdigit():
        return m
    try:
        return str(sum(bin(int(x)).count("1") for x in m.split(".")))
    except ValueError:
        return m


def same_host(a: dict[str, str], b: dict[str, str]) -> bool:
    """地址配置是否一致。dns 只在两边都有时比较（旧快照 / 读不到 DNS 的实验后端没有这个键）。"""
    if "dns" in a and "dns" in b and (a.get("dns") or "") != (b.get("dns") or ""):
        return False
    return (a.get("ip", ""), _mask_len(a.get("mask", "")) if a.get("ip") else "", a.get("gateway", "")) == \
        (b.get("ip", ""), _mask_len(b.get("mask", "")) if b.get("ip") else "", b.get("gateway", ""))


def baseline_key(lab_kind: str, topo: str, device: str) -> str:
    return f"baseline:{lab_kind}:{topo}:{device}"


class PracticeError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def new_seed() -> int:
    """练习 / 考试随机种子（0 保留给「默认值」）。"""
    return secrets.randbelow(2**31 - 1) + 1


_NAV_CMD = re.compile(r"^(system-view|return|quit|commit|interface\s+\S+)$", re.I)


def secret_strings(s: dict[str, Any]) -> list[str]:
    """考试进行中不能出现在任何接口返回里的内容：注入 / 还原 / 修复 / baseline 的配置命令、判定命令与正则。"""
    out: set[str] = set()
    for f in ("inject", "restore", "solution", "baseline"):
        for cmds in (s.get(f) or {}).values():
            for c in cmds:
                if c.strip() and not _NAV_CMD.match(c.strip()):
                    out.add(c.strip())
    for c in s.get("checks") or []:
        out.add(c["command"].strip())
        out.add(c["expect"])
    return sorted((x for x in out if len(x) >= 4), key=len, reverse=True)


def mask(text: str, s: dict[str, Any] | None) -> str:
    """把错误信息里的场景命令换成「（已隐藏）」（考试中注入 / 复位失败的原因会带出命令原文）。

    设备回显 / 复位差异里的写法和场景原文不一定一样（如 ip address 10.0.95.2 255.255.255.0、vlan batch 10 422），
    所以另外按整段匹配隐藏：只出现在故障 / 修复配置里的 IP（工单里没有的），以及工单里没有出现的参数取值（≥ 2 位）。
    """
    if not text or s is None:
        return text
    for x in secret_strings(s):
        text = text.replace(x, "（已隐藏）")
    import hints as _hints
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    tokens = {t for t in _hints.banned_terms(s) if ip_re.fullmatch(t)}
    symptom = s.get("symptom", "")
    for v in (s.get("param_values") or {}).values():
        v = str(v)
        if len(v) >= 2 and not re.search(rf"(?<![\w.]){re.escape(v)}(?![\w.]*\w)", symptom):
            tokens.add(v)
    for t in sorted(tokens, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.]){re.escape(t)}(?![\w]|\.\d)", "（已隐藏）", text)
    return text


@dataclass
class Active:
    attempt_id: int
    scenario: dict[str, Any]
    student_id: int
    started_at: str
    ports: dict[str, int]                 # 设备名 -> console 端口（required 设备）
    allowed_ports: set[int]               # 学员可开终端的端口（accessible 设备）
    phase: str = "starting"               # starting / running / passed / ending（已结束，后台复位中）
    hints_used: int = 0
    commands: int = 0
    passed_at: str | None = None
    ended_at: str | None = None           # 放弃时的结束时间（通关时 = passed_at）
    last_check: dict[str, Any] | None = None
    last_check_t: float = 0.0             # monotonic
    checks_run: int = 0
    judging: bool = False
    rejudge: bool = False
    debounce: asyncio.Task | None = None
    restore_error: str = ""
    port_dev: dict[int, str] = field(default_factory=dict)
    snapshots: dict[str, str] = field(default_factory=dict)   # 设备名 -> 开练前的整机配置
    closing: bool = False                 # 结束流程已开始：之后的设备输出（还原 / 复位事务的余波）不再记录
    last_log: dict[int, int] = field(default_factory=dict)   # 端口 -> 该设备最近一条命令记录的行 id
    hint_cache: dict[int, dict[str, Any]] = field(default_factory=dict)   # 级别 -> {text, source}
    hint_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 里程碑 6：参数化与考试
    seed: int = 0
    mode: str = "practice"                # practice / exam
    exam_id: int | None = None
    auto_judge: bool = True               # 考试默认关闭：只在学员「提交本题」时判定
    max_submissions: int = 0              # 考试：本题最多提交几次（1 + 允许重试次数）；0 = 不限（练习）
    submissions: list[dict[str, Any]] = field(default_factory=list)
    hosts: set[str] = field(default_factory=set)              # 终端主机（场景 hosts）
    pre_reset: dict[str, dict[str, Any]] = field(default_factory=dict)   # 开始前与干净基线的比对 / 复位
    end_view: dict[str, Any] | None = None   # 结束时立即返回的结果（resetting=True，复位结果另行补上）

    @property
    def locked(self) -> bool:
        """考试题已锁定：已通过，或提交次数用完（不能再提交）。"""
        return self.passed_at is not None or (self.max_submissions > 0 and
                                              len(self.submissions) >= self.max_submissions)

    def __post_init__(self) -> None:
        self.port_dev = {p: d for d, p in self.ports.items()}


class PracticeManager:
    def __init__(self, hub: Any, scenario_map: dict[str, dict[str, Any]], scenario_ids: dict[str, int],
                 resolve: Callable[[], Awaitable[dict[str, Any]]], *, debounce: float = 3.0,
                 min_interval: float = 10.0, busy_retry: float = 2.0, op_deadline: float = 10.0,
                 end_retries: int = 3, end_retry_wait: float = 5.0) -> None:
        self.hub = hub
        self.scenarios = scenario_map
        self.scenario_ids = scenario_ids
        self.resolve = resolve
        self.debounce_s = debounce
        self.min_interval = min_interval
        self.busy_retry = busy_retry
        self.op_deadline = op_deadline
        self.end_retries = end_retries     # 结束复位：没复位成功的设备最多做几轮
        self.end_retry_wait = end_retry_wait
        self.active: Active | None = None
        self.last_result: dict[str, Any] | None = None
        self._op = asyncio.Lock()          # start / end 串行
        self._check = asyncio.Lock()       # 判定轮次串行
        self._db_tail: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None   # 后台复位任务（begin_end 创建）
        self._ending: Active | None = None           # _end_task 复位的是哪一题
        self.scorer: Any = None            # grading.Scorer（main 注入）；None = 不评分
        self.hinter: Any = None            # hints.Hinter（main 注入）；None = 只用静态提示
        self.lab: Any = None               # 实验后端（main 注入）：终端主机的地址配置、干净基线的键
        self.capacity_guard: Callable[[dict[str, Any]], str | None] | None = None
        self.baseline_auto = config.LAB_BASELINE_AUTO
        hub.add_listener(self.on_hub_event)

    # ---- 场景列表 ----

    async def list(self, platform: str | None = None) -> list[dict[str, Any]]:
        """场景列表（含就绪状态）。platform 给出时只列该设备平台的题（模拟环境切换，spec §5.2）。"""
        cur = await self.resolve()
        out = []
        for s in self.scenarios.values():
            if platform is not None and s.get("platform", "vrp5") != platform:
                continue
            rd = sc.readiness(s, cur)
            # 参数化场景按默认值（种子 0）展示；实际开始时另取种子
            out.append({**sc.public(sc.expand(s, 0)), "ready": rd["ready"], "reason": rd["reason"],
                        "parametrized": bool(s.get("params"))})
        return out

    def require(self, mode: str) -> Active:
        """练习接口只能操作练习，考试接口只能操作考试题。"""
        a = self.active
        if a is None:
            raise PracticeError(409, "没有进行中的练习" if mode == "practice" else "没有进行中的考试题")
        if a.mode != mode:
            raise PracticeError(409, "考试进行中，请在「考试」页操作" if a.mode == "exam"
                                else "有进行中的练习，请先结束练习")
        return a

    def allowed_filter(self, ports: set[int]) -> set[int]:
        """练习期间终端白名单只放行可访问设备。"""
        a = self.active
        return ports & a.allowed_ports if a is not None else ports

    # ---- 隐藏事务封装 ----

    def _resetter(self) -> ConfigReset:
        # 每次按当前的 op_deadline / busy_retry 新建（测试会在构造后调小）
        return ConfigReset(self.hub, deadline=self.op_deadline, busy_retry=self.busy_retry)

    async def _reset(self, a: Active, devices: list[str] | None = None) -> dict[str, ResetResult]:
        """整机复位到开练前快照；没有快照的设备跳过。返回 {设备: 结果}。"""
        out: dict[str, ResetResult] = {}
        # 缺省 = 本题设备 + 练习中经 PC 设置气泡改过、另拍了快照的终端主机
        default = list(dict.fromkeys([*a.scenario["required_devices"], *a.snapshots]))
        for dev in (devices if devices is not None else default):
            snap = a.snapshots.get(dev)
            if snap is None:
                continue
            out[dev] = await self._restore_device(a.ports[dev], snap)
        return out

    async def _restore_device(self, port: int, snap: str) -> ResetResult:
        """复位一台设备到 snap：网络设备走 config_reset；终端主机把地址配置设回快照。"""
        host = parse_host_snapshot(snap)
        if host is None:
            return await self._resetter().restore_to(port, snap)
        try:
            cur = await self.lab.host_get(port)
            if same_host(cur, host):
                return ResetResult("ok", rounds=0)
            got = await self.lab.host_set(port, host)
        except (LabError, AttributeError) as exc:
            return ResetResult("error", f"设置终端主机地址失败：{getattr(exc, 'detail', exc)}")
        if same_host(got, host):
            return ResetResult("ok", rounds=1, commands=1)
        return ResetResult("dirty", rounds=1, commands=1,
                           leftovers=[f"地址配置：当前 {got}，应为 {host}"])

    async def _snapshot(self, a: Active, dev: str) -> str:
        """开练快照：网络设备为 display current-configuration；终端主机为地址配置。失败抛 ResetError。"""
        if dev in a.hosts:
            try:
                return host_snapshot(await self.lab.host_get(a.ports[dev]))
            except (LabError, AttributeError) as exc:
                raise ResetError("error", f"读取终端主机地址失败：{getattr(exc, 'detail', exc)}") from None
        return await self._resetter().snapshot(a.ports[dev])

    async def _apply(self, a: Active, dev: str, cmds: list[str], tolerate: bool = False) -> HiddenResult:
        """执行场景的配置命令：网络设备走 hub 的隐藏配置事务；终端主机翻译成实验后端的接口调用。"""
        if dev not in a.hosts:
            return await self._config(a.ports[dev], cmds, tolerate)
        try:
            cur = await self.lab.host_get(a.ports[dev])
            want = sc.apply_host_commands(cur, cmds)
            got = await self.lab.host_set(a.ports[dev], want)
        except ValueError as exc:
            return HiddenResult("error", str(exc))
        except (LabError, AttributeError) as exc:
            return HiddenResult("error", f"设置终端主机地址失败：{getattr(exc, 'detail', exc)}")
        if not same_host(got, want):
            return HiddenResult("error", f"终端主机地址没有生效：当前 {got}，应为 {want}")
        return HiddenResult("ok")

    # ---- 干净基线 ----

    def _baseline_key(self, cur: dict[str, Any], dev: str) -> str:
        lab_kind = (cur.get("lab") or {}).get("kind") or getattr(self.lab, "kind", "ensp")
        # 课程页添加的 eNSP Pro 实例（里程碑 6.19）按实例 key 区分基线（不同主机上同名沙箱的配置可能不同）；
        # 内置 enspro 的 env_key 就是 enspro，键与以前完全一样
        if lab_kind == getattr(self.lab, "kind", None) and getattr(self.lab, "env_key", None):
            lab_kind = self.lab.env_key
        return baseline_key(lab_kind, (cur.get("topology") or {}).get("name", ""), dev)

    async def _check_baseline(self, a: Active, cur: dict[str, Any], dev: str) -> str:
        """开练前：读当前配置，与干净基线比对；不一致先复位到基线。返回这次读到的（复位后的）配置，供开练快照用。

        没有基线时（且 baseline_auto）把读到的配置记为基线。复位不干净抛 PracticeError（拒绝开始，避免上一次留下的
        改动混进本题）。"""
        text = await self._snapshot(a, dev)
        if (cur.get("lab") or {}).get("kind") not in BASELINE_LABS:
            return text                    # eNSP 经典版的 VRP5 不自动保存：不需要基线
        key = self._baseline_key(cur, dev)
        raw = await asyncio.to_thread(db.get_setting, key)
        # ainet_test 以前以沙箱原始节点名 T1/T2 保存过干净基线；改为 AInet 展示别名后，
        # 首次开练时先迁移旧键，避免把已被污染的配置误当成新基线。
        topo = cur.get("topology") or {}
        native = {"AR1": "T1", "AR2": "T2"}.get(dev) \
            if (cur.get("lab") or {}).get("kind") == "enspro" and str(topo.get("name", "")).casefold() == "ainet_test" else None
        if raw is None and native:
            legacy_key = f"{key.rsplit(':', 1)[0]}:{native}"
            raw = await asyncio.to_thread(db.get_setting, legacy_key)
            if raw is not None:
                await asyncio.to_thread(db.set_setting, key, raw)
        if raw is None:
            if self.baseline_auto:
                await asyncio.to_thread(db.set_setting, key, json.dumps({"config": text, "at": _now_iso()},
                                                                       ensure_ascii=False))
                a.pre_reset[dev] = {"status": "captured"}
            return text
        golden = json.loads(raw)["config"]
        gh, th = parse_host_snapshot(golden), parse_host_snapshot(text)
        if gh is not None or th is not None:
            diffs = [] if (gh is not None and th is not None and same_host(gh, th)) else [f"地址配置：{th} → {gh}"]
        else:
            diffs = cr.differences(cr.parse(golden), cr.parse(text))
        if not diffs:
            a.pre_reset[dev] = {"status": "clean"}
            return text
        res = await self._restore_device(a.ports[dev], golden)
        a.pre_reset[dev] = {"status": "reset" if res.ok else res.status, "differences": diffs[:30],
                            "rounds": res.rounds, "commands": res.commands}
        if not res.ok:
            left = "；".join(res.leftovers[:10]) or res.reason
            raise PracticeError(409, f"{dev} 与干净基线不一致，自动复位没有成功（{left}）。"
                                     "请停止并重新启动该设备后重试，或用 POST /api/lab/baseline 重新记录基线")
        return await self._snapshot(a, dev)

    async def baselines(self) -> dict[str, Any]:
        """当前拓扑各设备的干净基线状态（GET /api/lab/baseline）。"""
        cur = await self.resolve()
        t = cur.get("topology") or {}
        out = []
        for d in t.get("devices", []):
            if console_of(d) <= 0:
                continue
            raw = await asyncio.to_thread(db.get_setting, self._baseline_key(cur, d["name"]))
            info = json.loads(raw) if raw else None
            out.append({"device": d["name"], "has_baseline": info is not None,
                        "at": info.get("at") if info else None})
        return {"topology": t.get("name"), "auto": self.baseline_auto, "devices": out}

    async def capture_baselines(self, devices: list[str] | None = None) -> dict[str, Any]:
        """把设备的当前配置记为干净基线（POST /api/lab/baseline）。练习 / 考试进行中拒绝（配置不是干净的）。"""
        async with self._op:
            if self.resetting:
                raise PracticeError(409, RESETTING_BUSY.format(what="记录基线"))
            if self.active is not None:
                raise PracticeError(409, "练习 / 考试进行中，设备配置不是干净状态，不能记录基线")
            cur = await self.resolve()
            t = cur.get("topology") or {}
            by_name = {d["name"]: d for d in t.get("devices", []) if console_of(d) > 0}
            names = list(devices) if devices else [n for n, d in by_name.items() if d.get("status") == "online"]
            bad = [n for n in names if n not in by_name or by_name[n].get("status", "online") != "online"]
            if bad:
                raise PracticeError(409, f"这些设备不在当前拓扑或没有运行：{'、'.join(bad)}")
            tmp = Active(attempt_id=0, scenario={"required_devices": names}, student_id=0, started_at="",
                         ports={n: console_of(by_name[n]) for n in names}, allowed_ports=set(),
                         hosts={n for n in names if by_name[n].get("kind") == "pc"})
            done = []
            for n in names:
                try:
                    text = await self._snapshot(tmp, n)
                except ResetError as exc:
                    raise PracticeError(409, f"无法读取 {n} 的配置（{exc.reason}），请稍后重试") from None
                await asyncio.to_thread(db.set_setting, self._baseline_key(cur, n),
                                        json.dumps({"config": text, "at": _now_iso()}, ensure_ascii=False))
                done.append(n)
            return {"ok": True, "devices": done, "topology": t.get("name")}

    async def _config(self, port: int, cmds: list[str], tolerate: bool = False) -> HiddenResult:
        """配置事务；学员正在输入等暂不可执行时重试到 op_deadline。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.op_deadline
        while True:
            # confirm：undo vlan batch / undo ospf 等会问 [Y/N]，不自动回答的话下一条命令会被当成回答
            r = await self.hub.run_hidden(port, cmds, kind="config", tolerate_errors=tolerate, confirm=True)
            if r.status != "busy" or loop.time() >= end:
                return r
            await asyncio.sleep(min(self.busy_retry, max(0.05, end - loop.time())))

    async def _checks_once(self, a: Active) -> dict[str, Any]:
        """跑一轮 checks。任一设备暂不可检查就整轮作废（不按部分结果判定）。"""
        s = a.scenario
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in s["checks"]:
            groups.setdefault(c["device"], []).append(c)
        verdicts: list[bool] = []
        for dev, items in groups.items():
            r = await self.hub.run_hidden(a.ports[dev], [c["command"] for c in items], kind="check")
            if not r.ok:
                return {"status": r.status, "reason": f"{dev}：{r.reason}", "at": _now_iso()}
            for c, out in zip(items, r.outputs):
                hit = re.search(c["expect"], out, re.M) is not None
                verdicts.append(not hit if c["negate"] else hit)
        passed = sum(verdicts)
        return {"status": "ok", "passed": passed, "total": len(verdicts),
                "all": passed == len(verdicts), "at": _now_iso()}

    async def _round(self, a: Active, source: str) -> dict[str, Any]:
        async with self._check:
            if self.active is not a or a.phase not in ("running", "starting"):
                return a.last_check or {"status": "error", "reason": "练习已结束"}
            res = await self._checks_once(a)
            res["source"] = source
            a.last_check_t = time.monotonic()
            if res["status"] == "ok":
                a.checks_run += 1
                a.last_check = res
                # 考试题关闭自动判定时，只有学员「提交本题」才算通过（开始时的故障确认 all 会被 start 判为注入无效）
                if res["all"] and a.phase == "running" and (a.auto_judge or source == "submit"):
                    await self._mark_passed(a)
            elif a.last_check is None or a.last_check.get("status") != "ok":
                a.last_check = res
            else:
                a.last_check = {**a.last_check, "skipped": res["reason"], "skipped_at": res["at"]}
            return res

    async def _mark_passed(self, a: Active) -> None:
        a.phase = "passed"
        a.passed_at = _now_iso()
        await asyncio.to_thread(db.update_attempt, a.attempt_id, passed=1, ended_at=a.passed_at,
                                hints_used=a.hints_used)
        self._persist(a)

    # ---- 开始 ----

    async def start(self, student_id: int, scenario_id: str, seed: int | None = None, *, mode: str = "practice",
                    exam_id: int | None = None, exam_template_id: int | None = None, exam_index: int | None = None,
                    auto_judge: bool = True, max_submissions: int = 0) -> dict[str, Any]:
        async with self._op:
            if self.active is not None:
                if self.resetting:
                    raise PracticeError(409, RESETTING_BUSY.format(what="开始"))
                raise PracticeError(409, "已有进行中的练习，请先结束当前练习" if self.active.mode == "practice"
                                    else "考试进行中，不能开始练习")
            template = self.scenarios.get(scenario_id)
            if template is None:
                raise PracticeError(404, f"场景不存在：{scenario_id}")
            reason = self.capacity_guard(template) if self.capacity_guard else None
            if reason:
                raise PracticeError(409, reason)
            if seed is None:
                seed = config.PRACTICE_SEED if config.PRACTICE_SEED is not None else new_seed()
            try:
                s = sc.expand(template, seed)
            except (ValueError, KeyError) as exc:
                raise PracticeError(400, f"种子 {seed} 无法展开场景：{exc}") from None
            cur = await self.resolve()
            rd = sc.readiness(s, cur)
            if not rd["ready"]:
                raise PracticeError(409, rd["reason"])
            ports = rd["ports"]
            if (cur.get("lab") or {}).get("kind", "ensp") == "ensp" and hasattr(self.hub, "check_ready"):
                for device in s["required_devices"]:
                    ready = await self.hub.check_ready(ports[device])
                    if not ready.ok:
                        raise PracticeError(409, f"{device} console 未就绪（busy）：{ready.reason}，请稍后重试")

            def create() -> dict[str, Any]:
                return db.create_attempt(student_id, self.scenario_ids[s["id"]], mode, seed=s["seed"],
                                         param_values=s["param_values"], exam_id=exam_id,
                                         exam_template_id=exam_template_id, exam_index=exam_index)
            att = await asyncio.to_thread(create)
            a = Active(attempt_id=att["id"], scenario=s, student_id=student_id, started_at=att["started_at"],
                       ports=ports, allowed_ports={ports[d] for d in s["accessible_devices"]}, seed=s["seed"],
                       mode=mode, exam_id=exam_id, auto_judge=auto_judge, max_submissions=max_submissions,
                       hosts=set(s.get("hosts") or []))
            # 拓扑里是 PC 的本题设备一律按终端主机处理（快照 / 复位走地址配置）：场景没把它写进 hosts 时，
            # PC 设置气泡改过的地址照样随复位还原
            kinds = {d["name"]: d.get("kind") for d in (cur.get("topology") or {}).get("devices") or []}
            a.hosts |= {d for d in s["required_devices"] if kinds.get(d) == "pc"}
            self.active = a
            touched: list[str] = []
            try:
                # 先给每台设备拍整机配置快照（结束时复位的目标），再动任何配置。拍之前先与干净基线比对（不一致先复位），
                # 上一次没撤销干净、又被自动保存下来的改动不会混进本题
                for dev in s["required_devices"]:
                    try:
                        text = await self._check_baseline(a, cur, dev)
                    except ResetError as exc:
                        raise PracticeError(409, f"无法读取 {dev} 的当前配置（{exc.reason}），请稍后重试") from None
                    a.snapshots[dev] = text
                    await asyncio.to_thread(db.save_config, a.attempt_id, dev, text, ports[dev])
                # baseline 与 inject 各自一个配置事务（各自从用户视图开始、回到用户视图），
                # 拼成一串会让 inject 开头的 system-view 在接口视图里执行而报错
                for part, label in (("baseline", "初始配置"), ("inject", "注入故障")):
                    for dev in s["required_devices"]:
                        cmds = s[part].get(dev)
                        if not cmds:
                            continue
                        r = await self._apply(a, dev, cmds)
                        # busy = 一条命令都没执行（前提检查未通过），这台设备不需要还原
                        if r.status != "busy" and dev not in touched:
                            touched.append(dev)
                        if not r.ok:
                            raise PracticeError(409, f"在 {dev} 上{label}失败：{r.reason}")
                res = await self._verify_fault(a)
                if res["status"] != "ok":
                    raise PracticeError(409, f"无法确认故障已生效（{res['reason']}），已还原，请稍后重试")
                if res["all"]:
                    raise PracticeError(409, "注入后判定已全部通过，说明故障没有生效（场景或设备当前配置有误），已还原")
            except BaseException as exc:
                restore_err = await self._restore(a, touched)
                if not restore_err:
                    reports = await self._reset(a, touched)
                    restore_err = "；".join(f"{d}：{r.reason or '、'.join(r.leftovers)}"
                                            for d, r in reports.items() if not r.ok)
                await asyncio.to_thread(db.delete_attempt, a.attempt_id)
                self.active = None
                secret = s if mode == "exam" else None     # 考试中错误信息不带出注入 / 判定命令
                if restore_err:
                    raise PracticeError(409, mask(f"开始失败且还原未完成：{restore_err}", secret)) from None
                if secret is not None and isinstance(exc, PracticeError):
                    raise PracticeError(exc.status, mask(exc.detail, secret)) from None
                raise
            a.phase = "running"
            for p in ports.values():
                self.hub.reset_replay(p)       # 新打开的标签看不到上一次练习的命令和答案
            self.hub.pin(ports.values())
            topo_ports = {console_of(d) for d in cur["topology"]["devices"] if console_of(d) > 0}
            label = "考试" if mode == "exam" else "练习"
            self.hub.drop_subscribers(topo_ports - a.allowed_ports,
                                      f"\r\n[{label}开始] 本题不开放此设备的终端\r\n")
            self._persist(a)
            return self.state(a)

    async def _verify_fault(self, a: Active) -> dict[str, Any]:
        """注入后跑一遍 checks；暂不可检查时短暂重试，仍不行就失败（不把「没检查」当「故障已生效」）。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.op_deadline
        while True:
            res = await self._round(a, "start")
            if res["status"] != "busy" or loop.time() >= end:
                return res
            await asyncio.sleep(self.busy_retry)

    # ---- 自动判定 ----

    def on_hub_event(self, event: str, port: int, data: str) -> None:
        a = self.active
        if a is None or a.phase != "running" or a.closing or port not in a.port_dev:
            return
        if event == "command":
            a.commands += 1
            dev = a.port_dev[port]

            def insert(a=a, port=port, dev=dev, cmd=data) -> None:
                a.last_log[port] = db.add_attempt_log(a.attempt_id, dev, cmd)
            self._db_write(insert)
            if a.auto_judge:
                self._schedule(a)
        elif event in ("output", "log"):
            # 输出写进对应命令那一行；采集之外的日志追加到该设备最近一条命令上（视为它的附带影响）。
            # 该设备还没有学员命令时的日志（注入故障的余波）不记。
            def update(a=a, port=port, text=data, append=event == "log") -> None:
                log_id = a.last_log.get(port)
                if log_id is not None and text:
                    db.set_log_output(log_id, text, append=append)
            self._db_write(update)
        elif event == "input" and a.debounce is not None and not a.debounce.done():
            self._schedule(a)              # 仍在输入：重新计时

    def _db_write(self, fn: Callable[[], None]) -> asyncio.Task:
        """命令记录的写库串行执行（插入行与回填输出不能乱序）。"""
        prev = self._db_tail

        async def run() -> None:
            if prev is not None:
                try:
                    await prev
                except BaseException:
                    pass
            await asyncio.to_thread(fn)
        self._db_tail = asyncio.create_task(run())
        return self._db_tail

    async def flush_logs(self) -> None:
        """等命令记录全部落库（评分 / 提示读取记录之前调用）。"""
        while self._db_tail is not None:
            t = self._db_tail
            try:
                await t
            except BaseException:
                pass
            if self._db_tail is t:
                return

    def _schedule(self, a: Active) -> None:
        if a.judging:
            a.rejudge = True               # 正在判定：结束后再来一轮，不取消进行中的事务
            return
        if a.debounce is not None and not a.debounce.done():
            a.debounce.cancel()
        a.debounce = asyncio.create_task(self._debounce(a))

    async def _debounce(self, a: Active) -> None:
        tries = 0
        while self.active is a and a.phase == "running":
            await asyncio.sleep(self.debounce_s)
            wait = a.last_check_t + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            a.judging = True
            try:
                res = await self._round(a, "auto")
            finally:
                a.judging = False
            tries += 1
            if a.rejudge:
                a.rejudge = False
                tries = 0
                continue
            if res.get("status") == "busy" and tries < 5:
                continue                   # 暂不可检查：稍后重试
            return

    async def check(self) -> dict[str, Any]:
        """「立即检查」。不受最小间隔限制，但与自动判定串行。只用于练习（考试题走 submit，次数受限）。"""
        a = self.require("practice")
        if a.phase == "running":
            a.judging = True
            try:
                res = await self._round(a, "manual")
            finally:
                a.judging = False
            if res.get("status") != "ok":
                return {**self.state(a), "check": res}
        return {**self.state(a), "check": a.last_check}

    # ---- 提示 ----

    async def submit(self) -> dict[str, Any]:
        """考试「提交本题」（「我修好了」验收）：跑一遍 checks 作为本题结果。

        设备暂不可检查（学员正在输入等）时重试到 op_deadline，仍不行返回 busy、不计提交次数。
        通过即锁定本题；未通过计一次提交，次数用完也锁定。返回本次提交记录（只含通过几项，不含判定命令）。
        """
        a = self.require("exam")
        if a.phase != "running":
            raise PracticeError(409, "本题正在结束或复位，不能再提交")
        if a.locked:
            raise PracticeError(409, "本题已通过" if a.passed_at else "本题提交次数已用完")
        loop = asyncio.get_running_loop()
        end = loop.time() + self.op_deadline
        a.judging = True
        try:
            while True:
                res = await self._round(a, "submit")
                if res.get("status") != "busy" or loop.time() >= end:
                    break
                await asyncio.sleep(self.busy_retry)
        finally:
            a.judging = False
        if self.active is not a:
            raise PracticeError(409, "本题已结束")
        if res.get("status") != "ok":
            return {"status": res.get("status"), "reason": mask(res.get("reason", ""), a.scenario),
                    "counted": False}
        sub = {"n": len(a.submissions) + 1, "at": res["at"], "passed": bool(res["all"]),
               "passed_items": res["passed"], "total": res["total"]}
        a.submissions.append(sub)
        self._persist(a)
        return {"status": "ok", "counted": True, **sub}

    async def hint(self, level: int, mode: str = "practice") -> dict[str, Any]:
        a = self.require(mode)
        hints = a.scenario["hints"]
        if not 1 <= level <= len(hints):
            raise PracticeError(400, f"提示级别应为 1~{len(hints)}")
        if level > a.hints_used + 1:
            raise PracticeError(400, f"请先查看第 {a.hints_used + 1} 级提示")
        async with a.hint_lock:            # 连点两次只生成一次
            got = a.hint_cache.get(level)
            if got is None:
                got = await self._make_hint(a, level)
                a.hint_cache[level] = got
            # AI 生成期间（最长十几秒）学员可能已经结束了练习：不再写库 / 持久化，否则会把已清掉的
            # practice_active 写回去（重启后恢复出幽灵练习），hints_used 也会在评分开始后才变
            if self.active is not a or a.closing:
                raise PracticeError(409, "练习已结束")
            if level > a.hints_used:
                a.hints_used = level
                await asyncio.to_thread(db.update_attempt, a.attempt_id, hints_used=a.hints_used)
                self._persist(a)
        out = {"level": level, "text": got["text"], "source": got["source"], "hints_used": a.hints_used}
        if got.get("reason"):
            out["fallback_reason"] = got["reason"]
        return out

    async def _make_hint(self, a: Active, level: int) -> dict[str, Any]:
        """AI 提示（结合学员当前的命令和输出）；没有 hinter 或 AI 不可用时用场景静态提示。"""
        if self.hinter is None:
            return {"text": a.scenario["hints"][level - 1], "source": "static"}
        await self.flush_logs()
        logs = await asyncio.to_thread(db.attempt_logs, a.attempt_id)
        for l in logs:
            l["t"] = _parse_iso(l["ts"]) if l.get("ts") else None
        return await self.hinter(a.scenario, level, logs, _parse_iso(a.started_at))

    # ---- 结束 ----

    async def _restore(self, a: Active, devices: list[str] | None = None) -> str:
        """执行场景 restore（尽力而为：单条命令出错继续）；返回未完成的原因，成功为空串。"""
        s = a.scenario
        errs = []
        for dev in (devices if devices is not None else s["required_devices"]):
            cmds = s["restore"].get(dev)
            if not cmds:
                continue
            r = await self._apply(a, dev, cmds, tolerate=True)
            if not r.ok:
                errs.append(f"{dev}：{r.reason}")
        return "；".join(errs)

    async def _powered_off(self, a: Active) -> list[str]:
        """本题设备里已关机（实验后端报 stopped）的设备名。后端不支持启停、或读不到状态时返回 []（照常尝试复位）。"""
        lab = self.lab
        if lab is None or not getattr(lab, "power", False):
            return []
        try:
            states = await lab.device_states()
        except Exception as exc:         # 会话被挤掉 / 接口出错：不猜，按原流程去复位
            log.warning("结束练习时读取设备电源状态失败，按原流程复位：%s", getattr(exc, "detail", None) or exc)
            return []
        return [d for d in a.scenario["required_devices"]
                if (states.get(a.ports.get(d)) or {}).get("status") == "stopped"]

    def _freeze(self, a: Active) -> None:
        """结束流程开始：本题设备的终端不再接受学员输入（不与复位抢终端）。"""
        freeze = getattr(self.hub, "freeze", None)
        if freeze is not None:
            label = "考试题" if a.mode == "exam" else "练习"
            freeze(a.ports.values(), f"\r\n[{label}已结束] 正在把设备复位到开练前的配置，复位完成前终端不接受输入\r\n")

    async def _reclaim(self, a: Active, dev: str) -> str:
        """强制收回 dev 的终端（学员停在分页 / 系统视图 / 半行输入等都不再 busy）；返回失败原因，成功为空串。
        终端主机走实验后端接口复位，不需要。"""
        reclaim = getattr(self.hub, "reclaim", None)
        if reclaim is None or dev in a.hosts:
            return ""
        r = await reclaim(a.ports[dev])
        return "" if r.ok else r.reason

    async def _end_reset(self, a: Active, devices: list[str]) -> tuple[dict[str, ResetResult], dict[str, str]]:
        """结束时逐台：强制收回终端 → 场景 restore（尽力而为）→ 整机复位到开练快照。没做成（busy / error，如设备连不上）
        的设备隔 end_retry_wait 秒重试，共 end_retries 轮。返回 (复位报告, {没复位成功的设备: 原因})。"""
        reports: dict[str, ResetResult] = {}
        failed: dict[str, str] = {}
        todo = list(devices)
        for n in range(max(1, self.end_retries)):
            if n:
                await asyncio.sleep(self.end_retry_wait)
            failed = {}
            # 先全部 restore（撤销场景自己的改动），再逐台整机复位（与开练快照比对，学员额外的改动一并撤销）
            for dev in todo:
                err = await self._reclaim(a, dev)
                cmds = a.scenario["restore"].get(dev) if dev in a.scenario["required_devices"] else None
                if not err and cmds:
                    r = await self._apply(a, dev, cmds, tolerate=True)
                    err = "" if r.ok else r.reason
                if err:
                    failed[dev] = err
            for dev in todo:
                if dev in failed or dev not in a.snapshots:
                    continue
                rr = await self._restore_device(a.ports[dev], a.snapshots[dev])
                reports[dev] = rr
                if rr.status in ("busy", "error"):          # dirty 重试也没用，按遗留配置报告
                    failed[dev] = rr.reason
            todo = list(failed)
            if not todo:
                break
            log.warning("结束复位第 %d 轮未完成：%s", n + 1, failed)
        return reports, failed

    async def _failed_hint(self, devs: list[str]) -> str:
        """没复位成功的设备之后会怎样：只有实验后端做干净基线比对（BASELINE_LABS）、且这些设备都已有基线时，
        下一题开始前才会自动比对复位；否则（eNSP 经典版，或还没有基线——会把这份配置直接记成基线）要手动处理。"""
        if getattr(self.lab, "kind", None) in BASELINE_LABS:
            try:
                cur = await self.resolve()
            except Exception:
                cur = {}
            for d in devs:
                if await asyncio.to_thread(db.get_setting, self._baseline_key(cur, d)) is None:
                    break
            else:
                return BASELINE_HINT
        return MANUAL_HINT

    async def _summary(self, a: Active) -> dict[str, Any]:
        commands = len(await asyncio.to_thread(db.attempt_logs, a.attempt_id))
        att = await asyncio.to_thread(db.get_attempt, a.attempt_id)
        return {"attempt_id": a.attempt_id, "scenario": sc.public(a.scenario),
                "passed": bool(att and att["passed"] == 1),
                "duration_s": int(_parse_iso(att["ended_at"]) - _parse_iso(a.started_at))
                if att and att.get("ended_at") else None,
                "hints_used": a.hints_used, "commands": commands}

    @property
    def resetting(self) -> bool:
        """上一次练习 / 考试题已结束、设备还在后台复位（active 仍占着槽位，开始新练习等一律 409）。"""
        return self.active is not None and self.active.phase == "ending"

    async def end(self, mode: str = "practice", score_opts: dict[str, Any] | None = None) -> dict[str, Any]:
        """结束并等复位完成，返回最终结果（考试的题间切换 / 交卷用；练习接口用 begin_end 立即返回）。"""
        await self.begin_end(mode, score_opts)
        task = self._end_task
        assert task is not None
        return await asyncio.shield(task)

    async def begin_end(self, mode: str = "practice", score_opts: dict[str, Any] | None = None) -> dict[str, Any]:
        """结束练习 / 考试题：记录放弃 / 通关与结束时间，冻结本题终端，复位放进后台任务，立即返回。

        返回 resetting=True 的结果（不含复位结果）；后台复位完成后 last_result（练习）/ end() 的返回里才有复位结果，
        之后才评分。复位失败（设备连不上等）重试几次后仍按结束处理（reset_ok=False，note 说明哪些设备没复位）。
        后台复位期间 active 仍占着槽位（phase=ending）：开始新练习 / 考试、切换环境、记录基线一律 409。"""
        async with self._op:
            a = self.require(mode)
            task = self._end_task
            if a.phase == "ending" and task is not None and not task.done() and self._ending is a:
                return dict(a.end_view or {})     # 后台复位进行中（重复点击）：不再起第二个
            if a.debounce is not None and not a.debounce.done():
                a.debounce.cancel()
            async with self._check:          # 等进行中的判定结束
                pass
            if not a.closing:
                flush = getattr(self.hub, "flush_output", None)
                for p in a.ports.values():   # 还没回到提示符的那条命令：按已收到的输出落库
                    if flush is not None:
                        flush(p)
                a.closing = True             # 之后还原事务带出的日志不算学员的
            await self.flush_logs()
            if a.ended_at is None:
                a.ended_at = a.passed_at or _now_iso()
                if a.passed_at is None:      # 主动放弃
                    await asyncio.to_thread(db.update_attempt, a.attempt_id, ended_at=a.ended_at,
                                            passed=0, hints_used=a.hints_used)
            a.phase = "ending"
            self._persist(a)
            self._freeze(a)
            a.end_view = {**await self._summary(a), "active": False, "resetting": True, "restored": None,
                          "reset_ok": None, "powered_off": [], "reset_failed": {}, "reset": {}, "leftovers": {},
                          "note": RESETTING_NOTE}
            if a.mode == "practice":
                self.last_result = a.end_view
            self._ending = a
            self._end_task = asyncio.create_task(self._end_bg(a, score_opts))
            return dict(a.end_view)

    async def _end_bg(self, a: Active, score_opts: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return await self._finish_end(a, score_opts)
        except asyncio.CancelledError:
            raise                            # 服务关闭：保留 ending，重启后 resume_ending 接着复位
        except Exception as exc:             # 兜底：无论如何都按结束处理，不能让 active 卡在 ending
            log.exception("结束复位出错，按结束处理")
            result = {**(a.end_view or {}), "active": False, "resetting": False, "restored": False,
                      "reset_ok": False, "note": f"复位过程出错（{exc}），设备上可能还留有本题的改动。" + MANUAL_HINT}
            self._close(a, result)
            return result

    def _close(self, a: Active, result: dict[str, Any]) -> None:
        if self.active is a:
            self.active = None
            self._persist(None)
        if a.mode == "practice":           # 考试题的结果只进成绩单（练习接口不能看到）
            self.last_result = result
        self.hub.unpin(a.ports.values())
        unfreeze = getattr(self.hub, "unfreeze", None)
        if unfreeze is not None:
            unfreeze(a.ports.values(), "\r\n[复位完成] 终端已恢复输入\r\n")

    async def _finish_end(self, a: Active, score_opts: dict[str, Any] | None) -> dict[str, Any]:
        # 已关机的设备连不上 console：跳过 restore 与复位（下一题开始前会与干净基线比对）
        off = await self._powered_off(a)
        devices = [d for d in dict.fromkeys([*a.scenario["required_devices"], *a.snapshots]) if d not in off]
        reports, failed = await self._end_reset(a, devices)
        if a.mode == "exam":
            failed = {d: mask(r, a.scenario) for d, r in failed.items()}
        leftovers = {d: r.leftovers for d, r in reports.items() if r.status == "dirty"}
        excluded = {d: r.excluded for d, r in reports.items() if r.excluded}
        reset_ok = not leftovers and bool(a.snapshots) and not off and not failed
        if leftovers:
            note = RELOAD_NOTE + "".join(f"\n{d}：\n  " + "\n  ".join(ls) for d, ls in leftovers.items())
        elif not a.snapshots:
            note = NO_SNAPSHOT_NOTE
        else:
            note = RESTORE_NOTE
        problems = []
        if off:
            problems.append(POWERED_OFF_NOTE.format(devices="、".join(off)))
        if failed:
            problems.append(RESET_FAILED_NOTE.format(items="；".join(f"{d}（{r}）" for d, r in failed.items()))
                            + await self._failed_hint(list(failed)))
        if problems:
            ok_any = any(r.ok for d, r in reports.items() if d not in failed)
            rest = ("\n其余设备已复位到开练前的配置。" if ok_any else "") if note == RESTORE_NOTE else "\n" + note
            note = "\n".join(problems) + rest
        if excluded:
            note += "\n" + EXCLUDED_NOTE + "".join(f"\n{d}：" + "；".join(ls) for d, ls in excluded.items())
        result = {
            **(a.end_view or await self._summary(a)),
            "active": False, "resetting": False, "restored": not leftovers and not off and not failed,
            "reset_ok": reset_ok, "powered_off": off, "reset_failed": failed,
            "reset": {d: r.as_dict() for d, r in reports.items()}, "leftovers": leftovers, "note": note,
        }
        # 复位结果落库（attempts.reset_json）后再评分：评分读库里的复位结果交给 AI（重新评分也拿得到）。
        # 复位没做干净是系统的问题，只作为背景信息，不扣学员的分（grading / prompts）
        reset_info = {"ok": reset_ok, "snapshot": bool(a.snapshots), "leftovers": leftovers,
                      "excluded": excluded, "devices": result["reset"], "powered_off": off, "failed": failed}
        await asyncio.to_thread(db.update_attempt, a.attempt_id,
                                reset_json=json.dumps(reset_info, ensure_ascii=False))
        if self.scorer is not None:      # 设备已复位：异步评分
            result["score"] = await self.scorer.schedule(a.attempt_id, a.scenario, score_opts)
        self._close(a, result)
        return result

    async def resume_ending(self) -> None:
        """服务重启后：恢复出来的练习停在 ending（后台复位被打断）时接着在后台复位。考试题由考试计时器调 end() 接着做。"""
        a = self.active
        if a is None or a.phase != "ending" or a.mode != "practice":
            return
        try:
            await self.begin_end("practice")
        except Exception:
            log.exception("服务重启后继续复位失败")

    async def shutdown(self) -> None:
        """服务关闭：取消后台复位（phase=ending 已持久化，重启后 resume_ending 接着做）。"""
        task = self._end_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    # ---- PC 设置气泡（终端主机地址，spec §16.1） ----

    async def set_host(self, port: int, name: str, cfg: dict[str, str]) -> dict[str, str]:
        """经实验后端设置终端主机（PC）的地址（等 PC 命令行恢复后返回设置后的配置）。

        练习 / 考试进行中：只能改本题开放的设备；第一次改某台 PC 之前先拍它的地址快照（结束时随整机复位还原，
        场景 hosts 里的 PC 开练时已拍过）；每次修改写一条伪命令进操作记录（「[PC 配置] PC1 ip=… mask=… gw=… dns=…」，
        命令回放与评分可见）；开着自动判定时改完排一次判定（改 PC 地址也可能修好故障）。与开始 / 结束串行。"""
        async with self._op:
            a = self.active
            dev = name
            if a is not None:
                label = "考试" if a.mode == "exam" else "练习"
                if port not in a.allowed_ports:
                    raise PracticeError(403, f"{label}进行中，本题不开放 {name}")
                if a.closing or a.phase not in ("running", "passed"):
                    raise PracticeError(409, f"{label}正在结束或复位，暂时不能修改 PC 设置")
                dev = a.port_dev.get(port, name)
                if dev not in a.snapshots:
                    try:
                        snap = host_snapshot(await self.lab.host_get(port))
                    except LabError as exc:
                        raise PracticeError(exc.status, f"读取 {dev} 的当前地址失败：{exc.detail}") from None
                    a.snapshots[dev] = snap
                    await asyncio.to_thread(db.save_config, a.attempt_id, dev, snap, port)
                if dev not in a.ports:
                    a.ports[dev] = port
                    a.port_dev[port] = dev
                    self.hub.pin([port])
                    self._persist(a)
            err, err_status = "", 502
            got: dict[str, str] = {}
            try:
                got = await self.lab.host_set(port, cfg)
            except LabError as exc:
                err, err_status = exc.detail, exc.status
            if a is not None and self.active is a:
                cmd = host_command(dev, cfg)
                out = f"设置失败：{err}" if err else \
                    f"已生效：ip={got.get('ip') or '-'} mask={got.get('mask') or '-'} gw={got.get('gateway') or '-'}" + \
                    (f" dns={got.get('dns') or '-'}" if "dns" in got else "")
                a.commands += 1

                def insert(a=a, dev=dev, cmd=cmd, out=out) -> None:
                    db.add_attempt_log(a.attempt_id, dev, cmd, out)
                self._db_write(insert)
                self._persist(a)
                if a.auto_judge and a.phase == "running":
                    self._schedule(a)
            if err:
                raise PracticeError(err_status, err)
            return got

    # ---- 状态 ----

    def state(self, a: Active | None = None) -> dict[str, Any]:
        a = a or self.active
        if a is None:
            return {"active": False, "last_result": self.last_result}
        if a.phase == "ending" and a.mode == "practice":
            # 练习已结束、后台复位中：对学员来说练习已结束（总结页轮询 last_result 等复位结果）
            return {"active": False, "resetting": True, "attempt_id": a.attempt_id,
                    "last_result": self.last_result or a.end_view}
        end_t = _parse_iso(a.passed_at or a.ended_at) if (a.passed_at or a.ended_at) else time.time()
        lc = a.last_check
        return {
            "active": True, "phase": a.phase, "attempt_id": a.attempt_id, "student_id": a.student_id,
            "scenario": sc.public(a.scenario), "started_at": a.started_at,
            "elapsed_s": int(end_t - _parse_iso(a.started_at)),
            "passed": a.passed_at is not None,
            "passed_at": a.passed_at, "hints_used": a.hints_used, "commands": a.commands,
            "devices": [{"name": d, "port": a.ports[d]} for d in a.scenario["accessible_devices"]],
            # 只给通过几项，不暴露 checks 的具体命令
            "last_check": None if lc is None else {k: lc[k] for k in
                                                   ("status", "passed", "total", "all", "at", "source",
                                                    "reason", "skipped", "skipped_at") if k in lc},
            "checks_run": a.checks_run, "restore_error": a.restore_error,
            "mode": a.mode, "auto_judge": a.auto_judge,
            # 开始前与干净基线的比对：clean / reset（已复位，含差异行数）/ captured（首次记录基线）
            "pre_reset": {d: {k: v for k, v in r.items() if k != "differences"} | (
                {"differences": len(r["differences"])} if "differences" in r else {}) for d, r in a.pre_reset.items()},
            # 考试中不给种子与参数取值（结合仓库里的场景文件可以反推注入值）
            **({"seed": a.seed, "param_values": a.scenario.get("param_values", {})} if a.mode == "practice" else
               {"exam_id": a.exam_id, "submissions": a.submissions, "max_submissions": a.max_submissions,
                "locked": a.locked}),
        }

    # ---- 持久化（服务重启后恢复进行中的练习） ----

    def _persist(self, a: Active | None) -> None:
        if a is None:
            db.set_setting(_SETTING, None)
            return
        db.set_setting(_SETTING, json.dumps({
            "attempt_id": a.attempt_id, "scenario_id": a.scenario["id"], "student_id": a.student_id,
            "started_at": a.started_at, "ports": a.ports, "phase": a.phase, "hints_used": a.hints_used,
            "passed_at": a.passed_at, "ended_at": a.ended_at, "commands": a.commands,
            "seed": a.seed, "mode": a.mode, "exam_id": a.exam_id, "auto_judge": a.auto_judge,
            "max_submissions": a.max_submissions, "submissions": a.submissions,
        }, ensure_ascii=False))

    def recover(self) -> None:
        raw = db.get_setting(_SETTING)
        if not raw:
            return
        try:
            d = json.loads(raw)
            # 按记录的种子重新展开（参数化场景的判定 / 还原命令取决于种子）
            s = sc.expand(self.scenarios[d["scenario_id"]], d.get("seed", 0))
            a = Active(attempt_id=d["attempt_id"], scenario=s, student_id=d["student_id"],
                       started_at=d["started_at"], ports={k: int(v) for k, v in d["ports"].items()},
                       allowed_ports=set(), seed=d.get("seed", 0), mode=d.get("mode", "practice"),
                       exam_id=d.get("exam_id"), auto_judge=d.get("auto_judge", True),
                       max_submissions=d.get("max_submissions", 0), submissions=d.get("submissions") or [])
            a.allowed_ports = {a.ports[x] for x in s["accessible_devices"] if x in a.ports}
            a.hosts = set(s.get("hosts") or [])
            # restore_failed：旧版本「复位未完成、等学员重试」的状态，现在一律按结束处理、接着后台复位
            a.phase = {"starting": "running", "restore_failed": "ending"}.get(d["phase"], d["phase"])
            a.closing = a.phase == "ending"
            a.hints_used, a.passed_at, a.commands = d["hints_used"], d["passed_at"], d.get("commands", 0)
            a.ended_at = d.get("ended_at")
            a.snapshots = db.get_configs(a.attempt_id)     # 开练前快照：重启后照样能整机复位
            self.active = a
            self.hub.pin(a.ports.values())
            if a.closing:
                self._freeze(a)
        except Exception:
            db.set_setting(_SETTING, None)
