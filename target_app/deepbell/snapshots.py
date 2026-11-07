"""Snapshots of app.db: take, list, check and restore them, and heal the database at startup."""

import glob
import hashlib
import os
import shutil
import sqlite3
from datetime import UTC, datetime

from . import (
    config,
    db,
    eventlog,
    metrics,
    runtime,
    timeutil,
)


# Self-Healing Database Integrity Check
def verify_and_heal_database():
    config.logger.info("Verifying database integrity...")
    if not os.path.exists(config.DB_FILE):
        return
    error = db.integrity_error(config.DB_FILE)
    if error is None or error.startswith("no such table"):
        config.logger.info("Database integrity verified successfully.")
        return
    config.logger.error(f"Database corruption detected: {error}. Executing auto-recovery...")
    if restore_latest_backup():
        return
    # No usable snapshot: keep the damaged file for inspection and start with an empty database.
    aside = f"{config.DB_FILE}.corrupt-{timeutil.utcnow():%Y%m%d_%H%M%S}"
    with runtime.db_file_lock:
        os.replace(config.DB_FILE, aside)
    config.logger.error(f"No usable snapshot; moved the damaged database to {aside}.")


def list_snapshots() -> list[str]:
    """Snapshot file names, newest first. The creation time (UTC) is part of the name."""
    names = [
        os.path.basename(p) for p in glob.glob(os.path.join(config.BACKUP_DIR, "app_*.db.bak"))
    ]
    return sorted((n for n in names if config.BACKUP_NAME_RE.match(n)), reverse=True)


def snapshot_time(name: str) -> datetime:
    stamp = config.BACKUP_NAME_RE.match(name).group(1)
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)


def file_digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def take_snapshot(now: datetime | None = None) -> str | None:
    """Copy app.db into BACKUP_DIR and keep the newest SNAPSHOTS_KEEP copies.

    Skipped when the database is unreadable or unchanged since the newest snapshot, so the
    kept snapshots are distinct restore points. Returns the new file name, or None.
    """
    if not os.path.exists(config.DB_FILE):
        return None
    with runtime.db_file_lock:
        with open(config.DB_FILE, "rb") as f:
            data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    snapshots = list_snapshots()
    if snapshots and file_digest(os.path.join(config.BACKUP_DIR, snapshots[0])) == digest:
        return None
    name = f"app_{(now or timeutil.utcnow()):%Y%m%d_%H%M%S}.db.bak"
    if name in snapshots:
        return None
    # Write to a temporary file and check that copy, so a torn read never becomes a snapshot.
    tmp = os.path.join(config.BACKUP_DIR, f".{name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    error = db.integrity_error(tmp)
    if error:
        os.remove(tmp)
        config.logger.warning(f"Snapshot skipped, database is unreadable: {error}")
        return None
    os.replace(tmp, os.path.join(config.BACKUP_DIR, name))
    for old in list_snapshots()[config.SNAPSHOTS_KEEP :]:
        os.remove(os.path.join(config.BACKUP_DIR, old))
    eventlog.log_event("info", f"Snapshot taken: {name}")
    return name


def snapshot_info(name: str) -> dict:
    """Whether a snapshot passes quick_check, and how many transactions it holds, so the Time
    Machine can say what a rollback brings back."""
    try:
        conn = sqlite3.connect(os.path.join(config.BACKUP_DIR, name))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            count = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return {"readable": False, "transactions": None}
    if not check or check[0] != "ok":
        return {"readable": False, "transactions": None}
    return {"readable": True, "transactions": count}


def restore_snapshot(name: str) -> None:
    with runtime.db_file_lock:
        shutil.copyfile(os.path.join(config.BACKUP_DIR, name), config.DB_FILE)
    metrics.SNAPSHOT_RESTORES.inc()


def newest_good_snapshot() -> str | None:
    """The newest snapshot that passes an integrity check."""
    for name in list_snapshots():
        if db.integrity_error(os.path.join(config.BACKUP_DIR, name)) is None:
            return name
    return None


def restore_latest_backup() -> str | None:
    """Restore the newest readable snapshot. Returns its name, or None if none was restored."""
    name = newest_good_snapshot()
    if name is None:
        config.logger.error("No backups available for restoration.")
        return None
    config.logger.info(f"Restoring database from snapshot: {name}")
    try:
        restore_snapshot(name)
    except OSError as e:
        config.logger.error(f"Failed to restore database: {e}")
        return None
    config.logger.info("Database restored successfully.")
    return name


def snapshot_is_current(name: str) -> bool:
    """True when the snapshot holds exactly what app.db holds now. Both files are a few KB
    (the demo keeps at most TRANSACTIONS_KEEP rows), so hashing them on each poll is cheap."""
    try:
        with runtime.db_file_lock:
            live = file_digest(config.DB_FILE)
        return live == file_digest(os.path.join(config.BACKUP_DIR, name))
    except OSError:
        return False


def latest_snapshot() -> dict | None:
    """The newest snapshot, and whether it is "current". Snapshots are taken only when the data
    changes, so an old snapshot that is current means the backups are up to date."""
    snapshots = list_snapshots()
    if not snapshots:
        return None
    newest = snapshots[0]
    return {
        "filename": newest,
        "timestamp": timeutil.iso(snapshot_time(newest)),
        "current": snapshot_is_current(newest),
    }
