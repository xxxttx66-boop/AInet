"""eNSP Pro 实验后端（LabProvider 实现，见 lab.py；spec §5、§10.4）。

eNSP Pro 是浏览器 + 服务端形态的模拟器（本机 KVM 虚机，https://ensp-pro.example:8443，自签证书）：
- REST：统一返回 {code, message, data}，code 0 成功。离线模式免密码登录：GET / → GET /api/login?service=<origin>
  （跟随跳转 ssoCheck?code=offline → userCheck → /），同一个 cookie jar 保存 __Host-SESSION 与 csrfToken；之后的请求带
  请求头 X-CSRF-TOKEN 与 csrfToken（值都取自 csrfToken cookie）。GET /api/getUserInfo 验证登录；
  GET /api/logout?service=<origin>&isManagement=false 退出。
- **同一账号只能有一个会话**：别处登录会把当前会话挤掉，之后接口返回 code 50070014（在其它地方重新登录）或
  50010005（尚未登录）。EnsproClient 识别这两种情况抛 EnsproSessionLost；是否自动重登由 AINET_ENSPRO_AUTO_RELOGIN
  决定（缺省关闭：页面提示「会话被挤掉」，用户点「重新登录 eNSP Pro」）。服务退出时登出，释放会话。
- 拓扑 = 配置的沙箱（AINET_ENSPRO_SANDBOX）：GET /service/ensp/sandbox/getOneSandbox?sandboxId=N，
  sandbox_to_topology() 转成前端已经会画的结构（与 topology.parse_topo 相同的字段）。
- 设备启停：POST /service/ensp/device/batchStart | batchStop | batchReboot，{deviceIds, sandboxId}。
- console：STOMP over WebSocket（connectors/enspro.py），设备键 = deviceId（lab.py「统一设备标识」）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable

import httpx

import config
from lab import LabError, LabProvider

log = logging.getLogger("ainet.enspro")

# 会话失效的返回码（实测）
SESSION_LOST_CODES = {
    50070014: "该账号在其它地方被重新登录（eNSP Pro 同一账号只允许一个会话）",
    50010005: "尚未登录或登录已过期",
}

# 设备状态码（eNSP Pro 前端常量 TOPO_COMPOMENT_STATUS.DEVICE）
DEVICE_STATUS = {
    0: "stopped",       # STOPPED
    1: "online",        # VM_READY
    2: "starting",      # VM_PENDING_ADD
    3: "error",         # VM_PENDING_ADD_ERROR
    4: "online",        # CONTAINER_READY
    5: "error",         # CONTAINER_PENDING_ADD_ERROR
    6: "starting",      # CONTAINER_PENDING_ADD
    17: "error",        # LOST
}
# 归一化坐标（-1 ~ 1）到画布坐标的比例。eNSP 经典版拓扑的画布单位接近像素，设备间距通常 100~300。
CANVAS_SCALE = 2500.0
# 启动阶段（状态码）：eNSP Pro 的 deviceStartProgress 在启动全程都是 100（2026-10-06 实测：状态 2 → 6 → 4，约 110 秒），
# 不能当进度用，界面显示「已用时 + 阶段」
START_PHASE = {2: "分配虚机", 6: "启动系统"}
# 启停请求发出后，状态多久没变过来就不再显示「启动中 / 停止中」（秒）
PENDING_TTL = 600.0


class EnsproError(Exception):
    """eNSP Pro 接口返回非 0 或网络错误。"""

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class EnsproSessionLost(EnsproError):
    """会话被挤掉 / 未登录（code 50070014 / 50010005），且未开启自动重登或重登间隔未到。"""


def device_password(key: str = "enspro") -> str | None:
    """设备 console 密码（按环境实例，里程碑 6.19）：ENSPRO_DEVICE_PASSWORD__<KEY> 优先，内置 enspro 再回落到
    ENSPRO_DEVICE_PASSWORD；各自环境变量 → secrets 文件（与 DeepSeek Key 同一套候选路径，见 ai.secrets_candidates；
    读取逻辑在 lab.device_password / read_secret）。绝不写进代码、日志或提交。"""
    import lab
    return lab.device_password(key)


# ---- REST 客户端 ----

class EnsproClient:
    """eNSP Pro REST 客户端（单会话）。transport 可换成 httpx.MockTransport（测试）。"""

    def __init__(self, base: str, *, auto_relogin: bool = False, relogin_interval: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base = base.rstrip("/")
        self.auto_relogin = auto_relogin
        self.relogin_interval = relogin_interval
        self.clock = clock
        self._http = httpx.AsyncClient(base_url=self.base, verify=False, follow_redirects=True,
                                       timeout=timeout, transport=transport)
        self.logged_in = False
        self.lost_reason = ""            # 非空 = 会话被挤掉，等待重登
        self.login_error: tuple[str, str] | None = None   # 最近一次登录失败（原因分类, 说明），登录成功清空
        self.user: dict[str, Any] | None = None
        self.logins = 0                  # 登录次数（测试 / 诊断）
        self._last_login = 0.0
        self._lock = asyncio.Lock()

    @property
    def origin(self) -> str:
        return self.base

    @property
    def ws_url(self) -> str:
        return ("wss://" + self.base[len("https://"):] if self.base.startswith("https://")
                else "ws://" + self.base[len("http://"):] if self.base.startswith("http://") else self.base)

    def csrf(self) -> str:
        for c in self._http.cookies.jar:
            if c.name == "csrfToken":
                return c.value or ""
        return ""

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self._http.cookies.jar)

    def _headers(self) -> dict[str, str]:
        t = self.csrf()
        return {"X-CSRF-TOKEN": t, "csrfToken": t}

    async def _login(self) -> None:
        # Stale gateway cookies can otherwise reuse the invalidated SSO session.
        self.logged_in = False
        self.user = None
        self._http.cookies.clear()
        self._last_login = self.clock()
        self.logins += 1
        try:
            await self._http.get("/")
            await self._http.get("/api/login", params={"service": self.origin})
            r = await self._http.get("/api/getUserInfo", headers=self._headers())
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.logged_in = False
            msg = f"登录 eNSP Pro 失败：{type(exc).__name__}: {exc}"
            text = f"{type(exc).__name__} {exc}".lower()
            self.login_error = ("tls" if "ssl" in text or "certificate" in text else
                                "unreachable" if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)) else
                                "other", msg)
            raise EnsproError(msg) from None
        if not isinstance(body, dict) or body.get("code") != 0:
            self.logged_in = False
            code = body.get("code") if isinstance(body, dict) else None
            msg = f"登录 eNSP Pro 失败：{body.get('message') if isinstance(body, dict) else body}"
            self.login_error = ("auth", msg + (f"（code {code}）" if code is not None else ""))
            raise EnsproError(msg, code)
        self.user = body.get("data")
        self.logged_in, self.lost_reason, self.login_error = True, "", None
        log.info("eNSP Pro 已登录：%s", (self.user or {}).get("username"))

    async def ensure(self) -> None:
        """保证已登录。会话被挤掉后：开了自动重登且距上次登录超过间隔才重登，否则抛 EnsproSessionLost。"""
        async with self._lock:
            if self.logged_in:
                return
            if self.lost_reason:
                if not self.auto_relogin:
                    raise EnsproSessionLost(self.lost_reason)
                wait = self._last_login + self.relogin_interval - self.clock()
                if wait > 0:
                    raise EnsproSessionLost(f"{self.lost_reason}（{int(wait) + 1} 秒后自动重新登录）")
                log.warning("eNSP Pro 会话失效（%s），自动重新登录", self.lost_reason)
            await self._login()

    async def relogin(self) -> None:
        """用户点「重新登录」：不受自动重登开关和间隔限制。"""
        async with self._lock:
            await self._login()

    def _lost(self, code: int, message: str) -> None:
        self.logged_in = False
        self.lost_reason = f"eNSP Pro 会话已失效：{SESSION_LOST_CODES.get(code, message)}（code {code}）"
        log.warning(self.lost_reason)

    async def call(self, method: str, path: str, body: Any = None, *, params: dict | None = None,
                   _retry: bool = True) -> Any:
        """调接口，返回 data。code 非 0 抛 EnsproError；会话失效抛 EnsproSessionLost（开了自动重登则重登后重试一次）。"""
        await self.ensure()
        login_generation = self.logins
        try:
            r = await self._http.request(method, path, json=body, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EnsproError(f"访问 eNSP Pro 失败：{type(exc).__name__}: {exc}") from None
        try:
            payload = r.json()
        except ValueError:
            raise EnsproError(f"eNSP Pro 返回的不是 JSON（HTTP {r.status_code}）", http_status=r.status_code) from None
        code = payload.get("code") if isinstance(payload, dict) else None
        if code in SESSION_LOST_CODES:
            if _retry and self.logged_in and self.logins != login_generation:
                # A response from the former session must not invalidate a completed relogin.
                return await self.call(method, path, body, params=params, _retry=False)
            self._lost(code, payload.get("message", ""))
            if self.auto_relogin and _retry:
                await self.ensure()
                return await self.call(method, path, body, params=params, _retry=False)
            raise EnsproSessionLost(self.lost_reason, code)
        if code != 0:
            raise EnsproError(f"eNSP Pro 接口 {path} 失败：{payload.get('message') if isinstance(payload, dict) else payload}"
                              f"（code {code}）", code)
        return payload.get("data")

    async def check(self) -> None:
        """确认会话仍有效（建 console 连接前调用：被挤掉时 WebSocket 握手可能照样成功，但收不到任何输出）。"""
        await self.call("GET", "/api/getUserInfo")

    async def logout(self) -> None:
        if not self.logged_in:
            return
        try:
            await self._http.get("/api/logout", params={"service": self.origin, "isManagement": "false"})
            log.info("eNSP Pro 已退出登录")
        except httpx.HTTPError:
            pass
        self.logged_in = False

    async def aclose(self) -> None:
        await self.logout()
        await self._http.aclose()

    # ---- 业务接口 ----

    async def sandbox(self, sandbox_id: int) -> dict[str, Any]:
        return await self.call("GET", "/service/ensp/sandbox/getOneSandbox", params={"sandboxId": sandbox_id})

    async def sandboxes(self, project_id: int | None = None) -> list[dict[str, Any]]:
        """全部沙箱（含设备）：POST /service/ensp/sandbox/getByPage（从前端 bundle 确认，2026-10-06 真机实测）。
        body {currentPage, pageSize, orderBy, orderDirection, projectId}；不带 projectId 返回 code 10000「参数缺失」。
        离线账号的 getUserInfo 里没有 projectId，沙箱自身的 projectId 是 1，所以缺省用 1。data 是沙箱列表，
        每项带 totalPages / totalElements。"""
        pid = project_id or (self.user or {}).get("projectId") or 1
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.call("POST", "/service/ensp/sandbox/getByPage", {
                "currentPage": page, "pageSize": 50, "orderBy": "createTime", "orderDirection": "descending",
                "projectId": int(pid)})
            rows = data if isinstance(data, list) else (data or {}).get("records") or []
            out += [r for r in rows if isinstance(r, dict)]
            pages = int((rows[0].get("totalPages") if rows else 1) or 1)
            if page >= pages or not rows or page >= 20:
                break
            page += 1
        return out

    async def devices_action(self, action: str, sandbox_id: int, device_ids: list[int]) -> Any:
        path = {"start": "batchStart", "stop": "batchStop", "reboot": "batchReboot"}[action]
        return await self.call("POST", f"/service/ensp/device/{path}",
                               {"deviceIds": list(device_ids), "sandboxId": sandbox_id})

    async def create_link(self, sandbox_id: int, left_dev: int, left_if: int, right_dev: int, right_if: int) -> Any:
        return await self.call("POST", "/service/ensp/link/create", {
            "leftDeviceId": left_dev, "leftIfId": left_if, "rightDeviceId": right_dev, "rightIfId": right_if,
            "linkTypeId": 1, "sandboxId": sandbox_id})

    async def device_log(self, device_id: int) -> bytes | None:
        """设备日志包（GET /service/ensp/device/downloadLog?deviceId=N，前端「下载日志」，2026-10-07 实测）：
        zip，内含 op_<dev>-<uuid>.log（vpnf 建容器 / 启动过程）与 vpnf_<dev>-<uuid>.tar.gz（容器内 VRP 日志、系统快照）。
        设备容器还没建好时返回 JSON（code 280000010「设备虚机暂未创建」）→ None。只读。"""
        await self.ensure()
        try:
            r = await self._http.get("/service/ensp/device/downloadLog", params={"deviceId": device_id},
                                     headers=self._headers())
        except httpx.HTTPError as exc:
            raise EnsproError(f"下载设备日志失败：{type(exc).__name__}: {exc}") from None
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            return None
        return r.content

    async def device_attrs(self, device_id: int) -> dict[str, str]:
        """设备属性（PC 的地址配置等）：GET /service/ensp/device/property/refreshAttrs?deviceId=N，data 是 JSON 字符串，
        如 {"ipv4.type": "static", "ipv4.config.ip": "10.0.10.10", "ipv4.config.ipmask": "255.255.255.0",
        "ipv4.config.gateway": "10.0.10.1", "basic.mac": …, "basic.status": "Ready"}（2026-10-06 实测）。"""
        data = await self.call("GET", "/service/ensp/device/property/refreshAttrs", params={"deviceId": device_id})
        if isinstance(data, str):
            try:
                data = json.loads(data or "{}")
            except ValueError:
                data = {}
        return {str(k): "" if v is None else str(v) for k, v in (data or {}).items()}

    async def set_device_attrs(self, device_id: int, attrs: dict[str, str]) -> dict[str, str]:
        """POST /service/ensp/device/property/updateAttrs?deviceId=N，body 为属性键值（前端的 deviceConfig 表单
        也是这样提交的）；运行中的 PC 立即生效（ifconfig 可见），返回更新后的属性。"""
        data = await self.call("POST", "/service/ensp/device/property/updateAttrs", attrs, params={"deviceId": device_id})
        if isinstance(data, str):
            try:
                return {str(k): str(v) for k, v in json.loads(data or "{}").items()}
            except ValueError:
                pass
        return await self.device_attrs(device_id)


def boot_loop_reason(log_zip: bytes) -> str | None:
    """从设备日志包判断容器是否陷入「板卡复位死循环」（spec §14 第 21 项）：容器里出现 systemctl_reboot.log
    （`ignore systemctl reboot command …`，系统请求重启被容器忽略）。是则返回原因（带 sfed 退出码），否则 None。"""
    import io
    import re as _re
    import tarfile
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(log_zip))
        inner = next((n for n in zf.namelist() if n.endswith(".tar.gz")), None)
        if inner is None:
            return None
        tf = tarfile.open(fileobj=io.BytesIO(zf.read(inner)), mode="r:gz")
        reboot = sfed = ""
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if m.name.endswith("/systemctl_reboot.log"):
                reboot = (tf.extractfile(m).read() or b"").decode("utf-8", "replace")
            elif m.name.endswith("/service_failure_proc.log"):
                sfed = (tf.extractfile(m).read() or b"").decode("utf-8", "replace")
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, KeyError):
        return None
    if "ignore systemctl reboot" not in reboot:
        return None
    m = next((x for x in _re.finditer(r"\[(\w+)\]=== exit, service result\([^)]*\), exit code\([^)]*\), "
                                      r"exit status\((\d+)\)", sfed) if x.group(2) != "0"), None)
    who = f"（{m.group(1)} 退出码 {m.group(2)}）" if m else ""
    return f"设备系统启动早期服务异常退出{who}、请求重启被容器忽略，陷入板卡复位循环"


# ---- 沙箱 -> 拓扑（纯函数） ----

def device_kind(dev: dict[str, Any]) -> str:
    dt = dev.get("deviceType") or {}
    sup = ((dt.get("superDeviceType") or {}).get("name") or "").lower()
    icon = (dt.get("iconName") or dt.get("displayName") or dt.get("name") or "").upper()
    if sup in ("route", "router") or icon.startswith(("AR", "NE")) or "-AR" in icon:
        return "router"
    if sup == "switch" or icon.startswith(("S", "CE")) and not icon.startswith(("STA", "SERVER")):
        return "switch"
    if icon.startswith("CLOUD") or sup == "cloud":
        return "cloud"
    if icon.startswith(("PC", "CLIENT", "SERVER", "STA", "MCS")) or sup in ("pc", "terminal", "host"):
        return "pc"
    return "other"


def mask_dotted(mask: str) -> str:
    """24 / 255.255.255.0 -> 255.255.255.0；空串原样。"""
    mask = (mask or "").strip()
    if mask.isdigit() and 0 <= int(mask) <= 32:
        n = int(mask)
        v = (0xFFFFFFFF << (32 - n)) & 0xFFFFFFFF if n else 0
        return ".".join(str((v >> s) & 255) for s in (24, 16, 8, 0))
    return mask


# PC 的 DNS 属性名（2026-10-07 真机核实）：PC-CLIENT 的设备模板（虚机 /opt/ensp_conn_service/crd/standalone-pc-sta-temp.yaml，
# boardcard ensp-pc-iface 的 ipv4 组）列出 ipv4.type / ipv4.config.dns1 / dns2 / ip / ipmask / gateway，eNSP Pro 前端 PC 表单
# 也是 dns1、dns2 两栏。updateAttrs 设 dns1 后 PC 容器的 /etc/resolv.conf 出现 nameserver，设 "" 即清空；从没设过时
# refreshAttrs 里没有这个键（按空处理）。ainet 只用首选 DNS（dns1），dns2 不动。
HOST_DNS_ATTR = "ipv4.config.dns1"


def host_cfg(attrs: dict[str, str]) -> dict[str, str]:
    """设备属性 -> {ip, mask, gateway, dns}（0.0.0.0 视为未配置；没有 DNS 属性 = 没设过 = 空）。
    dns 总是给出，这样开练快照里有 dns，复位时学员改过的 DNS 也能设回去。"""
    def val(k: str) -> str:
        v = (attrs.get(k) or "").strip()
        return "" if v == "0.0.0.0" else v
    return {"ip": val("ipv4.config.ip"), "mask": val("ipv4.config.ipmask"), "gateway": val("ipv4.config.gateway"),
            "dns": val(HOST_DNS_ATTR)}


def device_status(code: Any) -> str:
    try:
        return DEVICE_STATUS.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _xy(pos: Any) -> tuple[float, float]:
    pos = pos or {}
    try:
        return float(pos.get("positionX") or 0) * CANVAS_SCALE, float(pos.get("positionY") or 0) * CANVAS_SCALE
    except (TypeError, ValueError, AttributeError):
        return 0.0, 0.0


def _has_console(kind: str) -> bool:
    return kind not in ("cloud",)


def _shapes(items: Any) -> list[dict[str, Any]]:
    """背景图形（graphicsVOList）：字段未见实例，尽力识别矩形 / 椭圆 / 直线（坐标同为归一化）。"""
    out: list[dict[str, Any]] = []
    for g in items or []:
        if not isinstance(g, dict):
            continue
        t = str(g.get("type") or g.get("graphicsType") or g.get("shape") or "").lower()
        kind = ("ellipse" if "ellip" in t or "circle" in t else "line" if "line" in t else "rect")
        try:
            x = float(g.get("positionX", g.get("x", 0))) * CANVAS_SCALE
            y = float(g.get("positionY", g.get("y", 0))) * CANVAS_SCALE
            w = float(g.get("width", 0)) * CANVAS_SCALE
            h = float(g.get("height", 0)) * CANVAS_SCALE
        except (TypeError, ValueError):
            continue
        if kind != "line" and (w <= 0 or h <= 0):
            continue
        color = str(g.get("borderColor") or g.get("color") or "#999999")
        fill = g.get("fillColor") or g.get("backgroundColor")
        out.append({"kind": kind, "fill": bool(fill), "color": str(fill or color), "x": x, "y": y, "w": w, "h": h,
                    "x1": x, "y1": y, "x2": x + w, "y2": y + h, "width": 1})
    return out


def _notes(items: Any) -> list[dict[str, Any]]:
    """文本框（textBoxs）：尽力取文字与位置。"""
    out: list[dict[str, Any]] = []
    for t in items or []:
        if not isinstance(t, dict):
            continue
        text = str(t.get("content") or t.get("text") or t.get("value") or "").replace("\r\n", "\n").strip()
        if not text:
            continue
        x, y = _xy(t.get("position") or t)
        lines = text.split("\n")
        out.append({"text": text, "lines": lines, "x": x, "y": y, "w": max(len(l) for l in lines) * 8 + 6,
                    "h": len(lines) * 17 + 4, "color": str(t.get("fontColor") or t.get("color") or "#000000"),
                    "bg": str(t.get("backgroundColor") or "#ffffe0"), "size": 100, "font": ""})
    return out


def sandbox_to_topology(sb: dict[str, Any]) -> dict[str, Any]:
    """getOneSandbox 的 data -> 拓扑（字段同 topology.parse_topo，另有 status / progress / anchor）。

    - 设备 id 用字符串化的 deviceId（与 .topo 的 GUID 一样当作不透明 id），console = deviceId（统一设备标识）；
    - 坐标：devicePosition 是 -1~1 的归一化中心坐标，乘 CANVAS_SCALE 成画布坐标，anchor="center"（前端不再加半个图标）；
    - kind 按 deviceType 推导；interfaces 按 deviceIfs 的 serialId 排序取名字；连线按接口 id 映射成接口名；
    - 状态：1、4 online，0 stopped，2、6 starting，3、5、17 error（DEVICE_STATUS）。
    """
    devices: list[dict[str, Any]] = []
    if_name: dict[int, tuple[str, int]] = {}
    # 接口返回的设备 / 连线顺序不固定（实测两次调用 T1、T2 先后不同）：按 id 排序，拓扑指纹与绘制顺序才稳定
    for dev in sorted(sb.get("devices") or [], key=lambda d: int(d.get("id") or 0)):
        kind = device_kind(dev)
        ifs = sorted(dev.get("deviceIfs") or [], key=lambda i: (i.get("serialId", 0), i.get("id", 0)))
        names = [str(i.get("name") or f"port{n}") for n, i in enumerate(ifs)]
        for n, i in enumerate(ifs):
            if i.get("id") is not None:
                if_name[int(i["id"])] = (names[n], n)
        x, y = _xy(dev.get("devicePosition"))
        dt = dev.get("deviceType") or {}
        did = int(dev["id"])
        devices.append({
            "id": str(did), "name": str(dev.get("name") or did),
            "model": str(dt.get("name") or dt.get("displayName") or ""),
            "com_port": 0, "console": did if _has_console(kind) else 0,
            "device_id": did, "x": x, "y": y, "anchor": "center", "kind": kind, "interfaces": names,
            "state_code": dev.get("status"), "display_status": dev.get("displayStatus"),
            # 启动进度：`progress` 在启动中会变（AR 0→3→14→50→71→80，LSW 0→3→12→50，2026-10-07 实测），
            # deviceStartProgress 全程是 100；没有 progress 字段时退回 deviceStartProgress
            "progress": dev.get("progress", dev.get("deviceStartProgress")),
        })
    links: list[dict[str, Any]] = []
    for l in sorted(sb.get("links") or [], key=lambda l: int(l.get("id") or 0)):
        try:
            li, ri = int(l.get("leftIfId")), int(l.get("rightIfId"))
            ld, rd = int(l.get("leftDeviceId")), int(l.get("rightDeviceId"))
        except (TypeError, ValueError):
            continue
        ln, lidx = if_name.get(li, (f"if{li}", -1))
        rn, ridx = if_name.get(ri, (f"if{ri}", -1))
        links.append({"id": l.get("id"), "src_id": str(ld), "src_if": lidx, "src_label": ln,
                      "dst_id": str(rd), "dst_if": ridx, "dst_label": rn})
    return {
        "name": str(sb.get("name") or f"sandbox-{sb.get('id')}"),
        "path": None, "sandbox_id": sb.get("id"),
        "devices": devices, "links": links,
        "shapes": _shapes(sb.get("graphicsVOList")),
        "notes": _notes(sb.get("textBoxs") or sb.get("textBoxes")),
    }


def topo_signature(topo: dict[str, Any]) -> int:
    """拓扑结构的指纹（设备、位置、接口、连线、图形、注释；不含状态）。前端用 mtime 判断要不要重画。"""
    key = json.dumps({"n": topo["name"], "d": [(d["id"], d["name"], d["x"], d["y"], d["interfaces"])
                                               for d in topo["devices"]],
                      "l": [(l["src_id"], l["src_label"], l["dst_id"], l["dst_label"]) for l in topo["links"]],
                      "s": topo["shapes"], "t": topo["notes"]}, sort_keys=True, ensure_ascii=False)
    return int(hashlib.sha1(key.encode()).hexdigest()[:12], 16)


# ---- Provider ----

class EnsproProvider(LabProvider):
    kind = "enspro"
    platform = "yunshan"
    power = True

    def __init__(self, client: EnsproClient | None = None, sandbox: int | None = None,
                 connector_cls: Any = None, password: Callable[[], str | None] | None = None,
                 cache_ttl: float | None = None, url: str | None = None, env_key: str = "enspro",
                 **connector_kw: Any) -> None:
        # env_key：环境实例（内置 enspro，或课程页添加的 enspro-N，里程碑 6.19）；地址、设备密码、显示名、基线键都按它取。
        # kind 恒为 enspro（类型判断：connector_factory、题目就绪提示、干净基线等）。
        # 服务地址：显式给的 url → 该实例当前生效的接入地址（库里的记录 / AINET_ENSPRO_URL / 缺省，lab.endpoint_value）
        import lab
        self.env_key = env_key
        self.client = client or EnsproClient(url or lab.endpoint_value(env_key),
                                             auto_relogin=config.ENSPRO_AUTO_RELOGIN,
                                             relogin_interval=config.ENSPRO_RELOGIN_INTERVAL)
        self.sandbox_id = sandbox if sandbox is not None else config.ENSPRO_SANDBOX
        self.cache_ttl = config.ENSPRO_CACHE_TTL if cache_ttl is None else cache_ttl
        self.password = password or (lambda: device_password(env_key))
        self.secret_name = lab.DEVICE_PASSWORD if env_key == "enspro" else lab.device_secret_name(env_key)
        self._connector_cls = connector_cls
        self._connector_kw = connector_kw
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._last_topo: dict[str, Any] | None = None
        self.sandbox_name: str | None = None      # device_states() 时顺带记下的沙箱名
        self._fetch_lock = asyncio.Lock()
        # console 登录失败（密码错误等）的设备：只试一次，之后拒绝再连，直到由 ainet 重新启动该设备或服务重启
        self.auth_blocked: dict[int, str] = {}
        # 由 ainet 发起、设备状态还没变过来的启停：{deviceId: {"action", "at"}}
        self.pending: dict[int, dict[str, Any]] = {}
        self.stop_poll = 1.0               # 重启退回「停止 → 启动」时轮询停止状态的间隔（秒）
        self.host_ready_timeout = 20.0     # 设完 PC 地址后等 ipv4.status 回到 Ready 的上限（秒）

    def label(self) -> str:
        import lab
        env = lab.REGISTRY.get(self.env_key)
        return env.label if env is not None else "eNSP Pro"

    def health(self) -> tuple[str, str] | None:
        """会话被挤掉 / 登录被拒（探测只连端口看不出来，课程页环境列据此提示）。"""
        c = self.client
        if getattr(c, "lost_reason", ""):
            return "auth", c.lost_reason
        return getattr(c, "login_error", None)

    # ---- 沙箱 ----

    async def sandbox_data(self, force: bool = False) -> dict[str, Any]:
        async with self._fetch_lock:
            now = time.monotonic()
            if not force and self._cache and now - self._cache[0] < self.cache_ttl:
                return self._cache[1]
            data = await self.client.sandbox(self.sandbox_id)
            self._cache = (time.monotonic(), data)
            return data

    def invalidate(self) -> None:
        self._cache = None

    def _status(self, d: dict[str, Any]) -> str:
        st = device_status(d.get("state_code"))
        key = d["device_id"]
        p = self.pending.get(key)
        if p is not None:
            if time.monotonic() - p["at"] > PENDING_TTL:
                self.pending.pop(key, None)
            elif p["action"] == "stop":
                if st == "stopped":
                    self.pending.pop(key, None)
                else:
                    st = "stopping"
            elif p["action"] in ("start", "reboot"):
                if st == "online" and (p["action"] == "start" or time.monotonic() - p["at"] > 5):
                    self.pending.pop(key, None)
                elif st in ("stopped", "unknown", "online"):
                    st = "starting"
        return st

    def _base(self, state: str, message: str, topo: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "state": state, "message": message, "method": "enspro", "switch_method": "enspro",
            "source": "enspro", "path": None, "mtime": None, "recent": None, "ensp_running": None,
            "running": 0, "total": 0, "topology": topo, "devices": {}, "duplicates": [], "mixed": [],
            "unknown_ids": [], "unknown_switches": [], "unmatched_switches": [], "lab": self.info(),
            "progress": {}, "console_errors": {}, "start_errors": {},
        }

    async def resolve(self) -> dict[str, Any]:
        if self.sandbox_id is None:
            return self._base("none", "未选择 eNSP Pro 沙箱（管理员在顶栏选择，或设环境变量 AINET_ENSPRO_SANDBOX）")
        try:
            data = await self.sandbox_data()
        except EnsproSessionLost as exc:
            cur = self._base("session_lost", f"{exc.message}。请关闭其他 eNSP Pro 网页后点「重新登录 eNSP Pro」",
                             self._stale(self._last_topo))
            cur["relogin"] = True
            return self._finish(cur)
        except EnsproError as exc:
            return self._finish(self._base("error", f"连不上 eNSP Pro：{exc.message}", self._stale(self._last_topo)))
        topo = sandbox_to_topology(data)
        self._last_topo = topo
        cur = self._base("running", "", topo)
        status: dict[str, str] = {}
        progress: dict[str, Any] = {}
        running = total = 0
        w = self.watch
        for d in topo["devices"]:
            if d["console"] <= 0:
                status[d["id"]] = "na"
                continue
            total += 1
            st = self._status(d)
            note = None
            if w is not None:                 # 启动看门狗：排队 / 系统初始化中（initializing）/ 启动失败（start_failed）
                st, note = w.overlay(self.sandbox_id, d["device_id"], st)
            status[d["id"]] = st
            running += st == "online"
            if st in ("starting", "stopping"):
                text = self._progress_text(d, st)
                progress[d["id"]] = (note + text if note and note.endswith("· ") else note or text)
            elif note:
                progress[d["id"]] = note
        cur.update(devices=status, running=running, total=total, progress=progress,
                   topology={**topo, "devices": [{**d, "status": status.get(d["id"], "na")} for d in topo["devices"]]},
                   path=f"enspro://sandbox/{self.sandbox_id}", mtime=topo_signature(topo),
                   console_errors={str(k): v for k, v in self.auth_blocked.items()},
                   start_errors=w.start_errors(self.sandbox_id) if w is not None else {})
        n_init = sum(s == "initializing" for s in status.values())
        n_fail = sum(s == "start_failed" for s in status.values())
        cur["message"] = f"eNSP Pro 沙箱：{topo['name']}（运行 {running} / {total}）"
        if n_init:
            cur["message"] += f"；{n_init} 台系统初始化中"
        if n_fail:
            names = {d["id"]: d["name"] for d in topo["devices"]}
            cur["message"] += "；启动失败：" + "、".join(names[i] for i, s in status.items() if s == "start_failed")
        if self.auth_blocked:
            names = {d["device_id"]: d["name"] for d in topo["devices"]}
            cur["message"] += "；console 登录失败：" + "、".join(names.get(k, str(k)) for k in self.auth_blocked)
        return cur

    def _progress_text(self, d: dict[str, Any], st: str) -> str:
        """「启动中 35s（启动系统）」：已用时从 ainet 发出启动请求算起（别处发起的启动没有用时）。"""
        p = self.pending.get(d["device_id"])
        parts = ["启动中" if st == "starting" else "停止中"]
        pct = d.get("progress")
        if isinstance(pct, (int, float)) and 0 < pct < 100:
            parts.append(f"{int(pct)}%")
        if p is not None:
            parts.append(f"{int(time.monotonic() - p['at'])}s")
        text = " ".join(parts)
        phase = START_PHASE.get(d.get("state_code")) if st == "starting" else None
        return text + (f"（{phase}）" if phase else "")

    @staticmethod
    def _stale(topo: dict[str, Any] | None) -> dict[str, Any] | None:
        if topo is None:
            return None
        return {**topo, "devices": [{**d, "status": "stopped" if d["console"] > 0 else "na"} for d in topo["devices"]]}

    def _finish(self, cur: dict[str, Any]) -> dict[str, Any]:
        t = cur.get("topology")
        if t is not None:
            cur.update(path=f"enspro://sandbox/{self.sandbox_id}", mtime=topo_signature(t),
                       devices={d["id"]: d["status"] for d in t["devices"]})
        return cur

    def allowed(self, cur: dict[str, Any]) -> set[int]:
        t = cur.get("topology")
        if cur.get("state") != "running" or t is None:
            return set()
        # 系统初始化中（initializing）的设备 console 已可用（只是改配置会报 busy），终端可以打开看启动过程
        return {d["console"] for d in t["devices"] if d["console"] > 0 and d.get("status") in ("online", "initializing")}

    def deny_reason(self, cur: dict[str, Any], key: int) -> str:
        t = cur.get("topology")
        if cur.get("state") == "running" and t is not None:
            d = next((x for x in t["devices"] if x["console"] == key), None)
            if d is None:
                return f"沙箱 {t['name']} 中没有设备 {key}"
            if d.get("status") == "start_failed":
                return f"设备 {d['name']} 启动失败：{(cur.get('start_errors') or {}).get(d['id'], '')}"
            return f"设备 {d['name']} 当前状态为 {d.get('status')}，启动完成后才能打开终端"
        return cur.get("message") or "eNSP Pro 不可用"

    def device_name(self, key: int) -> str:
        t = self._last_topo
        if t is not None:
            for d in t["devices"]:
                if d["console"] == key:
                    return d["name"]
        return str(key)

    def endpoint(self, key: int) -> str:
        return f"eNSP Pro {self.device_name(key)}（沙箱 {self.sandbox_id} / 设备 {key}）"

    async def list_sandboxes(self) -> list[dict[str, Any]]:
        """沙箱列表（GET /api/lab/sandboxes）：[{id, name, devices, running, current}]，按 id 排序。"""
        try:
            pid = (self._cache[1].get("projectId") if self._cache else None)
            rows = await self.client.sandboxes(pid)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        out = []
        for sb in rows:
            try:
                sid = int(sb.get("id"))
            except (TypeError, ValueError):
                continue
            devs = [d for d in sb.get("devices") or [] if _has_console(device_kind(d))]
            out.append({"id": sid, "name": str(sb.get("name") or f"sandbox-{sid}"), "devices": len(devs),
                        "running": sum(device_status(d.get("status")) == "online" for d in devs),
                        "current": sid == self.sandbox_id})
        return sorted(out, key=lambda x: x["id"])

    async def running_elsewhere(self) -> list[dict[str, Any]]:
        """同一 eNSP Pro 上其他沙箱里占着资源的非 PC 设备（同时运行设备上限，里程碑 6.17）：getByPage 的沙箱列表带设备与
        状态，一次取全。[{sandbox, sandbox_name, id, name, kind, status}]；status 为 online / starting / error。"""
        try:
            pid = (self._cache[1].get("projectId") if self._cache else None)
            rows = await self.client.sandboxes(pid)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        out = []
        for sb in rows:
            try:
                sid = int(sb.get("id"))
            except (TypeError, ValueError):
                continue
            if sid == self.sandbox_id:
                continue
            for d in sb.get("devices") or []:
                kind, st = device_kind(d), device_status(d.get("status"))
                if kind in ("pc", "cloud") or st not in ("online", "starting", "error"):
                    continue
                try:
                    did = int(d.get("id"))
                except (TypeError, ValueError):
                    continue
                out.append({"sandbox": sid, "sandbox_name": str(sb.get("name") or f"sandbox-{sid}"), "id": did,
                            "name": str(d.get("name") or did), "kind": kind, "status": st})
        return out

    async def stop_in_sandbox(self, sandbox_id: int, ids: list[int]) -> None:
        """停止其他沙箱里的设备（batchStop 带该沙箱 id）。"""
        try:
            await self.client.devices_action("stop", int(sandbox_id), [int(i) for i in ids])
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None

    # ---- console ----

    def connector(self, key: int) -> Any:
        from connectors.enspro import EnsproConnector
        cls = self._connector_cls or EnsproConnector
        return cls(self, key, **self._connector_kw)

    async def ws_headers(self) -> dict[str, str]:
        """建 console WebSocket 前：确认 REST 会话有效，返回握手要带的 Cookie / Origin。"""
        await self.client.check()
        return {"Cookie": self.client.cookie_header(), "Origin": self.client.origin}

    def block(self, key: int, reason: str) -> None:
        self.auth_blocked[key] = reason
        log.error("设备 %s console 登录失败，已停止尝试：%s", self.device_name(key), reason)

    def blocked(self, key: int) -> str | None:
        return self.auth_blocked.get(key)

    # ---- 启停 ----

    async def device_states(self) -> dict[int, dict[str, Any]]:
        """接口原始状态（强制刷新，不叠加看门狗 / pending）：{deviceId: {name, status, code, console}}。"""
        if self.sandbox_id is None:
            raise LabError(409, "未选择 eNSP Pro 沙箱")
        try:
            data = await self.sandbox_data(force=True)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        self.sandbox_name = str(data.get("name") or f"sandbox-{self.sandbox_id}")
        out: dict[int, dict[str, Any]] = {}
        for d in data.get("devices") or []:
            try:
                did = int(d["id"])
            except (KeyError, TypeError, ValueError):
                continue
            out[did] = {"name": str(d.get("name") or did), "status": device_status(d.get("status")),
                        "code": d.get("status"), "console": _has_console(device_kind(d))}
        return out

    async def boot_failure(self, key: int) -> str | None:
        """启动中的设备是否已确定起不来（下载设备日志判断板卡复位循环，见 boot_loop_reason）。拿不到日志返回 None。"""
        try:
            data = await self.client.device_log(key)
        except EnsproError:
            return None
        return await asyncio.to_thread(boot_loop_reason, data) if data else None

    async def power_action(self, action: str, keys: list[int]) -> dict[str, Any]:
        if action not in ("start", "stop", "reboot"):
            raise LabError(400, f"未知操作：{action}")
        if self.sandbox_id is None:
            raise LabError(409, "未配置 eNSP Pro 沙箱（AINET_ENSPRO_SANDBOX）")
        try:
            data = await self.sandbox_data(force=True)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        ids = {int(d["id"]) for d in data.get("devices") or []}
        bad = [k for k in keys if k not in ids]
        if bad or not keys:
            raise LabError(404, f"沙箱 {self.sandbox_id} 中没有这些设备：{bad or '（空）'}")
        via = ""
        try:
            try:
                await self.client.devices_action(action, self.sandbox_id, keys)
            except EnsproError as exc:
                # 2026-10-06 实测：本机 eNSP Pro 的 batchReboot 返回 HTTP 405（前端有「重启」菜单，但接口未开放）。
                # 退回「停止 → 等到已停止 → 启动」，效果同冷启动（配置照样从 flash:/1.cfg 载入，见 spec §3）
                if action != "reboot" or exc.http_status != 405:
                    raise
                await self._stop_then_start(keys)
                via = "stop+start"
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        now = time.monotonic()
        for k in keys:
            self.pending[k] = {"action": action, "at": now}
            if action in ("start", "reboot"):
                self.auth_blocked.pop(k, None)     # 设备重新启动：允许再试一次登录
        self.invalidate()
        return {"ok": True, "action": action, "devices": keys, **({"via": via} if via else {})}

    async def _stop_then_start(self, keys: list[int], timeout: float = 90.0) -> None:
        await self.client.devices_action("stop", self.sandbox_id, keys)
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            data = await self.sandbox_data(force=True)
            st = {int(d["id"]): device_status(d.get("status")) for d in data.get("devices") or []}
            if all(st.get(k) == "stopped" for k in keys):
                break
            if loop.time() >= end:
                raise EnsproError(f"重启失败：设备 {keys} 在 {int(timeout)} 秒内没有停下来")
            await asyncio.sleep(self.stop_poll)
        await self.client.devices_action("start", self.sandbox_id, keys)

    # ---- 终端主机（PC-CLIENT 等，spec §10.4）----

    def is_host(self, key: int) -> bool:
        t = self._last_topo
        if t is None:
            return False
        return any(d["console"] == key and d["kind"] == "pc" for d in t["devices"])

    async def host_get(self, key: int) -> dict[str, str]:
        try:
            attrs = await self.client.device_attrs(key)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        return host_cfg(attrs)

    async def host_set(self, key: int, cfg: dict[str, str]) -> dict[str, str]:
        attrs = {"ipv4.type": "static", "ipv4.config.ip": cfg.get("ip", ""),
                 "ipv4.config.ipmask": mask_dotted(cfg.get("mask", "")), "ipv4.config.gateway": cfg.get("gateway", "")}
        if "dns" in cfg:
            attrs[HOST_DNS_ATTR] = cfg.get("dns") or ""
        try:
            got = await self.client.set_device_attrs(key, attrs)
            # 改完先是 ipv4.status = PendingUpdate，PC 的命令行此时会重启（吞掉输入），等它回到 Ready 再交回去
            loop = asyncio.get_running_loop()
            end = loop.time() + self.host_ready_timeout
            while got.get("ipv4.status", "Ready") not in ("Ready", "") and loop.time() < end:
                if got.get("ipv4.status") == "Error" or got.get("ipv4.errorCode"):
                    raise LabError(502, f"eNSP Pro 设置 PC 地址失败：{got.get('ipv4.msg') or got.get('ipv4.errorCode')}")
                await asyncio.sleep(self.stop_poll / 2)
                got = await self.client.device_attrs(key)
        except EnsproError as exc:
            raise LabError(409 if isinstance(exc, EnsproSessionLost) else 502, exc.message) from None
        return host_cfg(got)

    async def relogin(self) -> dict[str, Any]:
        try:
            await self.client.relogin()
            # Verify the simulator service, in addition to the SSO user endpoint.
            await self.client.sandboxes()
        except EnsproError as exc:
            raise LabError(502, exc.message) from None
        self.invalidate()
        return {"ok": True}

    async def close(self) -> None:
        await self.client.aclose()
