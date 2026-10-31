"""Telemetry samples: CPU capacity, scoring each sample, the warm-up seed and pruning."""

import os
import random
import time
from datetime import datetime, timedelta

from . import config, db, eventlog, incidents, runtime, timeutil


def read_cpu_quota(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """CPU cores allowed by the container's cgroup quota, or None when there is no quota."""
    try:  # cgroup v2: "<quota> <period>" or "max <period>"
        with open(os.path.join(cgroup_root, "cpu.max"), encoding="utf-8") as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"), encoding="utf-8") as f:
            quota = int(f.read())
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"), encoding="utf-8") as f:
            period = int(f.read())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def cpu_capacity() -> float:
    """Cores this process can use, capped at one (all a single busy Python thread can use).

    psutil reports CPU as a percentage of one core. In a container limited to a fraction of a
    core (0.1 on Render's free plan) a busy loop would show as ~10 %, so samples are scaled by
    this capacity to read as a percentage of the CPU actually available.
    """
    override = os.getenv("DEEPBELL_CPU_CORES", "").strip()
    try:
        cores = float(override) if override else read_cpu_quota()
    except ValueError:
        cores = None
    if not cores or cores <= 0:
        return 1.0
    return min(cores, 1.0)


CPU_CAPACITY = cpu_capacity()


def sample_status(kinds: list[str]) -> str:
    if runtime.detector.baseline is None:
        return config.LEARNING_STATUS
    if not kinds:
        return "Normal"
    return f"Anomalous ({', '.join(config.SAMPLE_LABELS[k] for k in kinds)})"


def flagged_text(kind: str, cpu: float, ram: float) -> str:
    """Timeline text for the first anomalous sample of a streak (called with _lock held)."""
    base = runtime.detector.baseline
    if kind == "cpu_spike":
        return f"First anomalous sample: CPU {cpu}% (alert above {base['cpu'] + config.CPU_MIN_DELTA:.1f}%)."
    return f"First anomalous sample: memory {ram} MB (alert above {base['ram'] + config.RAM_MIN_DELTA_MB:.0f} MB)."


def record_sample(cpu: float, ram: float, now: float | None = None) -> list[str]:
    """Store one telemetry sample, score it and open or close incidents. Returns its kinds."""
    cpu, ram = round(cpu, 1), round(ram, 1)
    with runtime.lock:
        kinds, opened, closed = runtime.detector.observe(
            cpu, ram, time.monotonic() if now is None else now
        )
        status = sample_status(kinds)
        runtime.last_cpu, runtime.last_ram = cpu, ram
        runtime.last_sample_at = timeutil.utcnow()
        runtime.latest_anomaly_score = 1.0 if kinds else 0.0
        # Remember where each anomalous streak began: that is the incident's start when no
        # Chaos Lab fault explains it.
        for kind in config.DETECTED_KINDS:
            if kind not in kinds:
                runtime.first_flagged.pop(kind, None)
            elif runtime.detector.streak[kind] == 1:
                runtime.first_flagged[kind] = (
                    timeutil.iso(timeutil.utcnow()),
                    flagged_text(kind, cpu, ram),
                )
    conn = db.get_metrics_db()
    try:
        conn.execute(
            "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (timeutil.iso(timeutil.utcnow()), os.getpid(), cpu, ram, status),
        )
        conn.commit()
    finally:
        conn.close()
    if kinds:
        config.logger.warning(f"Watchdog anomaly: CPU {cpu}%, RAM {ram} MB ({status}).")
    for kind in opened:
        base = runtime.detector.baseline
        detail = (
            f"CPU {cpu}% (baseline {base['cpu']}%), {config.OPEN_AFTER} anomalous samples in a row."
            if kind == "cpu_spike"
            else f"Memory {ram} MB (baseline {base['ram']} MB), "
            f"{config.OPEN_AFTER} anomalous samples in a row."
        )
        incidents.open_incident(kind, detail)
    for kind in closed:
        incidents.close_incident(kind)
    return kinds


def prune_metrics() -> None:
    conn = db.get_metrics_db()
    try:
        conn.execute(
            "DELETE FROM metrics WHERE id <= (SELECT max(id) FROM metrics) - ?",
            (config.METRICS_KEEP,),
        )
        conn.commit()
    finally:
        conn.close()


def seed_warmup_metrics(cpu_level: float, ram_mb: float, now: datetime | None = None) -> int:
    """Insert WARMUP_POINTS synthetic samples around the first live reading and hand them to
    the detector, so a fresh instance can score samples at once instead of after ~30 s of
    learning. They are stored with the status WARMUP_STATUS, and the model replaces them with
    live samples after the first minute. Skipped if live samples from the last 5 minutes exist.
    """
    now = now or timeutil.utcnow()
    conn = db.get_metrics_db()
    try:
        row = conn.execute("SELECT timestamp FROM metrics ORDER BY id DESC LIMIT 1").fetchone()
        last = timeutil.parse_utc(row[0]) if row else None
        if last and now - last < timedelta(minutes=5):
            return 0
        rng = random.Random(42)
        cpu_level = min(max(cpu_level, 1.0), 10.0)
        rows = []
        for i in range(config.WARMUP_POINTS, 0, -1):
            cpu = round(max(0.0, cpu_level + rng.uniform(-1.0, 1.0)), 1)
            ram = round(ram_mb + rng.uniform(-0.5, 0.5), 1)
            ts = timeutil.iso(now - timedelta(seconds=i * config.SAMPLE_INTERVAL_SEC))
            rows.append((ts, 0, cpu, ram, config.WARMUP_STATUS))
        conn.executemany(
            "INSERT INTO metrics (timestamp, pid, cpu_percent, memory_mb, anomaly_status) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    with runtime.lock:
        runtime.detector.add_warmup([(r[2], r[3]) for r in rows])
    eventlog.log_event(
        "info",
        f"Seeded {len(rows)} synthetic warm-up samples so the watchdog starts at once; "
        "live samples replace them within about a minute.",
    )
    return len(rows)
