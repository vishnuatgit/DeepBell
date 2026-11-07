"""Prometheus metrics, served at /metrics.

The gauges read DeepBell's live state when Prometheus scrapes (`set_function`), so the
watchdog thread does no extra work. The counters and histograms are updated at the moment
the event happens (an incident opens, a fault is injected, a report is written).
A private registry keeps the output to DeepBell's own metrics and lets tests read it.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from . import config, runtime

REGISTRY = CollectorRegistry()

# Detection and recovery take seconds to a couple of minutes in this lab.
TIME_BUCKETS = (2, 4, 6, 10, 20, 30, 60, 120, 300, 600)

CPU = Gauge("deepbell_cpu_percent", "Latest CPU sample, % of available CPU.", registry=REGISTRY)
MEMORY = Gauge("deepbell_memory_mb", "Latest resident memory sample, MB.", registry=REGISTRY)
ANOMALY_SCORE = Gauge(
    "deepbell_anomaly_score",
    "Latest sample: 1 if the watchdog flagged it as anomalous, else 0.",
    registry=REGISTRY,
)
INCIDENTS_OPEN = Gauge(
    "deepbell_incidents_open", "Incidents that are open right now.", registry=REGISTRY
)
INCIDENTS = Counter(
    "deepbell_incidents", "Incidents opened.", ["kind"], registry=REGISTRY
)  # exposed as deepbell_incidents_total
DETECTION_SECONDS = Histogram(
    "deepbell_detection_seconds",
    "Time to detect: from the start of an incident to the watchdog opening it.",
    buckets=TIME_BUCKETS,
    registry=REGISTRY,
)
RECOVERY_SECONDS = Histogram(
    "deepbell_recovery_seconds",
    "Time to recover: from the watchdog opening an incident to it resolving.",
    buckets=TIME_BUCKETS,
    registry=REGISTRY,
)
FAULTS = Counter(
    "deepbell_fault_injections", "Chaos Lab faults injected.", ["kind"], registry=REGISTRY
)
REPORTS = Counter(
    "deepbell_reports_generated", "Incident reports written.", ["source"], registry=REGISTRY
)
SNAPSHOT_RESTORES = Counter(
    "deepbell_snapshot_restores", "Database snapshot restores.", registry=REGISTRY
)

# Create the labelled series up front, so they read 0 (not "no data") before the first event.
for _kind in config.INCIDENT_NAMES:
    INCIDENTS.labels(_kind)
    FAULTS.labels(_kind)
for _source in ("template", "Gemini"):
    REPORTS.labels(_source)

CPU.set_function(lambda: runtime.last_cpu)
MEMORY.set_function(lambda: runtime.last_ram)
ANOMALY_SCORE.set_function(lambda: runtime.latest_anomaly_score)
INCIDENTS_OPEN.set_function(lambda: len(runtime.open_incidents))


def render() -> bytes:
    """The metrics in Prometheus' text format."""
    return generate_latest(REGISTRY)
