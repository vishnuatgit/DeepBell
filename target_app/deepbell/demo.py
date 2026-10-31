"""The guided demo: leak, detect, report, recover, then corrupt and roll back.

One scripted scenario at a time, run in a server thread rather than in the browser: a
closed tab can never leave memory leaked, and a second visitor watches the run in progress
instead of starting a competing one. It goes through the same caps and cooldowns as the
Chaos Lab, and the watchdog detects its faults exactly as it detects any other.
"""

import time

from fastapi import HTTPException

from . import chaos, config, eventlog, incident_store, incidents, llm, runtime, snapshots, timeutil


class DemoFailed(Exception):
    """A demo step could not finish. The message is shown to the visitor."""


def new_demo_run(run_id: int) -> dict:
    return {
        "id": run_id,
        "state": "running",
        "started_at": timeutil.iso(timeutil.utcnow()),
        "finished_at": None,
        "steps": [
            {"key": key, "title": title, "state": "pending", "detail": "", "done_at": None}
            for key, title in config.DEMO_STEPS
        ],
        "incident_ids": [],
        "summary": "",
    }


def demo_state() -> dict | None:
    """A copy of the current or last run, safe to serialise while the demo thread runs."""
    with runtime.lock:
        if runtime.demo is None:
            return None
        return {
            **runtime.demo,
            "steps": [dict(step) for step in runtime.demo["steps"]],
            "incident_ids": list(runtime.demo["incident_ids"]),
        }


def demo_step(key: str, state: str, detail: str | None = None) -> None:
    with runtime.lock:
        step = next(s for s in runtime.demo["steps"] if s["key"] == key)
        step["state"] = state
        if detail is not None:
            step["detail"] = detail
        if state in ("done", "failed"):
            step["done_at"] = timeutil.iso(timeutil.utcnow())


def finish_demo(state: str, summary: str) -> None:
    with runtime.lock:
        runtime.demo["state"] = state
        runtime.demo["finished_at"] = timeutil.iso(timeutil.utcnow())
        runtime.demo["summary"] = summary
        runtime.last_fault_at["demo"] = time.monotonic()  # the demo cooldown counts from the end
    eventlog.log_event(
        "success" if state == "succeeded" else "warn", f"Guided demo {state}. {summary}"
    )


def demo_wait(key: str, check, waiting_text, timeout: float = config.DEMO_STEP_TIMEOUT_SEC):
    """Poll check() until it returns something truthy, narrating waiting_text() meanwhile."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise DemoFailed(f"Timed out after {timeout:.0f} s. {waiting_text()}")
        demo_step(key, "active", waiting_text())
        time.sleep(config.DEMO_POLL_SEC)


def demo_claim(key: str, fault: str) -> None:
    """Start a fault's cooldown for the demo, waiting while another visitor's cooldown runs."""
    deadline = time.monotonic() + config.FAULT_COOLDOWN_SEC + 5
    while True:
        try:
            chaos.claim_fault(fault)
            return
        except HTTPException:
            if time.monotonic() > deadline:
                raise DemoFailed("The shared cooldown did not end in time.")
            demo_step(
                key,
                "active",
                f"Another visitor used this fault moments ago. Waiting {chaos.cooldown_left(fault)} s "
                f"for the shared {config.FAULT_COOLDOWN_SEC} s cooldown.",
            )
            time.sleep(config.DEMO_POLL_SEC)


def demo_blocker() -> str | None:
    """Why the shared server is not ready for a demo run, or None if it is."""
    with runtime.lock:
        if runtime.detector.baseline is None:
            return "The watchdog is still learning its baseline (about 30 s after a restart)."
        incident = next(iter(runtime.open_incidents.values()), None)
    if incident:
        return (
            f"Waiting for an open incident ({incident['name'].lower()}) to resolve. This server "
            "is shared, so another visitor may be testing it."
        )
    held = chaos.leaked_mb()
    if held:
        return f"Another visitor is holding {held} MB of leaked memory. Waiting for its release."
    return None


def demo_get_ready() -> None:
    """Wait for the shared server to be healthy; after a while, clear faults left behind."""
    began = time.monotonic()
    cleaned = False
    while (reason := demo_blocker()) is not None:
        waited = time.monotonic() - began
        if waited > config.DEMO_READY_TIMEOUT_SEC:
            raise DemoFailed(f"{reason} Try again in a minute.")
        if not cleaned and waited > config.DEMO_CLEANUP_AFTER_SEC:
            # Anyone can do both of these from the Chaos Lab and the Time Machine.
            cleaned = True
            chaos.release_leak("Guided demo (clearing a leftover leak)")
            snapshot = snapshots.newest_good_snapshot() if runtime.db_error else None
            if snapshot:
                chaos.rollback_to(snapshot, "Guided demo (clearing a leftover corruption)")
        demo_step("ready", "active", reason)
        time.sleep(config.DEMO_POLL_SEC)


def resolved_incident(incident_id: int) -> dict | None:
    incident = incident_store.get_incident(incident_id)
    return incident if incident and incident["status"] != "open" else None


def run_demo_scenario() -> None:
    """Leak memory, watch the watchdog open an incident and write its report, release the
    memory and watch the incident resolve; then corrupt the database and roll it back."""
    key = "ready"
    leaked = False
    try:
        demo_step(key, "active", "Checking the baseline, open incidents and held memory.")
        demo_get_ready()
        with runtime.lock:
            base_ram = runtime.detector.baseline["ram"]
        alert_mb = base_ram + config.RAM_MIN_DELTA_MB
        demo_step(
            key,
            "done",
            f"Healthy. Memory baseline {base_ram:.0f} MB; an incident opens above "
            f"{alert_mb:.0f} MB.",
        )
        time.sleep(config.DEMO_PAUSE_SEC)

        key = "leak"
        demo_step(key, "active", "Allocating memory.")
        demo_claim(key, "memory_leak")
        if chaos.leaked_mb() + config.DEMO_LEAK_MB > config.MAX_LEAK_MB:
            raise DemoFailed("Too much leaked memory is held already; the demo keeps to the cap.")
        # Record the fault first: the watchdog may sample the new memory within milliseconds.
        incidents.note_fault("memory_leak", f"Guided demo: leaked {config.DEMO_LEAK_MB} MB.")
        chaos.hold_leak(config.DEMO_LEAK_MB)
        leaked = True
        eventlog.log_event("warn", f"Guided demo: leaked {config.DEMO_LEAK_MB} MB of memory.")
        demo_step(
            key,
            "done",
            f"Holding {config.DEMO_LEAK_MB} MB. The next samples should read about "
            f"{base_ram + config.DEMO_LEAK_MB:.0f} MB.",
        )

        key = "detect"

        def streak_text():
            with runtime.lock:
                streak = runtime.detector.streak["memory_leak"]
            return (
                f"Memory {runtime.last_ram:.0f} MB, alert above {alert_mb:.0f} MB. Anomalous samples "
                f"in a row: {streak} of {config.OPEN_AFTER}."
            )

        incident = demo_wait(key, lambda: runtime.open_incidents.get("memory_leak"), streak_text)
        incident_id = incident["id"]
        if incident_id is None:
            raise DemoFailed("The incident opened but could not be recorded.")
        with runtime.lock:
            runtime.demo["incident_ids"].append(incident_id)
        ttd = timeutil.seconds_between(incident["started_at"], incident["opened_at"])
        demo_step(
            key,
            "done",
            f"Incident #{incident_id} opened {ttd} s after the leak, once {config.OPEN_AFTER} samples "
            "in a row were anomalous.",
        )

        key = "report"
        demo_wait(
            key,
            lambda: (incident_store.get_incident(incident_id) or {}).get("report"),
            lambda: "Writing the report from the last 15 samples.",
            timeout=90 if llm.get_api_key() else config.DEMO_STEP_TIMEOUT_SEC,
        )
        demo_step(
            key,
            "done",
            f"Linked to incident #{incident_id}; read or download it on the Incidents view.",
        )
        time.sleep(2 * config.DEMO_PAUSE_SEC)  # time to look at the open incident

        key = "release"
        released = chaos.release_leak("Guided demo")
        leaked = False
        demo_step(
            key,
            "done",
            f"Released {released} MB. Memory drops on the next sample."
            if released
            else "Another visitor released it first. Memory drops on the next sample.",
        )

        key = "resolve"

        def calm_text():
            with runtime.lock:
                calm = runtime.detector.calm["memory_leak"]
            return f"Memory {runtime.last_ram:.0f} MB. Normal samples in a row: {calm} of {config.CLOSE_AFTER}."

        resolved = demo_wait(key, lambda: resolved_incident(incident_id), calm_text)
        demo_step(
            key,
            "done",
            f"Incident #{incident_id} resolved {resolved['ttr_sec']} s after it opened.",
        )
        time.sleep(config.DEMO_PAUSE_SEC)

        key = "corrupt"
        demo_step(key, "active", "Writing over the database header.")
        demo_claim(key, "corrupt_db")
        if not chaos.corrupt_database("Guided demo"):
            raise DemoFailed("Could not write to the database file.")
        db_incident = demo_wait(
            key,
            lambda: runtime.open_incidents.get("db_corruption"),
            lambda: "Waiting for the database check to fail.",
        )
        with runtime.lock:
            runtime.demo["incident_ids"].append(db_incident["id"])
        demo_step(
            key,
            "done",
            f"Queries now fail ({runtime.db_error}). Incident #{db_incident['id']} opened.",
        )
        time.sleep(config.DEMO_PAUSE_SEC)

        key = "rollback"
        demo_step(key, "active", "Looking for the newest readable snapshot.")
        snapshot = snapshots.newest_good_snapshot()
        if snapshot is None:
            raise DemoFailed("No readable snapshot to roll back to.")
        error = chaos.rollback_to(snapshot, "Guided demo")
        if error:
            raise DemoFailed(f"The database is still unreadable after the rollback: {error}")
        demo_step(key, "done", f"Restored {snapshot}. Incident #{db_incident['id']} resolved.")
        finish_demo(
            "succeeded",
            f"The leak was detected {ttd} s after it started and resolved "
            f"{resolved['ttr_sec']} s after detection; the database was restored from "
            f"{snapshot}.",
        )
    except DemoFailed as e:
        demo_step(key, "failed", str(e))
        finish_demo("failed", str(e))
    except Exception as e:
        config.logger.exception("Guided demo failed")
        demo_step(key, "failed", f"Unexpected error: {e}")
        finish_demo("failed", "Unexpected error; see the server log.")
    finally:
        if leaked:
            chaos.release_leak("Guided demo (cleanup)")
