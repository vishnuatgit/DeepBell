"""Point the app at temporary directories and disable background threads before import."""

import glob
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="deepbell-tests-")
os.environ["DEEPBELL_DATA_DIR"] = os.path.join(_tmp, "data")
os.environ["DEEPBELL_BACKUP_DIR"] = os.path.join(_tmp, "backups")
os.environ["DEEPBELL_REPORTS_DIR"] = os.path.join(_tmp, "reports")
os.environ["DEEPBELL_ENV_FILE"] = os.path.join(_tmp, ".env")
os.environ["DEEPBELL_DISABLE_WORKERS"] = "1"
os.environ["DEEPBELL_SEED"] = "0"
os.environ["PUBLIC_DEMO"] = ""
os.environ.pop("DEEPBELL_CPU_CORES", None)
# An empty value stops python-dotenv from loading a real key from a local .env file.
os.environ["GOOGLE_API_KEY"] = ""


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    from deepbell import config, db, detection, runtime
    from deepbell.routes import chaos as chaos_routes

    monkeypatch.setenv("GOOGLE_API_KEY", "")
    for folder in (config.BACKUP_DIR, config.REPORTS_DIR):
        for f in glob.glob(os.path.join(folder, "*")):
            os.remove(f)
    for f in (config.DB_FILE, config.METRICS_DB):
        if os.path.exists(f):
            os.remove(f)
    db.init_metrics_db()
    chaos_routes.release_memory()
    monkeypatch.setattr(runtime, "detector", detection.Detector())
    monkeypatch.setattr(runtime, "api_key", "")
    monkeypatch.setattr(runtime, "db_error", None)
    monkeypatch.setattr(runtime, "demo", None)
    monkeypatch.setattr(runtime, "last_sample_at", None)
    runtime.open_incidents.clear()
    runtime.last_fault_at.clear()
    runtime.events.clear()
    runtime.pending_faults.clear()
    runtime.first_flagged.clear()
    yield
    chaos_routes.release_memory()
