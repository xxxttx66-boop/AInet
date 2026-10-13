"""eNSP 拓扑（.topo）解析、发现，以及按 eNSP 中运行的设备确定当前拓扑。

- parse_topo(path)       解析 .topo（XML）为设备 / 连线 / 背景图形 / 文字注释。
- list_topologies()      递归扫 config.TOPO_DIRS 下的 .topo，附设备数与 console 数。
- resolve_running(host)  取 ensp_runtime 的运行证据（VBox 设备 GUID + S 系列交换机进程设备名）
                         与 eNSP 最近打开的拓扑（topo.his），确定当前拓扑文件，给出
                         state（running / none / unknown / mixed）与每台设备状态。
- 不对 console 端口做任何探测：频繁连接/断开 eNSP 的 console 服务可能使其异常（2026-10-04 曾崩溃），
  设备状态只看运行证据；端口是否就绪留到打开终端时才知道。

设备 <dev id> 是 GUID，不同拓扑文件之间不重复（直接复制的副本除外），因此能由运行中的
设备反查当前拓扑；S 系列交换机没有虚机，按设备名（同一文件内唯一）核对。console 端口号
在不同拓扑间复用，不能用来识别。

.topo 声明为 encoding="UNICODE"，ElementTree 不认，故按字节检测编码、去掉 XML
声明后再解析。接口序号 -> 名称：已实测的型号按真机排列（_REAL_MAINBOARD），其余按文件尽力推断（_IF_NAMES / _port_base）。
"""
from __future__ import annotations

import asyncio
import os
import time
import re
import xml.etree.ElementTree as ET
from typing import Any

import config
import ensp_runtime

# 递归扫描时跳过的目录（用户目录下这些目录又深又大）
_SKIP_DIRS = {"appdata", "node_modules", "__pycache__", "$recycle.bin", "windows"}
_MAX_DEPTH = 6

# interfacename -> VRP 接口名前缀
_IF_NAMES = {"GE": "GigabitEthernet", "Ethernet": "Ethernet", "Serial": "Serial"}

_XML_DECL_STR = re.compile(r"^\s*<\?xml[^>]*\?>")


# ---- 解析 ----

def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) >= 2 and raw[1] == 0 and raw[0] != 0:  # 无 BOM 的 UTF-16LE
        return raw.decode("utf-16-le")
    if len(raw) >= 2 and raw[0] == 0 and raw[1] != 0:
        return raw.decode("utf-16-be")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 中文版 eNSP 保存的注释是 GBK 字节（声明却写 UNICODE）
        return raw.decode("gb18030", errors="replace")


def _load_root(path: str) -> ET.Element:
    with open(path, "rb") as f:
        text = _decode(f.read())
    return ET.fromstring(_XML_DECL_STR.sub("", text, count=1))


def device_kind(model: str) -> str:
    m = model.upper()
    if m.startswith(("AR", "ROUTER", "NE")):
        return "router"
    if re.match(r"^(S\d|CE\d|LSW)", m):
        return "switch"
    if m.startswith("CLOUD"):
        return "cloud"
    if m.startswith(("PC", "CLIENT", "SERVER", "MCS", "STA")):
        return "pc"
    return "other"


def _port_base(kind: str) -> int:
    # AR 路由器接口从 0 编号（实测 AR1220：GE0/0/0-1、Ethernet0/0/0-7）；
    # S 系列交换机 / PC / Cloud 惯例从 1 编号（未实测，推断）。
    return 0 if kind == "router" else 1


# 主板接口的真机排列（按 display interface brief 的顺序，即 .topo 里 interfacePair 序号对应的顺序）。
# 2026-10-04 在 eNSP Campus_Network 上实测：
# - AR1220：GE0/0/0-1 + Ethernet0/0/0-7（共 10 个）。Campus_Network.topo 是旧格式，主板写成
#   Ethernet×2 + GE×4 + Serial×4（也是 10 个），按文件顺序推出 AR0 idx1 = Ethernet0/0/1，与真机不符。
#   锚点：AR0 idx0（接 LSW0）= GE0/0/0（两端配同网段地址 ping 通）；AR0 idx1 ↔ AR2 idx0 = GE0/0/1 ↔ GE0/0/0
#   （ping 通）；AR0 的 GE0/0/0、GE0/0/1 物理 up、Ethernet 全 down，AR2 只有 GE0/0/0 up（AR1/3/4 未启动），
#   都与「序号按真机接口顺序」一致。新格式文件（2-1 OSPF）的主板本来就是 GE×2 + Ethernet×8，结果不变。
# - S5700：GE0/0/1-24；LSW0 idx0（接 AR0）= GE0/0/1，物理 up，与文件推断一致。
# 只替换表里的型号、且接口总数与文件一致时的主板；其他型号 / 扩展卡仍按文件推断。
_REAL_MAINBOARD: dict[str, list[tuple[str, int, int]]] = {
    "AR1220": [("GigabitEthernet", 0, 2), ("Ethernet", 0, 8)],
    "S5700": [("GigabitEthernet", 1, 24)],
}


def _real_mainboard(model: str) -> list[str] | None:
    layout = _REAL_MAINBOARD.get(model.upper())
    if layout is None:
        return None
    return [f"{name}0/0/{start + i}" for name, start, count in layout for i in range(count)]


def _interfaces(dev: ET.Element, kind: str, model: str = "") -> list[str]:
    """按 slot 内 interface 出现顺序展开 count，得到扁平序号 -> 接口名。

    主板（isMainBoard="1"）的型号在 _REAL_MAINBOARD 里、且文件中主板接口总数与真机一致时，主板部分
    直接用真机排列（旧格式 .topo 的 slot 描述与真机不符，见上）。
    """
    base = _port_base(kind)
    # Serial 在 AR 上来自扩展卡（如 2SA），卡所在 slot 号即接口首段（推断）
    serial_slot = 0
    for slot in dev.iter("slot"):
        if slot.get("isMainBoard") == "0" and (slot.get("id") or "").isdigit():
            serial_slot = int(slot.get("id"))
            break
    counters: dict[str, int] = {}
    labels: list[str] = []
    real = _real_mainboard(model)
    for slot in dev.iter("slot"):
        if real is not None and slot.get("isMainBoard") == "1":
            total = 0
            for itf in slot.findall("interface"):
                try:
                    total += int(itf.get("count") or 0)
                except ValueError:
                    pass
            if total == len(real):
                labels.extend(real)
                continue
        for itf in slot.findall("interface"):
            try:
                count = int(itf.get("count") or 0)
            except ValueError:
                count = 0
            name = itf.get("interfacename") or ""
            prefix = _IF_NAMES.get(name)
            for _ in range(count):
                n = counters.get(name, 0)
                counters[name] = n + 1
                if prefix is None:
                    labels.append(f"port{len(labels)}")
                elif name == "Serial":
                    labels.append(f"Serial{serial_slot}/0/{n}")
                else:
                    labels.append(f"{prefix}0/0/{n + base}")
    return labels


def _f(v: str | None) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


def _i(v: str | None) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def _point(v: str | None) -> tuple[float, float]:
    x, _, y = (v or "").partition(",")
    return _f(x), _f(y)


def colorref(v: str | None) -> str:
    """Windows COLORREF（十进制，可能为负）-> '#rrggbb'：低字节 R，其次 G，再次 B。"""
    c = _i(v) & 0xFFFFFFFF
    return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"


# <shape type>：0 直线（startpt -> endpt），1 矩形，2 椭圆（外接矩形 upleftcorner + width/height）
_SHAPE_KINDS = {"0": "line", "1": "rect", "2": "ellipse"}


def parse_topo(path: str) -> dict[str, Any]:
    root = _load_root(path)
    devices: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for dev in root.iter("dev"):
        model = dev.get("model") or ""
        kind = device_kind(model)
        d = {
            "id": dev.get("id") or "",
            "name": dev.get("name") or "",
            "model": model,
            "com_port": _i(dev.get("com_port")),
            "console": _i(dev.get("com_port")),   # 统一设备标识（lab.console_of）：eNSP 经典版 = com_port
            "x": _f(dev.get("cx")),
            "y": _f(dev.get("cy")),
            "kind": kind,
            "interfaces": _interfaces(dev, kind, model),
        }
        devices.append(d)
        by_id[d["id"]] = d

    def label(dev_id: str, idx: int) -> str:
        d = by_id.get(dev_id)
        if d and 0 <= idx < len(d["interfaces"]):
            return d["interfaces"][idx]
        return f"port{idx}"

    links: list[dict[str, Any]] = []
    for line in root.iter("line"):
        src, dst = line.get("srcDeviceID") or "", line.get("destDeviceID") or ""
        for pair in line.findall("interfacePair"):
            si, ti = _i(pair.get("srcIndex")), _i(pair.get("tarIndex"))
            links.append({
                "src_id": src, "src_if": si, "src_label": label(src, si),
                "dst_id": dst, "dst_if": ti, "dst_label": label(dst, ti),
            })

    shapes: list[dict[str, Any]] = []
    for sh in root.iter("shape"):
        kind = _SHAPE_KINDS.get(sh.get("type") or "")
        if kind is None:
            continue
        x, y = _point(sh.get("upleftcorner"))
        w, h = _f(sh.get("width")), _f(sh.get("height"))
        x1, y1 = _point(sh.get("startpt"))
        x2, y2 = _point(sh.get("endpt"))
        if kind != "line" and (w <= 0 or h <= 0):
            continue  # eNSP 里点一下没拖动留下的空图形
        shapes.append({
            "kind": kind, "fill": kind != "line" and sh.get("filloption") == "1",
            "color": colorref(sh.get("color")), "x": x, "y": y, "w": w, "h": h,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "width": max(1, _i(sh.get("borderWidth"))),
        })

    notes: list[dict[str, Any]] = []
    for tip in root.iter("txttip"):
        left, top = _f(tip.get("left")), _f(tip.get("top"))
        # content 里的 &#x0D;&#x0A; 解析后是 \r\n，即多行注释
        text = (tip.get("content") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        notes.append({
            "text": text, "lines": text.split("\n"), "x": left, "y": top,
            "w": _f(tip.get("right")) - left, "h": _f(tip.get("bottom")) - top,
            "color": colorref(tip.get("txtcolor")), "bg": colorref(tip.get("txtbkcolor")),
            "size": _i(tip.get("editsize")) or 100, "font": tip.get("fontname") or "",
        })

    return {
        "name": os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "devices": devices,
        "links": links,
        "shapes": shapes,
        "notes": notes,
    }


def console_ports(topo: dict[str, Any]) -> set[int]:
    return {d["com_port"] for d in topo["devices"] if d["com_port"] > 0}


# ---- 发现 ----

_PARSE_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _parse_cached(path: str, st: os.stat_result) -> dict[str, Any]:
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSE_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    topo = parse_topo(path)
    _PARSE_CACHE[path] = (key, topo)
    return topo


# 本项目自身目录（含测试样例拓扑）不参与扫描
_APP_ROOT = os.path.normcase(os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))


def _is_link(path: str) -> bool:
    """符号链接或 Windows 联接点（如用户目录下的 "Local Settings" -> AppData\\Local）。"""
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def _walk_topo_files(top: str) -> list[str]:
    found: list[str] = []
    top_depth = top.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(top, onerror=lambda e: None):
        if dirpath.count(os.sep) - top_depth >= _MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in _SKIP_DIRS
                and not _is_link(os.path.join(dirpath, d))
                and os.path.normcase(os.path.realpath(os.path.join(dirpath, d))) != _APP_ROOT
            ]
        found += [os.path.join(dirpath, f) for f in filenames if f.lower().endswith(".topo")]
    return found


# 拓扑文件清单短缓存：(TOPO_DIRS, 时刻, 文件真实路径列表)。轮询时不必每次遍历用户目录；
# 文件内容变化靠每次 stat + _parse_cached 感知，新增 / 删除文件最多延迟 _SCAN_TTL 秒。
_SCAN_TTL = 15.0
_SCAN_CACHE: tuple[tuple[str, ...], float, list[str]] | None = None


def _walked_files() -> list[str]:
    global _SCAN_CACHE
    key, now = tuple(config.TOPO_DIRS), time.monotonic()
    if _SCAN_CACHE and _SCAN_CACHE[0] == key and now - _SCAN_CACHE[1] < _SCAN_TTL:
        return _SCAN_CACHE[2]
    seen: set[str] = set()
    files: list[str] = []
    for top in config.TOPO_DIRS:
        if not os.path.isdir(top):
            continue
        for path in _walk_topo_files(top):
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                files.append(real)
    _SCAN_CACHE = (key, now, files)
    return files


def _topo_files(extra: list[str] = ()) -> list[str]:
    """TOPO_DIRS 下扫到的文件 + extra（eNSP topo.his 里记录的、可能在扫描范围外的文件）。"""
    files = list(_walked_files())
    seen = {os.path.normcase(f) for f in files}
    for p in extra:
        if not p.lower().endswith(".topo"):
            continue
        real = os.path.realpath(p)
        if os.path.normcase(real) not in seen and os.path.isfile(real):
            seen.add(os.path.normcase(real))
            files.append(real)
    return files


def clear_scan_cache() -> None:
    global _SCAN_CACHE
    _SCAN_CACHE = None


Entry = tuple[str, float, dict[str, Any]]  # (真实路径, mtime, 解析结果)


def _scan(extra: list[str] = ()) -> list[Entry]:
    """跳过读不了 / 解析失败的文件。"""
    result = []
    for real in _topo_files(extra):
        try:
            st = os.stat(real)
            result.append((real, st.st_mtime, _parse_cached(real, st)))
        except (OSError, ET.ParseError, UnicodeDecodeError):
            continue
    return result


def list_topologies() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for real, mtime, topo in _scan():
        ports = sorted(console_ports(topo))
        result.append({
            "name": topo["name"],
            "path": real,
            "device_count": len(topo["devices"]),
            "console_count": len(ports),
            "ports": ports,
            "mtime": mtime,
        })
    result.sort(key=lambda t: (t["name"].lower(), t["path"]))
    return result


# ---- 当前拓扑：由运行中的设备确定 ----

def is_process_switch(model: str) -> bool:
    """S 系列交换机（S3700 / S5700）：不走 VirtualBox，由 eNSP_Switch.exe 进程模拟，按设备名识别。"""
    return bool(re.match(r"^S\d", model.upper()))


def _guids(topo: dict[str, Any]) -> set[str]:
    return {d["id"].upper() for d in topo["devices"] if d["id"]}


def _switch_names(topo: dict[str, Any]) -> set[str]:
    return {d["name"] for d in topo["devices"] if is_process_switch(d["model"])}


def _fits(topo: dict[str, Any], ids: set[str], switches: set[str]) -> bool:
    return ids <= _guids(topo) and switches <= _switch_names(topo)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def match_running(ids: set[str], switches: set[str], entries: list[Entry],
                  recent: list[str] = (), client: bool | None = None) -> dict[str, Any]:
    """按运行证据找当前拓扑（纯函数，不碰网络；entries 为已解析的拓扑文件）。

    运行证据 = VBox 设备 GUID（ids）∪ 运行中的 S 系列交换机设备名（switches）。
    client 为 False（确知 eNSP_Client.exe 不在运行）时 eNSP 已退出，不看残留证据，直接 none 且
    ensp_running=False；None 表示无法判断，按证据走。
    - none：没有运行证据；仍给出 eNSP 最近打开的拓扑（recent 第一个）供前端画成灰色。
    - running：先看 recent 第一个文件（eNSP 最近打开的），运行中的 GUID 都属于它、交换机名
      也都是它的 S 系列设备名即选它；否则退回全部文件：含全部运行中 GUID 的文件（没有 GUID
      时为含全部交换机名的文件），优先交换机名也对得上的，再按在 recent 里的先后、修改时间
      新者优先；其余同样满足的列入 duplicates（直接复制的副本等）。
    - unknown：没有文件能包含全部，且有运行中的 GUID / 交换机名在任何文件里都找不到——
      通常是 eNSP 里新建（或在已保存拓扑上新加设备）后未保存，提示「先保存」最能解决问题。
    - mixed：都能找到，但分属不同文件；mixed 列出各文件及其中运行的设备名。
    """
    ids = {i.upper() for i in ids}
    switches = set(switches)
    by_path = {_norm(p): (p, m, t) for p, m, t in entries}
    rank: dict[str, int] = {}
    for i, p in enumerate(recent):
        rank.setdefault(_norm(p), i)
    last = by_path.get(_norm(recent[0])) if recent else None
    base: dict[str, Any] = {
        "topo": None, "path": None, "mtime": None, "source": None,
        "recent": {"name": last[2]["name"], "path": last[0]} if last else None,
        "duplicates": [], "mixed": [], "unknown_ids": [], "unknown_switches": [],
        "unmatched_switches": [], "ensp_running": client,
    }

    def chosen(e: Entry, source: str, others: list[Entry] = ()) -> dict[str, Any]:
        return {**base, "state": "running", "topo": e[2], "path": e[0], "mtime": e[1], "source": source,
                "duplicates": [{"name": t["name"], "path": p} for p, _, t in others],
                "unmatched_switches": sorted(switches - _switch_names(e[2]))}

    if client is False or (not ids and not switches):
        if last:
            return {**base, "state": "none", "topo": last[2], "path": last[0], "mtime": last[1], "source": "his"}
        return {**base, "state": "none"}
    if last and _fits(last[2], ids, switches):
        return chosen(last, "his")

    cands = [e for e in entries if (ids <= _guids(e[2]) if ids else switches <= _switch_names(e[2]))]
    if cands:
        pool = [e for e in cands if _fits(e[2], ids, switches)] or cands
        pool.sort(key=lambda e: (rank.get(_norm(e[0]), len(rank)), -e[1], e[0]))
        return chosen(pool[0], "index", pool[1:])

    all_ids = set().union(*(_guids(t) for _, _, t in entries)) if entries else set()
    all_sw = set().union(*(_switch_names(t) for _, _, t in entries)) if entries else set()
    if ids - all_ids or switches - all_sw:
        return {**base, "state": "unknown", "unknown_ids": sorted(ids - all_ids),
                "unknown_switches": sorted(switches - all_sw)}
    mixed = []
    for path, _, topo in entries:
        got = [d["name"] for d in topo["devices"] if d["id"].upper() in ids
               or (not ids and _norm(path) in rank and d["name"] in switches and is_process_switch(d["model"]))]
        if got:
            mixed.append({"name": topo["name"], "path": path, "devices": sorted(got)})
    mixed.sort(key=lambda x: (-len(x["devices"]), x["path"]))
    return {**base, "state": "mixed", "mixed": mixed}


_REASONS = {
    "none": "eNSP 中没有运行的设备，请在 eNSP 中启动设备",
    "not_running": "eNSP 未运行，请先启动 eNSP 并打开拓扑",
    "unknown": "eNSP 中运行的拓扑未保存，请先在 eNSP 中保存",
    "mixed": "eNSP 中运行的设备分属不同拓扑文件，请只运行同一个拓扑的设备",
}


def state_reason(state: str) -> str:
    return _REASONS.get(state, state)


def _message(m: dict[str, Any], running: int, total: int) -> str:
    st = m["state"]
    if st == "running":
        text = f"eNSP 当前拓扑：{m['topo']['name']}（运行 {running} / {total}）"
        if m["duplicates"]:
            text += f"；另有 {len(m['duplicates'])} 个文件也包含这些运行中的设备，已按最近打开 / 修改时间选取"
        return text
    if st == "none" and m.get("ensp_running") is False:
        recent = f"（最近打开：{m['topo']['name']}）" if m["topo"] is not None else ""
        return _REASONS["not_running"] + recent
    if st == "none" and m["topo"] is not None:
        return f"eNSP 最近打开：{m['topo']['name']}；{_REASONS['none']}"
    if st == "mixed":
        parts = "；".join(f"{x['name']}：{'、'.join(x['devices'])}" for x in m["mixed"][:4])
        return f"{_REASONS['mixed']}（{parts}）"
    return state_reason(st)


def current_running() -> dict[str, Any]:
    """同步部分：运行证据 + 匹配结果（不探测端口）。白名单只需要这一步。"""
    rt = ensp_runtime.snapshot()
    m = match_running(rt.ids, rt.switches, _scan(rt.recent), rt.recent, rt.client)
    return {**m, "ids": rt.ids, "switches": rt.switches,
            "method": rt.method, "switch_method": rt.switch_method}


def allowed_ports(cur: dict[str, Any]) -> set[int]:
    return console_ports(cur["topo"]) if cur["state"] == "running" else set()


def _is_running(d: dict[str, Any], ids: set[str], switches: set[str]) -> bool:
    # 交换机：workspace 占用（GUID，主）或进程命令行里的设备名（辅，需权限）
    if is_process_switch(d["model"]) and d["name"] in switches:
        return True
    return d["id"].upper() in ids


async def resolve_running() -> dict[str, Any]:
    """当前拓扑与各设备状态。

    设备状态：有 console 的设备，在运行（VBox 设备看 GUID，S 系列交换机看进程设备名）为 online，
    不在运行为 stopped；无 console 的设备（PC / STA / Cloud / FRSW 等由 eNSP 主程序模拟，检测不到）
    为 na。不探测 console 端口（见模块说明），刚启动、console 尚未就绪的设备也显示 online，
    打开终端时若连不上会提示稍后重连。
    state=none 时返回的是 eNSP 最近打开的拓扑，设备全部 stopped / na。
    """
    # 目录遍历、PowerShell、CreateFileW 都是阻塞调用，放线程里，不卡住正在桥接的 console
    cur = await asyncio.to_thread(current_running)
    topo, ids, switches = cur["topo"], cur["ids"], cur["switches"]
    if cur["state"] == "none":  # 最近打开的拓扑画成灰色；eNSP 已退出时残留证据不算
        ids, switches = set(), set()
    status: dict[str, str] = {}
    running = total = 0
    if topo is not None:
        for d in topo["devices"]:
            if d["com_port"] <= 0:
                status[d["id"]] = "na"
                continue
            total += 1
            if _is_running(d, ids, switches):
                running += 1
                status[d["id"]] = "online"
            else:
                status[d["id"]] = "stopped"
        topo = {**topo, "devices": [{**d, "status": status[d["id"]]} for d in topo["devices"]]}
    return {
        "state": cur["state"],
        "message": _message(cur, running, total),
        "method": cur["method"],
        "switch_method": cur["switch_method"],
        "source": cur["source"],
        "path": cur["path"],
        "mtime": cur["mtime"],
        "recent": cur["recent"],
        "ensp_running": cur["ensp_running"],
        "running": running,
        "total": total,
        "topology": topo,
        "devices": status,
        "duplicates": cur["duplicates"],
        "mixed": cur["mixed"],
        "unknown_ids": cur["unknown_ids"],
        "unknown_switches": cur["unknown_switches"],
        "unmatched_switches": cur["unmatched_switches"],
    }


