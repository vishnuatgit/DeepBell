"""Watchdog (anomaly detection and incidents) and snapshot logic, driven without threads."""

import os
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import main

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def idle(rng):
    """One idle sample: a few % CPU and ~130 MB with normal jitter."""
    return rng.uniform(0.5, 6.0), 130.0 + rng.uniform(-1.5, 1.5)


class Clock:
    def __init__(self):
        self.t = 0.0

    def tick(self):
        self.t += main.SAMPLE_INTERVAL_SEC
        return self.t


def feed(detector, samples, clock):
    """Observe samples; return every incident opened and closed along the way."""
    opened, closed = [], []
    for cpu, ram in samples:
        _, o, c = detector.observe(cpu, ram, clock.tick())
        opened += o
        closed += c
    return opened, closed


def trained_detector(clock, rng, n=40):
    det = main.Detector()
    feed(det, [idle(rng) for _ in range(n)], clock)
    assert det.baseline is not None
    return det


def test_jitter_never_opens_an_incident():
    rng, clock = random.Random(1), Clock()
    det = main.Detector()
    samples = [idle(rng) for _ in range(400)]
    # Occasional single bumps (a page load, a report) are jitter too.
    for i in range(20, 400, 37):
        samples[i] = (25.0, samples[i][1])
    opened, _ = feed(det, samples, clock)
    assert opened == []
    # 400 samples = 800 s: the first fit plus a refit every REFIT_INTERVAL_SEC, not one per
    # sample, and the forest only runs for the bumps that clear the minimum deviation.
    assert det.fit_count <= 1 + 800 // main.REFIT_INTERVAL_SEC
    assert det.forest_fits <= len(range(20, 400, 37))


def test_cpu_spike_opens_one_incident_and_closes():
    rng, clock = random.Random(2), Clock()
    det = trained_detector(clock, rng)
    opened, closed = feed(det, [(95.0, 130.0)] * 3, clock)
    assert opened == ["cpu_spike"]
    assert closed == []
    opened, closed = feed(det, [idle(rng) for _ in range(main.CLOSE_AFTER)], clock)
    assert opened == [] and closed == ["cpu_spike"]


def test_single_spike_sample_is_not_an_incident():
    rng, clock = random.Random(3), Clock()
    det = trained_detector(clock, rng)
    kinds, opened, _ = det.observe(95.0, 130.0, clock.tick())
    assert kinds == ["cpu_spike"] and opened == []
    opened, _ = feed(det, [idle(rng) for _ in range(5)], clock)
    assert opened == []


def test_held_leak_stays_open_until_released():
    rng, clock = random.Random(4), Clock()
    det = trained_detector(clock, rng)
    leaked = [(rng.uniform(0.5, 6.0), 190.0 + rng.uniform(-1, 1)) for _ in range(200)]
    opened, closed = feed(det, leaked, clock)
    # 200 samples = 400 s, past a refit: still exactly one open incident.
    assert opened == ["memory_leak"] and closed == []
    assert det.open == {"memory_leak"}
    assert det.baseline["ram"] < 135
    # The forest confirmed the first samples; the open incident needs no fit every 2 s.
    assert det.forest_fits == main.OPEN_AFTER
    opened, closed = feed(det, [idle(rng) for _ in range(main.CLOSE_AFTER)], clock)
    assert closed == ["memory_leak"]


def test_small_memory_growth_is_not_a_leak():
    rng, clock = random.Random(5), Clock()
    det = trained_detector(clock, rng)
    opened, _ = feed(det, [(2.0, 130.0 + main.RAM_MIN_DELTA_MB - 5)] * 20, clock)
    assert opened == []


def test_warmup_baseline_is_replaced_by_live_samples():
    rng, clock = random.Random(6), Clock()
    det = main.Detector()
    det.add_warmup([(1.5, 130.0)] * main.WARMUP_POINTS)
    det.observe(*idle(rng), clock.tick())
    assert det.baseline["source"] == "warm-up"
    feed(det, [idle(rng) for _ in range(main.LIVE_FIT_SAMPLES)], clock)
    assert det.baseline["source"] == "live"
    assert all(not synthetic for _, _, synthetic in det.samples)


def test_reset_relearns_baseline():
    rng, clock = random.Random(7), Clock()
    det = trained_detector(clock, rng)
    det.reset()
    assert det.baseline is None
    # After e.g. loading the Gemini client, the new memory level is the baseline.
    opened, _ = feed(det, [(2.0, 165.0 + rng.uniform(-1, 1)) for _ in range(40)], clock)
    assert opened == []
    assert det.baseline["ram"] > 160


def test_record_sample_raises_one_report_per_incident(client, monkeypatch):
    reports = []
    monkeypatch.setattr(main, "trigger_auto_rca", reports.append)
    rng, clock = random.Random(8), Clock()
    for _ in range(30):
        main.record_sample(*idle(rng), now=clock.tick())
    assert client.get("/api/status").json()["state"] == "healthy"

    for _ in range(4):
        main.record_sample(90.0, 131.0, now=clock.tick())
    status = client.get("/api/status").json()
    assert status["status"] == "Anomalous (CPU Spike)"
    assert status["state"] == "anomalous"
    assert status["incidents"][0]["kind"] == "cpu_spike"
    assert reports == ["cpu_spike"]

    for _ in range(main.CLOSE_AFTER):
        main.record_sample(*idle(rng), now=clock.tick())
    assert client.get("/api/status").json()["state"] == "healthy"
    # A second spike is a new incident with its own report.
    for _ in range(2):
        main.record_sample(90.0, 131.0, now=clock.tick())
    assert reports == ["cpu_spike", "cpu_spike"]

    metrics = client.get("/api/metrics", params={"limit": 3}).json()
    assert metrics[0]["status"] == "Anomalous (CPU Spike)"
    assert metrics[0]["timestamp"].endswith("Z")
    messages = [e["message"] for e in main.recent_events(0)]
    assert any(m.startswith("Incident opened: Anomalous (CPU Spike)") for m in messages)
    assert any(m.startswith("Incident resolved") for m in messages)


def test_seeded_warmup_lets_the_watchdog_start_at_once(client, monkeypatch):
    monkeypatch.setattr(main, "trigger_auto_rca", lambda kind: None)
    assert main.seed_warmup_metrics(2.0, 130.0) == main.WARMUP_POINTS
    # Recent samples exist now, so a restart does not seed again.
    assert main.seed_warmup_metrics(2.0, 130.0) == 0
    metrics = client.get("/api/metrics", params={"limit": 100}).json()
    assert len(metrics) == main.WARMUP_POINTS
    assert {m["status"] for m in metrics} == {main.WARMUP_STATUS}
    main.record_sample(2.0, 130.0, now=2.0)
    status = client.get("/api/status").json()
    assert status["state"] == "healthy"
    assert status["baseline"]["source"] == "warm-up"


# --- Snapshots ---


def add_tx(amount):
    with main.app_db() as conn:
        conn.execute("INSERT INTO transactions (id, amount) VALUES (?, ?)", (str(amount), amount))
        conn.commit()


def test_snapshot_uses_its_own_time_and_skips_unchanged(client):
    add_tx(1)
    first = main.take_snapshot(T0)
    assert first == "app_20260101_100000.db.bak"
    # Unchanged database: no new snapshot, however much later.
    assert main.take_snapshot(T0 + timedelta(minutes=5)) is None
    add_tx(2)
    second = main.take_snapshot(T0 + timedelta(minutes=6))
    assert second == "app_20260101_100600.db.bak"

    backups = client.get("/api/backups").json()
    assert [b["filename"] for b in backups] == [second, first]
    assert [b["timestamp"] for b in backups] == ["2026-01-01T10:06:00Z", "2026-01-01T10:00:00Z"]


def test_snapshots_keep_newest(client):
    for i in range(main.SNAPSHOTS_KEEP + 3):
        add_tx(i)
        assert main.take_snapshot(T0 + timedelta(seconds=30 * i))
    names = main.list_snapshots()
    assert len(names) == main.SNAPSHOTS_KEEP
    assert (
        names[0]
        == f"app_{T0 + timedelta(seconds=30 * (main.SNAPSHOTS_KEEP + 2)):%Y%m%d_%H%M%S}.db.bak"
    )
    assert not [f for f in os.listdir(main.BACKUP_DIR) if f.endswith(".tmp")]


def test_snapshot_skips_corrupt_database(client):
    add_tx(1)
    with open(main.DB_FILE, "r+b") as f:
        f.write(b"GARBAGE" * 10)
    assert main.take_snapshot(T0) is None
    assert main.list_snapshots() == []
    assert not os.listdir(main.BACKUP_DIR)


def test_restore_skips_unreadable_snapshot(client):
    add_tx(7)
    good = main.take_snapshot(T0)
    bad = "app_20260101_110000.db.bak"
    with open(os.path.join(main.BACKUP_DIR, bad), "wb") as f:
        f.write(b"not sqlite")
    with open(main.DB_FILE, "r+b") as f:
        f.write(b"GARBAGE" * 10)
    assert main.list_snapshots()[0] == bad
    assert main.restore_latest_backup()
    conn = sqlite3.connect(main.DB_FILE)
    try:
        assert conn.execute("SELECT amount FROM transactions").fetchall() == [(7.0,)]
    finally:
        conn.close()
    assert good in main.list_snapshots()


@pytest.mark.parametrize("name", ["app_2026_1.db.bak", "other.db.bak"])
def test_foreign_files_are_not_snapshots(name):
    open(os.path.join(main.BACKUP_DIR, name), "wb").close()
    assert main.list_snapshots() == []
