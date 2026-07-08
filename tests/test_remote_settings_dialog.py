"""Tests for `agent_takkub.remote.settings_dialog` — the 🌐 Remote chip's
settings dialog. Pure helpers (`derive_hostname`, `derive_cloudflared_bin`,
`build_config`) need no QApplication; the dialog class itself does (the
session-scoped `qapp` fixture in conftest.py provides one).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_takkub.remote import settings_dialog as sd
from agent_takkub.remote.auth import verify_password
from agent_takkub.remote.config import RemoteConfig, TunnelConfig

# ---------------------------------------------------------------------------
# derive_hostname — sibling config.yml -> ingress[].hostname
# ---------------------------------------------------------------------------


class TestDeriveHostname:
    def test_reads_hostname_from_sibling_config_yml(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        (tmp_path / "config.yml").write_text(
            "ingress:\n  - hostname: agent-takkub.example.com\n    service: http://x\n",
            encoding="utf-8",
        )
        assert sd.derive_hostname(str(creds)) == "agent-takkub.example.com"

    def test_no_sibling_file_returns_none(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        assert sd.derive_hostname(str(creds)) is None

    def test_empty_path_returns_none(self):
        assert sd.derive_hostname("") is None

    def test_malformed_yaml_returns_none(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        (tmp_path / "config.yml").write_text("not: [valid, yaml", encoding="utf-8")
        assert sd.derive_hostname(str(creds)) is None

    def test_ingress_without_hostname_returns_none(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        (tmp_path / "config.yml").write_text(
            "ingress:\n  - service: http_status:404\n", encoding="utf-8"
        )
        assert sd.derive_hostname(str(creds)) is None


# ---------------------------------------------------------------------------
# derive_cloudflared_bin — sibling cloudflared(.exe)
# ---------------------------------------------------------------------------


class TestDeriveCloudflaredBin:
    def test_finds_cloudflared_exe_next_to_credentials(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        (tmp_path / "cloudflared.exe").write_text("", encoding="utf-8")
        assert sd.derive_cloudflared_bin(str(creds)) == str(tmp_path / "cloudflared.exe")

    def test_no_sibling_binary_returns_none(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        assert sd.derive_cloudflared_bin(str(creds)) is None

    def test_empty_path_returns_none(self):
        assert sd.derive_cloudflared_bin("") is None


# ---------------------------------------------------------------------------
# build_config — fixed defaults + hashed password only, never plaintext
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_named_mode_fields(self):
        cfg = sd.build_config(
            tunnel_type="cloudflared",
            credentials_json="/path/creds.json",
            public_url="https://x.example.com",
            cloudflared_bin="",
            mode="view",
            password_hash="salt$digest",
        )
        assert cfg.tunnel.type == "cloudflared"
        assert cfg.tunnel.credentials_json == "/path/creds.json"
        assert cfg.public_url == "https://x.example.com"
        assert cfg.bind_port == 9999
        assert cfg.mode == "view"
        assert cfg.enabled is False
        assert cfg.password_hash == "salt$digest"

    def test_fixed_defaults_are_not_user_editable(self):
        cfg = sd.build_config(
            tunnel_type="quick",
            credentials_json="",
            public_url="",
            cloudflared_bin="",
            mode="control",
            password_hash="x",
        )
        assert cfg.bind_port == 9999
        assert cfg.idle_expire_min == 240
        assert cfg.lockout_after_fails == 5
        assert cfg.auto_start_tunnel is True

    def test_strips_whitespace_from_paths_and_urls(self):
        cfg = sd.build_config(
            tunnel_type="cloudflared",
            credentials_json="  /path/creds.json  ",
            public_url="  https://x.example.com  ",
            cloudflared_bin="  /opt/cloudflared  ",
            mode="view",
            password_hash="h",
        )
        assert cfg.tunnel.credentials_json == "/path/creds.json"
        assert cfg.public_url == "https://x.example.com"
        assert cfg.tunnel.cloudflared_bin == "/opt/cloudflared"

    def test_ngrok_random_mode_fields(self):
        cfg = sd.build_config(
            tunnel_type="ngrok",
            credentials_json="",
            public_url="",
            cloudflared_bin="",
            mode="view",
            password_hash="h",
            url_mode="random",
            ngrok_domain="",
        )
        assert cfg.tunnel.type == "ngrok"
        assert cfg.tunnel.url_mode == "random"
        assert cfg.tunnel.ngrok_domain == ""
        assert cfg.public_url == ""

    def test_ngrok_fixed_mode_fields(self):
        cfg = sd.build_config(
            tunnel_type="ngrok",
            credentials_json="",
            public_url="https://takkub.ngrok-free.app",
            cloudflared_bin="",
            mode="control",
            password_hash="h",
            url_mode="fixed",
            ngrok_domain="  takkub.ngrok-free.app  ",
        )
        assert cfg.tunnel.type == "ngrok"
        assert cfg.tunnel.url_mode == "fixed"
        assert cfg.tunnel.ngrok_domain == "takkub.ngrok-free.app"
        assert cfg.public_url == "https://takkub.ngrok-free.app"

    def test_cloudflare_modes_default_to_random_url_mode(self):
        cfg = sd.build_config(
            tunnel_type="quick",
            credentials_json="",
            public_url="",
            cloudflared_bin="",
            mode="view",
            password_hash="h",
        )
        assert cfg.tunnel.url_mode == "random"
        assert cfg.tunnel.ngrok_domain == ""

    def test_ngrok_bin_defaults_to_empty(self):
        cfg = sd.build_config(
            tunnel_type="ngrok",
            credentials_json="",
            public_url="",
            cloudflared_bin="",
            mode="view",
            password_hash="h",
        )
        assert cfg.tunnel.ngrok_bin == ""

    def test_ngrok_bin_is_forwarded_and_stripped(self):
        cfg = sd.build_config(
            tunnel_type="ngrok",
            credentials_json="",
            public_url="",
            cloudflared_bin="",
            mode="view",
            password_hash="h",
            ngrok_bin="  /opt/homebrew/bin/ngrok  ",
        )
        assert cfg.tunnel.ngrok_bin == "/opt/homebrew/bin/ngrok"


# ---------------------------------------------------------------------------
# _run_ngrok_authtoken — one-shot `ngrok config add-authtoken` subprocess
# ---------------------------------------------------------------------------


class TestRunNgrokAuthtoken:
    def test_success(self, monkeypatch):
        calls = {}

        def _fake_run(argv, **kwargs):
            calls["argv"] = argv
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(sd.subprocess, "run", _fake_run)
        ok, msg = sd._run_ngrok_authtoken("tok_abc123")
        assert ok is True
        assert msg == ""
        assert calls["argv"] == ["ngrok", "config", "add-authtoken", "tok_abc123"]

    def test_nonzero_exit_reports_stderr(self, monkeypatch):
        def _fake_run(argv, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "invalid token"})()

        monkeypatch.setattr(sd.subprocess, "run", _fake_run)
        ok, msg = sd._run_ngrok_authtoken("bad")
        assert ok is False
        assert msg == "invalid token"

    def test_binary_missing_reports_the_oserror(self, monkeypatch):
        def _fake_run(argv, **kwargs):
            raise FileNotFoundError("ngrok not found")

        monkeypatch.setattr(sd.subprocess, "run", _fake_run)
        ok, msg = sd._run_ngrok_authtoken("tok")
        assert ok is False
        assert msg


# ---------------------------------------------------------------------------
# RemoteSettingsDialog — widget behavior (needs a QApplication)
# ---------------------------------------------------------------------------


def _default_config(**kw) -> RemoteConfig:
    return RemoteConfig(**kw)


class TestDialogInitialState:
    def test_starts_in_enable_state_when_not_live(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        assert dlg._toggle_btn.text() == "Enable"
        # Dialog is never .show()n in these tests, so isVisible() is always
        # False regardless of setVisible() calls — isHidden() reflects the
        # widget's own explicit visibility flag instead.
        assert dlg._pairing_edit.isHidden() is True

    def test_starts_in_disable_state_when_live_and_shows_pairing_url(self):
        cfg = _default_config(public_url="https://x.example.com", secret_path="sek", token="tok")
        dlg = sd.RemoteSettingsDialog(None, is_live=True, current=cfg, on_apply=MagicMock())
        assert dlg._toggle_btn.text() == "Disable"
        assert dlg._pairing_edit.text() == cfg.pairing_url()
        assert dlg._password_edit.isEnabled() is False

    def test_named_mode_defaults_to_visible_credentials_row(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        assert dlg._form.isRowVisible(dlg._cred_row) is True
        assert dlg._quick_note.isHidden() is True

    def test_quick_mode_hides_credentials_row(self):
        cfg = _default_config(tunnel=TunnelConfig(type="quick"))
        dlg = sd.RemoteSettingsDialog(None, is_live=False, current=cfg, on_apply=MagicMock())
        assert dlg._form.isRowVisible(dlg._cred_row) is False
        assert dlg._quick_note.isHidden() is False

    def test_toggling_tunnel_mode_flips_row_visibility(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        dlg._tunnel_quick.setChecked(True)
        assert dlg._form.isRowVisible(dlg._cred_row) is False
        dlg._tunnel_named.setChecked(True)
        assert dlg._form.isRowVisible(dlg._cred_row) is True


class TestDialogEnableValidation:
    def _dlg(self, on_apply=None, **cfg_kw):
        return sd.RemoteSettingsDialog(
            None,
            is_live=False,
            current=_default_config(**cfg_kw),
            on_apply=on_apply or MagicMock(),
        )

    def test_named_mode_without_credentials_warns_and_skips_apply(self, monkeypatch):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: None)
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        on_apply.assert_not_called()

    def test_missing_password_warns_and_skips_apply(self, monkeypatch, tmp_path):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply, tunnel=TunnelConfig(type="quick"))
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: None)
        dlg._on_toggle()
        on_apply.assert_not_called()

    def test_too_short_password_warns_and_skips_apply(self, monkeypatch):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply, tunnel=TunnelConfig(type="quick"))
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: None)
        dlg._password_edit.setText("short1")
        assert len("short1") < sd._MIN_PASSWORD_LENGTH
        dlg._on_toggle()
        on_apply.assert_not_called()

    def test_successful_enable_hashes_password_and_calls_on_apply(self):
        on_apply = MagicMock(return_value=(True, "", "https://pair.example.com/sek/#token=tok"))
        dlg = self._dlg(on_apply, tunnel=TunnelConfig(type="quick"))
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()

        on_apply.assert_called_once()
        config, enable = on_apply.call_args[0]
        assert enable is True
        assert config.tunnel.type == "quick"
        assert verify_password("hunter22", config.password_hash) is True
        assert dlg._is_live is True
        assert dlg._toggle_btn.text() == "Disable"
        assert dlg._pairing_edit.text() == "https://pair.example.com/sek/#token=tok"

    def test_failed_enable_shows_error_and_stays_off(self, monkeypatch):
        on_apply = MagicMock(return_value=(False, "boom", ""))
        dlg = self._dlg(on_apply, tunnel=TunnelConfig(type="quick"))
        dlg._password_edit.setText("hunter22")
        critical_calls = []
        monkeypatch.setattr(sd.QMessageBox, "critical", lambda *a, **kw: critical_calls.append(a))
        dlg._on_toggle()
        assert dlg._is_live is False
        assert dlg._toggle_btn.text() == "Enable"
        assert len(critical_calls) == 1

    def test_password_never_reaches_on_apply_in_plaintext(self):
        """The RemoteConfig passed to on_apply must carry only a hash."""
        on_apply = MagicMock(return_value=(True, "", ""))
        dlg = self._dlg(on_apply, tunnel=TunnelConfig(type="quick"))
        dlg._password_edit.setText("super-secret")
        dlg._on_toggle()
        config, _enable = on_apply.call_args[0]
        assert "super-secret" not in config.password_hash


class TestDialogDisable:
    def test_disable_calls_on_apply_with_none_and_false(self):
        on_apply = MagicMock(return_value=(True, "", ""))
        cfg = _default_config(public_url="https://x.example.com", secret_path="s", token="t")
        dlg = sd.RemoteSettingsDialog(None, is_live=True, current=cfg, on_apply=on_apply)
        dlg._on_toggle()
        on_apply.assert_called_once_with(None, False)
        assert dlg._is_live is False
        assert dlg._toggle_btn.text() == "Enable"
        assert dlg._pairing_edit.isHidden() is True

    def test_disable_clears_the_password_field(self):
        cfg = _default_config(public_url="https://x.example.com", secret_path="s", token="t")
        dlg = sd.RemoteSettingsDialog(
            None, is_live=True, current=cfg, on_apply=MagicMock(return_value=(True, "", ""))
        )
        dlg._on_toggle()
        assert dlg._password_edit.text() == ""


class TestPasswordVisibilityToggle:
    def test_show_button_flips_echo_mode(self):
        from PyQt6.QtWidgets import QLineEdit

        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        assert dlg._password_edit.echoMode() == QLineEdit.EchoMode.Password
        dlg._password_show_btn.setChecked(True)
        assert dlg._password_edit.echoMode() == QLineEdit.EchoMode.Normal
        dlg._password_show_btn.setChecked(False)
        assert dlg._password_edit.echoMode() == QLineEdit.EchoMode.Password


# ---------------------------------------------------------------------------
# Provider (Cloudflare vs ngrok) — row visibility + Enable-time collection
# ---------------------------------------------------------------------------


class TestProviderToggle:
    def test_defaults_to_cloudflare_provider(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        assert dlg._provider_cloudflare.isChecked() is True
        assert dlg._form.isRowVisible(dlg._cred_row) is True
        assert dlg._form.isRowVisible(dlg._ngrok_token_edit) is False

    def test_ngrok_type_selects_ngrok_provider_on_open(self):
        cfg = _default_config(tunnel=TunnelConfig(type="ngrok"))
        dlg = sd.RemoteSettingsDialog(None, is_live=False, current=cfg, on_apply=MagicMock())
        assert dlg._provider_ngrok.isChecked() is True
        assert dlg._form.isRowVisible(dlg._ngrok_token_edit) is True
        assert dlg._tunnel_named.isVisible() is False
        assert dlg._form.isRowVisible(dlg._cred_row) is False

    def test_toggling_provider_flips_row_visibility(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        dlg._provider_ngrok.setChecked(True)
        assert dlg._form.isRowVisible(dlg._ngrok_token_edit) is True
        assert dlg._form.isRowVisible(dlg._bin_row) is False
        assert dlg._form.isRowVisible(dlg._ngrok_bin_row) is True
        dlg._provider_cloudflare.setChecked(True)
        assert dlg._form.isRowVisible(dlg._ngrok_token_edit) is False
        assert dlg._form.isRowVisible(dlg._bin_row) is True
        assert dlg._form.isRowVisible(dlg._ngrok_bin_row) is False

    def test_ngrok_bin_prefilled_from_current_config(self):
        cfg = _default_config(
            tunnel=TunnelConfig(type="ngrok", ngrok_bin="/opt/homebrew/bin/ngrok")
        )
        dlg = sd.RemoteSettingsDialog(None, is_live=False, current=cfg, on_apply=MagicMock())
        assert dlg._ngrok_bin_edit.text() == "/opt/homebrew/bin/ngrok"

    def test_browse_ngrok_bin_sets_the_field(self, monkeypatch):
        dlg = sd.RemoteSettingsDialog(
            None,
            is_live=False,
            current=_default_config(tunnel=TunnelConfig(type="ngrok")),
            on_apply=MagicMock(),
        )
        monkeypatch.setattr(
            sd.QFileDialog, "getOpenFileName", lambda *a, **kw: ("/usr/local/bin/ngrok", "")
        )
        dlg._on_browse_ngrok_bin()
        assert dlg._ngrok_bin_edit.text() == "/usr/local/bin/ngrok"

    def test_browse_ngrok_bin_no_filter_arg(self, monkeypatch):
        """Mac ngrok binaries have no `.exe` extension — a filter would make
        them unselectable, so this must mirror the unfiltered cloudflared
        Browse dialog exactly."""
        dlg = sd.RemoteSettingsDialog(
            None,
            is_live=False,
            current=_default_config(tunnel=TunnelConfig(type="ngrok")),
            on_apply=MagicMock(),
        )
        captured = {}

        def _fake_dialog(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "", ""

        monkeypatch.setattr(sd.QFileDialog, "getOpenFileName", _fake_dialog)
        dlg._on_browse_ngrok_bin()
        assert len(captured["args"]) <= 2  # (self-dialog, caption) — no filter string
        assert not captured["kwargs"]

    def test_ngrok_domain_row_only_visible_for_fixed(self):
        dlg = sd.RemoteSettingsDialog(
            None, is_live=False, current=_default_config(), on_apply=MagicMock()
        )
        dlg._provider_ngrok.setChecked(True)
        assert dlg._form.isRowVisible(dlg._ngrok_domain_edit) is False
        dlg._ngrok_fixed.setChecked(True)
        assert dlg._form.isRowVisible(dlg._ngrok_domain_edit) is True
        dlg._ngrok_random.setChecked(True)
        assert dlg._form.isRowVisible(dlg._ngrok_domain_edit) is False


class TestNgrokEnableValidation:
    def _dlg(self, on_apply=None, **cfg_kw):
        return sd.RemoteSettingsDialog(
            None,
            is_live=False,
            current=_default_config(tunnel=TunnelConfig(type="ngrok"), **cfg_kw),
            on_apply=on_apply or MagicMock(),
        )

    def test_missing_ngrok_binary_warns_and_skips_apply(self, monkeypatch):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: None)
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: None)
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        on_apply.assert_not_called()

    def test_manually_entered_ngrok_bin_skips_the_not_found_warning(self, monkeypatch):
        """A user-supplied path is proof enough — no PATH lookup needed, and
        this must hold on macOS where ngrok often lives in
        /opt/homebrew/bin or /usr/local/bin rather than being on PATH."""
        on_apply = MagicMock(return_value=(True, "", ""))
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: None)
        warn_calls = []
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: warn_calls.append(a))
        dlg._ngrok_bin_edit.setText("/opt/homebrew/bin/ngrok")
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        assert warn_calls == []
        on_apply.assert_called_once()
        config, _enable = on_apply.call_args[0]
        assert config.tunnel.ngrok_bin == "/opt/homebrew/bin/ngrok"

    def test_fixed_mode_without_domain_warns_and_skips_apply(self, monkeypatch):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **kw: None)
        dlg._ngrok_fixed.setChecked(True)
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        on_apply.assert_not_called()

    def test_random_mode_enable_builds_ngrok_config(self, monkeypatch):
        on_apply = MagicMock(return_value=(True, "", "https://pair.example.com/sek/#token=tok"))
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()

        on_apply.assert_called_once()
        config, enable = on_apply.call_args[0]
        assert enable is True
        assert config.tunnel.type == "ngrok"
        assert config.tunnel.url_mode == "random"
        assert config.public_url == ""

    def test_fixed_mode_enable_sets_public_url_upfront_without_scraping(self, monkeypatch):
        on_apply = MagicMock(return_value=(True, "", "https://pair.example.com/sek/#token=tok"))
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        dlg._ngrok_fixed.setChecked(True)
        dlg._ngrok_domain_edit.setText("takkub.ngrok-free.app")
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()

        config, _enable = on_apply.call_args[0]
        assert config.tunnel.type == "ngrok"
        assert config.tunnel.url_mode == "fixed"
        assert config.tunnel.ngrok_domain == "takkub.ngrok-free.app"
        assert config.public_url == "https://takkub.ngrok-free.app"

    def test_authtoken_is_applied_automatically_when_provided(self, monkeypatch):
        on_apply = MagicMock(return_value=(True, "", ""))
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        calls = []

        def _fake_authtoken(token):
            calls.append(token)
            return True, ""

        monkeypatch.setattr(sd, "_run_ngrok_authtoken", _fake_authtoken)
        dlg._ngrok_token_edit.setText("tok_abc123")
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()

        assert calls == ["tok_abc123"]
        on_apply.assert_called_once()

    def test_blank_authtoken_never_calls_the_subprocess_helper(self, monkeypatch):
        on_apply = MagicMock(return_value=(True, "", ""))
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        called = []
        monkeypatch.setattr(sd, "_run_ngrok_authtoken", lambda token: called.append(token))
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        assert called == []

    def test_failed_authtoken_shows_error_and_skips_apply(self, monkeypatch):
        on_apply = MagicMock()
        dlg = self._dlg(on_apply)
        monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/ngrok")
        monkeypatch.setattr(sd, "_run_ngrok_authtoken", lambda token: (False, "invalid token"))
        critical_calls = []
        monkeypatch.setattr(sd.QMessageBox, "critical", lambda *a, **kw: critical_calls.append(a))
        dlg._ngrok_token_edit.setText("bad-token")
        dlg._password_edit.setText("hunter22")
        dlg._on_toggle()
        on_apply.assert_not_called()
        assert len(critical_calls) == 1
