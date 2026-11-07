# 03. Incident timeline with correlation and a likely cause

## What it does and why

When an incident happens the first question is "what changed?". DeepBell now answers it in two ways on the incident detail view:

1. **One merged timeline.** Everything that happened around the incident, in order, with the seconds since it started: the fault that was injected, the first anomalous sample, alerts going pending, firing and resolved, the incident opening, the report being written, a snapshot being restored, and the incident closing.
2. **A "likely cause" panel.** The most probable trigger, the evidence for it, what was checked and not found, and a confidence level. It is labelled a heuristic and never claims more than the evidence supports.

The same analysis is in the Markdown report and in `GET /api/incidents/{id}`.

## How it works, end to end

**Building the timeline.** Each incident row already stores a list of events (`incident_store.py`). This feature adds more event types to that same list, so there is one source of truth:

- `incidents.open_incident` copies in the alert events that already happened since the anomaly began (for example "pending", which comes before the incident opens).
- While the incident is open, `alerts.evaluate_sample` sends every alert state change that belongs to it to `incidents.note_alert`. An alert belongs to an incident when its metric relates to the incident's kind (`alert_rules.INCIDENT_KINDS`: CPU rules to CPU incidents, memory rules to memory incidents, the database rule to database incidents, the anomaly rule to both CPU and memory).
- A snapshot restore is recorded with its own type, `restore` (it was a generic `action` before).
- `telemetry.record_sample` now evaluates alerts before closing incidents, so an alert that clears at the same moment still lands on the timeline.
- `incident_store.incident_from_row` sorts events by time (with a fixed order for ties) and adds `offset_sec`.

**Working out the likely cause** (`correlation.py`). `likely_cause(incident, samples, others)` is a plain function over data, so it is easy to test. The rules, in plain words:

1. **A fault of the same kind came first.** If the timeline holds a fault event (the app only records one when it matches the incident's kind and came within 60 s), the trigger is that fault. Confidence is **high** if it came within 10 s of the first anomalous sample and nothing contradicts it, otherwise **medium**.
2. **Which metric moved first.** From the telemetry around the incident, the code finds when CPU and memory each crossed their alert line (the median of the samples before the incident plus the same minimum deviation the watchdog uses). If the other metric crossed at least 2 s earlier, that does not fit a single-metric fault: the evidence says so and high confidence drops to medium.
3. **No fault recorded.** The trigger is "Unknown". The metrics can say what moved and when, not why, so confidence is **low** (CPU or memory incident with some evidence) or **unknown** (database incident, or no telemetry).
4. **Overlapping incidents** within 30 s are listed as context only, with the words "nothing here shows" a shared cause.

Alerts that fired are listed as supporting evidence. The result always carries `heuristic: true` and a disclaimer.

**Showing it.** `GET /api/incidents/{id}` returns the incident with `likely_cause`. The dashboard fetches it when an incident is selected and again only when its timeline grows or its status changes. The report template (`reports.build_template_report`) adds a "Correlation analysis (heuristic)" section; the download endpoint adds it to any report that lacks one (for example a Gemini report).

## Files

`deepbell/correlation.py` (the rules and the Markdown section), `deepbell/incidents.py` and `deepbell/alerts.py` (feeding events to the timeline), `deepbell/incident_store.py` (ordering and offsets), `deepbell/reports.py` and `deepbell/rca.py` (the report), `deepbell/routes/incidents.py` (the API), `static/app.js`, `templates/index.html`, `static/styles.css` (the panel), `tests/test_correlation.py`.

## Design choices and trade-offs

- **Simple, checkable rules instead of a model.** A person can verify every statement from the timeline. A learned model would sound more impressive and be harder to explain or trust.
- **Honest confidence.** High needs two things (a matching fault very close in time and no contradicting signal). With no fault the code says "unknown" rather than guessing, and says what it looked for and did not find.
- **Correlation is not causation.** A fault injected shortly before an anomaly is strong evidence in this lab because the lab injects the faults. In production the same rule would be weaker, and real systems have signals this app does not see (deploys, traffic, upstream outages). The disclaimer says so.
- **Alerts are stored on the incident**, not rebuilt later from memory, so an old incident keeps its alerts after a restart.
- **The cause is computed on request**, not stored. It always reflects the full timeline and costs a few small queries on the incident view.
- **One timeline format.** Reusing the existing event list meant the lifecycle bar, reports and API needed almost no change.

## Limits

- Only injected faults, CPU, memory, alerts and the database check are considered; there is no knowledge of deployments, traffic or other services.
- The "which metric moved first" rule has the samples' 2 s resolution, so differences below 2 s are reported as "together".
- Incidents opened before this feature have no alert events, and a restarted server loses alert history that never reached an incident.
- Gemini-written reports do not contain the analysis in their body; the download adds it.

## Likely interview questions

1. **How do you know your "likely cause" is right?** I do not, and the panel says so. It lists evidence that lines up in time and gives a confidence level; "high" needs a matching fault within 10 s and no contradiction.
2. **Why rules and not machine learning?** Every output must be explainable from the timeline in an interview or an incident review. With little data a model would add confidence it has not earned.
3. **What is the difference between correlation and causation here?** The fault and the anomaly happen close together, which makes the fault the best explanation, but an unseen third event could explain both. Hence the disclaimer and the medium level when anything disagrees.
4. **What happens when there is no evidence?** The trigger is "Unknown", the confidence "low" or "unknown", and the panel lists what was checked and not found.
5. **How did you merge events from different sources in order?** They all become entries in the incident's single event list with a timestamp and type; reading sorts by time and uses a fixed order for events in the same second.
6. **Why evaluate alerts before closing the incident?** Otherwise an alert that resolves in the same sample as the incident would be dropped, because the incident is already closed and no longer receives events.
7. **How is it tested?** The rules are a pure function tested with hand-built timelines and telemetry, and end-to-end tests inject real faults through the API and check the timeline, the API, the report and the download.
8. **What would you add for production?** Deploy and config-change events, traffic and dependency signals, a longer look-back window, and feedback from engineers on whether the suggestion was right.
