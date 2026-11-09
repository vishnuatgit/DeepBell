# User acceptance test: DeepBell

## Who to test with

Test with 3 to 5 people who have never seen DeepBell. Mix them:

- 2 students or junior developers (your likely recruiters' peers).
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
