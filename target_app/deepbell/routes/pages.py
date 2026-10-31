"""The dashboard page and the health check."""

import hashlib
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import config, llm, reports

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(config.BASE_DIR, "templates"))


def static_version() -> str:
    """Cache-buster for the CSS and JS URLs, so a redeploy never pairs new markup with stale
    assets."""
    digest = hashlib.sha256()
    for name in ("styles.css", "app.js"):
        with open(os.path.join(config.BASE_DIR, "static", name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:10]


STATIC_VERSION = static_version()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_dashboard(request: Request):
    """Serve the dashboard."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "report": reports.latest_report(),
            "api_key_configured": bool(llm.get_api_key()),
            "public_demo": config.PUBLIC_DEMO,
            "static_version": STATIC_VERSION,
            "repo_url": config.REPO_URL,
            "author_url": config.AUTHOR_URL,
            "demo_steps": config.DEMO_STEPS,
            "config": {
                "publicDemo": config.PUBLIC_DEMO,
                "maxSpikeSec": config.MAX_SPIKE_SEC,
                "leakStepMb": config.MAX_LEAK_STEP_MB,
                "leakLimitMb": config.MAX_LEAK_MB,
                "leakAutoReleaseSec": config.LEAK_AUTO_RELEASE_SEC,
                "cooldownSec": config.FAULT_COOLDOWN_SEC,
                "reportCooldownSec": config.REPORT_COOLDOWN_SEC,
                "cpuDeltaPct": config.CPU_MIN_DELTA,
                "ramDeltaMb": config.RAM_MIN_DELTA_MB,
                "openAfter": config.OPEN_AFTER,
                "closeAfter": config.CLOSE_AFTER,
                "sampleIntervalSec": config.SAMPLE_INTERVAL_SEC,
                "snapshotIntervalSec": config.SNAPSHOT_INTERVAL_SEC,
                "snapshotsKeep": config.SNAPSHOTS_KEEP,
                "reportsKeep": config.REPORTS_KEEP,
                "demoLeakMb": config.DEMO_LEAK_MB,
                "historyMinutes": config.HISTORY_MINUTES,
                "historyBars": config.HISTORY_BARS,
            },
        },
    )


@router.get("/healthz", tags=["status"])
def healthz():
    """Cheap liveness check for the host (Render's health check)."""
    return {"status": "ok"}
