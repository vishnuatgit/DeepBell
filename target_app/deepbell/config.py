"""Settings and constants: paths, environment variables, detection thresholds and caps.

Every module imports this first. On import it loads .env (local runs only), sets up logging
and creates the data folders. Modules read values as `config.NAME` when they run, so a test
can change one with monkeypatch.
"""

import logging
import os
import re
import sys

from dotenv import load_dotenv


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# Directory Definitions
# target_app/, the folder that holds main.py, templates/ and static/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
