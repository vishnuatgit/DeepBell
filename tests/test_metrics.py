"""The /metrics endpoint: metric names, values and what moves them (no network)."""

import random
import time

import psutil
from prometheus_client.parser import text_string_to_metric_families
from test_features import idle, no_reports, sample, train, wall  # noqa: F401

from deepbell import chaos, config, incidents, metrics, rca, runtime, snapshots


def value(name, **labels):
    """Current value of one sample in DeepBell's registry (0 if it has not been created yet)."""
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_serves_every_metric_name(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    families = {family.name for family in text_string_to_metric_families(response.text)}
    assert {
        "deepbell_cpu_percent",
        "deepbell_memory_mb",
        "deepbell_anomaly_score",
        "deepbell_incidents_open",
        "deepbell_incidents",
        "deepbell_detection_seconds",
        "deepbell_recovery_seconds",
        "deepbell_fault_injections",
        "deepbell_reports_generated",
        "deepbell_snapshot_restores",
    } <= families


def test_gauges_follow_the_latest_sample(client, wall, no_reports):  # noqa: F811
    rng = random.Random(1)
    mono = train(wall, rng)
    mono = sample(wall, mono, 3.0, 131.0)
    assert value("deepbell_cpu_percent") == 3.0
    assert value("deepbell_memory_mb") == 131.0
    assert value("deepbell_anomaly_score") == 0.0
    assert value("deepbell_incidents_open") == 0.0
    mono = sample(wall, mono, 3.0, 200.0)  # anomalous
    assert value("deepbell_anomaly_score") == 1.0
    mono = sample(wall, mono, 3.0, 200.0)  # second in a row: the incident opens
    assert value("deepbell_incidents_open") == 1.0
    assert "deepbell_incidents_open 1.0" in client.get("/metrics").text


def test_incident_counters_and_histograms(client, wall, no_reports):  # noqa: F811
    rng = random.Random(2)
    opened = value("deepbell_incidents_total", kind="memory_leak")
    faults = value("deepbell_fault_injections_total", kind="memory_leak")
    detected = value("deepbell_detection_seconds_count")
    recovered = value("deepbell_recovery_seconds_count")
    detect_sum = value("deepbell_detection_seconds_sum")
    mono = train(wall, rng)
    incidents.note_fault("memory_leak", "Chaos Lab: leaked 60 MB.")
    mono = sample(wall, mono, 2.0, 195.0, step=1)
    mono = sample(wall, mono, 2.0, 195.5)  # opens 3 s after the fault
    for _ in range(config.CLOSE_AFTER):
        mono = sample(wall, mono, *idle(rng))
    assert value("deepbell_fault_injections_total", kind="memory_leak") == faults + 1
    assert value("deepbell_incidents_total", kind="memory_leak") == opened + 1
    assert value("deepbell_detection_seconds_count") == detected + 1
    assert value("deepbell_detection_seconds_sum") == detect_sum + 3
    assert value("deepbell_recovery_seconds_count") == recovered + 1
    assert value("deepbell_incidents_open") == 0.0


def test_report_and_restore_counters(client, monkeypatch):
    reports_before = value("deepbell_reports_generated_total", source="template")
    restores_before = value("deepbell_snapshot_restores_total")
    rca.generate_rca_report_task()
    assert value("deepbell_reports_generated_total", source="template") == reports_before + 1
    name = snapshots.take_snapshot()
    chaos.rollback_to(name)
    assert value("deepbell_snapshot_restores_total") == restores_before + 1
    assert runtime.open_incidents == {}


def test_end_to_end_memory_leak_shows_up_in_metrics_incidents_and_report(client, wall):  # noqa: F811
    """Inject a real leak through the API, let the watchdog logic flag it, then read the result
    from /metrics, the incident API and the written report."""
    rng = random.Random(3)
    base = psutil.Process().memory_info().rss / config.MB
    mono = 0.0
    for _ in range(30):  # idle samples around today's real memory use
        mono = sample(wall, mono, rng.uniform(0.5, 6.0), base + rng.uniform(-1.0, 1.0))
    opened = value("deepbell_incidents_total", kind="memory_leak")
    reports_before = value("deepbell_reports_generated_total", source="template")

    assert client.post("/fault/memory_leak", params={"size_mb": 60}).status_code == 200
    for _ in range(config.OPEN_AFTER):  # the watchdog reads the real memory use
        mono = sample(wall, mono, 2.0, psutil.Process().memory_info().rss / config.MB)
    status = client.get("/api/status").json()
    assert status["incidents"][0]["kind"] == "memory_leak"
    text = client.get("/metrics").text
    assert "deepbell_incidents_open 1.0" in text
    assert 'deepbell_incidents_total{kind="memory_leak"} %s' % (opened + 1) in text

    for _ in range(100):  # the report is written by a background thread
        if runtime.reports_in_flight == 0:
            break
        time.sleep(0.1)
    assert value("deepbell_reports_generated_total", source="template") == reports_before + 1
    assert client.get("/api/incidents").json()[0]["report_available"] is True

    client.post("/fault/memory_release")
    for _ in range(config.CLOSE_AFTER):
        mono = sample(wall, mono, 2.0, base)
    assert "deepbell_incidents_open 0.0" in client.get("/metrics").text
    assert value("deepbell_recovery_seconds_count") >= 1
