"""Local-first entry point shared by Windows launch scripts and command-line use."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path


def load_local_config(path: Path) -> None:
    """Load private deployment overrides before importing the application.

    Process environment variables take precedence. Values are never logged.
    """
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not (name.startswith("AINET_") or name.startswith("ENSPRO_DEVICE_PASSWORD")
                           or name in {"ENSP_HOST", "DEEPSEEK_API_KEY"}):
            raise ValueError(f"本机配置第 {number} 行不是受支持的配置项")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def options(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 AInet（默认仅本机访问）")
    path = Path(os.environ.get("AINET_LOCAL_CONFIG", Path(__file__).parent / "data" / "local.env"))
    try:
        load_local_config(path)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    parser.add_argument("--host", default=os.environ.get("AINET_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("AINET_BIND_PORT", "8000"))
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1~65535 之间")
    allow = os.environ.get("AINET_ALLOW_LAN_AUTO_LOGIN", "").lower() in {"1", "true", "yes", "on"}
    if not loopback(args.host) and os.environ.get("AINET_AUTO_LOGIN", "").strip() and not allow:
        parser.error("局域网免登录需要显式设置 AINET_ALLOW_LAN_AUTO_LOGIN=1；能访问端口的人都能操作设备")
    return args


def main() -> None:
    args = options()
    if args.open_browser:
        import threading
        import webbrowser
        host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        host = f"[{host}]" if ":" in host else host
        timer = threading.Timer(1.5, webbrowser.open, args=(f"http://{host}:{args.port}/",))
        timer.daemon = True
        timer.start()
    import uvicorn
    uvicorn.run("main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
