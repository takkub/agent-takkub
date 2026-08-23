"""Tests for `agent_takkub.remote.reports` (#367 Remote Reports store):
publish/list/revoke/rotate, token resolution, path containment (traversal,
absolute, Windows drive-relative, symlink escape), extension whitelist, and
the standalone-HTML validator. `RUNTIME_DIR`/`remote.config._PATH` are
isolated per test by `tests/conftest.py`'s autouse fixture — no manual
monkeypatching needed here.
"""

from __future__ import annotations

import json

import pytest

from agent_takkub.remote import reports
from agent_takkub.remote.config import RemoteConfig


def _html(tmp_path, name: str = "src.html", body: str = "<html><body>hi</body></html>") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestPublish:
    def test_publish_creates_file_and_token(self, tmp_path):
        src = _html(tmp_path)
        record, warnings = reports.publish(src, "status.html", "demo")
        assert warnings == []
        assert record.name == "status.html"
        assert len(record.token) >= 32
        dest = reports.reports_root("demo") / "status.html"
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == "<html><body>hi</body></html>"

    def test_republish_same_name_keeps_token_replaces_file(self, tmp_path):
        src1 = _html(tmp_path, "src1.html", "<html>v1</html>")
        record1, _ = reports.publish(src1, "status.html", "demo")
        src2 = _html(tmp_path, "src2.html", "<html>v2</html>")
        record2, _ = reports.publish(src2, "status.html", "demo")
        assert record2.token == record1.token
        dest = reports.reports_root("demo") / "status.html"
        assert dest.read_text(encoding="utf-8") == "<html>v2</html>"

    def test_republish_without_expires_keeps_existing_expiry(self, tmp_path):
        src = _html(tmp_path)
        first, _ = reports.publish(src, "status.html", "demo", expires="30d")
        assert first.expires is not None
        second, _ = reports.publish(src, "status.html", "demo")
        assert second.expires == first.expires

    def test_republish_with_expires_none_clears_expiry(self, tmp_path):
        src = _html(tmp_path)
        reports.publish(src, "status.html", "demo", expires="30d")
        second, _ = reports.publish(src, "status.html", "demo", expires="none")
        assert second.expires is None

    def test_republish_without_label_keeps_existing_label(self, tmp_path):
        src = _html(tmp_path)
        reports.publish(src, "status.html", "demo", label="แผน lottery")
        second, _ = reports.publish(src, "status.html", "demo")
        assert second.label == "แผน lottery"

    def test_publish_missing_source_raises(self, tmp_path):
        with pytest.raises(reports.ReportError):
            reports.publish(str(tmp_path / "nope.html"), "status.html", "demo")

    def test_publish_rejects_unsupported_extension(self, tmp_path):
        src = tmp_path / "script.exe"
        src.write_bytes(b"MZ")
        with pytest.raises(reports.ReportError):
            reports.publish(str(src), "script.exe", "demo")

    def test_publish_rejects_external_script(self, tmp_path):
        src = _html(tmp_path, body='<script src="https://evil.example/x.js"></script>')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_link(self, tmp_path):
        src = _html(tmp_path, body='<link href="http://evil.example/x.css">')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_img(self, tmp_path):
        src = _html(tmp_path, body='<img src="https://evil.example/x.png">')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_allows_inline_script_and_data_uri_img(self, tmp_path):
        body = '<script>alert(1)</script><img src="data:image/png;base64,AA==">'
        src = _html(tmp_path, body=body)
        record, warnings = reports.publish(src, "status.html", "demo")
        assert warnings == []
        assert record.name == "status.html"

    def test_publish_warns_over_5mb(self, tmp_path):
        src = tmp_path / "big.html"
        src.write_bytes(b"<html>" + b"x" * (5 * 1024 * 1024 + 1) + b"</html>")
        record, warnings = reports.publish(str(src), "big.html", "demo")
        assert record.name == "big.html"
        assert any("MB" in w for w in warnings)

    def test_publish_non_html_never_checked_for_external_refs(self, tmp_path):
        src = tmp_path / "notes.md"
        src.write_text("![img](https://evil.example/x.png)", encoding="utf-8")
        record, warnings = reports.publish(str(src), "notes.md", "demo")
        assert record.name == "notes.md"
        assert warnings == []

    def test_publish_rejects_protocol_relative_script(self, tmp_path):
        src = _html(tmp_path, body='<script src="//evil.example/x.js"></script>')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_iframe(self, tmp_path):
        src = _html(tmp_path, body='<iframe src="https://evil.example/"></iframe>')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_object(self, tmp_path):
        src = _html(tmp_path, body='<object data="https://evil.example/x"></object>')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_embed(self, tmp_path):
        src = _html(tmp_path, body='<embed src="https://evil.example/x">')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_css_import(self, tmp_path):
        src = _html(tmp_path, body='<style>@import url("https://evil.example/x.css");</style>')
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_rejects_external_css_background_url(self, tmp_path):
        src = _html(tmp_path, body="<style>body{background:url(http://evil.example/x.png)}</style>")
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", "demo")

    def test_publish_allows_relative_css_import(self, tmp_path):
        src = _html(tmp_path, body='<style>@import "local.css"; body{color:red}</style>')
        record, warnings = reports.publish(src, "status.html", "demo")
        assert warnings == []
        assert record.name == "status.html"


class TestSvgValidation:
    """Should-fix #4 (review 2026-08-23-367): `.svg` was on the extension
    whitelist but never ran through the external-reference check at all —
    it now gets the same check `.html` gets."""

    def _svg(
        self,
        tmp_path,
        name: str = "pic.svg",
        body: str = '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    ) -> str:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_publish_allows_plain_svg(self, tmp_path):
        src = self._svg(tmp_path)
        record, warnings = reports.publish(src, "pic.svg", "demo")
        assert warnings == []
        assert record.name == "pic.svg"

    def test_publish_allows_svg_inline_script(self, tmp_path):
        body = '<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'
        record, warnings = reports.publish(self._svg(tmp_path, body=body), "pic.svg", "demo")
        assert warnings == []
        assert record.name == "pic.svg"

    def test_publish_rejects_svg_external_script(self, tmp_path):
        body = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<script src="https://evil.example/x.js"></script></svg>'
        )
        with pytest.raises(reports.ReportError):
            reports.publish(self._svg(tmp_path, body=body), "pic.svg", "demo")

    def test_publish_rejects_svg_external_image(self, tmp_path):
        body = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<image href="https://evil.example/x.png"/></svg>'
        )
        with pytest.raises(reports.ReportError):
            reports.publish(self._svg(tmp_path, body=body), "pic.svg", "demo")


class TestNameValidation:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "../evil.html",
            "a/../../etc/passwd.html",
            "/etc/passwd.html",  # absolute (posix-shaped)
            "sub/dir.html",  # path separator
            "sub\\dir.html",  # windows separator
            "C:\\Windows\\evil.html",  # windows absolute
            "C:evil.html",  # windows drive-relative (no slash)
            "",
            ".hidden.html",
        ],
    )
    def test_publish_rejects_unsafe_name(self, tmp_path, bad_name):
        src = _html(tmp_path)
        with pytest.raises(reports.ReportError):
            reports.publish(src, bad_name, "demo")


class TestProjectNsValidation:
    """#31/#32-37 (CodeQL py/path-injection): `reports_root`/`_contained_path`
    now additionally prove containment via the `os.path.normpath()` +
    `str.startswith()` idiom CodeQL's own sink analysis recognizes, on top
    of the regex reject `_project_ns`/`_validate_report_name` already did.
    These pin that a name/project the regex rejects still raises/404s
    exactly as before — the extra barrier must not loosen anything."""

    @pytest.mark.parametrize(
        "bad_project",
        [
            "../evil",
            "a/../../etc",
            "/etc",
            "has space",
            ".hidden",
            "trailing.",
            "a\\b",
        ],
    )
    def test_reports_root_rejects_unsafe_project(self, bad_project):
        with pytest.raises(reports.ReportError):
            reports.reports_root(bad_project)

    @pytest.mark.parametrize("bad_project", ["../evil", "/etc", "has space"])
    def test_publish_rejects_unsafe_project(self, tmp_path, bad_project):
        src = _html(tmp_path)
        with pytest.raises(reports.ReportError):
            reports.publish(src, "status.html", bad_project)

    @pytest.mark.parametrize("bad_project", ["../evil", "/etc", "has space"])
    def test_resolve_rejects_unsafe_project_returns_none(self, tmp_path, bad_project):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve(bad_project, "status.html", "whatever") is None


class TestListRevokeRotate:
    def test_list_shares_sorted_by_name(self, tmp_path):
        reports.publish(_html(tmp_path, "a.html"), "b.html", "demo")
        reports.publish(_html(tmp_path, "c.html"), "a.html", "demo")
        names = [r.name for r in reports.list_shares("demo")]
        assert names == ["a.html", "b.html"]

    def test_list_shares_empty_project(self):
        assert reports.list_shares("nope") == []

    def test_revoke_removes_token_keeps_file(self, tmp_path):
        record, _ = reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.revoke("status.html", "demo") is True
        dest = reports.reports_root("demo") / "status.html"
        assert dest.is_file()
        assert reports.resolve("demo", "status.html", record.token) is None

    def test_revoke_unknown_name_returns_false(self):
        assert reports.revoke("nope.html", "demo") is False

    def test_revoke_delete_removes_file_too(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        reports.revoke("status.html", "demo", delete=True)
        dest = reports.reports_root("demo") / "status.html"
        assert not dest.exists()

    def test_rotate_changes_token_old_dies_new_works(self, tmp_path):
        record, _ = reports.publish(_html(tmp_path), "status.html", "demo")
        rotated = reports.rotate("status.html", "demo")
        assert rotated.token != record.token
        assert reports.resolve("demo", "status.html", record.token) is None
        assert reports.resolve("demo", "status.html", rotated.token) is not None

    def test_rotate_unknown_name_raises(self):
        with pytest.raises(reports.ReportError):
            reports.rotate("nope.html", "demo")


class TestResolve:
    def test_resolve_correct_token_returns_path(self, tmp_path):
        record, _ = reports.publish(_html(tmp_path), "status.html", "demo")
        path = reports.resolve("demo", "status.html", record.token)
        assert path is not None
        assert path.is_file()

    def test_resolve_wrong_token_returns_none(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve("demo", "status.html", "totally-wrong") is None

    def test_resolve_missing_token_returns_none(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve("demo", "status.html", None) is None
        assert reports.resolve("demo", "status.html", "") is None

    def test_resolve_unknown_name_returns_none(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve("demo", "other.html", "whatever") is None

    def test_resolve_unknown_project_returns_none(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve("other-project", "status.html", "whatever") is None

    def test_resolve_expired_returns_none(self, tmp_path):
        record, _ = reports.publish(_html(tmp_path), "status.html", "demo")
        shares_path = reports.reports_root("demo") / "_shares.json"
        data = json.loads(shares_path.read_text(encoding="utf-8"))
        data["status.html"]["expires"] = "2000-01-01T00:00:00+00:00"
        shares_path.write_text(json.dumps(data), encoding="utf-8")
        assert reports.resolve("demo", "status.html", record.token) is None

    def test_resolve_not_yet_expired_returns_path(self, tmp_path):
        record, _ = reports.publish(_html(tmp_path), "status.html", "demo", expires="30d")
        assert reports.resolve("demo", "status.html", record.token) is not None

    def test_resolve_traversal_name_returns_none(self, tmp_path):
        reports.publish(_html(tmp_path), "status.html", "demo")
        assert reports.resolve("demo", "../status.html", "whatever") is None

    def test_resolve_rejects_symlink_escaping_root(self, tmp_path):
        outside = tmp_path / "outside.html"
        outside.write_text("<html>secret</html>", encoding="utf-8")
        root = reports.reports_root("demo")
        root.mkdir(parents=True, exist_ok=True)
        link = root / "evil.html"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation not permitted in this environment")
        shares_path = root / "_shares.json"
        shares_path.write_text(
            json.dumps(
                {"evil.html": {"token": "tok123", "created": "x", "expires": None, "label": ""}}
            ),
            encoding="utf-8",
        )
        assert reports.resolve("demo", "evil.html", "tok123") is None


class TestBuildUrl:
    def test_build_url_empty_when_remote_not_configured(self):
        RemoteConfig().save()
        assert reports.build_url("demo", "status.html", "tok") == ""

    def test_build_url_full(self):
        RemoteConfig(public_url="https://takkub.example.com", secret_path="sek").save()
        url = reports.build_url("demo", "status.html", "tok")
        assert url == "https://takkub.example.com/sek/r/demo/status.html?k=tok"

    def test_build_url_strips_trailing_slash_on_public_url(self):
        RemoteConfig(public_url="https://takkub.example.com/", secret_path="sek").save()
        url = reports.build_url("demo", "status.html", "tok")
        assert url.startswith("https://takkub.example.com/sek/r/")


class TestRemoteStatusText:
    def test_disabled_reports_off(self, monkeypatch):
        RemoteConfig(enabled=False).save()
        text = reports.remote_status_text()
        assert "ปิดอยู่" in text

    def test_enabled_but_tunnel_down_reports_off(self, monkeypatch):
        from agent_takkub.remote import tunnel as tunnel_mod

        RemoteConfig(enabled=True).save()
        monkeypatch.setattr(tunnel_mod, "is_tunnel_alive", lambda: False)
        text = reports.remote_status_text()
        assert "ปิดอยู่" in text

    def test_enabled_and_tunnel_up_reports_on(self, monkeypatch):
        from agent_takkub.remote import tunnel as tunnel_mod

        RemoteConfig(enabled=True).save()
        monkeypatch.setattr(tunnel_mod, "is_tunnel_alive", lambda: True)
        text = reports.remote_status_text()
        assert "เปิดอยู่" in text
        assert "tunnel up" in text
