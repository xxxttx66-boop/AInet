"""库迁移（m4 + m5 集成后）：从历史版本建的库升级到当前 schema，数据不丢、迁移幂等。

fixtures/legacy_db_schemas.json 是从历史 db.py 原样抽出的 SCHEMA 与 _MIGRATIONS（git show <rev>:backend/db.py）：
- m2_aced4c3：里程碑 2（只有建表，没有补列）；
- 3c91daf：里程碑 3 基线；
- m4：m4-reset 分支末端（多 attempt_configs 表）；
- m5：m5-ai 分支末端（attempts 多 score_status / score_json 列）。
每个版本按它当时的 init_db 建库、写入代表性数据，再用当前 db.init_db() 升级两次。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import db

LEGACY = json.loads((Path(__file__).resolve().parent / "fixtures" / "legacy_db_schemas.json").read_text(encoding="utf-8"))
NOW = "2026-10-04T12:00:00+08:00"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _build_legacy(path: Path, ver: dict) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(ver["schema"])
    for table, col, typ in ver["migrations"]:
        if col not in _cols(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("INSERT INTO students (login, name, password_plain, created_at) VALUES ('stu', '学员', 'p', ?)",
                 (NOW,))
    conn.execute("INSERT INTO scenarios (symptom, created_at) VALUES ('老题', ?)", (NOW,))
    conn.execute("INSERT INTO attempts (student_id, scenario_id, mode, started_at, ended_at, score_total, passed)"
                 " VALUES (1, 1, 'practice', ?, ?, 50, 1)", (NOW, NOW))
    conn.execute("INSERT INTO attempt_logs (attempt_id, ts, command, output) VALUES (1, ?, 'display version', 'v')",
                 (NOW,))
    if "attempt_configs" in ver["schema"]:
        conn.execute("INSERT INTO attempt_configs (attempt_id, device, kind, port, config, created_at)"
                     " VALUES (1, 'AR0', 'before', 2000, '#\nsysname AR0\n#\nreturn', ?)", (NOW,))
    if "score_status" in _cols(conn, "attempts"):
        conn.execute("UPDATE attempts SET score_status = 'done', score_json = '{\"status\": \"done\"}' WHERE id = 1")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("key", list(LEGACY))
def test_upgrade_from_legacy_db(key, db_path):
    ver = LEGACY[key]
    _build_legacy(db_path, ver)
    db.init_db()
    db.init_db()                                           # 幂等：第二次不报错、不重复加列
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(db.TABLES) <= tables
        assert {"score_status", "score_json", "reset_json"} <= _cols(conn, "attempts")
        assert "device" in _cols(conn, "attempt_logs")
        assert {"attempt_id", "device", "kind", "port", "config"} <= _cols(conn, "attempt_configs")
        # 旧数据还在
        assert conn.execute("SELECT login FROM students WHERE id = 1").fetchone() == ("stu",)
        assert conn.execute("SELECT score_total, passed FROM attempts WHERE id = 1").fetchone() == (50, 1)
        assert conn.execute("SELECT command FROM attempt_logs WHERE attempt_id = 1").fetchone() == ("display version",)
        if "attempt_configs" in ver["schema"]:
            assert db.get_configs(1) == {"AR0": "#\nsysname AR0\n#\nreturn"}
        if key == "m5":
            assert conn.execute("SELECT score_status FROM attempts WHERE id = 1").fetchone() == ("done",)
    finally:
        conn.close()
    # 升级后的库两边的新功能都能用
    db.save_config(1, "AR2", "#\nreturn", 2003)
    assert db.get_configs(1)["AR2"] == "#\nreturn"
    db.update_attempt(1, reset_json=json.dumps({"ok": True}), score_status="scoring", score_json="{}")
    att = db.get_attempt(1)
    assert json.loads(att["reset_json"]) == {"ok": True} and att["score_status"] == "scoring"
    log_id = db.add_attempt_log(1, "AR0", "display ip interface brief")
    db.set_log_output(log_id, "GE0/0/0 up")
    assert db.attempt_logs(1)[-1]["output"] == "GE0/0/0 up"
    assert db.fail_stale_scoring("重启") == 1
    db.delete_attempt(1)                                    # 连带删掉 attempt_configs / attempt_logs
    assert db.get_configs(1) == {} and db.attempt_logs(1) == []
