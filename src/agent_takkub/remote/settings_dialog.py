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

Two tunnel modes (addendum, user-confirmed — see
remote-control-plan/P3-addendum-2modes.md):
  * "named"  — user has their own domain: pick a cloudflared credentials
               .json, hostname auto-derived from a sibling config.yml.
  * "quick"  — no domain: cockpit spawns `cloudflared tunnel --url
               http://localhost:9999` itself and scrapes the random
               *.trycloudflare.com URL from stdout (`tunnel.py`'s existing
               `_scan_for_url`, reused via `TunnelConfig.type == "quick"`).

Third auth factor (addendum 2, user-confirmed): a password the user sets
here at Enable time, hashed via `auth.hash_password` before it ever reaches
`RemoteConfig`/disk — the plaintext lives only in this dialog's QLineEdit
for the duration of the click. It's never embedded in the pairing URL/QR,
so a leaked link alone still can't get in (see `auth.py` + `http_server.py`).
"""

from __future__ import annotations

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
) -> RemoteConfig:
    """Assemble a `RemoteConfig` from the dialog's inputs plus the fixed
    "middle of the road" defaults from task spec §2 — the user never has
    to see or tune these. `password_hash` must already be hashed (see
    `auth.hash_password`) — this function never sees the plaintext."""
    return RemoteConfig(
        enabled=False,  # caller flips this on only after a successful start
        mode=mode,
        bind_port=_FIXED_PORT,
        public_url=public_url.strip(),
        tunnel=TunnelConfig(
            type=tunnel_type,
            credentials_json=credentials_json.strip(),
            cloudflared_bin=cloudflared_bin.strip(),
        ),
        auto_start_tunnel=True,
        idle_expire_min=_IDLE_EXPIRE_MIN,
        lockout_after_fails=_LOCKOUT_AFTER_FAILS,
        password_hash=password_hash,
    )


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

        intro = QLabel("Control this cockpit's Lead from your phone over a Cloudflare tunnel.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ── tunnel mode: named (custom domain) vs quick (no domain) ──────
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
        self._tunnel_named.toggled.connect(self._on_tunnel_mode_toggled)

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

        bin_row = QHBoxLayout()
        self._cloudflared_bin_edit = QLineEdit(current.tunnel.cloudflared_bin)
        self._cloudflared_bin_edit.setPlaceholderText("blank = auto-detect (PATH or Browse…)")
        self._bin_browse_btn = QPushButton("Browse…")
        self._bin_browse_btn.clicked.connect(self._on_browse_cloudflared_bin)
        bin_row.addWidget(self._cloudflared_bin_edit, 1)
        bin_row.addWidget(self._bin_browse_btn, 0)
        self._form.addRow("cloudflared executable:", bin_row)

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
            "Preset: idle-expire 240min · lockout after 5 fails · cloudflared auto-start."
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

        self._on_tunnel_mode_toggled(not is_quick)
        self._render_state(pairing_url=current.pairing_url() if is_live else "")

    # ──────────────────────────────────────────────────────────────
    def _on_tunnel_mode_toggled(self, named_checked: bool) -> None:
        """Named ↔ Quick: credentials/public-url fields only make sense for
        Named; Quick needs neither (cockpit spawns the tunnel itself)."""
        self._form.setRowVisible(self._cred_row, named_checked)
        self._form.setRowVisible(self._public_url_edit, named_checked)
        self._quick_note.setVisible(not named_checked)

    def _render_state(self, *, pairing_url: str) -> None:
        self._toggle_btn.setText("Disable" if self._is_live else "Enable")
        editable = not self._is_live
        for w in (
            self._tunnel_named,
            self._tunnel_quick,
            self._browse_btn,
            self._public_url_edit,
            self._cloudflared_bin_edit,
            self._bin_browse_btn,
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

        is_named = self._tunnel_named.isChecked()
        tunnel_type = "cloudflared" if is_named else "quick"
        credentials_json = self._cred_edit.text().strip()
        public_url = self._public_url_edit.text().strip()
        cloudflared_bin = self._cloudflared_bin_edit.text().strip()
        password = self._password_edit.text()

        if is_named and not credentials_json:
            QMessageBox.warning(
                self, "Missing credentials", "Pick a cloudflared credentials .json file first."
            )
            return
        if is_named and not public_url:
            QMessageBox.warning(
                self,
                "Missing public URL",
                "Enter the tunnel's public URL (couldn't auto-derive it from a sibling config.yml).",
            )
            return
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
            credentials_json=credentials_json if is_named else "",
            public_url=public_url if is_named else "",
            cloudflared_bin=cloudflared_bin,
            mode=mode,
            password_hash=hash_password(password),
        )
        ok, msg, pairing_url = self._on_apply(config, True)
        if not ok:
            QMessageBox.critical(self, "Enable failed", msg or "Unknown error.")
            return
        self._is_live = True
        self._render_state(pairing_url=pairing_url)


__all__ = [
    "RemoteSettingsDialog",
    "build_config",
    "derive_cloudflared_bin",
    "derive_hostname",
]
