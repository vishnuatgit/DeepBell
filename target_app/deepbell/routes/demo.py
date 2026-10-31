"""The guided demo: the current run and starting a new one."""

import threading

from fastapi import APIRouter, HTTPException

from .. import chaos, config, demo, eventlog, runtime

router = APIRouter()


@router.get("/api/demo", tags=["guided demo"])
def get_demo():
    """The current or last guided-demo run, and the cooldown before the next one."""
    return {
        "run": demo.demo_state(),
        "cooldown": chaos.cooldown_left("demo", config.DEMO_COOLDOWN_SEC),
    }


@router.post("/api/demo/start", tags=["guided demo"])
def post_demo_start():
    """Start the guided demo, unless a run is in progress or has only just finished."""
    with runtime.lock:
        if runtime.demo and runtime.demo["state"] == "running":
            raise HTTPException(
                status_code=409,
                detail=(
                    "A guided demo is already running on this shared server. Its progress is "
                    "shown on the overview."
                ),
            )
        wait = chaos.cooldown_left("demo", config.DEMO_COOLDOWN_SEC)
        if wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    "The guided demo has just finished. This server is shared, so it can run "
                    f"again in {wait} s."
                ),
                headers={"Retry-After": str(wait)},
            )
        runtime.demo_seq += 1
        runtime.demo = demo.new_demo_run(runtime.demo_seq)
    eventlog.log_event("info", "Guided demo started.")
    threading.Thread(target=demo.run_demo_scenario, daemon=True).start()
    return demo.demo_state()
