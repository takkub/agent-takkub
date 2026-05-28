"""Claude Code auth override config.

The normal/default path is Claude Code's own login state (Max OAuth or
whatever `claude` already knows). This file only stores explicit cockpit
overrides for proxy/API-key setups, and blank values mean "use default".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_CONFIG_PATH = Path.home() / ".takkub" / "claude-auth.json"


@dataclass(frozen=True)
class ClaudeAuthConfig:
    base_url: str = ""
    api_key: str = ""
    auth_token: str = ""

    def active_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
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
    )


def save_claude_auth(config: ClaudeAuthConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "base_url": config.base_url.strip(),
        "api_key": config.api_key.strip(),
        "auth_token": config.auth_token.strip(),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def apply_claude_auth_overrides(env: dict[str, str]) -> None:
    """Mutate *env* with explicit Claude auth overrides, if configured.

    Blank config values deliberately do nothing, preserving Claude Code's
    default auth/login behavior and any parent env already present.
    """
    env.update(load_claude_auth().active_env())
