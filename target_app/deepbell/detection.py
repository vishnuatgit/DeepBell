"""Anomaly detection: a learned baseline check, confirmed by an Isolation Forest."""

from collections import deque

from . import config, timeutil


class Detector:
    """Isolation Forest anomaly detection over (CPU %, RSS MB) against a baseline window.

    The baseline (the newest TRAIN_WINDOW normal samples, their 95th-percentile CPU and median
    memory) is refreshed every REFIT_INTERVAL_SEC, not on every sample. A sample becomes a
    candidate only when it is at least CPU_MIN_DELTA points (or RAM_MIN_DELTA_MB) above the
    baseline; the forest then confirms it. The forest isolates almost any point outside the
    window's range, which is why it flagged ordinary jitter on its own.

    An incident opens after OPEN_AFTER anomalous samples in a row and closes after CLOSE_AFTER
    normal ones. Samples taken while an incident is open never enter the baseline, so a leak
    that is still held keeps its incident open instead of becoming the new normal.
    """

    def __init__(self):
        self.samples: deque = deque(maxlen=config.TRAIN_WINDOW)  # (cpu, ram, synthetic)
        self.window = None  # numpy array of the baseline samples
        self.baseline: dict | None = None
        self.fitted_at: float | None = None
        self.fit_count = 0
        self.forest_fits = 0
        self.new_since_fit = 0
        self.streak = dict.fromkeys(config.DETECTED_KINDS, 0)
        self.calm = dict.fromkeys(config.DETECTED_KINDS, 0)
        self.open: set[str] = set()

    def reset(self) -> None:
        """Forget the baseline and learn a new one from the next samples."""
        self.samples.clear()
        self.window = None
        self.baseline = None
        self.fitted_at = None

    def add_warmup(self, points: list[tuple[float, float]]) -> None:
        self.samples.extend((cpu, ram, True) for cpu, ram in points)

    def live_count(self) -> int:
        return sum(1 for s in self.samples if not s[2])

    def fit_due(self, now: float) -> bool:
        if self.baseline is None:
            return len(self.samples) >= config.FIRST_FIT_SAMPLES
        if self.baseline["source"] == "warm-up":
            return self.live_count() >= config.LIVE_FIT_SAMPLES
        return (
            now - self.fitted_at >= config.REFIT_INTERVAL_SEC
            and self.new_since_fit >= config.FIRST_FIT_SAMPLES
        )

    def fit(self, now: float) -> None:
        """Refresh the baseline window, preferring live samples over synthetic warm-up ones."""
        import numpy as np

        live = [s for s in self.samples if not s[2]]
        source = "live" if len(live) >= config.FIRST_FIT_SAMPLES else "warm-up"
        if source == "live":
            self.samples = deque(live, maxlen=config.TRAIN_WINDOW)
        self.window = np.array([[cpu, ram] for cpu, ram, _ in self.samples])
        self.baseline = {
            "cpu": round(float(np.percentile(self.window[:, 0], 95)), 1),
            "ram": round(float(np.median(self.window[:, 1])), 1),
            "source": source,
            "samples": len(self.window),
            "fitted_at": timeutil.iso(timeutil.utcnow()),
        }
        self.fitted_at = now
        self.fit_count += 1
        self.new_since_fit = 0

    def isolated(self, cpu: float, ram: float) -> bool:
        """Fit an Isolation Forest on the baseline window plus this sample; True if the sample
        is an outlier. A forest fitted on the baseline alone cannot rank points outside its
        training range (they all score like its most extreme samples), so the candidate is
        part of the fit."""
        import numpy as np
        from sklearn.ensemble import IsolationForest

        X = np.vstack([self.window, [[cpu, ram]]])
        model = IsolationForest(n_estimators=100, contamination=0.02, random_state=42).fit(X)
        self.forest_fits += 1
        return bool(model.predict([[cpu, ram]])[0] == -1)

    def classify(self, cpu: float, ram: float) -> list[str]:
        if self.baseline is None:
            return []
        kinds = []
        if cpu > self.baseline["cpu"] + config.CPU_MIN_DELTA:
            kinds.append("cpu_spike")
        if ram > self.baseline["ram"] + config.RAM_MIN_DELTA_MB:
            kinds.append("memory_leak")
        # The forest confirms new candidates only: an open incident stays open while its
        # deviation lasts, without a forest fit every 2 s.
        if any(k not in self.open for k in kinds) and not self.isolated(cpu, ram):
            kinds = [k for k in kinds if k in self.open]
        return kinds

    def observe(self, cpu: float, ram: float, now: float):
        """Score one sample. Returns (its anomaly kinds, incidents opened, incidents closed)."""
        if self.fit_due(now):
            self.fit(now)
        kinds = self.classify(cpu, ram)
        opened, closed = [], []
        for kind in config.DETECTED_KINDS:
            if kind in kinds:
                self.streak[kind] += 1
                self.calm[kind] = 0
                if kind not in self.open and self.streak[kind] >= config.OPEN_AFTER:
                    self.open.add(kind)
                    opened.append(kind)
            else:
                self.streak[kind] = 0
                self.calm[kind] += 1
                if kind in self.open and self.calm[kind] >= config.CLOSE_AFTER:
                    self.open.discard(kind)
                    closed.append(kind)
        if not kinds and not self.open:
            self.samples.append((cpu, ram, False))
            self.new_since_fit += 1
        return kinds, opened, closed
