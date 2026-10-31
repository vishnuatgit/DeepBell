"""Data for the overview: the uptime-style status strip, the live incident pipeline and
incident stats.
"""

import statistics
from datetime import datetime, timedelta

from . import chaos, config, db, incident_store, runtime, timeutil


def status_history(
    minutes: int = config.HISTORY_MINUTES,
    bars: int = config.HISTORY_BARS,
    now: datetime | None = None,
) -> dict:
    """Uptime-style bars for the last `minutes`, oldest first, from metrics and incidents.

    A bar is "incident" if an incident was open during it, else "degraded" if one of its
    samples was flagged, else "healthy" if it has live samples, "synthetic" if it only has
    seeded warm-up samples, and "nodata" otherwise.
    """
    now = now or timeutil.utcnow()
    start = now - timedelta(minutes=minutes)
    width = (now - start) / bars
    conn = db.get_metrics_db()
    try:
        samples = conn.execute(
            "SELECT timestamp, anomaly_status FROM metrics ORDER BY id DESC LIMIT ?",
            (minutes * 60 // config.SAMPLE_INTERVAL_SEC + 60,),
        ).fetchall()
        incident_rows = conn.execute(
            "SELECT id, status, detected_at, resolved_at FROM incidents "
            "WHERE resolved_at IS NULL OR resolved_at >= ?",
            (timeutil.iso(start),),
        ).fetchall()
    finally:
        conn.close()

    # Each incident is open from detection to resolution; an open one runs until now, and an
    # interrupted one (the server restarted) has no known end, so it only marks its start.
    spans = []
    for incident_id, status, detected, resolved in incident_rows:
        begin = timeutil.parse_utc(detected)
        if begin is None or begin > now:
            continue
        end = timeutil.parse_utc(resolved) if resolved else (now if status == "open" else begin)
        if end >= start:
            spans.append((incident_id, begin, end))

    buckets = [{"samples": 0, "flagged": 0, "synthetic": 0, "incidents": []} for _ in range(bars)]
    live = healthy = 0
    for timestamp, status in samples:
        t = timeutil.parse_utc(timestamp)
        if t is None or not start <= t <= now:
            continue
        bucket = buckets[min(int((t - start) / width), bars - 1)]
        if status == config.WARMUP_STATUS:
            bucket["synthetic"] += 1
            continue
        flagged = (status or "").startswith("Anomalous")
        in_incident = any(begin <= t <= end for _, begin, end in spans)
        bucket["samples"] += 1
        bucket["flagged"] += flagged
        live += 1
        healthy += not flagged and not in_incident

    result = []
    for i, bucket in enumerate(buckets):
        bar_start, bar_end = start + i * width, start + (i + 1) * width
        ids = [incident_id for incident_id, b, e in spans if b <= bar_end and e >= bar_start]
        if ids:
            state = "incident"
        elif bucket["flagged"]:
            state = "degraded"
        elif bucket["samples"]:
            state = "healthy"
        elif bucket["synthetic"]:
            state = "synthetic"
        else:
            state = "nodata"
        result.append(
            {
                "start": timeutil.iso(bar_start),
                "end": timeutil.iso(bar_end),
                "state": state,
                "samples": bucket["samples"],
                "flagged": bucket["flagged"],
                "incidents": ids,
            }
        )
    return {
        "minutes": minutes,
        "bar_sec": round(width.total_seconds()),
        "start": timeutil.iso(start),
        "end": timeutil.iso(now),
        "bars": result,
        "summary": {
            "live_samples": live,
            "healthy_samples": healthy,
            "healthy_pct": round(100 * healthy / live, 1) if live else None,
            "incidents": sum(1 for _, begin, _ in spans if begin >= start),
        },
    }


# --- LIVE PIPELINE AND INCIDENT STATS ---
#
# The overview draws the sample -> detect -> report -> recover loop as a pipeline that lights up
# while it runs. Its state comes from here, so the page shows what the server is really doing.

PIPELINE_RECENT_SEC = 60  # a resolved incident stays on the pipeline this long
STATS_WINDOW = 20  # incidents the overview's median times are computed over


def pending_fault() -> tuple[str, datetime] | None:
    """The newest injected fault that has not opened an incident yet (kind, injected at), if it
    was injected in the last FAULT_LINK_SEC."""
    with runtime.lock:
        faults = [
            (kind, timeutil.parse_utc(at)) for kind, (at, _) in runtime.pending_faults.items()
        ]
    now = timeutil.utcnow()
    recent = [
        (kind, at)
        for kind, at in faults
        if at and now - at <= timedelta(seconds=config.FAULT_LINK_SEC)
    ]
    return max(recent, key=lambda item: item[1]) if recent else None


def pipeline_focus() -> dict | None:
    """The incident the pipeline follows: the first open one, else one resolved in the last
    PIPELINE_RECENT_SEC, unless something new has started since (a streak of anomalous samples,
    or a fault injected after it resolved)."""
    with runtime.lock:
        open_ids = [
            runtime.open_incidents[k]["id"]
            for k in config.INCIDENT_LABELS
            if k in runtime.open_incidents
        ]
        streaking = any(runtime.detector.streak.values())
    if open_ids and open_ids[0] is not None:
        return incident_store.get_incident(open_ids[0])
    latest = incident_store.list_incidents(1)
    if streaking or not latest or latest[0]["status"] != "resolved":
        return None
    resolved = timeutil.parse_utc(latest[0]["resolved_at"])
    if not resolved or timeutil.utcnow() - resolved > timedelta(seconds=PIPELINE_RECENT_SEC):
        return None
    fault = pending_fault()
    if fault and fault[1] >= resolved:
        return None
    return latest[0]


def pipeline_stage(key: str, state: str, detail: str) -> dict:
    return {"key": key, "state": state, "detail": detail}


def demo_will(step_key: str) -> bool:
    """True while a guided-demo run is in progress and has not done this step yet, so the
    pipeline can say the demo is about to act instead of asking the visitor to."""
    with runtime.lock:
        if not runtime.demo or runtime.demo["state"] != "running":
            return False
        return any(s["key"] == step_key and s["state"] != "done" for s in runtime.demo["steps"])


def sample_stage() -> dict:
    with runtime.lock:
        sampled, base, cpu, ram = (
            runtime.last_sample_at,
            runtime.detector.baseline,
            runtime.last_cpu,
            runtime.last_ram,
        )
        learned = len(runtime.detector.samples)
    if sampled is None:
        return pipeline_stage("sample", "idle", "Waiting for the first sample.")
    age = (timeutil.utcnow() - sampled).total_seconds()
    if age > 3 * config.SAMPLE_INTERVAL_SEC:
        return pipeline_stage("sample", "idle", f"No sample for {age:.0f} s.")
    if base is None:
        return pipeline_stage(
            "sample",
            "active",
            f"Learning the baseline: {min(learned, config.FIRST_FIT_SAMPLES)} of {config.FIRST_FIT_SAMPLES} "
            "samples.",
        )
    return pipeline_stage("sample", "active", f"CPU {cpu:.1f} %, memory {ram:.0f} MB.")


def detect_stage(focus: dict | None) -> dict:
    if focus:
        return pipeline_stage(
            "detect",
            "done",
            f"Incident #{focus['id']} ({focus['name'].lower()}) opened {focus['ttd_sec']} s "
            "after it started.",
        )
    with runtime.lock:
        streaks = {k: n for k, n in runtime.detector.streak.items() if n}
    if streaks:
        kind, count = max(streaks.items(), key=lambda item: item[1])
        name = config.INCIDENT_NAMES[kind].lower()
        # A full streak is visible for a moment while the incident is being recorded.
        detail = (
            f"{count} anomalous samples in a row ({name}): opening an incident."
            if count >= config.OPEN_AFTER
            else f"{count} of {config.OPEN_AFTER} anomalous samples in a row ({name})."
        )
        return pipeline_stage("detect", "active", detail)
    fault = pending_fault()
    if fault:
        kind, at = fault
        seconds = max(0, round((timeutil.utcnow() - at).total_seconds()))
        return pipeline_stage(
            "detect",
            "active",
            f"{config.INCIDENT_NAMES[kind]} injected {seconds} s ago; waiting for {config.OPEN_AFTER} anomalous "
            "samples in a row.",
        )
    return pipeline_stage("detect", "idle", "Every sample is within the baseline.")


def report_stage(focus: dict | None) -> dict:
    with runtime.lock:
        writing = runtime.reports_in_flight > 0
    if focus and focus["report"]:
        return pipeline_stage("report", "done", f"Report linked to incident #{focus['id']}.")
    if focus or writing:
        return pipeline_stage("report", "active", "Writing the report from the last 15 samples.")
    return pipeline_stage("report", "idle", "Written when an incident opens.")


def recover_stage(focus: dict | None) -> dict:
    if focus is None:
        return pipeline_stage("recover", "idle", "Nothing to recover.")
    if focus["status"] == "resolved":
        return pipeline_stage(
            "recover", "done", f"Incident #{focus['id']} resolved after {focus['ttr_sec']} s."
        )
    if focus["kind"] == "db_corruption":
        if demo_will("rollback"):
            return pipeline_stage(
                "recover", "active", "The guided demo rolls back to the newest snapshot next."
            )
        return pipeline_stage(
            "recover", "waiting", "Restore a snapshot in the Time Machine to recover."
        )
    held = chaos.leaked_mb()
    if focus["kind"] == "memory_leak" and held:
        if demo_will("release"):
            return pipeline_stage(
                "recover", "active", f"The guided demo releases the {held} MB next."
            )
        return pipeline_stage(
            "recover", "waiting", f"Release the {held} MB of leaked memory to recover."
        )
    with runtime.lock:
        calm = runtime.detector.calm.get(focus["kind"], 0)
    return pipeline_stage(
        "recover",
        "active",
        f"{calm} of {config.CLOSE_AFTER} normal samples in a row before it closes.",
    )


def incident_pipeline() -> dict:
    """Where the sample -> detect -> report -> recover loop is now.

    Each stage is "idle", "active" (working), "waiting" (needs someone to act) or "done".
    `current` is the stage to highlight: the first one that is active or waiting.
    """
    focus = pipeline_focus()
    stages = [sample_stage(), detect_stage(focus), report_stage(focus), recover_stage(focus)]
    current = next(
        (s["key"] for s in stages[1:] if s["state"] in ("active", "waiting")),
        "sample" if stages[0]["state"] == "active" else None,
    )
    return {"incident_id": focus["id"] if focus else None, "current": current, "stages": stages}


def incident_stats(limit: int = STATS_WINDOW) -> dict:
    """Counts and median times to detect and recover over the newest `limit` incidents, plus
    each one's times (oldest first) for the overview's small bar charts."""
    recent = incident_store.list_incidents(limit)
    conn = db.get_metrics_db()
    try:
        total, open_count = conn.execute(
            "SELECT count(*), coalesce(sum(status = 'open'), 0) FROM incidents"
        ).fetchone()
    finally:
        conn.close()
    ttd = [i["ttd_sec"] for i in recent if i["ttd_sec"] is not None]
    ttr = [i["ttr_sec"] for i in recent if i["ttr_sec"] is not None]
    return {
        "total": total,
        "open": open_count,
        "window": len(recent),
        "median_ttd_sec": statistics.median(ttd) if ttd else None,
        "median_ttr_sec": statistics.median(ttr) if ttr else None,
        "recent": [
            {
                "id": i["id"],
                "kind": i["kind"],
                "name": i["name"],
                "status": i["status"],
                "detected_at": i["detected_at"],
                "ttd_sec": i["ttd_sec"],
                "ttr_sec": i["ttr_sec"],
            }
            for i in reversed(recent)
        ],
    }
