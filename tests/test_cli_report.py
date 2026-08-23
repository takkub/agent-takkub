"""Tests for `takkub report publish|list|revoke|rotate` (#367 Remote
Reports): CLI-level role gate, publish->GET 200 through a real (loopback)
`/r/` server, revoke->404, rotate old-dies-new-works, and the printed
remote-status line. `cmd_report` never talks to `cli_server` (see its
docstring) so these call `cli.main([...])` directly against the real
`agent_takkub.remote.reports` store — no socket/orchestrator mocking
needed. `RUNTIME_DIR`/`remote.config._PATH` are isolated per test by
`tests/conftest.py`'s autouse fixture.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from agent_takkub import cli
from agent_takkub.remote import http_server, reports
from agent_takkub.remote.config import RemoteConfig


class _FakeOrch:
    _lead_token = "lead-tok"

    def _resolve_project(self, project):
        return "default"


@pytest.fixture(autouse=True)
def _no_role_env(monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)


def _get_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _extract_url(printed: str) -> str:
    line = next(line for line in printed.splitlines() if line.startswith("URL:"))
    return line[len("URL: ") :]


@pytest.fixture
def live_server():
    """A real `/r/` server wired to `RemoteConfig` so `build_url()`'s output
    is an actually-fetchable loopback URL — `public_url` just points at this
    test server instead of a real tunnel domain, `build_url` doesn't care."""
    srv = http_server.start_server(
        RemoteConfig(bind_port=0, secret_path="sek", token="tok"), _FakeOrch()
    )
    RemoteConfig(
        enabled=True, public_url=f"http://127.0.0.1:{srv.port}", secret_path="sek", token="tok"
    ).save()
    yield srv
    srv.stop()


class TestPublish:
    def test_publish_then_get_returns_200(self, tmp_path, capsys, live_server):
        src = tmp_path / "status.html"
        src.write_text("<html>report</html>", encoding="utf-8")

        code = cli.main(
            ["report", "publish", str(src), "--name", "status.html", "--project", "demo"]
        )
        assert code == 0
        url = _extract_url(capsys.readouterr().out)
        assert _get_status(url) == 200

    def test_publish_prints_remote_off_status_when_not_configured(self, tmp_path, capsys):
        src = tmp_path / "status.html"
        src.write_text("<html>report</html>", encoding="utf-8")
        code = cli.main(
            ["report", "publish", str(src), "--name", "status.html", "--project", "demo"]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "ปิดอยู่" in out
        assert "local file only" in out

    def test_publish_default_name_is_source_filename(self, tmp_path, capsys):
        src = tmp_path / "weekly-report.html"
        src.write_text("<html>1</html>", encoding="utf-8")
        code = cli.main(["report", "publish", str(src), "--project", "demo"])
        assert code == 0
        assert reports.list_shares("demo")[0].name == "weekly-report.html"

    def test_publish_rejects_external_script(self, tmp_path, capsys):
        src = tmp_path / "bad.html"
        src.write_text('<script src="https://evil.example/x.js"></script>', encoding="utf-8")
        code = cli.main(["report", "publish", str(src), "--project", "demo"])
        assert code == 1
        assert "standalone" in capsys.readouterr().out

    def test_republish_same_name_keeps_url_alive(self, tmp_path, capsys, live_server):
        src = tmp_path / "status.html"
        src.write_text("<html>v1</html>", encoding="utf-8")
        cli.main(["report", "publish", str(src), "--name", "status.html", "--project", "demo"])
        url = _extract_url(capsys.readouterr().out)

        src.write_text("<html>v2</html>", encoding="utf-8")
        cli.main(["report", "publish", str(src), "--name", "status.html", "--project", "demo"])
        url2 = _extract_url(capsys.readouterr().out)

        assert url == url2
        assert _get_status(url) == 200


class TestList:
    def test_list_shows_published_reports(self, tmp_path, capsys):
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        cli.main(["report", "publish", str(src), "--project", "demo", "--label", "แผน lottery"])
        capsys.readouterr()

        code = cli.main(["report", "list", "--project", "demo"])
        assert code == 0
        out = capsys.readouterr().out
        assert "status.html" in out
        assert "แผน lottery" in out

    def test_list_empty_project_still_ok(self, capsys):
        code = cli.main(["report", "list", "--project", "no-reports-here"])
        assert code == 0
        assert "no published reports" in capsys.readouterr().out


class TestRevokeRotate:
    def test_revoke_kills_link_file_stays(self, tmp_path, capsys, live_server):
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        cli.main(["report", "publish", str(src), "--name", "status.html", "--project", "demo"])
        url = _extract_url(capsys.readouterr().out)
        assert _get_status(url) == 200

        code = cli.main(["report", "revoke", "status.html", "--project", "demo"])
        assert code == 0
        assert _get_status(url) == 404
        assert (reports.reports_root("demo") / "status.html").is_file()

    def test_revoke_unknown_name_fails(self, capsys):
        code = cli.main(["report", "revoke", "nope.html", "--project", "demo"])
        assert code == 1

    def test_rotate_old_link_dies_new_link_works(self, tmp_path, capsys, live_server):
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        cli.main(["report", "publish", str(src), "--name", "status.html", "--project", "demo"])
        old_url = _extract_url(capsys.readouterr().out)

        code = cli.main(["report", "rotate", "status.html", "--project", "demo"])
        assert code == 0
        new_url = _extract_url(capsys.readouterr().out)

        assert old_url != new_url
        assert _get_status(old_url) == 404
        assert _get_status(new_url) == 200


class TestRoleGate:
    def test_teammate_role_is_rejected(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        code = cli.main(["report", "publish", str(src), "--project", "demo"])
        assert code == 1
        assert "only lead can run" in capsys.readouterr().err
        assert reports.list_shares("demo") == []

    def test_lead_role_is_allowed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        code = cli.main(["report", "publish", str(src), "--project", "demo"])
        assert code == 0

    def test_unset_role_is_allowed_manual_terminal(self, tmp_path, capsys):
        src = tmp_path / "status.html"
        src.write_text("<html>x</html>", encoding="utf-8")
        code = cli.main(["report", "publish", str(src), "--project", "demo"])
        assert code == 0
