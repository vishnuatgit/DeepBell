"""The Chaos Lab: capped faults (CPU spike, memory leak, database corruption, crash) with
shared cooldowns, and the recovery actions (release memory, roll back).
"""

import math
import mmap
import os
import time

from fastapi import HTTPException

from . import config, eventlog, incidents, runtime, snapshots


def leaked_mb() -> int:
    with runtime.lock:
        return sum(len(block) for block in runtime.memory_leak_array) // config.MB


def cooldown_left(name: str, cooldown: int = config.FAULT_COOLDOWN_SEC) -> int:
    with runtime.lock:
        last = runtime.last_fault_at.get(name)
    if last is None:
        return 0
    return max(0, math.ceil(cooldown - (time.monotonic() - last)))


def claim_fault(name: str, cooldown: int = config.FAULT_COOLDOWN_SEC) -> None:
    """Start the cooldown for a fault, or raise a friendly 429 if it is still cooling down."""
    with runtime.lock:
        wait = cooldown_left(name, cooldown)
        if wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"{config.FAULT_NAMES[name]} was used moments ago. This demo is shared, so each "
                    f"action has a {cooldown} s cooldown: try again in {wait} s."
                ),
                headers={"Retry-After": str(wait)},
            )
        runtime.last_fault_at[name] = time.monotonic()


def allocate_leak(size_mb: int) -> mmap.mmap:
    """Anonymous memory with every page written, so it counts towards RSS at once and goes
    back to the operating system as soon as it is closed."""
    block = mmap.mmap(-1, size_mb * config.MB)
    for offset in range(0, size_mb * config.MB, mmap.PAGESIZE):
        block[offset] = 1
    return block


def hold_leak(size_mb: int) -> int:
    """Allocate a leak block and keep it. Returns the total MB now held."""
    block = allocate_leak(size_mb)
    with runtime.lock:
        if not runtime.memory_leak_array:
            runtime.leak_since = time.monotonic()
        runtime.memory_leak_array.append(block)
    return leaked_mb()


def free_leaked_memory() -> int:
    """Close every leak block (the memory goes straight back to the OS). Returns the MB freed."""
    with runtime.lock:
        blocks = list(runtime.memory_leak_array)
        runtime.memory_leak_array.clear()
        runtime.leak_since = None
    released = sum(len(block) for block in blocks) // config.MB
    for block in blocks:
        block.close()
    return released


def release_leak(source: str) -> int:
    """Free all leaked memory and record who did it. Returns the MB freed."""
    released = free_leaked_memory()
    if released:
        eventlog.log_event("info", f"{source}: released {released} MB of leaked memory.")
        incidents.note_action("memory_leak", f"{source}: released {released} MB of leaked memory.")
    return released


def release_stale_leak(now: float | None = None) -> int:
    """Free leaked memory held for LEAK_AUTO_RELEASE_SEC. Called by the watchdog every tick."""
    with runtime.lock:
        since = runtime.leak_since
    if since is None:
        return 0
    if (time.monotonic() if now is None else now) - since < config.LEAK_AUTO_RELEASE_SEC:
        return 0
    return release_leak(f"Auto-release after {config.LEAK_AUTO_RELEASE_SEC // 60} minutes")


def corrupt_database(source: str) -> bool:
    """Write garbage over the SQLite header of app.db, after making sure a snapshot exists."""
    if not snapshots.list_snapshots():
        snapshots.take_snapshot()
    try:
        # Overwrite the SQLite header. Appending bytes is not enough: SQLite
        # ignores trailing data, so the file would still open normally.
        with runtime.db_file_lock, open(config.DB_FILE, "r+b") as f:
            f.write(b"GARBAGE DATA CORRUPTION " * 5)
    except OSError as e:
        config.logger.error(f"Failed to corrupt DB: {e}")
        return False
    incidents.note_fault(
        "db_corruption", f"{source}: garbage written over the database file header."
    )
    eventlog.log_event("warn", f"{source}: wrote garbage over the database header.")
    incidents.refresh_db_state()
    return True


def rollback_to(filename: str, source: str = "Rollback") -> str | None:
    """Restore a snapshot and re-check the database. Returns the remaining error, if any."""
    snapshots.restore_snapshot(filename)
    incidents.note_action("db_corruption", f"{source}: database restored from snapshot {filename}.")
    eventlog.log_event("success", f"{source}: database restored from snapshot {filename}.")
    return incidents.refresh_db_state()


def fault_status() -> dict:
    with runtime.lock:
        since = runtime.leak_since
    auto_release_in = (
        max(0, math.ceil(config.LEAK_AUTO_RELEASE_SEC - (time.monotonic() - since)))
        if since is not None
        else None
    )
    return {
        "cooldown_sec": config.FAULT_COOLDOWN_SEC,
        "cpu_spike": {"cooldown": cooldown_left("cpu_spike"), "max_sec": config.MAX_SPIKE_SEC},
        "memory_leak": {
            "cooldown": cooldown_left("memory_leak"),
            "held_mb": leaked_mb(),
            "limit_mb": config.MAX_LEAK_MB,
            "step_mb": config.MAX_LEAK_STEP_MB,
            "auto_release_in": auto_release_in,
        },
        "corrupt_db": {"cooldown": cooldown_left("corrupt_db")},
        "fatal_crash": {"enabled": not config.PUBLIC_DEMO},
        "report": {"cooldown": cooldown_left("report", config.REPORT_COOLDOWN_SEC)},
        "demo": {"cooldown": cooldown_left("demo", config.DEMO_COOLDOWN_SEC)},
    }


def hard_exit():
    os._exit(1)  # Immediate hard exit
