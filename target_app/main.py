import glob
import hashlib
import logging
import math
import mmap
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
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

# Snapshots, reports and data
SNAPSHOT_INTERVAL_SEC = 30
SNAPSHOTS_KEEP = 10
REPORTS_KEEP = 20
TRANSACTIONS_KEEP = 200
MAX_AMOUNT = 1_000_000
EVENTS_KEEP = 100

# Chaos Lab limits. The demo is shared, so every fault has a global cooldown.
MAX_SPIKE_SEC = 5
MAX_LEAK_MB = 120
MAX_LEAK_STEP_MB = 60
FAULT_COOLDOWN_SEC = 20
REPORT_COOLDOWN_SEC = 30
MB = 1024 * 1024

INCIDENT_LABELS = {
    "db_corruption": "Degraded (Database Error)",
    "memory_leak": "Anomalous (Memory Leak)",
    "cpu_spike": "Anomalous (CPU Spike)",
}
DETECTED_KINDS = ("cpu_spike", "memory_leak")
SAMPLE_LABELS = {"cpu_spike": "CPU Spike", "memory_leak": "Memory Leak"}
FAULT_NAMES = {
    "cpu_spike": "CPU spike",
    "memory_leak": "Memory leak",
    "corrupt_db": "Database corruption",
    "report": "Report generation",
}


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


app = FastAPI(title="DeepBell SRE Cockpit", lifespan=lifespan)

# Dashboard UI Setup
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
# Cache-buster for the stylesheet URL, so a redeploy never pairs new markup with stale CSS.
with open(os.path.join(BASE_DIR, "static", "styles.css"), "rb") as _css:
    STATIC_VERSION = hashlib.sha256(_css.read()).hexdigest()[:10]

# Global states
_lock = threading.RLock()
_db_file_lock = threading.RLock()  # serialises access to app.db (queries, rollback, corruption)
_report_lock = threading.Lock()
_memory_leak_array: list[mmap.mmap] = []
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


def restore_snapshot(name: str) -> None:
    with _db_file_lock:
        shutil.copyfile(os.path.join(BACKUP_DIR, name), DB_FILE)


def restore_latest_backup() -> bool:
    """Restore the newest snapshot that passes an integrity check."""
    for name in list_snapshots():
        if integrity_error(os.path.join(BACKUP_DIR, name)) is None:
            logger.info(f"Restoring database from snapshot: {name}")
            try:
                restore_snapshot(name)
                logger.info("Database restored successfully.")
                return True
            except OSError as e:
                logger.error(f"Failed to restore database: {e}")
                return False
    logger.error("No backups available for restoration.")
    return False


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


def open_incident(kind: str, detail: str = "") -> bool:
    """Open an incident and write its report, unless one of the same kind is already open."""
    with _lock:
        if kind in open_incidents:
            return False
        open_incidents[kind] = {
            "kind": kind,
            "label": INCIDENT_LABELS[kind],
            "opened_at": iso(utcnow()),
            "detail": detail,
        }
    log_event("error", f"Incident opened: {INCIDENT_LABELS[kind]}. {detail}".strip())
    trigger_auto_rca(kind)
    return True


def close_incident(kind: str) -> None:
    with _lock:
        incident = open_incidents.pop(kind, None)
    if incident:
        opened = parse_utc(incident["opened_at"])
        seconds = int((utcnow() - opened).total_seconds()) if opened else 0
        log_event("success", f"Incident resolved: {incident['label']} after {seconds} s.")


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
    global last_cpu, last_ram, latest_anomaly_score
    cpu, ram = round(cpu, 1), round(ram, 1)
    with _lock:
        kinds, opened, closed = detector.observe(cpu, ram, time.monotonic() if now is None else now)
        status = sample_status(kinds)
        last_cpu, last_ram = cpu, ram
        latest_anomaly_score = 1.0 if kinds else 0.0
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
            f"CPU {cpu}% (baseline {base['cpu']}%)."
            if kind == "cpu_spike"
            else f"Memory {ram} MB (baseline {base['ram']} MB)."
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

    def run():
        global _reports_in_flight
        try:
            generate_rca_report_task(incident)
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


def build_template_report(
    rows: list[dict], generated_at: str, note: str = "", incident: str = "", detail: str = ""
) -> str:
    """Deterministic incident report used when no Gemini API key is configured."""
    if not rows and not incident:
        return (
            f"# Incident Report\n\nGenerated: {generated_at}\n\n"
            "No telemetry has been recorded yet, so there is nothing to analyse."
        )
    cpus = [r["cpu"] for r in rows]
    rams = [r["ram"] for r in rows]
    anomalies = [r for r in rows if "Anomalous" in (r["status"] or "")]
    if anomalies:
        latest = anomalies[-1]["status"]
        kind = incident or latest
        first = anomalies[0]["timestamp"]
        summary = (
            f"The watchdog flagged {len(anomalies)} of the last {len(rows)} samples. "
            f"The first flagged sample was at {first}; the latest classification is **{latest}**."
        )
    else:
        kind = incident or "No anomaly in window"
        summary = f"None of the last {len(rows)} samples was flagged by the watchdog."
    if detail:
        summary += f"\n\nDetail: {detail}"
    if "Database" in kind:
        cause = (
            "The application database no longer reads as SQLite: its file was overwritten or "
            "damaged on disk. CPU and memory are not expected to move for this kind of fault."
        )
        actions = [
            "Restore the newest good snapshot from the Time Machine",
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


def latest_report() -> dict | None:
    for name in list_reports():
        try:
            with open(os.path.join(REPORTS_DIR, name), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        match = REPORT_NAME_RE.match(name)
        generated = (
            iso(datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC))
            if match
            else None
        )
        return {"filename": name, "generated_at": generated, "content": content}
    return None


def generate_rca_report_task(incident: str = "") -> str:
    """Generate a report (AI if a key is set, template otherwise) and save it. Returns the path."""
    with _report_lock:
        logger.info("Starting Root Cause Analysis...")
        try:
            rows = fetch_recent_metrics(15)
            generated_at = iso(utcnow())
            label = INCIDENT_LABELS.get(incident, "")
            with _lock:
                detail = open_incidents.get(incident, {}).get("detail", "")
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
                content = build_template_report(rows, generated_at, note, label, detail)
            report_path = write_report(content)
            log_event(
                "success",
                f"Incident report written ({source}): {os.path.basename(report_path)}",
            )
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
    # Verify (and if needed restore) the database before touching its schema:
    # init_db() raises on a corrupted file, which would crash every restart.
    verify_and_heal_database()
    init_db()
    init_metrics_db()
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


# --- COCKPIT UI ROUTES ---


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


def fault_status() -> dict:
    return {
        "cooldown_sec": FAULT_COOLDOWN_SEC,
        "cpu_spike": {"cooldown": cooldown_left("cpu_spike"), "max_sec": MAX_SPIKE_SEC},
        "memory_leak": {
            "cooldown": cooldown_left("memory_leak"),
            "held_mb": leaked_mb(),
            "limit_mb": MAX_LEAK_MB,
            "step_mb": MAX_LEAK_STEP_MB,
        },
        "corrupt_db": {"cooldown": cooldown_left("corrupt_db")},
        "fatal_crash": {"enabled": not PUBLIC_DEMO},
        "report": {"cooldown": cooldown_left("report", REPORT_COOLDOWN_SEC)},
    }


def latest_snapshot() -> dict | None:
    snapshots = list_snapshots()
    if not snapshots:
        return None
    return {"filename": snapshots[0], "timestamp": iso(snapshot_time(snapshots[0]))}


@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request):
    """Serve the SRE Cockpit Dashboard."""
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
            "config": {
                "publicDemo": PUBLIC_DEMO,
                "maxSpikeSec": MAX_SPIKE_SEC,
                "leakStepMb": MAX_LEAK_STEP_MB,
                "leakLimitMb": MAX_LEAK_MB,
                "cooldownSec": FAULT_COOLDOWN_SEC,
                "cpuDeltaPct": CPU_MIN_DELTA,
                "ramDeltaMb": RAM_MIN_DELTA_MB,
                "snapshotIntervalSec": SNAPSHOT_INTERVAL_SEC,
                "snapshotsKeep": SNAPSHOTS_KEEP,
            },
        },
    )


@app.get("/healthz")
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


@app.post("/transaction")
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


@app.get("/transactions")
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


# --- COCKPIT REST APIs ---


@app.get("/api/status")
def get_system_status(events_after: int | None = None):
    """Returns the live status of the cockpit (and, if asked, events newer than events_after)."""
    with _lock:
        incidents = [open_incidents[k] for k in INCIDENT_LABELS if k in open_incidents]
        baseline = dict(detector.baseline) if detector.baseline else None
        if baseline:
            baseline["live_samples"] = detector.live_count()
            baseline["live_needed"] = LIVE_FIT_SAMPLES
        warming = detector.baseline is None
        db_error = _db_error
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
        "incidents": [dict(i) for i in incidents],
        "anomaly_score": latest_anomaly_score,
        "cpu": last_cpu,
        "ram": last_ram,
        "cpu_capacity": CPU_CAPACITY,
        "baseline": baseline,
        "database": {"ok": db_error is None, "error": db_error},
        "is_generating_rca": _reports_in_flight > 0,
        "api_key_configured": bool(get_api_key()),
        "public_demo": PUBLIC_DEMO,
        "faults": fault_status(),
        "latest_report": (list_reports() or [None])[0],
        "latest_snapshot": latest_snapshot(),
        "server_time": iso(utcnow()),
    }
    if events_after is not None:
        events = recent_events(events_after)
        result["events"] = events
        result["event_seq"] = events[-1]["seq"] if events else events_after
    return result


@app.get("/api/metrics")
def get_metrics(limit: int = 15):
    """Fetch the latest telemetry entries, newest first (timestamps in UTC)."""
    limit = max(1, min(limit, 300))
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


@app.get("/api/reports/latest")
def get_latest_report():
    """The newest incident report (Markdown), or 404 if none exists yet."""
    report = latest_report()
    if not report:
        raise HTTPException(status_code=404, detail="No incident reports yet.")
    return report


@app.get("/api/backups")
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
            }
        )
    return backup_list


@app.post("/api/rollback")
def post_rollback(filename: str = Form(...)):
    """Manually restore the database to a selected snapshot."""
    if not BACKUP_NAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid snapshot file name.")
    target_backup = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(target_backup):
        raise HTTPException(status_code=404, detail="Backup snapshot file not found.")

    logger.info(f"Triggering manual database rollback to: {filename}")
    try:
        restore_snapshot(filename)
    except OSError as e:
        logger.error(f"Manual rollback failed: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
    log_event("success", f"Time Machine: database rolled back to {filename}.")
    error = refresh_db_state()
    return {
        "status": "success" if error is None else "restored_with_errors",
        "message": f"Database successfully rolled back to snapshot {filename}.",
    }


@app.post("/api/rca/generate")
def post_generate_rca():
    """Manually trigger the AI agent to generate a Root Cause Analysis report."""
    if _reports_in_flight:
        return {"status": "busy", "message": "A report is already being generated."}
    claim_fault("report", REPORT_COOLDOWN_SEC)
    trigger_auto_rca()
    return {"status": "started", "message": "Root Cause Analysis report generation triggered."}


@app.post("/api/settings/apikey")
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


# --- CHAOS INJECTION ENDPOINTS ---


def allocate_leak(size_mb: int) -> mmap.mmap:
    """Anonymous memory with every page written, so it counts towards RSS at once and goes
    back to the operating system as soon as it is closed."""
    block = mmap.mmap(-1, size_mb * MB)
    for offset in range(0, size_mb * MB, mmap.PAGESIZE):
        block[offset] = 1
    return block


@app.post("/fault/memory_leak")
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
    block = allocate_leak(size_mb)
    with _lock:
        _memory_leak_array.append(block)
    total = leaked_mb()
    log_event("warn", f"Chaos Lab: leaked {size_mb} MB of memory (now holding {total} MB).")
    return {
        "status": "fault_injected",
        "type": "memory_leak",
        "allocated_mb": size_mb,
        "held_mb": total,
        "total_leaked_blocks": len(_memory_leak_array),
    }


@app.post("/fault/memory_release")
def release_memory():
    """Free all memory held by the memory leak fault."""
    with _lock:
        blocks = list(_memory_leak_array)
        _memory_leak_array.clear()
    released = sum(len(b) for b in blocks) // MB
    for block in blocks:
        block.close()
    if released:
        log_event("info", f"Chaos Lab: released {released} MB of leaked memory.")
    return {"status": "released", "released_mb": released}


@app.post("/fault/cpu_spike")
def inject_cpu_spike(duration_sec: int = MAX_SPIKE_SEC):
    """Simulates a CPU spike with a busy loop in a background thread."""
    if not 1 <= duration_sec <= MAX_SPIKE_SEC:
        raise HTTPException(
            status_code=400, detail=f"duration_sec must be between 1 and {MAX_SPIKE_SEC}."
        )
    claim_fault("cpu_spike")
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


@app.post("/fault/fatal_crash")
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


@app.post("/fault/corrupt_db")
def inject_db_corruption():
    """Simulates database corruption by writing garbage over the SQLite header."""
    claim_fault("corrupt_db")
    # Make sure there is something to roll back to.
    if not list_snapshots():
        take_snapshot()
    try:
        # Overwrite the SQLite header. Appending bytes is not enough: SQLite
        # ignores trailing data, so the file would still open normally.
        with _db_file_lock, open(DB_FILE, "r+b") as f:
            f.write(b"GARBAGE DATA CORRUPTION " * 5)
    except OSError as e:
        logger.error(f"Failed to corrupt DB: {e}")
        return {"status": "failed_to_corrupt"}
    log_event("warn", "Chaos Lab: wrote garbage over the database header.")
    refresh_db_state()
    return {"status": "fault_injected", "type": "corrupt_db"}
