import glob
import mmap
import os
import shutil
import sqlite3
import time

import pytest

from deepbell import (
    chaos,
    config,
    db,
    eventlog,
    incidents,
    llm,
    rca,
    reports,
    runtime,
    snapshots,
    startup,
    telemetry,
    timeutil,
)


def add_metric(cpu, ram, status="Normal"):
    conn = db.get_metrics_db()
    conn.execute(
        "INSERT INTO metrics (pid, cpu_percent, memory_mb, anomaly_status) VALUES (?, ?, ?, ?)",
        (1, cpu, ram, status),
    )
    conn.commit()
    conn.close()


ROWS = [
    {"timestamp": "2026-01-01 10:00:00", "cpu": 1.0, "ram": 90.0, "status": "Normal"},
    {"timestamp": "2026-01-01 10:00:02", "cpu": 2.0, "ram": 91.0, "status": "Normal"},
    {
        "timestamp": "2026-01-01 10:00:04",
        "cpu": 97.5,
        "ram": 92.0,
        "status": "Anomalous (CPU Spike)",
    },
]


def test_template_report_describes_cpu_spike():
    report = reports.build_template_report(ROWS, "2026-01-01 10:00:05")
    assert report.startswith("# Incident Report: Anomalous (CPU Spike)")
    assert "first flagged sample was at 2026-01-01 10:00:04" in report
    assert "max 97.5 %" in report
    assert "| 2026-01-01 10:00:04 | 97.5 | 92.0 | Anomalous (CPU Spike) |" in report
    assert "GOOGLE_API_KEY" in report
    # Deterministic: same input, same output.
    assert report == reports.build_template_report(ROWS, "2026-01-01 10:00:05")


def test_template_report_memory_leak_and_empty():
    rows = [dict(r) for r in ROWS]
    rows[-1]["status"] = "Anomalous (Memory Leak)"
    assert "never released" in reports.build_template_report(rows, "now")
    assert "No telemetry" in reports.build_template_report([], "now")


def test_template_report_for_database_incident():
    report = reports.build_template_report(
        ROWS[:2], "now", incident="Degraded (Database Error)", detail="file is not a database"
    )
    assert report.startswith("# Incident Report: Degraded (Database Error)")
    assert "file is not a database" in report
    assert "Roll back to the newest good snapshot" in report
    # Works before any telemetry exists, too.
    assert "## Likely cause" in reports.build_template_report([], "now", incident="Degraded (X)")
    # Samples flagged for another fault are named as such, never as this fault's own.
    report = reports.build_template_report(ROWS, "now", incident="Degraded (Database Error)")
    assert report.startswith("# Incident Report: Degraded (Database Error)")
    assert "latest classification" not in report
    assert "belong to another fault (cpu spike)" in report


def test_report_without_key_never_calls_llm(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("LLM must not be called without a key")

    monkeypatch.setattr(llm, "generate_ai_report", boom)
    add_metric(1.0, 90.0)
    path = rca.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "built-in template" in text
    assert "failed" not in text


def test_report_with_key_uses_llm(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(
        llm, "generate_ai_report", lambda key, rows, incident="": f"# AI report ({key})"
    )
    path = rca.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        assert f.read() == "# AI report (test-key)"


def test_report_falls_back_when_llm_fails(client, monkeypatch):
    def fail(*_args):
        raise RuntimeError("network down")

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "generate_ai_report", fail)
    add_metric(1.0, 90.0)
    path = rca.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "AI report failed (RuntimeError)" in text
    assert "## Recent telemetry" in text


def test_reports_are_pruned_to_newest(client):
    for _ in range(config.REPORTS_KEEP + 5):
        reports.write_report("# r\n")
    names = reports.list_reports()
    assert len(names) == config.REPORTS_KEEP
    assert names == sorted(names, reverse=True)
    assert client.get("/api/reports/latest").json()["filename"] == names[0]


def test_dashboard_shows_report_and_key_note(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "No incident reports yet" in res.text
    assert "AI reports need a Google Gemini API key" in res.text
    assert 'id="settings-api-key"' in res.text
    assert client.get("/api/reports/latest").status_code == 404
    add_metric(1.0, 90.0)
    rca.generate_rca_report_task()
    assert "built-in template" in client.get("/").text
    report = client.get("/api/reports/latest").json()
    assert report["generated_at"].endswith("Z")
    assert "built-in template" in report["content"]


def test_status_and_metrics(client):
    add_metric(3.5, 100.0)
    status = client.get("/api/status").json()
    assert status["api_key_configured"] is False
    assert status["public_demo"] is False
    assert status["state"] == "warming"
    assert status["database"] == {"ok": True, "error": None}
    metrics = client.get("/api/metrics").json()
    assert metrics[0]["cpu"] == 3.5
    assert metrics[0]["status"] == "Normal"
    # SQLite's CURRENT_TIMESTAMP is UTC; the API emits it as ISO-8601 with a Z.
    assert metrics[0]["timestamp"].endswith("Z") and "T" in metrics[0]["timestamp"]


def test_status_returns_new_events(client):
    seq = client.get("/api/status", params={"events_after": 0}).json()["event_seq"]
    eventlog.log_event("warn", "hello")
    res = client.get("/api/status", params={"events_after": seq}).json()
    assert [e["message"] for e in res["events"]] == ["hello"]
    assert res["event_seq"] == seq + 1
    assert client.get("/api/status", params={"events_after": seq + 1}).json()["events"] == []


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_rca_endpoint_generates_report_with_cooldown(client):
    assert client.post("/api/rca/generate").json()["status"] in ("started", "busy")
    deadline = time.time() + 10
    while time.time() < deadline and (
        not glob.glob(os.path.join(config.REPORTS_DIR, "*.md")) or runtime.reports_in_flight
    ):
        time.sleep(0.05)
    assert glob.glob(os.path.join(config.REPORTS_DIR, "*.md"))
    res = client.post("/api/rca/generate")
    assert res.status_code == 429
    assert "cooldown" in res.json()["detail"]


def count_transactions():
    conn = sqlite3.connect(config.DB_FILE)
    try:
        return conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


def test_transactions_are_validated_and_use_utc(client):
    for bad in (0, -5, 2_000_000):
        assert client.post("/transaction", params={"amount": bad}).status_code == 400
    assert client.post("/transaction", params={"amount": 12.345}).json()["amount"] == 12.35
    rows = client.get("/transactions").json()
    assert rows[0]["amount"] == 12.35
    assert rows[0]["timestamp"].endswith("Z")


def test_transactions_table_is_capped(client, monkeypatch):
    monkeypatch.setattr(config, "TRANSACTIONS_KEEP", 3)
    for amount in range(1, 6):
        client.post("/transaction", params={"amount": amount})
    assert count_transactions() == 3
    assert [r["amount"] for r in client.get("/transactions").json()] == [5, 4, 3]


def test_snapshot_rollback_restores_earlier_state(client):
    assert client.post("/transaction", params={"amount": 10}).status_code == 200
    snapshot = "app_20260101_100000.db.bak"
    shutil.copy2(config.DB_FILE, os.path.join(config.BACKUP_DIR, snapshot))
    client.post("/transaction", params={"amount": 20})
    assert len(client.get("/transactions").json()) == 2

    backups = client.get("/api/backups").json()
    assert [b["filename"] for b in backups] == [snapshot]
    assert backups[0]["timestamp"] == "2026-01-01T10:00:00Z"

    res = client.post("/api/rollback", data={"filename": snapshot})
    assert res.status_code == 200
    assert count_transactions() == 1


@pytest.mark.parametrize(
    "name,code",
    [("../data/metrics.db", 400), ("app.db", 400), ("app_20260101_100000.db.bak", 404)],
)
def test_rollback_rejects_bad_names(client, name, code):
    assert client.post("/api/rollback", data={"filename": name}).status_code == code


def test_corruption_is_detected_and_healed_from_snapshot(client):
    client.post("/transaction", params={"amount": 42})
    shutil.copy2(config.DB_FILE, os.path.join(config.BACKUP_DIR, "app_20260101_100000.db.bak"))

    assert client.post("/fault/corrupt_db").json()["status"] == "fault_injected"
    with pytest.raises(sqlite3.DatabaseError):
        count_transactions()

    snapshots.verify_and_heal_database()
    assert count_transactions() == 1


def test_startup_moves_aside_a_corrupt_db_without_snapshots(client):
    client.post("/transaction", params={"amount": 1})
    with open(config.DB_FILE, "r+b") as f:
        f.write(b"x" * 100)
    snapshots.verify_and_heal_database()
    assert not os.path.exists(config.DB_FILE)
    assert glob.glob(config.DB_FILE + ".corrupt-*")
    for f in glob.glob(config.DB_FILE + ".corrupt-*"):
        os.remove(f)


def test_corruption_degrades_health_until_rollback(client, monkeypatch):
    reports = []
    monkeypatch.setattr(rca, "trigger_auto_rca", reports.append)
    client.post("/transaction", params={"amount": 42})

    # With no snapshot yet, the fault takes one first so the damage is recoverable.
    assert client.post("/fault/corrupt_db").status_code == 200
    snapshot_names = snapshots.list_snapshots()
    assert len(snapshot_names) == 1

    status = client.get("/api/status").json()
    assert status["state"] == "degraded"
    assert status["status"] == "Degraded (Database Error)"
    assert status["database"]["ok"] is False
    assert reports == ["db_corruption"]

    res = client.get("/transactions")
    assert res.status_code == 503
    assert "Roll back to a snapshot" in res.json()["detail"]
    assert client.post("/transaction", params={"amount": 1}).status_code == 503
    # Still one incident and one report, however often the error is seen.
    incidents.refresh_db_state()
    assert reports == ["db_corruption"]

    assert client.post("/api/rollback", data={"filename": snapshot_names[0]}).status_code == 200
    status = client.get("/api/status").json()
    assert status["database"]["ok"] is True
    assert status["state"] != "degraded"
    assert [r["amount"] for r in client.get("/transactions").json()] == [42]


def test_api_key_is_kept_in_memory_only(client, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", str(env_file))
    monkeypatch.setattr(llm, "load_llm", lambda relearn: None)
    assert client.post("/api/settings/apikey", data={"api_key": "  "}).status_code == 400
    res = client.post("/api/settings/apikey", data={"api_key": "new"})
    assert res.status_code == 200
    assert "not saved to disk" in res.json()["message"]
    assert llm.get_api_key() == "new"
    assert env_file.read_text(encoding="utf-8") == "OTHER=1\n"
    assert os.environ["GOOGLE_API_KEY"] == ""


def test_public_demo_locks_settings_and_crash(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_DEMO", True)
    monkeypatch.setattr(runtime, "api_key", "should-be-ignored")
    assert client.post("/api/settings/apikey").status_code == 403
    assert client.post("/api/settings/apikey", data={"api_key": "x"}).status_code == 403
    assert llm.get_api_key() == ""
    res = client.post("/fault/fatal_crash")
    assert res.status_code == 403
    assert "public demo" in res.json()["detail"]

    html = client.get("/").text
    assert 'id="settings-api-key"' not in html
    assert 'href="#settings"' not in html
    assert "How it works" in html
    assert "shared instance" in html
    status = client.get("/api/status").json()
    assert status["public_demo"] is True
    assert status["faults"]["fatal_crash"]["enabled"] is False


def test_fatal_crash_exits_locally(client, monkeypatch):
    exits = []
    monkeypatch.setattr(chaos, "hard_exit", lambda: exits.append(1))
    assert client.post("/fault/fatal_crash").json()["type"] == "fatal_crash"
    deadline = time.time() + 5
    while time.time() < deadline and not exits:
        time.sleep(0.05)
    assert exits == [1]


def test_faults_are_post_only(client):
    for path in ("/fault/cpu_spike", "/fault/memory_leak", "/fault/corrupt_db"):
        assert client.get(path).status_code == 405


def test_cpu_spike_is_capped_and_cooled_down(client):
    res = client.post("/fault/cpu_spike", params={"duration_sec": config.MAX_SPIKE_SEC + 1})
    assert res.status_code == 400
    assert client.post("/fault/cpu_spike", params={"duration_sec": 0}).status_code == 400
    assert client.post("/fault/cpu_spike", params={"duration_sec": 1}).status_code == 200
    res = client.post("/fault/cpu_spike", params={"duration_sec": 1})
    assert res.status_code == 429
    assert 0 < int(res.headers["Retry-After"]) <= config.FAULT_COOLDOWN_SEC
    assert "try again in" in res.json()["detail"]
    assert client.get("/api/status").json()["faults"]["cpu_spike"]["cooldown"] > 0


def test_memory_leak_is_capped_and_released(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_LEAK_MB", 3)
    monkeypatch.setattr(config, "MAX_LEAK_STEP_MB", 2)
    assert client.post("/fault/memory_leak", params={"size_mb": 3}).status_code == 400
    res = client.post("/fault/memory_leak", params={"size_mb": 2}).json()
    assert res["allocated_mb"] == 2 and res["held_mb"] == 2
    assert client.post("/fault/memory_leak", params={"size_mb": 1}).status_code == 429
    runtime.last_fault_at.clear()
    res = client.post("/fault/memory_leak", params={"size_mb": 2})
    assert res.status_code == 409
    assert "Release it first" in res.json()["detail"]
    assert client.get("/api/status").json()["faults"]["memory_leak"]["held_mb"] == 2

    assert client.post("/fault/memory_release").json()["released_mb"] == 2
    assert chaos.leaked_mb() == 0
    assert client.post("/fault/memory_leak", params={"size_mb": 1}).status_code == 200


def test_leak_blocks_are_resident_memory():
    block = chaos.allocate_leak(1)
    try:
        assert len(block) == config.MB
        assert block[0] == 1 and block[config.MB - mmap.PAGESIZE] == 1
    finally:
        block.close()


def test_seed_demo_data_is_idempotent(client):
    startup.seed_demo_data()
    startup.seed_demo_data()
    rows = client.get("/transactions").json()
    assert len(rows) == len(startup.SAMPLE_AMOUNTS)
    assert all(r["timestamp"].endswith("Z") for r in rows)
    assert len(reports.list_reports()) == 1
    report = client.get("/api/reports/latest").json()["content"]
    assert "Example report" in report
    assert "synthetic telemetry" in report


@pytest.mark.parametrize(
    "files,expected",
    [
        ({"cpu.max": "10000 100000\n"}, 0.1),
        ({"cpu.max": "max 100000\n"}, None),
        ({"cpu/cpu.cfs_quota_us": "50000", "cpu/cpu.cfs_period_us": "100000"}, 0.5),
        ({"cpu/cpu.cfs_quota_us": "-1", "cpu/cpu.cfs_period_us": "100000"}, None),
        ({}, None),
    ],
)
def test_read_cpu_quota(tmp_path, files, expected):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert telemetry.read_cpu_quota(str(tmp_path)) == expected


def test_cpu_capacity_override(monkeypatch):
    monkeypatch.setenv("DEEPBELL_CPU_CORES", "0.25")
    assert telemetry.cpu_capacity() == 0.25
    monkeypatch.setenv("DEEPBELL_CPU_CORES", "4")
    assert telemetry.cpu_capacity() == 1.0
    monkeypatch.setenv("DEEPBELL_CPU_CORES", "oops")
    assert telemetry.cpu_capacity() == 1.0


def test_timestamps_are_normalised_to_utc():
    assert timeutil.to_iso_utc("2026-01-01 10:00:00") == "2026-01-01T10:00:00Z"
    assert timeutil.to_iso_utc("2026-01-01T15:30:00+05:30") == "2026-01-01T10:00:00Z"
    assert timeutil.to_iso_utc("not a time") == "not a time"
    assert timeutil.to_iso_utc(None) is None
