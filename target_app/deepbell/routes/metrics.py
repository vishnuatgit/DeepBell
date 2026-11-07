"""Prometheus scrape endpoint."""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST

from .. import metrics

router = APIRouter()


@router.get("/metrics", tags=["monitoring"])
def get_metrics_text():
    """DeepBell's own metrics in the Prometheus text format (for Prometheus and Grafana)."""
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)
