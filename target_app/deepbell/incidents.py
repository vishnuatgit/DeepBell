"""Incident lifecycle: open (one per kind), time from the fault or first anomalous sample,
record faults and recovery actions, and close. Also tracks the database health check.
"""

import sqlite3
from datetime import datetime, timedelta

from . import config, db, eventlog, incident_store, metrics, rca, runtime, timeutil
from .alert_rules import INCIDENT_KINDS


def incident_start(kind: str, now: datetime) -> tuple[str, list[dict]]:
    """When an incident that is being opened now really began, plus its first timeline events.

    The start is the Chaos Lab fault that caused it (if one was injected in the last
    FAULT_LINK_SEC), else the first anomalous sample of the streak, else now.
    Called with _lock held.
    """
    events = []
    started = timeutil.iso(now)
    flagged = runtime.first_flagged.get(kind)
    if flagged:
        started = flagged[0]
        events.append({"at": flagged[0], "type": "flagged", "text": flagged[1]})
    fault = runtime.pending_faults.pop(kind, None)
    fault_time = timeutil.parse_utc(fault[0]) if fault else None
    if fault_time and timedelta(0) <= now - fault_time <= timedelta(seconds=config.FAULT_LINK_SEC):
        started = min(started, fault[0])  # same ISO format, so strings sort by time
        events.append({"at": fault[0], "type": "fault", "text": fault[1]})
    return started, events


def open_incident(kind: str, detail: str = "") -> bool:
    """Open an incident and write its report, unless one of the same kind is already open."""
    now = timeutil.utcnow()
    with runtime.lock:
        if kind in runtime.open_incidents:
            return False
        started, events = incident_start(kind, now)
        detected = timeutil.iso(now)
        # Alerts that already changed state while the anomaly built up (for example "pending").
        events += [
            {"at": e["at"], "type": "alert", "text": f"Alert: {e['text']}"}
            for e in runtime.alert_engine.events_since(started, kind)
        ]
        events.append({"at": detected, "type": "detected", "text": f"Incident opened. {detail}"})
        try:
            incident_id = incident_store.insert_incident(kind, started, detected, detail, events)
        except sqlite3.Error as e:  # history is a nice-to-have; never let it stop detection
            config.logger.error(f"Could not record the incident: {e}")
            incident_id = None
        runtime.open_incidents[kind] = {
            "id": incident_id,
            "kind": kind,
            "name": config.INCIDENT_NAMES[kind],
            "label": config.INCIDENT_LABELS[kind],
            "started_at": started,
            "opened_at": detected,
            "detail": detail,
        }
    metrics.INCIDENTS.labels(kind).inc()
    metrics.DETECTION_SECONDS.observe(timeutil.seconds_between(started, detected) or 0)
    eventlog.log_event(
        "error", f"Incident opened: {config.INCIDENT_LABELS[kind]}. {detail}".strip()
    )
    rca.trigger_auto_rca(kind)
    return True


def close_incident(kind: str, resolution: str = "") -> None:
    with runtime.lock:
        incident = runtime.open_incidents.pop(kind, None)
    if not incident:
        return
    resolved = timeutil.utcnow()
    text = resolution or config.INCIDENT_RESOLUTIONS[kind].format(n=config.CLOSE_AFTER)
    try:
        incident_store.update_incident(
            incident["id"],
            ("resolved", text),
            status="resolved",
            resolved_at=timeutil.iso(resolved),
        )
    except sqlite3.Error as e:
        config.logger.error(f"Could not record the resolution: {e}")
    opened = timeutil.parse_utc(incident["opened_at"])
    seconds = int((resolved - opened).total_seconds()) if opened else 0
    metrics.RECOVERY_SECONDS.observe(seconds)
    eventlog.log_event("success", f"Incident resolved: {incident['label']} after {seconds} s.")


def note_fault(kind: str, text: str) -> None:
    """Record an injected fault: on the open incident of that kind, or as the start of the next."""
    now = timeutil.iso(timeutil.utcnow())
    metrics.FAULTS.labels(kind).inc()
    with runtime.lock:
        incident = runtime.open_incidents.get(kind)
        if incident is None:
            runtime.pending_faults[kind] = (now, text)
            return
    incident_store.update_incident(incident["id"], ("fault", text))


def note_action(kind: str, text: str, event_type: str = "action") -> None:
    """Add a recovery action ("action") or a snapshot restore ("restore") to the open incident
    of that kind, if there is one."""
    with runtime.lock:
        incident = runtime.open_incidents.get(kind)
    if incident:
        incident_store.update_incident(incident["id"], (event_type, text))


def note_alert(event: dict) -> None:
    """Put an alert state change on the timeline of each open incident it belongs to."""
    with runtime.lock:
        open_now = [runtime.open_incidents.get(k) for k in INCIDENT_KINDS[event["metric"]]]
    for incident in filter(None, open_now):
        incident_store.update_incident(incident["id"], ("alert", f"Alert: {event['text']}"))


def set_db_error(error: str | None) -> None:
    """Track the database state: an error opens an incident that stays open until a rollback."""
    with runtime.lock:
        runtime.db_error = error
        is_open = "db_corruption" in runtime.open_incidents
    if error and not is_open:
        open_incident("db_corruption", f"SQLite error: {error}.")
    elif not error and is_open:
        close_incident("db_corruption")


def refresh_db_state() -> str | None:
    error = db.check_app_db()
    set_db_error(error)
    return error
