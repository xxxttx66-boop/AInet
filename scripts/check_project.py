"""Validate scenario content and Python syntax without contacting a simulator."""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from scenarios import load_dir  # noqa: E402


def main() -> None:
    for path in (ROOT / "backend").rglob("*.py"):
        if ".venv" not in path.parts and "__pycache__" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    scenarios, errors = load_dir(ROOT / "scenarios", [])
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Python syntax and {len(scenarios)} scenarios validated without device connections.")


if __name__ == "__main__":
    main()
