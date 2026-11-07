"""DeepBell's entry point: builds the FastAPI app and exposes it as `main:app`.

Run it with `uvicorn main:app --app-dir target_app`. The application code lives in the
`deepbell` package next to this file; this module only wires it together.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from deepbell import config, startup
from deepbell.routes import ROUTERS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup.startup_event()
    yield


OPENAPI_TAGS = [
    {"name": "status", "description": "Live health, telemetry samples and the status history."},
    {"name": "incidents", "description": "Incident history with timelines and measured times."},
    {"name": "reports", "description": "Markdown incident reports (template or Gemini)."},
    {"name": "database", "description": "Sample transactions, snapshots and rollback."},
    {"name": "chaos lab", "description": "Capped fault injection with shared cooldowns."},
    {"name": "guided demo", "description": "The scripted detect, report, recover scenario."},
    {"name": "monitoring", "description": "Prometheus metrics for Grafana dashboards."},
    {"name": "settings", "description": "Local runs only: the Gemini API key, kept in memory."},
]

app = FastAPI(
    title="DeepBell",
    description=(
        "A mini SRE lab in FastAPI: it detects its own CPU and memory anomalies, writes "
        "incident reports, and restores from snapshots. All timestamps are UTC (ISO-8601)."
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# Dashboard UI Setup
app.mount("/static", StaticFiles(directory=os.path.join(config.BASE_DIR, "static")), name="static")
for router in ROUTERS:
    app.include_router(router)
