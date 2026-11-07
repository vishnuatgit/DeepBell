# Roadmap: what gets built next, and what does not

DeepBell is a small lab, and the free hosting tier is a hard limit. This page records the decision on a nine-item wishlist so the reasoning is not lost.

## Decision: build three things, in this order

1. **Prometheus and Grafana.** A `/metrics` endpoint plus a local Prometheus and Grafana stack with a ready-made dashboard. (Walkthrough: [01-prometheus-grafana.md](walkthroughs/01-prometheus-grafana.md).)
2. **Configurable alert rules with notifications.** Rules in a versioned file (metric, condition, threshold, duration, severity), a pending, firing, resolved lifecycle, an Alerts view and optional Slack or Discord style webhooks. (Walkthrough: [02-alert-rules.md](walkthroughs/02-alert-rules.md).)
3. **Incident timeline with correlation and an evidence-based likely cause.** One ordered timeline per incident that merges faults, alerts, reports and restores, and a labelled heuristic that says what most probably triggered the incident and how sure it is. (Walkthrough: [03-incident-correlation.md](walkthroughs/03-incident-correlation.md).)

## What was dropped or deferred, and why

| Wishlist item | Decision | Why |
|---|---|---|
| OpenTelemetry and distributed tracing | Dropped | A trace follows one request across several services. DeepBell is a single process, so a trace would be one span: nothing to show or learn. It becomes worthwhile only once there are several services. |
| Monitoring the owner's other free Render demos | Deferred | Watching another service means pinging it regularly, and a request every few seconds keeps a free Render service awake. Render's free plan gives 750 instance-hours a month shared by all free services in a workspace, so always-awake demos would use that allowance up and the demos would be suspended until the month ends. |
| Rollback tracking | Mostly exists | Every restore is already written on the incident timeline, in the event console and in the snapshot list. Feature 1 adds a restore counter, and feature 3 puts restores on the correlated timeline. |
| Recovery verification | Mostly exists | After a rollback the database is checked again, and the incident only closes when the check passes (or CPU and memory stay inside the baseline for three samples). Time to recover is measured. |
| Grafana dashboards | Covered | Feature 1 is this item. |
| Chaos tests | Exist, extended where needed | The suite already injects each fault. Each new feature adds an end-to-end test that injects a real fault and follows it through the new code. |

## Not planned

Anything that needs a paid service, a database server or a second always-on process. The live demo must keep working with no new required environment variables.
