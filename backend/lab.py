"""实验后端（LabProvider，spec §5）：「当前拓扑、设备状态、终端白名单、设备连接、设备启停」的统一接口。

上层（终端桥接、练习、考试、复位）只对这里编程，不关心设备跑在哪个模拟器里：
- EnspProvider（缺省，AINET_LAB_PROVIDER=ensp）：eNSP 经典版。当前拓扑由运行中的设备 GUID 反查 .topo
  （topology.resolve_running），console 是 telnet（connectors.EnspConnector），设备只能在 eNSP 界面上启停。
- EnsproProvider（AINET_LAB_PROVIDER=enspro，见 enspro.py）：eNSP Pro。当前拓扑 = 配置的沙箱
  （AINET_ENSPRO_SANDBOX），console 走 STOMP over WebSocket，设备可经 REST 接口启停。

统一设备标识（console 键）：拓扑里每台设备都有整数字段 `console`——能开终端的设备 > 0，否则 0。
- eNSP 经典版：console = com_port（2000 起的 telnet 端口，与旧代码完全一致）；
- eNSP Pro：console = deviceId（沙箱内设备的数据库 id）。
ConsoleHub 的通道键、/ws/console/{console}、练习 / 考试里的「设备名 -> 端口」映射、终端白名单都用这个整数，
所以上层代码与前端只需把 com_port 换成 console。同一时刻只有一个后端在用，两种整数不会混在一起。

resolve() 返回的结构与 topology.resolve_running() 相同（state / message / topology / devices / running / total …），
另加 `lab`：{kind, platform, power, label}，前端据此显示启停按钮、终端标题等。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import config
import topology

log = logging.getLogger("ainet.lab")


def console_of(d: dict[str, Any]) -> int:
    """拓扑设备的 console 键（旧数据只有 com_port 时退回 com_port）。"""
    v = d.get("console")
    if v is None:
        v = d.get("com_port", 0)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class LabError(Exception):
    """实验后端操作失败（status 为建议的 HTTP 状态码）。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class LabProvider:
    kind = "base"
    platform = "vrp5"          # 设备系统：vrp5（eNSP 经典版 AR / S 系列）/ yunshan（eNSP Pro 的 YunShan OS）
    power = False              # 能否由 ainet 启停设备

    def info(self) -> dict[str, Any]:
        return {"kind": self.kind, "platform": self.platform, "power": self.power, "label": self.label()}

    def label(self) -> str:
        return self.kind

    async def resolve(self) -> dict[str, Any]:
        """当前拓扑与各设备状态（给前端 / 练习就绪判断）。"""
        raise NotImplementedError

    async def current(self) -> dict[str, Any]:
        """终端白名单用的快速版本（可不含设备状态细节）。"""
        return await self.resolve()

    def allowed(self, cur: dict[str, Any]) -> set[int]:
        """当前可开终端的 console 键。"""
        t = cur.get("topology")
        if cur.get("state") != "running" or t is None:
            return set()
        return {console_of(d) for d in t["devices"] if console_of(d) > 0}

    def deny_reason(self, cur: dict[str, Any], key: int) -> str:
        """key 不在白名单（且不是练习限制）时给学员看的原因。"""
        return cur.get("message") or "当前没有可用的设备"

    def connector(self, key: int) -> Any:
        """新建一个未连接的 DeviceConnector。"""
        raise NotImplementedError

    def endpoint(self, key: int) -> str:
        """终端里「[已连接] …」显示的连接目标。"""
        return str(key)

    async def power_action(self, action: str, keys: list[int]) -> dict[str, Any]:
        raise LabError(400, "当前实验后端不支持由 ainet 启停设备，请在模拟器界面上操作")

    # 启动看门狗（labpower.PowerWatch，LabManager 建后端时挂上）：resolve() 用它叠加 initializing / start_failed 等状态
    watch: Any = None

    async def device_states(self) -> dict[int, dict[str, Any]]:
        """能启停的后端：当前沙箱各设备的接口原始状态 {console 键: {name, status, code, console}}（不叠加看门狗状态）。"""
        raise LabError(400, "当前实验后端不支持由 ainet 启停设备")

    async def relogin(self) -> dict[str, Any]:
        raise LabError(400, "当前实验后端不需要登录")

    def health(self) -> tuple[str, str] | None:
        """探测（只连端口）看不出来、只有当前后端自己知道的问题：(原因分类, 中文说明)，如 eNSP Pro 登录被拒 / 会话被挤掉
        （("auth", …)）。None = 没有已知问题。课程页环境列据此提示（spec §5.2「接入设置」）。"""
        return None

    # ---- 终端主机（PC）：命令行只能 ping，地址由实验后端的接口配置（eNSP Pro，spec §10.4） ----

    def is_host(self, key: int) -> bool:
        """key 是不是终端主机（PC 一类：有命令行但不能用命令配地址）。"""
        return False

    async def host_get(self, key: int) -> dict[str, str]:
        """主机的地址配置 {ip, mask, gateway}（未配置为空串）。"""
        raise LabError(400, "当前实验后端不支持由 ainet 配置终端主机")

    async def host_set(self, key: int, cfg: dict[str, str]) -> dict[str, str]:
        """设置主机地址（ip / mask / gateway，空串 = 清除），返回设置后的配置。"""
        raise LabError(400, "当前实验后端不支持由 ainet 配置终端主机")

    async def close(self) -> None:
        return None


class EnspProvider(LabProvider):
    """eNSP 经典版：行为与抽象之前完全一致（topology.py + ensp_runtime.py + telnet console）。"""
    kind = "ensp"
    platform = "vrp5"
    power = False

    def __init__(self, connector_factory=None) -> None:
        self._factory = connector_factory

    def label(self) -> str:
        return "eNSP"

    async def resolve(self) -> dict[str, Any]:
        cur = await topology.resolve_running()
        return {**cur, "lab": self.info()}

    async def current(self) -> dict[str, Any]:
        return await asyncio.to_thread(topology.current_running)

    def allowed(self, cur: dict[str, Any]) -> set[int]:
        return topology.allowed_ports(cur)

    def deny_reason(self, cur: dict[str, Any], key: int) -> str:
        if cur["state"] == "running":
            return f"当前拓扑 {cur['topo']['name']} 的 console 端口为 {sorted(self.allowed(cur))}"
        return topology.state_reason("not_running" if cur.get("ensp_running") is False else cur["state"])

    def connector(self, key: int) -> Any:
        if self._factory is not None:
            return self._factory(key)
        from connectors import EnspConnector
        return EnspConnector(endpoint_value("ensp"), key, config.CONNECT_TIMEOUT)

    def endpoint(self, key: int) -> str:
        return f"{endpoint_value('ensp')}:{key}"


# ---- 模拟环境注册表（spec §5.2「模拟环境注册与切换」）----
#
# 每种模拟环境一个 LabEnv：key（存库 / 接口里用）、label（界面显示）、platform（设备系统，决定哪些题目可用）、
# available()（快速探测：带超时与缓存，绝不登录、绝不连设备 console）、make(options)（新建 LabProvider，
# options 里目前只有 sandbox）。新增一种环境 = 实现一个 LabProvider + register(LabEnv(...)) 一行。
#
# 模拟环境原则上是独立节点（可能在局域网或别的网络里，2026-10-07 用户定）：每种环境的接入地址（endpoint）可在运行时
# 修改（课程页「接入设置」，存 app_settings 的 lab:endpoint:<环境>），凭据只放 ainet 主机的环境变量 / secrets.env，
# 接口只报有无、绝不返回值（spec §5.2「接入设置」）。


@dataclass
class Endpoint:
    """一种环境的接入地址怎么配：kind = url（http/https 服务地址）/ host（主机名或 IP）。
    缺省值取 config.<config_attr>（启动时由环境变量 env_var 决定），运行时可被库里的记录覆盖。"""
    kind: str
    label: str
    env_var: str
    config_attr: str


@dataclass(frozen=True)
class WatchParam:
    """启动看门狗的一个参数（spec §5.2「接入设置」/ §10.5，按环境单独配置）。缺省值取 config.<config_attr>（启动时由环境变量
    env_var 决定，没设时为内置缺省 builtin），运行时可被库里的记录覆盖。attr：对应的 PowerWatch 属性名。
    取值范围 [lo, hi]；zero_off：0 = 关闭（不受下限约束）；integer：只能是整数。"""
    name: str
    label: str
    unit: str
    env_var: str
    config_attr: str
    attr: str
    builtin: float
    lo: float
    hi: float
    integer: bool = False
    zero_off: bool = False


# eNSP Pro 的看门狗参数。2026-10-07：一台 Windows 笔记本上 T1 / T2 冷启动 187~202 秒（Linux KVM 上约 100 秒），全局 150 秒
# 的启动超时让看门狗误判卡住、自动重启 3 次后标 start_failed，所以改为按环境配置。
ENSPRO_WATCH = (
    WatchParam("start_timeout", "启动超时", "秒", "AINET_ENSPRO_START_TIMEOUT", "ENSPRO_START_TIMEOUT", "start_timeout",
               150, 60, 1800),
    WatchParam("start_retries", "卡住自动重启次数", "次", "AINET_ENSPRO_START_RETRIES", "ENSPRO_START_RETRIES", "retries",
               3, 0, 10, integer=True),
    WatchParam("early_check", "早期卡死检查", "秒", "AINET_ENSPRO_EARLY_CHECK", "ENSPRO_EARLY_CHECK", "early_check",
               40, 10, 600, zero_off=True),
    WatchParam("probe_timeout", "就绪探测上限", "秒", "AINET_ENSPRO_PROBE_TIMEOUT", "ENSPRO_PROBE_TIMEOUT", "probe_timeout",
               300, 30, 1800),
    # 同时运行设备上限（里程碑 6.17）：不是看门狗参数（PowerWatch 不认识它，configure 时跳过），但同样按环境配置、
    # 同一套来源与优先级，所以放在同一个参数集里。0 = 不限。计数口径见 scenarios.device_load。
    WatchParam("max_devices", "同时运行设备上限", "台", "AINET_ENSPRO_MAX_DEVICES", "ENSPRO_MAX_DEVICES", "max_devices",
               0, 0, 64, integer=True),
)


def max_devices(key: str) -> int:
    """环境 key 的同时运行设备上限（不含 PC；0 = 不限；没有该参数的环境恒为 0）。"""
    env = REGISTRY.get((key or "").strip().lower())
    if env is None or not any(p.name == "max_devices" for p in env.watch):
        return 0
    return int(watch_value(env.key, "max_devices"))


@dataclass
class LabEnv:
    key: str
    label: str
    platform: str
    make: Callable[[dict[str, Any]], LabProvider]
    probe: Callable[..., Awaitable[tuple]]   # probe(endpoint=None) -> (在线, 原因[, 原因分类])
    power: bool = False
    sandboxes: bool = False          # 有沙箱（拓扑 = 选定的沙箱，需要选择）
    order: int = 0
    endpoint: Endpoint | None = None  # 可在运行时修改的接入地址（None = 没有）
    secrets: tuple[str, ...] = ()    # 需要的凭据名（只在 ainet 主机的环境变量 / secrets.env 里，接口只报有无）
    local_runtime: bool = False      # 设备运行状态靠 ainet 本机的文件 / 进程检测（eNSP 经典版，ensp_runtime.py）
    watch: tuple[WatchParam, ...] = ()   # 启动看门狗参数（能由 ainet 启停设备的环境才有；空 = 没有看门狗）
    type: str = ""                   # 环境类型（ENV_TYPES 的 key：ensp / enspro）；内置环境的 key 与类型相同
    custom: bool = False             # 用户在课程页添加的环境实例（存 lab:envs，可改名 / 删除；里程碑 6.19）

    def view(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "platform": self.platform, "power": self.power,
                "sandboxes": self.sandboxes, "type": self.type or self.key, "custom": self.custom}


REGISTRY: dict[str, LabEnv] = {}
PROBE_TIMEOUT = 2.0                  # 单次探测上限（秒）
PROBE_TTL = 15.0                     # 探测结果缓存（秒）
_probe_cache: dict[str, tuple[float, bool, str, str]] = {}
_probe_locks: dict[str, asyncio.Lock] = {}

# 离线原因分类（available() 的 reason_code）：前端据此显示短标签，reason 是完整的中文说明
REASON_CODES = {
    "unreachable": "连不上（网络不通 / 超时 / 解析不了主机名）",
    "tls": "TLS 握手失败",
    "auth": "登录被拒 / 会话冲突",
    "missing_secret": "缺少凭据",
    "other": "其他",
}


def register(env: LabEnv) -> LabEnv:
    REGISTRY[env.key] = env
    _probe_cache.pop(env.key, None)
    return env


def unregister(key: str) -> None:
    """去掉一个环境（只用于用户添加的实例）：注册表与内存里的探测缓存、接入地址 / 看门狗参数覆盖。"""
    REGISTRY.pop(key, None)
    _probe_cache.pop(key, None)
    _probe_locks.pop(key, None)
    _endpoints.pop(key, None)
    _watch.pop(key, None)


def environments() -> list[LabEnv]:
    return sorted(REGISTRY.values(), key=lambda e: (e.order, e.key))


def get_env(key: str) -> LabEnv:
    env = REGISTRY.get((key or "").strip().lower())
    if env is None:
        raise ValueError(f"未知的模拟环境 {key!r}（可选 {' / '.join(REGISTRY)}）")
    return env


def env_of_platform(platform: str, prefer: str | None = None) -> LabEnv | None:
    """某个设备平台对应的模拟环境（题目按 platform 自动选环境时用）。同一平台可能有多个实例（如几台 eNSP Pro，里程碑
    6.19）：prefer（通常是当前环境 / 正在浏览的环境）的平台相同就用它，否则取注册顺序第一个。"""
    if prefer:
        env = REGISTRY.get(prefer.strip().lower())
        if env is not None and env.platform == platform:
            return env
    return next((e for e in environments() if e.platform == platform), None)


def _probe_result(res: Any) -> tuple[bool, str, str]:
    """probe 的返回值统一成 (在线, 原因, 原因分类)：兼容只返回 (在线, 原因) 的旧探测（离线时分类记 other）。"""
    ok, reason = bool(res[0]), str(res[1] or "")
    code = str(res[2] or "") if len(res) > 2 else ""
    return ok, reason, ("" if ok else (code if code in REASON_CODES else "other"))


async def _run_probe(env: LabEnv, *args: Any) -> tuple[bool, str, str]:
    try:
        return _probe_result(await asyncio.wait_for(env.probe(*args), PROBE_TIMEOUT))
    except asyncio.TimeoutError:
        return False, f"探测超时（{PROBE_TIMEOUT:g} 秒没有响应）", "unreachable"
    except Exception as exc:                     # 探测本身出错也只当不可用
        return False, f"探测失败：{type(exc).__name__}: {exc}", "other"


async def available(key: str, force: bool = False) -> dict[str, Any]:
    """探测某个环境是否可用：{available, reason, reason_code, checked_at}。结果缓存 PROBE_TTL 秒，探测超过
    PROBE_TIMEOUT 秒按不可用处理（reason_code = unreachable）。reason_code 见 REASON_CODES，在线时为空串。"""
    env = get_env(key)
    lock = _probe_locks.setdefault(env.key, asyncio.Lock())
    async with lock:                                 # 同一环境并发探测只跑一次
        hit = _probe_cache.get(env.key)
        now = time.monotonic()
        if hit is not None and not force and now - hit[0] < PROBE_TTL:
            return {"available": hit[1], "reason": hit[2], "reason_code": hit[3],
                    "checked_at": time.time() - (now - hit[0])}
        ok, reason, code = await _run_probe(env)
        _probe_cache[env.key] = (time.monotonic(), ok, reason, code)
        return {"available": ok, "reason": reason, "reason_code": code, "checked_at": time.time()}


async def probe_endpoint(key: str, value: str) -> dict[str, Any]:
    """「测试连接」：用给定的接入地址探测一次（不缓存、不改任何设置；与 available() 同样不登录、不连设备 console）。"""
    env = get_env(key)
    ok, reason, code = await _run_probe(env, value)
    return {"available": ok, "reason": reason, "reason_code": code, "value": value, "checked_at": time.time()}


def clear_probe_cache(key: str | None = None) -> None:
    if key is None:
        _probe_cache.clear()
    else:
        _probe_cache.pop(key, None)


# ---- 接入地址（运行时可改，spec §5.2「接入设置」）----

SETTING_ENDPOINT = "lab:endpoint:{}"
# 运行时覆盖 {环境: (地址, 来源)}：来源 db = 库里的记录（页面上设置的），force = AINET_LAB_FORCE=1 时以环境变量为准。
# 没有覆盖时用 config 的缺省值（每次现读，测试 monkeypatch config 也生效）。LabManager.boot() 从库里装载。
_endpoints: dict[str, tuple[str, str]] = {}
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def normalize_endpoint(kind: str, raw: str) -> str:
    """校验并规范化接入地址；不合法抛 ValueError（中文说明）。
    url：http / https，要有主机名，不能带用户名密码（凭据不入库）、查询串、片段，去掉尾部斜杠；host：主机名或 IP，不带协议 / 端口。"""
    v = str(raw or "").strip()
    if not v:
        raise ValueError("地址不能为空")
    if len(v) > 300 or any(c.isspace() or ord(c) < 32 for c in v):
        raise ValueError("地址里不能有空白或控制字符（最长 300 个字符）")
    if kind == "url":
        if "://" not in v:                           # 不写协议（如 203.0.113.10:8443、host:8443）：按 https，缺端口补 8443
            v = _with_https(v)
        try:
            u = urlsplit(v)
            port = u.port                            # 端口不合法时抛 ValueError
        except ValueError:
            raise ValueError(f"地址格式不对：{v}") from None
        if u.scheme.lower() not in ("http", "https"):
            raise ValueError("地址要以 http:// 或 https:// 开头，如 https://192.168.56.101:8443")
        if "@" in u.netloc:
            raise ValueError("地址里不能带用户名或密码（凭据只放 ainet 主机的 secrets.env）")
        if not u.hostname:
            raise ValueError("地址里缺少主机名")
        if u.query or u.fragment or "?" in v or "#" in v:
            raise ValueError("地址里不能有查询串（?）或片段（#）")
        if port == 0:
            raise ValueError("端口不能为 0")
        return f"{u.scheme.lower()}://{u.netloc}{u.path}".rstrip("/")
    if kind == "host":
        h = v[1:-1] if v.startswith("[") and v.endswith("]") else v
        try:
            return str(ipaddress.ip_address(h))
        except ValueError:
            pass
        if "://" in v or "/" in v or ":" in v:
            raise ValueError("只填主机名或 IP（不带协议和端口），如 127.0.0.1")
        if not _HOST_RE.match(h):
            raise ValueError(f"主机名不合法：{v}")
        return h.lower()
    raise ValueError(f"未知的地址类型 {kind}")


ENSPRO_DEFAULT_PORT = 8443             # eNSP Pro 的缺省端口（与 config.ENSPRO_URL 的缺省值一致）


def _with_https(v: str) -> str:
    """不带协议的地址补成 https://…；只有主机名（没有端口）时补 :8443。不判断合法性（交给 normalize_endpoint）。
    不能用 urlsplit 判断有没有协议：enspro.lan:8443 会被拆成 scheme=enspro.lan。"""
    host, sep, rest = v.partition("/")
    try:                                             # 裸 IPv6（fd00::5）补方括号
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            host = f"[{host}]"
    except ValueError:
        pass
    has_port = (host.rsplit("]", 1)[-1].startswith(":")) if host.startswith("[") else ":" in host
    return f"https://{host}{'' if has_port else f':{ENSPRO_DEFAULT_PORT}'}{sep}{rest}"


# 自动识别 https（spec §5.2「接入设置」，里程碑 6.20）：用户写 http:// 或没写协议时，先试 TLS 握手（与 _probe_enspro 同样
# 不校验自签证书、不登录），通就用 https；不通再明文发一个 HEAD 看回的是不是 HTTP 响应，是就保留 http 并警告。
SCHEME_TIMEOUT = PROBE_TIMEOUT * 0.45  # 每一步（TCP / TLS / 明文 HTTP）的上限（秒）
NOTICE_HTTPS = "已自动改为 https"


def _insecure_tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                   # eNSP Pro 是自签证书（与 EnsproClient 的 verify=False 一致）
    return ctx


async def _tcp_ok(host: str, port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), SCHEME_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    await _close_writer(w)
    return True


async def _tls_ok(host: str, port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=_insecure_tls()), SCHEME_TIMEOUT)
    except (OSError, ssl.SSLError, asyncio.TimeoutError):
        return False
    await _close_writer(w)
    return True


async def _plain_http_ok(host: str, port: int) -> bool:
    """明文发 HEAD /，回的是 HTTP 响应（以 HTTP/ 开头）才算 http 能连上（光 TCP 能连不算）。不带任何凭据。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), SCHEME_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        w.write(f"HEAD / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii", "replace"))
        await asyncio.wait_for(w.drain(), SCHEME_TIMEOUT)
        return await asyncio.wait_for(r.read(5), SCHEME_TIMEOUT) == b"HTTP/"
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        await _close_writer(w)


async def detect_scheme(kind: str, raw: str) -> dict[str, Any]:
    """校验 + 规范化 + 自动识别 https：{value, scheme_check, normalized_from?, notice?, warning?}；格式不对抛 ValueError。
    只在 kind=url 且用户写的是 http:// 或没写协议时探测（写了 https:// 的不探测，不连）。scheme_check：https（TLS 握手成功）/
    http（明文 HTTP 响应）/ unreachable（TCP 连不上）/ unknown（能连上但既不是 TLS 也没有 HTTP 响应）/ skip（不探测）。
    都不通时按输入（没写协议按 https）。normalized_from = 原输入，只在协议 / 端口被改或补上时给。
    http://host 不带端口时按原来的 80 端口试 https（改成 https://host:80，不悄悄换到 443）。"""
    value = normalize_endpoint(kind, raw)
    src = str(raw or "").strip()
    plain = "://" not in src
    if kind != "url" or (value.startswith("https://") and not plain):
        return {"value": value, "scheme_check": "skip"}
    u = urlsplit(value)
    rest = value.split("://", 1)[1] if plain or u.port is not None else f"{u.netloc}:80{u.path}"
    https_url, http_url = f"https://{rest}", f"http://{rest}"
    host = u.hostname or ""
    port = urlsplit(https_url).port or 443
    out: dict[str, Any] = {"value": value}
    if not await _tcp_ok(host, port):
        out["scheme_check"] = "unreachable"
        if plain:
            out.update(normalized_from=src, notice="未写协议，按 https 处理（连不上，没能自动识别）")
        return out
    if await _tls_ok(host, port):
        out.update(value=https_url, scheme_check="https", normalized_from=src, notice=NOTICE_HTTPS)
        return out
    if await _plain_http_ok(host, port):
        out.update(value=http_url if plain else value, scheme_check="http",
                   warning=f"{host}:{port} 是明文 HTTP（TLS 握手不通），已按 http 保留；eNSP Pro 一般是 https，请确认地址")
        if plain:
            out.update(normalized_from=src, notice="未写协议，该端口是明文 HTTP，已按 http 处理")
        return out
    out["scheme_check"] = "unknown"
    if plain:
        out.update(normalized_from=src, notice="未写协议，按 https 处理（没能自动识别）")
    return out


def endpoint_default(key: str) -> str:
    """环境变量 / 内置缺省值（没有库里的记录时用它）。"""
    env = REGISTRY.get(key)
    if env is None or env.endpoint is None:
        return ""
    return str(getattr(config, env.endpoint.config_attr, "") or "")


def endpoint_value(key: str) -> str:
    """当前生效的接入地址：运行时覆盖（库里的记录 / AINET_LAB_FORCE）优先，否则 config 的缺省值。"""
    hit = _endpoints.get(key)
    return hit[0] if hit is not None else endpoint_default(key)


def endpoint_source(key: str) -> str:
    """db（页面上设置，存库）/ force（AINET_LAB_FORCE=1，以环境变量为准）/ env（环境变量）/ default（内置缺省值）。"""
    hit = _endpoints.get(key)
    if hit is not None:
        return hit[1]
    env = REGISTRY.get(key)
    if env is not None and env.endpoint is not None and os.environ.get(env.endpoint.env_var, "").strip():
        return "env"
    return "default"


def set_endpoint_override(key: str, value: str | None, source: str = "db") -> None:
    """只改内存里的覆盖（存库由调用方做）；value 为 None = 去掉覆盖、回到缺省值。"""
    if value is None:
        _endpoints.pop(key, None)
    else:
        _endpoints[key] = (value, source)
    _probe_cache.pop(key, None)


def load_endpoints(force: bool = False) -> None:
    """启动时（LabManager.boot）从库里装载各环境的接入地址。force（AINET_LAB_FORCE=1）且设了对应环境变量时以环境变量为准
    并写回库，与 lab:env / lab:sandbox 的规则一致。库里的记录不合法时忽略（日志警告）。"""
    import db
    _endpoints.clear()
    for env in environments():
        ep = env.endpoint
        if ep is None:
            continue
        name = SETTING_ENDPOINT.format(env.key)
        if force and os.environ.get(ep.env_var, "").strip():
            try:
                v = normalize_endpoint(ep.kind, endpoint_default(env.key))
            except ValueError as exc:
                log.warning("环境变量 %s 不合法（%s），不覆盖库里的记录", ep.env_var, exc)
            else:
                db.set_setting(name, v)
                _endpoints[env.key] = (v, "force")
                continue
        raw = db.get_setting(name)
        if raw is None or not raw.strip():
            continue
        try:
            _endpoints[env.key] = (normalize_endpoint(ep.kind, raw), "db")
        except ValueError as exc:
            log.warning("库里记录的 %s 接入地址不合法（%s），改用缺省值", env.label, exc)


def read_secret(name: str) -> str | None:
    """凭据：环境变量 → secrets 文件（与 DeepSeek Key 同一套候选路径，见 ai.secrets_candidates）。绝不写进代码、日志、接口。"""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    import ai
    for path in ai.secrets_candidates():
        v = ai.read_secrets_file(path, name)
        if v:
            return v
    return None


DEVICE_PASSWORD = "ENSPRO_DEVICE_PASSWORD"


def device_secret_name(key: str) -> str:
    """eNSP Pro 实例专属的设备 console 密码名：ENSPRO_DEVICE_PASSWORD__<环境 key 大写，非字母数字换成 _>（如 ENSPRO_2）。"""
    return f"{DEVICE_PASSWORD}__{re.sub(r'[^A-Z0-9]', '_', (key or '').upper())}"


def device_password(key: str = "enspro") -> str | None:
    """某个 eNSP Pro 实例的设备 console 密码：实例专属的 ENSPRO_DEVICE_PASSWORD__<KEY> 优先；只有内置 enspro 再回落到
    ENSPRO_DEVICE_PASSWORD（与以前一致）。各自按 read_secret 的顺序：环境变量 → secrets 文件。绝不写进日志、接口。"""
    v = read_secret(device_secret_name(key))
    if v or key != "enspro":
        return v
    return read_secret(DEVICE_PASSWORD)


def device_password_source(key: str) -> str:
    """设备密码来自哪里（不含值）：env（环境变量）/ file（secrets 文件）/ none。"""
    for n in [device_secret_name(key)] + ([DEVICE_PASSWORD] if key == "enspro" else []):
        if os.environ.get(n, "").strip():
            return "env"
        if read_secret(n):
            return "file"
    return "none"


def secrets_view(env: LabEnv) -> dict[str, bool]:
    """{凭据名: 有没有配置}——只报有无，绝不返回值。eNSP Pro 的设备密码按实例算（内置的显示 ENSPRO_DEVICE_PASSWORD，
    实例专属的 …__ENSPRO 也算配置了；用户添加的实例显示 ENSPRO_DEVICE_PASSWORD__<KEY>）。"""
    out = {}
    for name in env.secrets:
        if (env.type or env.key) == "enspro" and name in (DEVICE_PASSWORD, device_secret_name(env.key)):
            out[name] = bool(device_password(env.key))
        else:
            out[name] = bool(read_secret(name))
    return out


_local_names: tuple[float, set[str]] | None = None


def _local_addresses() -> set[str]:
    """本机主机名与各地址（缓存 60 秒；解析失败只剩主机名）。"""
    global _local_names
    now = time.monotonic()
    if _local_names is not None and now - _local_names[0] < 60:
        return _local_names[1]
    names: set[str] = set()
    try:
        hn = socket.gethostname()
        names |= {hn.lower(), socket.getfqdn().lower()}
        for info in socket.getaddrinfo(hn, None):
            names.add(str(info[4][0]).split("%")[0].lower())
    except OSError:
        pass
    _local_names = (now, names)
    return names


def is_local_host(host: str) -> bool:
    """host 是不是 ainet 本机（环回地址、localhost、本机主机名或本机地址）。"""
    h = str(host or "").strip().strip("[]").lower()
    if not h or h == "localhost":
        return True
    try:
        if ipaddress.ip_address(h).is_loopback:
            return True
    except ValueError:
        pass
    return h in _local_addresses()


def endpoint_view(key: str) -> dict[str, Any] | None:
    """接入设置气泡用：{kind, label, value, source, default, env_var, runtime_local}（没有可配地址的环境为 None）。
    runtime_local：设备运行状态要靠 ainet 本机检测的环境（eNSP 经典版），地址不是本机时为 False。"""
    env = get_env(key)
    ep = env.endpoint
    if ep is None:
        return None
    value = endpoint_value(env.key)
    return {"kind": ep.kind, "label": ep.label, "value": value, "source": endpoint_source(env.key),
            "default": endpoint_default(env.key), "env_var": ep.env_var,
            "runtime_local": is_local_host(value) if env.local_runtime else None}


# ---- 启动看门狗参数（按环境单独配置，spec §5.2「接入设置」/ §10.5）----
#
# 存 app_settings 的 lab:watch:<环境>（JSON {参数: 值}，只含页面上设置过的参数；注意与 lab:power 不同——那是看门狗的
# 跟踪状态，labpower.SETTING_POWER）。优先级与接入地址一致：库里的记录优先，没有则环境变量（AINET_ENSPRO_START_TIMEOUT 等）
# 缺省，再没有用内置缺省；AINET_LAB_FORCE=1 时设了的环境变量优先（启动时写回库）。

SETTING_WATCH = "lab:watch:{}"
# 运行时覆盖 {环境: {参数: (值, 来源)}}，来源 db / force；没有覆盖的参数用 config 的缺省值（每次现读）。
_watch: dict[str, dict[str, tuple[float, str]]] = {}


def _watch_param(env: LabEnv, name: str) -> WatchParam:
    p = next((p for p in env.watch if p.name == name), None)
    if p is None:
        raise ValueError(f"{env.label} 没有看门狗参数 {name!r}（可选 {' / '.join(x.name for x in env.watch) or '无'}）")
    return p


def _num(p: WatchParam, v: Any) -> float | int:
    return int(round(float(v))) if p.integer else float(v)


def normalize_watch(p: WatchParam, raw: Any) -> float | int:
    """校验一个看门狗参数；不合法抛 ValueError（中文说明）。"""
    if isinstance(raw, bool):
        raise ValueError(f"{p.label}要填数字")
    try:
        v = float(str(raw).strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{p.label}要填数字：{raw!r}") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{p.label}要填数字：{raw!r}")
    if p.integer and v != int(v):
        raise ValueError(f"{p.label}只能是整数：{raw}")
    if p.zero_off and v == 0:
        return _num(p, 0)
    if not p.lo <= v <= p.hi:
        off = "（0 = 关闭）" if p.zero_off else ""
        raise ValueError(f"{p.label}要在 {p.lo:g}~{p.hi:g} {p.unit}之间{off}：{raw}")
    return _num(p, v)


def check_watch(values: dict[str, Any]) -> None:
    """参数之间的约束：早期卡死检查（及其 2 倍）要早于启动超时才有意义。"""
    ec, st = values.get("early_check"), values.get("start_timeout")
    if ec and st and ec >= st:
        raise ValueError(f"早期卡死检查（{ec:g} 秒）要小于启动超时（{st:g} 秒），或填 0 关闭")


def watch_default(key: str, name: str) -> float | int:
    """环境变量 / 内置缺省值（没有库里的记录时用它）。"""
    env = get_env(key)
    p = _watch_param(env, name)
    return _num(p, getattr(config, p.config_attr, p.builtin))


def watch_value(key: str, name: str) -> float | int:
    hit = _watch.get(key, {}).get(name)
    return hit[0] if hit is not None else watch_default(key, name)


def watch_values(key: str) -> dict[str, float | int]:
    """该环境当前生效的全部看门狗参数 {参数: 值}（没有看门狗的环境为空）。"""
    env = get_env(key)
    return {p.name: watch_value(env.key, p.name) for p in env.watch}


def watch_source(key: str, name: str) -> str:
    """db（页面上设置）/ force（AINET_LAB_FORCE=1）/ env（环境变量）/ default（内置缺省值）。"""
    hit = _watch.get(key, {}).get(name)
    if hit is not None:
        return hit[1]
    p = _watch_param(get_env(key), name)
    return "env" if os.environ.get(p.env_var, "").strip() else "default"


def watch_saved(key: str) -> dict[str, float | int]:
    """要存库的部分：库里有记录的参数（来源 db / force）。"""
    return {n: v for n, (v, _) in _watch.get(key, {}).items()}


def set_watch_override(key: str, name: str, value: float | int | None, source: str = "db") -> None:
    """只改内存里的覆盖（存库由调用方做）；value 为 None = 去掉覆盖、回到环境变量 / 缺省值。"""
    if value is None:
        _watch.get(key, {}).pop(name, None)
    else:
        _watch.setdefault(key, {})[name] = (value, source)


def load_watch(force: bool = False) -> None:
    """启动时（LabManager.boot）从库里装载各环境的看门狗参数。库里的记录不合法时忽略该项（日志警告）；
    force（AINET_LAB_FORCE=1）时设了环境变量的参数以环境变量为准并写回库。"""
    import db
    _watch.clear()
    for env in environments():
        if not env.watch:
            continue
        name = SETTING_WATCH.format(env.key)
        raw = db.get_setting(name)
        data: dict[str, Any] = {}
        if raw and raw.strip():
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("不是 JSON 对象")
            except ValueError as exc:
                log.warning("库里记录的 %s 看门狗参数不合法（%s），改用缺省值", env.label, exc)
                data = {}
        for p in env.watch:
            if p.name not in data:
                continue
            try:
                _watch.setdefault(env.key, {})[p.name] = (normalize_watch(p, data[p.name]), "db")
            except ValueError as exc:
                log.warning("库里记录的 %s %s不合法（%s），改用缺省值", env.label, p.label, exc)
        if force:
            hit = False
            for p in env.watch:
                if os.environ.get(p.env_var, "").strip():
                    try:
                        v = normalize_watch(p, getattr(config, p.config_attr, p.builtin))
                    except ValueError as exc:
                        log.warning("环境变量 %s 不合法（%s），不覆盖库里的记录", p.env_var, exc)
                        continue
                    _watch.setdefault(env.key, {})[p.name] = (v, "force")
                    hit = True
            if hit:
                db.set_setting(name, json.dumps(watch_saved(env.key)))


def watch_view(key: str) -> dict[str, Any] | None:
    """接入设置气泡用：{values{参数: 值}, params[{name, label, unit, value, source, default, builtin, env_var, min, max,
    integer, zero_off}]}；没有看门狗的环境（eNSP 经典版）为 None。"""
    env = get_env(key)
    if not env.watch:
        return None
    params = [{"name": p.name, "label": p.label, "unit": p.unit, "value": watch_value(env.key, p.name),
               "source": watch_source(env.key, p.name), "default": watch_default(env.key, p.name),
               "builtin": _num(p, p.builtin), "env_var": p.env_var, "min": _num(p, p.lo), "max": _num(p, p.hi),
               "integer": p.integer, "zero_off": p.zero_off} for p in env.watch]
    return {"values": {x["name"]: x["value"] for x in params}, "params": params}


async def _resolve_host(host: str, port: int | None = None) -> str:
    """解析主机名（只查 DNS / hosts，不连任何端口）；失败返回中文原因，成功返回空串。"""
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port), PROBE_TIMEOUT * 0.6)
    except asyncio.TimeoutError:
        return f"解析主机名 {host} 超时"
    except OSError as exc:
        return f"解析不了主机名 {host}（{exc.strerror or exc}）"
    return ""


async def _probe_ensp(host: str | None = None) -> tuple[bool, str, str]:
    """eNSP 经典版：只看本机 eNSP_Client.exe 进程（AINET_ENSP_CLIENT=0/1 可覆盖）。
    绝不连设备 console 端口探测（频繁连接 / 断开曾疑似导致 eNSP 崩溃，spec §3、§14 第 9 项）。
    console 主机不是本机时：只解析主机名；设备运行状态（ensp_runtime.py 靠本机文件和进程）检测不了，除非设了覆盖。"""
    import ensp_runtime
    host = host or endpoint_value("ensp")
    local = await asyncio.to_thread(is_local_host, host)
    if not local:
        why = await _resolve_host(host)
        if why:
            return False, f"{why}。请在课程页「接入设置」里检查 eNSP console 主机", "unreachable"
        if os.environ.get("AINET_ENSP_CLIENT") is None:
            return False, (f"eNSP console 主机 {host} 不是本机：设备运行状态无法检测（需在 eNSP 主机上运行 ainet 或以后的"
                           "节点代理；远程调试可设 AINET_ENSP_CLIENT=1）"), "other"
    running = await asyncio.to_thread(ensp_runtime.ensp_client_running)
    if running is None:
        if os.name != "nt":
            return False, ("本机不是 Windows，没有运行 eNSP 经典版（eNSP 只能在 Windows 上运行；远程调试可设 "
                           "AINET_ENSP_CLIENT=1）"), "other"
        return False, "无法读取进程列表，判断不了 eNSP 是否在运行", "other"
    if not running:
        return False, "eNSP 没有运行（未找到 eNSP_Client.exe 进程），请先打开 eNSP", "other"
    return True, "", ""


async def _probe_enspro(url: str | None = None, key: str = "enspro", *,
                        check_secret: bool = True) -> tuple[bool, str, str]:
    """eNSP Pro：TCP 连接 +（https 时）TLS 握手（不校验自签证书），**不登录**（同一账号只能有一个会话，登录会挤掉别处）；
    再看该实例的设备 console 密码有没有配置（device_password(key)，只看有无；check_secret=False 时不看——添加环境前的
    「测试连接」，密码还没保存）。key：环境实例（内置 enspro 或用户添加的 enspro-N）。"""
    url = url or endpoint_value(key)
    u = urlsplit(url)
    host = u.hostname or ""
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return False, f"地址格式不对：{url}", "other"
    hint = "请确认地址正确、ainet 主机能访问 eNSP Pro 所在的网络（课程页环境列「接入设置」可改地址）"
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), PROBE_TIMEOUT * 0.45)
    except asyncio.TimeoutError:
        return False, f"连不上 {host}:{port}（超时）。{hint}", "unreachable"
    except OSError as exc:
        return False, f"连不上 {host}:{port}（{exc.strerror or exc}）。{hint}", "unreachable"
    await _close_writer(w)
    if u.scheme == "https":
        ctx = _insecure_tls()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), PROBE_TIMEOUT * 0.45)
        except asyncio.TimeoutError:
            return False, (f"{host}:{port} 端口能连上，但 TLS 握手超时（该端口是不是 http？可把地址改成 http://…）"), "tls"
        except (ssl.SSLError, ConnectionError) as exc:
            return False, (f"{host}:{port} 端口能连上，但 TLS 握手失败（{getattr(exc, 'reason', None) or exc}；"
                           "该端口是不是 http？）"), "tls"
        except OSError as exc:
            return False, f"连不上 {host}:{port}（{exc.strerror or exc}）。{hint}", "unreachable"
        await _close_writer(w)
    if check_secret and not await asyncio.to_thread(device_password, key):
        if key == "enspro":
            return False, ("缺少设备 console 密码 ENSPRO_DEVICE_PASSWORD：在 ainet 主机的 secrets.env（backend/data/secrets.env 或 "
                           "~/.config/ainet/secrets.env）或环境变量里配置后重新检测"), "missing_secret"
        return False, (f"缺少设备 console 密码 {device_secret_name(key)}：在课程页该环境的「⚙ 接入设置」里填写，或在 ainet "
                       "主机的 secrets.env / 环境变量里配置后重新检测"), "missing_secret"
    return True, "", ""


async def _close_writer(w: asyncio.StreamWriter) -> None:
    w.close()
    try:
        await w.wait_closed()
    except Exception:
        pass


def _make_ensp(opts: dict[str, Any]) -> LabProvider:
    return EnspProvider(opts.get("connector_factory"))


def _make_enspro(opts: dict[str, Any]) -> LabProvider:
    from enspro import EnsproProvider
    return EnsproProvider(**{k: v for k, v in opts.items() if k != "connector_factory"})


register(LabEnv("ensp", "eNSP", "vrp5", _make_ensp, _probe_ensp, power=False, order=0,
                endpoint=Endpoint("host", "console 主机", "ENSP_HOST", "ENSP_HOST"), local_runtime=True, type="ensp"))
register(LabEnv("enspro", "eNSP Pro", "yunshan", _make_enspro, _probe_enspro, power=True, sandboxes=True, order=1,
                endpoint=Endpoint("url", "服务地址", "AINET_ENSPRO_URL", "ENSPRO_URL"),
                secrets=(DEVICE_PASSWORD,), watch=ENSPRO_WATCH, type="enspro"))
BUILTIN = frozenset(REGISTRY)


# ---- 用户添加的环境实例（里程碑 6.19，课程页环境列「+」）----
#
# 模拟环境是独立节点（2026-10-07 用户定）：可能有多台 eNSP Pro（局域网或公网），每台各有地址、设备 console 密码、启动
# 超时、设备上限。内置的 enspro 不变（兼容库里已有的 lab:env / lab:sandbox:enspro / lab:endpoint:enspro / lab:watch:enspro）；
# 另外添加的实例存 app_settings 的 lab:envs（JSON 列表 [{key, type, label}]），启动时（LabManager.boot 最先）注册。
# 每个实例的其他设置沿用按环境配置的那一套键：lab:endpoint:<key>（地址，必填，没有环境变量缺省）、lab:watch:<key>
# （看门狗参数与设备上限；不受全局 AINET_ENSPRO_* 环境变量影响，缺省 = 内置值）、lab:sandbox:<key>；设备 console 密码
# ENSPRO_DEVICE_PASSWORD__<KEY>（页面上填写时写进 secrets 文件）。key 形如 enspro-2、enspro-3，编号只增不复用
# （lab:envs:seq），删掉的实例留下的基线 / 设置不会被新实例继承。题目按 platform 归属，同一平台的题对所有实例都可见。

SETTING_ENVS = "lab:envs"
SETTING_ENVS_SEQ = "lab:envs:seq"
_KEY_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9]{1,6}$")
LABEL_MAX = 40
# 用户添加的实例的看门狗参数：与内置同一套范围与内置缺省，但不读环境变量（env_var / config_attr 为空）
INSTANCE_WATCH = tuple(replace(p, env_var="", config_attr="") for p in ENSPRO_WATCH)


@dataclass(frozen=True)
class EnvType:
    """可添加的环境类型（目前只有 eNSP Pro；以后其他类型在 ENV_TYPES 里加一项）。build(key, label, order) 建 LabEnv。"""
    key: str
    label: str
    platform: str
    endpoint_kind: str
    endpoint_label: str
    placeholder: str
    watch: tuple[WatchParam, ...]
    device_password: bool
    build: Callable[[str, str, int], LabEnv]

    def view(self) -> dict[str, Any]:
        params = [{"name": p.name, "label": p.label, "unit": p.unit, "default": _num(p, p.builtin),
                   "min": _num(p, p.lo), "max": _num(p, p.hi), "integer": p.integer, "zero_off": p.zero_off}
                  for p in self.watch]
        return {"key": self.key, "label": self.label, "platform": self.platform, "endpoint_kind": self.endpoint_kind,
                "endpoint_label": self.endpoint_label, "placeholder": self.placeholder,
                "device_password": self.device_password, "watch": params}


def _build_enspro(key: str, label: str, order: int) -> LabEnv:
    from functools import partial
    return LabEnv(key, label, "yunshan", lambda opts: _make_enspro({**opts, "env_key": key}),
                  partial(_probe_enspro, key=key), power=True, sandboxes=True, order=order,
                  endpoint=Endpoint("url", "服务地址", "", ""), secrets=(device_secret_name(key),),
                  watch=INSTANCE_WATCH, type="enspro", custom=True)


ENV_TYPES: dict[str, EnvType] = {
    "enspro": EnvType("enspro", "eNSP Pro", "yunshan", "url", "服务地址", "https://主机:端口", INSTANCE_WATCH, True,
                      _build_enspro),
}


async def probe_type(type_: str, url: str) -> dict[str, Any]:
    """添加环境前的「测试连接」：用该类型的探测连一次给定地址（不缓存、不登录、不连设备 console；密码还没保存，不查）。"""
    t = ENV_TYPES[type_]
    if t.key == "enspro":
        probe: Callable[..., Awaitable[tuple]] = lambda: _probe_enspro(url, "", check_secret=False)  # noqa: E731
    else:                                            # pragma: no cover - 目前只有 eNSP Pro
        raise ValueError(f"类型 {type_} 不支持测试连接")
    ok, reason, code = await _run_probe(LabEnv("", t.label, t.platform, lambda o: None, probe))
    return {"available": ok, "reason": reason, "reason_code": code, "value": url, "checked_at": time.time()}


def custom_envs() -> list[LabEnv]:
    return [e for e in environments() if e.custom]


def normalize_label(raw: Any, *, exclude: str | None = None) -> str:
    """显示名：合并空白、去首尾空白，1~40 个字符，不能有控制字符，不能与其他环境重名（不分大小写）。不合法抛 ValueError。"""
    v = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not v:
        raise ValueError("显示名不能为空")
    if len(v) > LABEL_MAX or any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError(f"显示名最长 {LABEL_MAX} 个字符，不能有控制字符")
    for e in environments():
        if e.key != exclude and e.label.lower() == v.lower():
            raise ValueError(f"显示名「{v}」已被环境 {e.key} 使用")
    return v


def normalize_device_password(raw: Any) -> str:
    """设备 console 密码（写进 secrets 文件的一行）：1~128 个可打印 ASCII 字符，不能有空白、引号（可以有 #）。"""
    v = str(raw or "")
    if not 1 <= len(v) <= 128 or any(ord(c) <= 32 or ord(c) >= 127 or c in "\"'" for c in v):
        raise ValueError("设备 console 密码应为 1~128 个可打印字符，不能有空格或引号")
    return v


def save_custom_envs() -> None:
    """把现在注册着的用户实例写回 lab:envs（没有实例时删掉该键）。"""
    import db
    recs = [{"key": e.key, "type": e.type, "label": e.label} for e in custom_envs()]
    db.set_setting(SETTING_ENVS, json.dumps(recs, ensure_ascii=False) if recs else None)


def register_custom(key: str, type_: str, label: str) -> LabEnv:
    n = int(key.rsplit("-", 1)[1])
    return register(ENV_TYPES[type_].build(key, label, 10 + n))


def load_custom_envs() -> None:
    """启动时（LabManager.boot 最先）按库里的 lab:envs 注册用户实例：整体替换（库里没有的实例先去掉）；不合法的记录
    （key 格式不对、与已注册的重名、类型未知）忽略并告警。"""
    import db
    for e in custom_envs():
        unregister(e.key)
    raw = db.get_setting(SETTING_ENVS)
    if not raw or not raw.strip():
        return
    try:
        recs = json.loads(raw)
        if not isinstance(recs, list):
            raise ValueError("不是 JSON 列表")
    except ValueError as exc:
        log.warning("库里记录的用户环境列表 %s 不合法（%s），忽略", SETTING_ENVS, exc)
        return
    for r in recs:
        if not isinstance(r, dict):
            continue
        key, type_ = str(r.get("key") or ""), str(r.get("type") or "")
        label = re.sub(r"\s+", " ", str(r.get("label") or "")).strip()[:LABEL_MAX]
        if not _KEY_RE.match(key) or key in REGISTRY or type_ not in ENV_TYPES or not key.startswith(f"{type_}-"):
            log.warning("库里记录的用户环境 %r 不合法（key / 类型 / 重复），忽略", r)
            continue
        register_custom(key, type_, label or key)


def next_custom_key(type_: str) -> str:
    """新实例的 key：<类型>-N，N 从 2 起、只增不复用（lab:envs:seq 记已用过的最大编号）。调用方负责存库。"""
    import db
    used = [int(e.key.rsplit("-", 1)[1]) for e in custom_envs() if e.key.startswith(f"{type_}-")]
    try:
        seq = int(db.get_setting(SETTING_ENVS_SEQ) or 1)
    except ValueError:
        seq = 1
    n = max([seq, 1] + used) + 1
    db.set_setting(SETTING_ENVS_SEQ, str(n))
    return f"{type_}-{n}"


def make_provider(kind: str | None = None, **kw: Any) -> LabProvider:
    """按注册表新建实验后端。kw：sandbox（有沙箱的环境）、connector_factory（测试）等。"""
    kind = (kind or config.LAB_PROVIDER).strip().lower() or "ensp"
    try:
        env = get_env(kind)
    except ValueError:
        raise ValueError(f"未知的实验后端 AINET_LAB_PROVIDER={kind!r}（可选 {' / '.join(REGISTRY)}）") from None
    return env.make(dict(kw))
