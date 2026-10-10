"""Read the selected Codex account without importing the UI profile manager.

Provider discovery imports codex_helper through provider_spec. Keeping this
small disk reader independent of user_profile avoids a provider_config cycle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import SETTINGS_HOME


def selected_codex_config_dir(project: str, settings_home: Path | None = None) -> Path | None:
    """Return the named Codex account directory recorded for *project*."""
    home = settings_home or SETTINGS_HOME
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", project)
    slug = slug if slug.strip(".") else "default"
    try:
        selection = json.loads(
            (home / "projects" / slug / "user-profile.json").read_text(encoding="utf-8")
        )
        if not isinstance(selection, dict):
            return None
        providers = selection.get("providers", {})
        if not isinstance(providers, dict):
            return None
        name = str(providers.get("codex", providers.get("openai", "default"))).strip()
        if not name or name == "default":
            return None
        registry = json.loads((home / "user-profiles.json").read_text(encoding="utf-8"))
        if not isinstance(registry, list):
            return None
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider", "claude")).strip().lower()
            if provider == "openai":
                provider = "codex"
            if provider == "codex" and str(entry.get("name", "")).strip() == name:
                path = str(entry.get("config_dir", "")).strip()
                return Path(path) if path else None
    except (OSError, ValueError, TypeError):
        pass
    return None
