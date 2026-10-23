import glob
import os
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    for folder in (main.BACKUP_DIR, main.REPORTS_DIR):
        for f in glob.glob(os.path.join(folder, "*")):
            os.remove(f)
    if os.path.exists(main.DB_FILE):
        os.remove(main.DB_FILE)
    yield


def add_metric(cpu, ram, status="Normal"):
    conn = main.get_metrics_db()
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
    report = main.build_template_report(ROWS, "2026-01-01 10:00:05")
    assert report.startswith("# Incident Report: Anomalous (CPU Spike)")
    assert "first flagged sample was at 2026-01-01 10:00:04" in report
    assert "max 97.5 %" in report
    assert "| 2026-01-01 10:00:04 | 97.5 | 92.0 | Anomalous (CPU Spike) |" in report
    assert "GOOGLE_API_KEY" in report
    # Deterministic: same input, same output.
    assert report == main.build_template_report(ROWS, "2026-01-01 10:00:05")


def test_template_report_memory_leak_and_empty():
    rows = [dict(r) for r in ROWS]
    rows[-1]["status"] = "Anomalous (Memory Leak)"
    assert "never released" in main.build_template_report(rows, "now")
    assert "No telemetry" in main.build_template_report([], "now")


def test_report_without_key_never_calls_llm(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("LLM must not be called without a key")

    monkeypatch.setattr(main, "generate_ai_report", boom)
    add_metric(1.0, 90.0)
    path = main.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "built-in template" in text
    assert "failed" not in text


def test_report_with_key_uses_llm(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(main, "generate_ai_report", lambda key, rows: f"# AI report ({key})")
    path = main.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        assert f.read() == "# AI report (test-key)"


def test_report_falls_back_when_llm_fails(client, monkeypatch):
    def fail(*_args):
        raise RuntimeError("network down")

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(main, "generate_ai_report", fail)
    add_metric(1.0, 90.0)
    path = main.generate_rca_report_task()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "AI report failed (RuntimeError)" in text
    assert "## Recent telemetry" in text


def test_dashboard_shows_report_and_key_note(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "No RCA reports generated yet" in res.text
    assert "AI reports need a Google Gemini API key" in res.text
    main.generate_rca_report_task()
    assert "built-in template" in client.get("/").text


def test_status_and_metrics(client):
    add_metric(3.5, 100.0)
    status = client.get("/api/status").json()
    assert status["api_key_configured"] is False
    metrics = client.get("/api/metrics").json()
    assert metrics[0]["cpu"] == 3.5
    assert metrics[0]["status"] == "Normal"


def test_rca_endpoint_generates_report(client):
    assert client.post("/api/rca/generate").json()["status"] in ("started", "busy")
    deadline = time.time() + 10
    while time.time() < deadline and not glob.glob(os.path.join(main.REPORTS_DIR, "*.md")):
        time.sleep(0.05)
    assert glob.glob(os.path.join(main.REPORTS_DIR, "*.md"))


def count_transactions():
    conn = sqlite3.connect(main.DB_FILE)
    try:
        return conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


def test_snapshot_rollback_restores_earlier_state(client):
    assert client.post("/transaction", params={"amount": 10}).status_code == 200
    snapshot = "app_20260101_100000.db.bak"
    shutil.copy2(main.DB_FILE, os.path.join(main.BACKUP_DIR, snapshot))
    client.post("/transaction", params={"amount": 20})
    assert len(client.get("/transactions").json()) == 2

    backups = client.get("/api/backups").json()
    assert [b["filename"] for b in backups] == [snapshot]

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
    shutil.copy2(main.DB_FILE, os.path.join(main.BACKUP_DIR, "app_20260101_100000.db.bak"))

    assert client.get("/fault/corrupt_db").json()["status"] == "fault_injected"
    with pytest.raises(sqlite3.DatabaseError):
        count_transactions()

    main.verify_and_heal_database()
    assert count_transactions() == 1


def test_save_api_key_keeps_other_settings(client, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\nGOOGLE_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setattr(main, "ENV_FILE", str(env_file))
    assert client.post("/api/settings/apikey", data={"api_key": "  "}).status_code == 400
    assert client.post("/api/settings/apikey", data={"api_key": "new"}).status_code == 200
    assert env_file.read_text(encoding="utf-8") == "OTHER=1\nGOOGLE_API_KEY=new\n"
    assert os.environ["GOOGLE_API_KEY"] == "new"


def test_memory_leak_fault(client):
    before = len(main._memory_leak_array)
    res = client.get("/fault/memory_leak", params={"size_mb": 1}).json()
    assert res["allocated_mb"] == 1
    assert len(main._memory_leak_array) == before + 1
    main._memory_leak_array.clear()
