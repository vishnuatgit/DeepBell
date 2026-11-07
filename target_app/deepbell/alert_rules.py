"""Alert rules and the engine that evaluates them.

A rule watches one metric: `metric condition threshold` for at least `for_sec` seconds. Its
state moves ok -> pending (condition true, not yet long enough) -> firing (true for the whole
duration) -> resolved (false again). A pending rule whose condition turns false goes back to ok
without ever firing, which keeps a one-sample blip from raising an alert.

This module has no side effects (no logging, notifications or metrics), so it can be tested
with a fake clock: `evaluate` takes the time as an argument.
"""

import json
import math
import os
import threading
from collections import deque
from dataclasses import dataclass

from . import config, timeutil

# Metric name -> (label, unit) of the values a rule can watch.
METRICS = {
    "cpu_percent": ("CPU", "%"),
    "memory_mb": ("Memory", "MB"),
    "memory_over_baseline_mb": ("Memory above baseline", "MB"),
    "anomaly_score": ("Anomaly score", ""),
    "database_error": ("Database error", ""),
}
# Which incident kinds an alert on this metric belongs to (used to put alert events on the
# timeline of the right incident).
INCIDENT_KINDS = {
    "cpu_percent": ("cpu_spike",),
    "memory_mb": ("memory_leak",),
    "memory_over_baseline_mb": ("memory_leak",),
    "anomaly_score": ("cpu_spike", "memory_leak"),
    "database_error": ("db_corruption",),
}
CONDITIONS = (">", "<")
SEVERITIES = ("info", "warning", "critical")
MAX_RULES = 50


@dataclass(frozen=True)
class Rule:
    name: str
    metric: str
    condition: str
    threshold: float
    for_sec: float = 0
    severity: str = "warning"
    description: str = ""

    def breached(self, value: float) -> bool:
        return value > self.threshold if self.condition == ">" else value < self.threshold

    def text(self) -> str:
        label, unit = METRICS[self.metric]
        text = f"{label} {self.condition} {self.threshold:g}{' ' + unit if unit else ''}"
        return f"{text} for {self.for_sec:g} s" if self.for_sec else text


def is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def parse_rule(raw) -> Rule:
    """Build one Rule from its JSON object, or raise ValueError saying what is wrong.
    Missing optional fields get defaults: condition ">", for_sec 0, severity "warning"."""
    if not isinstance(raw, dict):
        raise ValueError("a rule must be a JSON object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("name must be a text of 1 to 60 characters")
    metric = raw.get("metric")
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}")
    condition = raw.get("condition", ">")
    if condition not in CONDITIONS:
        raise ValueError("condition must be > or <")
    threshold = raw.get("threshold")
    if not is_number(threshold):
        raise ValueError("threshold must be a number")
    for_sec = raw.get("for_sec", 0)
    if not is_number(for_sec) or not 0 <= for_sec <= 3600:
        raise ValueError("for_sec must be a number from 0 to 3600")
    severity = raw.get("severity", "warning")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be text")
    return Rule(name.strip(), metric, condition, threshold, for_sec, severity, description)


def parse_rules(raw) -> tuple[list[Rule], list[str]]:
    """Validate a loaded rules document. Bad rules are skipped and reported, not fatal."""
    items = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ['The file must hold a "rules" list.']
    rules, errors = [], []
    for position, item in enumerate(items, start=1):
        label = item.get("name", f"#{position}") if isinstance(item, dict) else f"#{position}"
        try:
            rule = parse_rule(item)
        except ValueError as e:
            errors.append(f"Rule {label} skipped: {e}.")
            continue
        if any(rule.name == other.name for other in rules):
            errors.append(f"Rule {rule.name} skipped: the name is used twice.")
        elif len(rules) >= MAX_RULES:
            errors.append(f"Rule {rule.name} skipped: at most {MAX_RULES} rules are loaded.")
        else:
            rules.append(rule)
    return rules, errors


def load_rules(path: str | None = None) -> tuple[list[Rule], list[str]]:
    """Read and validate the rules file. Returns (rules, problems)."""
    path = path or config.ALERT_RULES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return [], [f"Rules file not found: {os.path.basename(path)}."]
    except (OSError, ValueError) as e:
        return [], [f"Could not read {os.path.basename(path)}: {e}."]
    return parse_rules(raw)


class AlertEngine:
    """Holds the rules, their states and the recent history of state changes."""

    def __init__(self, rules: list[Rule] | None = None, errors: list[str] | None = None):
        self.rules = list(rules or [])
        self.errors = list(errors or [])
        self.lock = threading.Lock()
        self.history: deque = deque(maxlen=config.ALERT_HISTORY_KEEP)
        self.states = {
            rule.name: {
                "state": "ok",
                "value": None,
                "since": None,
                "since_at": None,
                "changed_at": None,
            }
            for rule in self.rules
        }

    def evaluate(self, values: dict[str, float], now: float) -> list[dict]:
        """Check every rule against these metric values. `now` is a time in seconds from any
        steady clock. Returns the state changes (pending, firing, resolved) this call caused."""
        events = []
        with self.lock:
            for rule in self.rules:
                st = self.states[rule.name]
                value = values.get(rule.metric)
                if value is None:
                    continue
                st["value"] = value
                if rule.breached(value):
                    new = self.advance(rule, st, now)
                elif st["state"] == "firing":
                    new = "resolved"
                elif st["state"] == "pending":
                    new = "ok"  # it never lasted long enough to fire
                else:
                    new = st["state"]
                if new != st["state"]:
                    event = self.change(rule, st, new, value, now)
                    if event:
                        events.append(event)
        return events

    @staticmethod
    def advance(rule: Rule, st: dict, now: float) -> str:
        """The next state while the rule's condition is true."""
        if st["state"] in ("ok", "resolved"):
            st["since"], st["since_at"] = now, timeutil.iso(timeutil.utcnow())
            return "pending" if rule.for_sec > 0 else "firing"
        if st["state"] == "pending" and now - st["since"] >= rule.for_sec:
            return "firing"
        return st["state"]

    def change(self, rule: Rule, st: dict, new: str, value: float, now: float) -> dict | None:
        """Move a rule to its new state. Returns the history event (None when it just cleared)."""
        previous_since = st["since"]
        st["state"] = new
        st["changed_at"] = timeutil.iso(timeutil.utcnow())
        if new == "ok":
            st["since"] = st["since_at"] = None
            return None
        label = METRICS[rule.metric][0]
        if new == "resolved":
            lasted = max(0, round(now - previous_since)) if previous_since is not None else 0
            text = f"{rule.name} resolved after {lasted} s ({label} {value:g})."
        else:
            text = f"{rule.name} is {new}: {rule.text()} (now {value:g})."
        event = {
            "at": timeutil.iso(timeutil.utcnow()),
            "rule": rule.name,
            "metric": rule.metric,
            "state": new,
            "severity": rule.severity,
            "value": value,
            "text": text,
        }
        self.history.append(event)
        return event

    def events_since(self, at: str, kind: str) -> list[dict]:
        """State changes at or after `at` (ISO time) that belong to incidents of this kind."""
        with self.lock:
            return [
                e for e in self.history if e["at"] >= at and kind in INCIDENT_KINDS[e["metric"]]
            ]

    def counts(self) -> dict:
        with self.lock:
            states = [s["state"] for s in self.states.values()]
        return {"firing": states.count("firing"), "pending": states.count("pending")}

    def snapshot(self) -> dict:
        """Everything the Alerts view shows: rules with their state, history (newest first)."""
        with self.lock:
            rules = [
                {
                    "name": rule.name,
                    "description": rule.description,
                    "metric": rule.metric,
                    "condition": rule.text(),
                    "severity": rule.severity,
                    "state": self.states[rule.name]["state"],
                    "value": self.states[rule.name]["value"],
                    "since": self.states[rule.name]["since_at"],
                    "changed_at": self.states[rule.name]["changed_at"],
                }
                for rule in self.rules
            ]
            history = list(self.history)[::-1]
        return {"rules": rules, "history": history, "errors": list(self.errors)}
