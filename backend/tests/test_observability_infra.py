"""Structural checks for the issue #18 infra deliverables: k6 load-test scripts,
Grafana dashboard provisioning, and the Prometheus scrape config (acceptance criteria
#3/#4/#5, gaps #3-#5). These files don't exist yet -- Phase 3 (implementation) places
them; this file only proves they exist, parse, and reference the right metrics/paths
once it does.

Deliberately TOLERANT (per `.orchestrator/CONTEXT.md`'s "structure-validated, not
necessarily an app page" framing): these assert "the right file exists, parses as
valid JSON/YAML, and contains the key tokens the capacity thesis depends on" -- never
brittle formatting/whitespace/ordering checks that would make Phase 3 fight the tests
over presentation instead of substance.

Paths are resolved relative to the repo root (`loadtest/`, `monitoring/` are TOP-LEVEL
directories, siblings of `backend/`, per CONTEXT.md's "Infra files ... live outside
`backend/src`; keep them in a top-level `loadtest/` and `monitoring/`") -- NOT under
`backend/`, so this file walks up from its own path rather than assuming the current
working directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

# tests/ -> backend/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
LOADTEST_DIR = REPO_ROOT / "loadtest"
MONITORING_DIR = REPO_ROOT / "monitoring"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_one(directory: Path, *, name_hint: str, suffix: str) -> Path:
    """Locate a single file under `directory` whose name contains `name_hint`
    (case-insensitive) and matches `suffix` -- tolerant of the implementer's exact
    naming, as long as it's discoverable by intent."""
    if not directory.is_dir():
        pytest.fail(f"expected directory {directory} to exist")
    candidates = [
        p
        for p in directory.rglob(f"*{suffix}")
        if name_hint.lower() in p.name.lower()
    ]
    if not candidates:
        pytest.fail(
            f"expected a *{suffix} file with {name_hint!r} in its name under {directory}"
        )
    return candidates[0]


# ── AC3/AC4: k6 load-test scripts ──────────────────────────────────────────────────


def test_loadtest_directory_exists() -> None:
    assert LOADTEST_DIR.is_dir(), f"expected a top-level loadtest/ directory at {LOADTEST_DIR}"


def test_connection_ceiling_k6_script_exists_and_ramps_sse_connections() -> None:
    """AC3: a k6 script that ramps SSE (`/events`) connections to find the
    per-instance connection ceiling."""
    script = _find_one(LOADTEST_DIR, name_hint="connection", suffix=".js")
    text = _read(script)
    assert "k6" in text
    assert "/events" in text
    # A ramping/ceiling-finding load test needs a scenario definition of some kind --
    # tolerant of k6's several APIs for this (`stages`, `ramping-vus`, `executor`).
    assert any(token in text for token in ("stages", "ramping", "executor", "vus"))


def test_create_rate_k6_script_exists_and_targets_the_create_endpoint() -> None:
    """AC4: a k6 script sustaining a target `POST /create` rate against the
    simulator seam (the write-path throughput proof)."""
    script = _find_one(LOADTEST_DIR, name_hint="create", suffix=".js")
    text = _read(script)
    assert "k6" in text
    assert "/create" in text


# ── AC2/AC4: Grafana dashboard provisioning ────────────────────────────────────────


def test_monitoring_directory_exists() -> None:
    assert MONITORING_DIR.is_dir(), f"expected a top-level monitoring/ directory at {MONITORING_DIR}"


def test_pipeline_dashboard_json_parses_and_references_pipeline_gauges() -> None:
    """AC2: the pipeline dashboard (jobs by state, slot utilization, failure/refund
    rates) -- validated as well-formed JSON referencing the metrics it must chart,
    not a pixel-perfect layout check."""
    dashboard = _find_one(MONITORING_DIR, name_hint="pipeline", suffix=".json")
    data = json.loads(_read(dashboard))
    assert isinstance(data, dict)
    rendered = json.dumps(data)
    assert "songforge_jobs_in_state" in rendered
    assert "songforge_generation_slots_in_use" in rendered


def test_capacity_dashboard_json_parses_and_references_the_capacity_thesis_metrics() -> (
    None
):
    """AC4: the capacity-thesis dashboard -- `sse_connected_listeners` growing with N
    while `radio_advances_total`/datastore-op rate stay flat, plus create-rate
    throughput."""
    dashboard = _find_one(MONITORING_DIR, name_hint="capacity", suffix=".json")
    data = json.loads(_read(dashboard))
    assert isinstance(data, dict)
    rendered = json.dumps(data)
    assert "songforge_sse_connected_listeners" in rendered
    assert "songforge_radio_advances_total" in rendered
    assert "songforge_jobs_created_total" in rendered


# ── AC5: env split -- Prometheus scrape config + docs ──────────────────────────────


def test_prometheus_scrape_config_exists_and_parses_with_a_songforge_job() -> None:
    config_path = _find_one(MONITORING_DIR, name_hint="prometheus", suffix=".yml")
    data = yaml.safe_load(_read(config_path))
    assert isinstance(data, dict)
    scrape_configs = data.get("scrape_configs")
    assert scrape_configs, "expected at least one entry under scrape_configs"
    job_names = [job.get("job_name", "") for job in scrape_configs]
    assert any("songforge" in name.lower() for name in job_names)


def test_monitoring_readme_documents_the_three_environment_split() -> None:
    readme = MONITORING_DIR / "README.md"
    assert readme.is_file(), f"expected {readme} documenting the env split"
    text = _read(readme).lower()
    assert "local" in text
    assert "staging" in text
    assert "prod" in text
    # AC5: prod is scraped by a managed free tier, not self-hosted Grafana/Prometheus.
    assert "grafana cloud" in text
