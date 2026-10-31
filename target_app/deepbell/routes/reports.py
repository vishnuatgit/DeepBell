"""Incident reports: list, read, download and generate."""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import chaos, config, rca, reports, runtime

router = APIRouter()


@router.get("/api/reports", tags=["reports"])
def get_reports():
    """All kept reports, newest first, with their title and incident (if any)."""
    incident_ids = reports.report_incident_ids()
    listing = []
    for name in reports.list_reports():
        try:
            with open(os.path.join(config.REPORTS_DIR, name), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        listing.append(
            {
                "filename": name,
                "generated_at": reports.report_generated_at(name),
                "title": reports.report_title(content, name),
                "incident_id": incident_ids.get(name),
                "source": reports.report_source(content),
            }
        )
    return listing


@router.get("/api/reports/latest", tags=["reports"])
def get_latest_report():
    """The newest incident report (Markdown), or 404 if none exists yet."""
    report = reports.latest_report()
    if not report:
        raise HTTPException(status_code=404, detail="No incident reports yet.")
    return report


def checked_report_name(name: str) -> str:
    if not config.REPORT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid report file name.")
    return name


@router.get("/api/reports/{name}", tags=["reports"])
def get_report(name: str):
    report = reports.read_report(checked_report_name(name))
    if report is None:
        raise HTTPException(
            status_code=404, detail="Report not found. Only the newest 20 are kept."
        )
    return report


@router.get("/api/reports/{name}/download", tags=["reports"])
def download_report(name: str):
    """The report as a Markdown file, with its incident's timeline appended."""
    content = reports.report_for_download(checked_report_name(name))
    if content is None:
        raise HTTPException(
            status_code=404, detail="Report not found. Only the newest 20 are kept."
        )
    return Response(
        content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/rca/generate", tags=["reports"])
def post_generate_rca():
    """Manually trigger the AI agent to generate a Root Cause Analysis report."""
    if runtime.reports_in_flight:
        return {"status": "busy", "message": "A report is already being generated."}
    chaos.claim_fault("report", config.REPORT_COOLDOWN_SEC)
    rca.trigger_auto_rca()
    return {"status": "started", "message": "Root Cause Analysis report generation triggered."}
