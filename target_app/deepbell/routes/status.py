"""Live status, the status history and raw telemetry."""

from fastapi import APIRouter

from .. import (
    chaos,
    config,
    db,
    demo,
    eventlog,
    llm,
    overview,
    reports,
    runtime,
    snapshots,
    telemetry,
    timeutil,
)

router = APIRouter()


@router.get("/api/status", tags=["status"])
def get_system_status(events_after: int | None = None):
    """Returns the live status of the cockpit (and, if asked, events newer than events_after)."""
    with runtime.lock:
        incidents = [
            dict(runtime.open_incidents[k])
            for k in config.INCIDENT_LABELS
            if k in runtime.open_incidents
        ]
        baseline = dict(runtime.detector.baseline) if runtime.detector.baseline else None
        if baseline:
            baseline["live_samples"] = runtime.detector.live_count()
            baseline["live_needed"] = config.LIVE_FIT_SAMPLES
        warming = runtime.detector.baseline is None
        db_error = runtime.db_error
        detection = {
            "open_after": config.OPEN_AFTER,
            "close_after": config.CLOSE_AFTER,
            "streak": dict(runtime.detector.streak),
            "calm": dict(runtime.detector.calm),
            "fits": runtime.detector.fit_count,
            "forest_fits": runtime.detector.forest_fits,
        }
        incidents_rev = runtime.incidents_rev
        sampled_at = runtime.last_sample_at
    if incidents:
        status = incidents[0]["label"]
        state = "degraded" if incidents[0]["kind"] == "db_corruption" else "anomalous"
    elif warming:
        status, state = "Healthy (Learning Baseline)", "warming"
    else:
        status, state = "Healthy", "healthy"
    result = {
        "status": status,
        "state": state,
        "incidents": incidents,
        "anomaly_score": runtime.latest_anomaly_score,
        "cpu": runtime.last_cpu,
        "ram": runtime.last_ram,
        "cpu_capacity": telemetry.CPU_CAPACITY,
        "baseline": baseline,
        "detection": detection,
        "database": {"ok": db_error is None, "error": db_error},
        "is_generating_rca": runtime.reports_in_flight > 0,
        "api_key_configured": bool(llm.get_api_key()),
        "public_demo": config.PUBLIC_DEMO,
        "faults": chaos.fault_status(),
        "latest_report": (reports.list_reports() or [None])[0],
        "latest_snapshot": snapshots.latest_snapshot(),
        "incidents_rev": incidents_rev,
        "demo": demo.demo_state(),
        "pipeline": overview.incident_pipeline(),
        "last_sample_at": timeutil.iso(sampled_at) if sampled_at else None,
        "started_at": timeutil.iso(runtime.started_at),
        "uptime_sec": max(0, round((timeutil.utcnow() - runtime.started_at).total_seconds())),
        "server_time": timeutil.iso(timeutil.utcnow()),
    }
    if events_after is not None:
        events = eventlog.recent_events(events_after)
        result["events"] = events
        result["event_seq"] = events[-1]["seq"] if events else events_after
    return result


@router.get("/api/status/history", tags=["status"])
def get_status_history(minutes: int = config.HISTORY_MINUTES, bars: int = config.HISTORY_BARS):
    """Uptime-style bars (healthy / degraded / incident) for the last `minutes`."""
    return overview.status_history(max(5, min(minutes, 120)), max(10, min(bars, 120)))


@router.get("/api/metrics", tags=["status"])
def get_metrics(limit: int = 15):
    """Fetch the latest telemetry entries, newest first (timestamps in UTC)."""
    limit = max(1, min(limit, config.METRICS_PAGE_MAX))
    conn = db.get_metrics_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"timestamp": timeutil.to_iso_utc(r[0]), "cpu": r[1], "ram": r[2], "status": r[3]}
        for r in rows
    ]
