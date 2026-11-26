"""Home dev server shell scripts (scripts/server-*.sh).

Each script runs against a fake ``docker`` first on ``PATH`` that records every
invocation (one line of space-joined args per call) and answers the two queries the
scripts make: ``compose version --short`` and ``info -f {{.DockerRootDir}}``.
Nothing here talks to a real Docker daemon or the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

SECRETS = [
    "POSTGRES_PASSWORD",
    "S3_SECRET_ACCESS_KEY",
    "SESSION_SECRET",
    "WEBHOOK_SECRET",
    "GRAFANA_ADMIN_PASSWORD",
]

COMPLETE_ENV = {
    "PUBLIC_HOSTNAME": "dev.example.com",
    "TUNNEL_TOKEN": "tok",
    "POSTGRES_PASSWORD": "a" * 40,
    "S3_ACCESS_KEY_ID": "b" * 16,
    "S3_SECRET_ACCESS_KEY": "c" * 40,
    "SESSION_SECRET": "d" * 40,
    "WEBHOOK_SECRET": "e" * 40,
    "GRAFANA_ADMIN_PASSWORD": "f" * 40,
}

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1 $2 $3" = "compose version --short" ]; then
  echo "${FAKE_COMPOSE_VERSION:-2.40.0}"
elif [ "$1" = "info" ]; then
  echo /
fi
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def parse(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)


class Runner:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.log = tmp / "docker.log"
        self.log.touch()
        self.env_file = tmp / "server.env"
        self.cookie_file = tmp / "cookies.json"

    def __call__(
        self, script: str, *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        base = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "FAKE_DOCKER_LOG": str(self.log),
            "SERVER_ENV_FILE": str(self.env_file),
            "SERVER_COOKIE_FILE": str(self.cookie_file),
        }
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            input=stdin,
            env={**base, **(env or {})},
            capture_output=True,
            text=True,
            cwd=self.tmp,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def compose_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("compose") and "version" not in c]


@pytest.fixture
def run(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


@pytest.fixture
def env_file(run: Runner) -> Path:
    return run.env_file


@pytest.fixture
def complete_env(env_file: Path) -> Path:
    write_env(env_file, COMPLETE_ENV)
    return env_file


# ── server-init.sh ───────────────────────────────────────────────────────────────


def test_init_writes_secrets(run: Runner, env_file: Path) -> None:
    result = run("server-init.sh", "https://dev.example.com/", stdin="tok\n")
    assert result.returncode == 0, result.stderr
    env = parse(env_file)
    assert env["PUBLIC_HOSTNAME"] == "dev.example.com"
    assert env["TUNNEL_TOKEN"] == "tok"
    assert all(len(env[k]) == 40 for k in SECRETS)
    assert len(env["S3_ACCESS_KEY_ID"]) == 16
    assert len({env[k] for k in SECRETS}) == len(SECRETS)
    assert all(c in "0123456789abcdef" for k in SECRETS for c in env[k])
    assert env_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "raw", ["dev.example.com", "DEV.Example.com", "http://dev.example.com", "dev.example.com/x/"]
)
def test_init_normalises_hostname(run: Runner, env_file: Path, raw: str) -> None:
    assert run("server-init.sh", raw, stdin="tok\n").returncode == 0
    assert parse(env_file)["PUBLIC_HOSTNAME"] == "dev.example.com"


@pytest.mark.parametrize("raw", ["", "https://", "dev example.com", "dev_.example.com:443"])
def test_init_rejects_bad_hostname(run: Runner, env_file: Path, raw: str) -> None:
    result = run("server-init.sh", raw, stdin="tok\n")
    assert result.returncode == 1
    assert not env_file.exists()


def test_init_requires_token(run: Runner, env_file: Path) -> None:
    result = run("server-init.sh", "dev.example.com", stdin="\n")
    assert result.returncode == 1
    assert "token" in result.stderr.lower()
    assert not env_file.exists()


def test_init_keeps_existing_secrets(run: Runner, env_file: Path) -> None:
    run("server-init.sh", "dev.example.com", stdin="tok\n")
    before = env_file.read_bytes()
    result = run("server-init.sh", "other.example.com", stdin="tok2\n")
    assert result.returncode == 0, result.stderr
    assert env_file.read_bytes() == before


def test_init_fills_only_blank_values(run: Runner, env_file: Path) -> None:
    write_env(env_file, {**COMPLETE_ENV, "WEBHOOK_SECRET": "", "EXTRA": "keep-me"})
    env_file.write_text(env_file.read_text().replace("GRAFANA_ADMIN_PASSWORD=" + "f" * 40 + "\n", ""))
    result = run("server-init.sh", "other.example.com", stdin="tok2\n")
    assert result.returncode == 0, result.stderr
    env = parse(env_file)
    assert env["PUBLIC_HOSTNAME"] == "dev.example.com"
    assert env["TUNNEL_TOKEN"] == "tok"
    assert env["POSTGRES_PASSWORD"] == "a" * 40
    assert len(env["WEBHOOK_SECRET"]) == 40
    assert len(env["GRAFANA_ADMIN_PASSWORD"]) == 40
    assert env["EXTRA"] == "keep-me"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_init_does_not_print_secrets(run: Runner, env_file: Path) -> None:
    result = run("server-init.sh", "dev.example.com", stdin="tok-value-xyz\n")
    env = parse(env_file)
    out = result.stdout + result.stderr
    for key in [*SECRETS, "S3_ACCESS_KEY_ID", "TUNNEL_TOKEN"]:
        assert env[key] not in out, key


# ── server-up.sh ─────────────────────────────────────────────────────────────────


def test_up_without_env_fails_before_docker(run: Runner) -> None:
    result = run("server-up.sh")
    assert result.returncode == 1
    assert "server-init.sh" in result.stderr
    assert not any(" up " in f" {c} " for c in run.calls())


def test_up_with_blank_token_fails(run: Runner, env_file: Path) -> None:
    write_env(env_file, {**COMPLETE_ENV, "TUNNEL_TOKEN": ""})
    result = run("server-up.sh")
    assert result.returncode == 1
    assert "TUNNEL_TOKEN" in result.stderr
    assert not any(" up " in f" {c} " for c in run.calls())


def test_up_with_missing_variable_fails(run: Runner, env_file: Path) -> None:
    write_env(env_file, {k: v for k, v in COMPLETE_ENV.items() if k != "SESSION_SECRET"})
    result = run("server-up.sh")
    assert result.returncode == 1
    assert "SESSION_SECRET" in result.stderr


def test_up_error_does_not_print_values(run: Runner, env_file: Path) -> None:
    write_env(env_file, {**COMPLETE_ENV, "TUNNEL_TOKEN": ""})
    result = run("server-up.sh")
    assert "a" * 40 not in result.stdout + result.stderr


@pytest.mark.parametrize("version", ["2.20.0", "v2.24.3", "1.29.2"])
def test_up_rejects_old_compose(run: Runner, complete_env: Path, version: str) -> None:
    result = run("server-up.sh", env={"FAKE_COMPOSE_VERSION": version})
    assert result.returncode == 1
    assert "2.24.4" in result.stderr
    assert not run.compose_calls()


@pytest.mark.parametrize("version", ["2.24.4", "v2.40.0", "5.6.1", "2.100.0-desktop.1"])
def test_up_accepts_new_compose(run: Runner, complete_env: Path, version: str) -> None:
    result = run("server-up.sh", env={"FAKE_COMPOSE_VERSION": version})
    assert result.returncode == 0, result.stderr


def test_up_invokes_compose(run: Runner, complete_env: Path) -> None:
    result = run("server-up.sh")
    assert result.returncode == 0, result.stderr
    calls = run.compose_calls()
    up = next(c for c in calls if "up -d --build --remove-orphans" in c)
    assert "--profile tunnel" in up and "--profile monitoring" in up
    assert f"--env-file {complete_env}" in up
    assert f"-f {REPO}/docker-compose.yml -f {REPO}/deploy/server/compose.yml" in up
    assert calls[-1].endswith(" ps")


def test_up_respects_profile_override(run: Runner, complete_env: Path) -> None:
    run("server-up.sh", env={"SERVER_PROFILES": "monitoring"})
    up = next(c for c in run.compose_calls() if " up " in c)
    assert "--profile monitoring" in up and "tunnel" not in up


# ── server-loadtest.sh ───────────────────────────────────────────────────────────


@pytest.fixture
def cookies(run: Runner) -> Path:
    # A ready cookie file, so the wrapper never tries to mint identities.
    run.cookie_file.write_text('["c1", "c2"]')
    return run.cookie_file


def _docker_runs(run: Runner) -> list[str]:
    return [c for c in run.calls() if c.startswith("run ")]


def test_loadtest_runs_k6(run: Runner, complete_env: Path, cookies: Path) -> None:
    result = run("server-loadtest.sh", "create-rate")
    assert result.returncode == 0, result.stderr
    (k6,) = _docker_runs(run)
    assert "--network host" in k6
    assert "--ulimit nofile=65536:65536" in k6
    assert "-e BASE_URL=http://localhost:8000" in k6
    assert f"-v {REPO}/loadtest:/scripts:ro" in k6
    assert f"-v {cookies}:/cookies.json:ro" in k6
    assert "-e COOKIE_FILE=/cookies.json" in k6
    assert k6.endswith("grafana/k6:1.8.3 run /scripts/create-rate.js")


def test_loadtest_frontend_target(run: Runner, complete_env: Path, cookies: Path) -> None:
    run("server-loadtest.sh", "create-rate", env={"TARGET": "frontend"})
    (k6,) = _docker_runs(run)
    assert "-e BASE_URL=http://localhost:3000" in k6


def test_loadtest_unknown_target(run: Runner, complete_env: Path, cookies: Path) -> None:
    result = run("server-loadtest.sh", "create-rate", env={"TARGET": "web"})
    assert result.returncode == 1
    assert "TARGET" in result.stderr
    assert not _docker_runs(run)


def test_loadtest_forwards_tunables(run: Runner, complete_env: Path, cookies: Path) -> None:
    run("server-loadtest.sh", "realistic-session", env={"TARGET_USERS": "50", "DURATION": "30s"})
    (k6,) = _docker_runs(run)
    assert "-e TARGET_USERS=50" in k6
    assert "-e DURATION=30s" in k6
    assert "STEADY_MINUTES" not in k6


@pytest.mark.parametrize("name", ["nope", "create-rate.js", "../loadtest/create-rate", ""])
def test_loadtest_unknown_script(run: Runner, complete_env: Path, cookies: Path, name: str) -> None:
    result = run("server-loadtest.sh", name)
    assert result.returncode == 1
    assert "create-rate" in result.stderr and "realistic-session" in result.stderr
    assert not _docker_runs(run)


def test_loadtest_needs_env(run: Runner, cookies: Path) -> None:
    result = run("server-loadtest.sh", "create-rate")
    assert result.returncode == 1
    assert not _docker_runs(run)


def test_loadtest_low_disk_refuses(run: Runner, complete_env: Path, cookies: Path) -> None:
    result = run("server-loadtest.sh", "create-rate", env={"MIN_FREE_GB": "999999"})
    assert result.returncode == 1
    assert "FORCE=1" in result.stderr
    assert not _docker_runs(run)


def test_loadtest_force_overrides_disk(run: Runner, complete_env: Path, cookies: Path) -> None:
    result = run("server-loadtest.sh", "create-rate", env={"MIN_FREE_GB": "999999", "FORCE": "1"})
    assert result.returncode == 0, result.stderr
    assert len(_docker_runs(run)) == 1


@pytest.fixture
def quota_server():
    """A stand-in /quota that mints a fresh sf_uid cookie per request."""
    import itertools
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    counter = itertools.count()
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            n = next(counter)
            self.send_response(200)
            self.send_header("Set-Cookie", "other=x; Path=/")
            self.send_header("Set-Cookie", f"sf_uid=uid{n}.sig; HttpOnly; Secure; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", paths
    server.shutdown()


@pytest.mark.parametrize("existing", [None, '["none"]'])
def test_loadtest_mints_cookies(run: Runner, complete_env: Path, quota_server, existing) -> None:
    import json

    url, paths = quota_server
    if existing is not None:
        run.cookie_file.write_text(existing)
    result = run("server-loadtest.sh", "create-rate", env={"BASE_URL": url})
    assert result.returncode == 0, result.stderr
    minted = json.loads(run.cookie_file.read_text())
    assert len(minted) == 100 and len(set(minted)) == 100
    assert minted[0] == "uid0.sig"
    assert set(paths) == {"/quota"}
    assert run.cookie_file.stat().st_mode & 0o777 == 0o644
    (k6,) = _docker_runs(run)
    assert f"-e BASE_URL={url}" in k6


def test_loadtest_mint_failure_stops(run: Runner, complete_env: Path) -> None:
    result = run("server-loadtest.sh", "create-rate", env={"BASE_URL": "http://127.0.0.1:9"})
    assert result.returncode != 0
    assert not run.cookie_file.exists()
    assert not _docker_runs(run)


# ── server-wipe.sh ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer", ["y\n", "yes\n", "WIPE\n", " wipe\n", ""])
def test_wipe_requires_confirmation(run: Runner, complete_env: Path, answer: str) -> None:
    result = run("server-wipe.sh", stdin=answer)
    assert result.returncode == 1
    assert run.calls() == []


def test_wipe_needs_env(run: Runner) -> None:
    result = run("server-wipe.sh", stdin="wipe\n")
    assert result.returncode == 1
    assert run.calls() == []


def test_wipe_resets_volumes(run: Runner, complete_env: Path) -> None:
    result = run("server-wipe.sh", stdin="wipe\n")
    assert result.returncode == 0, result.stderr
    calls = run.compose_calls()
    down = next(i for i, c in enumerate(calls) if "down --volumes" in c)
    up = next(i for i, c in enumerate(calls) if "up -d --build --remove-orphans" in c)
    assert down < up
    assert "--profile tunnel" in calls[down] and "--profile monitoring" in calls[down]
