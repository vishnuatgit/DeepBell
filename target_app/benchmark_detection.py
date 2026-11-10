"""Reproducible reliability benchmark for the DeepBell anomaly detector.

Runs the real ``Detector`` with the real config constants on seeded synthetic sample streams
and a fake clock (no threads, no network, no real faults). It measures:

1. false incidents on jitter-only streams,
2. detection latency for a CPU spike and a memory leak, sized like the Chaos Lab faults,
3. how fast an open incident closes once the fault is released.

It is a simulation of the detector, not production telemetry, and "recovery" here means the
detector noticing normal samples again, not a real process recovering.

Run from the repository root:  .venv/Scripts/python target_app/benchmark_detection.py
"""

import argparse
import math
import platform
import random
import sys

from deepbell import config, detection

DETECT_WITHIN_SEC = 60.0
CLOSE_WITHIN_SEC = 60.0
WARMUP_SAMPLES = 40  # past FIRST_FIT_SAMPLES, so the first fit has happened
JITTER_MINUTES = 10  # default stream length; the forest fit per bump makes longer runs slow
DEFAULT_M, DEFAULT_N = 20, 30
# Fault sizes follow the Chaos Lab: a spike lasts MAX_SPIKE_SEC at ~95 % CPU (as in the tests),
# a leak step is MAX_LEAK_STEP_MB on top of the ~130 MB idle level.
SPIKE_CPU = 95.0
SPIKE_SAMPLES = math.ceil(config.MAX_SPIKE_SEC / config.SAMPLE_INTERVAL_SEC)
LEAK_MB = float(config.MAX_LEAK_STEP_MB)
LEAK_HOLD_SAMPLES = 60  # give up waiting for detection after this many samples
FAULTS = ("cpu_spike", "memory_leak")


class Clock:
    def __init__(self):
        self.t = 0.0

    def tick(self):
        self.t += config.SAMPLE_INTERVAL_SEC
        return self.t


def idle(rng):
    """One idle sample: a few % CPU and ~130 MB with normal jitter (as in tests/test_watchdog)."""
    return rng.uniform(0.5, 6.0), 130.0 + rng.uniform(-1.5, 1.5)


def percentile(values, q):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def false_positive_run(seed, minutes=JITTER_MINUTES):
    """Jitter-only stream with occasional single bumps. Returns (incidents opened, seconds)."""
    rng, clock, det = random.Random(seed), Clock(), detection.Detector()
    n = int(minutes * 60 / config.SAMPLE_INTERVAL_SEC)
    samples = [idle(rng) for _ in range(n)]
    for i in range(20, n, 37):
        samples[i] = (25.0, samples[i][1])
    opened = 0
    for cpu, ram in samples:
        opened += len(det.observe(cpu, ram, clock.tick())[1])
    return opened, n * config.SAMPLE_INTERVAL_SEC


def fault_run(kind, seed):
    """Warm up, inject a fault, release it. Returns (detect_sec, close_sec); None if missed."""
    rng, clock, det = random.Random(seed), Clock(), detection.Detector()
    for _ in range(WARMUP_SAMPLES):
        det.observe(*idle(rng), clock.tick())
    injected_at = clock.t
    if kind == "cpu_spike":
        fault = [
            (SPIKE_CPU + rng.uniform(-3, 3), 130.0 + rng.uniform(-1.5, 1.5))
            for _ in range(SPIKE_SAMPLES)
        ]
    else:
        fault = [
            (rng.uniform(0.5, 6.0), 130.0 + LEAK_MB + rng.uniform(-1, 1))
            for _ in range(LEAK_HOLD_SAMPLES)
        ]
    detect = None
    for cpu, ram in fault:
        _, opened, _ = det.observe(cpu, ram, clock.tick())
        if opened and detect is None:
            detect = clock.t - injected_at
            if kind == "memory_leak":
                break  # the leak is released as soon as it is detected
    if detect is None:
        return None, None
    released_at = clock.t
    for _ in range(int(CLOSE_WITHIN_SEC * 4 / config.SAMPLE_INTERVAL_SEC)):
        _, _, closed = det.observe(*idle(rng), clock.tick())
        if closed:
            return detect, clock.t - released_at
    return detect, None


def run_benchmark(m=DEFAULT_M, n=DEFAULT_N, minutes=JITTER_MINUTES):
    runs = [false_positive_run(seed, minutes) for seed in range(m)]
    total_incidents = sum(o for o, _ in runs)
    hours = sum(s for _, s in runs) / 3600
    result = {
        "false_positive": {
            "runs": m,
            "incidents": total_incidents,
            "hours": hours,
            "incidents_per_hour": total_incidents / hours if hours else 0.0,
            "runs_with_false_incident": sum(1 for o, _ in runs if o) / m if m else 0.0,
        },
        "faults": {},
    }
    for kind in FAULTS:
        results = [fault_run(kind, seed) for seed in range(n)]
        det = [d for d, _ in results if d is not None]
        closes = [c for d, c in results if d is not None and c is not None]
        result["faults"][kind] = {
            "runs": n,
            "detected": len(det),
            "detection_rate": sum(1 for d in det if d <= DETECT_WITHIN_SEC) / n if n else 0.0,
            "detect_p50": percentile(det, 50),
            "detect_p95": percentile(det, 95),
            "detect_max": max(det) if det else None,
            "recovery_rate": (
                sum(1 for c in closes if c <= CLOSE_WITHIN_SEC) / len(det) if det else 0.0
            ),
            "close_p50": percentile(closes, 50),
            "close_p95": percentile(closes, 95),
            "close_max": max(closes) if closes else None,
        }
    return result


def fmt(v):
    return "n/a" if v is None else f"{v:.0f} s"


def render(result, m, n, command, minutes=JITTER_MINUTES):
    fp = result["false_positive"]
    lines = [
        "| Runs | Simulated hours | False incidents | Incidents per hour | Runs with a false incident |",
        "|---|---|---|---|---|",
        f"| {fp['runs']} | {fp['hours']:.1f} | {fp['incidents']} | "
        f"{fp['incidents_per_hour']:.3f} | {fp['runs_with_false_incident']:.1%} |",
        "",
        f"| Fault | Runs | Detected within {DETECT_WITHIN_SEC:.0f} s | Detect p50 | Detect p95 "
        f"| Detect max | Closed within {CLOSE_WITHIN_SEC:.0f} s of release | Close p50 | Close p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for kind, f in result["faults"].items():
        lines.append(
            f"| {config.SAMPLE_LABELS[kind]} | {f['runs']} | {f['detection_rate']:.1%} | "
            f"{fmt(f['detect_p50'])} | {fmt(f['detect_p95'])} | {fmt(f['detect_max'])} | "
            f"{f['recovery_rate']:.1%} | {fmt(f['close_p50'])} | {fmt(f['close_p95'])} |"
        )
    lines += [
        "",
        "Parameters:",
        "",
        f"- Command: `{command}`",
        f"- Python {platform.python_version()}; false-positive runs M={m} (seeds 0..{m - 1}), "
        f"fault runs N={n} per fault (seeds 0..{n - 1})",
        f"- Jitter streams: {minutes} simulated minutes at SAMPLE_INTERVAL_SEC="
        f"{config.SAMPLE_INTERVAL_SEC} s, idle CPU 0.5-6 %, RAM 130 +/- 1.5 MB, a 25 % CPU bump "
        "every 37th sample",
        f"- Warm-up before injection: {WARMUP_SAMPLES} idle samples (FIRST_FIT_SAMPLES="
        f"{config.FIRST_FIT_SAMPLES})",
        f"- CPU spike: {SPIKE_SAMPLES} samples at about {SPIKE_CPU:.0f} % (MAX_SPIKE_SEC="
        f"{config.MAX_SPIKE_SEC}); memory leak: +{LEAK_MB:.0f} MB (MAX_LEAK_STEP_MB), released as "
        "soon as it is detected",
        "- Detection time: seconds from the last sample before injection to the incident opening. "
        "Close time: seconds from release (CPU: end of the spike) to the incident closing",
        f"- Config: CPU_MIN_DELTA={config.CPU_MIN_DELTA}, "
        f"RAM_MIN_DELTA_MB={config.RAM_MIN_DELTA_MB}, OPEN_AFTER={config.OPEN_AFTER}, "
        f"CLOSE_AFTER={config.CLOSE_AFTER}, TRAIN_WINDOW={config.TRAIN_WINDOW}, "
        f"REFIT_INTERVAL_SEC={config.REFIT_INTERVAL_SEC}",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-m", type=int, default=DEFAULT_M, help="false-positive runs")
    parser.add_argument("-n", type=int, default=DEFAULT_N, help="runs per fault kind")
    parser.add_argument("--minutes", type=int, default=JITTER_MINUTES, help="jitter stream length")
    args = parser.parse_args(argv)
    command = ".venv/Scripts/python target_app/benchmark_detection.py"
    if (args.m, args.n, args.minutes) != (DEFAULT_M, DEFAULT_N, JITTER_MINUTES):
        command += f" -m {args.m} -n {args.n} --minutes {args.minutes}"
    result = run_benchmark(args.m, args.n, args.minutes)
    print(render(result, args.m, args.n, command, args.minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
