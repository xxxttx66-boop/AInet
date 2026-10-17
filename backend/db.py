"""SQLite 数据层（spec §13）：标准库 sqlite3，不引 ORM。

- init_db()   幂等建表（全部表一次建齐，后续里程碑直接用）。
- 学生 CRUD    创建 / 按登录名查 / 列表 / 明文校验密码（spec §12）。
- get_profile 学生档案聚合（spec §11）。
- 场景同步 / 练习记录（里程碑 3）：sync_scenarios、create_attempt、finish_attempt、add_attempt_log 等。

每次调用临时开连接，路径在调用时读 config.DB_PATH，便于测试替换。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    login          TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    password_plain TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_tags (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    category TEXT
);
CREATE TABLE IF NOT EXISTS topologies (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    ensp_ref  TEXT,
    note      TEXT
);
CREATE TABLE IF NOT EXISTS scenarios (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    topology_id    INTEGER REFERENCES topologies(id),
    symptom        TEXT,
    inject_script  TEXT,
    target_state   TEXT,
    reference_path TEXT,
    scoring_points TEXT,
    tags_json      TEXT,
    baseline_json  TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exam_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id       INTEGER NOT NULL REFERENCES students(id),
    scenario_id      INTEGER REFERENCES scenarios(id),
    mode             TEXT NOT NULL,
    exam_template_id INTEGER REFERENCES exam_templates(id),
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    score_total      REAL,
    score_result     REAL,
    score_process    REAL,
    score_side       REAL,
    hints_used       INTEGER NOT NULL DEFAULT 0,
    passed           INTEGER
);
CREATE TABLE IF NOT EXISTS attempt_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    ts         TEXT NOT NULL,
    command    TEXT,
    output     TEXT
);
CREATE TABLE IF NOT EXISTS student_mastery (
    student_id INTEGER NOT NULL REFERENCES students(id),
    tag_id     INTEGER NOT NULL REFERENCES knowledge_tags(id),
    level      REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, tag_id)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- 里程碑 4：练习开始前每台设备的整机配置快照（复位目标，服务重启后仍可复位）
CREATE TABLE IF NOT EXISTS attempt_configs (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    device     TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'before',
    port       INTEGER,
    config     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, device, kind)
);
-- 里程碑 6：考试会话（模板快照、截止时间、各题状态与种子；服务重启后据此恢复）
CREATE TABLE IF NOT EXISTS exams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL REFERENCES students(id),
    template_id   INTEGER,
    template_name TEXT,
    config_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    deadline_at   TEXT NOT NULL,
    ended_at      TEXT,
    state_json    TEXT NOT NULL,
    score_total   REAL,
    score_status  TEXT
);
-- Agent 面板（spec §16）：对话记录；练习中的对话关联 attempt_id（自由实验为 NULL），评分时作为过程分参考
CREATE TABLE IF NOT EXISTS agent_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    attempt_id INTEGER REFERENCES attempts(id),
    mode       TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tools_json TEXT,
    leaked     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_labs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id),
    env_key     TEXT NOT NULL,
    sandbox_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(env_key, sandbox_id)
);
CREATE INDEX IF NOT EXISTS idx_custom_labs_owner ON custom_labs(student_id, id);
CREATE INDEX IF NOT EXISTS idx_attempts_student ON attempts(student_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_messages_attempt ON agent_messages(attempt_id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_student ON agent_messages(student_id, id);
CREATE INDEX IF NOT EXISTS idx_attempt_logs_attempt ON attempt_logs(attempt_id);
"""

TABLES = (
    "students", "knowledge_tags", "topologies", "scenarios",
    "exam_templates", "attempts", "attempt_logs", "student_mastery",
    "app_settings", "attempt_configs", "exams", "agent_messages", "custom_labs",
)

# 学生对外字段（不含密码）。
_STUDENT_PUBLIC = "id, login, name, created_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 里程碑 3 新增列：(表, 列, 类型)。旧库按 PRAGMA table_info 补列，幂等。
_MIGRATIONS = (
    ("scenarios", "key", "TEXT"),                  # 场景 JSON 的 id（scenarios/*.json）
    ("scenarios", "title", "TEXT"),
    ("scenarios", "level", "TEXT"),
    ("scenarios", "topology_ref", "TEXT"),
    ("scenarios", "required_devices_json", "TEXT"),
    ("scenarios", "accessible_devices_json", "TEXT"),
    ("scenarios", "data_json", "TEXT"),            # 场景 JSON 全文（版本留痕）
    ("scenarios", "updated_at", "TEXT"),
    ("attempt_logs", "device", "TEXT"),
    # 里程碑 5：评分状态（scoring / done / degraded / failed）与评分全文（含 AI 点评的 JSON）
    ("attempts", "score_status", "TEXT"),
    ("attempts", "score_json", "TEXT"),
    # 里程碑 4 + 5 集成：结束时整机复位的结果（JSON：是否复位干净、无法撤销的配置行、各设备详情），评分时交给 AI
    ("attempts", "reset_json", "TEXT"),
    # 里程碑 6：参数化场景的种子与本次取值；考试的会话 id 与题号
    ("attempts", "seed", "INTEGER"),
    ("attempts", "param_values_json", "TEXT"),
    ("attempts", "exam_id", "INTEGER"),
    ("attempts", "exam_index", "INTEGER"),
    ("exam_templates", "created_at", "TEXT"),
    ("exam_templates", "updated_at", "TEXT"),
    # Agent 面板：导师回复被判定含答案级内容的次数（每次按第 3 级提示记扣分）
    ("attempts", "agent_penalties", "INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scenarios_key ON scenarios(key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exams_student ON exams(student_id, started_at)")


def init_db() -> None:
    """幂等建表 + 补列；库里没有任何学生时创建默认账号。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO students (login, name, password_plain, created_at)"
                " VALUES (?, ?, ?, ?)",
                (config.DEFAULT_LOGIN, config.DEFAULT_NAME,
                 config.DEFAULT_PASSWORD, _now()),
            )
    conn.close()


# ---- 学生 CRUD ----

class DuplicateLogin(Exception):
    """登录名已存在。"""


def create_student(login: str, name: str, password: str) -> dict[str, Any]:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO students (login, name, password_plain, created_at)"
                " VALUES (?, ?, ?, ?)",
                (login, name, password, _now()),
            )
        return get_student(cur.lastrowid)  # type: ignore[return-value]
    except sqlite3.IntegrityError as exc:
        raise DuplicateLogin(login) from exc
    finally:
        conn.close()


def get_student(student_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_STUDENT_PUBLIC} FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_student_by_login(login: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_STUDENT_PUBLIC} FROM students WHERE login = ?", (login,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_students() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_STUDENT_PUBLIC} FROM students ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_password(login: str, password: str) -> dict[str, Any] | None:
    """明文比对；成功返回学生（不含密码），失败返回 None。"""
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_STUDENT_PUBLIC}, password_plain FROM students WHERE login = ?",
            (login,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["password_plain"] != password:
        return None
    student = dict(row)
    student.pop("password_plain")
    return student


# ---- 档案聚合（spec §11） ----

def get_profile(student_id: int, recent_limit: int = 10) -> dict[str, Any] | None:
    """身份 + 进度 + 掌握度 + 最近记录 + 错题本。表空时各块为空数组/0。"""
    student = get_student(student_id)
    if student is None:
        return None
    conn = connect()
    try:
        completed = conn.execute(
            "SELECT COUNT(DISTINCT scenario_id) FROM attempts"
            " WHERE student_id = ? AND passed = 1",
            (student_id,),
        ).fetchone()[0]
        mastery = conn.execute(
            "SELECT m.tag_id, t.name AS tag_name, t.category, m.level, m.updated_at"
            " FROM student_mastery m JOIN knowledge_tags t ON t.id = m.tag_id"
            " WHERE m.student_id = ? ORDER BY t.category, t.name",
            (student_id,),
        ).fetchall()
        recent = conn.execute(
            "SELECT a.id, a.scenario_id, s.key AS scenario_key, s.title AS scenario_title,"
            " COALESCE(json_extract(s.data_json, '$.platform'), 'vrp5') AS scenario_platform,"
            " a.mode, a.exam_template_id, a.started_at, a.ended_at,"
            " a.score_total, a.score_result, a.score_process, a.score_side, a.score_status,"
            " a.hints_used, a.passed,"
            " (SELECT COUNT(*) FROM attempt_logs l WHERE l.attempt_id = a.id) AS commands"
            " FROM attempts a LEFT JOIN scenarios s ON s.id = a.scenario_id WHERE a.student_id = ?"
            # 进行中考试的各题不显示（分数与点评交卷后在成绩单里看）
            " AND (a.exam_id IS NULL OR a.exam_id NOT IN (SELECT id FROM exams WHERE status = 'running'))"
            " ORDER BY a.started_at DESC, a.id DESC LIMIT ?",
            (student_id, recent_limit),
        ).fetchall()
        exams = conn.execute(
            "SELECT id, template_id, template_name, status, started_at, ended_at, score_total, score_status"
            " FROM exams WHERE student_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (student_id, recent_limit * 2),
        ).fetchall()
        wrong = conn.execute(
            "SELECT a.scenario_id, s.key AS scenario_key, s.title AS scenario_title,"
            " COALESCE(json_extract(s.data_json, '$.platform'), 'vrp5') AS scenario_platform,"
            " s.symptom, COUNT(*) AS fail_count,"
            " MAX(a.started_at) AS last_attempt_at"
            " FROM attempts a LEFT JOIN scenarios s ON s.id = a.scenario_id"
            " WHERE a.student_id = ? AND a.passed = 0"
            " AND (a.exam_id IS NULL OR a.exam_id NOT IN (SELECT id FROM exams WHERE status = 'running'))"
            " GROUP BY a.scenario_id ORDER BY last_attempt_at DESC",
            (student_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "student": student,
        "progress": {"completed_scenarios": completed},
        "mastery": [dict(r) for r in mastery],
        "recent_attempts": [dict(r) for r in recent],
        "wrong_scenarios": [dict(r) for r in wrong],
        "exams": [dict(r) for r in exams],
    }


def scenario_progress(student_id: int) -> dict[str, str]:
    """课程页的完成状态：{场景 key: passed | failed}（没有记录的场景不出现 = 未开始）。

    练习与考试题都算；只看已结束的记录（进行中的不算），进行中考试的各题不算（交卷前不透露结果）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.key AS key, MAX(a.passed) AS passed FROM attempts a JOIN scenarios s ON s.id = a.scenario_id"
            " WHERE a.student_id = ? AND (a.ended_at IS NOT NULL OR a.passed = 1)"
            " AND (a.exam_id IS NULL OR a.exam_id NOT IN (SELECT id FROM exams WHERE status = 'running'))"
            " GROUP BY s.key",
            (student_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["key"]: "passed" if r["passed"] == 1 else "failed" for r in rows}


# ---- 场景同步（scenarios/*.json -> scenarios 表） ----

def sync_scenarios(items: list[dict[str, Any]]) -> dict[str, int]:
    """按 key upsert 场景，返回 {key: 行 id}。文件里删掉的场景保留在库中（历史记录还引用它）。"""
    import json

    ids: dict[str, int] = {}
    conn = connect()
    try:
        with conn:
            for s in items:
                row = conn.execute("SELECT id FROM scenarios WHERE key = ?", (s["id"],)).fetchone()
                vals = (
                    s["title"], str(s.get("level", "")), s["topology"],
                    json.dumps(s["required_devices"], ensure_ascii=False),
                    json.dumps(s["accessible_devices"], ensure_ascii=False),
                    s["symptom"], json.dumps(s.get("inject", {}), ensure_ascii=False),
                    json.dumps(s.get("checks", []), ensure_ascii=False),
                    s.get("reference_path", ""),
                    json.dumps(s.get("knowledge_tags", []), ensure_ascii=False),
                    json.dumps(s.get("baseline", {}), ensure_ascii=False),
                    json.dumps(s, ensure_ascii=False), _now(),
                )
                cols = ("title, level, topology_ref, required_devices_json, accessible_devices_json,"
                        " symptom, inject_script, target_state, reference_path, tags_json, baseline_json,"
                        " data_json, updated_at")
                if row:
                    sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
                    conn.execute(f"UPDATE scenarios SET {sets} WHERE id = ?", (*vals, row["id"]))
                    ids[s["id"]] = row["id"]
                else:
                    cur = conn.execute(
                        f"INSERT INTO scenarios (key, {cols}, created_at)"
                        f" VALUES (?, {', '.join('?' * len(vals))}, ?)",
                        (s["id"], *vals, _now()))
                    ids[s["id"]] = cur.lastrowid
    finally:
        conn.close()
    return ids


# ---- 练习记录（attempts / attempt_logs） ----

def create_attempt(student_id: int, scenario_db_id: int, mode: str = "practice", *, seed: int | None = None,
                   param_values: dict[str, Any] | None = None, exam_id: int | None = None,
                   exam_template_id: int | None = None, exam_index: int | None = None) -> dict[str, Any]:
    import json

    started = _now()
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO attempts (student_id, scenario_id, mode, started_at, hints_used, seed, param_values_json,"
                " exam_id, exam_template_id, exam_index) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (student_id, scenario_db_id, mode, started, seed,
                 json.dumps(param_values, ensure_ascii=False) if param_values is not None else None,
                 exam_id, exam_template_id, exam_index))
        return {"id": cur.lastrowid, "started_at": started}
    finally:
        conn.close()


def delete_attempt(attempt_id: int) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM attempt_logs WHERE attempt_id = ?", (attempt_id,))
            conn.execute("DELETE FROM attempt_configs WHERE attempt_id = ?", (attempt_id,))
            conn.execute("DELETE FROM agent_messages WHERE attempt_id = ?", (attempt_id,))
            conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
    finally:
        conn.close()


def update_attempt(attempt_id: int, **fields: Any) -> None:
    """更新 attempts 的 ended_at / passed / hints_used / score_* 等字段。"""
    allowed = {"ended_at", "passed", "hints_used", "score_total", "score_result",
               "score_process", "score_side", "score_status", "score_json", "reset_json", "agent_penalties"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    conn = connect()
    try:
        with conn:
            conn.execute(f"UPDATE attempts SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                         (*[fields[k] for k in keys], attempt_id))
    finally:
        conn.close()


def get_attempt(attempt_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def add_attempt_log(attempt_id: int, device: str, command: str, output: str | None = None,
                    ts: str | None = None) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO attempt_logs (attempt_id, ts, device, command, output) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, ts or _now(), device, command, output))
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def set_log_output(log_id: int, output: str, append: bool = False) -> None:
    """写入（append=True 时追加一行）一条命令记录的设备输出摘要。"""
    conn = connect()
    try:
        with conn:
            if append:
                conn.execute("UPDATE attempt_logs SET output = CASE WHEN output IS NULL OR output = ''"
                             " THEN ? ELSE output || char(10) || ? END WHERE id = ?", (output, output, log_id))
            else:
                conn.execute("UPDATE attempt_logs SET output = ? WHERE id = ?", (output, log_id))
    finally:
        conn.close()


def fail_stale_scoring(reason: str) -> int:
    """服务启动时调用：上次进程没跑完的评分标为失败（否则前端会一直显示「评分中」）。"""
    import json

    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE attempts SET score_status = 'failed', score_json = ? WHERE score_status = 'scoring'",
                (json.dumps({"status": "failed", "reason": reason}, ensure_ascii=False),))
        return cur.rowcount
    finally:
        conn.close()


def scenario_by_db_id(scenario_db_id: int | None) -> dict[str, Any] | None:
    """按 attempts.scenario_id 取场景 JSON 全文（库里的镜像；场景文件删掉后评分仍可用）。"""
    import json

    if scenario_db_id is None:
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT data_json FROM scenarios WHERE id = ?", (scenario_db_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not row["data_json"]:
        return None
    try:
        return json.loads(row["data_json"])
    except ValueError:
        return None


def attempt_logs(attempt_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, ts, device, command, output FROM attempt_logs WHERE attempt_id = ? ORDER BY id",
            (attempt_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_config(attempt_id: int, device: str, config_text: str, port: int | None = None,
                kind: str = "before") -> None:
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO attempt_configs (attempt_id, device, kind, port, config, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_id, device, kind)"
                " DO UPDATE SET config = excluded.config, port = excluded.port, created_at = excluded.created_at",
                (attempt_id, device, kind, port, config_text, _now()))
    finally:
        conn.close()


def get_configs(attempt_id: int, kind: str = "before") -> dict[str, str]:
    """{设备名: 配置原文}"""
    conn = connect()
    try:
        rows = conn.execute("SELECT device, config FROM attempt_configs WHERE attempt_id = ? AND kind = ?",
                            (attempt_id, kind)).fetchall()
        return {r["device"]: r["config"] for r in rows}
    finally:
        conn.close()


# ---- Agent 对话（spec §16） ----

def add_agent_message(student_id: int, attempt_id: int | None, mode: str, role: str, content: str,
                      tools_json: str | None = None, leaked: bool = False) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO agent_messages (student_id, attempt_id, mode, role, content, tools_json, leaked,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (student_id, attempt_id, mode, role, content, tools_json, 1 if leaked else 0, _now()))
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def agent_messages(*, attempt_id: int | None = None, student_id: int | None = None,
                   mode: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """按练习记录取对话（attempt_id）；或取某学员自由实验（attempt_id 为空）的最近对话（student_id）。按时间正序。"""
    conn = connect()
    try:
        if attempt_id is not None:
            rows = conn.execute("SELECT * FROM agent_messages WHERE attempt_id = ? ORDER BY id DESC LIMIT ?",
                                (attempt_id, limit)).fetchall()
        elif mode is not None:
            rows = conn.execute("SELECT * FROM agent_messages WHERE student_id = ? AND attempt_id IS NULL"
                                " AND mode = ? ORDER BY id DESC LIMIT ?", (student_id, mode, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agent_messages WHERE student_id = ? AND attempt_id IS NULL"
                                " ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


# ---- 自定义实验：只有创建者能看到或修改其沙箱 ----

def list_custom_labs(student_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM custom_labs WHERE student_id = ? ORDER BY id DESC", (student_id,))]
    finally:
        conn.close()


def get_custom_lab(lab_id: int, student_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM custom_labs WHERE id = ? AND student_id = ?",
                           (lab_id, student_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def custom_lab_at(student_id: int, env_key: str, sandbox_id: int | None) -> dict[str, Any] | None:
    if sandbox_id is None:
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM custom_labs WHERE student_id = ? AND env_key = ? AND sandbox_id = ?",
                           (student_id, env_key, sandbox_id)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_custom_lab(student_id: int, env_key: str, sandbox_id: int, name: str) -> dict[str, Any]:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO custom_labs (student_id, env_key, sandbox_id, name, created_at)"
                " VALUES (?, ?, ?, ?, ?)", (student_id, env_key, sandbox_id, name, _now()))
            row = conn.execute("SELECT * FROM custom_labs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def delete_custom_lab(lab_id: int, student_id: int) -> bool:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM agent_messages WHERE student_id = ? AND mode = ?",
                         (student_id, f"custom:{lab_id}"))
            cur = conn.execute("DELETE FROM custom_labs WHERE id = ? AND student_id = ?", (lab_id, student_id))
        return cur.rowcount > 0
    finally:
        conn.close()


def add_agent_penalty(attempt_id: int) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE attempts SET agent_penalties = COALESCE(agent_penalties, 0) + 1 WHERE id = ?",
                         (attempt_id,))
    finally:
        conn.close()


# ---- 应用设置（键值） ----

def get_setting(key: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_setting(key: str, value: str | None) -> None:
    """value 为 None 时删除该键。"""
    conn = connect()
    try:
        with conn:
            if value is None:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
    finally:
        conn.close()


def get_settings_prefix(prefix: str) -> dict[str, str]:
    """Read scoped runtime settings without treating wildcard characters as SQL patterns."""
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM app_settings WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))}
    finally:
        conn.close()


def delete_settings_prefix(prefix: str) -> int:
    """删除键以 prefix 开头的全部设置（删除用户添加的环境时清理它的干净基线 baseline:<环境>:…）。返回删除的条数。"""
    if not prefix:
        raise ValueError("prefix 不能为空")
    conn = connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM app_settings WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
            return cur.rowcount
    finally:
        conn.close()


# ---- 考试模板与考试会话（里程碑 6，spec §8） ----

def list_templates() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute("SELECT id, name, config_json, created_at, updated_at FROM exam_templates ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_template(template_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT id, name, config_json, created_at, updated_at FROM exam_templates WHERE id = ?",
                           (template_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_template(name: str, config_json: str) -> int:
    conn = connect()
    try:
        with conn:
            now = _now()
            cur = conn.execute("INSERT INTO exam_templates (name, config_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                               (name, config_json, now, now))
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def update_template(template_id: int, name: str, config_json: str) -> bool:
    conn = connect()
    try:
        with conn:
            cur = conn.execute("UPDATE exam_templates SET name = ?, config_json = ?, updated_at = ? WHERE id = ?",
                               (name, config_json, _now(), template_id))
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_template(template_id: int) -> bool:
    """删除模板。历史考试保存了模板快照（exams.config_json / template_name），不受影响；
    attempts.exam_template_id 有外键，先置空。"""
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE attempts SET exam_template_id = NULL WHERE exam_template_id = ?", (template_id,))
            cur = conn.execute("DELETE FROM exam_templates WHERE id = ?", (template_id,))
        return cur.rowcount > 0
    finally:
        conn.close()


_EXAM_FIELDS = {"status", "deadline_at", "ended_at", "state_json", "score_total", "score_status"}


def create_exam(student_id: int, template_id: int | None, template_name: str, config_json: str,
                started_at: str, deadline_at: str, state_json: str) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO exams (student_id, template_id, template_name, config_json, status, started_at,"
                " deadline_at, state_json) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (student_id, template_id, template_name, config_json, started_at, deadline_at, state_json))
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def delete_exam(exam_id: int) -> None:
    """删除一场考试（单题考试开题失败时回滚用：还没有任何作答记录）。"""
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE attempts SET exam_id = NULL WHERE exam_id = ?", (exam_id,))
            conn.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
    finally:
        conn.close()


def update_exam(exam_id: int, **fields: Any) -> None:
    keys = [k for k in fields if k in _EXAM_FIELDS]
    if not keys:
        return
    conn = connect()
    try:
        with conn:
            conn.execute(f"UPDATE exams SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                         (*[fields[k] for k in keys], exam_id))
    finally:
        conn.close()


def get_exam(exam_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def running_exams() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM exams WHERE status = 'running' ORDER BY id").fetchall()]
    finally:
        conn.close()


def list_exams(student_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, template_id, template_name, status, started_at, ended_at, score_total, score_status"
            " FROM exams WHERE student_id = ? ORDER BY started_at DESC, id DESC", (student_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def exam_attempts(exam_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM attempts WHERE exam_id = ? ORDER BY exam_index, id", (exam_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_exams_scoring() -> list[dict[str, Any]]:
    """已交卷、总分还在等各题评分汇总的考试。"""
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id FROM exams WHERE status = 'finished' AND score_status = 'scoring'").fetchall()]
    finally:
        conn.close()
