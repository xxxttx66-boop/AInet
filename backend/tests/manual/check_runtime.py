"""手动检查 eNSP 运行中设备探测（不进 pytest）。在 eNSP 所在的 Windows 机器上运行：

    cd backend
    .venv\\Scripts\\python tests\\manual\\check_runtime.py [devices 目录]

缺省 devices 目录为 C:\\Users\\15111\\AppData\\Local\\eNSP\\VBoxServer\\devices（也可用
环境变量 AINET_ENSP_DEVICES_DIRS 指定）。打印找到的目录、每个 GUID 的 VBox.log 打开结果、
交换机进程设备名、topo.his 内容、最终运行中的 GUID 与所用方法、当前拓扑判定，并对照
fixtures 里的拓扑给出设备名。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# SSH 非交互运行时 stdout 是管道，编码取系统代码页，中文可能打印失败
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

DEFAULT_DIR = r"C:\Users\15111\AppData\Local\eNSP\VBoxServer\devices"
if len(sys.argv) > 1:
    os.environ["AINET_ENSP_DEVICES_DIRS"] = sys.argv[1]
else:
    os.environ.setdefault("AINET_ENSP_DEVICES_DIRS", DEFAULT_DIR)
for _k in ("AINET_ENSP_RUNNING_IDS", "AINET_ENSP_SWITCH_NAMES", "AINET_ENSP_TOPO_HIS", "AINET_ENSP_CLIENT"):
    os.environ.pop(_k, None)

import ensp_runtime  # noqa: E402
import topology  # noqa: E402

print("platform:", os.name, " user:", os.environ.get("USERNAME") or os.environ.get("USER"))
print("AINET_ENSP_DEVICES_DIRS:", os.environ["AINET_ENSP_DEVICES_DIRS"])
dirs = ensp_runtime.devices_dirs()
print("devices dirs found:", dirs or "（无，检查路径或权限）")

print("\n-- VBoxManage list runningvms --")
print("ids:", sorted(ensp_runtime._vboxmanage_ids()) or "（空：服务与 eNSP 不是同一用户时属正常）")

if os.name == "nt":
    print("\n-- lockfile 逐个 GUID --")
    detail: dict[str, str] = {}
    ids = ensp_runtime.lockfile_ids(dirs, ensp_runtime._win_open_probe, detail)
    for gid, d in sorted(detail.items()):
        print(f"{gid}: {'RUNNING' if gid in ids else 'stopped'}  [{d}]")

print("\n-- eNSP_Switch.exe 进程 --")
switches, switch_method = ensp_runtime.running_switch_names()
print("method:", switch_method, " count:", len(switches))
print("names:", sorted(switches))

print("eNSP_Client.exe running:", ensp_runtime.ensp_client_running())

print("\n-- topo.his --")
print("file:", ensp_runtime.topo_his_path())
recent = ensp_runtime.recent_topologies()
for i, p in enumerate(recent):
    print(f"  [{i}] {'exists ' if os.path.isfile(p) else 'MISSING'} {p}")

rt = ensp_runtime.running_device_ids()
print("\n== running_device_ids() ==")
print("method:", rt.method)
print("ids:", sorted(rt.ids))

cur = topology.current_running()
print("\n== 当前拓扑判定（TOPO_DIRS =", topology.config.TOPO_DIRS, "+ topo.his）==")
print("state:", cur["state"], " ensp_running:", cur["ensp_running"], " source:", cur["source"], " path:", cur["path"])
print("recent:", cur["recent"], " duplicates:", cur["duplicates"])
print("unknown_ids:", cur["unknown_ids"], " unknown_switches:", cur["unknown_switches"],
      " unmatched_switches:", cur["unmatched_switches"])
if cur["mixed"]:
    print("mixed:", cur["mixed"])

# 对照仓库自带的样例拓扑给出设备名（2-1 OSPF 的 R1/R2/R3 等）
names = {}
for p in (BACKEND / "tests" / "fixtures" / "topos").glob("*.topo"):
    for d in topology.parse_topo(str(p))["devices"]:
        names[d["id"].upper()] = f"{p.stem}/{d['name']}"
for gid in sorted(rt.ids):
    print(f"  {gid} -> {names.get(gid, '（不在样例拓扑中）')}")
