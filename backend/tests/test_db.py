import sqlite3

import pytest

import config
import db


def test_init_db_idempotent_creates_all_tables(db_path):
    db.init_db()
    db.init_db()
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert set(db.TABLES) <= names
    # 默认账号只建一次
    students = db.list_students()
    assert [s["login"] for s in students] == [config.DEFAULT_LOGIN]


def test_db_dir_auto_created(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "x.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    db.init_db()
    assert path.exists()


def test_student_crud(db_path):
    db.init_db()
    s = db.create_student("alice", "爱丽丝", "pw1")
    assert s["login"] == "alice" and "password_plain" not in s
    assert db.get_student_by_login("alice")["id"] == s["id"]
    assert db.get_student_by_login("nobody") is None
    with pytest.raises(db.DuplicateLogin):
        db.create_student("alice", "另一个", "x")
    assert db.verify_password("alice", "pw1")["id"] == s["id"]
    assert db.verify_password("alice", "bad") is None
    assert db.verify_password("nobody", "pw1") is None


def test_profile_aggregates_attempts_and_mastery(db_path):
    db.init_db()
    sid = db.create_student("bob", "鲍勃", "pw")["id"]
    with db.connect() as conn:
        conn.execute("INSERT INTO knowledge_tags (name, category) VALUES ('OSPF', '路由')")
        conn.execute("INSERT INTO student_mastery VALUES (?, 1, 0.6, '2026-10-01')", (sid,))
        conn.execute("INSERT INTO scenarios (symptom, created_at) VALUES ('ping 不通', '2026-10-01')")
        conn.execute(
            "INSERT INTO attempts (student_id, scenario_id, mode, started_at, passed)"
            " VALUES (?, 1, 'practice', '2026-10-01T01:00', 0)", (sid,))
        conn.execute(
            "INSERT INTO attempts (student_id, scenario_id, mode, started_at, passed)"
            " VALUES (?, 1, 'practice', '2026-10-01T02:00', 1)", (sid,))
    p = db.get_profile(sid)
    assert p["progress"]["completed_scenarios"] == 1
    assert p["mastery"][0]["tag_name"] == "OSPF"
    assert len(p["recent_attempts"]) == 2
    assert p["recent_attempts"][0]["passed"] == 1  # 最新在前
    assert p["wrong_scenarios"][0]["symptom"] == "ping 不通"


def test_migration_from_milestone2_db_is_idempotent(db_path):
    """里程碑 2 的旧库（只有 SCHEMA，没有新列）：init_db 两次后补齐列与索引，原有数据保留。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(db.SCHEMA)   # SCHEMA 未改动，即 aced4c3 的表结构
    conn.execute("INSERT INTO students (login, name, password_plain, created_at)"
                 " VALUES ('old', '老学生', 'pw', '2026-10-01')")
    conn.execute("INSERT INTO scenarios (symptom, created_at) VALUES ('旧场景', '2026-10-01')")
    conn.commit()
    conn.close()
    db.init_db()
    db.init_db()
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scenarios)")}
    assert {"key", "title", "level", "topology_ref", "required_devices_json",
            "accessible_devices_json", "data_json", "updated_at"} <= cols
    assert "device" in {r[1] for r in conn.execute("PRAGMA table_info(attempt_logs)")}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(scenarios)")}
    assert "idx_scenarios_key" in idx
    assert conn.execute("SELECT name FROM students WHERE login = 'old'").fetchone()[0] == "老学生"
    assert conn.execute("SELECT symptom FROM scenarios").fetchone()[0] == "旧场景"
    conn.close()
    # 同步场景后再次 init_db / 再同步不重复插入
    item = {"id": "s1", "title": "T", "topology": "x.topo", "required_devices": ["A"],
            "accessible_devices": ["A"], "symptom": "s"}
    ids = db.sync_scenarios([item])
    db.init_db()
    assert db.sync_scenarios([{**item, "title": "T2"}]) == ids
