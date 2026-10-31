import glob
import hashlib
import json
import logging
import math
import mmap
import os
import random
import re
import shutil
import sqlite3
import statistics
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# Directory Definitions
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
ENV_FILE = os.getenv("DEEPBELL_ENV_FILE") or os.path.join(PROJECT_ROOT, ".env")

# Local runs read settings from the .env file. The public demo never does: its settings come
# only from the host's environment variables, and nothing in the app ever writes .env.
if not env_flag("PUBLIC_DEMO"):
    load_dotenv(ENV_FILE)
PUBLIC_DEMO = env_flag("PUBLIC_DEMO")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("DeepBellCockpit")

DATA_DIR = os.getenv("DEEPBELL_DATA_DIR") or os.path.join(PROJECT_ROOT, "data")
DB_FILE = os.path.join(DATA_DIR, "app.db")
METRICS_DB = os.path.join(DATA_DIR, "metrics.db")
BACKUP_DIR = os.getenv("DEEPBELL_BACKUP_DIR") or os.path.join(PROJECT_ROOT, "backups")
REPORTS_DIR = os.getenv("DEEPBELL_REPORTS_DIR") or os.path.join(PROJECT_ROOT, "reports")
# Set DEEPBELL_DISABLE_WORKERS=1 to skip the background threads (used by the test suite).
WORKERS_ENABLED = not env_flag("DEEPBELL_DISABLE_WORKERS")
# Seed a fresh instance with warm-up telemetry, sample transactions and an example report.
SEED_DEMO_DATA = os.getenv("DEEPBELL_SEED", "1").strip().lower() not in ("0", "false", "no", "off")
BACKUP_NAME_RE = re.compile(r"^app_(\d{8}_\d{6})\.db\.bak$")
REPORT_NAME_RE = re.compile(r"^RCA_Report_(\d{8}_\d{6})(?:_\d+)?\.md$")

REPO_URL = "https://github.com/vishnuatgit/DeepBell"
AUTHOR_URL = "https://vishnuatgit.github.io"

# Telemetry and anomaly detection
SAMPLE_INTERVAL_SEC = 2
WARMUP_POINTS = 60  # synthetic samples seeded on a fresh start (two minutes of telemetry)
WARMUP_STATUS = "Warm-up (synthetic)"
LEARNING_STATUS = "Learning baseline"
FIRST_FIT_SAMPLES = 15  # samples needed before the first fit
LIVE_FIT_SAMPLES = 30  # live samples that replace the synthetic warm-up in the model
TRAIN_WINDOW = 150  # newest normal samples the model is fitted on
REFIT_INTERVAL_SEC = 300
CPU_MIN_DELTA = 15.0  # percentage points above the baseline's 95th percentile
RAM_MIN_DELTA_MB = 30.0  # MB above the baseline median
OPEN_AFTER = 2  # anomalous samples in a row before an incident opens
CLOSE_AFTER = 3  # normal samples in a row before it closes
METRICS_KEEP = 10_000
METRICS_PAGE_MAX = 400  # samples one /api/metrics call returns at most (over 13 minutes)

# Snapshots, reports and data
SNAPSHOT_INTERVAL_SEC = 30
SNAPSHOTS_KEEP = 10
REPORTS_KEEP = 20
INCIDENTS_KEEP = 100
TRANSACTIONS_KEEP = 200
MAX_AMOUNT = 1_000_000
EVENTS_KEEP = 100

# Chaos Lab limits. The demo is shared, so every fault has a global cooldown.
MAX_SPIKE_SEC = 5
MAX_LEAK_MB = 120
MAX_LEAK_STEP_MB = 60
FAULT_COOLDOWN_SEC = 20
REPORT_COOLDOWN_SEC = 30
# Leaked memory is freed automatically after this long, so a closed browser tab cannot keep
# the shared demo in an incident forever.
LEAK_AUTO_RELEASE_SEC = 300
# A Chaos Lab fault counts as an incident's start if it was injected this recently.
FAULT_LINK_SEC = 60
MB = 1024 * 1024

# Guided demo: one scripted run at a time, executed on the server (see run_demo_scenario).
DEMO_LEAK_MB = 60
DEMO_COOLDOWN_SEC = 20  # after a run ends
DEMO_READY_TIMEOUT_SEC = 45  # how long to wait for the shared server to become healthy
DEMO_CLEANUP_AFTER_SEC = 15  # then clear faults other visitors left behind
DEMO_STEP_TIMEOUT_SEC = 30
DEMO_POLL_SEC = 0.5
DEMO_PAUSE_SEC = 3.0  # pacing between steps, so a person can follow along

# Status strip on the overview (uptime-style bars from the metrics history)
HISTORY_MINUTES = 30
HISTORY_BARS = 60

INCIDENT_LABELS = {
    "db_corruption": "Degraded (Database Error)",
    "memory_leak": "Anomalous (Memory Leak)",
    "cpu_spike": "Anomalous (CPU Spike)",
}
INCIDENT_NAMES = {
    "db_corruption": "Database corruption",
    "memory_leak": "Memory leak",
    "cpu_spike": "CPU spike",
}
INCIDENT_RESOLUTIONS = {
    "db_corruption": "Resolved: the database passes its check again.",
    "memory_leak": "Resolved: memory back within the baseline for {n} samples in a row.",
    "cpu_spike": "Resolved: CPU back within the baseline for {n} samples in a row.",
}
DETECTED_KINDS = ("cpu_spike", "memory_leak")
SAMPLE_LABELS = {"cpu_spike": "CPU Spike", "memory_leak": "Memory Leak"}
FAULT_NAMES = {
    "cpu_spike": "CPU spike",
    "memory_leak": "Memory leak",
    "corrupt_db": "Database corruption",
    "report": "Report generation",
    "demo": "The guided demo",
}
DEMO_STEPS = [
    ("ready", "Check that the server is healthy"),
    ("leak", f"Leak {DEMO_LEAK_MB} MB of memory"),
    ("detect", "Watchdog detects the leak"),
    ("report", "Incident report is written"),
    ("release", "Release the leaked memory"),
    ("resolve", "Incident resolves"),
    ("corrupt", "Corrupt the database"),
    ("rollback", "Roll back to the newest snapshot"),
]


# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value) -> datetime | None:
    """Parse a stored timestamp: ISO-8601, or SQLite's 'YYYY-MM-DD HH:MM:SS' (which is UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_iso_utc(value) -> str | None:
    dt = parse_utc(value)
    return iso(dt) if dt else value


def read_cpu_quota(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """CPU cores allowed by the container's cgroup quota, or None when there is no quota."""
    try:  # cgroup v2: "<quota> <period>" or "max <period>"
        with open(os.path.join(cgroup_root, "cpu.max"), encoding="utf-8") as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"), encoding="utf-8") as f:
            quota = int(f.read())
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"), encoding="utf-8") as f:
            period = int(f.read())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def cpu_capacity() -> float:
    """Cores this process can use, capped at one (all a single busy Python thread can use).

    psutil reports CPU as a percentage of one core. In a container limited to a fraction of a
    core (0.1 on Render's free plan) a busy loop would show as ~10 %, so samples are scaled by
    this capacity to read as a percentage of the CPU actually available.
    """
    override = os.getenv("DEEPBELL_CPU_CORES", "").strip()
    try:
        cores = float(override) if override else read_cpu_quota()
    except ValueError:
        cores = None
    if not cores or cores <= 0:
        return 1.0
    return min(cores, 1.0)


CPU_CAPACITY = cpu_capacity()


# Lifespan / Startup Handler
@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup_event()
    yield


OPENAPI_TAGS = [
    {"name": "status", "description": "Live health, telemetry samples and the status history."},
    {"name": "incidents", "description": "Incident history with timelines and measured times."},
    {"name": "reports", "description": "Markdown incident reports (template or Gemini)."},
    {"name": "database", "description": "Sample transactions, snapshots and rollback."},
    {"name": "chaos lab", "description": "Capped fault injection with shared cooldowns."},
    {"name": "guided demo", "description": "The scripted detect, report, recover scenario."},
    {"name": "settings", "description": "Local runs only: the Gemini API key, kept in memory."},
]

app = FastAPI(
    title="DeepBell",
    description=(
        "A mini SRE lab in FastAPI: it detects its own CPU and memory anomalies, writes "
        "incident reports, and restores from snapshots. All timestamps are UTC (ISO-8601)."
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# Dashboard UI Setup
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def static_version() -> str:
    """Cache-buster for the CSS and JS URLs, so a redeploy never pairs new markup with stale
    assets."""
    digest = hashlib.sha256()
    for name in ("styles.css", "app.js"):
        with open(os.path.join(BASE_DIR, "static", name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:10]


STATIC_VERSION = static_version()

# Global states
_lock = threading.RLock()
_db_file_lock = threading.RLock()  # serialises access to app.db (queries, rollback, corruption)
_report_lock = threading.Lock()
_incident_lock = threading.Lock()  # serialises read-modify-write of incident timelines
_memory_leak_array: list[mmap.mmap] = []
_leak_since: float | None = None  # monotonic time the oldest held leak block was allocated
latest_anomaly_score = 0.0
last_cpu = 0.0
last_ram = 0.0
_reports_in_flight = 0
_runtime_api_key = ""  # set from the Settings tab on local runs; never written to disk
_llm_loaded = False
_db_error: str | None = None
open_incidents: dict[str, dict] = {}
_events: deque = deque(maxlen=EVENTS_KEEP)
_event_seq = 0
_last_fault_at: dict[str, float] = {}
# Incident kind -> (time, description) of the last fault injected while no incident of that
# kind was open; the next incident of that kind starts at that time.
_pending_faults: dict[str, tuple[str, str]] = {}
# Incident kind -> (time, description) of the first anomalous sample in the current streak.
_first_flagged: dict[str, tuple[str, str]] = {}
_incidents_rev = 0  # bumped on every incident change, so the dashboard knows when to reload
_demo: dict | None = None  # the current or last guided-demo run
_demo_seq = 0
_started_at = datetime.now(UTC)  # when this process started serving (reset by startup_event)
_last_sample_at: datetime | None = None  # wall-clock time of the newest telemetry sample


def get_api_key() -> str:
    if _runtime_api_key and not PUBLIC_DEMO:
        return _runtime_api_key
    return os.getenv("GOOGLE_API_KEY", "").strip()


def log_event(level: str, message: str) -> None:
    """Record an event for the dashboard console (shared by every visitor) and the server log."""
    global _event_seq
    with _lock:
        _event_seq += 1
        _events.append(
            {"seq": _event_seq, "time": iso(utcnow()), "level": level, "message": message}
        )
    log = {"warn": logger.warning, "error": logger.error}.get(level, logger.info)
    log(message)


def recent_events(after: int) -> list[dict]:
    with _lock:
        return [e for e in _events if e["seq"] > after]


# Database helper functions
def get_db():
    return sqlite3.connect(DB_FILE)


@contextmanager
def app_db():
    """A connection to app.db, held under the lock so a rollback never races a query."""
    with _db_file_lock:
        conn = get_db()
        try:
            yield conn
        finally:
            conn.close()


def get_metrics_db():
    return sqlite3.connect(METRICS_DB)


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
        logger.info("Database migration: Added anomaly_status column to metrics table.")
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


# --- INCIDENT HISTORY ---
#
# An incident has three times: started (the Chaos Lab fault that caused it, or else its first
# anomalous sample), detected (when the watchdog opened it) and resolved. Time to detect is
# detected - started; time to recover is resolved - detected. In this lab they are demo
# measurements of this one process, not production numbers.

INCIDENT_COLUMNS = (
    "id, kind, status, started_at, detected_at, resolved_at, detail, report, timeline"
)
# Timeline events are sorted by time; events in the same second keep this logical order.
EVENT_ORDER = {
    "fault": 0,
    "flagged": 1,
    "detected": 2,
    "report": 3,
    "action": 4,
    "resolved": 5,
    "interrupted": 6,
}


def bump_incidents_rev() -> None:
    global _incidents_rev
    with _lock:
        _incidents_rev += 1


def seconds_between(start, end) -> int | None:
    a, b = parse_utc(start), parse_utc(end)
    return max(0, round((b - a).total_seconds())) if a and b else None


def incident_from_row(row) -> dict:
    incident_id, kind, status, started, detected, resolved, detail, report, timeline = row
    events = sorted(
        json.loads(timeline or "[]"), key=lambda e: (e["at"], EVENT_ORDER.get(e["type"], 9))
    )
    return {
        "id": incident_id,
        "kind": kind,
        "name": INCIDENT_NAMES.get(kind, kind),
        "label": INCIDENT_LABELS.get(kind, kind),
        "status": status,
        "started_at": started,
        "detected_at": detected,
        "resolved_at": resolved,
        "detail": detail,
        "report": report,
        "timeline": events,
        "ttd_sec": seconds_between(started, detected),
        "ttr_sec": seconds_between(detected, resolved) if resolved else None,
    }


def insert_incident(kind: str, started: str, detected: str, detail: str, events: list) -> int:
    conn = get_metrics_db()
    try:
        cur = conn.execute(
            "INSERT INTO incidents (kind, status, started_at, detected_at, detail, timeline) "
            "VALUES (?, 'open', ?, ?, ?, ?)",
            (kind, started, detected, detail, json.dumps(events)),
        )
        conn.execute(
            "DELETE FROM incidents WHERE id <= (SELECT max(id) FROM incidents) - ?",
            (INCIDENTS_KEEP,),
        )
        conn.commit()
        incident_id = cur.lastrowid
    finally:
        conn.close()
    bump_incidents_rev()
    return incident_id


def update_incident(incident_id: int | None, event: tuple[str, str] | None = None, **fields):
    """Set columns of one incident and optionally append (type, text) to its timeline."""
    if incident_id is None:
        return
    with _incident_lock:
        conn = get_metrics_db()
        try:
            row = conn.execute(
                "SELECT timeline FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return
            if event:
                events = json.loads(row[0] or "[]")
                at = fields.get("resolved_at") or iso(utcnow())
                events.append({"at": at, "type": event[0], "text": event[1]})
                fields["timeline"] = json.dumps(events)
            if fields:
                # Column names come from the callers in this module, never from a request.
                assignments = ", ".join(f"{column} = ?" for column in fields)
                conn.execute(
                    f"UPDATE incidents SET {assignments} WHERE id = ?",
                    (*fields.values(), incident_id),
                )
                conn.commit()
        finally:
            conn.close()
    bump_incidents_rev()


def get_incident(incident_id: int) -> dict | None:
    conn = get_metrics_db()
    try:
        row = conn.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    finally:
        conn.close()
    return incident_from_row(row) if row else None


def list_incidents(limit: int = 20) -> list[dict]:
    conn = get_metrics_db()
    try:
        rows = conn.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [incident_from_row(r) for r in rows]


def interrupt_open_incidents() -> int:
    """Mark incidents left open by a previous process as interrupted (their end is unknown)."""
    conn = get_metrics_db()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM incidents WHERE status = 'open'")]
    finally:
        conn.close()
    for incident_id in ids:
        update_incident(
            incident_id,
            event=("interrupted", "The server restarted while this incident was open."),
            status="interrupted",
        )
    return len(ids)


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


# Self-Healing Database Integrity Check
def verify_and_heal_database():
    logger.info("Verifying database integrity...")
    if not os.path.exists(DB_FILE):
        return
    error = integrity_error(DB_FILE)
    if error is None or error.startswith("no such table"):
        logger.info("Database integrity verified successfully.")
        return
    logger.error(f"Database corruption detected: {error}. Executing auto-recovery...")
    if restore_latest_backup():
        return
    # No usable snapshot: keep the damaged file for inspection and start with an empty database.
    aside = f"{DB_FILE}.corrupt-{utcnow():%Y%m%d_%H%M%S}"
    with _db_file_lock:
        os.replace(DB_FILE, aside)
    logger.error(f"No usable snapshot; moved the damaged database to {aside}.")


# --- SNAPSHOTS ---


def list_snapshots() -> list[str]:
    """Snapshot file names, newest first. The creation time (UTC) is part of the name."""
    names = [os.path.basename(p) for p in glob.glob(os.path.join(BACKUP_DIR, "app_*.db.bak"))]
    return sorted((n for n in names if BACKUP_NAME_RE.match(n)), reverse=True)


def snapshot_time(name: str) -> datetime:
    stamp = BACKUP_NAME_RE.match(name).group(1)
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)


def file_digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def take_snapshot(now: datetime | None = None) -> str | None:
    """Copy app.db into BACKUP_DIR and keep the newest SNAPSHOTS_KEEP copies.

    Skipped when the database is unreadable or unchanged since the newest snapshot, so the
    kept snapshots are distinct restore points. Returns the new file name, or None.
    """
    if not os.path.exists(DB_FILE):
        return None
    with _db_file_lock:
        with open(DB_FILE, "rb") as f:
            data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    snapshots = list_snapshots()
    if snapshots and file_digest(os.path.join(BACKUP_DIR, snapshots[0])) == digest:
        return None
    name = f"app_{(now or utcnow()):%Y%m%d_%H%M%S}.db.bak"
    if name in snapshots:
        return None
    # Write to a temporary file and check that copy, so a torn read never becomes a snapshot.
    tmp = os.path.join(BACKUP_DIR, f".{name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    error = integrity_error(tmp)
    if error:
        os.remove(tmp)
        logger.warning(f"Snapshot skipped, database is unreadable: {error}")
        return None
    os.replace(tmp, os.path.join(BACKUP_DIR, name))
    for old in list_snapshots()[SNAPSHOTS_KEEP:]:
        os.remove(os.path.join(BACKUP_DIR, old))
    log_event("info", f"Snapshot taken: {name}")
    return name


def snapshot_info(name: str) -> dict:
    """Whether a snapshot passes quick_check, and how many transactions it holds, so the Time
    Machine can say what a rollback brings back."""
    try:
        conn = sqlite3.connect(os.path.join(BACKUP_DIR, name))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            count = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return {"readable": False, "transactions": None}
    if not check or check[0] != "ok":
        return {"readable": False, "transactions": None}
    return {"readable": True, "transactions": count}


def restore_snapshot(name: str) -> None:
    with _db_file_lock:
        shutil.copyfile(os.path.join(BACKUP_DIR, name), DB_FILE)


def newest_good_snapshot() -> str | None:
    """The newest snapshot that passes an integrity check."""
    for name in list_snapshots():
        if integrity_error(os.path.join(BACKUP_DIR, name)) is None:
            return name
    return None


def restore_latest_backup() -> str | None:
    """Restore the newest readable snapshot. Returns its name, or None if none was restored."""
    name = newest_good_snapshot()
    if name is None:
        logger.error("No backups available for restoration.")
        return None
    logger.info(f"Restoring database from snapshot: {name}")
    try:
        restore_snapshot(name)
    except OSError as e:
        logger.error(f"Failed to restore database: {e}")
        return None
    logger.info("Database restored successfully.")
    return name


# Background Snapshot Worker
def snapshot_manager_worker():
    logger.info("Snapshot Manager started.")
    while True:
        try:
            take_snapshot()
        except Exception as e:
            logger.error(f"Snapshot Manager Error: {e}")
        time.sleep(SNAPSHOT_INTERVAL_SEC)


# --- ANOMALY DETECTION ---


class Detector:
    """Isolation Forest anomaly detection over (CPU %, RSS MB) against a baseline window.

    The baseline (the newest TRAIN_WINDOW normal samples, their 95th-percentile CPU and median
    memory) is refreshed every REFIT_INTERVAL_SEC, not on every sample. A sample becomes a
    candidate only when it is at least CPU_MIN_DELTA points (or RAM_MIN_DELTA_MB) above the
    baseline; the forest then confirms it. The forest isolates almost any point outside the
    window's range, which is why it flagged ordinary jitter on its own.

    An incident opens after OPEN_AFTER anomalous samples in a row and closes after CLOSE_AFTER
    normal ones. Samples taken while an incident is open never enter the baseline, so a leak
    that is still held keeps its incident open instead of becoming the new normal.
    """

    def __init__(self):
        self.samples: deque = deque(maxlen=TRAIN_WINDOW)  # (cpu, ram, synthetic)
        self.window = None  # numpy array of the baseline samples
        self.baseline: dict | None = None
        self.fitted_at: float | None = None
        self.fit_count = 0
        self.forest_fits = 0
        self.new_since_fit = 0
        self.streak = dict.fromkeys(DETECTED_KINDS, 0)
        self.calm = dict.fromkeys(DETECTED_KINDS, 0)
        self.open: set[str] = set()

    def reset(self) -> None:
        """Forget the baseline and learn a new one from the next samples."""
        self.samples.clear()
        self.window = None
        self.baseline = None
        self.fitted_at = None

    def add_warmup(self, points: list[tuple[float, float]]) -> None:
        self.samples.extend((cpu, ram, True) for cpu, ram in points)

    def live_count(self) -> int:
        return sum(1 for s in self.samples if not s[2])

    def fit_due(self, now: float) -> bool:
        if self.baseline is None:
            return len(self.samples) >= FIRST_FIT_SAMPLES
        if self.baseline["source"] == "warm-up":
            return self.live_count() >= LIVE_FIT_SAMPLES
        return (
            now - self.fitted_at >= REFIT_INTERVAL_SEC and self.new_since_fit >= FIRST_FIT_SAMPLES
        )

    def fit(self, now: float) -> None:
        """Refresh the baseline window, preferring live samples over synthetic warm-up ones."""
        import numpy as np

        live = [s for s in self.samples if not s[2]]
        source = "live" if len(live) >= FIRST_FIT_SAMPLES else "warm-up"
        if source == "live":
            self.samples = deque(live, maxlen=TRAIN_WINDOW)
        self.window = np.array([[cpu, ram] for cpu, ram, _ in self.samples])
        self.baseline = {
            "cpu": round(float(np.percentile(self.window[:, 0], 95)), 1),
            "ram": round(float(np.median(self.window[:, 1])), 1),
            "source": source,
            "samples": len(self.window),
            "fitted_at": iso(utcnow()),
        }
        self.fitted_at = now
        self.fit_count += 1
        self.new_since_fit = 0

    def isolated(self, cpu: float, ram: float) -> bool:
        """Fit an Isolation Forest on the baseline window plus this sample; True if the sample
        is an outlier. A forest fitted on the baseline alone cannot rank points outside its
        training range (they all score like its most extreme samples), so the candidate is
        part of the fit."""
        import numpy as np
        from sklearn.ensemble import IsolationForest

        X = np.vstack([self.window, [[cpu, ram]]])
        model = IsolationForest(n_estimators=100, contamination=0.02, random_state=42).fit(X)
        self.forest_fits += 1
        return bool(model.predict([[cpu, ram]])[0] == -1)

    def classify(self, cpu: float, ram: float) -> list[str]:
        if self.baseline is None:
            return []
        kinds = []
        if cpu > self.baseline["cpu"] + CPU_MIN_DELTA:
            kinds.append("cpu_spike")
        if ram > self.baseline["ram"] + RAM_MIN_DELTA_MB:
            kinds.append("memory_leak")
        # The forest confirms new candidates only: an open incident stays open while its
        # deviation lasts, without a forest fit every 2 s.
        if any(k not in self.open for k in kinds) and not self.isolated(cpu, ram):
            kinds = [k for k in kinds if k in self.open]
        return kinds

    def observe(self, cpu: float, ram: float, now: float):
        """Score one sample. Returns (its anomaly kinds, incidents opened, incidents closed)."""
        if self.fit_due(now):
            self.fit(now)
        kinds = self.classify(cpu, ram)
        opened, closed = [], []
        for kind in DETECTED_KINDS:
            if kind in kinds:
                self.streak[kind] += 1
                self.calm[kind] = 0
                if kind not in self.open and self.streak[kind] >= OPEN_AFTER:
                    self.open.add(kind)
                    opened.append(kind)
            else:
                self.streak[kind] = 0
                self.calm[kind] += 1
                if kind in self.open and self.calm[kind] >= CLOSE_AFTER:
                    self.open.discard(kind)
                    closed.append(kind)
        if not kinds and not self.open:
            self.samples.append((cpu, ram, False))
            self.new_since_fit += 1
        return kinds, opened, closed


detector = Detector()


def sample_status(kinds: list[str]) -> str:
    if detector.baseline is None:
        return LEARNING_STATUS
    if not kinds:
        return "Normal"
    return f"Anomalous ({', '.join(SAMPLE_LABELS[k] for k in kinds)})"


def flagged_text(kind: str, cpu: float, ram: float) -> str:
    """Timeline text for the first anomalous sample of a streak (called with _lock held)."""
    base = detector.baseline
    if kind == "cpu_spike":
        return (
            f"First anomalous sample: CPU {cpu}% (alert above {base['cpu'] + CPU_MIN_DELTA:.1f}%)."
        )
    return f"First anomalous sample: memory {ram} MB (alert above {base['ram'] + RAM_MIN_DELTA_MB:.0f} MB)."


def incident_start(kind: str, now: datetime) -> tuple[str, list[dict]]:
    """When an incident that is being opened now really began, plus its first timeline events.

    The start is the Chaos Lab fault that caused it (if one was injected in the last
    FAULT_LINK_SEC), else the first anomalous sample of the streak, else now.
    Called with _lock held.
    """
    events = []
    started = iso(now)
    flagged = _first_flagged.get(kind)
    if flagged:
        started = flagged[0]
        events.append({"at": flagged[0], "type": "flagged", "text": flagged[1]})
    fault = _pending_faults.pop(kind, None)
    fault_time = parse_utc(fault[0]) if fault else None
    if fault_time and timedelta(0) <= now - fault_time <= timedelta(seconds=FAULT_LINK_SEC):
        started = min(started, fault[0])  # same ISO format, so strings sort by time
        events.append({"at": fault[0], "type": "fault", "text": fault[1]})
    return started, events


def open_incident(kind: str, detail: str = "") -> bool:
    """Open an incident and write its report, unless one of the same kind is already open."""
    now = utcnow()
    with _lock:
        if kind in open_incidents:
            return False
        started, events = incident_start(kind, now)
        detected = iso(now)
        events.append({"at": detected, "type": "detected", "text": f"Incident opened. {detail}"})
        try:
            incident_id = insert_incident(kind, started, detected, detail, events)
        except sqlite3.Error as e:  # history is a nice-to-have; never let it stop detection
            logger.error(f"Could not record the incident: {e}")
            incident_id = None
        open_incidents[kind] = {
            "id": incident_id,
            "kind": kind,
            "name": INCIDENT_NAMES[kind],
            "label": INCIDENT_LABELS[kind],
            "started_at": started,
            "opened_at": detected,
            "detail": detail,
        }
    log_event("error", f"Incident opened: {INCIDENT_LABELS[kind]}. {detail}".strip())
    trigger_auto_rca(kind)
    return True


def close_incident(kind: str, resolution: str = "") -> None:
    with _lock:
        incident = open_incidents.pop(kind, None)
    if not incident:
        return
    resolved = utcnow()
    text = resolution or INCIDENT_RESOLUTIONS[kind].format(n=CLOSE_AFTER)
    try:
        update_incident(
            incident["id"], ("resolved", text), status="resolved", resolved_at=iso(resolved)
        )
    except sqlite3.Error as e:
        logger.error(f"Could not record the resolution: {e}")
    opened = parse_utc(incident["opened_at"])
    seconds = int((resolved - opened).total_seconds()) if opened else 0
    log_event("success", f"Incident resolved: {incident['label']} after {seconds} s.")


def note_fault(kind: str, text: str) -> None:
    """Record an injected fault: on the open incident of that kind, or as the start of the next."""
    now = iso(utcnow())
    with _lock:
        incident = open_incidents.get(kind)
        if incident is None:
            _pending_faults[kind] = (now, text)
            return
    update_incident(incident["id"], ("fault", text))


def note_action(kind: str, text: str) -> None:
    """Add a recovery action to the open incident of that kind, if there is one."""
    with _lock:
        incident = open_incidents.get(kind)
    if incident:
        update_incident(incident["id"], ("action", text))


def check_app_db() -> str | None:
    """None if app.db answers a query, else the SQLite error message."""
    try:
        with app_db() as conn:
            conn.execute("SELECT count(*) FROM transactions").fetchone()
    except sqlite3.Error as e:
        return str(e)
    return None


def set_db_error(error: str | None) -> None:
    """Track the database state: an error opens an incident that stays open until a rollback."""
    global _db_error
    with _lock:
        _db_error = error
        is_open = "db_corruption" in open_incidents
    if error and not is_open:
        open_incident("db_corruption", f"SQLite error: {error}.")
    elif not error and is_open:
        close_incident("db_corruption")


def refresh_db_state() -> str | None:
    error = check_app_db()
    set_db_error(error)
    return error


def record_sample(cpu: float, ram: float, now: float | None = None) -> list[str]:
    """Store one telemetry sample, score it and open or close incidents. Returns its kinds."""
    global last_cpu, last_ram, latest_anomaly_score, _last_sample_at
    cpu, ram = round(cpu, 1), round(ram, 1)
    with _lock:
        kinds, opened, closed = detector.observe(cpu, ram, time.monotonic() if now is None else now)
        status = sample_status(kinds)
        last_cpu, last_ram = cpu, ram
        _last_sample_at = utcnow()
        latest_anomaly_score = 1.0 if kinds else 0.0
        # Remember where each anomalous streak began: that is the incident's start when no
        # Chaos Lab fault explains it.
        for kind in DETECTED_KINDS:
            if kind not in kinds:
                _first_flagged.pop(kind, None)
            elif detector.streak[kind] == 1:
                _first_flagged[kind] = (iso(utcnow()), flagged_text(kind, cpu, ram))
    conn = get_metrics_db()
    try:
        conn.execute(
            "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (iso(utcnow()), os.getpid(), cpu, ram, status),
        )
        conn.commit()
    finally:
        conn.close()
    if kinds:
        logger.warning(f"Watchdog anomaly: CPU {cpu}%, RAM {ram} MB ({status}).")
    for kind in opened:
        base = detector.baseline
        detail = (
            f"CPU {cpu}% (baseline {base['cpu']}%), {OPEN_AFTER} anomalous samples in a row."
            if kind == "cpu_spike"
            else f"Memory {ram} MB (baseline {base['ram']} MB), "
            f"{OPEN_AFTER} anomalous samples in a row."
        )
        open_incident(kind, detail)
    for kind in closed:
        close_incident(kind)
    return kinds


def prune_metrics() -> None:
    conn = get_metrics_db()
    try:
        conn.execute(
            "DELETE FROM metrics WHERE id <= (SELECT max(id) FROM metrics) - ?", (METRICS_KEEP,)
        )
        conn.commit()
    finally:
        conn.close()


def seed_warmup_metrics(cpu_level: float, ram_mb: float, now: datetime | None = None) -> int:
    """Insert WARMUP_POINTS synthetic samples around the first live reading and hand them to
    the detector, so a fresh instance can score samples at once instead of after ~30 s of
    learning. They are stored with the status WARMUP_STATUS, and the model replaces them with
    live samples after the first minute. Skipped if live samples from the last 5 minutes exist.
    """
    now = now or utcnow()
    conn = get_metrics_db()
    try:
        row = conn.execute("SELECT timestamp FROM metrics ORDER BY id DESC LIMIT 1").fetchone()
        last = parse_utc(row[0]) if row else None
        if last and now - last < timedelta(minutes=5):
            return 0
        rng = random.Random(42)
        cpu_level = min(max(cpu_level, 1.0), 10.0)
        rows = []
        for i in range(WARMUP_POINTS, 0, -1):
            cpu = round(max(0.0, cpu_level + rng.uniform(-1.0, 1.0)), 1)
            ram = round(ram_mb + rng.uniform(-0.5, 0.5), 1)
            ts = iso(now - timedelta(seconds=i * SAMPLE_INTERVAL_SEC))
            rows.append((ts, 0, cpu, ram, WARMUP_STATUS))
        conn.executemany(
            "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    with _lock:
        detector.add_warmup([(r[2], r[3]) for r in rows])
    log_event(
        "info",
        f"Seeded {len(rows)} synthetic warm-up samples so the watchdog starts at once; "
        "live samples replace them within about a minute.",
    )
    return len(rows)


def load_llm(relearn: bool) -> None:
    """Import the LangChain/Gemini client before the first AI report.

    The import grows the process by about 30 MB, which the watchdog would otherwise report as a
    memory leak, so after a late import the detector re-learns its baseline.
    """
    global _llm_loaded
    if _llm_loaded:
        return
    try:
        import langchain_core.prompts  # noqa: F401
        import langchain_google_genai  # noqa: F401
    except Exception as e:
        logger.error(f"Could not load the Gemini client: {e}")
        return
    _llm_loaded = True
    if relearn:
        with _lock:
            detector.reset()
        log_event("info", "Gemini client loaded; the watchdog is re-learning its memory baseline.")


# Background Watchdog & Anomaly Detection Worker
def watchdog_worker():
    logger.info("Watchdog Telemetry & Anomaly Detector started.")
    import psutil
    from sklearn.ensemble import IsolationForest  # noqa: F401  (load before measuring memory)

    if get_api_key():
        load_llm(relearn=False)
    process = psutil.Process(os.getpid())
    # First CPU read to establish baseline
    process.cpu_percent(interval=None)
    time.sleep(1)
    if SEED_DEMO_DATA:
        seed_warmup_metrics(
            process.cpu_percent(interval=None) / CPU_CAPACITY,
            process.memory_info().rss / MB,
        )
    ticks = 0
    while True:
        time.sleep(SAMPLE_INTERVAL_SEC)
        try:
            cpu = process.cpu_percent(interval=None) / CPU_CAPACITY
            record_sample(cpu, process.memory_info().rss / MB)
            refresh_db_state()
            release_stale_leak()
            ticks += 1
            if ticks % 150 == 0:
                prune_metrics()
        except Exception as e:
            logger.error(f"Watchdog Worker Error: {e}")


# --- INCIDENT REPORTS ---


def trigger_auto_rca(incident: str = "") -> None:
    """Write a report in a background thread. Reports are serialised, never dropped."""
    global _reports_in_flight
    with _lock:
        _reports_in_flight += 1
        # Capture the incident now: by the time a queued report runs, it may have closed.
        record = dict(open_incidents.get(incident) or {})

    def run():
        global _reports_in_flight
        try:
            generate_rca_report_task(incident, record)
        finally:
            with _lock:
                _reports_in_flight -= 1

    threading.Thread(target=run, daemon=True).start()


def fetch_recent_metrics(limit: int = 15) -> list[dict]:
    conn = get_metrics_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"timestamp": to_iso_utc(r[0]), "cpu": r[1], "ram": r[2], "status": r[3]} for r in rows
    ][::-1]


def telemetry_table(rows: list[dict]) -> str:
    lines = ["| Timestamp | CPU (%) | Memory (MB) | Watchdog status |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['timestamp']} | {r['cpu']:.1f} | {r['ram']:.1f} | {r['status']} |")
    return "\n".join(lines)


def flagged_labels(rows: list[dict]) -> list[str]:
    """The sample classifications (CPU Spike, Memory Leak) present among these rows."""
    statuses = " ".join(r["status"] or "" for r in rows)
    return [label for label in SAMPLE_LABELS.values() if label in statuses]


def report_summary(rows: list[dict], incident: str, detail: str) -> tuple[str, str]:
    """The report's kind and summary paragraph.

    Only samples of the incident's own kind are counted: a CPU report ignores samples flagged
    for a memory leak, and a database report says that CPU and memory are not part of its fault
    instead of describing another fault's samples as its own.
    """
    flagged = [r for r in rows if "Anomalous" in (r["status"] or "")]
    if "Database" in incident:
        failed = f"failed ({detail.rstrip('.')})" if detail else "failed"
        summary = f"The database check {failed}. CPU and memory are not part of this fault."
        if flagged:
            kinds = ", ".join(label.lower() for label in flagged_labels(flagged))
            summary += (
                f" {len(flagged)} of the last {len(rows)} samples were flagged, but they belong "
                f"to another fault ({kinds})."
            )
        return incident, summary
    word = next((label for label in SAMPLE_LABELS.values() if label in incident), None)
    anomalies = [r for r in flagged if word is None or word in r["status"]]
    if anomalies:
        latest = anomalies[-1]["status"]
        summary = (
            f"The watchdog flagged {len(anomalies)} of the last {len(rows)} samples. "
            f"The first flagged sample was at {anomalies[0]['timestamp']}; the latest "
            f"classification is **{latest}**."
        )
        kind = incident or latest
    else:
        kind = incident or "No anomaly in window"
        summary = f"None of the last {len(rows)} samples was flagged by the watchdog."
    if detail:
        summary += f"\n\nDetail: {detail}"
    return kind, summary


def build_template_report(
    rows: list[dict],
    generated_at: str,
    note: str = "",
    incident: str = "",
    detail: str = "",
    timeline: list[dict] | None = None,
) -> str:
    """Deterministic incident report used when no Gemini API key is configured."""
    if not rows and not incident:
        return (
            f"# Incident Report\n\nGenerated: {generated_at}\n\n"
            "No telemetry has been recorded yet, so there is nothing to analyse."
        )
    cpus = [r["cpu"] for r in rows]
    rams = [r["ram"] for r in rows]
    kind, summary = report_summary(rows, incident, detail)
    if "Database" in kind:
        cause = (
            "The application database no longer reads as SQLite: its file was overwritten or "
            "damaged on disk. CPU and memory are not expected to move for this kind of fault."
        )
        actions = [
            "Roll back to the newest good snapshot (Time Machine view)",
            "Find out what wrote to the database file outside SQLite",
            "Keep snapshots on separate storage from the live database",
        ]
    elif "CPU" in kind:
        cause = (
            "A burst of CPU-bound work in the process (for example a busy loop or a hot code path)."
        )
        actions = [
            "Profile the hot path",
            "Move CPU-heavy work off the request thread",
            "Add CPU alerts",
        ]
    elif "Memory" in kind:
        cause = (
            "Memory that is allocated and never released (resident memory grew and stayed high)."
        )
        actions = [
            "Take a heap snapshot",
            "Check caches and global lists for unbounded growth",
            "Restart the process if memory keeps rising",
        ]
    elif "Degradation" in kind:
        cause = "CPU and memory moved away from their baseline together."
        actions = [
            "Correlate with recent deploys and traffic",
            "Check both CPU and memory profiles",
        ]
    else:
        cause = "No cause identified: telemetry is within its normal range."
        actions = ["Keep monitoring"]
    parts = [
        f"# Incident Report: {kind}",
        "",
        f"**Generated:** {generated_at}  ",
        "**Generator:** built-in template (set `GOOGLE_API_KEY` for an AI-written analysis)",
    ]
    if note:
        parts += ["", f"> {note}"]
    parts += ["", "## Summary", "", summary, ""]
    if timeline:
        parts += ["## Timeline (UTC)", "", "| Time | Event |", "|---|---|"]
        parts += [f"| {e['at']} | {e['text']} |" for e in timeline]
        parts += [""]
    if rows:
        parts += [
            "## Telemetry statistics",
            "",
            f"- CPU: min {min(cpus):.1f} %, mean {sum(cpus) / len(cpus):.1f} %, max {max(cpus):.1f} %",
            f"- Memory: min {min(rams):.1f} MB, mean {sum(rams) / len(rams):.1f} MB, max {max(rams):.1f} MB",
            "",
            "## Recent telemetry",
            "",
            telemetry_table(rows),
            "",
        ]
    parts += ["## Likely cause", "", cause, "", "## Recommended actions", ""]
    parts += [f"- {a}" for a in actions]
    return "\n".join(parts) + "\n"


def generate_ai_report(api_key: str, rows: list[dict], incident: str = "") -> str:
    """Ask Gemini (through LangChain) for an RCA report. Imported lazily."""
    from langchain_core.prompts import PromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        temperature=0.2,
        google_api_key=api_key,
    )
    prompt_template = """
You are an expert Site Reliability Engineer (SRE).
The system watchdog has detected an anomaly or database incident.
Incident type reported by the watchdog: {incident}

Here is the telemetry data (timestamps in UTC) from the last few minutes before the incident:
{telemetry_data}

Write a Root Cause Analysis (RCA) report in Markdown with:
1. Executive Summary
2. Timeline of Anomaly (when values started deviating)
3. Suspected Root Cause (CPU lockup, memory leak or database corruption, based on the data)
4. Recommended Preventative Actions
"""
    prompt = PromptTemplate(
        input_variables=["telemetry_data", "incident"], template=prompt_template
    )
    response = (prompt | llm).invoke(
        {"telemetry_data": telemetry_table(rows), "incident": incident or "manual request"}
    )
    content = response.content
    if isinstance(content, list):
        content = "".join(c if isinstance(c, str) else c.get("text", "") for c in content)
    return content


def list_reports() -> list[str]:
    """Report file names, newest first (the UTC creation time is part of the name)."""
    names = [os.path.basename(p) for p in glob.glob(os.path.join(REPORTS_DIR, "RCA_Report_*.md"))]
    return sorted(names, reverse=True)


def write_report(content: str, now: datetime | None = None) -> str:
    now = now or utcnow()
    path = os.path.join(REPORTS_DIR, f"RCA_Report_{now:%Y%m%d_%H%M%S_%f}.md")
    while os.path.exists(path):  # the clock can repeat a microsecond on some platforms
        now += timedelta(microseconds=1)
        path = os.path.join(REPORTS_DIR, f"RCA_Report_{now:%Y%m%d_%H%M%S_%f}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    for old in list_reports()[REPORTS_KEEP:]:
        os.remove(os.path.join(REPORTS_DIR, old))
    return path


def report_generated_at(name: str) -> str | None:
    match = REPORT_NAME_RE.match(name)
    if not match:
        return None
    return iso(datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC))


def report_incident_ids() -> dict[str, int]:
    """Report file name -> id of the incident it was written for."""
    conn = get_metrics_db()
    try:
        rows = conn.execute("SELECT report, id FROM incidents WHERE report IS NOT NULL").fetchall()
    finally:
        conn.close()
    return dict(rows)


def read_report(name: str) -> dict | None:
    """One report with its metadata, or None if the name is invalid or the file is gone."""
    if not REPORT_NAME_RE.match(name):
        return None
    try:
        with open(os.path.join(REPORTS_DIR, name), encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    return {
        "filename": name,
        "generated_at": report_generated_at(name),
        "content": content,
        "incident_id": report_incident_ids().get(name),
        "source": report_source(content),
    }


def report_title(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return (match.group(1) if match else fallback).replace("Incident Report: ", "").strip()


def report_source(content: str) -> str:
    """Who wrote a report, read from its text so older files are labelled too.

    "example" (the startup seed), "template-fallback" (the AI call failed), "template" or
    "gemini". The dashboard labels template reports as such, so they are never mistaken for AI.
    """
    if "Example report, written at startup" in content:
        return "example"
    if "this is the template fallback" in content:
        return "template-fallback"
    if "**Generator:** built-in template" in content:
        return "template"
    return "gemini"


def latest_report() -> dict | None:
    for name in list_reports():
        report = read_report(name)
        if report:
            return report
    return None


def report_for_download(name: str) -> str | None:
    """The report as saved, plus the incident's timeline and measured times when it has one."""
    report = read_report(name)
    if report is None:
        return None
    content = report["content"].rstrip() + "\n"
    incident = get_incident(report["incident_id"]) if report["incident_id"] else None
    if incident is None:
        return content
    lines = [
        "",
        "---",
        "",
        "## Incident timeline (demo measurements)",
        "",
        f"Incident #{incident['id']}: {incident['name']}, status **{incident['status']}**.",
        "",
        "| Time (UTC) | Event |",
        "|---|---|",
    ]
    lines += [f"| {e['at']} | {e['text']} |" for e in incident["timeline"]]
    ttd, ttr = incident["ttd_sec"], incident["ttr_sec"]
    lines += [
        "",
        f"- Time to detect (start to incident opened): {ttd} s",
        "- Time to recover (incident opened to resolved): "
        + (f"{ttr} s" if ttr is not None else "not resolved yet"),
        "",
        "These times come from this DeepBell instance's own timestamps during a demo. They "
        "describe this lab, not a production system.",
    ]
    return content + "\n".join(lines) + "\n"


def generate_rca_report_task(incident: str = "", record: dict | None = None) -> str:
    """Generate a report (AI if a key is set, template otherwise) and save it. Returns the path.

    `record` is the open incident the report is for (captured when it was triggered); the
    report is linked to it and listed on its timeline.
    """
    with _report_lock:
        logger.info("Starting Root Cause Analysis...")
        try:
            rows = fetch_recent_metrics(15)
            generated_at = iso(utcnow())
            label = INCIDENT_LABELS.get(incident, "")
            record = record or {}
            with _lock:
                detail = record.get("detail") or open_incidents.get(incident, {}).get("detail", "")
            incident_id = record.get("id")
            stored = get_incident(incident_id) if incident_id else None
            api_key = get_api_key()
            content = None
            note = ""
            if api_key:
                try:
                    content = generate_ai_report(api_key, rows, label)
                except Exception as e:
                    logger.error(f"AI report failed, using template instead: {e}")
                    note = (
                        f"The AI report failed ({type(e).__name__}); this is the template fallback."
                    )
            source = "Gemini" if content else "template"
            if not content:
                timeline = stored["timeline"] if stored else None
                content = build_template_report(rows, generated_at, note, label, detail, timeline)
            report_path = write_report(content)
            name = os.path.basename(report_path)
            update_incident(
                incident_id, ("report", f"Report written ({source}): {name}"), report=name
            )
            log_event("success", f"Incident report written ({source}): {name}")
            return report_path
        except Exception as e:
            logger.error(f"RCA generation error: {e}")
            return ""


# --- DEMO SEED DATA ---

SAMPLE_AMOUNTS = [49.99, 120.0, 15.5, 310.25, 72.4]
EXAMPLE_CPU = [1.4, 0.9, 1.6, 1.1, 1.3, 0.8, 1.5, 1.2, 97.6, 99.1, 64.2, 1.7, 1.1, 1.3, 0.9]


def seed_demo_data(now: datetime | None = None) -> None:
    """Give a fresh instance something to show: a few transactions and an example report."""
    now = now or utcnow()
    with app_db() as conn:
        if conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0:
            for i, amount in enumerate(SAMPLE_AMOUNTS):
                ts = iso(now - timedelta(minutes=5 * (len(SAMPLE_AMOUNTS) - i)))
                conn.execute(
                    "INSERT INTO transactions (id, amount, timestamp) VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), amount, ts),
                )
            conn.commit()
            logger.info(f"Seeded {len(SAMPLE_AMOUNTS)} sample transactions.")
    if not list_reports():
        start = now - timedelta(seconds=2 * len(EXAMPLE_CPU))
        rows = [
            {
                "timestamp": iso(start + timedelta(seconds=2 * i)),
                "cpu": cpu,
                "ram": 128.0 + (i % 3) * 0.2,
                "status": "Anomalous (CPU Spike)" if cpu > 50 else "Normal",
            }
            for i, cpu in enumerate(EXAMPLE_CPU)
        ]
        note = (
            "Example report, written at startup from synthetic telemetry so a fresh instance "
            "has something to show. Inject a fault in the Chaos Lab to get a real one."
        )
        content = build_template_report(rows, iso(now), note, INCIDENT_LABELS["cpu_spike"])
        write_report(content, now - timedelta(seconds=1))


def startup_event():
    global _started_at
    _started_at = utcnow()
    # Verify (and if needed restore) the database before touching its schema:
    # init_db() raises on a corrupted file, which would crash every restart.
    verify_and_heal_database()
    init_db()
    init_metrics_db()
    interrupted = interrupt_open_incidents()
    if interrupted:
        logger.info(f"Marked {interrupted} incident(s) left open by the last run as interrupted.")
    if SEED_DEMO_DATA:
        seed_demo_data()
    refresh_db_state()
    log_event(
        "info",
        "Cockpit started"
        + (" in public demo mode." if PUBLIC_DEMO else ".")
        + f" CPU is shown as a share of {CPU_CAPACITY:g} core(s).",
    )

    if not WORKERS_ENABLED:
        logger.info("Background workers disabled (DEEPBELL_DISABLE_WORKERS).")
        return
    threading.Thread(target=snapshot_manager_worker, daemon=True).start()
    threading.Thread(target=watchdog_worker, daemon=True).start()
    logger.info("All SRE Cockpit background threads initialized.")


# --- CHAOS LAB STATE (shared by the API endpoints and the guided demo) ---


def leaked_mb() -> int:
    with _lock:
        return sum(len(block) for block in _memory_leak_array) // MB


def cooldown_left(name: str, cooldown: int = FAULT_COOLDOWN_SEC) -> int:
    with _lock:
        last = _last_fault_at.get(name)
    if last is None:
        return 0
    return max(0, math.ceil(cooldown - (time.monotonic() - last)))


def claim_fault(name: str, cooldown: int = FAULT_COOLDOWN_SEC) -> None:
    """Start the cooldown for a fault, or raise a friendly 429 if it is still cooling down."""
    with _lock:
        wait = cooldown_left(name, cooldown)
        if wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"{FAULT_NAMES[name]} was used moments ago. This demo is shared, so each "
                    f"action has a {cooldown} s cooldown: try again in {wait} s."
                ),
                headers={"Retry-After": str(wait)},
            )
        _last_fault_at[name] = time.monotonic()


def allocate_leak(size_mb: int) -> mmap.mmap:
    """Anonymous memory with every page written, so it counts towards RSS at once and goes
    back to the operating system as soon as it is closed."""
    block = mmap.mmap(-1, size_mb * MB)
    for offset in range(0, size_mb * MB, mmap.PAGESIZE):
        block[offset] = 1
    return block


def hold_leak(size_mb: int) -> int:
    """Allocate a leak block and keep it. Returns the total MB now held."""
    global _leak_since
    block = allocate_leak(size_mb)
    with _lock:
        if not _memory_leak_array:
            _leak_since = time.monotonic()
        _memory_leak_array.append(block)
    return leaked_mb()


def free_leaked_memory() -> int:
    """Close every leak block (the memory goes straight back to the OS). Returns the MB freed."""
    global _leak_since
    with _lock:
        blocks = list(_memory_leak_array)
        _memory_leak_array.clear()
        _leak_since = None
    released = sum(len(block) for block in blocks) // MB
    for block in blocks:
        block.close()
    return released


def release_leak(source: str) -> int:
    """Free all leaked memory and record who did it. Returns the MB freed."""
    released = free_leaked_memory()
    if released:
        log_event("info", f"{source}: released {released} MB of leaked memory.")
        note_action("memory_leak", f"{source}: released {released} MB of leaked memory.")
    return released


def release_stale_leak(now: float | None = None) -> int:
    """Free leaked memory held for LEAK_AUTO_RELEASE_SEC. Called by the watchdog every tick."""
    with _lock:
        since = _leak_since
    if since is None:
        return 0
    if (time.monotonic() if now is None else now) - since < LEAK_AUTO_RELEASE_SEC:
        return 0
    return release_leak(f"Auto-release after {LEAK_AUTO_RELEASE_SEC // 60} minutes")


def corrupt_database(source: str) -> bool:
    """Write garbage over the SQLite header of app.db, after making sure a snapshot exists."""
    if not list_snapshots():
        take_snapshot()
    try:
        # Overwrite the SQLite header. Appending bytes is not enough: SQLite
        # ignores trailing data, so the file would still open normally.
        with _db_file_lock, open(DB_FILE, "r+b") as f:
            f.write(b"GARBAGE DATA CORRUPTION " * 5)
    except OSError as e:
        logger.error(f"Failed to corrupt DB: {e}")
        return False
    note_fault("db_corruption", f"{source}: garbage written over the database file header.")
    log_event("warn", f"{source}: wrote garbage over the database header.")
    refresh_db_state()
    return True


def rollback_to(filename: str, source: str = "Rollback") -> str | None:
    """Restore a snapshot and re-check the database. Returns the remaining error, if any."""
    restore_snapshot(filename)
    note_action("db_corruption", f"{source}: database restored from snapshot {filename}.")
    log_event("success", f"{source}: database restored from snapshot {filename}.")
    return refresh_db_state()


def fault_status() -> dict:
    with _lock:
        since = _leak_since
    auto_release_in = (
        max(0, math.ceil(LEAK_AUTO_RELEASE_SEC - (time.monotonic() - since)))
        if since is not None
        else None
    )
    return {
        "cooldown_sec": FAULT_COOLDOWN_SEC,
        "cpu_spike": {"cooldown": cooldown_left("cpu_spike"), "max_sec": MAX_SPIKE_SEC},
        "memory_leak": {
            "cooldown": cooldown_left("memory_leak"),
            "held_mb": leaked_mb(),
            "limit_mb": MAX_LEAK_MB,
            "step_mb": MAX_LEAK_STEP_MB,
            "auto_release_in": auto_release_in,
        },
        "corrupt_db": {"cooldown": cooldown_left("corrupt_db")},
        "fatal_crash": {"enabled": not PUBLIC_DEMO},
        "report": {"cooldown": cooldown_left("report", REPORT_COOLDOWN_SEC)},
        "demo": {"cooldown": cooldown_left("demo", DEMO_COOLDOWN_SEC)},
    }


def snapshot_is_current(name: str) -> bool:
    """True when the snapshot holds exactly what app.db holds now. Both files are a few KB
    (the demo keeps at most TRANSACTIONS_KEEP rows), so hashing them on each poll is cheap."""
    try:
        with _db_file_lock:
            live = file_digest(DB_FILE)
        return live == file_digest(os.path.join(BACKUP_DIR, name))
    except OSError:
        return False


def latest_snapshot() -> dict | None:
    """The newest snapshot, and whether it is "current". Snapshots are taken only when the data
    changes, so an old snapshot that is current means the backups are up to date."""
    snapshots = list_snapshots()
    if not snapshots:
        return None
    newest = snapshots[0]
    return {
        "filename": newest,
        "timestamp": iso(snapshot_time(newest)),
        "current": snapshot_is_current(newest),
    }


# --- STATUS STRIP ---


def status_history(
    minutes: int = HISTORY_MINUTES, bars: int = HISTORY_BARS, now: datetime | None = None
) -> dict:
    """Uptime-style bars for the last `minutes`, oldest first, from metrics and incidents.

    A bar is "incident" if an incident was open during it, else "degraded" if one of its
    samples was flagged, else "healthy" if it has live samples, "synthetic" if it only has
    seeded warm-up samples, and "nodata" otherwise.
    """
    now = now or utcnow()
    start = now - timedelta(minutes=minutes)
    width = (now - start) / bars
    conn = get_metrics_db()
    try:
        samples = conn.execute(
            "SELECT timestamp, anomaly_status FROM metrics ORDER BY id DESC LIMIT ?",
            (minutes * 60 // SAMPLE_INTERVAL_SEC + 60,),
        ).fetchall()
        incident_rows = conn.execute(
            "SELECT id, status, detected_at, resolved_at FROM incidents "
            "WHERE resolved_at IS NULL OR resolved_at >= ?",
            (iso(start),),
        ).fetchall()
    finally:
        conn.close()

    # Each incident is open from detection to resolution; an open one runs until now, and an
    # interrupted one (the server restarted) has no known end, so it only marks its start.
    spans = []
    for incident_id, status, detected, resolved in incident_rows:
        begin = parse_utc(detected)
        if begin is None or begin > now:
            continue
        end = parse_utc(resolved) if resolved else (now if status == "open" else begin)
        if end >= start:
            spans.append((incident_id, begin, end))

    buckets = [{"samples": 0, "flagged": 0, "synthetic": 0, "incidents": []} for _ in range(bars)]
    live = healthy = 0
    for timestamp, status in samples:
        t = parse_utc(timestamp)
        if t is None or not start <= t <= now:
            continue
        bucket = buckets[min(int((t - start) / width), bars - 1)]
        if status == WARMUP_STATUS:
            bucket["synthetic"] += 1
            continue
        flagged = (status or "").startswith("Anomalous")
        in_incident = any(begin <= t <= end for _, begin, end in spans)
        bucket["samples"] += 1
        bucket["flagged"] += flagged
        live += 1
        healthy += not flagged and not in_incident

    result = []
    for i, bucket in enumerate(buckets):
        bar_start, bar_end = start + i * width, start + (i + 1) * width
        ids = [incident_id for incident_id, b, e in spans if b <= bar_end and e >= bar_start]
        if ids:
            state = "incident"
        elif bucket["flagged"]:
            state = "degraded"
        elif bucket["samples"]:
            state = "healthy"
        elif bucket["synthetic"]:
            state = "synthetic"
        else:
            state = "nodata"
        result.append(
            {
                "start": iso(bar_start),
                "end": iso(bar_end),
                "state": state,
                "samples": bucket["samples"],
                "flagged": bucket["flagged"],
                "incidents": ids,
            }
        )
    return {
        "minutes": minutes,
        "bar_sec": round(width.total_seconds()),
        "start": iso(start),
        "end": iso(now),
        "bars": result,
        "summary": {
            "live_samples": live,
            "healthy_samples": healthy,
            "healthy_pct": round(100 * healthy / live, 1) if live else None,
            "incidents": sum(1 for _, begin, _ in spans if begin >= start),
        },
    }


# --- LIVE PIPELINE AND INCIDENT STATS ---
#
# The overview draws the sample -> detect -> report -> recover loop as a pipeline that lights up
# while it runs. Its state comes from here, so the page shows what the server is really doing.

PIPELINE_RECENT_SEC = 60  # a resolved incident stays on the pipeline this long
STATS_WINDOW = 20  # incidents the overview's median times are computed over


def pending_fault() -> tuple[str, datetime] | None:
    """The newest injected fault that has not opened an incident yet (kind, injected at), if it
    was injected in the last FAULT_LINK_SEC."""
    with _lock:
        faults = [(kind, parse_utc(at)) for kind, (at, _) in _pending_faults.items()]
    now = utcnow()
    recent = [
        (kind, at) for kind, at in faults if at and now - at <= timedelta(seconds=FAULT_LINK_SEC)
    ]
    return max(recent, key=lambda item: item[1]) if recent else None


def pipeline_focus() -> dict | None:
    """The incident the pipeline follows: the first open one, else one resolved in the last
    PIPELINE_RECENT_SEC, unless something new has started since (a streak of anomalous samples,
    or a fault injected after it resolved)."""
    with _lock:
        open_ids = [open_incidents[k]["id"] for k in INCIDENT_LABELS if k in open_incidents]
        streaking = any(detector.streak.values())
    if open_ids and open_ids[0] is not None:
        return get_incident(open_ids[0])
    latest = list_incidents(1)
    if streaking or not latest or latest[0]["status"] != "resolved":
        return None
    resolved = parse_utc(latest[0]["resolved_at"])
    if not resolved or utcnow() - resolved > timedelta(seconds=PIPELINE_RECENT_SEC):
        return None
    fault = pending_fault()
    if fault and fault[1] >= resolved:
        return None
    return latest[0]


def pipeline_stage(key: str, state: str, detail: str) -> dict:
    return {"key": key, "state": state, "detail": detail}


def demo_will(step_key: str) -> bool:
    """True while a guided-demo run is in progress and has not done this step yet, so the
    pipeline can say the demo is about to act instead of asking the visitor to."""
    with _lock:
        if not _demo or _demo["state"] != "running":
            return False
        return any(s["key"] == step_key and s["state"] != "done" for s in _demo["steps"])


def sample_stage() -> dict:
    with _lock:
        sampled, base, cpu, ram = _last_sample_at, detector.baseline, last_cpu, last_ram
        learned = len(detector.samples)
    if sampled is None:
        return pipeline_stage("sample", "idle", "Waiting for the first sample.")
    age = (utcnow() - sampled).total_seconds()
    if age > 3 * SAMPLE_INTERVAL_SEC:
        return pipeline_stage("sample", "idle", f"No sample for {age:.0f} s.")
    if base is None:
        return pipeline_stage(
            "sample",
            "active",
            f"Learning the baseline: {min(learned, FIRST_FIT_SAMPLES)} of {FIRST_FIT_SAMPLES} "
            "samples.",
        )
    return pipeline_stage("sample", "active", f"CPU {cpu:.1f} %, memory {ram:.0f} MB.")


def detect_stage(focus: dict | None) -> dict:
    if focus:
        return pipeline_stage(
            "detect",
            "done",
            f"Incident #{focus['id']} ({focus['name'].lower()}) opened {focus['ttd_sec']} s "
            "after it started.",
        )
    with _lock:
        streaks = {k: n for k, n in detector.streak.items() if n}
    if streaks:
        kind, count = max(streaks.items(), key=lambda item: item[1])
        name = INCIDENT_NAMES[kind].lower()
        # A full streak is visible for a moment while the incident is being recorded.
        detail = (
            f"{count} anomalous samples in a row ({name}): opening an incident."
            if count >= OPEN_AFTER
            else f"{count} of {OPEN_AFTER} anomalous samples in a row ({name})."
        )
        return pipeline_stage("detect", "active", detail)
    fault = pending_fault()
    if fault:
        kind, at = fault
        seconds = max(0, round((utcnow() - at).total_seconds()))
        return pipeline_stage(
            "detect",
            "active",
            f"{INCIDENT_NAMES[kind]} injected {seconds} s ago; waiting for {OPEN_AFTER} anomalous "
            "samples in a row.",
        )
    return pipeline_stage("detect", "idle", "Every sample is within the baseline.")


def report_stage(focus: dict | None) -> dict:
    with _lock:
        writing = _reports_in_flight > 0
    if focus and focus["report"]:
        return pipeline_stage("report", "done", f"Report linked to incident #{focus['id']}.")
    if focus or writing:
        return pipeline_stage("report", "active", "Writing the report from the last 15 samples.")
    return pipeline_stage("report", "idle", "Written when an incident opens.")


def recover_stage(focus: dict | None) -> dict:
    if focus is None:
        return pipeline_stage("recover", "idle", "Nothing to recover.")
    if focus["status"] == "resolved":
        return pipeline_stage(
            "recover", "done", f"Incident #{focus['id']} resolved after {focus['ttr_sec']} s."
        )
    if focus["kind"] == "db_corruption":
        if demo_will("rollback"):
            return pipeline_stage(
                "recover", "active", "The guided demo rolls back to the newest snapshot next."
            )
        return pipeline_stage(
            "recover", "waiting", "Restore a snapshot in the Time Machine to recover."
        )
    held = leaked_mb()
    if focus["kind"] == "memory_leak" and held:
        if demo_will("release"):
            return pipeline_stage(
                "recover", "active", f"The guided demo releases the {held} MB next."
            )
        return pipeline_stage(
            "recover", "waiting", f"Release the {held} MB of leaked memory to recover."
        )
    with _lock:
        calm = detector.calm.get(focus["kind"], 0)
    return pipeline_stage(
        "recover", "active", f"{calm} of {CLOSE_AFTER} normal samples in a row before it closes."
    )


def incident_pipeline() -> dict:
    """Where the sample -> detect -> report -> recover loop is now.

    Each stage is "idle", "active" (working), "waiting" (needs someone to act) or "done".
    `current` is the stage to highlight: the first one that is active or waiting.
    """
    focus = pipeline_focus()
    stages = [sample_stage(), detect_stage(focus), report_stage(focus), recover_stage(focus)]
    current = next(
        (s["key"] for s in stages[1:] if s["state"] in ("active", "waiting")),
        "sample" if stages[0]["state"] == "active" else None,
    )
    return {"incident_id": focus["id"] if focus else None, "current": current, "stages": stages}


def incident_stats(limit: int = STATS_WINDOW) -> dict:
    """Counts and median times to detect and recover over the newest `limit` incidents, plus
    each one's times (oldest first) for the overview's small bar charts."""
    recent = list_incidents(limit)
    conn = get_metrics_db()
    try:
        total, open_count = conn.execute(
            "SELECT count(*), coalesce(sum(status = 'open'), 0) FROM incidents"
        ).fetchone()
    finally:
        conn.close()
    ttd = [i["ttd_sec"] for i in recent if i["ttd_sec"] is not None]
    ttr = [i["ttr_sec"] for i in recent if i["ttr_sec"] is not None]
    return {
        "total": total,
        "open": open_count,
        "window": len(recent),
        "median_ttd_sec": statistics.median(ttd) if ttd else None,
        "median_ttr_sec": statistics.median(ttr) if ttr else None,
        "recent": [
            {
                "id": i["id"],
                "kind": i["kind"],
                "name": i["name"],
                "status": i["status"],
                "detected_at": i["detected_at"],
                "ttd_sec": i["ttd_sec"],
                "ttr_sec": i["ttr_sec"],
            }
            for i in reversed(recent)
        ],
    }


# --- GUIDED DEMO ---
#
# One scripted scenario at a time, run in a server thread rather than in the browser: a
# closed tab can never leave memory leaked, and a second visitor watches the run in progress
# instead of starting a competing one. It goes through the same caps and cooldowns as the
# Chaos Lab, and the watchdog detects its faults exactly as it detects any other.


class DemoFailed(Exception):
    """A demo step could not finish. The message is shown to the visitor."""


def new_demo_run(run_id: int) -> dict:
    return {
        "id": run_id,
        "state": "running",
        "started_at": iso(utcnow()),
        "finished_at": None,
        "steps": [
            {"key": key, "title": title, "state": "pending", "detail": "", "done_at": None}
            for key, title in DEMO_STEPS
        ],
        "incident_ids": [],
        "summary": "",
    }


def demo_state() -> dict | None:
    """A copy of the current or last run, safe to serialise while the demo thread runs."""
    with _lock:
        if _demo is None:
            return None
        return {
            **_demo,
            "steps": [dict(step) for step in _demo["steps"]],
            "incident_ids": list(_demo["incident_ids"]),
        }


def demo_step(key: str, state: str, detail: str | None = None) -> None:
    with _lock:
        step = next(s for s in _demo["steps"] if s["key"] == key)
        step["state"] = state
        if detail is not None:
            step["detail"] = detail
        if state in ("done", "failed"):
            step["done_at"] = iso(utcnow())


def finish_demo(state: str, summary: str) -> None:
    with _lock:
        _demo["state"] = state
        _demo["finished_at"] = iso(utcnow())
        _demo["summary"] = summary
        _last_fault_at["demo"] = time.monotonic()  # the demo cooldown counts from the end
    log_event("success" if state == "succeeded" else "warn", f"Guided demo {state}. {summary}")


def demo_wait(key: str, check, waiting_text, timeout: float = DEMO_STEP_TIMEOUT_SEC):
    """Poll check() until it returns something truthy, narrating waiting_text() meanwhile."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise DemoFailed(f"Timed out after {timeout:.0f} s. {waiting_text()}")
        demo_step(key, "active", waiting_text())
        time.sleep(DEMO_POLL_SEC)


def demo_claim(key: str, fault: str) -> None:
    """Start a fault's cooldown for the demo, waiting while another visitor's cooldown runs."""
    deadline = time.monotonic() + FAULT_COOLDOWN_SEC + 5
    while True:
        try:
            claim_fault(fault)
            return
        except HTTPException:
            if time.monotonic() > deadline:
                raise DemoFailed("The shared cooldown did not end in time.")
            demo_step(
                key,
                "active",
                f"Another visitor used this fault moments ago. Waiting {cooldown_left(fault)} s "
                f"for the shared {FAULT_COOLDOWN_SEC} s cooldown.",
            )
            time.sleep(DEMO_POLL_SEC)


def demo_blocker() -> str | None:
    """Why the shared server is not ready for a demo run, or None if it is."""
    with _lock:
        if detector.baseline is None:
            return "The watchdog is still learning its baseline (about 30 s after a restart)."
        incident = next(iter(open_incidents.values()), None)
    if incident:
        return (
            f"Waiting for an open incident ({incident['name'].lower()}) to resolve. This server "
            "is shared, so another visitor may be testing it."
        )
    held = leaked_mb()
    if held:
        return f"Another visitor is holding {held} MB of leaked memory. Waiting for its release."
    return None


def demo_get_ready() -> None:
    """Wait for the shared server to be healthy; after a while, clear faults left behind."""
    began = time.monotonic()
    cleaned = False
    while (reason := demo_blocker()) is not None:
        waited = time.monotonic() - began
        if waited > DEMO_READY_TIMEOUT_SEC:
            raise DemoFailed(f"{reason} Try again in a minute.")
        if not cleaned and waited > DEMO_CLEANUP_AFTER_SEC:
            # Anyone can do both of these from the Chaos Lab and the Time Machine.
            cleaned = True
            release_leak("Guided demo (clearing a leftover leak)")
            snapshot = newest_good_snapshot() if _db_error else None
            if snapshot:
                rollback_to(snapshot, "Guided demo (clearing a leftover corruption)")
        demo_step("ready", "active", reason)
        time.sleep(DEMO_POLL_SEC)


def resolved_incident(incident_id: int) -> dict | None:
    incident = get_incident(incident_id)
    return incident if incident and incident["status"] != "open" else None


def run_demo_scenario() -> None:
    """Leak memory, watch the watchdog open an incident and write its report, release the
    memory and watch the incident resolve; then corrupt the database and roll it back."""
    key = "ready"
    leaked = False
    try:
        demo_step(key, "active", "Checking the baseline, open incidents and held memory.")
        demo_get_ready()
        with _lock:
            base_ram = detector.baseline["ram"]
        alert_mb = base_ram + RAM_MIN_DELTA_MB
        demo_step(
            key,
            "done",
            f"Healthy. Memory baseline {base_ram:.0f} MB; an incident opens above "
            f"{alert_mb:.0f} MB.",
        )
        time.sleep(DEMO_PAUSE_SEC)

        key = "leak"
        demo_step(key, "active", "Allocating memory.")
        demo_claim(key, "memory_leak")
        if leaked_mb() + DEMO_LEAK_MB > MAX_LEAK_MB:
            raise DemoFailed("Too much leaked memory is held already; the demo keeps to the cap.")
        # Record the fault first: the watchdog may sample the new memory within milliseconds.
        note_fault("memory_leak", f"Guided demo: leaked {DEMO_LEAK_MB} MB.")
        hold_leak(DEMO_LEAK_MB)
        leaked = True
        log_event("warn", f"Guided demo: leaked {DEMO_LEAK_MB} MB of memory.")
        demo_step(
            key,
            "done",
            f"Holding {DEMO_LEAK_MB} MB. The next samples should read about "
            f"{base_ram + DEMO_LEAK_MB:.0f} MB.",
        )

        key = "detect"

        def streak_text():
            with _lock:
                streak = detector.streak["memory_leak"]
            return (
                f"Memory {last_ram:.0f} MB, alert above {alert_mb:.0f} MB. Anomalous samples "
                f"in a row: {streak} of {OPEN_AFTER}."
            )

        incident = demo_wait(key, lambda: open_incidents.get("memory_leak"), streak_text)
        incident_id = incident["id"]
        if incident_id is None:
            raise DemoFailed("The incident opened but could not be recorded.")
        with _lock:
            _demo["incident_ids"].append(incident_id)
        ttd = seconds_between(incident["started_at"], incident["opened_at"])
        demo_step(
            key,
            "done",
            f"Incident #{incident_id} opened {ttd} s after the leak, once {OPEN_AFTER} samples "
            "in a row were anomalous.",
        )

        key = "report"
        demo_wait(
            key,
            lambda: (get_incident(incident_id) or {}).get("report"),
            lambda: "Writing the report from the last 15 samples.",
            timeout=90 if get_api_key() else DEMO_STEP_TIMEOUT_SEC,
        )
        demo_step(
            key,
            "done",
            f"Linked to incident #{incident_id}; read or download it on the Incidents view.",
        )
        time.sleep(2 * DEMO_PAUSE_SEC)  # time to look at the open incident

        key = "release"
        released = release_leak("Guided demo")
        leaked = False
        demo_step(
            key,
            "done",
            f"Released {released} MB. Memory drops on the next sample."
            if released
            else "Another visitor released it first. Memory drops on the next sample.",
        )

        key = "resolve"

        def calm_text():
            with _lock:
                calm = detector.calm["memory_leak"]
            return f"Memory {last_ram:.0f} MB. Normal samples in a row: {calm} of {CLOSE_AFTER}."

        resolved = demo_wait(key, lambda: resolved_incident(incident_id), calm_text)
        demo_step(
            key,
            "done",
            f"Incident #{incident_id} resolved {resolved['ttr_sec']} s after it opened.",
        )
        time.sleep(DEMO_PAUSE_SEC)

        key = "corrupt"
        demo_step(key, "active", "Writing over the database header.")
        demo_claim(key, "corrupt_db")
        if not corrupt_database("Guided demo"):
            raise DemoFailed("Could not write to the database file.")
        db_incident = demo_wait(
            key,
            lambda: open_incidents.get("db_corruption"),
            lambda: "Waiting for the database check to fail.",
        )
        with _lock:
            _demo["incident_ids"].append(db_incident["id"])
        demo_step(
            key,
            "done",
            f"Queries now fail ({_db_error}). Incident #{db_incident['id']} opened.",
        )
        time.sleep(DEMO_PAUSE_SEC)

        key = "rollback"
        demo_step(key, "active", "Looking for the newest readable snapshot.")
        snapshot = newest_good_snapshot()
        if snapshot is None:
            raise DemoFailed("No readable snapshot to roll back to.")
        error = rollback_to(snapshot, "Guided demo")
        if error:
            raise DemoFailed(f"The database is still unreadable after the rollback: {error}")
        demo_step(key, "done", f"Restored {snapshot}. Incident #{db_incident['id']} resolved.")
        finish_demo(
            "succeeded",
            f"The leak was detected {ttd} s after it started and resolved "
            f"{resolved['ttr_sec']} s after detection; the database was restored from "
            f"{snapshot}.",
        )
    except DemoFailed as e:
        demo_step(key, "failed", str(e))
        finish_demo("failed", str(e))
    except Exception as e:
        logger.exception("Guided demo failed")
        demo_step(key, "failed", f"Unexpected error: {e}")
        finish_demo("failed", "Unexpected error; see the server log.")
    finally:
        if leaked:
            release_leak("Guided demo (cleanup)")


# --- DASHBOARD ---


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_dashboard(request: Request):
    """Serve the dashboard."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "report": latest_report(),
            "api_key_configured": bool(get_api_key()),
            "public_demo": PUBLIC_DEMO,
            "static_version": STATIC_VERSION,
            "repo_url": REPO_URL,
            "author_url": AUTHOR_URL,
            "demo_steps": DEMO_STEPS,
            "config": {
                "publicDemo": PUBLIC_DEMO,
                "maxSpikeSec": MAX_SPIKE_SEC,
                "leakStepMb": MAX_LEAK_STEP_MB,
                "leakLimitMb": MAX_LEAK_MB,
                "leakAutoReleaseSec": LEAK_AUTO_RELEASE_SEC,
                "cooldownSec": FAULT_COOLDOWN_SEC,
                "reportCooldownSec": REPORT_COOLDOWN_SEC,
                "cpuDeltaPct": CPU_MIN_DELTA,
                "ramDeltaMb": RAM_MIN_DELTA_MB,
                "openAfter": OPEN_AFTER,
                "closeAfter": CLOSE_AFTER,
                "sampleIntervalSec": SAMPLE_INTERVAL_SEC,
                "snapshotIntervalSec": SNAPSHOT_INTERVAL_SEC,
                "snapshotsKeep": SNAPSHOTS_KEEP,
                "reportsKeep": REPORTS_KEEP,
                "demoLeakMb": DEMO_LEAK_MB,
                "historyMinutes": HISTORY_MINUTES,
                "historyBars": HISTORY_BARS,
            },
        },
    )


@app.get("/healthz", tags=["status"])
def healthz():
    """Cheap liveness check for the host (Render's health check)."""
    return {"status": "ok"}


# --- TRANSACTION ENDPOINTS ---


def database_unavailable(error: str) -> HTTPException:
    set_db_error(error)
    return HTTPException(
        status_code=503,
        detail=f"Database unreadable ({error}). Roll back to a snapshot in the Time Machine.",
    )


@app.post("/transaction", tags=["database"])
def create_transaction(amount: float):
    if not math.isfinite(amount) or not 0 < amount <= MAX_AMOUNT:
        raise HTTPException(
            status_code=400, detail=f"Amount must be greater than 0 and at most {MAX_AMOUNT:,}."
        )
    amount = round(amount, 2)
    tx_id = str(uuid.uuid4())
    try:
        with app_db() as conn:
            conn.execute(
                "INSERT INTO transactions (id, amount, timestamp) VALUES (?, ?, ?)",
                (tx_id, amount, iso(utcnow())),
            )
            # Keep the demo table small: only the newest TRANSACTIONS_KEEP rows.
            conn.execute(
                "DELETE FROM transactions WHERE rowid NOT IN "
                "(SELECT rowid FROM transactions ORDER BY rowid DESC LIMIT ?)",
                (TRANSACTIONS_KEEP,),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        raise database_unavailable(str(e))
    logger.info(f"Transaction created: {tx_id} with amount {amount}")
    return {"id": tx_id, "amount": amount}


@app.get("/transactions", tags=["database"])
def list_transactions():
    try:
        with app_db() as conn:
            rows = conn.execute(
                "SELECT id, amount, timestamp FROM transactions ORDER BY rowid DESC LIMIT 10"
            ).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch transactions: {e}")
        raise database_unavailable(str(e))
    return [{"id": r[0], "amount": r[1], "timestamp": to_iso_utc(r[2])} for r in rows]


# --- REST API ---


@app.get("/api/status", tags=["status"])
def get_system_status(events_after: int | None = None):
    """Returns the live status of the cockpit (and, if asked, events newer than events_after)."""
    with _lock:
        incidents = [dict(open_incidents[k]) for k in INCIDENT_LABELS if k in open_incidents]
        baseline = dict(detector.baseline) if detector.baseline else None
        if baseline:
            baseline["live_samples"] = detector.live_count()
            baseline["live_needed"] = LIVE_FIT_SAMPLES
        warming = detector.baseline is None
        db_error = _db_error
        detection = {
            "open_after": OPEN_AFTER,
            "close_after": CLOSE_AFTER,
            "streak": dict(detector.streak),
            "calm": dict(detector.calm),
            "fits": detector.fit_count,
            "forest_fits": detector.forest_fits,
        }
        incidents_rev = _incidents_rev
        sampled_at = _last_sample_at
    if incidents:
        status = incidents[0]["label"]
        state = "degraded" if incidents[0]["kind"] == "db_corruption" else "anomalous"
    elif warming:
        status, state = "Healthy (Learning Baseline)", "warming"
    else:
        status, state = "Healthy", "healthy"
    result = {
        "status": status,
        "state": state,
        "incidents": incidents,
        "anomaly_score": latest_anomaly_score,
        "cpu": last_cpu,
        "ram": last_ram,
        "cpu_capacity": CPU_CAPACITY,
        "baseline": baseline,
        "detection": detection,
        "database": {"ok": db_error is None, "error": db_error},
        "is_generating_rca": _reports_in_flight > 0,
        "api_key_configured": bool(get_api_key()),
        "public_demo": PUBLIC_DEMO,
        "faults": fault_status(),
        "latest_report": (list_reports() or [None])[0],
        "latest_snapshot": latest_snapshot(),
        "incidents_rev": incidents_rev,
        "demo": demo_state(),
        "pipeline": incident_pipeline(),
        "last_sample_at": iso(sampled_at) if sampled_at else None,
        "started_at": iso(_started_at),
        "uptime_sec": max(0, round((utcnow() - _started_at).total_seconds())),
        "server_time": iso(utcnow()),
    }
    if events_after is not None:
        events = recent_events(events_after)
        result["events"] = events
        result["event_seq"] = events[-1]["seq"] if events else events_after
    return result


@app.get("/api/status/history", tags=["status"])
def get_status_history(minutes: int = HISTORY_MINUTES, bars: int = HISTORY_BARS):
    """Uptime-style bars (healthy / degraded / incident) for the last `minutes`."""
    return status_history(max(5, min(minutes, 120)), max(10, min(bars, 120)))


@app.get("/api/metrics", tags=["status"])
def get_metrics(limit: int = 15):
    """Fetch the latest telemetry entries, newest first (timestamps in UTC)."""
    limit = max(1, min(limit, METRICS_PAGE_MAX))
    conn = get_metrics_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [{"timestamp": to_iso_utc(r[0]), "cpu": r[1], "ram": r[2], "status": r[3]} for r in rows]


@app.get("/api/incidents", tags=["incidents"])
def get_incidents(limit: int = 20):
    """Incident history, newest first, with timelines and times to detect and recover."""
    available = set(list_reports())
    incidents = list_incidents(max(1, min(limit, INCIDENTS_KEEP)))
    for incident in incidents:
        incident["report_available"] = incident["report"] in available
    return incidents


@app.get("/api/incidents/stats", tags=["incidents"])
def get_incident_stats():
    """Incident counts and median times to detect and recover (newest 20 incidents)."""
    return incident_stats()


@app.get("/api/incidents/{incident_id}", tags=["incidents"])
def get_incident_detail(incident_id: int):
    incident = get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="No such incident.")
    incident["report_available"] = incident["report"] in set(list_reports())
    return incident


@app.get("/api/reports", tags=["reports"])
def get_reports():
    """All kept reports, newest first, with their title and incident (if any)."""
    incident_ids = report_incident_ids()
    reports = []
    for name in list_reports():
        try:
            with open(os.path.join(REPORTS_DIR, name), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        reports.append(
            {
                "filename": name,
                "generated_at": report_generated_at(name),
                "title": report_title(content, name),
                "incident_id": incident_ids.get(name),
                "source": report_source(content),
            }
        )
    return reports


@app.get("/api/reports/latest", tags=["reports"])
def get_latest_report():
    """The newest incident report (Markdown), or 404 if none exists yet."""
    report = latest_report()
    if not report:
        raise HTTPException(status_code=404, detail="No incident reports yet.")
    return report


def checked_report_name(name: str) -> str:
    if not REPORT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid report file name.")
    return name


@app.get("/api/reports/{name}", tags=["reports"])
def get_report(name: str):
    report = read_report(checked_report_name(name))
    if report is None:
        raise HTTPException(
            status_code=404, detail="Report not found. Only the newest 20 are kept."
        )
    return report


@app.get("/api/reports/{name}/download", tags=["reports"])
def download_report(name: str):
    """The report as a Markdown file, with its incident's timeline appended."""
    content = report_for_download(checked_report_name(name))
    if content is None:
        raise HTTPException(
            status_code=404, detail="Report not found. Only the newest 20 are kept."
        )
    return Response(
        content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/backups", tags=["database"])
def get_backups():
    """List all available database snapshots, newest first, with their creation time (UTC)."""
    backup_list = []
    for name in list_snapshots():
        try:
            size = os.path.getsize(os.path.join(BACKUP_DIR, name))
        except OSError:
            continue
        backup_list.append(
            {
                "filename": name,
                "size_kb": round(size / 1024, 2),
                "timestamp": iso(snapshot_time(name)),
                **snapshot_info(name),
            }
        )
    return backup_list


@app.post("/api/rollback", tags=["database"])
def post_rollback(filename: str = Form(...)):
    """Manually restore the database to a selected snapshot."""
    if not BACKUP_NAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid snapshot file name.")
    if not os.path.exists(os.path.join(BACKUP_DIR, filename)):
        raise HTTPException(status_code=404, detail="Backup snapshot file not found.")

    logger.info(f"Triggering manual database rollback to: {filename}")
    try:
        error = rollback_to(filename)
    except OSError as e:
        logger.error(f"Manual rollback failed: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
    return {
        "status": "success" if error is None else "restored_with_errors",
        "message": f"Database successfully rolled back to snapshot {filename}.",
    }


@app.post("/api/rca/generate", tags=["reports"])
def post_generate_rca():
    """Manually trigger the AI agent to generate a Root Cause Analysis report."""
    if _reports_in_flight:
        return {"status": "busy", "message": "A report is already being generated."}
    claim_fault("report", REPORT_COOLDOWN_SEC)
    trigger_auto_rca()
    return {"status": "started", "message": "Root Cause Analysis report generation triggered."}


@app.post("/api/settings/apikey", tags=["settings"])
def post_save_apikey(api_key: str = Form("")):
    """Set the Gemini API key for this server process (local runs only, never saved to disk)."""
    global _runtime_api_key
    if PUBLIC_DEMO:
        raise HTTPException(
            status_code=403,
            detail="The API key cannot be changed on the public demo.",
        )
    api_key = api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key cannot be empty.")
    _runtime_api_key = api_key
    threading.Thread(target=load_llm, kwargs={"relearn": True}, daemon=True).start()
    logger.info("Gemini API key set for this process.")
    return {
        "status": "success",
        "message": (
            "API key set until the server restarts. It is not saved to disk: "
            "put GOOGLE_API_KEY in your .env file to keep it."
        ),
    }


@app.get("/api/demo", tags=["guided demo"])
def get_demo():
    """The current or last guided-demo run, and the cooldown before the next one."""
    return {"run": demo_state(), "cooldown": cooldown_left("demo", DEMO_COOLDOWN_SEC)}


@app.post("/api/demo/start", tags=["guided demo"])
def post_demo_start():
    """Start the guided demo, unless a run is in progress or has only just finished."""
    global _demo, _demo_seq
    with _lock:
        if _demo and _demo["state"] == "running":
            raise HTTPException(
                status_code=409,
                detail=(
                    "A guided demo is already running on this shared server. Its progress is "
                    "shown on the overview."
                ),
            )
        wait = cooldown_left("demo", DEMO_COOLDOWN_SEC)
        if wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    "The guided demo has just finished. This server is shared, so it can run "
                    f"again in {wait} s."
                ),
                headers={"Retry-After": str(wait)},
            )
        _demo_seq += 1
        _demo = new_demo_run(_demo_seq)
    log_event("info", "Guided demo started.")
    threading.Thread(target=run_demo_scenario, daemon=True).start()
    return demo_state()


# --- CHAOS INJECTION ENDPOINTS ---


@app.post("/fault/memory_leak", tags=["chaos lab"])
def inject_memory_leak(size_mb: int = MAX_LEAK_STEP_MB):
    """Simulates a memory leak by holding a block of memory until it is released."""
    if not 1 <= size_mb <= MAX_LEAK_STEP_MB:
        raise HTTPException(
            status_code=400, detail=f"size_mb must be between 1 and {MAX_LEAK_STEP_MB}."
        )
    held = leaked_mb()
    if held + size_mb > MAX_LEAK_MB:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The demo already holds {held} MB of leaked memory (limit {MAX_LEAK_MB} MB). "
                "Release it first."
            ),
        )
    claim_fault("memory_leak")
    note_fault("memory_leak", f"Chaos Lab: leaked {size_mb} MB (holding {held + size_mb} MB).")
    total = hold_leak(size_mb)
    log_event("warn", f"Chaos Lab: leaked {size_mb} MB of memory (now holding {total} MB).")
    with _lock:
        blocks = len(_memory_leak_array)
    return {
        "status": "fault_injected",
        "type": "memory_leak",
        "allocated_mb": size_mb,
        "held_mb": total,
        "total_leaked_blocks": blocks,
    }


@app.post("/fault/memory_release", tags=["chaos lab"])
def release_memory():
    """Free all memory held by the memory leak fault."""
    return {"status": "released", "released_mb": release_leak("Chaos Lab")}


@app.post("/fault/cpu_spike", tags=["chaos lab"])
def inject_cpu_spike(duration_sec: int = MAX_SPIKE_SEC):
    """Simulates a CPU spike with a busy loop in a background thread."""
    if not 1 <= duration_sec <= MAX_SPIKE_SEC:
        raise HTTPException(
            status_code=400, detail=f"duration_sec must be between 1 and {MAX_SPIKE_SEC}."
        )
    claim_fault("cpu_spike")
    note_fault("cpu_spike", f"Chaos Lab: CPU busy loop for {duration_sec} s.")
    log_event("warn", f"Chaos Lab: CPU spike for {duration_sec} s.")

    def busy_loop():
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            _ = 1000000 * 1000000

    # Run in background to prevent blocking main web app requests completely
    threading.Thread(target=busy_loop, daemon=True).start()
    return {"status": "fault_injected", "type": "cpu_spike", "duration": duration_sec}


def hard_exit():
    os._exit(1)  # Immediate hard exit


@app.post("/fault/fatal_crash", tags=["chaos lab"])
def inject_fatal_crash():
    """Simulates a fatal application crash by exiting the process (local runs only)."""
    if PUBLIC_DEMO:
        raise HTTPException(
            status_code=403,
            detail=(
                "Disabled on the public demo: it would take the site down for everyone. "
                "Run DeepBell locally (python run_all.py) to try it."
            ),
        )
    logger.fatal("FAULT INJECTED: Fatal crash triggered. Shutting down process immediately.")

    def kill_soon():
        time.sleep(1)
        hard_exit()

    threading.Thread(target=kill_soon, daemon=True).start()
    return {
        "status": "fault_injected",
        "type": "fatal_crash",
        "message": "Process will terminate in 1 second.",
    }


@app.post("/fault/corrupt_db", tags=["chaos lab"])
def inject_db_corruption():
    """Simulates database corruption by writing garbage over the SQLite header."""
    claim_fault("corrupt_db")
    if not corrupt_database("Chaos Lab"):
        return {"status": "failed_to_corrupt"}
    return {"status": "fault_injected", "type": "corrupt_db"}
