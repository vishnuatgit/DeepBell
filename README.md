# DeepBell

**Catches what breaks in a running app, finds the likely cause and writes the incident report.** A mini SRE lab in FastAPI: it detects its own CPU and memory anomalies, explains each incident and restores from snapshots.

Monitoring usually stops at an alert. DeepBell practises the steps after it: **detect → report → recover**. A background thread samples the service's own CPU and memory every 2 s, a baseline check plus an Isolation Forest flags anomalies and opens de-duplicated incidents, each incident gets a Markdown report (Google Gemini through LangChain when a key is set, a built-in template otherwise), and rolling SQLite snapshots give one-click rollback. A capped Chaos Lab lets you break it on purpose.

[![CI](https://github.com/vishnuatgit/DeepBell/actions/workflows/ci.yml/badge.svg)](https://github.com/vishnuatgit/DeepBell/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Live demo

**[deepbell.onrender.com](https://deepbell.onrender.com)** (deployed from [`render.yaml`](render.yaml), see [Deploy on Render](#deploy-on-render)). Free hosting: the first visit can take about a minute to wake it up. Press **Run the guided demo** to watch it leak memory, detect it, write a report and recover.

The demo is one shared instance on Render's free plan: everyone sees the same telemetry and incidents, and the first visit after 15 quiet minutes takes about a minute while the service wakes up.

![DeepBell's live console during the guided demo: a memory-leak incident is open, the How it works pipeline has lit up Sample, Detect and Report and shows that the demo releases the memory next, and the guided demo is on step 5 of 8](docs/screenshots/cover.png)

## Features

- **Live operations console.** The overview opens on a status banner (healthy, anomalous sample, incident open, connection lost), key numbers from real data (uptime, open incidents, median time to detect and to recover, with a bar per incident that links to it), live CPU and memory charts on a real time axis with the learned baseline, the alert line, flagged samples, shaded incident spans and visible gaps where no sample was taken, a 30-minute status strip, the latest report beside the recent incidents, and the server's event log. The header, the chart headings and the charts always show the same sample.
- **How it works, live.** A four-step pipeline (Sample, Detect, Report, Recover) lights up from the server's own state while an incident moves through it: "CPU spike injected 2 s ago", "1 of 2 anomalous samples in a row", "Report linked to incident #5", "Release the 60 MB of leaked memory to recover". It works for the guided demo and for any fault injected by hand, including one injected right after another incident resolved.
- **Guided demo.** One button runs a narrated scenario on the server: leak 60 MB, watch the watchdog open an incident and write its report, release the memory, watch the incident resolve, then corrupt the database and roll it back to a snapshot. A progress panel shows each step as it happens. It runs one scenario at a time, waits out shared cooldowns, and says why when another visitor's fault is in the way.
- **Self-monitoring watchdog.** A background thread samples the process's own CPU and resident memory every 2 s with `psutil` and scores each sample against a learned baseline (see [Detection logic](#detection-logic)).
- **Incidents and timelines.** Every incident records when it started (the injected fault, or its first anomalous sample), when it was detected and when it resolved, plus the reports and recovery actions in between. The Incidents view lists them with filters and shows each one's time to detect and time to recover, a lifecycle bar drawn to scale and its timeline. Times are computed from the server's own timestamps and labelled as demo measurements.
- **Likely cause.** Each incident detail shows one merged timeline (fault injected, first anomalous sample, alerts going pending, firing and resolved, incident opened, report written, snapshot restored, incident closed, each with its offset from the start) and a "likely cause" panel: the most probable trigger, the evidence, what was checked and not found, and a confidence level (high, medium, low or unknown) from a few explainable rules. It is labelled a heuristic: a fault of the matching kind injected within 10 s of the first anomalous sample gives high confidence, and with no recorded fault the answer is "unknown". It is also in the Markdown report. See [docs/walkthroughs/03-incident-correlation.md](docs/walkthroughs/03-incident-correlation.md).
- **Incident reports.** Each incident gets a Markdown report: a deterministic built-in template by default, or Google Gemini when `GOOGLE_API_KEY` is set (with the template as fallback). Gemini is given the incident's detail, timeline and likely cause as well as the telemetry, is told to use only that data and state its confidence, and its report opens with an AI-written label and ends with the evidence it was given. The template describes only the incident's own fault: a database report never borrows a memory leak's samples. Every report is labelled with what wrote it (Gemini, template, template fallback, or the example written at startup) and downloads as `.md` with the incident's timeline and measured times appended.
- **Alert rules.** Rules in [`target_app/alert_rules.json`](target_app/alert_rules.json) (metric, `>` or `<`, threshold, for-duration, severity) are checked on every sample and move through pending, firing and resolved. The shipped rules fire on each Chaos Lab fault. The Alerts view shows each rule's state and the recent changes; `GET /api/alerts` returns the same. Set `DEEPBELL_ALERT_WEBHOOK_URL` to post firing and resolved alerts to a Slack or Discord webhook (rate-limited, sent from a background thread); without it they are only logged. See [docs/walkthroughs/02-alert-rules.md](docs/walkthroughs/02-alert-rules.md).
- **Status strip.** Uptime-style bars for the last 30 minutes, built from the metrics history: healthy, flagged samples without an incident, incident open, synthetic warm-up, no data. A bar with an incident links to it.
- **Live Telemetry.** CPU and memory over 2 or 10 minutes, the detector's baseline and streaks, and a CPU-against-memory scatter plot that shows which samples crossed an alert line and became candidates for the Isolation Forest.
- **Time Machine.** The SQLite database is copied every 30 s when its contents changed (newest 10 kept, each integrity-checked). The Time Machine lists each snapshot with its size, whether it passes `PRAGMA quick_check` and how many transactions it holds, says whether the newest one holds the current data, and restores any of them in one click after a confirmation. A corrupt database is restored automatically at startup.
- **Chaos Lab.** Inject a CPU spike, a memory leak, database corruption or (locally) a hard crash, each with its caps and what to watch for shown on the card, and watch the pipeline, the charts and the event log react. On a phone the page scrolls to the live results after an injection.
- **Dashboard.** One Jinja template, one stylesheet and vanilla JavaScript with no build step; Chart.js and marked come from a CDN at pinned versions with subresource integrity. Dark theme by default with a light theme, toasts and confirmations for every fault, a "Connection lost" state that pauses every action while the server restarts, and a layout that works down to 360 px. Keyboard focus survives the live updates, screen readers hear state changes (not every new number), and reduced-motion settings are respected.

![Key numbers and live charts after a guided demo run: median time to detect and to recover, CPU and memory with the learned baseline, the alert line, flagged samples and shaded incident spans, and the 30-minute status strip](docs/screenshots/overview.png)

<p>
  <img src="docs/screenshots/incident.png" alt="Incidents view: a memory-leak incident with its time to detect, time to recover, lifecycle bar and timeline" width="68%">
  <img src="docs/screenshots/mobile.png" alt="The overview on a 390 px wide phone screen" width="28%">
</p>

## How it works

```mermaid
flowchart LR
    subgraph proc["One FastAPI process (single uvicorn worker)"]
        sampler["Sampler thread<br/>CPU + RSS every 2 s"] --> detector["Detector<br/>baseline, Isolation Forest,<br/>2 samples in a row"]
        sampler --> metrics[("metrics.db<br/>samples, incidents")]
        detector -->|"open / close"| incidents["Incidents<br/>timeline, TTD, TTR"]
        incidents --> metrics
        incidents --> reporter["Report writer<br/>template or Gemini"]
        reporter --> reports[("reports/*.md")]
        sampler -->|"integrity check"| appdb[("app.db<br/>sample transactions")]
        appdb --> snapper["Snapshot thread<br/>every 30 s if changed"]
        snapper --> backups[("backups/*.db.bak")]
        backups -->|rollback| appdb
        faults["Chaos Lab and guided demo<br/>capped faults"] -.->|"leak, CPU spike"| sampler
        faults -.->|corrupt| appdb
    end
    browser["Browser UI<br/>Jinja + vanilla JS"] <-->|"polls /api/status every 2 s"| proc
```

1. **Sample.** Every 2 s the watchdog reads the process's CPU % and resident memory (RSS) and stores the sample in `metrics.db`. Inside a container with a CPU quota (0.1 CPU on Render's free plan), CPU is scaled to that quota.
2. **Detect.** The detector scores the sample. Two anomalous samples in a row open an incident; three normal ones close it. Every 2 s it also queries `app.db`, so a corrupted file opens a database incident.
3. **Report.** Opening an incident starts a report in a background thread. The report and every later action (memory released, snapshot restored) are added to the incident's timeline.
4. **Recover.** Releasing the leaked memory lets the samples return to the baseline, which closes the incident. Restoring a snapshot makes the database readable again, which closes the database incident.

Incident history is kept in `metrics.db`, not `app.db`, so corrupting or rolling back the application database never rewrites it.

Every user flow (landing, guided demo, each fault, incidents and reports, the Time Machine, reconnecting) and every system flow (startup, sampling and detection, the incident lifecycle, reports, snapshots, rollback, the live pipeline) is documented step by step, with diagrams, files, functions and error paths, in [docs/process-flows.md](docs/process-flows.md).

## Detection logic

The detector is deliberately conservative: a single noisy sample should never page anyone.

- **Baseline.** The newest 150 normal samples. CPU uses their 95th percentile and memory their median. The baseline is refitted every 5 minutes, not on every sample. A fresh instance seeds 60 synthetic warm-up samples so it can score at once; they are stored as `Warm-up (synthetic)`, drawn grey, and replaced by live samples after 30 live ones.
- **Candidate.** A sample is a candidate when CPU is at least 15 percentage points above the baseline or memory at least 30 MB above it.
- **Confirmation.** An Isolation Forest (100 trees, contamination 0.02) is fitted on the baseline window plus the candidate and must label the candidate an outlier. It runs only for new candidates, not on every sample. On its own the forest flagged ordinary jitter, which is why it only confirms candidates that already cleared the minimum deviation.
- **Consecutive samples.** Two anomalous samples in a row open an incident and three normal samples close it. While an incident is open its samples stay out of the baseline, so a leak that is still held never becomes the new normal. One incident (and one report) per fault kind at a time.
- **What it costs.** Detection needs at least two samples, so time to detect is at least one sample interval after the first anomalous sample. In six local runs of the guided demo the leak was detected 2 to 4 s after it was injected and resolved 10 to 13 s after detection (release, then three normal samples). These are demo measurements on a laptop, not production numbers.
- **What it misses.** Leaks smaller than 30 MB, CPU bursts shorter than two samples, and anything that is not CPU or memory. Database corruption is caught by a query, not by the model.

## Reliability benchmark

Run on 2026-11-10. This is the larger, reproducible complement to the six local demo runs above: a deterministic simulation of the real `Detector` (real config constants, fake clock, seeded RNG, no threads or network) on synthetic sample streams. It is **not production telemetry**, and "closed" measures the detector noticing normal samples again, not a real process recovering.

Command: `.venv/Scripts/python target_app/benchmark_detection.py` (flags `-m`, `-n` and `--minutes` change the sample sizes; the defaults are small so a full run takes about 70 s, because the Isolation Forest is refitted for every candidate sample).

| Runs | Simulated hours | False incidents | Incidents per hour | Runs with a false incident |
|---|---|---|---|---|
| 20 | 3.3 | 0 | 0.000 | 0.0% |

| Fault | Runs | Detected within 60 s | Detect p50 | Detect p95 | Detect max | Closed within 60 s of release | Close p50 | Close p95 |
|---|---|---|---|---|---|---|---|---|
| CPU Spike | 30 | 100.0% | 4 s | 4 s | 4 s | 100.0% | 6 s | 6 s |
| Memory Leak | 30 | 100.0% | 4 s | 4 s | 4 s | 100.0% | 6 s | 6 s |

Parameters:

- Command: `.venv/Scripts/python target_app/benchmark_detection.py`
- Python 3.11.9; false-positive runs M=20 (seeds 0..19), fault runs N=30 per fault (seeds 0..29)
- Jitter streams: 10 simulated minutes at SAMPLE_INTERVAL_SEC=2 s, idle CPU 0.5-6 %, RAM 130 +/- 1.5 MB, a 25 % CPU bump every 37th sample
- Warm-up before injection: 40 idle samples (FIRST_FIT_SAMPLES=15)
- CPU spike: 3 samples at about 95 % (MAX_SPIKE_SEC=5); memory leak: +60 MB (MAX_LEAK_STEP_MB), released as soon as it is detected
- Detection time: seconds from the last sample before injection to the incident opening. Close time: seconds from release (CPU: end of the spike) to the incident closing
- Config: CPU_MIN_DELTA=15.0, RAM_MIN_DELTA_MB=30.0, OPEN_AFTER=2, CLOSE_AFTER=3, TRAIN_WINDOW=150, REFIT_INTERVAL_SEC=300

Reading the numbers: the sample sizes are modest (20 jitter streams, 30 runs per fault), so "0 false incidents in 3.3 simulated hours" is evidence, not a guarantee. Detection time is the same 4 s in every run (the two samples needed to open, 2 s apart) because the synthetic faults are well above the thresholds and the detector is deterministic; faults near the 15-point CPU or 30 MB memory thresholds would be slower or missed.

## Safety in public demo mode

Set `PUBLIC_DEMO=1` (the Render Blueprint does) when the app is reachable by anyone:

- `POST /api/settings/apikey` and the crash fault return 403, the Settings view is hidden, and no `.env` file is read. The app never writes `.env` in any mode.
- The guided demo runs in a server thread, one run at a time, with a 20 s cooldown after each run. A second visitor watches the run in progress instead of starting another, and a closed browser tab cannot leave a fault behind. If another visitor's leak or corruption is still in place after 15 s, the demo clears it (anyone can, from the Chaos Lab or the Time Machine) and says so.
- Leaked memory is released automatically after 5 minutes.

The caps and cooldowns apply in every mode:

| Fault | Limit |
|---|---|
| CPU spike | 1 to 5 seconds |
| Memory leak | 1 to 60 MB per request, 120 MB held at most, freed after 5 minutes |
| Every fault, and manual reports | Shared cooldown (20 s per fault, 30 s for reports); a request during it gets a 429 with `Retry-After` and a readable message |

## Tech stack

Python 3.11 and 3.12, FastAPI, Uvicorn, SQLite, psutil, scikit-learn and NumPy (Isolation Forest), LangChain with `langchain-google-genai` (only used when a key is set), Jinja2, Chart.js, marked, pytest, ruff, Docker, GitHub Actions and Render.

## Run locally

Requires Python 3.11 or 3.12.

```bash
git clone https://github.com/vishnuatgit/DeepBell.git
cd DeepBell
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r target_app/requirements.txt
cp .env.example .env               # optional; add GOOGLE_API_KEY for AI reports
python run_all.py
```

Open http://127.0.0.1:8000 and press **Run the guided demo**, or open the Chaos Lab. The API is documented at http://127.0.0.1:8000/docs, grouped by tag. `run_all.py` restarts the server after a crash; to run without it:

```bash
uvicorn main:app --app-dir target_app --port 8000
```

## Run with Docker

```bash
docker build -t deepbell .
docker run --rm -p 8000:8000 -e PUBLIC_DEMO=1 deepbell
```

The image is based on `python:3.11-slim`, runs as an unprivileged user and starts a single uvicorn worker (the app keeps its state and background threads in one process). It listens on `$PORT` (default 8000) and keeps its data in `/app/var`. Drop `-e PUBLIC_DEMO=1` to enable the crash fault and the Settings view; add `-e GOOGLE_API_KEY=...` for AI reports. `.dockerignore` keeps `.env`, local data and development files out of the image.

## Monitoring with Prometheus and Grafana

DeepBell serves its own metrics at `/metrics` (Prometheus text format; CPU and memory gauges, the latest anomaly score, incidents open and opened by kind, detection and recovery time histograms, fault injections, reports written, snapshot restores, alerts firing, alert state changes and alert notifications). To see them in Grafana on your machine (needs Docker):

```bash
docker compose -f monitoring/docker-compose.yml up --build
```

Then open Grafana at http://localhost:3000 (the DeepBell dashboard is the home page, no login needed), Prometheus at http://localhost:9090 and DeepBell at http://localhost:8000. Inject a fault in the Chaos Lab and watch the graphs; incident openings and fault injections appear as red and orange annotation lines. Stop it with `docker compose -f monitoring/docker-compose.yml down`. This stack is for local use only: the public demo serves `/metrics` but runs no Prometheus or Grafana. Details: [docs/walkthroughs/01-prometheus-grafana.md](docs/walkthroughs/01-prometheus-grafana.md).

## Tests

```bash
pip install -r target_app/requirements.txt -r requirements-dev.txt
ruff check . && ruff format --check .
pytest --cov=target_app
```

144 tests. They use temporary directories, disable the background threads (or drive them with a fake clock) and replace the Gemini call with stubs, so they never touch the network. They cover the detector (jitter, spikes, held leaks, baseline refresh, warm-up), incident timelines and measured times, the live pipeline through a whole incident (and a new fault right after one resolves), the incident statistics, report sources, wording per fault kind, linking and download, the guided demo end to end (with a fast fake watchdog), the status strip, snapshots (readability, contents, freshness) and rollback, corruption detection, public demo mode, fault caps, cooldowns and auto-release, the alert rules (validation, the pending, firing and resolved states with a fake clock, webhook limits), the likely-cause rules and the merged timeline, the `/metrics` output and the monitoring files, the OpenAPI tags and the main API endpoints. CI runs them on Python 3.11 and 3.12, lints with ruff, and builds and smoke-tests the Docker image.

## Deploy on Render

[`render.yaml`](render.yaml) is a Render Blueprint for a free Docker web service in the Singapore region, with `PUBLIC_DEMO=1` and a health check on `/healthz`.

1. Push the repository to GitHub.
2. In the Render dashboard choose **New > Blueprint** and select the repository.
3. When asked for `GOOGLE_API_KEY`, paste a Gemini key for AI-written reports, or leave it empty for template reports.
4. Deploy, then put the service URL in the [Live demo](#live-demo) section.

`autoDeployTrigger: checksPass` redeploys after each commit whose CI checks pass. The free plan has 0.1 CPU and 512 MB of RAM and an ephemeral disk (Render docs, checked 2026-10-31), so data resets on every restart; the app seeds itself on startup (60 synthetic warm-up samples, five sample transactions and one example report labelled as an example). In a local measurement (Windows, Python 3.11, 60 s windows) DeepBell idled at about 137 MB of resident memory and 0.3 % of one core with no visitors, or 0.6 % with one dashboard open; the guided demo and the Chaos Lab add at most 120 MB.

### Configuration

All variables are optional. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | empty | Enables AI-written reports via Gemini. On local runs it can also be set in the Settings view (in memory until restart). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model for AI reports. |
| `PUBLIC_DEMO` | empty (off) | Set to `1` for a public deployment (see [Safety in public demo mode](#safety-in-public-demo-mode)). |
| `DEEPBELL_DATA_DIR` | `data/` | Folder for `app.db` and `metrics.db`. |
| `DEEPBELL_BACKUP_DIR` | `backups/` | Folder for snapshots. |
| `DEEPBELL_REPORTS_DIR` | `reports/` | Folder for incident reports. |
| `DEEPBELL_ENV_FILE` | `.env` | Settings file read at startup on local runs (never in public demo mode). |
| `DEEPBELL_SEED` | `1` | Set to `0` to skip the startup seed data. |
| `DEEPBELL_ALERT_WEBHOOK_URL` | empty | Slack or Discord style incoming-webhook URL for alert notifications. Without it alerts are only shown in the UI and logged. Treat it as a secret: set it in the host's environment, never commit it. |
| `DEEPBELL_ALERT_RULES_FILE` | `target_app/alert_rules.json` | Alert rules file to load instead of the shipped one. |
| `DEEPBELL_CPU_CORES` | detected | CPU cores available to the app, used to scale CPU %. Read from the container's cgroup quota when unset. |
| `DEEPBELL_DISABLE_WORKERS` | empty | Set to `1` to skip the telemetry and snapshot threads. |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Address and port used by `run_all.py`. The Docker image reads `PORT`. |

Default paths are relative to the project root. The Docker image sets the three data folders to `/app/var/...`.

## Project structure

```
DeepBell/
├── target_app/
│   ├── main.py              # Creates the FastAPI app and includes the routers (main:app)
│   ├── alert_rules.json     # The shipped alert rules
│   ├── deepbell/            # The application package
│   │   ├── config.py        # Paths, environment variables, thresholds, caps and labels
│   │   ├── runtime.py       # Process-wide state: locks, the detector, open incidents, demo run
│   │   ├── timeutil.py      # UTC helpers (utcnow, iso, parse_utc)
│   │   ├── db.py            # SQLite connections, schema and the app.db health check
│   │   ├── snapshots.py     # Take, check and restore app.db snapshots; heal at startup
│   │   ├── detection.py     # Detector: baseline check confirmed by an Isolation Forest
│   │   ├── telemetry.py     # CPU quota, scoring samples, warm-up seed, pruning
│   │   ├── incident_store.py # Incident rows and timelines in metrics.db
│   │   ├── incidents.py     # Open (de-duplicated), time and close incidents
│   │   ├── correlation.py   # The likely-cause heuristic for one incident
│   │   ├── alert_rules.py   # Load and validate alert rules; the pending/firing/resolved engine
│   │   ├── alerts.py        # Feeds each sample to the engine; sends state changes on
│   │   ├── notifications.py # Optional webhook with rate limits (otherwise just the log)
│   │   ├── metrics.py       # Prometheus metrics served at /metrics
│   │   ├── reports.py       # Template reports and the report files
│   │   ├── llm.py           # Gemini via LangChain: key lookup, lazy load, AI reports
│   │   ├── rca.py           # Writes a report in the background (Gemini or template)
│   │   ├── chaos.py         # Chaos Lab faults with caps and shared cooldowns
│   │   ├── demo.py          # The guided demo scenario
│   │   ├── overview.py      # Status strip, live pipeline and incident stats
│   │   ├── eventlog.py      # The shared event console
│   │   ├── workers.py       # Snapshot and watchdog threads
│   │   ├── startup.py       # Startup checks, demo seed data, starts the workers
│   │   └── routes/          # One APIRouter per area: pages, transactions, status,
│   │                        #   alerts, metrics, incidents, reports, snapshots, settings,
│   │                        #   demo, chaos
│   ├── requirements.txt     # Pinned runtime dependencies
│   ├── templates/index.html # The dashboard markup (one page, eight views, the Settings view on local runs only)
│   └── static/              # app.js (behaviour), styles.css (dark and light themes), favicon.svg
├── tests/                   # pytest: test_app.py, test_watchdog.py, test_features.py, test_console.py,
│                            #   test_alerts.py, test_correlation.py, test_metrics.py, test_monitoring_files.py
├── docs/
│   ├── process-flows.md     # Every user and system flow, with diagrams
│   ├── roadmap.md           # What was built next, and what was dropped and why
│   ├── sdlc.md              # How it was planned, designed, built, tested and shipped
│   ├── uat.md               # User acceptance test: tasks, pass criteria, results
│   ├── walkthroughs/        # How Prometheus/Grafana, alert rules and correlation work
│   ├── screenshots/         # README and portfolio images
│   └── major_project_proposal.md
├── monitoring/              # docker-compose (DeepBell, Prometheus, Grafana), the Prometheus
│                            #   config and the provisioned Grafana dashboard
├── run_all.py               # Supervisor: restarts the server after a crash
├── Dockerfile               # Production image (single uvicorn worker, non-root)
├── render.yaml              # Render Blueprint for the public demo
├── requirements-dev.txt     # Test and lint tools
├── pyproject.toml           # ruff and pytest settings
└── .github/workflows/ci.yml # Lint, tests, Docker build
```

`data/`, `backups/` and `reports/` are created at runtime and are git-ignored.

<details>
<summary><strong>API reference</strong></summary>

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/healthz` | Liveness check |
| GET | `/api/status` | Health, open incidents, alert counts, last sample and its time, baseline, detector streaks, database state, fault cooldowns, guided-demo progress, the live pipeline (`pipeline`), the newest snapshot and whether it holds the current data, and uptime. `?events_after=N` also returns server events newer than N. |
| GET | `/api/status/history?minutes=&bars=` | Status-strip bars (default 30 minutes in 60 bars) and the share of healthy live samples |
| GET | `/api/alerts` | Alert rules with their state (`ok`, `pending`, `firing`, `resolved`), the newest state changes, problems found in the rules file and whether a webhook is configured |
| GET | `/metrics` | Prometheus metrics (text format) |
| GET | `/api/metrics?limit=` | Latest telemetry samples, newest first (default 15, at most 400) |
| GET | `/api/incidents?limit=` | Incident history, newest first, with timelines and times to detect and recover |
| GET | `/api/incidents/stats` | Incident counts and median times to detect and recover over the newest 20 |
| GET | `/api/incidents/{id}` | One incident with its merged timeline (each event has `offset_sec`) and `likely_cause`: `trigger`, `summary`, `confidence`, `confidence_reason`, `evidence`, `not_found`, `heuristic` and `disclaimer` |
| GET | `/api/reports` | Kept reports, newest first, with title, incident and source (`gemini`, `template`, `template-fallback`, `example`) |
| GET | `/api/reports/latest` | Newest report (404 if none) |
| GET | `/api/reports/{name}` | One report as JSON |
| GET | `/api/reports/{name}/download` | One report as a Markdown file, with its incident timeline appended |
| GET | `/api/backups` | Snapshots, newest first, with their creation time, whether each is readable and how many transactions it holds |
| POST | `/api/rollback` | Form field `filename`: restore that snapshot |
| POST | `/api/rca/generate` | Write a report now (AI or template) |
| GET | `/api/demo` | Current or last guided-demo run and its cooldown |
| POST | `/api/demo/start` | Start the guided demo (409 while one runs, 429 during its cooldown) |
| POST | `/api/settings/apikey` | Form field `api_key`: set the Gemini key in memory (403 in public demo mode) |
| POST | `/transaction?amount=` | Insert a sample transaction (0 < amount ≤ 1,000,000; the newest 200 are kept) |
| GET | `/transactions` | Last 10 transactions (503 while the database is unreadable) |
| POST | `/fault/cpu_spike?duration_sec=` | Busy loop in a background thread (1 to 5 s) |
| POST | `/fault/memory_leak?size_mb=` | Hold 1 to 60 MB until released (120 MB at most) |
| POST | `/fault/memory_release` | Free all leaked memory |
| POST | `/fault/corrupt_db` | Overwrite the SQLite header of `app.db` |
| POST | `/fault/fatal_crash` | `os._exit(1)` after one second (403 in public demo mode) |

All timestamps in API responses are ISO-8601 in UTC; the UI shows them in the viewer's time zone. The interactive OpenAPI docs are at `/docs`.

</details>

## Limitations

- It monitors only itself, on two signals (CPU and resident memory). It is a lab for the detect, report, recover loop, not a general monitoring tool.
- The minimum deviations are fixed (15 CPU points, 30 MB), so smaller spikes and slow leaks are deliberately ignored.
- Snapshots are not point-in-time recovery: you can restore one of the last 10 distinct snapshots, taken at most every 30 s, and a rollback loses whatever changed since.
- At runtime the rollback is manual (or part of the guided demo); it is automatic only in the startup check.
- There is no authentication. Public demo mode disables the dangerous endpoints and caps the rest, but any visitor can inject the capped faults and roll back the shared database. Cooldowns are global and kept in memory.
- Open incidents, cooldowns, the guided demo, leaked memory and the alert states live in one process, so the app must run as a single worker. If the server restarts while an incident is open, that incident is marked interrupted.
- Times to detect and recover are measured on injected faults in this lab and say nothing about a production system.
- The likely cause is a heuristic: it only knows about injected faults, CPU, memory and alerts, and it can say "unknown". Alert rules are read at startup (edit the file and restart), and a failed webhook post is logged, not retried.

## Author

Built by Chandana Vishnu Jayanth · [Portfolio](https://vishnuatgit.github.io) · [Case study](https://vishnuatgit.github.io/projects/deepbell.html) ([PDF](https://vishnuatgit.github.io/pdf/deepbell-case-study.pdf))

## License

[MIT](LICENSE) © 2026 Chandana Vishnu Jayanth
