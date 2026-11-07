"""Incident correlation: an evidence-based "likely cause" for one incident.

This is a heuristic, not root-cause analysis. It reads the incident's merged timeline (faults,
flagged samples, alerts, reports, restores) and the telemetry around it, and applies a few
rules a person could check by hand:

1. A Chaos Lab fault of the incident's own kind injected shortly before the first anomalous
   sample is the most likely trigger. High confidence when it came within HIGH_CONFIDENCE_SEC
   and nothing contradicts it, medium when it came later (up to FAULT_LINK_SEC) or the metrics
   disagree.
2. Which metric moved first: CPU or memory crossing its alert line. If the other metric moved
   clearly earlier than the incident's own, that contradicts a single-metric fault and lowers
   the confidence.
3. With no fault on record, the metrics alone can say what moved, never why: confidence is
   "low" for a CPU or memory incident and "unknown" for a database incident.
4. Other incidents that overlap in time are listed as context, never as a cause.

`likely_cause` takes plain data (the incident, telemetry rows, other incidents), so the rules
can be tested without a database.
"""

from datetime import datetime, timedelta
from statistics import median

from . import config, db, incident_store, timeutil

HIGH_CONFIDENCE_SEC = 10  # a matching fault this soon before the first anomalous sample
MOVED_FIRST_SEC = 2  # one metric must lead the other by this much to count as "first"
OVERLAP_SEC = 30  # incidents opened this close together are reported as overlapping
BASELINE_SAMPLES = 15  # samples before the incident used as its local baseline
LOOK_BEFORE_SEC = 40
DISCLAIMER = (
    "Heuristic: this lists what lines up in time with the incident. A matching event is "
    "evidence, not proof, and only the signals DeepBell records (injected faults, CPU, "
    "memory, alerts) are considered."
)


def parse(value) -> datetime | None:
    return timeutil.parse_utc(value)


def first_event(events: list[dict], event_type: str) -> dict | None:
    return next((e for e in events if e["type"] == event_type), None)


def fetch_samples(start: str, end: str) -> list[dict]:
    """Telemetry rows between two ISO times (inclusive), oldest first."""
    conn = db.get_metrics_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, cpu_percent, memory_mb, anomaly_status FROM metrics "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY id",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    return [{"at": r[0], "cpu": r[1], "ram": r[2], "status": r[3] or ""} for r in rows]


def metric_crossings(
    samples: list[dict], before: datetime, anomaly_at: datetime | None = None
) -> dict | None:
    """When CPU and memory first rose above their own alert lines, as seconds-resolution times.

    The baseline is the median of the samples taken before `before`, so it is local to this
    incident. Returns {"cpu": datetime | None, "memory": datetime | None}, or None when there
    are too few earlier samples to say. A metric that crossed its line but was back below it
    when the incident's anomaly began is ignored: that was an earlier, separate event.
    """
    quiet = [s for s in samples if parse(s["at"]) < before and "Anomalous" not in s["status"]]
    quiet = quiet[-BASELINE_SAMPLES:]
    if len(quiet) < 3:
        return None
    cpu_line = median(s["cpu"] for s in quiet) + config.CPU_MIN_DELTA
    ram_line = median(s["ram"] for s in quiet) + config.RAM_MIN_DELTA_MB
    crossed = {"cpu": None, "memory": None}
    elevated = {"cpu": False, "memory": False}  # above the line at the anomaly sample
    for s in samples:
        at = parse(s["at"])
        if at < before:
            continue
        if crossed["cpu"] is None and s["cpu"] > cpu_line:
            crossed["cpu"] = at
        if crossed["memory"] is None and s["ram"] > ram_line:
            crossed["memory"] = at
        if anomaly_at is not None and at <= anomaly_at:
            elevated = {"cpu": s["cpu"] > cpu_line, "memory": s["ram"] > ram_line}
    if anomaly_at is not None:
        crossed = {name: when if elevated[name] else None for name, when in crossed.items()}
    return crossed


def clock(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S") + " UTC"


def result(trigger, summary, confidence, reason, evidence, checked) -> dict:
    return {
        "trigger": trigger,
        "summary": summary,
        "confidence": confidence,
        "confidence_reason": reason,
        "evidence": evidence,
        "not_found": checked,
        "heuristic": True,
        "disclaimer": DISCLAIMER,
    }


def likely_cause(
    incident: dict, samples: list[dict] | None = None, others: list[dict] | None = None
) -> dict:
    """The most probable trigger of this incident, with the evidence and a confidence level."""
    kind, events = incident["kind"], incident["timeline"]
    own_metric = {"cpu_spike": "cpu", "memory_leak": "memory"}.get(kind)
    fault, flagged = first_event(events, "fault"), first_event(events, "flagged")
    detected_at = parse(incident["detected_at"])
    # The incident's own anomaly begins at its first anomalous sample (a database fault has no
    # such sample: the failed check is the anomaly).
    anomaly_at = parse(flagged["at"]) if flagged else detected_at
    evidence, not_found = [], []

    # Rule 4 (context only): incidents that overlap this one.
    for other in others or []:
        if other["id"] == incident["id"]:
            continue
        gap = (parse(other["detected_at"]) - detected_at).total_seconds()
        if abs(gap) <= OVERLAP_SEC:
            when = f"{abs(round(gap))} s {'later' if gap > 0 else 'earlier'}"
            evidence.append(
                f"Context: incident #{other['id']} ({other['name']}) opened {when}. "
                "A shared cause is possible but nothing here shows it."
            )

    # Rule 2: which metric moved first (CPU and memory incidents only).
    movement = None
    if own_metric and samples:
        movement = metric_crossings(samples, anomaly_at - timedelta(seconds=15), anomaly_at)
    conflict = None
    if movement and (movement["cpu"] or movement["memory"]):
        other_metric = "memory" if own_metric == "cpu" else "cpu"
        mine, theirs = movement[own_metric], movement[other_metric]
        names = {"cpu": "CPU", "memory": "Memory"}
        if mine and theirs:
            lead = (mine - theirs).total_seconds()
            if lead >= MOVED_FIRST_SEC:
                conflict = (
                    f"{names[other_metric]} crossed its alert line {round(lead)} s before "
                    f"{names[own_metric].lower()}, which does not fit a {names[own_metric]}-only fault."
                )
                evidence.append(conflict)
            elif -lead >= MOVED_FIRST_SEC:
                evidence.append(
                    f"{names[own_metric]} moved first ({clock(mine)}); "
                    f"{names[other_metric].lower()} followed {round(-lead)} s later."
                )
            else:
                evidence.append(
                    f"CPU and memory crossed their alert lines together ({clock(mine)})."
                )
        elif mine:
            evidence.append(
                f"{names[own_metric]} crossed its alert line at {clock(mine)}; "
                f"{names[other_metric]} stayed within its normal range."
            )

    # Alerts that fired are part of the evidence that the anomaly was real.
    alerts = [e for e in events if e["type"] == "alert" and " is firing" in e["text"]]
    if alerts:
        names = ", ".join(
            sorted({e["text"].split(" is firing")[0].removeprefix("Alert: ") for e in alerts})
        )
        evidence.append(f"Alert(s) firing during the incident: {names}.")

    # Rule 1: a fault of the same kind shortly before the first anomalous sample.
    if fault:
        gap = max(0, round((anomaly_at - parse(fault["at"])).total_seconds()))
        trigger = fault["text"].rstrip(".")
        when = "in the same second as" if gap == 0 else f"{gap} s before"
        evidence.insert(0, f"{fault['text']} It was recorded {when} the first anomalous sample.")
        if gap <= HIGH_CONFIDENCE_SEC and not conflict:
            confidence = "high"
            reason = (
                f"A fault of the same kind was injected {when} the first anomalous sample "
                "and no other signal contradicts it."
            )
        else:
            confidence = "medium"
            reason = (
                f"A fault of the same kind was injected {when} the first anomalous sample"
                + (f", longer than {HIGH_CONFIDENCE_SEC} s" if gap > HIGH_CONFIDENCE_SEC else "")
                + (", and the metrics partly disagree." if conflict else ".")
            )
        return result(
            trigger,
            f"Most likely triggered by: {trigger}.",
            confidence,
            reason,
            evidence,
            not_found,
        )

    # Rule 3: no fault on record.
    not_found.append(
        f"No injected fault of this kind was recorded in the {config.FAULT_LINK_SEC} s "
        "before the first anomalous sample."
    )
    if kind == "db_corruption":
        detail = incident.get("detail") or "the integrity check failed"
        evidence.insert(0, f"The database check failed: {detail}")
        not_found.append("CPU and memory are not part of this kind of fault, so they say nothing.")
        return result(
            "Unknown",
            "The trigger is unknown: the database file stopped reading as SQLite and nothing "
            "recorded explains it.",
            "unknown",
            "No fault was recorded and no other signal is related to a database failure.",
            evidence,
            not_found,
        )
    metric = {"cpu": "CPU", "memory": "Memory"}.get(own_metric, "The metric")
    summary = f"{metric} left its normal range with no recorded fault, so the trigger is unknown."
    return result(
        "Unknown",
        summary,
        "low" if evidence else "unknown",
        "Only the shape of the telemetry is available: it shows what moved and when, not why.",
        evidence,
        not_found,
    )


def analyse(incident: dict) -> dict:
    """The likely cause of a stored incident, with the telemetry and neighbours it needs."""
    started = parse(incident["started_at"])
    end = parse(incident["resolved_at"]) or parse(incident["detected_at"])
    if started is None or end is None:
        return likely_cause(incident)
    samples = fetch_samples(
        timeutil.iso(started - timedelta(seconds=LOOK_BEFORE_SEC)),
        timeutil.iso(end + timedelta(seconds=OVERLAP_SEC)),
    )
    return likely_cause(incident, samples, incident_store.list_incidents(10))


def markdown_section(cause: dict) -> str:
    """The likely cause as a Markdown section, for the incident report."""
    lines = [
        "## Correlation analysis (heuristic)",
        "",
        f"**{cause['summary']}** Confidence: **{cause['confidence']}**. {cause['confidence_reason']}",
        "",
    ]
    if cause["evidence"]:
        lines += ["Evidence:", ""] + [f"- {e}" for e in cause["evidence"]] + [""]
    if cause["not_found"]:
        lines += ["Checked, not found:", ""] + [f"- {n}" for n in cause["not_found"]] + [""]
    lines += [f"_{cause['disclaimer']}_", ""]
    return "\n".join(lines)
