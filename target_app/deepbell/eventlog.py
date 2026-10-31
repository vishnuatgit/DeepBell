"""The event console shared by every dashboard visitor, mirrored to the server log."""

from . import config, runtime, timeutil


def log_event(level: str, message: str) -> None:
    """Record an event for the dashboard console (shared by every visitor) and the server log."""
    with runtime.lock:
        runtime.event_seq += 1
        runtime.events.append(
            {
                "seq": runtime.event_seq,
                "time": timeutil.iso(timeutil.utcnow()),
                "level": level,
                "message": message,
            }
        )
    log = {"warn": config.logger.warning, "error": config.logger.error}.get(
        level, config.logger.info
    )
    log(message)


def recent_events(after: int) -> list[dict]:
    with runtime.lock:
        return [e for e in runtime.events if e["seq"] > after]
