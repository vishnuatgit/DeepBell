"""Startup: check and migrate the databases, seed demo data and start the workers."""

import threading
import uuid
from datetime import datetime, timedelta

from . import (
    alerts,
    config,
    db,
    eventlog,
    incident_store,
    incidents,
    reports,
    runtime,
    snapshots,
    telemetry,
    timeutil,
    workers,
)

SAMPLE_AMOUNTS = [49.99, 120.0, 15.5, 310.25, 72.4]
EXAMPLE_CPU = [1.4, 0.9, 1.6, 1.1, 1.3, 0.8, 1.5, 1.2, 97.6, 99.1, 64.2, 1.7, 1.1, 1.3, 0.9]


def seed_demo_data(now: datetime | None = None) -> None:
    """Give a fresh instance something to show: a few transactions and an example report."""
    now = now or timeutil.utcnow()
    with db.app_db() as conn:
        if conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0:
            for i, amount in enumerate(SAMPLE_AMOUNTS):
                ts = timeutil.iso(now - timedelta(minutes=5 * (len(SAMPLE_AMOUNTS) - i)))
                conn.execute(
                    "INSERT INTO transactions (id, amount, timestamp) VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), amount, ts),
                )
            conn.commit()
            config.logger.info(f"Seeded {len(SAMPLE_AMOUNTS)} sample transactions.")
    if not reports.list_reports():
        start = now - timedelta(seconds=2 * len(EXAMPLE_CPU))
        rows = [
            {
                "timestamp": timeutil.iso(start + timedelta(seconds=2 * i)),
                "cpu": cpu,
                "ram": 128.0 + (i % 3) * 0.2,
                "status": "Anomalous (CPU Spike)" if cpu > 50 else "Normal",
            }
            for i, cpu in enumerate(EXAMPLE_CPU)
        ]
        note = (
            "Example report, written at startup from synthetic telemetry so a fresh instance "
            "has something to show. Inject a fault in the Chaos Lab to get a real one."
        )
        content = reports.build_template_report(
            rows, timeutil.iso(now), note, config.INCIDENT_LABELS["cpu_spike"]
        )
        reports.write_report(content, now - timedelta(seconds=1))


def startup_event():
    runtime.started_at = timeutil.utcnow()
    alerts.load_rules()
    # Verify (and if needed restore) the database before touching its schema:
    # init_db() raises on a corrupted file, which would crash every restart.
    snapshots.verify_and_heal_database()
    db.init_db()
    db.init_metrics_db()
    interrupted = incident_store.interrupt_open_incidents()
    if interrupted:
        config.logger.info(
            f"Marked {interrupted} incident(s) left open by the last run as interrupted."
        )
    if config.SEED_DEMO_DATA:
        seed_demo_data()
    incidents.refresh_db_state()
    eventlog.log_event(
        "info",
        "Cockpit started"
        + (" in public demo mode." if config.PUBLIC_DEMO else ".")
        + f" CPU is shown as a share of {telemetry.CPU_CAPACITY:g} core(s).",
    )

    if not config.WORKERS_ENABLED:
        config.logger.info("Background workers disabled (DEEPBELL_DISABLE_WORKERS).")
        return
    threading.Thread(target=workers.snapshot_manager_worker, daemon=True).start()
    threading.Thread(target=workers.watchdog_worker, daemon=True).start()
    config.logger.info("All SRE Cockpit background threads initialized.")
