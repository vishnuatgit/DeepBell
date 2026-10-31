"""SQLite access: app.db (the sample transactions) and metrics.db (telemetry, incidents)."""

import sqlite3
from contextlib import contextmanager

from . import config, runtime


def get_db():
    return sqlite3.connect(config.DB_FILE)


@contextmanager
def app_db():
    """A connection to app.db, held under the lock so a rollback never races a query."""
    with runtime.db_file_lock:
        conn = get_db()
        try:
            yield conn
        finally:
            conn.close()


def get_metrics_db():
    return sqlite3.connect(config.METRICS_DB)


def init_db():
    with app_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                amount REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def init_metrics_db():
    conn = get_metrics_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            pid INTEGER,
            cpu_percent REAL,
            memory_mb REAL
        )
    """)
    # Check if anomaly_status column exists and add it if not
    cursor.execute("PRAGMA table_info(metrics)")
    columns = [col[1] for col in cursor.fetchall()]
    if "anomaly_status" not in columns:
        cursor.execute("ALTER TABLE metrics ADD COLUMN anomaly_status TEXT DEFAULT 'Normal'")
        config.logger.info("Database migration: Added anomaly_status column to metrics table.")
    # Incident history lives in metrics.db, not app.db, so corrupting or rolling back the
    # application database never rewrites it. `timeline` is a JSON list of {at, type, text}.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            started_at TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            resolved_at TEXT,
            detail TEXT NOT NULL DEFAULT '',
            report TEXT,
            timeline TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.commit()
    conn.close()


def integrity_error(path: str) -> str | None:
    """None if the SQLite file passes quick_check and has the transactions table, else the error."""
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                return f"quick_check returned {result}"
            conn.execute("SELECT count(*) FROM transactions").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return str(e)
    return None


def check_app_db() -> str | None:
    """None if app.db answers a query, else the SQLite error message."""
    try:
        with app_db() as conn:
            conn.execute("SELECT count(*) FROM transactions").fetchone()
    except sqlite3.Error as e:
        return str(e)
    return None
