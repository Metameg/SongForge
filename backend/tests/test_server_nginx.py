"""Home dev server front door (deploy/server/nginx.conf).

cloudflared's single route points at lb:80; this config splits it into audio (MinIO) and
everything else (the frontend replicas). The syntax check runs the pinned nginx image
(skipped without a Docker daemon); the rest reads the file so the deliberate connection
sizing and the streaming settings cannot silently regress.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONF = REPO / "deploy" / "server" / "nginx.conf"
NGINX_IMAGE = "nginx:1.27-alpine"  # same image as the base `lb` service


def _text() -> str:
    # Drop comments so prose can't satisfy (or break) a directive match.
    return "\n".join(line.split("#", 1)[0] for line in CONF.read_text().splitlines())


def _directive(name: str, text: str | None = None) -> list[str]:
    return re.findall(rf"^\s*{name}\s+([^;]+);", text if text is not None else _text(), re.M)


def _location(prefix: str) -> str:
    match = re.search(rf"location\s+{re.escape(prefix)}\s*\{{(.*?)\}}", _text(), re.S)
    assert match, f"no `location {prefix}` block"
    return match.group(1)


def _docker_daemon() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_daemon(), reason="needs a Docker daemon")
def test_config_passes_nginx_t() -> None:
    # Checked first: a missing bind-mount source makes Docker create it as a directory.
    assert CONF.is_file(), f"{CONF} missing"
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{CONF}:/etc/nginx/nginx.conf:ro", NGINX_IMAGE, "nginx", "-t"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_connection_limits_are_sized_for_held_open_streams() -> None:
    # Each SSE listener holds two nginx connections (client + upstream) for its lifetime.
    (connections,) = _directive("worker_connections")
    (nofile,) = _directive("worker_rlimit_nofile")
    assert int(connections) >= 16384
    assert int(nofile) >= 2 * int(connections)
    assert _directive("worker_processes") == ["auto"]


def test_audio_goes_to_minio_with_the_original_host() -> None:
    block = _location("/songforge-audio/")
    assert "minio:9000" in block
    # Presigned download signatures cover the host the browser used (dev.<domain>).
    assert "Host $http_host" in _directive("proxy_set_header", block)
    assert _directive("proxy_buffering", block) == ["off"]


def test_everything_else_goes_to_the_frontend_as_a_stream() -> None:
    block = _location("/")
    assert "frontend:3000" in block
    assert _directive("proxy_buffering", block) == ["off"]
    (timeout,) = _directive("proxy_read_timeout", block)
    assert int(timeout.rstrip("s")) >= 3600
    # Never replay a POST on another replica; only retry requests that got no response.
    (retry,) = _directive("proxy_next_upstream", block)
    assert "non_idempotent" not in retry


def test_upstreams_are_re_resolved_as_replicas_change() -> None:
    # A literal proxy_pass host is resolved once at startup; a variable uses the resolver.
    assert _directive("resolver") and _directive("resolver")[0].startswith("127.0.0.11")
    for prefix in ("/", "/songforge-audio/"):
        (target,) = _directive("proxy_pass", _location(prefix))
        assert target.startswith("http://$"), prefix


def test_health_endpoint_is_local() -> None:
    block = _location("= /lb-health")
    assert _directive("return", block)[0].startswith("200")
