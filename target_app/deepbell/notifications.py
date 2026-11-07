"""Alert notifications: an optional Slack or Discord style webhook, otherwise just the log.

Set DEEPBELL_ALERT_WEBHOOK_URL to turn the webhook on. Sending never blocks the watchdog:
`notify` only puts the message on a small queue, and one background thread posts it.
Two limits keep a flapping alert from spamming a channel:
  * the same rule cannot notify "firing" more than once per NOTIFY_MIN_INTERVAL_SEC;
  * at most NOTIFY_MAX_PER_MIN messages leave in any 60 seconds.
Only "firing" and "resolved" notify (a "resolved" only if its "firing" was sent), not "pending".
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlparse

from . import config, metrics

WEBHOOK_ENV = "DEEPBELL_ALERT_WEBHOOK_URL"
USER_AGENT = "DeepBell-Alerts/1.0"  # Discord rejects the default Python user agent


def webhook_url() -> str:
    """The configured webhook URL, or "" when none is set or it is not an http(s) URL."""
    url = os.getenv(WEBHOOK_ENV, "").strip()
    return url if urlparse(url).scheme in ("http", "https") and urlparse(url).netloc else ""


def message(event: dict) -> str:
    return f"[DeepBell] {event['severity'].upper()}: {event['text']}"


def post_json(url: str, payload: dict) -> None:
    """POST the payload as JSON. Raises on a network error or a non-2xx answer."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=config.WEBHOOK_TIMEOUT_SEC):
        pass


class Notifier:
    def __init__(self, post=post_json, threaded: bool = True, clock=time.monotonic):
        self.post = post
        self.threaded = threaded  # tests turn this off to deliver in the calling thread
        self.clock = clock
        self.queue: queue.Queue = queue.Queue(maxsize=config.NOTIFY_QUEUE_MAX)
        self.sent_at: deque = deque()  # times of recent sends, for the per-minute limit
        self.last_firing: dict[str, float] = {}  # rule -> time its firing was last sent
        self.firing_sent: set[str] = set()  # rules whose current firing notification went out
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None

    def notify(self, event: dict) -> str:
        """Handle one alert event. Returns "logged", "queued", "suppressed" or "ignored"."""
        if event["state"] == "pending":
            return "ignored"
        url = webhook_url()
        if not url:
            config.logger.info(f"Alert (no webhook configured): {message(event)}")
            metrics.ALERT_NOTIFICATIONS.labels("logged").inc()
            return "logged"
        if not self.allow(event):
            config.logger.info(f"Alert notification rate-limited: {message(event)}")
            metrics.ALERT_NOTIFICATIONS.labels("suppressed").inc()
            return "suppressed"
        payload = {"text": message(event), "content": message(event)}  # Slack, Discord
        if not self.threaded:
            self.deliver(url, payload)
            return "queued"
        try:
            self.queue.put_nowait((url, payload))
        except queue.Full:
            config.logger.warning("Alert notification dropped: the send queue is full.")
            metrics.ALERT_NOTIFICATIONS.labels("suppressed").inc()
            return "suppressed"
        self.start_worker()
        return "queued"

    def allow(self, event: dict) -> bool:
        now = self.clock()
        rule = event["rule"]
        with self.lock:
            if event["state"] == "resolved" and rule not in self.firing_sent:
                return False  # nobody was told it fired, so do not announce that it ended
            if event["state"] == "firing" and now - self.last_firing.get(rule, -1e9) < (
                config.NOTIFY_MIN_INTERVAL_SEC
            ):
                self.firing_sent.discard(rule)
                return False
            while self.sent_at and now - self.sent_at[0] >= 60:
                self.sent_at.popleft()
            if len(self.sent_at) >= config.NOTIFY_MAX_PER_MIN:
                if event["state"] == "firing":
                    self.firing_sent.discard(rule)
                return False
            self.sent_at.append(now)
            if event["state"] == "firing":
                self.last_firing[rule] = now
                self.firing_sent.add(rule)
            else:
                self.firing_sent.discard(rule)
            return True

    def start_worker(self) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.worker = threading.Thread(target=self.run, daemon=True)
            self.worker.start()

    def run(self) -> None:
        while True:
            url, payload = self.queue.get()
            self.deliver(url, payload)

    def deliver(self, url: str, payload: dict) -> None:
        host = urlparse(url).netloc  # never log the whole URL: it contains the secret token
        try:
            self.post(url, payload)
        except (OSError, urllib.error.URLError, ValueError) as e:
            config.logger.warning(f"Alert webhook to {host} failed: {type(e).__name__}.")
            metrics.ALERT_NOTIFICATIONS.labels("failed").inc()
        else:
            metrics.ALERT_NOTIFICATIONS.labels("sent").inc()


notifier = Notifier()
