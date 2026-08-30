"""RemoteConfig — persisted settings for the remote-control bolt-on.

P0 scaffold only: this defines the on-disk shape and safe load/save. Nothing
in P0 reads `enabled=true` as license to open a socket — see `__init__.py`.
Full field semantics: `remote-control-plan/2026-07-07-remote-control.md` §6.1.

State file: ``~/.takkub/remote.json`` (atomic tmp+rename, same pattern as
`exec_mode.py`). Missing file or corrupt JSON -> default (`enabled=False`),
and `load()` never creates the file — booting with remote off must cost zero
disk writes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import SETTINGS_HOME

_PATH = SETTINGS_HOME / "remote.json"

# The phone gets *pushed* Lead-only traffic (user directive 2026-07-23).
#
# A teammate's `takkub done` used to reach the phone as an unsolicited SSE
# push. With a normal fan-out (frontend + backend + qa + reviewer, sometimes
# sharded) that is a stream of notifications about work the user delegated
# precisely so they would not have to watch it — "เป็นขยะเยอะเกินไป". Lead is
# the one pane the user actually converses with, and Lead already summarises
# what the team did, so teammate push traffic is pure duplication on a
# 6-inch screen.
#
# This only gates the one *push* reader: `notify.LeadNotifier._on_done`
# (teammate `done` events dropped when on; Lead's own `done` always goes
# through). It says nothing about *pull* — the user opening the Pulse page
# themselves to check what's running — that's `PULSE_SHOW_TEAM` below (#200).
# Flip this to False to put teammate `done` notifications back on the phone.
LEAD_ONLY_STREAM = True

# The phone shows every open pane when the user *pulls* by opening the Pulse
# page (#200, 2026-08-14) — separate concern from `LEAD_ONLY_STREAM` above.
#
# The 2026-07-23 directive was about unsolicited push notifications, not
# about hiding the team's existence from a screen the user opened on purpose.
# A user who opens Pulse and sees only "Lead idle" while three teammates are
# actually mid-task has no way to tell the system is doing anything — the
# opposite of what Pulse is for. So pull stays separate and defaults to
# showing the team, even though push (LEAD_ONLY_STREAM) still defaults to
# Lead-only.
#
# Two readers gate on this flag: `api.activity` (roles list always [] when
# off — the phone-driven Pulse page) and `api.pulse` (count scoped down to
# the `lead` entry when off — the legacy `/api/pulse` route the shipped PWA
# no longer calls, kept live for any other authenticated client). Flip to
# False to go back to Lead-only visibility on pull too, matching the
# pre-#200 default when both flags are on.
PULSE_SHOW_TEAM = True


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


@dataclass
class TunnelConfig:
    type: str = "cloudflared"
    credentials_json: str = ""
    cloudflared_bin: str = ""
    # ngrok only (`type == "ngrok"`): "random" (cockpit scrapes the
    # *.ngrok-free.app URL from stdout, same as quick-tunnel mode) or
    # "fixed" (user's reserved domain, known upfront -> `ngrok_domain`).
    url_mode: str = "random"
    ngrok_domain: str = ""
    ngrok_bin: str = ""


@dataclass
class RemoteConfig:
    enabled: bool = False
    mode: str = "view"
    bind_port: int = 8899
    public_url: str = ""
    secret_path: str = ""
    token: str = ""
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    auto_start_tunnel: bool = True
    idle_expire_min: int = 240
    lockout_after_fails: int = 5
    tier2_terminal: bool = False
    # Third auth factor (addendum): PBKDF2 hash as "<salt-hex>$<digest-hex>"
    # (see `auth.hash_password`/`auth.verify_password`) — the plaintext
    # password never touches disk or this dataclass. Empty = feature off
    # (backward compatible with configs/tests created before this field).
    password_hash: str = ""
    # URL-only access (user request 2026-08-30): the phone opens the plain
    # `https://host/<secret>/` link — no `#token=` fragment, no pairing
    # screen. The bearer check is skipped server-side; the secret path (and
    # the password, still mandatory at Enable time) are the whole gate.
    # Default off — existing pairings keep working exactly as before.
    url_only_auth: bool = False

    @classmethod
    def load(cls) -> RemoteConfig:
        """Read `~/.takkub/remote.json`. Missing/corrupt -> default (off);
        never creates the file as a side effect of reading it."""
        if not _PATH.exists():
            return cls()
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            tunnel_data = data.pop("tunnel", None)
            if isinstance(tunnel_data, dict):
                known_tunnel = {
                    k: v for k, v in tunnel_data.items() if k in TunnelConfig.__dataclass_fields__
                }
                tunnel = TunnelConfig(**known_tunnel)
            else:
                tunnel = TunnelConfig()
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(tunnel=tunnel, **known)
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        """Persist atomically (tmp+rename), same pattern as `exec_mode.py`."""
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
        payload = json.dumps(asdict(self), indent=2) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        if os.name != "nt":
            tmp.chmod(0o600)
        tmp.replace(_PATH)

    def pairing_url(self) -> str:
        """URL to scan/open on the phone. Empty until `secret_path`/`token`
        exist (generated by `RemoteControl._start()` on first successful
        boot) — never guess a URL that wouldn't actually authenticate.
        `url_only_auth` drops the `#token=` fragment: the plain link IS the
        pairing."""
        if not (self.public_url and self.secret_path and self.token):
            return ""
        base = f"{self.public_url.rstrip('/')}/{self.secret_path}/"
        if self.url_only_auth:
            return base
        return f"{base}#token={self.token}"
