r"""eNSP 运行时探测：哪些设备正在运行、eNSP 最近打开了哪个拓扑。

eNSP 启动设备时在 <LOCALAPPDATA>\\eNSP\\VBoxServer\\devices\\<GUID>\\ 下建虚机副本
（如 AR_Clone_<GUID>），停止后目录不删，所以「目录存在」不等于「在运行」。

running_device_ids() 依次尝试：
1. override     环境变量 AINET_ENSP_RUNNING_IDS（逗号分隔 GUID，仅开发测试用）。
2. vboxmanage   VBoxManage list runningvms，从 VM 名里取 _Clone_<GUID>。只有服务与
                eNSP 属于同一 Windows 用户时才看得到虚机。
3. lockfile     <GUID>\\*\\Logs\\VBox.log 被虚机进程打开写入时，以「只读、只允许别人读」
                打开会得到 ERROR_SHARING_VIOLATION；能打开即判未运行。
非 Windows 平台（且无 override）为 unsupported，返回空集合。

S 系列交换机（S3700 / S5700）不走 VirtualBox：每台对应一个 eNSP_Switch.exe 进程。两种识别：
- workspace（主）：运行中的交换机占用 <eNSP 根目录>\VBoxServer\workspaces\<GUID>\*\flash.dat，
  同样用「只读、只允许别人读」打开判断，结果并入 running_device_ids() 的 GUID 集合。
- 进程命令行（辅）：最后一个参数是设备名（"...eNSP_Switch.exe" "sim" "system_mac=..." "LSW0"）。
  eNSP 以管理员权限运行，普通权限的服务（start.bat 启动）读不到它的命令行，只在有权限时生效。
running_switch_names() 依次尝试 override（AINET_ENSP_SWITCH_NAMES，仅开发测试用）、
PowerShell Get-CimInstance Win32_Process。同一次查询还看 eNSP 主程序
eNSP_Client.exe 是否在运行（ensp_client_running()，override：AINET_ENSP_CLIENT=0/1）。

recent_topologies()：<eNSP 根目录>\tmp\topo.his（GBK，| 分隔的 .topo 绝对路径，第一个是
最近打开的）。eNSP 根目录 = devices 目录往上两级；AINET_ENSP_TOPO_HIS 可直接指定文件。

snapshot() 汇总以上三项。接口名为将来 EVE-NG 实现预留。
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

_CACHE_TTL = 2.0
_VBOX_TIMEOUT = 5.0
_VBOXMANAGE = (r"C:\Program Files\VBoxManage.exe",
               r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe")
_CLONE = re.compile(r"_Clone_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})")
_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

# 锁文件探测结果
OPEN_OK = "ok"
OPEN_SHARING = "sharing"


@dataclass
class Runtime:
    ids: set[str]
    method: str  # vboxmanage / lockfile / override / unsupported
    detail: dict[str, str] = field(default_factory=dict)  # GUID -> 探测明细（诊断用）
    switches: set[str] = field(default_factory=set)       # 运行中的 S 系列交换机设备名
    switch_method: str = "unsupported"                     # powershell / override / unsupported
    recent: list[str] = field(default_factory=list)        # topo.his 中的 .topo 路径，最近的在前
    client: bool | None = None                             # eNSP_Client.exe 是否在运行；None 未知


def devices_dirs() -> list[str]:
    """eNSP devices 目录：AINET_ENSP_DEVICES_DIRS 优先；否则本进程 LOCALAPPDATA，再 glob 各用户。"""
    raw = os.environ.get("AINET_ENSP_DEVICES_DIRS")
    if raw is not None:
        cands = [d for d in raw.split(os.pathsep) if d.strip()]
    else:
        cands = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(os.path.join(local, "eNSP", "VBoxServer", "devices"))
        if os.name == "nt":
            cands += sorted(glob.glob(r"C:\Users\*\AppData\Local\eNSP\VBoxServer\devices"))
    seen: set[str] = set()
    result: list[str] = []
    for d in cands:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            result.append(d)
    return result


# ---- 方法一：VBoxManage ----

def parse_runningvms(text: str) -> set[str]:
    return {m.upper() for m in _CLONE.findall(text)}


def _vboxmanage_ids() -> set[str]:
    exe = next((p for p in _VBOXMANAGE if os.path.isfile(p)), None)
    if exe is None:
        return set()
    try:
        out = subprocess.run(
            [exe, "list", "runningvms"], capture_output=True, text=True,
            timeout=_VBOX_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_runningvms(out.stdout or "")


# ---- 方法二：VBox.log 是否被占用 ----

def _win_open_probe(path: str) -> str | tuple[str, int]:
    """CreateFileW(GENERIC_READ, FILE_SHARE_READ)：成功立即关闭返回 OPEN_OK；
    共享冲突(32) 返回 OPEN_SHARING；其他错误返回 ("error", code)。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.restype = ctypes.c_void_p
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    close = k32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x80000000, 1, 3, 0x80
    h = create(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        return OPEN_SHARING if code == 32 else ("error", code)
    close(h)
    return OPEN_OK


def lockfile_ids(dirs: list[str], open_probe: Callable[[str], str | tuple[str, int]],
                 detail: dict[str, str] | None = None,
                 pattern: tuple[str, ...] = ("*", "Logs", "VBox.log")) -> set[str]:
    """对每个 <GUID>\\<pattern> 匹配到的文件调 open_probe；任一被占用即判该 GUID 运行中。

    缺省 pattern 为 VBox 设备的 VBox.log；交换机用 workspace_dirs() 与 ("*", "flash.dat")。
    """
    running: set[str] = set()
    for base in dirs:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if not _GUID.match(name):
                continue
            guid = name.upper()
            logs = glob.glob(os.path.join(base, name, *pattern))
            results = []
            for log in logs:
                r = open_probe(log)
                results.append(f"{log} -> {r}")
                if r == OPEN_SHARING:
                    running.add(guid)
            if detail is not None:
                detail[guid] = "; ".join(results) or f"无 {pattern[-1]}"
    return running


def workspace_dirs(devices: list[str]) -> list[str]:
    """交换机运行目录：devices 目录的同级 workspaces。"""
    return [os.path.join(os.path.dirname(os.path.normpath(d)), "workspaces") for d in devices]


# ---- 汇总 + 短缓存 ----

_cache: tuple[float, Runtime] | None = None


def clear_cache() -> None:
    global _cache, _switch_cache
    _cache = None
    _switch_cache = None


def _detect() -> Runtime:
    if os.name != "nt":
        return Runtime(set(), "unsupported")
    dirs = devices_dirs()
    detail: dict[str, str] = {}
    ids = _vboxmanage_ids()
    method = "vboxmanage"
    if not ids:
        ids = lockfile_ids(dirs, _win_open_probe, detail)
        method = "lockfile"
    # 交换机不走 VirtualBox，按 workspace 文件占用识别，与上面互补
    sw = lockfile_ids(workspace_dirs(dirs), _win_open_probe, detail, ("*", "flash.dat"))
    return Runtime(ids | sw, f"{method}+workspace", detail)


def _env_list(name: str) -> set[str] | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


def running_device_ids() -> Runtime:
    """正在运行的设备 GUID（大写）与所用方法。"""
    global _cache
    ids = _env_list("AINET_ENSP_RUNNING_IDS")
    if ids is not None:  # 仅开发测试用；设成空串表示「没有运行的设备」
        return Runtime({i.upper() for i in ids}, "override")
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    rt = _detect()
    _cache = (now, rt)
    return rt


# ---- S 系列交换机：eNSP_Switch.exe 进程 ----

# 一次查出交换机进程与 eNSP 主程序进程：每行 "进程名|命令行"
_PROCESS_PS = ("Get-CimInstance Win32_Process -Filter "
               "\"Name='eNSP_Switch.exe' OR Name='eNSP_Client.exe'\" | "
               "ForEach-Object { $_.Name + '|' + $_.CommandLine }")
_QUOTED = re.compile(r'"([^"]*)"|(\S+)')


def parse_switch_cmdline(line: str) -> str | None:
    """'"...\\eNSP_Switch.exe" "sim" "system_mac=..." "LSW0"' -> 'LSW0'（最后一个参数）。"""
    args = [a or b for a, b in _QUOTED.findall(line.strip())]
    if len(args) < 2 or not args[0].lower().endswith("ensp_switch.exe"):
        return None
    name = args[-1]
    return name if "=" not in name and name.lower() != "sim" else None


def parse_process_list(text: str) -> tuple[set[str], bool]:
    """PowerShell 输出 -> (交换机设备名, eNSP_Client.exe 是否在运行)。"""
    names: set[str] = set()
    client = False
    for line in text.splitlines():
        proc, _, cmd = line.partition("|")
        proc = proc.strip().lower()
        if proc == "ensp_client.exe":
            client = True
        elif proc == "ensp_switch.exe":
            name = parse_switch_cmdline(cmd)
            if name:
                names.add(name)
    return names, client


# (时刻, 交换机设备名, eNSP_Client 是否运行；None 表示查询失败)
_switch_cache: tuple[float, set[str], bool | None] | None = None


def _powershell_processes() -> tuple[set[str], bool | None]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PROCESS_PS], capture_output=True, text=True,
            timeout=_VBOX_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return set(), None
    if out.returncode != 0:
        return set(), None
    return parse_process_list(out.stdout or "")


def _processes() -> tuple[set[str], bool | None]:
    global _switch_cache
    now = time.monotonic()
    if not (_switch_cache and now - _switch_cache[0] < _CACHE_TTL):
        _switch_cache = (now, *_powershell_processes())
    return _switch_cache[1], _switch_cache[2]


def running_switch_names() -> tuple[set[str], str]:
    """运行中的 S 系列交换机设备名与所用方法。"""
    names = _env_list("AINET_ENSP_SWITCH_NAMES")
    if names is not None:  # 仅开发测试用
        return names, "override"
    if os.name != "nt":
        return set(), "unsupported"
    return _processes()[0], "powershell"


def ensp_client_running() -> bool | None:
    """eNSP 主程序（eNSP_Client.exe）是否在运行；None 表示无法判断（非 Windows / 查询失败）。"""
    raw = os.environ.get("AINET_ENSP_CLIENT")
    if raw is not None:  # 仅开发测试用：1 运行 / 0 未运行
        return raw.strip() not in ("", "0", "false", "no")
    if os.name != "nt":
        return None
    return _processes()[1]


# ---- eNSP 最近打开的拓扑：topo.his ----

def parse_topo_his(raw: bytes) -> list[str]:
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    return [p.strip() for p in text.split("|") if p.strip()]


def topo_his_path() -> str | None:
    """topo.his 位置：AINET_ENSP_TOPO_HIS；否则各 eNSP 根目录下 tmp\\topo.his 中最近修改的。"""
    env = os.environ.get("AINET_ENSP_TOPO_HIS")
    if env is not None:
        return env or None
    found = []
    for d in devices_dirs():
        his = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(d))), "tmp", "topo.his")
        try:
            found.append((os.path.getmtime(his), his))
        except OSError:
            continue
    return max(found)[1] if found else None


def recent_topologies() -> list[str]:
    path = topo_his_path()
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            return parse_topo_his(f.read())
    except OSError:
        return []


def snapshot() -> Runtime:
    """运行证据（VBox 设备 GUID + 交换机进程名）与 eNSP 最近打开的拓扑。"""
    rt = running_device_ids()
    switches, switch_method = running_switch_names()
    return Runtime(rt.ids, rt.method, rt.detail, switches, switch_method, recent_topologies(),
                   ensp_client_running())
