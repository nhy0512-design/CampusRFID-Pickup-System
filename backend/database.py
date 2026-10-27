"""SQLite 数据访问层。"""
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "pickup.db")
DB_PATH = os.path.abspath(DB_PATH)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_no   TEXT UNIQUE,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    rfid_epc     TEXT UNIQUE,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_students_phone ON students(phone);
CREATE INDEX IF NOT EXISTS idx_students_epc   ON students(rfid_epc);

CREATE TABLE IF NOT EXISTS packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_no   TEXT,
    pickup_code   TEXT NOT NULL,
    student_id    INTEGER NOT NULL,
    shelf         TEXT,     -- 货架/存放位置（导入时可选）
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / picked
    arrived_at    INTEGER NOT NULL,
    picked_at     INTEGER,
    picked_event  TEXT,    -- 'manual' / 'auto' / 'checkout'
    FOREIGN KEY (student_id) REFERENCES students(id)
);

CREATE INDEX IF NOT EXISTS idx_packages_student ON packages(student_id);
CREATE INDEX IF NOT EXISTS idx_packages_status  ON packages(status);
CREATE INDEX IF NOT EXISTS idx_packages_code    ON packages(pickup_code);

CREATE TABLE IF NOT EXISTS card_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    epc        TEXT NOT NULL,
    student_id INTEGER,
    event      TEXT NOT NULL,    -- 'show' / 'checkout' / 'unknown_card'
    detail     TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_events_epc ON card_events(epc);

CREATE TABLE IF NOT EXISTS card_state (
    epc         TEXT PRIMARY KEY,
    state       TEXT NOT NULL,    -- 'idle' / 'shown'
    student_id  INTEGER,
    shown_at    INTEGER,
    last_seen   INTEGER           -- 最近一次读到这张卡的时间，用于判断「卡是否被拿起过」
);
"""

# 兼容老库：补一列
MIGRATIONS = [
    ("card_state", "last_seen", "ALTER TABLE card_state ADD COLUMN last_seen INTEGER"),
    ("packages", "shelf", "ALTER TABLE packages ADD COLUMN shelf TEXT"),
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s)
        # 增量迁移：给已存在的表补新列
        for table, col, ddl in MIGRATIONS:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if cols and col not in cols:
                conn.execute(ddl)


# ============== Student ==============

def add_student(student_no, name, phone, rfid_epc=None):
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO students (student_no, name, phone, rfid_epc, created_at) VALUES (?,?,?,?,?)",
            (student_no, name, phone, rfid_epc, now),
        )
        return cur.lastrowid


def update_student(student_id, **fields):
    if not fields:
        return
    allowed = {"student_no", "name", "phone", "rfid_epc"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [student_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE students SET {sets} WHERE id=?", vals)


def get_student_by_id(student_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        return dict(row) if row else None


def get_student_by_phone(phone):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM students WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None


def get_student_by_epc(epc):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM students WHERE rfid_epc=?", (epc,)).fetchone()
        return dict(row) if row else None


def list_students(keyword=None):
    with get_conn() as conn:
        if keyword:
            like = f"%{keyword}%"
            rows = conn.execute(
                "SELECT * FROM students WHERE name LIKE ? OR phone LIKE ? OR student_no LIKE ? ORDER BY id DESC",
                (like, like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM students ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def bind_card_to_student(student_id, epc):
    """把一张 RFID 卡绑定到学生。"""
    with get_conn() as conn:
        conn.execute("UPDATE students SET rfid_epc=? WHERE id=?", (epc, student_id))
        # 解绑持有此 EPC 的其他学生
        conn.execute("UPDATE students SET rfid_epc=NULL WHERE rfid_epc=? AND id<>?", (epc, student_id))


# ============== Package ==============

def add_package(tracking_no, pickup_code, student_id, shelf=None):
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO packages (tracking_no, pickup_code, student_id, shelf, status, arrived_at) "
            "VALUES (?,?,?,?,?,?)",
            (tracking_no, pickup_code, student_id, shelf, "pending", now),
        )
        return cur.lastrowid


def has_pending_code(student_id, pickup_code):
    """该学生名下是否已有同取件码的待取快递（导入去重用）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM packages WHERE student_id=? AND pickup_code=? AND status='pending' LIMIT 1",
            (student_id, pickup_code),
        ).fetchone()
        return row is not None


def list_packages(student_id=None, status=None, limit=200):
    sql = (
        "SELECT p.*, s.name AS student_name, s.phone AS student_phone, s.student_no "
        "FROM packages p JOIN students s ON s.id=p.student_id "
    )
    conds, vals = [], []
    if student_id is not None:
        conds.append("p.student_id=?")
        vals.append(student_id)
    if status:
        conds.append("p.status=?")
        vals.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY p.id DESC LIMIT ?"
    vals.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]


def pending_for_student(student_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM packages WHERE student_id=? AND status='pending' ORDER BY id",
            (student_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def checkout_all_pending(student_id, source="checkout"):
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE packages SET status='picked', picked_at=?, picked_event=? "
            "WHERE student_id=? AND status='pending'",
            (now, source, student_id),
        )
        return cur.rowcount


def reset_all_packages_pending():
    """把所有快递恢复为「待取」，并清空刷卡会话状态 —— 回到系统初始状态。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE packages SET status='pending', picked_at=NULL, picked_event=NULL "
            "WHERE status<>'pending'"
        )
        conn.execute("DELETE FROM card_state")
        return cur.rowcount


# ============== Card Event Log ==============

def log_card_event(epc, student_id, event, detail=None):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO card_events (epc, student_id, event, detail, created_at) VALUES (?,?,?,?,?)",
            (epc, student_id, event, detail, now),
        )


# ============== Card State (状态机持久化) ==============

def get_card_state(epc):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM card_state WHERE epc=?", (epc,)).fetchone()
        return dict(row) if row else None


def set_card_state(epc, state, student_id=None, shown_at=None, last_seen=None):
    with get_conn() as conn:
        if state == "idle":
            conn.execute("DELETE FROM card_state WHERE epc=?", (epc,))
        else:
            conn.execute(
                "INSERT INTO card_state (epc, state, student_id, shown_at, last_seen) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(epc) DO UPDATE SET state=excluded.state, "
                "student_id=excluded.student_id, shown_at=excluded.shown_at, "
                "last_seen=excluded.last_seen",
                (epc, state, student_id, shown_at or int(time.time()),
                 last_seen or int(time.time())),
            )


def touch_card_seen(epc):
    """只更新 last_seen（同一次刷卡期间的持续读取），不改变业务状态。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE card_state SET last_seen=? WHERE epc=?",
            (int(time.time()), epc),
        )


# ============== Stats ==============

def get_stats():
    now = int(time.time())
    today_start = now - (now % 86400)  # 粗略按 0 点分割
    with get_conn() as conn:
        total_students = conn.execute("SELECT COUNT(*) AS c FROM students").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) AS c FROM packages WHERE status='pending'").fetchone()["c"]
        today_arrived = conn.execute(
            "SELECT COUNT(*) AS c FROM packages WHERE arrived_at>=?", (today_start,)
        ).fetchone()["c"]
        today_picked = conn.execute(
            "SELECT COUNT(*) AS c FROM packages WHERE picked_at>=?", (today_start,)
        ).fetchone()["c"]
        return {
            "total_students": total_students,
            "pending": pending,
            "today_arrived": today_arrived,
            "today_picked": today_picked,
        }


if __name__ == "__main__":
    init_db()
    print("DB 初始化完成:", DB_PATH)
