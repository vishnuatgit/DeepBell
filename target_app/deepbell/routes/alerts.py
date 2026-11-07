"""Alert rules, their live states and the recent state changes."""

from fastapi import APIRouter

from .. import notifications, runtime

router = APIRouter()


@router.get("/api/alerts", tags=["alerts"])
def get_alerts():
    """Every rule with its state (ok, pending, firing, resolved), the newest state changes,
    any problems found in the rules file, and whether a webhook is configured."""
    result = runtime.alert_engine.snapshot()
    result["counts"] = runtime.alert_engine.counts()
    result["webhook_configured"] = bool(notifications.webhook_url())
    return result
