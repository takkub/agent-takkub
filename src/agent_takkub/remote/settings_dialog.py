"""settings_dialog.py — the 🌐 Remote status-bar chip's settings dialog.

Lives inside `remote/` (delete-to-uninstall, see `__init__.py`'s module
docstring): the chip and its click handler (`status_header.py` /
`user_actions.py`) reach this module only through `importlib.import_module`,
never a static import, so `rm -rf remote/` turns the chip into a silent
no-op instead of an ImportError at cockpit boot.

`RemoteSettingsDialog` never imports `MainWindow` — the caller injects an
`on_apply(config, enable) -> (ok, msg, pairing_url)` callable instead, so
this module stays fully decoupled from the cockpit shell's shape. The
pure helpers (`derive_hostname`, `derive_cloudflared_bin`, `build_config`)
take no Qt import and are tested without a QApplication; only the dialog
class itself touches Qt.

Two providers, three tunnel modes total (addendum, user-confirmed — see
remote-control-plan/P3-addendum-2modes.md):
  * Cloudflare "named" — user has their own domain: pick a cloudflared
               credentials .json, hostname auto-derived from a sibling
               config.yml.
  * Cloudflare "quick" — no domain: cockpit spawns `cloudflared tunnel
               --url http://localhost:9999` itself and scrapes the random
               *.trycloudflare.com URL from stdout (`tunnel.py`'s existing
               `_scan_for_url`, reused via `TunnelConfig.type == "quick"`).
  * ngrok (`TunnelConfig.type == "ngrok"`) — provider chosen instead of
               Cloudflare. "random" scrapes a `*.ngrok-free.app` URL from
               stdout the same way quick-tunnel does; "fixed" uses the
               user's reserved domain (`ngrok_domain`), known upfront. An
               optional authtoken field runs `ngrok config add-authtoken`
               once at Enable time (`_run_ngrok_authtoken`) so the user
               never has to open a terminal — the token itself is never
               persisted to `RemoteConfig`, only handed to that one-shot
               subprocess call.

Third auth factor (addendum 2, user-confirmed): a password the user sets
here at Enable time, hashed via `auth.hash_password` before it ever reaches
`RemoteConfig`/disk — the plaintext lives only in this dialog's QLineEdit
for the duration of the click. It's never embedded in the pairing URL/QR,
so a leaked link alone still can't get in (see `auth.py` + `http_server.py`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .auth import hash_password
from .config import RemoteConfig, TunnelConfig

# Fixed per task spec — the user only ever supplies the credentials .json
# (named mode) or nothing at all (quick mode).
_FIXED_PORT = 9999
_IDLE_EXPIRE_MIN = 240
_LOCKOUT_AFTER_FAILS = 5
# L4: a password reachable with just secret-path + token (it *is* the
# password gate) needs a minimum length so it can't be brute-forced in a
# practical window even under the PBKDF2 + lockout throttling in auth.py.
_MIN_PASSWORD_LENGTH = 8

_CLOUDFLARED_BIN_NAMES = ("cloudflared.exe", "cloudflared")

_CONTROL_WARNING = (
    "⚠ Control mode lets a paired phone send commands to Lead — anyone with\n"
    "the pairing URL can act as you. Only use it on a trusted device/network."
)
_PAIRING_WARNING = "⚠ Don't share this URL — it's the key to this machine."
_QUICK_TUNNEL_NOTE = (
    "Quick tunnel: cockpit runs cloudflared itself and gets a random\n"
    "*.trycloudflare.com address — no domain or credentials file needed."
)
_NGROK_NOTE = (
    "ngrok: cockpit runs `ngrok http 9999` itself. Leave the authtoken\n"
    "blank if this machine already has one set (`ngrok config\n"
    "add-authtoken ...`) — otherwise paste it here and it's applied\n"
    "automatically when you click Enable."
)


def derive_hostname(credentials_json: str) -> str | None:
    """Best-effort: peek at a `config.yml` sitting next to the user's
    cloudflared credentials file and pull the first `ingress[].hostname`
    out of it, so Public URL can be prefilled instead of copy-pasted by
    hand. Returns None on anything but a clean match — a malformed or
    missing sibling file just means "no auto-fill", never an error."""
    if not credentials_json:
        return None
    sibling = Path(credentials_json).parent / "config.yml"
    if not sibling.is_file():
        return None
    try:
        data = yaml.safe_load(sibling.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ingress = data.get("ingress")
    if not isinstance(ingress, list):
        return None
    for entry in ingress:
        if isinstance(entry, dict) and entry.get("hostname"):
            return str(entry["hostname"])
    return None


def derive_cloudflared_bin(credentials_json: str) -> str | None:
    """Best-effort: look for a cloudflared executable sitting next to the
    user's credentials file — a common setup is a tunnel repo folder
    holding both — so the field can be prefilled when cloudflared isn't on
    PATH. Returns None when nothing is found (user must Browse manually,
    e.g. for quick-tunnel mode where there's no credentials file to look
    beside)."""
    if not credentials_json:
        return None
    folder = Path(credentials_json).parent
    for name in _CLOUDFLARED_BIN_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def build_config(
    *,
    tunnel_type: str,
    credentials_json: str,
    public_url: str,
    cloudflared_bin: str,
    mode: str,
    password_hash: str,
    url_mode: str = "random",
    ngrok_domain: str = "",
    ngrok_bin: str = "",
) -> RemoteConfig:
    """Assemble a `RemoteConfig` from the dialog's inputs plus the fixed
    "middle of the road" defaults from task spec §2 — the user never has
    to see or tune these. `password_hash` must already be hashed (see
    `auth.hash_password`) — this function never sees the plaintext.
    `url_mode`/`ngrok_domain`/`ngrok_bin` are only meaningful for
    `tunnel_type == "ngrok"` — ignored (kept at their defaults) by the
    Cloudflare modes."""
    return RemoteConfig(
        enabled=False,  # caller flips this on only after a successful start
        mode=mode,
        bind_port=_FIXED_PORT,
        public_url=public_url.strip(),
        tunnel=TunnelConfig(
            type=tunnel_type,
            credentials_json=credentials_json.strip(),
            cloudflared_bin=cloudflared_bin.strip(),
            url_mode=url_mode,
            ngrok_domain=ngrok_domain.strip(),
            ngrok_bin=ngrok_bin.strip(),
        ),
        auto_start_tunnel=True,
        idle_expire_min=_IDLE_EXPIRE_MIN,
        lockout_after_fails=_LOCKOUT_AFTER_FAILS,
        password_hash=password_hash,
    )


def _run_ngrok_authtoken(token: str) -> tuple[bool, str]:
    """Best-effort ``ngrok config add-authtoken <token>`` so the user never
    has to open a terminal to set it up. Returns (ok, message) — a failure
    here is surfaced via `QMessageBox`, never a crash. Not called at all
    when the authtoken field is left blank (ngrok already configured on
    this machine, or the user is relying on an env-var-based setup)."""
    try:
        result = subprocess.run(
            ["ngrok", "config", "add-authtoken", token],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "").strip()
    return True, ""


class RemoteSettingsDialog(QDialog):
    """🌐 Remote chip's dialog: pick a tunnel mode, flip Enable/Disable.

    `on_apply` does the actual live start/stop (owned by MainWindow, which
    holds the `_remote` handle) — this dialog only collects input and
    displays whatever `on_apply` reports back.
    """

    def __init__(self, parent, *, is_live: bool, current: RemoteConfig, on_apply) -> None:
        super().__init__(parent)
        self._on_apply = on_apply
        self._is_live = is_live
        self.setWindowTitle("🌐 Remote Control")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        intro = QLabel("Control this cockpit's Lead from your phone over a tunnel.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ── provider: Cloudflare vs ngrok ─────────────────────────────────
        provider_row = QHBoxLayout()
        self._provider_cloudflare = QRadioButton("Cloudflare")
        self._provider_ngrok = QRadioButton("ngrok")
        self._provider_group = QButtonGroup(self)
        self._provider_group.addButton(self._provider_cloudflare)
        self._provider_group.addButton(self._provider_ngrok)
        is_ngrok_provider = current.tunnel.type == "ngrok"
        (self._provider_ngrok if is_ngrok_provider else self._provider_cloudflare).setChecked(True)
        provider_row.addWidget(self._provider_cloudflare)
        provider_row.addWidget(self._provider_ngrok)
        layout.addLayout(provider_row)
        self._provider_cloudflare.toggled.connect(self._refresh_visibility)

        # ── Cloudflare tunnel mode: named (custom domain) vs quick (none) ─
        tunnel_mode_row = QHBoxLayout()
        self._tunnel_named = QRadioButton("Named tunnel (I have a domain)")
        self._tunnel_quick = QRadioButton("Quick tunnel (no domain)")
        self._tunnel_group = QButtonGroup(self)
        self._tunnel_group.addButton(self._tunnel_named)
        self._tunnel_group.addButton(self._tunnel_quick)
        is_quick = current.tunnel.type == "quick"
        (self._tunnel_quick if is_quick else self._tunnel_named).setChecked(True)
        tunnel_mode_row.addWidget(self._tunnel_named)
        tunnel_mode_row.addWidget(self._tunnel_quick)
        layout.addLayout(tunnel_mode_row)
        self._tunnel_named.toggled.connect(self._refresh_visibility)

        self._form = QFormLayout()
        layout.addLayout(self._form)

        self._cred_row = QHBoxLayout()
        self._cred_edit = QLineEdit(current.tunnel.credentials_json)
        self._cred_edit.setReadOnly(True)
        self._cred_edit.setPlaceholderText("required for named tunnel — credentials .json")
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._on_browse_credentials)
        self._cred_row.addWidget(self._cred_edit, 1)
        self._cred_row.addWidget(self._browse_btn, 0)
        self._form.addRow("Credentials .json:", self._cred_row)

        self._public_url_edit = QLineEdit(current.public_url)
        self._public_url_edit.setPlaceholderText("https://your-tunnel-hostname.example.com")
        self._form.addRow("Public URL:", self._public_url_edit)

        self._quick_note = QLabel(_QUICK_TUNNEL_NOTE)
        self._quick_note.setWordWrap(True)
        self._quick_note.setStyleSheet("color:#71717a;")
        layout.addWidget(self._quick_note)

        self._bin_row = QHBoxLayout()
        self._cloudflared_bin_edit = QLineEdit(current.tunnel.cloudflared_bin)
        self._cloudflared_bin_edit.setPlaceholderText("blank = auto-detect (PATH or Browse…)")
        self._bin_browse_btn = QPushButton("Browse…")
        self._bin_browse_btn.clicked.connect(self._on_browse_cloudflared_bin)
        self._bin_row.addWidget(self._cloudflared_bin_edit, 1)
        self._bin_row.addWidget(self._bin_browse_btn, 0)
        self._form.addRow("cloudflared executable:", self._bin_row)

        # ── ngrok provider fields ─────────────────────────────────────────
        self._ngrok_token_edit = QLineEdit()
        self._ngrok_token_edit.setPlaceholderText(
            "optional — blank if already set (`ngrok config add-authtoken`)"
        )
        self._form.addRow("ngrok authtoken:", self._ngrok_token_edit)

        self._ngrok_url_mode_row = QHBoxLayout()
        self._ngrok_random = QRadioButton("Random")
        self._ngrok_fixed = QRadioButton("Fixed")
        self._ngrok_url_mode_group = QButtonGroup(self)
        self._ngrok_url_mode_group.addButton(self._ngrok_random)
        self._ngrok_url_mode_group.addButton(self._ngrok_fixed)
        (
            self._ngrok_fixed if current.tunnel.url_mode == "fixed" else self._ngrok_random
        ).setChecked(True)
        self._ngrok_url_mode_row.addWidget(self._ngrok_random)
        self._ngrok_url_mode_row.addWidget(self._ngrok_fixed)
        self._form.addRow("ngrok URL:", self._ngrok_url_mode_row)
        self._ngrok_fixed.toggled.connect(self._refresh_visibility)

        self._ngrok_domain_edit = QLineEdit(current.tunnel.ngrok_domain)
        self._ngrok_domain_edit.setPlaceholderText("e.g. takkub.ngrok-free.app")
        self._form.addRow("ngrok domain:", self._ngrok_domain_edit)

        self._ngrok_bin_row = QHBoxLayout()
        self._ngrok_bin_edit = QLineEdit(current.tunnel.ngrok_bin)
        self._ngrok_bin_edit.setPlaceholderText("blank = auto-detect (PATH or Browse…)")
        self._ngrok_bin_browse_btn = QPushButton("Browse…")
        self._ngrok_bin_browse_btn.clicked.connect(self._on_browse_ngrok_bin)
        self._ngrok_bin_row.addWidget(self._ngrok_bin_edit, 1)
        self._ngrok_bin_row.addWidget(self._ngrok_bin_browse_btn, 0)
        self._form.addRow("ngrok executable:", self._ngrok_bin_row)

        self._ngrok_note = QLabel(_NGROK_NOTE)
        self._ngrok_note.setWordWrap(True)
        self._ngrok_note.setStyleSheet("color:#71717a;")
        layout.addWidget(self._ngrok_note)

        port_label = QLabel(str(_FIXED_PORT))
        port_label.setStyleSheet("color:#71717a;")
        self._form.addRow("Port (fixed):", port_label)

        access_row = QHBoxLayout()
        self._access_view = QRadioButton("View (read-only, safe)")
        self._access_control = QRadioButton("Control (Lead commands)")
        self._access_group = QButtonGroup(self)
        self._access_group.addButton(self._access_view)
        self._access_group.addButton(self._access_control)
        (self._access_control if current.mode == "control" else self._access_view).setChecked(True)
        access_row.addWidget(self._access_view)
        access_row.addWidget(self._access_control)
        self._form.addRow("Access mode:", access_row)

        self._access_warning = QLabel(_CONTROL_WARNING)
        self._access_warning.setWordWrap(True)
        self._access_warning.setStyleSheet("color:#f59e0b;")
        self._access_warning.setVisible(current.mode == "control")
        layout.addWidget(self._access_warning)
        self._access_control.toggled.connect(self._access_warning.setVisible)

        # Third auth factor (addendum 2): required every time Enable is
        # clicked — only the hash is ever kept (see build_config/_on_toggle),
        # so there's no plaintext to prefill even when re-opening this dialog
        # while already live.
        pw_row = QHBoxLayout()
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText(
            f"required, min {_MIN_PASSWORD_LENGTH} chars — asked again on every Enable"
        )
        self._password_show_btn = QPushButton("👁")
        self._password_show_btn.setCheckable(True)
        self._password_show_btn.setFixedWidth(32)
        self._password_show_btn.toggled.connect(self._on_password_show_toggled)
        pw_row.addWidget(self._password_edit, 1)
        pw_row.addWidget(self._password_show_btn, 0)
        self._form.addRow("Password:", pw_row)

        password_note = QLabel(
            "Only the hash is ever stored — never in the pairing URL/QR, so a\n"
            "leaked link alone still can't get in without this password."
        )
        password_note.setWordWrap(True)
        password_note.setStyleSheet("color:#71717a;")
        layout.addWidget(password_note)

        defaults_note = QLabel(
            "Preset: idle-expire 240min · lockout after 5 fails · tunnel auto-start."
        )
        defaults_note.setWordWrap(True)
        defaults_note.setStyleSheet("color:#71717a;")
        layout.addWidget(defaults_note)

        self._toggle_btn = QPushButton()
        self._toggle_btn.clicked.connect(self._on_toggle)
        layout.addWidget(self._toggle_btn)

        self._pairing_label = QLabel(_PAIRING_WARNING)
        self._pairing_label.setWordWrap(True)
        self._pairing_label.setStyleSheet("color:#f59e0b;")
        self._pairing_edit = QLineEdit()
        self._pairing_edit.setReadOnly(True)
        self._copy_btn = QPushButton("📋 Copy")
        self._copy_btn.clicked.connect(self._on_copy)
        pairing_row = QHBoxLayout()
        pairing_row.addWidget(self._pairing_edit, 1)
        pairing_row.addWidget(self._copy_btn, 0)
        layout.addWidget(self._pairing_label)
        layout.addLayout(pairing_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh_visibility()
        self._render_state(pairing_url=current.pairing_url() if is_live else "")

    # ──────────────────────────────────────────────────────────────
    def _refresh_visibility(self, *_args) -> None:
        """Single source of truth for which rows are shown, driven by two
        independent choices: provider (Cloudflare vs ngrok) and, within
        Cloudflare, tunnel mode (Named vs Quick). Connected to every radio
        button whose state affects visibility, so it also runs as a plain
        no-arg call from `__init__` — `*_args` absorbs whichever `toggled`
        signal triggered it (bool) without caring about the value."""
        is_cloudflare = self._provider_cloudflare.isChecked()
        is_named = self._tunnel_named.isChecked()
        self._tunnel_named.setVisible(is_cloudflare)
        self._tunnel_quick.setVisible(is_cloudflare)
        self._form.setRowVisible(self._cred_row, is_cloudflare and is_named)
        self._form.setRowVisible(self._public_url_edit, is_cloudflare and is_named)
        self._quick_note.setVisible(is_cloudflare and not is_named)
        self._form.setRowVisible(self._bin_row, is_cloudflare)

        is_ngrok = not is_cloudflare
        is_fixed = self._ngrok_fixed.isChecked()
        self._form.setRowVisible(self._ngrok_token_edit, is_ngrok)
        self._form.setRowVisible(self._ngrok_url_mode_row, is_ngrok)
        self._form.setRowVisible(self._ngrok_domain_edit, is_ngrok and is_fixed)
        self._form.setRowVisible(self._ngrok_bin_row, is_ngrok)
        self._ngrok_note.setVisible(is_ngrok)

    def _render_state(self, *, pairing_url: str) -> None:
        self._toggle_btn.setText("Disable" if self._is_live else "Enable")
        editable = not self._is_live
        for w in (
            self._provider_cloudflare,
            self._provider_ngrok,
            self._tunnel_named,
            self._tunnel_quick,
            self._browse_btn,
            self._public_url_edit,
            self._cloudflared_bin_edit,
            self._bin_browse_btn,
            self._ngrok_token_edit,
            self._ngrok_random,
            self._ngrok_fixed,
            self._ngrok_domain_edit,
            self._ngrok_bin_edit,
            self._ngrok_bin_browse_btn,
            self._access_view,
            self._access_control,
            self._password_edit,
            self._password_show_btn,
        ):
            w.setEnabled(editable)
        if editable:
            # No plaintext survives a disable — always start blank so a
            # re-enable can't accidentally reuse a stale value on screen.
            self._password_edit.clear()
            self._ngrok_token_edit.clear()

        self._pairing_edit.setText(pairing_url)
        show_pairing = bool(pairing_url)
        self._pairing_label.setVisible(show_pairing)
        self._pairing_edit.setVisible(show_pairing)
        self._copy_btn.setVisible(show_pairing)

    def _on_password_show_toggled(self, checked: bool) -> None:
        self._password_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_browse_credentials(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select cloudflared credentials JSON", "", "JSON files (*.json)"
        )
        if not path:
            return
        self._cred_edit.setText(path)
        if not self._public_url_edit.text().strip():
            hostname = derive_hostname(path)
            if hostname:
                self._public_url_edit.setText(f"https://{hostname}")
        if not self._cloudflared_bin_edit.text().strip():
            cloudflared_bin = derive_cloudflared_bin(path)
            if cloudflared_bin:
                self._cloudflared_bin_edit.setText(cloudflared_bin)

    def _on_browse_cloudflared_bin(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select cloudflared executable")
        if path:
            self._cloudflared_bin_edit.setText(path)

    def _on_browse_ngrok_bin(self) -> None:
        # No filter, mirroring `_on_browse_cloudflared_bin` — a Mac ngrok
        # binary has no `.exe` extension a filter could match on.
        path, _ = QFileDialog.getOpenFileName(self, "Select ngrok executable")
        if path:
            self._ngrok_bin_edit.setText(path)

    def _on_copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._pairing_edit.text())

    def _on_toggle(self) -> None:
        if self._is_live:
            self._on_apply(None, False)
            self._is_live = False
            self._render_state(pairing_url="")
            return

        password = self._password_edit.text()
        if self._provider_cloudflare.isChecked():
            result = self._collect_cloudflare_fields()
        else:
            result = self._collect_ngrok_fields()
        if result is None:
            return
        (
            tunnel_type,
            credentials_json,
            public_url,
            cloudflared_bin,
            url_mode,
            ngrok_domain,
            ngrok_bin,
        ) = result

        if len(password) < _MIN_PASSWORD_LENGTH:
            QMessageBox.warning(
                self,
                "Password too short",
                f"Set a password of at least {_MIN_PASSWORD_LENGTH} characters — it's the "
                "last line of defense if the pairing URL leaks.",
            )
            return

        mode = "control" if self._access_control.isChecked() else "view"
        config = build_config(
            tunnel_type=tunnel_type,
            credentials_json=credentials_json,
            public_url=public_url,
            cloudflared_bin=cloudflared_bin,
            mode=mode,
            password_hash=hash_password(password),
            url_mode=url_mode,
            ngrok_domain=ngrok_domain,
            ngrok_bin=ngrok_bin,
        )
        ok, msg, pairing_url = self._on_apply(config, True)
        if not ok:
            QMessageBox.critical(self, "Enable failed", msg or "Unknown error.")
            return
        self._is_live = True
        self._render_state(pairing_url=pairing_url)

    def _collect_cloudflare_fields(self) -> tuple[str, str, str, str, str, str, str] | None:
        """Validate + gather the Cloudflare-provider fields for `_on_toggle`.
        Returns None (having already shown the warning) on a validation
        failure — same contract as `_collect_ngrok_fields`."""
        is_named = self._tunnel_named.isChecked()
        tunnel_type = "cloudflared" if is_named else "quick"
        credentials_json = self._cred_edit.text().strip()
        public_url = self._public_url_edit.text().strip()
        cloudflared_bin = self._cloudflared_bin_edit.text().strip()

        if is_named and not credentials_json:
            QMessageBox.warning(
                self, "Missing credentials", "Pick a cloudflared credentials .json file first."
            )
            return None
        if is_named and not public_url:
            QMessageBox.warning(
                self,
                "Missing public URL",
                "Enter the tunnel's public URL (couldn't auto-derive it from a sibling config.yml).",
            )
            return None
        return (
            tunnel_type,
            credentials_json if is_named else "",
            public_url if is_named else "",
            cloudflared_bin,
            "random",
            "",
            "",
        )

    def _collect_ngrok_fields(self) -> tuple[str, str, str, str, str, str, str] | None:
        """Validate + gather the ngrok-provider fields for `_on_toggle`,
        including the one-shot `ngrok config add-authtoken` call when a
        token was pasted in. Returns None (having already shown the
        warning/error) on a validation or authtoken failure."""
        ngrok_bin = self._ngrok_bin_edit.text().strip()
        if shutil.which("ngrok") is None and not ngrok_bin:
            QMessageBox.warning(
                self,
                "ngrok not found",
                "Install ngrok and make sure it's on PATH, then try again "
                "(or Browse to its executable above).",
            )
            return None
        url_mode = "fixed" if self._ngrok_fixed.isChecked() else "random"
        ngrok_domain = self._ngrok_domain_edit.text().strip()
        if url_mode == "fixed" and not ngrok_domain:
            QMessageBox.warning(
                self,
                "Missing domain",
                "Enter your reserved ngrok domain (e.g. takkub.ngrok-free.app).",
            )
            return None
        authtoken = self._ngrok_token_edit.text().strip()
        if authtoken:
            ok_token, err = _run_ngrok_authtoken(authtoken)
            if not ok_token:
                QMessageBox.critical(self, "ngrok authtoken failed", err or "Unknown error.")
                return None
        public_url = f"https://{ngrok_domain}" if url_mode == "fixed" else ""
        return "ngrok", "", public_url, "", url_mode, ngrok_domain, ngrok_bin


__all__ = [
    "RemoteSettingsDialog",
    "build_config",
    "derive_cloudflared_bin",
    "derive_hostname",
]
