"""Incident history, one incident's detail, and incident stats."""

from fastapi import APIRouter, HTTPException

from .. import config, incident_store, overview, reports

router = APIRouter()


@router.get("/api/incidents", tags=["incidents"])
def get_incidents(limit: int = 20):
    """Incident history, newest first, with timelines and times to detect and recover."""
    available = set(reports.list_reports())
    incidents = incident_store.list_incidents(max(1, min(limit, config.INCIDENTS_KEEP)))
    for incident in incidents:
        incident["report_available"] = incident["report"] in available
    return incidents


@router.get("/api/incidents/stats", tags=["incidents"])
def get_incident_stats():
    """Incident counts and median times to detect and recover (newest 20 incidents)."""
    return overview.incident_stats()


@router.get("/api/incidents/{incident_id}", tags=["incidents"])
def get_incident_detail(incident_id: int):
    incident = incident_store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="No such incident.")
    incident["report_available"] = incident["report"] in set(reports.list_reports())
    return incident
