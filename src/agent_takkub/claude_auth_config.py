"""Claude Code auth override config.

The normal/default path is Claude Code's own login state (Max OAuth or
whatever `claude` already knows). This file only stores explicit cockpit
overrides for proxy/API-key setups, and blank values mean "use default".

Besides the three structured fields (base_url / api_key / auth_token), an
optional `extra_env` map lets the user inject arbitrary NAME=value env vars
into every spawned Claude pane (e.g. a backend's own key, a feature flag).
Like the auth fields, these are applied *after* the pane-env allowlist filter
(see orchestrator spawn), so they reach the pane regardless of the allowlist.
The three structured fields always win on a name collision so a stray row can
never clobber base_url / the auth headers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_CONFIG_PATH = Path.home() / ".takkub" / "claude-auth.json"


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


def config_path() -> Path:
    return _CONFIG_PATH


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


def load_claude_auth() -> ClaudeAuthConfig:
    path = config_path()
    if not path.exists():
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


def save_claude_auth(config: ClaudeAuthConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "base_url": config.base_url.strip(),
        "api_key": config.api_key.strip(),
        "auth_token": config.auth_token.strip(),
        "env": _clean_env_map(config.extra_env),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def apply_claude_auth_overrides(env: dict[str, str]) -> None:
    """Mutate *env* with explicit Claude auth overrides, if configured.

    Blank config values deliberately do nothing, preserving Claude Code's
    default auth/login behavior and any parent env already present.
    """
    env.update(load_claude_auth().active_env())
