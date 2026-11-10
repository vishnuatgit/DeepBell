"""The reliability benchmark runs and returns sane numbers (tiny M and N)."""

import benchmark_detection as bench


def test_benchmark_runs_with_sane_fields():
    result = bench.run_benchmark(m=1, n=2)
    fp = result["false_positive"]
    assert fp["runs"] == 1 and fp["incidents_per_hour"] >= 0
    assert 0 <= fp["runs_with_false_incident"] <= 1
    assert set(result["faults"]) == {"cpu_spike", "memory_leak"}
    for f in result["faults"].values():
        assert 0 <= f["detection_rate"] <= 1
        assert 0 <= f["recovery_rate"] <= 1
        assert f["detect_p50"] is None or f["detect_p50"] <= f["detect_p95"] <= f["detect_max"]
    assert "| Fault |" in bench.render(result, 1, 2, "cmd")
