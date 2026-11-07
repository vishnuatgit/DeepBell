"""Process-wide mutable state, kept in one place.

DeepBell runs as a single process (one uvicorn worker): the watchdog, the snapshot thread, open
incidents and Chaos Lab state all live here. Other modules read and write these names through
the module (`runtime.open_incidents`, `runtime.detector`), never by importing them directly, so
a rebinding such as `runtime.demo = ...` is seen everywhere.
"""

import mmap
import threading
from collections import deque
from datetime import UTC, datetime

from . import config
from .alert_rules import AlertEngine
from .detection import Detector

# Locks
lock = threading.RLock()  # guards everything below
db_file_lock = threading.RLock()  # serialises access to app.db (queries, rollback, corruption)
report_lock = threading.Lock()  # one report is written at a time
incident_lock = threading.Lock()  # serialises read-modify-write of incident timelines

# Watchdog
detector = Detector()
latest_anomaly_score = 0.0
last_cpu = 0.0
last_ram = 0.0
last_sample_at: datetime | None = None  # wall-clock time of the newest telemetry sample
started_at = datetime.now(UTC)  # when this process started serving (reset by startup_event)

# Alert rules (loaded from alert_rules.json at startup)
alert_engine = AlertEngine()

# Incidents
open_incidents: dict[str, dict] = {}
incidents_rev = 0  # bumped on every incident change, so the dashboard knows when to reload
db_error: str | None = None
# Incident kind -> (time, description) of the last fault injected while no incident of that
# kind was open; the next incident of that kind starts at that time.
pending_faults: dict[str, tuple[str, str]] = {}
# Incident kind -> (time, description) of the first anomalous sample in the current streak.
first_flagged: dict[str, tuple[str, str]] = {}

# Reports and the Gemini client
reports_in_flight = 0
api_key = ""  # set from the Settings tab on local runs; never written to disk
llm_loaded = False

# Dashboard console events
events: deque = deque(maxlen=config.EVENTS_KEEP)
event_seq = 0

# Chaos Lab and the guided demo
memory_leak_array: list[mmap.mmap] = []
leak_since: float | None = None  # monotonic time the oldest held leak block was allocated
last_fault_at: dict[str, float] = {}
demo: dict | None = None  # the current or last guided-demo run
demo_seq = 0
