"""Alert rules: validation, the pending -> firing -> resolved states, notifications, the API,
and one end-to-end run that injects a real fault."""

import json
import os
import threading
import time

import psutil
import pytest
from test_features import no_reports, sample, wall  # noqa: F401

from deepbell import alert_rules, alerts, config, metrics, notifications, runtime
from deepbell.alert_rules import AlertEngine, Rule

CPU_RULE = Rule("High CPU", "cpu_percent", ">", 80, for_sec=4, severity="critical")


def engine(*rules):
    return AlertEngine(list(rules or [CPU_RULE]))


def step(eng, cpu, now):
    """Evaluate one sample at second `now` and return the state changes."""
    return eng.evaluate({"cpu_percent": cpu}, now)


# --- Loading and validation ---


def test_rule_defaults():
    rule = alert_rules.parse_rule({"name": " Busy ", "metric": "cpu_percent", "threshold": 50})
    assert rule == Rule("Busy", "cpu_percent", ">", 50, for_sec=0, severity="warning")


@pytest.mark.parametrize(
    "raw, problem",
    [
        ({"metric": "cpu_percent", "threshold": 1}, "name"),
        ({"name": "x", "metric": "disk", "threshold": 1}, "metric"),
        ({"name": "x", "metric": "cpu_percent", "threshold": "high"}, "threshold"),
        ({"name": "x", "metric": "cpu_percent", "threshold": True}, "threshold"),
        ({"name": "x", "metric": "cpu_percent", "threshold": 1, "condition": "="}, "condition"),
        ({"name": "x", "metric": "cpu_percent", "threshold": 1, "for_sec": -1}, "for_sec"),
        ({"name": "x", "metric": "cpu_percent", "threshold": 1, "severity": "meh"}, "severity"),
        ("not an object", "object"),
    ],
)
def test_invalid_rules_are_rejected_with_a_reason(raw, problem):
    with pytest.raises(ValueError, match=problem):
        alert_rules.parse_rule(raw)


def test_bad_rules_are_skipped_and_reported_not_fatal():
    good = {"name": "A", "metric": "cpu_percent", "threshold": 1}
    rules, errors = alert_rules.parse_rules(
        {"rules": [good, good, {"name": "B", "metric": "nope", "threshold": 1}]}
    )
    assert [r.name for r in rules] == ["A"]
    assert len(errors) == 2
    assert "used twice" in errors[0] and "Rule B skipped" in errors[1]
    assert alert_rules.parse_rules({"oops": 1})[0] == []


def test_load_rules_handles_missing_and_broken_files(tmp_path):
    rules, errors = alert_rules.load_rules(str(tmp_path / "missing.json"))
    assert rules == [] and "not found" in errors[0]
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    rules, errors = alert_rules.load_rules(str(broken))
    assert rules == [] and "Could not read" in errors[0]


def test_the_shipped_rules_file_is_valid_and_covers_every_fault():
    rules, errors = alert_rules.load_rules()
    assert errors == []
    assert {r.metric for r in rules} >= {
        "cpu_percent",
        "memory_over_baseline_mb",
        "anomaly_score",
        "database_error",
    }
    with open(config.ALERT_RULES_FILE, encoding="utf-8") as f:
        assert json.load(f)["rules"]  # a versioned file, not code


# --- States, with a fake clock ---


def test_rule_goes_pending_then_firing_then_resolved(wall):  # noqa: F811
    eng = engine()
    assert step(eng, 10, 0) == []
    assert [e["state"] for e in step(eng, 95, 2)] == ["pending"]
    assert step(eng, 96, 4) == []  # still pending: only 2 s so far
    firing = step(eng, 97, 6)  # 4 s after it began
    assert [e["state"] for e in firing] == ["firing"]
    assert firing[0]["severity"] == "critical" and "High CPU is firing" in firing[0]["text"]
    assert step(eng, 98, 8) == []  # already firing: no repeat
    resolved = step(eng, 5, 10)
    assert [e["state"] for e in resolved] == ["resolved"]
    assert "resolved after 8 s" in resolved[0]["text"]
    assert eng.snapshot()["rules"][0]["state"] == "resolved"
    assert [e["state"] for e in eng.snapshot()["history"]] == ["resolved", "firing", "pending"]


def test_a_blip_shorter_than_the_duration_never_fires(wall):  # noqa: F811
    eng = engine()
    assert [e["state"] for e in step(eng, 95, 0)] == ["pending"]
    assert step(eng, 5, 2) == []  # back to normal: cancelled quietly
    assert eng.snapshot()["rules"][0]["state"] == "ok"
    assert eng.counts() == {"firing": 0, "pending": 0}
    assert [e["state"] for e in step(eng, 95, 4)] == ["pending"]  # a new attempt starts over
    assert step(eng, 95, 6) == []  # only 2 s into the new attempt


def test_zero_duration_fires_at_once_and_can_fire_again(wall):  # noqa: F811
    eng = engine(Rule("DB", "cpu_percent", ">", 0, for_sec=0))
    assert [e["state"] for e in step(eng, 1, 0)] == ["firing"]
    assert [e["state"] for e in step(eng, 0, 2)] == ["resolved"]
    assert [e["state"] for e in step(eng, 1, 4)] == ["firing"]
    assert eng.counts()["firing"] == 1


def test_less_than_rules_and_missing_metrics(wall):  # noqa: F811
    eng = engine(Rule("Low", "cpu_percent", "<", 10, for_sec=0))
    assert [e["state"] for e in step(eng, 5, 0)] == ["firing"]
    assert eng.evaluate({}, 2) == []  # a missing metric changes nothing
    assert [e["state"] for e in step(eng, 50, 4)] == ["resolved"]


def test_metric_values_are_zero_without_a_baseline(client):
    values = alerts.metric_values(50.0, 300.0, [])
    assert values["memory_over_baseline_mb"] == 0.0
    assert values["database_error"] == 0.0 and values["anomaly_score"] == 0.0


def test_a_broken_engine_never_stops_sampling(monkeypatch):
    class Broken:
        def evaluate(self, *args):
            raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "alert_engine", Broken())
    assert alerts.evaluate_sample(1.0, 1.0, [], 0.0) == []


# --- Notifications ---

FIRING = {
    "rule": "High CPU",
    "state": "firing",
    "severity": "critical",
    "text": "High CPU is firing.",
}
RESOLVED = {**FIRING, "state": "resolved", "text": "High CPU resolved."}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def webhook(monkeypatch):
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "https://hooks.example.test/secret-token")
    sent = []
    clock = Clock()
    notifier = notifications.Notifier(
        post=lambda url, payload: sent.append((url, payload)), threaded=False, clock=clock
    )
    return notifier, sent, clock


def test_without_a_webhook_alerts_are_only_logged(monkeypatch, caplog):
    monkeypatch.delenv(notifications.WEBHOOK_ENV, raising=False)
    notifier = notifications.Notifier(post=lambda *a: pytest.fail("must not post"))
    with caplog.at_level("INFO", logger="DeepBellCockpit"):
        assert notifier.notify(FIRING) == "logged"
    assert "no webhook configured" in caplog.text and "High CPU is firing" in caplog.text


def test_a_non_http_webhook_url_counts_as_not_configured(monkeypatch):
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "file:///etc/passwd")
    assert notifications.webhook_url() == ""
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "not a url")
    assert notifications.webhook_url() == ""


def test_webhook_gets_slack_and_discord_compatible_json(webhook):
    notifier, sent, _ = webhook
    assert notifier.notify({**FIRING, "state": "pending"}) == "ignored"  # pending is not sent
    assert notifier.notify(FIRING) == "queued"
    url, payload = sent[0]
    assert url == "https://hooks.example.test/secret-token"
    assert payload["text"] == payload["content"]
    assert payload["text"] == "[DeepBell] CRITICAL: High CPU is firing."


def test_a_flapping_rule_cannot_spam_the_webhook(webhook):
    notifier, sent, clock = webhook
    assert notifier.notify(FIRING) == "queued"
    assert notifier.notify(RESOLVED) == "queued"
    clock.now += 10
    assert notifier.notify(FIRING) == "suppressed"  # same rule within a minute
    assert notifier.notify(RESOLVED) == "suppressed"  # its firing was never announced
    clock.now += config.NOTIFY_MIN_INTERVAL_SEC
    assert notifier.notify(FIRING) == "queued"
    assert len(sent) == 3


def test_all_rules_together_are_capped_per_minute(webhook):
    notifier, sent, clock = webhook
    results = [
        notifier.notify({**FIRING, "rule": f"Rule {i}"})
        for i in range(config.NOTIFY_MAX_PER_MIN + 2)
    ]
    assert results.count("queued") == config.NOTIFY_MAX_PER_MIN
    assert results.count("suppressed") == 2
    clock.now += 61
    assert notifier.notify({**FIRING, "rule": "Rule 9"}) == "queued"


def test_a_failing_webhook_is_counted_and_never_raises(monkeypatch, caplog):
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "https://hooks.example.test/secret-token")

    def refuse(url, payload):
        raise OSError("connection refused")

    notifier = notifications.Notifier(post=refuse, threaded=False)
    failed = metrics.REGISTRY.get_sample_value(
        "deepbell_alert_notifications_total", {"result": "failed"}
    )
    notifier.notify(FIRING)
    assert (
        metrics.REGISTRY.get_sample_value(
            "deepbell_alert_notifications_total", {"result": "failed"}
        )
        == failed + 1
    )
    assert "failed" in caplog.text and "secret-token" not in caplog.text


def test_sending_never_blocks_the_sampler(monkeypatch):
    """A slow webhook must not delay notify(): the post happens on the worker thread."""
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "https://hooks.example.test/x")
    release, delivered = threading.Event(), threading.Event()

    def slow_post(url, payload):
        release.wait(5)
        delivered.set()

    notifier = notifications.Notifier(post=slow_post)
    started = time.monotonic()
    assert notifier.notify(FIRING) == "queued"
    assert time.monotonic() - started < 0.5
    assert not delivered.is_set()
    release.set()
    assert delivered.wait(5)


# --- API and the end-to-end run ---


def test_alerts_api_lists_rules_and_problems(client, monkeypatch, tmp_path):
    data = client.get("/api/alerts").json()
    assert [r["name"] for r in data["rules"]][:1] == ["High CPU"]
    assert {r["state"] for r in data["rules"]} == {"ok"}
    assert data["history"] == [] and data["errors"] == []
    assert data["webhook_configured"] is False
    assert client.get("/api/status").json()["alerts"] == {"firing": 0, "pending": 0}


def test_a_bad_rules_file_is_reported_by_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    path = tmp_path / "rules.json"
    path.write_text(
        '{"rules": [{"name": "X", "metric": "nope", "threshold": 1}]}', encoding="utf-8"
    )
    monkeypatch.setattr(config, "ALERT_RULES_FILE", str(path))
    with TestClient(main.app) as c:
        data = c.get("/api/alerts").json()
    assert data["rules"] == [] and "Rule X skipped" in data["errors"][0]


def test_end_to_end_memory_leak_fires_alert_notifies_and_resolves(
    client,
    wall,  # noqa: F811
    monkeypatch,
    caplog,
):
    """Real fault through the API -> samples -> alert -> webhook -> incident -> /metrics."""
    monkeypatch.setenv(notifications.WEBHOOK_ENV, "https://hooks.example.test/token")
    sent = []
    monkeypatch.setattr(
        notifications,
        "notifier",
        notifications.Notifier(post=lambda url, payload: sent.append(payload), threaded=False),
    )
    import random

    rng = random.Random(5)
    base = psutil.Process().memory_info().rss / config.MB
    mono = 0.0
    for _ in range(30):
        mono = sample(wall, mono, rng.uniform(0.5, 6.0), base + rng.uniform(-1.0, 1.0))
    assert client.get("/api/alerts").json()["counts"] == {"firing": 0, "pending": 0}

    assert client.post("/fault/memory_leak", params={"size_mb": 60}).status_code == 200
    for _ in range(3):  # 0 s, 2 s, 4 s above the baseline
        mono = sample(wall, mono, 2.0, psutil.Process().memory_info().rss / config.MB)
    data = client.get("/api/alerts").json()
    by_name = {r["name"]: r for r in data["rules"]}
    assert by_name["Memory above baseline"]["state"] == "firing"
    assert by_name["Watchdog flagging samples"]["state"] == "firing"
    assert by_name["High CPU"]["state"] == "ok"
    assert data["counts"]["firing"] == 2
    assert [e["state"] for e in data["history"] if e["rule"] == "Memory above baseline"] == [
        "firing",
        "pending",
    ]
    assert client.get("/api/status").json()["incidents"][0]["kind"] == "memory_leak"
    assert "deepbell_alerts_firing 2.0" in client.get("/metrics").text
    assert any("Memory above baseline is firing" in p["text"] for p in sent)
    assert any(
        "Alert: Memory above baseline is firing" in e["message"]
        for e in client.get("/api/status", params={"events_after": 0}).json()["events"]
    )

    client.post("/fault/memory_release")
    mono = sample(wall, mono, 2.0, base)
    states = {r["name"]: r["state"] for r in client.get("/api/alerts").json()["rules"]}
    assert states["Memory above baseline"] == "resolved"
    assert any("resolved" in p["text"] for p in sent)
    assert os.path.exists(config.ALERT_RULES_FILE)
