"""Background threads: the snapshot manager and the watchdog that samples telemetry."""

import os
import time

from . import chaos, config, incidents, llm, snapshots, telemetry


def snapshot_manager_worker():
    config.logger.info("Snapshot Manager started.")
    while True:
        try:
            snapshots.take_snapshot()
        except Exception as e:
            config.logger.error(f"Snapshot Manager Error: {e}")
        time.sleep(config.SNAPSHOT_INTERVAL_SEC)


def watchdog_worker():
    config.logger.info("Watchdog Telemetry & Anomaly Detector started.")
    import psutil
    from sklearn.ensemble import IsolationForest  # noqa: F401  (load before measuring memory)

    if llm.get_api_key():
        llm.load_llm(relearn=False)
    process = psutil.Process(os.getpid())
    # First CPU read to establish baseline
    process.cpu_percent(interval=None)
    time.sleep(1)
    if config.SEED_DEMO_DATA:
        telemetry.seed_warmup_metrics(
            process.cpu_percent(interval=None) / telemetry.CPU_CAPACITY,
            process.memory_info().rss / config.MB,
        )
    ticks = 0
    while True:
        time.sleep(config.SAMPLE_INTERVAL_SEC)
        try:
            cpu = process.cpu_percent(interval=None) / telemetry.CPU_CAPACITY
            telemetry.record_sample(cpu, process.memory_info().rss / config.MB)
            incidents.refresh_db_state()
            chaos.release_stale_leak()
            ticks += 1
            if ticks % 150 == 0:
                telemetry.prune_metrics()
        except Exception as e:
            config.logger.error(f"Watchdog Worker Error: {e}")
