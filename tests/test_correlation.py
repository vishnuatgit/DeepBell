"""Incident correlation: the likely-cause rules, the merged timeline, the API and the report."""

import random
import re
import time
from datetime import UTC, datetime, timedelta

import psutil
from test_features import sample, wall  # noqa: F401

from deepbell import config, correlation, reports, runtime, snapshots, timeutil

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def at(sec):
    return timeutil.iso(T0 + timedelta(seconds=sec))


def event(sec, kind, text):
    return {"at": at(sec), "type": kind, "text": text}


def incident(kind="memory_leak", events=(), detected=10, incident_id=1, detail=""):
    return {
        "id": incident_id,
        "kind": kind,
        "name": config.INCIDENT_NAMES[kind],
        "started_at": at(0),
        "detected_at": at(detected),
        "resolved_at": None,
        "detail": detail,
        "timeline": list(events),
    }


def quiet_samples(start=-30, end=0):
    """Idle telemetry every 2 s from `start` to `end` seconds."""
    return [
        {"at": at(t), "cpu": 2.0, "ram": 130.0, "status": "Normal"} for t in range(start, end, 2)
    ]


def moving(samples, cpu_at=None, ram_at=None, until=14):
    """Idle samples followed by samples where CPU and/or memory cross their alert lines."""
    rows = list(samples)
    for t in range(0, until, 2):
        rows.append(
            {
                "at": at(t),
                "cpu": 90.0 if cpu_at is not None and t >= cpu_at else 2.0,
                "ram": 200.0 if ram_at is not None and t >= ram_at else 130.0,
                "status": "Anomalous",
            }
        )
    return rows


LEAK = "Chaos Lab: leaked 60 MB (holding 60 MB)."


# --- The rules ---


def test_matching_fault_just_before_the_anomaly_gives_high_confidence():
    inc = incident(
        events=[
            event(0, "fault", LEAK),
            event(2, "flagged", "First anomalous sample: memory 200 MB."),
            event(4, "detected", "Incident opened."),
        ],
        detected=4,
    )
    cause = correlation.likely_cause(inc, moving(quiet_samples(), ram_at=2), [])
    assert cause["confidence"] == "high"
    assert cause["trigger"] == "Chaos Lab: leaked 60 MB (holding 60 MB)"
    assert "2 s before the first anomalous sample" in cause["evidence"][0]
    assert any("CPU stayed within its normal range" in e for e in cause["evidence"])
    assert cause["heuristic"] is True and "not proof" in cause["disclaimer"]


def test_a_fault_long_before_the_anomaly_is_only_medium():
    inc = incident(
        events=[event(0, "fault", LEAK), event(25, "flagged", "First anomalous sample.")],
        detected=29,
    )
    cause = correlation.likely_cause(inc)
    assert cause["confidence"] == "medium"
    assert "25 s" in cause["confidence_reason"]


def test_if_the_other_metric_moved_first_confidence_drops():
    inc = incident(
        events=[event(0, "fault", LEAK), event(6, "flagged", "First anomalous sample.")],
        detected=8,
    )
    samples = moving(quiet_samples(), cpu_at=0, ram_at=6)  # CPU led memory by 6 s
    cause = correlation.likely_cause(inc, samples, [])
    assert cause["confidence"] == "medium"
    assert any("CPU crossed its alert line 6 s before memory" in e for e in cause["evidence"])
    assert "partly disagree" in cause["confidence_reason"]


def test_evidence_has_no_clock_times():
    """Clock times would be UTC here while the timeline shows local time, so evidence uses
    only offsets."""
    inc = incident(
        events=[event(0, "fault", LEAK), event(6, "flagged", "First anomalous sample.")],
        detected=8,
    )
    for samples in (
        moving(quiet_samples(), ram_at=0),  # memory alone
        moving(quiet_samples(), cpu_at=0, ram_at=0),  # together
        moving(quiet_samples(), cpu_at=6, ram_at=0),  # memory first
    ):
        cause = correlation.likely_cause(inc, samples, [])
        assert any("crossed" in e or "moved first" in e for e in cause["evidence"])
        assert not any(re.search(r"\d\d:\d\d:\d\d|UTC", e) for e in cause["evidence"])


def test_an_earlier_metric_blip_that_ended_is_not_a_conflict():
    """CPU spiked 12 s before the leak but was back to normal when the leak began."""
    inc = incident(
        events=[event(0, "fault", LEAK), event(6, "flagged", "First anomalous sample.")],
        detected=8,
    )
    samples = quiet_samples()
    for t in range(-12, 4, 2):
        samples.append(
            {"at": at(t), "cpu": 90.0 if t < -6 else 2.0, "ram": 130.0, "status": "Normal"}
        )
    samples += [{"at": at(t), "cpu": 2.0, "ram": 200.0, "status": "Anomalous"} for t in (6, 8)]
    cause = correlation.likely_cause(inc, samples, [])
    assert cause["confidence"] == "high"
    assert not any("before memory" in e for e in cause["evidence"])


def test_without_a_fault_the_cause_is_unknown_and_never_overclaimed():
    inc = incident(events=[event(2, "flagged", "First anomalous sample.")], detected=4)
    cause = correlation.likely_cause(inc, moving(quiet_samples(), ram_at=2), [])
    assert cause["trigger"] == "Unknown" and cause["confidence"] == "low"
    assert "trigger is unknown" in cause["summary"]
    assert any("No injected fault" in n for n in cause["not_found"])
    assert correlation.likely_cause(inc)["confidence"] == "unknown"  # no telemetry either


def test_database_incident_rules():
    plain = incident("db_corruption", [event(4, "detected", "x")], detail="SQLite error: bad.")
    cause = correlation.likely_cause(plain)
    assert cause["confidence"] == "unknown" and cause["trigger"] == "Unknown"
    assert "SQLite error: bad." in cause["evidence"][0]
    injected = incident(
        "db_corruption",
        [event(0, "fault", "Chaos Lab: garbage written over the database file header.")],
        detected=2,
    )
    assert correlation.likely_cause(injected)["confidence"] == "high"


def test_overlapping_incidents_are_context_not_cause():
    inc = incident(events=[event(0, "fault", LEAK)], detected=10)
    near = incident("cpu_spike", incident_id=2, detected=7)
    far = incident("cpu_spike", incident_id=3, detected=300)
    cause = correlation.likely_cause(inc, None, [inc, near, far])
    context = [e for e in cause["evidence"] if e.startswith("Context")]
    assert len(context) == 1 and "#2" in context[0] and "3 s earlier" in context[0]
    assert "nothing here shows it" in context[0]
    assert cause["trigger"] != "Unknown"  # the fault still explains it; the overlap does not


def test_alerts_that_fired_are_listed_as_evidence():
    inc = incident(
        events=[
            event(0, "fault", LEAK),
            event(6, "alert", "Alert: Memory above baseline is firing: x (now 61)."),
        ]
    )
    cause = correlation.likely_cause(inc)
    assert any(
        "Alert(s) firing during the incident: Memory above baseline" in e for e in cause["evidence"]
    )


def test_too_little_telemetry_means_no_metric_claims():
    assert correlation.metric_crossings(quiet_samples(-4, 0), T0) is None


def test_markdown_section_lists_evidence_and_the_disclaimer():
    inc = incident(events=[event(0, "fault", LEAK), event(2, "flagged", "x")], detected=4)
    text = correlation.markdown_section(correlation.likely_cause(inc))
    assert text.startswith("## Correlation analysis (heuristic)")
    assert "Confidence: **high**" in text and "- Chaos Lab: leaked 60 MB" in text
    assert "not proof" in text


# --- Merged timeline, API and report, through the real app ---


def wait_for_reports():
    """Reports are written on a background thread: let it finish before the test ends."""
    for _ in range(100):
        if runtime.reports_in_flight == 0:
            return
        time.sleep(0.1)


def run_leak_incident(client, wall, release=True):  # noqa: F811
    """Inject a real leak, let it be detected, optionally release it. Returns the incident."""
    rng = random.Random(7)
    base = psutil.Process().memory_info().rss / config.MB
    mono = 0.0
    for _ in range(30):
        mono = sample(wall, mono, rng.uniform(0.5, 6.0), base + rng.uniform(-1.0, 1.0))
    assert client.post("/fault/memory_leak", params={"size_mb": 60}).status_code == 200
    for _ in range(3):
        mono = sample(wall, mono, 2.0, psutil.Process().memory_info().rss / config.MB)
    wait_for_reports()
    if release:
        client.post("/fault/memory_release")
        for _ in range(config.CLOSE_AFTER):
            mono = sample(wall, mono, 2.0, base)
    return client.get("/api/incidents").json()[0]


def test_end_to_end_timeline_cause_report_and_download(client, wall):  # noqa: F811
    summary = run_leak_incident(client, wall)
    detail = client.get(f"/api/incidents/{summary['id']}").json()

    types = [e["type"] for e in detail["timeline"]]
    assert types[0] == "fault" and types[1] == "flagged"
    for expected in ("alert", "detected", "report", "action", "resolved"):
        assert expected in types
    times = [e["at"] for e in detail["timeline"]]
    assert times == sorted(times)  # one timeline, in order
    assert all("offset_sec" in e for e in detail["timeline"])
    alert_texts = " ".join(e["text"] for e in detail["timeline"] if e["type"] == "alert")
    assert "Memory above baseline is pending" in alert_texts
    assert "Memory above baseline is firing" in alert_texts
    assert "Memory above baseline resolved" in alert_texts

    cause = detail["likely_cause"]
    assert cause["confidence"] == "high" and cause["heuristic"] is True
    assert cause["trigger"].startswith("Chaos Lab: leaked 60 MB")

    report = client.get(f"/api/reports/{detail['report']}").json()["content"]
    assert "## Correlation analysis (heuristic)" in report
    assert "Confidence: **high**" in report

    download = client.get(f"/api/reports/{detail['report']}/download").text
    assert download.count("## Correlation analysis (heuristic)") == 1  # not repeated


def test_download_adds_the_analysis_to_reports_written_without_it(client, wall):  # noqa: F811
    summary = run_leak_incident(client, wall)
    name = summary["report"]
    path = f"{config.REPORTS_DIR}/{name}"
    with open(path, encoding="utf-8") as f:
        original = f.read()
    stripped = original.replace("## Correlation analysis (heuristic)", "## Something else")
    with open(path, "w", encoding="utf-8") as f:
        f.write(stripped)  # the shape of a Gemini report: no analysis section
    download = reports.report_for_download(name)
    assert "## Correlation analysis (heuristic)" in download
    assert "Chaos Lab: leaked 60 MB" in download


def test_database_incident_timeline_has_the_restore_event(client, wall):  # noqa: F811
    rng = random.Random(8)
    mono = 0.0
    for _ in range(5):
        mono = sample(wall, mono, rng.uniform(0.5, 6.0), 130.0)
    client.post("/transaction", params={"amount": 5})
    assert client.post("/fault/corrupt_db").status_code == 200
    for _ in range(3):  # the "Database unhealthy" alert fires on a sample taken while it fails
        mono = sample(wall, mono, 2.0, 130.0)
    snapshot = snapshots.list_snapshots()[0]
    assert client.post("/api/rollback", data={"filename": snapshot}).status_code == 200
    mono = sample(wall, mono, 2.0, 130.0)
    wait_for_reports()
    detail = client.get(f"/api/incidents/{client.get('/api/incidents').json()[0]['id']}").json()
    types = [e["type"] for e in detail["timeline"]]
    assert types[0] == "fault" and "restore" in types and "resolved" in types
    assert any("Database unhealthy is firing" in e["text"] for e in detail["timeline"])
    assert detail["likely_cause"]["confidence"] == "high"
    assert "CPU and memory" not in " ".join(detail["likely_cause"]["evidence"])


def test_incident_without_a_fault_says_unknown_through_the_api(client, wall):  # noqa: F811
    rng = random.Random(9)
    mono = 0.0
    for _ in range(30):
        mono = sample(wall, mono, rng.uniform(0.5, 6.0), 130.0 + rng.uniform(-1, 1))
    for _ in range(3):
        mono = sample(wall, mono, 95.0, 130.0)  # no fault was injected
    wait_for_reports()
    inc = client.get("/api/incidents").json()[0]
    cause = client.get(f"/api/incidents/{inc['id']}").json()["likely_cause"]
    assert cause["trigger"] == "Unknown" and cause["confidence"] == "low"
    assert client.get("/api/incidents/999").status_code == 404
