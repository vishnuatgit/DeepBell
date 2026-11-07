"""Glue between the sampler and the alert engine: build the metric values, evaluate the rules,
and send every state change to the console, the log, the metrics and the notifier.
"""

from . import alert_rules, config, eventlog, metrics, notifications, runtime

EVENT_LEVELS = {"pending": "warn", "firing": "error", "resolved": "success"}


def load_rules() -> None:
    """Read the rules file into the engine (at startup). Problems are logged, never fatal."""
    rules, errors = alert_rules.load_rules()
    runtime.alert_engine = alert_rules.AlertEngine(rules, errors)
    for error in errors:
        config.logger.warning(f"Alert rules: {error}")
    config.logger.info(f"Loaded {len(rules)} alert rule(s).")


def metric_values(cpu: float, ram: float, kinds: list[str]) -> dict[str, float]:
    """The numbers a rule can watch, for the sample just taken."""
    with runtime.lock:
        baseline = runtime.detector.baseline
        db_error = runtime.db_error
    return {
        "cpu_percent": cpu,
        "memory_mb": ram,
        # Zero until a baseline exists, so the first samples cannot fire a false alert.
        "memory_over_baseline_mb": round(ram - baseline["ram"], 1) if baseline else 0.0,
        "anomaly_score": 1.0 if kinds else 0.0,
        "database_error": 1.0 if db_error else 0.0,
    }


def evaluate_sample(cpu: float, ram: float, kinds: list[str], now: float) -> list[dict]:
    """Evaluate every rule for one sample. Never raises: alerting must not stop sampling."""
    try:
        events = runtime.alert_engine.evaluate(metric_values(cpu, ram, kinds), now)
        for event in events:
            metrics.ALERT_TRANSITIONS.labels(event["state"], event["severity"]).inc()
            eventlog.log_event(EVENT_LEVELS[event["state"]], f"Alert: {event['text']}")
            notifications.notifier.notify(event)
        return events
    except Exception as e:
        config.logger.error(f"Alert evaluation failed: {e}")
        return []
