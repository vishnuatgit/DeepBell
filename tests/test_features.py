"""Incident timeline, report download, status strip, guided demo and leak auto-release."""

import os
import random
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

import main

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


class WallClock:
    """A controllable replacement for main.utcnow, so measured times are exact."""

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
    """Feed idle samples 2 s apart until the detector has a baseline."""
    for i in range(n):
        wall.advance(main.SAMPLE_INTERVAL_SEC)
        main.record_sample(*idle(rng), now=float(i * main.SAMPLE_INTERVAL_SEC))
    assert main.detector.baseline is not None
    return n * main.SAMPLE_INTERVAL_SEC


def sample(wall, mono, cpu, ram, step=main.SAMPLE_INTERVAL_SEC):
    wall.advance(step)
    main.record_sample(cpu, ram, now=mono + step)
    return mono + step


# --- Dashboard ---


def test_dashboard_explains_itself(client):
    html = client.get("/").text
    assert (
        "A mini SRE lab in FastAPI: it detects its own CPU and memory anomalies, writes "
        "incident reports, and restores from snapshots." in html
    )
    assert "Run the guided demo" in html
    assert "How it works" in html
    for _, title in main.DEMO_STEPS:
        assert title in html
    assert main.REPO_URL in html and main.AUTHOR_URL in html
    assert "Built by Vishnu Jayanth" in html
    assert f"/static/app.js?v={main.STATIC_VERSION}" in html
    assert client.get("/static/app.js").status_code == 200


# --- Incident timeline ---


def test_incident_timeline_measures_detect_and_resolve(client, wall, no_reports):
    rng = random.Random(1)
    mono = train(wall, rng)
    fault_at = wall.now
    main.note_fault("memory_leak", "Chaos Lab: leaked 60 MB.")
    mono = sample(wall, mono, 2.0, 195.0, step=1)  # first anomalous sample, 1 s later
    mono = sample(wall, mono, 2.0, 195.5)  # second in a row: the incident opens, 3 s in
    for _ in range(main.CLOSE_AFTER):
        mono = sample(wall, mono, *idle(rng))  # back to normal: closes 6 s after opening

    incidents = client.get("/api/incidents").json()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["kind"] == "memory_leak"
    assert incident["status"] == "resolved"
    assert incident["started_at"] == main.iso(fault_at)
    assert incident["ttd_sec"] == 3
    assert incident["ttr_sec"] == 2 * main.CLOSE_AFTER
    assert [e["type"] for e in incident["timeline"]] == [
        "fault",
        "flagged",
        "detected",
        "resolved",
    ]
    assert "alert above" in incident["timeline"][1]["text"]
    assert no_reports == ["memory_leak"]
    assert client.get(f"/api/incidents/{incident['id']}").json()["id"] == incident["id"]
    assert client.get("/api/incidents/999").status_code == 404


def test_without_a_fault_the_first_flagged_sample_is_the_start(client, wall, no_reports):
    rng = random.Random(2)
    mono = train(wall, rng)
    # A fault injected long ago does not explain this spike.
    main._pending_faults["cpu_spike"] = (main.iso(wall.now - timedelta(minutes=5)), "old")
    mono = sample(wall, mono, 95.0, 130.0)
    first = wall.now
    sample(wall, mono, 96.0, 130.0)
    incident = client.get("/api/incidents").json()[0]
    assert incident["status"] == "open"
    assert incident["started_at"] == main.iso(first)
    assert incident["ttd_sec"] == main.SAMPLE_INTERVAL_SEC
    assert incident["ttr_sec"] is None
    assert [e["type"] for e in incident["timeline"]] == ["flagged", "detected"]
    status = client.get("/api/status").json()
    assert status["incidents"][0]["id"] == incident["id"]
    assert status["incidents_rev"] > 0


def test_open_incidents_are_interrupted_by_a_restart(client, no_reports):
    main.open_incident("cpu_spike", "CPU 90%.")
    assert main.interrupt_open_incidents() == 1
    incident = client.get("/api/incidents").json()[0]
    assert incident["status"] == "interrupted"
    assert incident["resolved_at"] is None and incident["ttr_sec"] is None
    assert incident["timeline"][-1]["type"] == "interrupted"
    assert main.interrupt_open_incidents() == 0


def test_rollback_is_recorded_on_the_database_incident(client, no_reports):
    client.post("/transaction", params={"amount": 5})
    assert client.post("/fault/corrupt_db").status_code == 200
    incident = client.get("/api/incidents").json()[0]
    assert incident["kind"] == "db_corruption" and incident["status"] == "open"
    assert incident["timeline"][0]["type"] == "fault"

    snapshot = main.list_snapshots()[0]
    assert client.post("/api/rollback", data={"filename": snapshot}).status_code == 200
    incident = client.get("/api/incidents").json()[0]
    assert incident["status"] == "resolved"
    types = [e["type"] for e in incident["timeline"]]
    assert types == ["fault", "detected", "action", "resolved"]
    assert snapshot in incident["timeline"][2]["text"]


def test_release_is_recorded_on_the_leak_incident(client, no_reports):
    main.hold_leak(1)
    main.open_incident("memory_leak", "Memory 200 MB.")
    assert client.post("/fault/memory_release").json()["released_mb"] == 1
    timeline = client.get("/api/incidents").json()[0]["timeline"]
    assert timeline[-1]["type"] == "action"
    assert "released 1 MB" in timeline[-1]["text"]


def test_incident_history_is_capped(client, no_reports, monkeypatch):
    monkeypatch.setattr(main, "INCIDENTS_KEEP", 3)
    for _ in range(5):
        main.open_incident("cpu_spike", "")
        main.close_incident("cpu_spike")
    ids = [i["id"] for i in client.get("/api/incidents").json()]
    assert ids == [5, 4, 3]


# --- Reports: linking and download ---


def test_report_is_linked_and_downloads_with_timeline(client, no_reports):
    main.open_incident("cpu_spike", "CPU 90%.")
    record = dict(main.open_incidents["cpu_spike"])
    name = os.path.basename(main.generate_rca_report_task("cpu_spike", record))
    main.close_incident("cpu_spike")

    incident = client.get("/api/incidents").json()[0]
    assert incident["report"] == name and incident["report_available"] is True
    assert [e["type"] for e in incident["timeline"]] == ["detected", "report", "resolved"]

    report = client.get(f"/api/reports/{name}").json()
    assert report["incident_id"] == incident["id"]
    assert "## Timeline (UTC)" in report["content"]
    listed = client.get("/api/reports").json()
    assert listed[0]["filename"] == name
    assert listed[0]["incident_id"] == incident["id"]
    assert listed[0]["title"] == "Anomalous (CPU Spike)"

    res = client.get(f"/api/reports/{name}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/markdown; charset=utf-8"
    assert res.headers["content-disposition"] == f'attachment; filename="{name}"'
    body = res.text
    assert body.startswith("# Incident Report: Anomalous (CPU Spike)")
    assert "## Incident timeline (demo measurements)" in body
    assert "Time to detect (start to incident opened): 0 s" in body
    assert "Time to recover (incident opened to resolved):" in body
    assert "not a production system" in body


def test_report_without_incident_downloads_as_saved(client):
    main.write_report("# Incident Report: Example\n\nHello.\n")
    name = main.list_reports()[0]
    res = client.get(f"/api/reports/{name}/download")
    assert res.text == "# Incident Report: Example\n\nHello.\n"
    assert client.get(f"/api/reports/{name}").json()["incident_id"] is None


@pytest.mark.parametrize(
    "name,code",
    [("app.db", 400), ("RCA_Report_x.md", 400), ("RCA_Report_20260101_100000.md", 404)],
)
def test_report_endpoints_reject_bad_names(client, name, code):
    assert client.get(f"/api/reports/{name}").status_code == code
    assert client.get(f"/api/reports/{name}/download").status_code == code


# --- Status strip ---


def add_metric_at(when, status, cpu=1.0, ram=130.0):
    conn = main.get_metrics_db()
    conn.execute(
        "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (main.iso(when), 1, cpu, ram, status),
    )
    conn.commit()
    conn.close()


def test_status_history_bars(client):
    now = T0 + timedelta(minutes=10)
    minute = timedelta(minutes=1)
    add_metric_at(T0 + 1.5 * minute, main.WARMUP_STATUS)  # bar 1: synthetic only
    add_metric_at(T0 + 2.5 * minute, "Normal")  # bar 2: healthy
    add_metric_at(T0 + 3.5 * minute, "Normal")
    add_metric_at(T0 + 3.6 * minute, "Anomalous (CPU Spike)")  # bar 3: flagged, no incident
    add_metric_at(T0 + 5.5 * minute, "Anomalous (Memory Leak)")  # bar 5: in an incident
    add_metric_at(T0 + 6.5 * minute, "Normal")  # bar 6: in an incident too
    main.insert_incident(
        "memory_leak", main.iso(T0 + 5.2 * minute), main.iso(T0 + 5.4 * minute), "", []
    )
    main.update_incident(1, status="resolved", resolved_at=main.iso(T0 + 6.8 * minute))

    history = main.status_history(minutes=10, bars=10, now=now)
    states = [b["state"] for b in history["bars"]]
    assert states == [
        "nodata",
        "synthetic",
        "healthy",
        "degraded",
        "nodata",
        "incident",
        "incident",
        "nodata",
        "nodata",
        "nodata",
    ]
    assert history["bar_sec"] == 60
    assert history["bars"][5]["incidents"] == [1]
    # Live samples: 5 (the warm-up one is synthetic); healthy: the two normal ones outside
    # the incident.
    assert history["summary"] == {
        "live_samples": 5,
        "healthy_samples": 2,
        "healthy_pct": 40.0,
        "incidents": 1,
    }


def test_status_history_endpoint_clamps_its_arguments(client):
    res = client.get("/api/status/history", params={"minutes": 1, "bars": 500}).json()
    assert res["minutes"] == 5
    assert len(res["bars"]) == 120
    assert res["summary"]["healthy_pct"] is None


# --- Leaked memory auto-release ---


def test_stale_leak_is_released_automatically(client):
    main.hold_leak(1)
    assert main.release_stale_leak() == 0  # just allocated
    status = client.get("/api/status").json()["faults"]["memory_leak"]
    assert status["held_mb"] == 1
    assert 0 < status["auto_release_in"] <= main.LEAK_AUTO_RELEASE_SEC
    later = time.monotonic() + main.LEAK_AUTO_RELEASE_SEC + 1
    assert main.release_stale_leak(now=later) == 1
    assert main.leaked_mb() == 0
    assert any("Auto-release" in e["message"] for e in main.recent_events(0))
    status = client.get("/api/status").json()["faults"]["memory_leak"]
    assert status["auto_release_in"] is None


# --- Guided demo ---


class FakeWatchdog:
    """Samples like the real watchdog (memory = baseline + leaked MB), but every 20 ms."""

    def __init__(self):
        self.rng = random.Random(9)
        self.mono = 0.0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def tick(self):
        self.mono += main.SAMPLE_INTERVAL_SEC
        cpu, ram = idle(self.rng)
        main.record_sample(cpu, ram + main.leaked_mb(), now=self.mono)
        main.refresh_db_state()

    def run(self):
        while not self.stop.is_set():
            self.tick()
            time.sleep(0.02)


@pytest.fixture()
def fast_demo(client, monkeypatch):
    monkeypatch.setattr(main, "DEMO_PAUSE_SEC", 0)
    monkeypatch.setattr(main, "DEMO_POLL_SEC", 0.01)
    client.post("/transaction", params={"amount": 7})
    watchdog = FakeWatchdog()
    for _ in range(30):
        watchdog.tick()
    watchdog.thread.start()
    yield watchdog
    watchdog.stop.set()
    watchdog.thread.join(5)


def wait_for_demo(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = main.demo_state()
        if run and run["state"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("guided demo did not finish")


def test_guided_demo_runs_end_to_end(client, fast_demo):
    assert client.get("/api/demo").json() == {"run": None, "cooldown": 0}
    res = client.post("/api/demo/start")
    assert res.status_code == 200
    assert res.json()["state"] == "running"
    # One run at a time on the shared server.
    assert client.post("/api/demo/start").status_code == 409

    run = wait_for_demo()
    assert run["state"] == "succeeded", run
    assert [s["state"] for s in run["steps"]] == ["done"] * len(main.DEMO_STEPS)
    assert "detected" in run["summary"] and "restored from" in run["summary"]
    leak_id, db_id = run["incident_ids"]
    incidents = {i["id"]: i for i in client.get("/api/incidents").json()}
    assert incidents[leak_id]["kind"] == "memory_leak"
    assert incidents[leak_id]["status"] == "resolved"
    assert incidents[leak_id]["timeline"][0]["text"].startswith("Guided demo: leaked")
    assert incidents[db_id]["kind"] == "db_corruption"
    assert incidents[db_id]["status"] == "resolved"
    assert main.leaked_mb() == 0
    assert client.get("/api/status").json()["database"]["ok"] is True
    assert [r["amount"] for r in client.get("/transactions").json()] == [7]

    # The next run waits for the shared cooldown.
    res = client.post("/api/demo/start")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0
    assert client.get("/api/status").json()["demo"]["id"] == run["id"]


def test_guided_demo_explains_why_it_cannot_start(client, monkeypatch):
    # No baseline yet (nothing sampled), and only a moment to wait.
    monkeypatch.setattr(main, "DEMO_READY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(main, "DEMO_POLL_SEC", 0.01)
    monkeypatch.setattr(main, "_demo", main.new_demo_run(1))
    main.run_demo_scenario()
    run = main.demo_state()
    assert run["state"] == "failed"
    assert run["steps"][0]["state"] == "failed"
    assert "learning its baseline" in run["steps"][0]["detail"]
    assert main.cooldown_left("demo", main.DEMO_COOLDOWN_SEC) > 0


def test_guided_demo_waits_for_another_visitors_cooldown(client, monkeypatch):
    monkeypatch.setattr(main, "DEMO_POLL_SEC", 0.01)
    monkeypatch.setattr(main, "_demo", main.new_demo_run(1))
    # Another visitor leaked memory just under FAULT_COOLDOWN_SEC ago.
    main._last_fault_at["memory_leak"] = time.monotonic() - main.FAULT_COOLDOWN_SEC + 0.2
    started = time.monotonic()
    main.demo_claim("leak", "memory_leak")
    assert time.monotonic() - started >= 0.1
    step = main.demo_state()["steps"][1]
    assert "Waiting" in step["detail"] and "cooldown" in step["detail"]


def test_guided_demo_clears_a_leftover_leak(client, fast_demo, monkeypatch):
    monkeypatch.setattr(main, "DEMO_CLEANUP_AFTER_SEC", 0.2)
    main.hold_leak(5)  # another visitor's leak that nobody released
    assert client.post("/api/demo/start").status_code == 200
    run = wait_for_demo()
    assert run["state"] == "succeeded", run
    assert any("clearing a leftover leak" in e["message"] for e in main.recent_events(0))
