"""Database snapshots and rollback."""

import os

from fastapi import APIRouter, Form, HTTPException

from .. import chaos, config, snapshots, timeutil

router = APIRouter()


@router.get("/api/backups", tags=["database"])
def get_backups():
    """List all available database snapshots, newest first, with their creation time (UTC)."""
    backup_list = []
    for name in snapshots.list_snapshots():
        try:
            size = os.path.getsize(os.path.join(config.BACKUP_DIR, name))
        except OSError:
            continue
        backup_list.append(
            {
                "filename": name,
                "size_kb": round(size / 1024, 2),
                "timestamp": timeutil.iso(snapshots.snapshot_time(name)),
                **snapshots.snapshot_info(name),
            }
        )
    return backup_list


@router.post("/api/rollback", tags=["database"])
def post_rollback(filename: str = Form(...)):
    """Manually restore the database to a selected snapshot."""
    if not config.BACKUP_NAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid snapshot file name.")
    if not os.path.exists(os.path.join(config.BACKUP_DIR, filename)):
        raise HTTPException(status_code=404, detail="Backup snapshot file not found.")

    config.logger.info(f"Triggering manual database rollback to: {filename}")
    try:
        error = chaos.rollback_to(filename)
    except OSError as e:
        config.logger.error(f"Manual rollback failed: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
    return {
        "status": "success" if error is None else "restored_with_errors",
        "message": f"Database successfully rolled back to snapshot {filename}.",
    }
