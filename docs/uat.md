# User acceptance test: DeepBell

## Who to test with

Test with 3 to 5 people who have never seen DeepBell. Mix them:

- 2 students or junior developers (typical reviewers of a portfolio).
- 1 person who has heard of monitoring or on-call work.
- 1 non-technical person, if you can.

Do not explain the app first. Do not help during a task. Note where they hesitate.

## Setup

- Live demo: open https://deepbell.onrender.com (from the README). If the site slept, note the wait time as part of Task 1. The README says the first visit can take about a minute.
- Local alternative: from the repo root run `python run_all.py` (named in the README) and open the address it prints.
- Use one laptop browser. Run one tester on a phone as well.
- Use a stopwatch. Ask testers to think aloud. Only one tester at a time on the live demo, because it is a shared server with cooldowns.
- Tell the tester only: "This is a website. Try to do the tasks. I will not help."

## Tasks

Read each task aloud. Stop a task at 3 minutes and mark it a fail.

**T1. Get started.** "Open the site and tell me, in your own words, what it is for."
Pass: the tester says it watches a server and writes reports when something goes wrong, within 60 seconds of the page showing. Time any wait for the page to load.

**T2. See it work.** "Show me the system detecting a problem by itself."
Pass: the tester starts the guided demo and sees at least the first incident open, without help.

**T3. Read the result.** "Find out what went wrong in the incident and how long it took to notice it."
Pass: the tester opens an incident or report and states the fault and the time to detect, correctly.

**T4. Judge the report.** "Tell me whether the report was written by a person, a template or an AI, and how sure it is of the cause."
Pass: the tester gives the right source (as shown on the report) and finds the confidence level or says there is none.

**T5. Break something yourself.** "Cause a problem of your own choosing, then put the system back to normal."
Pass: the tester injects a fault and restores normal state (release memory or roll back), and reads any limit or cooldown message correctly without help.

## Post-test questions

1. What was the most confusing moment, and where were you when it happened?
2. Would you trust the cause in the report enough to act on it? Why or why not?
3. What would you tell a friend this app does, in one sentence?

## Results

| Tester | T1 | T2 | T3 | T4 | T5 | Time (total) | Notes |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |
| 2 | | | | | | | |
| 3 | | | | | | | |
| 4 | | | | | | | |
| 5 | | | | | | | |

Mark each task pass or fail.

## Sign-off

Tested by: ____________________  Date: ____________  Build or commit: ____________  Result (pass if at least 4 of 5 testers pass T1 to T3): ____________

## Automated acceptance check (2026-11-09)

> Automated stand-in, not a real-user test: a scripted browser ran each task on the live demo. It shows each task *can* be completed, not that people understand the page. The real-user results table above is still to be filled.

Playwright with Chrome against https://deepbell.onrender.com, at 390x844 and 1280x800. The guided demo was run once and one fault (a 60 MB memory leak, then Release memory) was injected once, both at desktop size; the phone run reused the resulting incidents and only checked that the demo and fault controls were visible and enabled.

| Task | phone 390px | desktop 1280px | notes |
|---|---|---|---|
| T1 Get started | n/a (needs a person); page shown in 4.0 s, heading and intro text present | n/a (needs a person); page shown in 4.1 s, heading and intro text present | Server was already awake, so the 60 s cold start was not measured. Whether a visitor can say what it is for needs a person. |
| T2 See it work | not re-run (shared demo); "Run the demo again" button visible and enabled | pass: clicked "Run the guided demo", incidents appeared within seconds (4 incidents after the full demo) | Demo run once only, to protect the shared instance. |
| T3 Read the result | pass: incident #4 opens; "Time to detect 3 s" and cause shown | pass: same | Correctness of what a person reads from it needs a person. |
| T4 Judge the report | pass: badge "Built-in template"; cause "Heuristic, low confidence" | pass: same | Whether the visitor gets it right needs a person. Report notes no Gemini key is set, so a template report is the only source seen. |
| T5 Break something yourself | not re-run; "Inject 60 MB leak" and "Release memory" visible and enabled | pass: injected the memory leak, then Release memory; "Released ... MB" confirmed | No cooldown or cap message was hit (the demo's cooldown had ended). Reading such a message correctly needs a person. |

Screenshots (one per task per size) are kept outside the repo.

### Accessibility (axe-core 4.10.2)

Views scanned at both sizes: overview, incidents (list with an incident and its report open), alerts, Chaos Lab. Reports live inside the Incidents view, so there is no separate reports page. Injection worked; no CSP or SRI block.

Totals across distinct view/size scans: critical 0, serious 14 violation instances across 4 rules, moderate 1 (phone Chaos Lab, 3 nodes), minor 0.

- `scrollable-region-focusable` (serious, on every view): a scrolling area cannot be reached by keyboard.
- `nested-interactive` (serious, overview, 3 nodes at both sizes): a control sits inside another control, which confuses screen readers.
- `color-contrast` (serious, Chaos Lab, 2 nodes at both sizes): text too faint against its background.
- `skip-link` (moderate, phone Chaos Lab, 3 nodes): a skip link's target is missing or not focusable at that view.

Fixed the same day: a shared event log and the data tables are now keyboard-focusable with a visible focus ring; the three overview KPI bar charts are role=group instead of role=img, so their links no longer nest inside an image; the faint Chaos Lab text (disabled crash card, warn and orange labels in the light theme) is raised to at least 4.5:1; the nav hash targets now exist. A local re-scan with axe 4.10.2 of overview, incidents, alerts and Chaos Lab at 390 and 1280 px, in both themes, reports 0 violations. The nested-interactive fix was not re-scanned with incident data, as that needs injected faults.

### Bugs found

None blocking. Observations: the phone tab bar clips "Live Telemetry" (scrolls sideways); the detail of CPU spike #4 reads "No injected fault on record" and shows the cause as "Heuristic, low confidence". That is correct: the guided demo injects only a memory leak and database corruption, never a CPU spike, so this spike was a natural one; CPU spike #1 and Memory leak #2 opened at the same second (15:39:29), which may read as a duplicate.

Dependency check the same day: target_app/requirements.txt had 48 known advisories on OSV.dev; upgraded to fastapi 0.162.2, uvicorn 0.54.0, jinja2 3.1.6, psutil 7.2.2, numpy 2.4.6, scikit-learn 1.9.1, langchain-core 1.7.6, langchain-google-genai 4.4.3, python-dotenv 1.2.4, python-multipart 0.0.33; now 0.
