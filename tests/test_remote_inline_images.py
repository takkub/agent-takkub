"""#424 — Remote inline image preview + compact-summary pill + resume picker
teammate filter for one-shot spawns.

* `api.lead_image_path`: extension whitelist, root confinement (project cwd /
  RUNTIME_DIR), magic bytes, size cap — every rejection is a bare 404.
* `/api/image` route: bearer-gated, streams bytes with nosniff + no-store.
* `notify`: a Claude `isCompactSummary` record never surfaces as user prose
  (history shows a short `sys` pill instead) and a session whose first typed
  line is the one-shot spawn trigger is a teammate, not a Lead candidate.
* PWA: image-card + lightbox wiring exists and the CSP allows blob: images.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import ClassVar

import pytest

from agent_takkub.remote import api, http_server
from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.config import RemoteConfig

_PNG = b"\x89PNG\r\n\x1a\n" + b"px" * 8
_STATIC = Path(api.__file__).parent / "static"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(api._config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
    monkeypatch.setattr(
        api._config, "lead_cwd", lambda name=None: str(cwd) if name == "proj-a" else ""
    )
    return cwd, runtime


class TestLeadImagePath:
    def test_absolute_png_under_project_cwd(self, roots):
        cwd, _ = roots
        f = cwd / "shot.png"
        f.write_bytes(_PNG)
        out = api.lead_image_path("proj-a", str(f))
        assert out["ok"] is True
        assert Path(out["path"]) == f.resolve()
        assert out["mime"] == "image/png"
        assert out["size"] == len(_PNG)

    def test_relative_path_resolves_against_project_cwd(self, roots):
        cwd, _ = roots
        (cwd / "docs").mkdir()
        (cwd / "docs" / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 8)
        out = api.lead_image_path("proj-a", "docs/a.jpg")
        assert out["mime"] == "image/jpeg"

    def test_quoted_and_backticked_paths_are_unwrapped(self, roots):
        cwd, _ = roots
        f = cwd / "shot.png"
        f.write_bytes(_PNG)
        for wrapped in (f'"{f}"', f"`{f}`", f"'{f}'"):
            assert api.lead_image_path("proj-a", wrapped)["ok"] is True

    def test_runtime_dir_is_an_allowed_root(self, roots):
        _, runtime = roots
        f = runtime / "exports" / "remote-x.webp"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        assert api.lead_image_path(None, str(f))["mime"] == "image/webp"

    @pytest.mark.parametrize(
        "bad",
        [None, "", "   ", 12, "x" * 1100, "notes.txt", "logo.svg", "../../etc/passwd.png"],
    )
    def test_non_image_or_malformed_is_404(self, roots, bad):
        with pytest.raises(api.RemoteApiError) as exc:
            api.lead_image_path("proj-a", bad)
        assert exc.value.status == 404

    def test_outside_every_root_is_404_even_if_real_image(self, roots, tmp_path):
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(_PNG)
        with pytest.raises(api.RemoteApiError) as exc:
            api.lead_image_path("proj-a", str(outside))
        assert exc.value.status == 404

    def test_traversal_out_of_cwd_is_404(self, roots, tmp_path):
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(_PNG)
        with pytest.raises(api.RemoteApiError):
            api.lead_image_path("proj-a", "../elsewhere.png")

    def test_wrong_magic_bytes_is_404(self, roots):
        cwd, _ = roots
        (cwd / "fake.png").write_bytes(b"<html>not a png</html>")
        with pytest.raises(api.RemoteApiError):
            api.lead_image_path("proj-a", str(cwd / "fake.png"))

    def test_oversized_is_404(self, roots, monkeypatch):
        cwd, _ = roots
        (cwd / "big.png").write_bytes(_PNG)
        monkeypatch.setattr(api, "_MAX_REMOTE_IMAGE_BYTES", 4)
        with pytest.raises(api.RemoteApiError):
            api.lead_image_path("proj-a", str(cwd / "big.png"))

    def test_missing_file_is_404(self, roots):
        cwd, _ = roots
        with pytest.raises(api.RemoteApiError):
            api.lead_image_path("proj-a", str(cwd / "nope.png"))

    def test_relative_path_without_known_project_is_404(self, roots):
        with pytest.raises(api.RemoteApiError):
            api.lead_image_path("unknown", "shot.png")


class _FakeOrch:
    _lead_token = "lead-tok"

    def _resolve_project(self, project):
        return "default"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(api, "usage", lambda: {"providers": []})
    config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
    srv = http_server.start_server(config, _FakeOrch())
    yield srv
    srv.stop()


def _get(srv, path, headers=None):
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", headers=headers or {}),
            timeout=5,
        ) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _run_pumped(fn):
    import threading

    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(value=fn()))
    t.start()
    deadline = time.time() + 5
    while t.is_alive() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    t.join(timeout=1)
    return result["value"]


class TestImageRoute:
    def test_no_bearer_is_404(self, server):
        status, _, _ = _get(server, "/sek/api/image?path=x.png")
        assert status == 404

    def test_streams_validated_image_in_view_mode(self, server, monkeypatch, tmp_path):
        f = tmp_path / "shot.png"
        f.write_bytes(_PNG)
        seen: dict = {}

        def _fake(project, path):
            seen.update(project=project, path=path)
            return {"ok": True, "path": str(f), "mime": "image/png", "size": len(_PNG)}

        monkeypatch.setattr(api, "lead_image_path", _fake)
        status, headers, body = _run_pumped(
            lambda: _get(
                server,
                "/sek/api/image?path=C%3A%5Cshot.png&project=default",
                {"Authorization": "Bearer tok"},
            )
        )
        assert status == 200
        assert body == _PNG
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "no-store" in headers["Cache-Control"]
        assert "sandbox" in headers["Content-Security-Policy"]
        assert seen == {"project": "default", "path": "C:\\shot.png"}

    def test_rejected_path_is_bare_404(self, server, monkeypatch):
        def _fake(project, path):
            raise api.RemoteApiError(404, "not found")

        monkeypatch.setattr(api, "lead_image_path", _fake)
        status, _, _ = _run_pumped(
            lambda: _get(server, "/sek/api/image?path=secret.png", {"Authorization": "Bearer tok"})
        )
        assert status == 404

    def test_shell_csp_allows_blob_images(self):
        assert "img-src 'self' data: blob:" in http_server._CSP_HEADER


class TestCompactSummary:
    _REC: ClassVar[dict] = {
        "type": "user",
        "isCompactSummary": True,
        "message": {
            "role": "user",
            "content": "This session is being continued from a previous conversation…",
        },
    }

    def test_compact_summary_is_never_user_prose(self):
        assert notify_mod._lead_user_text(self._REC) is None
        assert notify_mod._claude_live_users(self._REC) == []

    def test_history_shows_a_short_sys_pill_instead(self, tmp_path):
        p = tmp_path / "s.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                }
            ),
            json.dumps(self._REC),
            json.dumps({"type": "user", "message": {"role": "user", "content": "ต่อเลย"}}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = notify_mod.read_recent_lead_messages(p, provider="claude")
        assert out == [
            {"text": "hi", "kind": "lead"},
            {"text": notify_mod._COMPACT_MARKER_TEXT, "kind": "sys"},
            {"text": "ต่อเลย", "kind": "me"},
        ]
        assert len(notify_mod._COMPACT_MARKER_TEXT) < 80


class TestResumePickerOneShotSpawnFilter:
    def test_trigger_first_line_is_teammate(self):
        assert notify_mod._is_teammate_session_line(notify_mod._SPAWN_TASK_TRIGGER)
        assert notify_mod._is_teammate_session_line("[ROLE: qa] run smoke")
        assert not notify_mod._is_teammate_session_line("takkub ma")

    def test_one_shot_teammate_sessions_are_filtered_from_claude_picker(
        self, tmp_path, monkeypatch
    ):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        proj_dir = tmp_path / "claude-config" / "projects" / "C--cwd"
        proj_dir.mkdir(parents=True)
        monkeypatch.setattr(notify_mod, "config_dir_for", lambda p: tmp_path / "claude-config")
        monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda project=None: str(cwd))
        monkeypatch.setattr(
            "agent_takkub.token_meter.session_project_dirs_for_cwd",
            lambda *a, **k: [proj_dir],
        )

        def _write(uuid, first_line, age):
            p = proj_dir / f"{uuid}.jsonl"
            p.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": first_line}})
                + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(p, (now - age, now - age))
            return p

        _write("lead-1", "takkub ma", 100)
        for i in range(5):
            _write(f"mate-{i}", notify_mod._SPAWN_TASK_TRIGGER, i)
        _write("mate-legacy", "[ROLE: backend] do it", 50)

        sessions = notify_mod._list_recent_claude_sessions("proj", limit=10)
        assert [s["uuid"] for s in sessions] == ["lead-1"]


class TestPwaWiring:
    def test_image_404_never_logs_the_phone_out(self):
        # #445: `/api/image` answers a bare 404 for any unservable path
        # (outside a project root, file gone, a pasted screenshot in the
        # CLI's own cache). `apiFetch` otherwise reads "404 while holding a
        # token" as "token revoked" and wipes the pairing — so switching to
        # a project whose history carries one stale image sent the user
        # back to the QR screen. The image fetch must opt out.
        js = (_STATIC / "app.js").read_text(encoding="utf-8")
        assert "apiFetch(q, { allow404: true })" in js
        assert "res.status === 404 && hadToken && !opts.allow404" in js

    def test_app_js_has_image_cards_and_lightbox(self):
        js = (_STATIC / "app.js").read_text(encoding="utf-8")
        assert "api/image?path=" in js
        assert "function hydrateImages" in js
        assert "function openLightbox" in js
        assert "wireLightbox();" in js
        assert "historyKind(m)" in js
        assert 'if (m.kind === "sys") return "sys";' in js

    def test_index_has_lightbox_dom_and_css(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="lightbox"' in html
        assert 'id="lightbox-img"' in html
        assert ".img-card" in html
        assert "touch-action: none" in html

    def test_sw_cache_bumped(self):
        sw = (_STATIC / "sw.js").read_text(encoding="utf-8")
        m = re.search(r"takkub-remote-shell-v(\d+)", sw)
        assert m and int(m.group(1)) >= 28

    def test_no_inline_script_added(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert html.count("<script") == 1
