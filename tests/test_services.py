"""Tests for `services.py` — Docker Compose management.

All subprocess.run calls are mocked so no real Docker is required.
Tests cover:
  - detect_compose (file detection)
  - up / down / ps / logs return values
  - COMPOSE_PROJECT_NAME isolation env injection
  - timeout and error surfacing
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_takkub import services

# ── helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["docker", "compose"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── detect_compose ────────────────────────────────────────────────────────────


class TestDetectCompose:
    def test_finds_docker_compose_yml(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("version: '3'")
        assert services.detect_compose(tmp_path) == tmp_path / "docker-compose.yml"

    def test_finds_compose_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").write_text("services: {}")
        assert services.detect_compose(tmp_path) == tmp_path / "compose.yaml"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert services.detect_compose(tmp_path) is None

    def test_priority_order(self, tmp_path: Path) -> None:
        # docker-compose.yml takes priority over compose.yaml
        (tmp_path / "docker-compose.yml").write_text("a")
        (tmp_path / "compose.yaml").write_text("b")
        result = services.detect_compose(tmp_path)
        assert result == tmp_path / "docker-compose.yml"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        assert services.detect_compose(str(tmp_path)) is not None


# ── isolation: COMPOSE_PROJECT_NAME ──────────────────────────────────────────


class TestProjectSlug:
    def test_slug_appends_cockpit(self, tmp_path: Path) -> None:
        slug = services._project_slug(tmp_path)
        assert slug.endswith("-cockpit")

    def test_slug_is_lowercase_safe(self, tmp_path: Path) -> None:
        slug = services._project_slug(tmp_path)
        # Only lowercase, digits, hyphens
        assert all(c.isalnum() or c == "-" for c in slug)

    def test_different_dirs_produce_different_slugs(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "project-alpha"
        dir_b = tmp_path / "project-beta"
        dir_a.mkdir()
        dir_b.mkdir()
        assert services._project_slug(dir_a) != services._project_slug(dir_b)

    def test_base_env_injects_compose_project_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(services.os, "environ", {"PATH": "/usr/bin"})
        env = services._base_env(tmp_path)
        assert "COMPOSE_PROJECT_NAME" in env
        assert env["COMPOSE_PROJECT_NAME"].endswith("-cockpit")


# ── up ────────────────────────────────────────────────────────────────────────


class TestUp:
    def test_returns_error_when_no_compose_file(self, tmp_path: Path) -> None:
        ok, msg = services.up("proj", tmp_path)
        assert ok is False
        assert "no compose file" in msg

    def test_calls_docker_compose_up_detached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["env"] = kwargs.get("env", {})
            return _proc(0, stdout="started")

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        ok, _msg = services.up("proj", tmp_path)
        assert ok is True
        assert seen["args"] == ["docker", "compose", "up", "-d"]

    def test_isolation_env_injected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen["env"] = kwargs.get("env", {})
            return _proc(0)

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        services.up("proj", tmp_path)
        assert "COMPOSE_PROJECT_NAME" in seen["env"]
        assert seen["env"]["COMPOSE_PROJECT_NAME"].endswith("-cockpit")

    def test_surfaces_docker_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        monkeypatch.setattr(
            services.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="Cannot connect to Docker daemon"),
        )
        ok, msg = services.up("proj", tmp_path)
        assert ok is False
        assert "Docker daemon" in msg

    def test_timeout_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")

        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=30)

        monkeypatch.setattr(services.subprocess, "run", raise_timeout)
        ok, msg = services.up("proj", tmp_path)
        assert ok is False
        assert "timed out" in msg


# ── down ─────────────────────────────────────────────────────────────────────


class TestDown:
    def test_returns_error_when_no_compose_file(self, tmp_path: Path) -> None:
        ok, msg = services.down("proj", tmp_path)
        assert ok is False
        assert "no compose file" in msg

    def test_calls_docker_compose_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return _proc(0)

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        ok, _ = services.down("proj", tmp_path)
        assert ok is True
        assert seen["args"] == ["docker", "compose", "down"]


# ── ps ────────────────────────────────────────────────────────────────────────


class TestPs:
    def test_returns_empty_on_no_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _proc(0, stdout=""))
        assert services.ps("proj", tmp_path) == []

    def test_returns_empty_on_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            services.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="not running"),
        )
        assert services.ps("proj", tmp_path) == []

    def test_parses_json_array(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            [
                {"Name": "api", "State": "running", "Health": "healthy"},
                {"Name": "db", "State": "running", "Health": ""},
            ]
        )
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _proc(0, stdout=payload))
        result = services.ps("proj", tmp_path)
        assert len(result) == 2
        assert result[0].name == "api"
        assert result[0].state == "running"
        assert result[0].health == "healthy"
        assert result[1].health == ""

    def test_parses_newline_delimited_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines = "\n".join(
            [
                json.dumps({"Name": "web", "State": "running", "Health": "starting"}),
                json.dumps({"Name": "redis", "State": "running", "Health": ""}),
            ]
        )
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _proc(0, stdout=lines))
        result = services.ps("proj", tmp_path)
        assert len(result) == 2
        assert result[0].name == "web"
        assert result[0].health == "starting"

    def test_isolation_env_in_ps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen["env"] = kwargs.get("env", {})
            return _proc(0, stdout="[]")

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        services.ps("proj", tmp_path)
        assert seen["env"].get("COMPOSE_PROJECT_NAME", "").endswith("-cockpit")

    def test_state_lowercased(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps([{"Name": "svc", "State": "Running", "Health": "Healthy"}])
        monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: _proc(0, stdout=payload))
        result = services.ps("proj", tmp_path)
        assert result[0].state == "running"
        assert result[0].health == "healthy"


# ── logs ─────────────────────────────────────────────────────────────────────


class TestLogs:
    def test_returns_error_when_no_compose_file(self, tmp_path: Path) -> None:
        ok, msg = services.logs("proj", tmp_path)
        assert ok is False
        assert "no compose file" in msg

    def test_uses_tail_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")
        seen: dict = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return _proc(0, stdout="log output")

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        ok, _output = services.logs("proj", tmp_path, tail=100)
        assert ok is True
        assert "--tail=100" in seen["args"]

    def test_never_uses_follow_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")

        def fake_run(args, **kwargs):
            return _proc(0)

        monkeypatch.setattr(services.subprocess, "run", fake_run)
        with pytest.MonkeyPatch.context() as mp:
            seen: list = []
            mp.setattr(
                services.subprocess, "run", lambda args, **k: (seen.append(args), _proc(0))[1]
            )
            services.logs("proj", tmp_path, tail=10)
            assert "--follow" not in seen[0]
            assert "-f" not in seen[0]

    def test_docker_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "docker-compose.yml").write_text("")

        def raise_not_found(*a, **k):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(services.subprocess, "run", raise_not_found)
        ok, msg = services.logs("proj", tmp_path)
        assert ok is False
        assert "not found" in msg
