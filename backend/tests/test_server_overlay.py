"""Home dev server Compose overlay (deploy/server/compose.yml).

Renders the merged config of the unchanged base ``docker-compose.yml`` plus the server
overlay with ``docker compose config`` and asserts the server-specific contract: only
loopback ports, restarts, simulator + no rate limits, tunnel-facing URLs, required
secrets. Needs only the ``docker compose`` CLI (no daemon); skipped when it is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OVERLAY = REPO / "deploy" / "server" / "compose.yml"

REQUIRED = [
    "PUBLIC_HOSTNAME",
    "TUNNEL_TOKEN",
    "POSTGRES_PASSWORD",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "SESSION_SECRET",
    "WEBHOOK_SECRET",
    "GRAFANA_ADMIN_PASSWORD",
]

DUMMY = {
    "PUBLIC_HOSTNAME": "dev.example.com",
    "TUNNEL_TOKEN": "dummy-token",
    "POSTGRES_PASSWORD": "dummy-pg",
    "S3_ACCESS_KEY_ID": "dummy-access",
    "S3_SECRET_ACCESS_KEY": "dummy-s3-secret",
    "SESSION_SECRET": "dummy-session",
    "WEBHOOK_SECRET": "dummy-webhook",
    "GRAFANA_ADMIN_PASSWORD": "dummy-grafana",
}

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose CLI not available",
)


def _clean_env() -> dict[str, str]:
    # Compose lets shell variables beat --env-file, so start from a minimal environment.
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _config(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose",
            "-f", str(REPO / "docker-compose.yml"),
            "-f", str(OVERLAY),
            "--env-file", str(env_file),
            "--profile", "tunnel",
            "--profile", "monitoring",
            "config", "--format", "json",
        ],
        cwd=REPO,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )


def _write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> dict:
    env_file = _write_env(tmp_path_factory.mktemp("overlay") / ".env", DUMMY)
    result = _config(env_file)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_only_loopback_ports(cfg: dict) -> None:
    pub = {n: s["ports"] for n, s in cfg["services"].items() if s.get("ports")}
    assert set(pub) == {"lb", "prometheus", "grafana"}
    assert all(p["host_ip"] == "127.0.0.1" for ports in pub.values() for p in ports)


def test_lb_is_the_front_door_on_8080(cfg: dict) -> None:
    # The k6 wrapper targets localhost:8080 -- the visitor path minus Cloudflare.
    (port,) = cfg["services"]["lb"]["ports"]
    assert (port["published"], port["target"]) == ("8080", 80)


def test_front_door_port_is_fixed(tmp_path: Path) -> None:
    # Not overridable: the load-test wrapper hardcodes it.
    env = _write_env(tmp_path / ".env", {**DUMMY, "LB_HOST_PORT": "9999"})
    services = json.loads(_config(env).stdout)["services"]
    assert [p["published"] for p in services["lb"]["ports"]] == ["8080"]


def test_frontend_runs_two_replicas_behind_lb(cfg: dict) -> None:
    fe = cfg["services"]["frontend"]
    assert fe["deploy"]["replicas"] == 2
    assert not fe.get("ports")
    # The API hop is unchanged: the frontend spreads calls over the web replicas itself
    # (frontend/lib/backend.ts), like Railway's private-domain hop.
    assert fe["environment"]["BACKEND_URL"] == "http://web:8000"


def test_lb_uses_the_server_nginx_config(cfg: dict) -> None:
    mounts = [v for v in cfg["services"]["lb"]["volumes"] if v["target"] == "/etc/nginx/nginx.conf"]
    assert len(mounts) == 1
    assert mounts[0]["source"] == str(REPO / "deploy" / "server" / "nginx.conf")
    assert mounts[0]["read_only"] is True


def test_lb_waits_for_its_upstreams_and_has_a_healthcheck(cfg: dict) -> None:
    lb = cfg["services"]["lb"]
    for name in ("frontend", "minio"):
        assert lb["depends_on"][name]["condition"] == "service_healthy"
    assert "/lb-health" in " ".join(lb["healthcheck"]["test"])


def test_long_running_services_restart(cfg: dict) -> None:
    for name, svc in cfg["services"].items():
        assert svc.get("restart") == ("no" if name == "migrate" else "unless-stopped"), name


def test_backend_env(cfg: dict) -> None:
    for name in ("web", "worker", "migrate"):
        env = cfg["services"][name]["environment"]
        assert env["ENVIRONMENT"] == "staging"
        assert env["ENFORCE_RATE_LIMITS"] == "false"
        assert env["MUSICGPT_BASE_URL"] == "http://simulator:8080"
        assert env["S3_PUBLIC_BASE_URL"] == "https://dev.example.com/songforge-audio"
        assert env["S3_PRESIGN_ENDPOINT_URL"] == "https://dev.example.com"
        assert env["GLOBAL_GENERATION_CONCURRENCY"] == "50"
        assert env["WEBHOOK_SECRET"] == "dummy-webhook"
        assert env["SESSION_SECRET"] == "dummy-session"
        assert env["S3_SECRET_ACCESS_KEY"] == "dummy-s3-secret"


def test_secrets_reach_datastores(cfg: dict) -> None:
    s = cfg["services"]
    assert s["postgres"]["environment"]["POSTGRES_PASSWORD"] == "dummy-pg"
    assert s["minio"]["environment"]["MINIO_ROOT_USER"] == "dummy-access"
    assert s["minio"]["environment"]["MINIO_ROOT_PASSWORD"] == "dummy-s3-secret"
    assert s["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"] == "dummy-grafana"


def test_base_settings_survive_merge(cfg: dict) -> None:
    s = cfg["services"]
    assert s["web"]["environment"]["DB_PGBOUNCER_TRANSACTION_MODE"] == "true"
    assert "@postgres:5432" in s["worker"]["environment"]["DATABASE_URL"]
    assert ":dummy-pg@pgbouncer:6432" in s["web"]["environment"]["DATABASE_URL"]


def test_tunnel(cfg: dict) -> None:
    cf = cfg["services"]["cloudflared"]
    assert cf["image"] == "cloudflare/cloudflared:2026.10.7"
    assert cf["environment"]["TUNNEL_TOKEN"] == "dummy-token"
    assert cf["profiles"] == ["tunnel"]
    assert cf["command"] == ["tunnel", "--no-autoupdate", "run"]
    # The tunnel's single route is http://lb:80.
    assert set(cf["depends_on"]) == {"lb"}
    assert cf["depends_on"]["lb"]["condition"] == "service_healthy"


def test_project_name(cfg: dict) -> None:
    assert cfg["name"] == "songforge-server"


@pytest.mark.parametrize("var", REQUIRED)
def test_missing_variable_fails(var: str, tmp_path: Path) -> None:
    values = {k: v for k, v in DUMMY.items() if k != var}
    result = _config(_write_env(tmp_path / ".env", values))
    assert result.returncode != 0
    assert var in result.stderr


@pytest.mark.parametrize("var", REQUIRED)
def test_blank_variable_fails(var: str, tmp_path: Path) -> None:
    result = _config(_write_env(tmp_path / ".env", {**DUMMY, var: ""}))
    assert result.returncode != 0
    assert var in result.stderr
