"""
database.py
SQLite integration for persistent storage.

Tables:
  users        — accounts with hashed passwords
  records      — every OCR extraction result
  downloads    — export history per user

Usage:
  from database import init_db, get_db
  db = get_db()
  db.execute("SELECT * FROM records WHERE user_id=?", [uid])
"""

import sqlite3
import os
from datetime import datetime
from flask import g

DB_PATH = os.path.join(os.path.dirname(__file__), "poultry.db")


# ── Schema ─────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    UNIQUE NOT NULL,
    password   TEXT    NOT NULL,
    joined     TEXT    NOT NULL DEFAULT (strftime('%d %b %Y', 'now'))
);

CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    image_name    TEXT,
    ocr_engine    TEXT,
    elapsed       REAL,
    -- Daily record fields
    feed          INTEGER,
    eggs          INTEGER,
    mortality     INTEGER,
    birds         INTEGER,
    date_field    TEXT,
    batch         TEXT,
    -- Growth register fields
    latest_abw    INTEGER,
    latest_fcr    REAL,
    abw_values    TEXT,   -- JSON array
    fcr_values    TEXT,   -- JSON array
    medicine_notes TEXT,  -- JSON array
    -- Raw text
    raw_text      TEXT,
    cleaned_text  TEXT,
    formatted     TEXT,
    -- Timestamps
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS downloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    record_id  INTEGER REFERENCES records(id) ON DELETE SET NULL,
    filename   TEXT    NOT NULL,
    fmt        TEXT    NOT NULL,   -- 'Excel' or 'PDF'
    size_kb    REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_records_user  ON records(user_id);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
"""


def init_db(app):
    """Create tables if they don't exist. Call once at app startup."""
    with app.app_context():
        db = _connect()
        db.executescript(SCHEMA)
        # Migration: add joined column default if column exists without default
        try:
            db.execute("UPDATE users SET joined = strftime('%d %b %Y', 'now') WHERE joined IS NULL")
            db.commit()
        except Exception:
            pass
        db.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # rows behave like dicts
    return conn


def get_db() -> sqlite3.Connection:
    """Return per-request DB connection (stored on Flask's g object)."""
    if 'db' not in g:
        g.db = _connect()
    return g.db


def close_db(e=None):
    """Teardown — called automatically via app.teardown_appcontext."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ── User helpers ───────────────────────────────────────────────

def create_user(db, username: str, hashed_pw: str) -> int:
    from datetime import datetime
    joined = datetime.now().strftime("%d %b %Y")
    cur = db.execute(
        "INSERT INTO users (username, password, joined) VALUES (?, ?, ?)",
        [username, hashed_pw, joined]
    )
    db.commit()
    return cur.lastrowid


def get_user_by_name(db, username: str):
    return db.execute(
        "SELECT * FROM users WHERE username = ?", [username]
    ).fetchone()


def get_user_by_id(db, user_id: int):
    return db.execute(
        "SELECT * FROM users WHERE id = ?", [user_id]
    ).fetchone()


# ── Record helpers ─────────────────────────────────────────────

import json

def save_record(db, user_id: int, image_name: str, data: dict) -> int:
    """Insert a new extraction record. Returns the new record id."""
    cur = db.execute("""
        INSERT INTO records (
            user_id, image_name, ocr_engine, elapsed,
            feed, eggs, mortality, birds, date_field, batch,
            latest_abw, latest_fcr,
            abw_values, fcr_values, medicine_notes,
            raw_text, cleaned_text, formatted
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        user_id,
        image_name,
        data.get("ocr_engine"),
        data.get("elapsed"),
        data.get("feed"),
        data.get("eggs"),
        data.get("mortality"),
        data.get("birds"),
        data.get("date"),
        data.get("batch"),
        data.get("latest_abw"),
        data.get("latest_fcr"),
        json.dumps(data.get("abw_values", [])),
        json.dumps(data.get("fcr_values", [])),
        json.dumps(data.get("medicine_notes", [])),
        data.get("raw_text", ""),
        data.get("cleaned_text", ""),
        data.get("formatted", ""),
    ])
    db.commit()
    return cur.lastrowid


def update_record(db, record_id: int, edits: dict):
    """Update specific fields after user correction."""
    allowed = {'feed','eggs','mortality','birds','date_field','batch',
               'latest_abw','latest_fcr','abw_values','fcr_values'}
    sets, vals = [], []
    for k, v in edits.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            # Serialise lists to JSON
            vals.append(json.dumps(v) if isinstance(v, list) else v)

    if not sets:
        return

    sets.append("updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now')")
    vals.append(record_id)
    db.execute(f"UPDATE records SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()


def get_record(db, record_id: int):
    row = db.execute("SELECT * FROM records WHERE id = ?",
                     [record_id]).fetchone()
    return _deserialise_record(row) if row else None


def get_user_records(db, user_id: int, limit: int = 50) -> list:
    rows = db.execute(
        "SELECT * FROM records WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        [user_id, limit]
    ).fetchall()
    return [_deserialise_record(r) for r in rows]


def _deserialise_record(row) -> dict:
    """Convert sqlite3.Row to dict and parse JSON fields."""
    d = dict(row)
    for key in ('abw_values', 'fcr_values', 'medicine_notes'):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key] = []
        else:
            d[key] = []
    return d


def get_record_stats(db, user_id: int) -> dict:
    """Summary stats for the profile page."""
    row = db.execute("""
        SELECT
            COUNT(*)                          AS total_records,
            SUM(CASE WHEN feed IS NOT NULL THEN 1 ELSE 0 END) AS with_feed,
            SUM(CASE WHEN eggs IS NOT NULL THEN 1 ELSE 0 END) AS with_eggs
        FROM records WHERE user_id = ?
    """, [user_id]).fetchone()
    return dict(row) if row else {}


# ── Download helpers ───────────────────────────────────────────

def log_download(db, user_id: int, record_id: int,
                 filename: str, fmt: str, size_kb: float):
    db.execute("""
        INSERT INTO downloads (user_id, record_id, filename, fmt, size_kb)
        VALUES (?, ?, ?, ?, ?)
    """, [user_id, record_id, filename, fmt, size_kb])
    db.commit()


def get_user_downloads(db, user_id: int, limit: int = 100) -> list:
    rows = db.execute("""
        SELECT d.*, r.formatted as record_summary
        FROM downloads d
        LEFT JOIN records r ON d.record_id = r.id
        WHERE d.user_id = ?
        ORDER BY d.created_at DESC
        LIMIT ?
    """, [user_id, limit]).fetchall()
    return [dict(r) for r in rows]