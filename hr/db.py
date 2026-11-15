# -*- coding: utf-8 -*-
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    login_id TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',       -- admin / manager / employee
    department_id INTEGER REFERENCES departments(id),
    position TEXT,
    phone TEXT,
    hire_date TEXT NOT NULL,
    work_start TEXT,                              -- NULL이면 회사 기본값
    work_end TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    location_consent INTEGER NOT NULL DEFAULT 0,
    must_change_pw INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS workplaces (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m INTEGER NOT NULL DEFAULT 200
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    work_date TEXT NOT NULL,
    check_in TEXT,
    check_out TEXT,
    in_lat REAL, in_lon REAL, in_place TEXT, in_ip TEXT,
    out_lat REAL, out_lon REAL, out_place TEXT, out_ip TEXT,
    work_type TEXT NOT NULL DEFAULT 'office',     -- office / field / remote / trip
    late_min INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    UNIQUE(user_id, work_date)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,          -- annual / comp / holiday_work / overtime / correction ...
    unit TEXT NOT NULL DEFAULT 'full',   -- full / am / pm / quarter / hours
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days REAL NOT NULL DEFAULT 0,
    minutes INTEGER NOT NULL DEFAULT 0,  -- 연장근무 신청 시간(분)
    corr_in TEXT,                        -- 근태정정: 정정 출근시각 HH:MM
    corr_out TEXT,                       -- 근태정정: 정정 퇴근시각 HH:MM
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / canceled
    approver_id INTEGER REFERENCES users(id),
    decided_at TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS leave_adjustments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    period_start TEXT NOT NULL,
    days REAL NOT NULL,
    reason TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS holidays (
    day TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS ix_att_date ON attendance(work_date);
CREATE INDEX IF NOT EXISTS ix_req_user ON requests(user_id, status);

-- ---------- 대화(메신저)
CREATE TABLE IF NOT EXISTS chat_rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'custom',     -- all(전 직원 자동 참여) / custom
    description TEXT,
    created_by INTEGER REFERENCES users(id),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS chat_members (
    room_id INTEGER NOT NULL REFERENCES chat_rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    last_read_id INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (room_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES chat_rooms(id),
    user_id INTEGER REFERENCES users(id),
    kind TEXT NOT NULL DEFAULT 'text',       -- text / status / location / photo / system
    body TEXT,
    lat REAL, lon REAL,
    file_path TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_msg_room ON chat_messages(room_id, id);

CREATE TABLE IF NOT EXISTS user_status (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    status TEXT NOT NULL,                    -- office / visit / outside / field / trip / remote / leave / off
    place TEXT,
    eta TEXT,                                -- 복귀 예정 HH:MM
    note TEXT,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "company_name": "GroupK",
    "work_start": "09:00",
    "work_end": "18:00",
    "break_min": "60",
    "late_grace_min": "10",    # 출근시각 + 10분까지는 지각 아님
    "comp_ratio": "1",         # 휴일근무 1일당 대체휴가 적립 배율 (1 또는 1.5)
    "require_location": "0",   # 1이면 사내망 또는 근무지 반경 확인 없이는 '사무실' 출근 불가
    "office_networks": "",     # 사무실 인터넷 공인 IP(또는 대역) — Wi‑Fi 출근 인증 대용
    "office_network_name": "사무실 Wi‑Fi",
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if not db.execute("SELECT 1 FROM chat_rooms WHERE kind='all'").fetchone():
        db.execute("INSERT INTO chat_rooms(name, kind, description) VALUES "
                   "('전체대화방', 'all', '전 직원이 자동으로 참여하는 대화방')")
    db.commit()


def setting(key: str) -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def audit(actor_id, action, target="", detail=""):
    get_db().execute(
        "INSERT INTO audit_log(actor_id, action, target, detail) VALUES (?,?,?,?)",
        (actor_id, action, str(target), str(detail)),
    )
