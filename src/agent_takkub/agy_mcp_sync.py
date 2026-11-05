"""Deliver a role's allowed user MCP servers to gemini/agy panes (#978).

agy has no session-scoped MCP channel. It reads one machine-wide
``~/.gemini/config/mcp_config.json`` (``serverUrl``/``headers`` for HTTP,
``command``/``args``/``env`` for stdio). Probed on agy 1.5.0 (2026-11-05): a
custom agent's ``plugins:`` entry pointing at a plugin with its own
``mcp_config.json`` does NOT start that plugin's servers, neither at boot nor
on the first turn — so the per-pane agent.md cannot carry them.

What this module does, at every agy pane spawn:

- Merges the servers the pane's role is allowed (``takkub mcp allow``) from
  the cockpit master ``shared-mcp.json`` into agy's file. Only servers the
  user added (``takkub mcp add``); cockpit-managed ones (browser MCPs, graft,
  ...) are never pushed machine-wide.
- Never touches an entry it did not write. Names it wrote are recorded in
  ``agy-mcps-synced.json`` next to the master file; an existing entry with
  the same name that is not in that record is the user's and is skipped.
- Drops an entry it wrote once the server is gone from the master
  (``takkub mcp remove``).

Known gap (shown to the Lead by ``takkub mcp allow/deny``): the file is
machine-wide, so a server synced for one role is visible to every agy
session on the machine, and ``takkub mcp deny`` cannot hide it from one agy
role. Never raises.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

_log = logging.getLogger(__name__)
_SYNCED_FILENAME = "agy-mcps-synced.json"


def agy_mcp_config_path() -> pathlib.Path:
    return pathlib.Path.home() / ".gemini" / "config" / "mcp_config.json"


def _synced_record_path() -> pathlib.Path:
    from . import shared_dev_tools as sdt

    return sdt.SHARED_MCP_FILE.parent / _SYNCED_FILENAME


def _read_synced() -> set[str]:
    try:
        data = json.loads(_synced_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = data.get("names") if isinstance(data, dict) else None
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def _atomic_write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".takkub-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def to_agy_entry(cfg: dict) -> dict | None:
    """Claude-style master entry → agy ``mcp_config.json`` entry.

    HTTP (``type: http``/``sse`` or a bare ``url``) maps to ``serverUrl`` +
    ``headers``; stdio keeps ``command``/``args``/``env``. Anything else →
    None (not convertible)."""
    if not isinstance(cfg, dict):
        return None
    url = cfg.get("url") or cfg.get("serverUrl")
    if isinstance(url, str) and url:
        out: dict = {"serverUrl": url}
        headers = cfg.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        return out
    command = cfg.get("command")
    if not isinstance(command, str) or not command:
        return None
    out = {"command": command}
    args = cfg.get("args")
    if isinstance(args, list):
        out["args"] = [str(a) for a in args]
    env = cfg.get("env")
    if isinstance(env, dict) and env:
        out["env"] = {str(k): str(v) for k, v in env.items()}
    return out


def sync_role_to_agy(role: str) -> dict:
    """Merge *role*'s allowed user MCPs into agy's config. Returns a summary
    ``{"added": [...], "updated": [...], "removed": [...], "skipped_user": [...],
    "unconvertible": [...], "error": str | None}``. Never raises."""
    summary: dict = {
        "added": [],
        "updated": [],
        "removed": [],
        "skipped_user": [],
        "unconvertible": [],
        "error": None,
    }
    try:
        from . import shared_dev_tools as sdt

        master = sdt.list_master_mcps()
        allow = sdt.role_mcp_allowlist(role) or frozenset()
        user_names = set(master) - set(sdt.MANAGED_MCP_NAMES)
        wanted = {n: master[n] for n in sorted(allow & user_names)}
        synced = _read_synced()
        if not wanted and not (synced - user_names):
            return summary

        path = agy_mcp_config_path()
        config: dict = {}
        if path.is_file():
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                # agy tolerates comments/trailing commas; we will not rewrite
                # a file we cannot round-trip.
                summary["error"] = f"{path} is not plain JSON — left untouched"
                return summary
            if not isinstance(config, dict):
                summary["error"] = f"{path} is not a JSON object — left untouched"
                return summary
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        changed = False

        for name in sorted(synced - user_names):  # removed from the master
            if name in servers:
                del servers[name]
                summary["removed"].append(name)
                changed = True
            synced.discard(name)

        for name, cfg in wanted.items():
            entry = to_agy_entry(cfg)
            if entry is None:
                summary["unconvertible"].append(name)
                continue
            if name in servers and name not in synced:
                summary["skipped_user"].append(name)
                continue
            if servers.get(name) == entry:
                synced.add(name)
                continue
            summary["updated" if name in servers else "added"].append(name)
            servers[name] = entry
            synced.add(name)
            changed = True

        if changed:
            config["mcpServers"] = servers
            _atomic_write_json(path, config)
        if synced != _read_synced():
            _atomic_write_json(_synced_record_path(), {"names": sorted(synced)})
    except Exception as exc:  # spawn must never fail over this
        summary["error"] = repr(exc)
        _log.warning("agy mcp sync for %s failed: %r", role, exc)
    return summary
