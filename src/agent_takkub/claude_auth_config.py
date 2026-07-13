"""Claude Code auth override config — stored *per user profile*.

The normal/default path is Claude Code's own login state (Max OAuth or
whatever `claude` already knows). This file only stores explicit cockpit
overrides for proxy/API-key setups, and blank values mean "use default".

**Per-profile storage:** the auth config lives *inside each profile's Claude
config dir* (the same folder `CLAUDE_CONFIG_DIR` points at), as
``<config_dir>/takkub-claude-auth.json``. The default profile uses
``~/.claude/takkub-claude-auth.json``. This way each login account carries its
own auth override — setting a base URL for the ``openrouter`` profile makes
*only* that profile's panes use the API, while a profile with no override keeps
its normal Claude CLI login. (Previously a single global file at
``~/.takkub/claude-auth.json`` was applied to every pane, so one base URL
turned every project into API mode — that file is now read only as a
back-compat fallback for the default profile until it is re-saved.)

Besides the three structured fields (base_url / api_key / auth_token), an
optional `extra_env` map lets the user inject arbitrary NAME=value env vars
into the profile's spawned Claude panes (e.g. a backend's own key, a feature
flag). Like the auth fields, these are applied *after* the pane-env allowlist
filter (see orchestrator spawn), so they reach the pane regardless of the
allowlist. The three structured fields always win on a name collision so a
stray row can never clobber base_url / the auth headers.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import SETTINGS_HOME as _SETTINGS_HOME

_AUTH_FILENAME = "takkub-claude-auth.json"
_DEFAULT_CONFIG_DIR = Path.home() / ".claude"
# Legacy single global file. Read (never written) as a back-compat source for
# the *default* profile only, so existing setups keep working until re-saved.
# Rooted at SETTINGS_HOME so an installed build never reads a dev checkout's
# auth override (installed SETTINGS_HOME has no legacy file — clean start).
_LEGACY_GLOBAL_PATH = _SETTINGS_HOME / "claude-auth.json"


@dataclass(frozen=True)
class ClaudeAuthConfig:
    base_url: str = ""
    api_key: str = ""
    auth_token: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    def active_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        # User-defined extra vars first; the dedicated fields below override
        # so a stray row can never clobber base_url / the auth headers.
        for name, value in self.extra_env.items():
            name = str(name).strip()
            if name and value:
                env[name] = str(value)
        if self.base_url.strip():
            env["ANTHROPIC_BASE_URL"] = self.base_url.strip()
        api_key = self.api_key.strip()
        auth_token = self.auth_token.strip()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        elif self.base_url.strip() and api_key:
            # Many Claude-compatible proxies document the value as an API
            # key, while others expect it as a Bearer token. When a custom
            # base URL is configured and no separate auth token is provided,
            # expose the same secret through both supported Claude Code envs.
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
        return env


def _is_default_dir(config_dir: Path | str | None) -> bool:
    """True when *config_dir* is the implicit default profile (``~/.claude``)."""
    if config_dir is None:
        return True
    try:
        return Path(config_dir).resolve() == _DEFAULT_CONFIG_DIR.resolve()
    except OSError:
        return Path(config_dir) == _DEFAULT_CONFIG_DIR


def config_path(config_dir: Path | str | None = None) -> Path:
    """Path to the auth file for the profile rooted at *config_dir*.

    ``None`` → the default profile (``~/.claude/takkub-claude-auth.json``).
    """
    base = Path(config_dir) if config_dir else _DEFAULT_CONFIG_DIR
    return base / _AUTH_FILENAME


def _clean_env_map(raw: object) -> dict[str, str]:
    """Coerce a loaded/raw `env` value into a clean {NAME: value} dict.

    Drops entries with a blank name; stringifies everything. Non-dict input
    (corrupt file, wrong type) yields an empty map — never blocks spawn."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        name = str(k).strip()
        if name:
            out[name] = str(v)
    return out


def load_claude_auth(config_dir: Path | str | None = None) -> ClaudeAuthConfig:
    """Load the auth override for the profile rooted at *config_dir*.

    ``None`` → default profile. If the per-profile file is missing, the default
    profile falls back to the legacy global file (back-compat); any other
    profile with no file resolves to a blank config (= normal Claude login).
    """
    path = config_path(config_dir)
    if not path.exists():
        if _is_default_dir(config_dir) and _LEGACY_GLOBAL_PATH.exists():
            path = _LEGACY_GLOBAL_PATH
        else:
            return ClaudeAuthConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ClaudeAuthConfig()
    if not isinstance(data, dict):
        return ClaudeAuthConfig()
    return ClaudeAuthConfig(
        base_url=str(data.get("base_url") or "").strip(),
        api_key=str(data.get("api_key") or "").strip(),
        auth_token=str(data.get("auth_token") or "").strip(),
        extra_env=_clean_env_map(data.get("env")),
    )


def save_claude_auth(config: ClaudeAuthConfig, config_dir: Path | str | None = None) -> None:
    path = config_path(config_dir)
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" and not parent_existed:
        path.parent.chmod(0o700)
    data = {
        "base_url": config.base_url.strip(),
        "api_key": config.api_key.strip(),
        "auth_token": config.auth_token.strip(),
        "env": _clean_env_map(config.extra_env),
    }
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_path, path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def apply_claude_auth_overrides(env: dict[str, str]) -> None:
    """Mutate *env* with the auth override for *this pane's* profile.

    The pane's profile is identified by ``CLAUDE_CONFIG_DIR`` (already injected
    by ``inject_user_profile_env`` for non-default profiles; absent = default
    profile → ``~/.claude``). Only that profile's auth file is applied, so a
    base URL set for one profile never leaks into another's panes.

    Blank config values deliberately do nothing, preserving Claude Code's
    default auth/login behavior and any parent env already present.
    """
    config_dir = env.get("CLAUDE_CONFIG_DIR") or None
    env.update(load_claude_auth(config_dir).active_env())
