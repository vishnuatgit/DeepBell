"""The operations console's data: the live pipeline, incident stats, report sources,
snapshot details, uptime and the OpenAPI tags."""

import os
import random
import shutil
from datetime import UTC, datetime, timedelta

import pytest

import main

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


class WallClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def wall(monkeypatch):
    clock = WallClock()
    monkeypatch.setattr(main, "utcnow", clock)
    return clock


@pytest.fixture()
def no_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "trigger_auto_rca", calls.append)
    return calls


def idle(rng):
    return rng.uniform(0.5, 6.0), 130.0 + rng.uniform(-1.5, 1.5)


def train(wall, rng, n=30):
    for i in range(n):
        wall.advance(main.SAMPLE_INTERVAL_SEC)
        main.record_sample(*idle(rng), now=float(i * main.SAMPLE_INTERVAL_SEC))
    return n * main.SAMPLE_INTERVAL_SEC


def sample(wall, mono, cpu, ram):
    wall.advance(main.SAMPLE_INTERVAL_SEC)
    main.record_sample(cpu, ram, now=mono + main.SAMPLE_INTERVAL_SEC)
    return mono + main.SAMPLE_INTERVAL_SEC


def stages(client):
    pipeline = client.get("/api/status").json()["pipeline"]
    return pipeline, {s["key"]: s["state"] for s in pipeline["stages"]}


# --- Live pipeline ---


def test_pipeline_before_any_sample(client):
    pipeline, states = stages(client)
    assert states == {"sample": "idle", "detect": "idle", "report": "idle", "recover": "idle"}
    assert pipeline["current"] is None
    assert pipeline["incident_id"] is None


def test_pipeline_follows_a_leak_from_detection_to_recovery(client, wall, no_reports):
    rng = random.Random(3)
    mono = train(wall, rng)
    pipeline, states = stages(client)
    assert states["sample"] == "active" and states["detect"] == "idle"
    assert pipeline["current"] == "sample"

    # One anomalous sample: detection is under way, nothing is open yet.
    main.hold_leak(1)
    mono = sample(wall, mono, 2.0, 195.0)
    pipeline, states = stages(client)
    assert states["detect"] == "active" and pipeline["current"] == "detect"
    assert f"1 of {main.OPEN_AFTER}" in pipeline["stages"][1]["detail"]

    # The second one opens the incident; the report is being written; recovery needs an
    # action because leaked memory is still held.
    mono = sample(wall, mono, 2.0, 195.5)
    pipeline, states = stages(client)
    incident_id = client.get("/api/incidents").json()[0]["id"]
    assert pipeline["incident_id"] == incident_id
    assert states == {
        "sample": "active",
        "detect": "done",
        "report": "active",
        "recover": "waiting",
    }
    assert pipeline["current"] == "report"
    assert "Release the 1 MB" in pipeline["stages"][3]["detail"]

    # A linked report completes the report stage; recovery is now the current step.
    main.update_incident(incident_id, ("report", "Report written."), report="RCA_Report_x.md")
    pipeline, states = stages(client)
    assert states["report"] == "done" and pipeline["current"] == "recover"

    # Released: normal samples count towards closing it, then it is done.
    main.free_leaked_memory()
    mono = sample(wall, mono, *idle(rng))
    pipeline, states = stages(client)
    assert states["recover"] == "active"
    assert f"1 of {main.CLOSE_AFTER}" in pipeline["stages"][3]["detail"]
    for _ in range(main.CLOSE_AFTER - 1):
        mono = sample(wall, mono, *idle(rng))
    pipeline, states = stages(client)
    assert states["recover"] == "done"
    assert "resolved after" in pipeline["stages"][3]["detail"]

    # A minute later the resolved incident leaves the pipeline.
    wall.advance(main.PIPELINE_RECENT_SEC + 1)
    main.record_sample(*idle(rng), now=mono + 61)
    pipeline, states = stages(client)
    assert pipeline["incident_id"] is None
    assert states["detect"] == states["report"] == states["recover"] == "idle"


def test_pipeline_asks_for_a_rollback_while_the_database_is_corrupt(client, no_reports):
    client.post("/transaction", params={"amount": 3})
    assert client.post("/fault/corrupt_db").status_code == 200
    pipeline, states = stages(client)
    assert states["recover"] == "waiting"
    assert "Time Machine" in pipeline["stages"][3]["detail"]
    snapshot = main.list_snapshots()[0]
    client.post("/api/rollback", data={"filename": snapshot})
    _, states = stages(client)
    assert states["recover"] == "done"


def test_pipeline_reports_a_stale_sampler(client, wall):
    main.record_sample(2.0, 130.0, now=0.0)
    wall.advance(3 * main.SAMPLE_INTERVAL_SEC + 1)
    pipeline, states = stages(client)
    assert states["sample"] == "idle"
    assert "No sample for" in pipeline["stages"][0]["detail"]


def test_pipeline_follows_a_new_fault_right_after_an_incident_resolves(client, wall, no_reports):
    rng = random.Random(5)
    mono = train(wall, rng)
    # A memory incident opens and resolves; for a minute the pipeline shows it as done.
    for ram in (195.0, 195.5):
        mono = sample(wall, mono, 2.0, ram)
    for _ in range(main.CLOSE_AFTER):
        mono = sample(wall, mono, *idle(rng))
    pipeline, states = stages(client)
    assert states["detect"] == states["recover"] == "done"

    # A CPU spike injected seconds later takes over the pipeline at once.
    wall.advance(2)
    main.note_fault("cpu_spike", "Chaos Lab: CPU busy loop for 5 s.")
    pipeline, states = stages(client)
    assert pipeline["incident_id"] is None
    assert states["detect"] == "active" and pipeline["current"] == "detect"
    assert "CPU spike injected" in pipeline["stages"][1]["detail"]
    assert states["recover"] == "idle"

    # Its first anomalous sample counts towards opening an incident.
    mono = sample(wall, mono, 90.0, 130.0)
    pipeline, states = stages(client)
    assert states["detect"] == "active" and pipeline["current"] == "detect"
    assert f"1 of {main.OPEN_AFTER} anomalous samples" in pipeline["stages"][1]["detail"]

    # For a moment a full streak is visible before the incident is recorded.
    main.detector.streak["cpu_spike"] = main.OPEN_AFTER
    assert "opening an incident" in main.detect_stage(None)["detail"]


def test_pipeline_lets_the_guided_demo_recover(client, wall, no_reports, monkeypatch):
    rng = random.Random(6)
    mono = train(wall, rng)
    main.hold_leak(1)
    for ram in (195.0, 195.5):
        mono = sample(wall, mono, 2.0, ram)
    run = main.new_demo_run(1)
    for step in run["steps"][:4]:
        step["state"] = "done"
    monkeypatch.setattr(main, "_demo", run)
    pipeline, states = stages(client)
    assert states["recover"] == "active"
    assert "guided demo releases the 1 MB next" in pipeline["stages"][3]["detail"]

    # Once the demo has finished, a held leak asks the visitor to act again.
    run["state"] = "succeeded"
    pipeline, states = stages(client)
    assert states["recover"] == "waiting"
    assert "Release the 1 MB" in pipeline["stages"][3]["detail"]


# --- Incident stats ---


def test_incident_stats_are_empty_at_first(client):
    stats = client.get("/api/incidents/stats").json()
    assert stats == {
        "total": 0,
        "open": 0,
        "window": 0,
        "median_ttd_sec": None,
        "median_ttr_sec": None,
        "recent": [],
    }


def test_incident_stats_medians_and_order(client, wall, no_reports):
    # Three incidents with known times: detect 1, 3, 5 s; the last one stays open.
    for ttd, ttr in ((1, 10), (3, 20), (5, None)):
        main._first_flagged["cpu_spike"] = (main.iso(wall.now), "first")
        wall.advance(ttd)
        main.open_incident("cpu_spike", "")
        if ttr is not None:
            wall.advance(ttr)
            main.close_incident("cpu_spike")
        wall.advance(30)
    stats = client.get("/api/incidents/stats").json()
    assert stats["total"] == 3 and stats["open"] == 1 and stats["window"] == 3
    assert stats["median_ttd_sec"] == 3
    assert stats["median_ttr_sec"] == 15
    assert [i["ttd_sec"] for i in stats["recent"]] == [1, 3, 5]  # oldest first
    assert stats["recent"][-1]["status"] == "open"
    # The route is not swallowed by /api/incidents/{incident_id}.
    assert client.get("/api/incidents/stats").status_code == 200


# --- Report sources ---


def test_report_source_labels(client, monkeypatch):
    assert main.report_source("# Incident Report: X\n\nAnything Gemini wrote.") == "gemini"
    template = main.build_template_report([], "now", incident="Anomalous (CPU Spike)")
    assert main.report_source(template) == "template"
    fallback = main.build_template_report(
        [], "now", "The AI report failed (TimeoutError); this is the template fallback.", "X"
    )
    assert main.report_source(fallback) == "template-fallback"

    main.seed_demo_data()
    name = main.list_reports()[0]
    assert client.get(f"/api/reports/{name}").json()["source"] == "example"
    assert client.get("/api/reports").json()[0]["source"] == "example"
    assert client.get("/api/reports/latest").json()["source"] == "example"


def test_template_points_to_the_time_machine():
    report = main.build_template_report([], "now", incident="Degraded (Database Error)")
    assert "Time Machine" in report


LEAK_ROWS = [
    {"timestamp": f"2026-01-01T10:00:0{i}Z", "cpu": 2.0, "ram": ram, "status": status}
    for i, (ram, status) in enumerate(
        [(130.0, "Normal"), (195.0, "Anomalous (Memory Leak)"), (197.7, "Anomalous (Memory Leak)")]
    )
]


def test_database_report_does_not_describe_another_faults_samples():
    report = main.build_template_report(
        LEAK_ROWS,
        "now",
        incident="Degraded (Database Error)",
        detail="SQLite error: file is not a database.",
    )
    assert "latest classification is **Anomalous (Memory Leak)**" not in report
    assert "The database check failed (SQLite error: file is not a database)." in report
    assert "CPU and memory are not part of this fault." in report
    assert "2 of the last 3 samples were flagged, but they belong to another fault" in report
    assert "(memory leak)" in report


def test_cpu_report_counts_only_cpu_samples():
    rows = [*LEAK_ROWS, {**LEAK_ROWS[0], "cpu": 95.0, "status": "Anomalous (CPU Spike)"}]
    report = main.build_template_report(rows, "now", incident="Anomalous (CPU Spike)")
    assert "flagged 1 of the last 4 samples" in report
    assert "latest classification is **Anomalous (CPU Spike)**" in report


# --- Snapshots ---


def test_backups_say_whether_a_snapshot_is_readable(client):
    client.post("/transaction", params={"amount": 10})
    client.post("/transaction", params={"amount": 20})
    good = "app_20260101_100000.db.bak"
    shutil.copy2(main.DB_FILE, os.path.join(main.BACKUP_DIR, good))
    bad = "app_20260101_100100.db.bak"
    with open(os.path.join(main.BACKUP_DIR, bad), "wb") as f:
        f.write(b"not a database at all" * 50)

    backups = {b["filename"]: b for b in client.get("/api/backups").json()}
    assert backups[good]["readable"] is True and backups[good]["transactions"] == 2
    assert backups[bad]["readable"] is False and backups[bad]["transactions"] is None


def test_latest_snapshot_says_whether_it_holds_the_current_data(client, no_reports):
    client.post("/transaction", params={"amount": 10})
    assert client.get("/api/status").json()["latest_snapshot"] is None
    main.take_snapshot()
    snapshot = client.get("/api/status").json()["latest_snapshot"]
    assert snapshot["filename"] == main.list_snapshots()[0]
    assert snapshot["current"] is True

    # New data that no snapshot holds yet.
    client.post("/transaction", params={"amount": 20})
    assert client.get("/api/status").json()["latest_snapshot"]["current"] is False

    # A corrupt database never matches a snapshot.
    main.take_snapshot(main.utcnow() + timedelta(seconds=1))
    assert client.get("/api/status").json()["latest_snapshot"]["current"] is True
    client.post("/fault/corrupt_db")
    assert client.get("/api/status").json()["latest_snapshot"]["current"] is False


def test_metrics_pages_are_capped(client):
    rows = [(main.iso(T0 + timedelta(seconds=2 * i)), 1, 1.0, 130.0, "Normal") for i in range(450)]
    conn = main.get_metrics_db()
    conn.executemany(
        "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    assert len(client.get("/api/metrics", params={"limit": 310}).json()) == 310
    assert len(client.get("/api/metrics", params={"limit": 5000}).json()) == main.METRICS_PAGE_MAX


def test_corrupt_database_message_points_to_the_time_machine(client, no_reports):
    client.post("/transaction", params={"amount": 1})
    client.post("/fault/corrupt_db")
    detail = client.get("/transactions").json()["detail"]
    assert "Roll back to a snapshot in the Time Machine" in detail


# --- Status and OpenAPI ---


def test_status_reports_uptime(client, wall, monkeypatch):
    monkeypatch.setattr(main, "_started_at", T0)
    wall.advance(125)
    status = client.get("/api/status").json()
    assert status["started_at"] == "2026-01-01T10:00:00Z"
    assert status["uptime_sec"] == 125
    assert status["last_sample_at"] is None  # the page shows dashes until the first sample
    main.record_sample(1.0, 130.0, now=0.0)
    assert client.get("/api/status").json()["last_sample_at"] == "2026-01-01T10:02:05Z"


def test_startup_resets_the_uptime_clock(client):
    before = main._started_at
    main.startup_event()
    assert main._started_at >= before


def test_openapi_groups_every_route_under_a_tag(client):
    schema = client.get("/openapi.json").json()
    assert "detects its own CPU and memory anomalies" in schema["info"]["description"]
    declared = {t["name"] for t in schema["tags"]}
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            assert operation.get("tags"), path
            assert set(operation["tags"]) <= declared, path
    assert "/" not in schema["paths"]
    assert "/api/incidents/stats" in schema["paths"]
