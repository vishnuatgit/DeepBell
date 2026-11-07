"""Incident history, stored in metrics.db.

An incident has three times: started (the Chaos Lab fault that caused it, or else its first
anomalous sample), detected (when the watchdog opened it) and resolved. Time to detect is
detected - started; time to recover is resolved - detected. In this lab they are demo
measurements of this one process, not production numbers.
"""

import json

from . import config, db, runtime, timeutil

INCIDENT_COLUMNS = (
    "id, kind, status, started_at, detected_at, resolved_at, detail, report, timeline"
)
# Timeline events are sorted by time; events in the same second keep this logical order.
EVENT_ORDER = {
    "fault": 0,
    "flagged": 1,
    "alert": 2,
    "detected": 3,
    "report": 4,
    "action": 5,
    "restore": 5,
    "resolved": 6,
    "interrupted": 7,
}


def bump_incidents_rev() -> None:
    with runtime.lock:
        runtime.incidents_rev += 1


def incident_from_row(row) -> dict:
    incident_id, kind, status, started, detected, resolved, detail, report, timeline = row
    events = sorted(
        json.loads(timeline or "[]"), key=lambda e: (e["at"], EVENT_ORDER.get(e["type"], 9))
    )
    for event in events:  # seconds since the incident started, for the timeline view
        event["offset_sec"] = timeutil.seconds_between(started, event["at"]) or 0
    return {
        "id": incident_id,
        "kind": kind,
        "name": config.INCIDENT_NAMES.get(kind, kind),
        "label": config.INCIDENT_LABELS.get(kind, kind),
        "status": status,
        "started_at": started,
        "detected_at": detected,
        "resolved_at": resolved,
        "detail": detail,
        "report": report,
        "timeline": events,
        "ttd_sec": timeutil.seconds_between(started, detected),
        "ttr_sec": timeutil.seconds_between(detected, resolved) if resolved else None,
    }


def insert_incident(kind: str, started: str, detected: str, detail: str, events: list) -> int:
    conn = db.get_metrics_db()
    try:
        cur = conn.execute(
            "INSERT INTO incidents (kind, status, started_at, detected_at, detail, timeline) "
            "VALUES (?, 'open', ?, ?, ?, ?)",
            (kind, started, detected, detail, json.dumps(events)),
        )
        conn.execute(
            "DELETE FROM incidents WHERE id <= (SELECT max(id) FROM incidents) - ?",
            (config.INCIDENTS_KEEP,),
        )
        conn.commit()
        incident_id = cur.lastrowid
    finally:
        conn.close()
    bump_incidents_rev()
    return incident_id


def update_incident(incident_id: int | None, event: tuple[str, str] | None = None, **fields):
    """Set columns of one incident and optionally append (type, text) to its timeline."""
    if incident_id is None:
        return
    with runtime.incident_lock:
        conn = db.get_metrics_db()
        try:
            row = conn.execute(
                "SELECT timeline FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return
            if event:
                events = json.loads(row[0] or "[]")
                at = fields.get("resolved_at") or timeutil.iso(timeutil.utcnow())
                events.append({"at": at, "type": event[0], "text": event[1]})
                fields["timeline"] = json.dumps(events)
            if fields:
                # Column names come from the callers in this module, never from a request.
                assignments = ", ".join(f"{column} = ?" for column in fields)
                conn.execute(
                    f"UPDATE incidents SET {assignments} WHERE id = ?",
                    (*fields.values(), incident_id),
                )
                conn.commit()
        finally:
            conn.close()
    bump_incidents_rev()


def get_incident(incident_id: int) -> dict | None:
    conn = db.get_metrics_db()
    try:
        row = conn.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    finally:
        conn.close()
    return incident_from_row(row) if row else None


def list_incidents(limit: int = 20) -> list[dict]:
    conn = db.get_metrics_db()
    try:
        rows = conn.execute(
            f"SELECT {INCIDENT_COLUMNS} FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [incident_from_row(r) for r in rows]


def interrupt_open_incidents() -> int:
    """Mark incidents left open by a previous process as interrupted (their end is unknown)."""
    conn = db.get_metrics_db()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM incidents WHERE status = 'open'")]
    finally:
        conn.close()
    for incident_id in ids:
        update_incident(
            incident_id,
            event=("interrupted", "The server restarted while this incident was open."),
            status="interrupted",
        )
    return len(ids)
