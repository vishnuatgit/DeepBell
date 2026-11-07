"""The monitoring/ stack files: valid, consistent with each other and with /metrics."""

import json
import os
import re

import yaml
from prometheus_client.parser import text_string_to_metric_families

ROOT = os.path.join(os.path.dirname(__file__), "..", "monitoring")


def load_yaml(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_runs_the_three_services_and_mounts_existing_files():
    compose = load_yaml("docker-compose.yml")
    assert set(compose["services"]) == {"deepbell", "prometheus", "grafana"}
    for service in ("prometheus", "grafana"):
        for volume in compose["services"][service]["volumes"]:
            source = volume.split(":")[0]
            assert os.path.exists(os.path.join(ROOT, source)), source


def test_prometheus_scrapes_the_deepbell_service():
    config = load_yaml("prometheus.yml")
    job = config["scrape_configs"][0]
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["deepbell:8000"]


def test_grafana_datasource_matches_the_dashboard():
    datasource = load_yaml("grafana", "provisioning", "datasources", "prometheus.yml")[
        "datasources"
    ][0]
    assert datasource["url"] == "http://prometheus:9090"
    with open(os.path.join(ROOT, "grafana", "dashboards", "deepbell.json"), encoding="utf-8") as f:
        dashboard = json.load(f)
    uids = {p["datasource"]["uid"] for p in dashboard["panels"]}
    assert uids == {datasource["uid"]}
    assert len(dashboard["annotations"]["list"]) >= 1


def test_dashboard_queries_only_use_metrics_that_exist(client):
    families = set()
    for family in text_string_to_metric_families(client.get("/metrics").text):
        families.add(family.name)
        families.update(f"{family.name}{s}" for s in ("_total", "_bucket", "_sum", "_count"))
    with open(os.path.join(ROOT, "grafana", "dashboards", "deepbell.json"), encoding="utf-8") as f:
        dashboard = json.load(f)
    exprs = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    exprs += [a["expr"] for a in dashboard["annotations"]["list"]]
    used = {name for expr in exprs for name in re.findall(r"deepbell_\w+", expr)}
    assert used and used <= families, used - families
