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
    import main

    monkeypatch.setenv("GOOGLE_API_KEY", "")
    for folder in (main.BACKUP_DIR, main.REPORTS_DIR):
        for f in glob.glob(os.path.join(folder, "*")):
            os.remove(f)
    for f in (main.DB_FILE, main.METRICS_DB):
        if os.path.exists(f):
            os.remove(f)
    main.init_metrics_db()
    main.release_memory()
    monkeypatch.setattr(main, "detector", main.Detector())
    monkeypatch.setattr(main, "_runtime_api_key", "")
    monkeypatch.setattr(main, "_db_error", None)
    main.open_incidents.clear()
    main._last_fault_at.clear()
    main._events.clear()
    yield
    main.release_memory()
