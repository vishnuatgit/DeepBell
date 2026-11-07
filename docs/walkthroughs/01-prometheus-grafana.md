# 01. Prometheus metrics and a Grafana dashboard

## What it does and why

DeepBell now exposes its own health as numbers at `/metrics`. Prometheus (a time-series database) reads that page every few seconds and keeps the history; Grafana draws graphs from Prometheus. This is the standard way real services are monitored, so the project now shows that pipeline end to end: the app being watched, the metrics store and the dashboard.

The dashboard UI built into DeepBell only shows roughly the last half hour and only for one process. Prometheus keeps long history and lets you ask questions such as "what was the average time to detect over the last day".

## How it works, end to end

1. `target_app/deepbell/metrics.py` creates the metrics with `prometheus_client`:
   - **Gauges** (a value that goes up and down): `deepbell_cpu_percent`, `deepbell_memory_mb`, `deepbell_anomaly_score` (1 when the latest sample was flagged, else 0) and `deepbell_incidents_open`. They use `set_function`, so the number is read from the live state (`runtime.py`) at the moment Prometheus asks. The 2-second sampler does no extra work.
   - **Counters** (only go up): `deepbell_incidents_total{kind}`, `deepbell_fault_injections_total{kind}`, `deepbell_reports_generated_total{source}` and `deepbell_snapshot_restores_total`.
   - **Histograms** (count how many observations fall in each time bucket): `deepbell_detection_seconds` and `deepbell_recovery_seconds`.
2. The counters and histograms are updated where the event really happens:
   - `incidents.open_incident` counts the incident and records its time to detect.
   - `incidents.close_incident` records the time to recover.
   - `incidents.note_fault` counts an injected fault (the Chaos Lab routes and the guided demo both call it).
   - `rca.generate_rca_report_task` counts a written report (by source: template or Gemini).
   - `snapshots.restore_snapshot` counts a restore (rollbacks and the startup self-heal).
3. `routes/metrics.py` serves `generate_latest(registry)` at `GET /metrics`.
4. `monitoring/` holds the stack: `docker-compose.yml` (DeepBell built from the root Dockerfile, Prometheus, Grafana), `prometheus.yml` (scrape `deepbell:8000/metrics` every 5 s) and the Grafana provisioning files. "Provisioning" means Grafana loads the data source and the dashboard JSON from files at start, so nothing is clicked together by hand.
5. The dashboard (`monitoring/grafana/dashboards/deepbell.json`) shows CPU and memory with red lines where incidents opened and orange lines where faults were injected, the anomaly score, average detection and recovery times, and faults, incidents and reports by kind.

## Run it

```bash
docker compose -f monitoring/docker-compose.yml up --build
```

Grafana: http://localhost:3000 (home page is the dashboard). Prometheus: http://localhost:9090. DeepBell: http://localhost:8000. Inject a CPU spike or memory leak in the Chaos Lab and watch the graphs and annotations. Docker is only needed for this stack; `/metrics` works without it (`curl http://127.0.0.1:8000/metrics`).

## Design choices and trade-offs

- **A private registry** instead of the default one. The output holds only DeepBell's metrics and tests can read them without interference from other collectors. The cost is that Python process metrics (garbage collection and so on) are not included.
- **Gauges read live state when scraped.** Simple and cheap, but a value between two scrapes is invisible: a 2-second CPU spike can fall between scrapes, which is why the scrape interval is 5 s and the anomaly score and incident counters exist.
- **Labels are a small fixed set** (`kind`, `source`). Labels with unbounded values (a user name, an incident id) would create a new time series each time and overload Prometheus. The series are created at start so graphs show 0 instead of "no data".
- **Counters are cumulative since the process started.** After a restart they begin at 0; Prometheus handles that, and the dashboard uses `increase()` over the chosen time range.
- **`/metrics` is public on the live demo.** It contains only aggregate numbers about the demo itself, no secrets. The Prometheus and Grafana stack is not deployed: a free Render service cannot run them, so they are local only.

## Limits

- The stack needs Docker and is not part of the live demo. The compose files were checked structurally by tests, not run, on the development machine.
- History lives in Prometheus's container storage and is lost when the container is removed.
- Grafana is open without login in the compose file. That is fine on your own machine and not for anything shared.

## Likely interview questions

1. **Why Prometheus pulls instead of the app pushing?** Pull makes the app simple (it only serves a page), lets Prometheus notice that a target is down, and one place controls the scrape rate.
2. **Gauge vs counter vs histogram?** A gauge is a current value (CPU). A counter only increases (incidents opened); you graph its rate or `increase()`. A histogram counts observations per bucket so you can compute averages and percentiles (time to detect).
3. **Why are the gauges functions instead of being set in the sampler?** The value is read when scraped, so there is no extra code in the hot path and no stale value if the sampler is slow.
4. **What is label cardinality and why does it matter?** Every distinct label combination is a separate time series stored in memory. Using an incident id as a label would grow without limit.
5. **How would you alert from Prometheus instead of inside the app?** Write an alerting rule such as `deepbell_cpu_percent > 80` for 1 minute and route it with Alertmanager. DeepBell's own alert rules (feature 2) do the same inside the app so the live demo needs no extra service.
6. **Why `increase()` in the dashboard and not the raw counter?** The raw counter is a running total since start; `increase()` shows what happened inside the selected time range and survives restarts.
7. **What does provisioning give you?** The dashboard and data source are in git, reviewed and reproducible; nobody has to rebuild them by clicking.
8. **How did you test it without Prometheus?** Tests read the registry and the `/metrics` text directly, drive a real fault through the API, and check the dashboard's queries only use metric names that exist.
