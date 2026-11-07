"""Report generation in the background: Gemini when a key is set, the template otherwise."""

import os
import threading

from . import config, eventlog, incident_store, llm, metrics, reports, runtime, timeutil


def trigger_auto_rca(incident: str = "") -> None:
    """Write a report in a background thread. Reports are serialised, never dropped."""
    with runtime.lock:
        runtime.reports_in_flight += 1
        # Capture the incident now: by the time a queued report runs, it may have closed.
        record = dict(runtime.open_incidents.get(incident) or {})

    def run():
        try:
            generate_rca_report_task(incident, record)
        finally:
            with runtime.lock:
                runtime.reports_in_flight -= 1

    threading.Thread(target=run, daemon=True).start()


def generate_rca_report_task(incident: str = "", record: dict | None = None) -> str:
    """Generate a report (AI if a key is set, template otherwise) and save it. Returns the path.

    `record` is the open incident the report is for (captured when it was triggered); the
    report is linked to it and listed on its timeline.
    """
    with runtime.report_lock:
        config.logger.info("Starting Root Cause Analysis...")
        try:
            rows = reports.fetch_recent_metrics(15)
            generated_at = timeutil.iso(timeutil.utcnow())
            label = config.INCIDENT_LABELS.get(incident, "")
            record = record or {}
            with runtime.lock:
                detail = record.get("detail") or runtime.open_incidents.get(incident, {}).get(
                    "detail", ""
                )
            incident_id = record.get("id")
            stored = incident_store.get_incident(incident_id) if incident_id else None
            api_key = llm.get_api_key()
            content = None
            note = ""
            if api_key:
                try:
                    content = llm.generate_ai_report(api_key, rows, label)
                except Exception as e:
                    config.logger.error(f"AI report failed, using template instead: {e}")
                    note = (
                        f"The AI report failed ({type(e).__name__}); this is the template fallback."
                    )
            source = "Gemini" if content else "template"
            if not content:
                timeline = stored["timeline"] if stored else None
                content = reports.build_template_report(
                    rows, generated_at, note, label, detail, timeline
                )
            report_path = reports.write_report(content)
            name = os.path.basename(report_path)
            metrics.REPORTS.labels(source).inc()
            incident_store.update_incident(
                incident_id, ("report", f"Report written ({source}): {name}"), report=name
            )
            eventlog.log_event("success", f"Incident report written ({source}): {name}")
            return report_path
        except Exception as e:
            config.logger.error(f"RCA generation error: {e}")
            return ""
