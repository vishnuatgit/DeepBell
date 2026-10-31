"""Chaos Lab endpoints: inject capped faults and release leaked memory."""

import threading
import time

from fastapi import APIRouter, HTTPException

from .. import chaos, config, eventlog, incidents, runtime

router = APIRouter()


@router.post("/fault/memory_leak", tags=["chaos lab"])
def inject_memory_leak(size_mb: int = config.MAX_LEAK_STEP_MB):
    """Simulates a memory leak by holding a block of memory until it is released."""
    if not 1 <= size_mb <= config.MAX_LEAK_STEP_MB:
        raise HTTPException(
            status_code=400, detail=f"size_mb must be between 1 and {config.MAX_LEAK_STEP_MB}."
        )
    held = chaos.leaked_mb()
    if held + size_mb > config.MAX_LEAK_MB:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The demo already holds {held} MB of leaked memory (limit {config.MAX_LEAK_MB} MB). "
                "Release it first."
            ),
        )
    chaos.claim_fault("memory_leak")
    incidents.note_fault(
        "memory_leak", f"Chaos Lab: leaked {size_mb} MB (holding {held + size_mb} MB)."
    )
    total = chaos.hold_leak(size_mb)
    eventlog.log_event(
        "warn", f"Chaos Lab: leaked {size_mb} MB of memory (now holding {total} MB)."
    )
    with runtime.lock:
        blocks = len(runtime.memory_leak_array)
    return {
        "status": "fault_injected",
        "type": "memory_leak",
        "allocated_mb": size_mb,
        "held_mb": total,
        "total_leaked_blocks": blocks,
    }


@router.post("/fault/memory_release", tags=["chaos lab"])
def release_memory():
    """Free all memory held by the memory leak fault."""
    return {"status": "released", "released_mb": chaos.release_leak("Chaos Lab")}


@router.post("/fault/cpu_spike", tags=["chaos lab"])
def inject_cpu_spike(duration_sec: int = config.MAX_SPIKE_SEC):
    """Simulates a CPU spike with a busy loop in a background thread."""
    if not 1 <= duration_sec <= config.MAX_SPIKE_SEC:
        raise HTTPException(
            status_code=400, detail=f"duration_sec must be between 1 and {config.MAX_SPIKE_SEC}."
        )
    chaos.claim_fault("cpu_spike")
    incidents.note_fault("cpu_spike", f"Chaos Lab: CPU busy loop for {duration_sec} s.")
    eventlog.log_event("warn", f"Chaos Lab: CPU spike for {duration_sec} s.")

    def busy_loop():
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            _ = 1000000 * 1000000

    # Run in background to prevent blocking main web app requests completely
    threading.Thread(target=busy_loop, daemon=True).start()
    return {"status": "fault_injected", "type": "cpu_spike", "duration": duration_sec}


@router.post("/fault/fatal_crash", tags=["chaos lab"])
def inject_fatal_crash():
    """Simulates a fatal application crash by exiting the process (local runs only)."""
    if config.PUBLIC_DEMO:
        raise HTTPException(
            status_code=403,
            detail=(
                "Disabled on the public demo: it would take the site down for everyone. "
                "Run DeepBell locally (python run_all.py) to try it."
            ),
        )
    config.logger.fatal("FAULT INJECTED: Fatal crash triggered. Shutting down process immediately.")

    def kill_soon():
        time.sleep(1)
        chaos.hard_exit()

    threading.Thread(target=kill_soon, daemon=True).start()
    return {
        "status": "fault_injected",
        "type": "fatal_crash",
        "message": "Process will terminate in 1 second.",
    }


@router.post("/fault/corrupt_db", tags=["chaos lab"])
def inject_db_corruption():
    """Simulates database corruption by writing garbage over the SQLite header."""
    chaos.claim_fault("corrupt_db")
    if not chaos.corrupt_database("Chaos Lab"):
        return {"status": "failed_to_corrupt"}
    return {"status": "fault_injected", "type": "corrupt_db"}
