"""Incident reports: the built-in template, the Markdown files on disk and their metadata."""

import glob
import os
import re
from datetime import UTC, datetime, timedelta

from . import (
    config,
    db,
    incident_store,
    timeutil,
)


def fetch_recent_metrics(limit: int = 15) -> list[dict]:
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
    ][::-1]


def telemetry_table(rows: list[dict]) -> str:
    lines = ["| Timestamp | CPU (%) | Memory (MB) | Watchdog status |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['timestamp']} | {r['cpu']:.1f} | {r['ram']:.1f} | {r['status']} |")
    return "\n".join(lines)


def flagged_labels(rows: list[dict]) -> list[str]:
    """The sample classifications (CPU Spike, Memory Leak) present among these rows."""
    statuses = " ".join(r["status"] or "" for r in rows)
    return [label for label in config.SAMPLE_LABELS.values() if label in statuses]


def report_summary(rows: list[dict], incident: str, detail: str) -> tuple[str, str]:
    """The report's kind and summary paragraph.

    Only samples of the incident's own kind are counted: a CPU report ignores samples flagged
    for a memory leak, and a database report says that CPU and memory are not part of its fault
    instead of describing another fault's samples as its own.
    """
    flagged = [r for r in rows if "Anomalous" in (r["status"] or "")]
    if "Database" in incident:
        failed = f"failed ({detail.rstrip('.')})" if detail else "failed"
        summary = f"The database check {failed}. CPU and memory are not part of this fault."
        if flagged:
            kinds = ", ".join(label.lower() for label in flagged_labels(flagged))
            summary += (
                f" {len(flagged)} of the last {len(rows)} samples were flagged, but they belong "
                f"to another fault ({kinds})."
            )
        return incident, summary
    word = next((label for label in config.SAMPLE_LABELS.values() if label in incident), None)
    anomalies = [r for r in flagged if word is None or word in r["status"]]
    if anomalies:
        latest = anomalies[-1]["status"]
        summary = (
            f"The watchdog flagged {len(anomalies)} of the last {len(rows)} samples. "
            f"The first flagged sample was at {anomalies[0]['timestamp']}; the latest "
            f"classification is **{latest}**."
        )
        kind = incident or latest
    else:
        kind = incident or "No anomaly in window"
        summary = f"None of the last {len(rows)} samples was flagged by the watchdog."
    if detail:
        summary += f"\n\nDetail: {detail}"
    return kind, summary


def build_template_report(
    rows: list[dict],
    generated_at: str,
    note: str = "",
    incident: str = "",
    detail: str = "",
    timeline: list[dict] | None = None,
) -> str:
    """Deterministic incident report used when no Gemini API key is configured."""
    if not rows and not incident:
        return (
            f"# Incident Report\n\nGenerated: {generated_at}\n\n"
            "No telemetry has been recorded yet, so there is nothing to analyse."
        )
    cpus = [r["cpu"] for r in rows]
    rams = [r["ram"] for r in rows]
    kind, summary = report_summary(rows, incident, detail)
    if "Database" in kind:
        cause = (
            "The application database no longer reads as SQLite: its file was overwritten or "
            "damaged on disk. CPU and memory are not expected to move for this kind of fault."
        )
        actions = [
            "Roll back to the newest good snapshot (Time Machine view)",
            "Find out what wrote to the database file outside SQLite",
            "Keep snapshots on separate storage from the live database",
        ]
    elif "CPU" in kind:
        cause = (
            "A burst of CPU-bound work in the process (for example a busy loop or a hot code path)."
        )
        actions = [
            "Profile the hot path",
            "Move CPU-heavy work off the request thread",
            "Add CPU alerts",
        ]
    elif "Memory" in kind:
        cause = (
            "Memory that is allocated and never released (resident memory grew and stayed high)."
        )
        actions = [
            "Take a heap snapshot",
            "Check caches and global lists for unbounded growth",
            "Restart the process if memory keeps rising",
        ]
    elif "Degradation" in kind:
        cause = "CPU and memory moved away from their baseline together."
        actions = [
            "Correlate with recent deploys and traffic",
            "Check both CPU and memory profiles",
        ]
    else:
        cause = "No cause identified: telemetry is within its normal range."
        actions = ["Keep monitoring"]
    parts = [
        f"# Incident Report: {kind}",
        "",
        f"**Generated:** {generated_at}  ",
        "**Generator:** built-in template (set `GOOGLE_API_KEY` for an AI-written analysis)",
    ]
    if note:
        parts += ["", f"> {note}"]
    parts += ["", "## Summary", "", summary, ""]
    if timeline:
        parts += ["## Timeline (UTC)", "", "| Time | Event |", "|---|---|"]
        parts += [f"| {e['at']} | {e['text']} |" for e in timeline]
        parts += [""]
    if rows:
        parts += [
            "## Telemetry statistics",
            "",
            f"- CPU: min {min(cpus):.1f} %, mean {sum(cpus) / len(cpus):.1f} %, max {max(cpus):.1f} %",
            f"- Memory: min {min(rams):.1f} MB, mean {sum(rams) / len(rams):.1f} MB, max {max(rams):.1f} MB",
            "",
            "## Recent telemetry",
            "",
            telemetry_table(rows),
            "",
        ]
    parts += ["## Likely cause", "", cause, "", "## Recommended actions", ""]
    parts += [f"- {a}" for a in actions]
    return "\n".join(parts) + "\n"


def list_reports() -> list[str]:
    """Report file names, newest first (the UTC creation time is part of the name)."""
    names = [
        os.path.basename(p) for p in glob.glob(os.path.join(config.REPORTS_DIR, "RCA_Report_*.md"))
    ]
    return sorted(names, reverse=True)


def write_report(content: str, now: datetime | None = None) -> str:
    now = now or timeutil.utcnow()
    path = os.path.join(config.REPORTS_DIR, f"RCA_Report_{now:%Y%m%d_%H%M%S_%f}.md")
    while os.path.exists(path):  # the clock can repeat a microsecond on some platforms
        now += timedelta(microseconds=1)
        path = os.path.join(config.REPORTS_DIR, f"RCA_Report_{now:%Y%m%d_%H%M%S_%f}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    for old in list_reports()[config.REPORTS_KEEP :]:
        os.remove(os.path.join(config.REPORTS_DIR, old))
    return path


def report_generated_at(name: str) -> str | None:
    match = config.REPORT_NAME_RE.match(name)
    if not match:
        return None
    return timeutil.iso(datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC))


def report_incident_ids() -> dict[str, int]:
    """Report file name -> id of the incident it was written for."""
    conn = db.get_metrics_db()
    try:
        rows = conn.execute("SELECT report, id FROM incidents WHERE report IS NOT NULL").fetchall()
    finally:
        conn.close()
    return dict(rows)


def read_report(name: str) -> dict | None:
    """One report with its metadata, or None if the name is invalid or the file is gone."""
    if not config.REPORT_NAME_RE.match(name):
        return None
    try:
        with open(os.path.join(config.REPORTS_DIR, name), encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    return {
        "filename": name,
        "generated_at": report_generated_at(name),
        "content": content,
        "incident_id": report_incident_ids().get(name),
        "source": report_source(content),
    }


def report_title(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return (match.group(1) if match else fallback).replace("Incident Report: ", "").strip()


def report_source(content: str) -> str:
    """Who wrote a report, read from its text so older files are labelled too.

    "example" (the startup seed), "template-fallback" (the AI call failed), "template" or
    "gemini". The dashboard labels template reports as such, so they are never mistaken for AI.
    """
    if "Example report, written at startup" in content:
        return "example"
    if "this is the template fallback" in content:
        return "template-fallback"
    if "**Generator:** built-in template" in content:
        return "template"
    return "gemini"


def latest_report() -> dict | None:
    for name in list_reports():
        report = read_report(name)
        if report:
            return report
    return None


def report_for_download(name: str) -> str | None:
    """The report as saved, plus the incident's timeline and measured times when it has one."""
    report = read_report(name)
    if report is None:
        return None
    content = report["content"].rstrip() + "\n"
    incident = incident_store.get_incident(report["incident_id"]) if report["incident_id"] else None
    if incident is None:
        return content
    lines = [
        "",
        "---",
        "",
        "## Incident timeline (demo measurements)",
        "",
        f"Incident #{incident['id']}: {incident['name']}, status **{incident['status']}**.",
        "",
        "| Time (UTC) | Event |",
        "|---|---|",
    ]
    lines += [f"| {e['at']} | {e['text']} |" for e in incident["timeline"]]
    ttd, ttr = incident["ttd_sec"], incident["ttr_sec"]
    lines += [
        "",
        f"- Time to detect (start to incident opened): {ttd} s",
        "- Time to recover (incident opened to resolved): "
        + (f"{ttr} s" if ttr is not None else "not resolved yet"),
        "",
        "These times come from this DeepBell instance's own timestamps during a demo. They "
        "describe this lab, not a production system.",
    ]
    return content + "\n".join(lines) + "\n"
