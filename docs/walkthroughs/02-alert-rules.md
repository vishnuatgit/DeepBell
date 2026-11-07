# 02. Alert rules and notifications

## What it does and why

The watchdog already opens incidents, but an incident is a coarse event. Alert rules are the usual monitoring building block: "tell me when this number is bad for long enough". Each rule is one line of configuration, for example "CPU above 50 % for 2 seconds, severity warning". When a rule fires you see it in the new **Alerts** view, in the event console and the server log, and optionally in a Slack or Discord channel.

The rules live in a versioned file (`target_app/alert_rules.json`), so a change to an alert goes through git review like code does. The shipped rules fire on each Chaos Lab fault, so the feature is visible on the live demo without any setup.

## How it works, end to end

1. **Startup.** `startup.py` calls `alerts.load_rules()`, which reads the JSON file with `alert_rules.load_rules()`. Each rule is validated: known metric, `>` or `<`, a numeric threshold, `for_sec` from 0 to 3600, a severity of info, warning or critical, a unique name. Optional fields have defaults (condition `>`, `for_sec` 0, severity `warning`). A bad rule is skipped and listed under "problems" in the API and the Alerts view; a missing or broken file leaves no rules but never stops the server.
2. **Every sample.** `telemetry.record_sample` finishes its work (store the sample, run the detector, open or close incidents) and then calls `alerts.evaluate_sample`. That builds the numbers a rule can watch: `cpu_percent`, `memory_mb`, `memory_over_baseline_mb`, `anomaly_score` (1 when the watchdog flagged the sample) and `database_error` (1 when the integrity check fails).
3. **The state machine** (`alert_rules.AlertEngine.evaluate`). A rule whose condition is true becomes **pending** and remembers when that began. When it has stayed true for `for_sec` it becomes **firing**. When the condition is false again a firing rule becomes **resolved**; a pending rule just goes back to **ok** (a short blip never fires). With `for_sec` of 0 the rule fires at once. The time is passed in as a number, so tests use a fake clock.
4. **Each state change** is written to the event console and the log, counted in `/metrics` (`deepbell_alert_transitions_total`, `deepbell_alerts_firing`) and handed to the notifier.
5. **Notifications** (`notifications.py`). Without `DEEPBELL_ALERT_WEBHOOK_URL`, the alert is only logged. With it, the message is put on a small queue and one background thread posts it as JSON containing both `text` (Slack) and `content` (Discord). Only firing and resolved are sent, not pending.
6. **Showing it.** `GET /api/alerts` returns the rules with their state, the newest changes, file problems and whether a webhook is set. The Alerts view in the dashboard polls it every 2 s while open; the sidebar shows a red count of firing alerts.

## Files

- `target_app/alert_rules.json`: the rules.
- `deepbell/alert_rules.py`: validation, loading and the state machine (no side effects).
- `deepbell/alerts.py`: glue to the sampler, console, log, metrics and notifier.
- `deepbell/notifications.py`: webhook, queue, rate limits.
- `deepbell/routes/alerts.py`: the API. `static/app.js`, `templates/index.html`, `static/styles.css`: the Alerts view.
- `tests/test_alerts.py`: validation, the state machine with a fake clock, a fake webhook, the API and an end-to-end run with a real memory leak.

## Design choices and trade-offs

- **A JSON file, not YAML.** The standard library reads JSON, so there is no new dependency in the image. The cost is that JSON has no comments, which is why each rule has a `description` field.
- **"for" duration measured in seconds between samples.** Samples arrive every 2 s or a little later, so `for_sec: 2` means two samples in a row. A rule that needs a duration shorter than the sampling interval is effectively "one sample".
- **The shipped thresholds are tuned to the Chaos Lab.** A real CPU spike of 5 s shows up as only two samples at about 60 to 70 % on a laptop, because the busy loop and the watchdog share Python's interpreter lock. So "High CPU" is above 50 % for 2 s, not above 90 %. In production you would pick thresholds from your own data.
- **Never block the sampler.** `notify` only enqueues. A slow or dead webhook delays nothing and a full queue drops the message (counted as suppressed). Evaluation is wrapped so an exception in alerting is logged and the sampler carries on.
- **Rate limits.** The same rule cannot send "firing" more than once a minute, at most 10 messages a minute leave in total, and a "resolved" is sent only if its "firing" was. This stops a flapping rule from flooding a channel. The cost is that a very fast re-fire is not announced.
- **State is in memory.** After a restart rules start at ok and the history is empty. That matches the rest of the app (open incidents are marked interrupted on restart).
- **Secrets.** The webhook URL contains a token. It is only read from an environment variable, is never logged (only the host name is) and is never returned by the API (only "configured" or not).

## Limits

- Rules are read at startup; editing the file needs a restart. There is no editing in the UI.
- Only five metrics can be watched, and a rule watches one metric at a time (no "A and B").
- Notifications are fire-and-forget: a failed post is logged and counted, not retried.
- On the free Render plan the service sleeps without traffic, so nothing can alert while it is asleep.

## Owner setup

Nothing is required. To get notifications, create an incoming webhook in Slack or Discord and set `DEEPBELL_ALERT_WEBHOOK_URL` in the host's environment (for Render: the service's Environment tab). Do not put it in a file that is committed.

## Likely interview questions

1. **Why do rules have a "for" duration?** It stops a one-sample blip from alerting. The rule must stay true for the whole duration, so people are paged for conditions that last, not noise.
2. **Walk me through pending, firing, resolved.** True and young is pending, true for the duration is firing, false after firing is resolved; false while pending silently returns to ok.
3. **How did you test time-dependent logic?** The engine takes the current time as an argument and tests pass a fake clock, so nothing sleeps.
4. **How do you stop notifications from blocking or spamming?** The sampler only enqueues; a worker thread posts with a 5 s timeout. A per-rule interval and a global per-minute cap limit volume, and I count suppressed messages.
5. **Why not alert in Prometheus/Alertmanager instead?** That is the production answer and feature 1 exports the metrics for it. The in-app rules work on the free demo with no extra service, and show the state machine itself.
6. **How is bad configuration handled?** Each rule is validated; bad ones are skipped with a message shown in the UI, and a missing file means no rules, never a crash.
7. **What are the security concerns of a webhook?** The URL is a secret and sending to it is an outbound request. It only comes from the environment, only http or https is accepted, and it is never logged or returned.
8. **What would you change to scale it?** Persist state, run evaluation in one place (not per process), add retries with backoff, group related alerts and support rules on more than one metric.
