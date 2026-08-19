"""First-use seeding of a non-claude provider's isolated home (2026-08-19).

``config.provider_home_env`` moves an installed cockpit's codex/opencode
state into DATA_HOME. Without this module that move would read to the user
as data loss: the pane comes up logged out, with no config and an empty
session picker, because everything it had is still sitting in ``~/.codex`` /
``~/.local/share/opencode``.

So the first time a provider is spawned under the isolated home, seed it
from the provider's OS-wide home using the SAME policy
``user_profile.bootstrap_default_profile`` already proved out for claude:

* **allowlist, never a whole-tree copy** — an unbounded copy of a provider
  home is what blocked cockpit startup for 8+ minutes on a 2.9 GB
  ``~/.claude`` (see that module's header). Anything unrecognised defaults
  to "don't copy" instead of silently ballooning again.
* **bounded session history** — only the newest
  :data:`RECENT_SESSIONS_CLONE` transcripts, each capped by
  :data:`_MAX_ITEM_BYTES`, so the Remote picker and history have something
  to show without dragging years of rollouts along.
* **atomic** — build in a ``.partial`` sibling, drop a completion marker,
  then one :func:`os.replace`. A kill mid-copy leaves a ``.partial`` that
  the next attempt discards, never a half-seeded home the provider would
  treat as real.
* **copy, never move** — the OS-wide home is left exactly as it was, so a
  user's own hand-run ``codex``/``opencode`` outside the cockpit is
  untouched and rolling back is just deleting the isolated dir.

Credentials ARE copied here, unlike claude's clone. That asymmetry is
deliberate: claude's login is per-CLAUDE_CONFIG_DIR and cheap to redo
(``claude login``), while silently logging every existing codex/opencode
user out of their cockpit on upgrade is precisely the "อย่าให้เครื่องอื่นพัง"
failure mode. The copy stays on the same machine and the same user account.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)

# Per-provider cap on cloned session transcripts.
RECENT_SESSIONS_CLONE = 40
# Single-item safety valve — one oversized rollout/db must not turn a
# "cheap bounded copy" back into the unbounded one this policy avoids.
_MAX_ITEM_BYTES = 64 * 1024 * 1024
_MARKER = ".takkub-seeded"


def _legacy_codex_home() -> Path:
    """Codex's OS-wide home — the seed source, never the cockpit's own."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def _legacy_opencode_data() -> Path | None:
    home = Path.home()
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "opencode")
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        candidates.append(Path(xdg) / "opencode")
    candidates.append(home / ".local" / "share" / "opencode")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _legacy_opencode_config() -> Path | None:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    for c in ([Path(xdg) / "opencode"] if xdg else []) + [Path.home() / ".config" / "opencode"]:
        if c.is_dir():
            return c
    return None


def _copy_item(src: Path, dest: Path) -> None:
    """Copy one allowlisted file/dir, skipping anything oversized."""
    try:
        if not src.exists():
            return
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
            return
        if src.stat().st_size > _MAX_ITEM_BYTES:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError:
        return


# A cockpit-assigned teammate session always opens with one of these on its
# first human line (mirrors `remote/notify.py`'s picker filter). Only Lead
# conversations are worth carrying into the isolated home — the user resumes
# Lead, never a finished teammate task (directive 2026-08-19).
_TEAMMATE_TASK_PREFIXES = ("[ROLE:", "[SESSION GOAL")


def _is_teammate_rollout(path: Path) -> bool:
    """True when a Codex rollout's first user message is an assigned task.

    Reads only until that first message: this runs once per candidate file
    during a one-time seed, and the files can be large.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"user_message"' not in line and '"UserMessage"' not in line:
                    continue
                text = line
                for prefix in _TEAMMATE_TASK_PREFIXES:
                    if prefix in text:
                        return True
                return False
    except OSError:
        return False
    return False


def _clone_codex_sessions(src_home: Path, dest_home: Path) -> None:
    """Copy the newest Lead rollouts, keeping Codex's ``YYYY/MM/DD`` layout.

    The layout is preserved rather than flattened because
    ``remote/notify.py``'s candidate walk uses those day directories to stop
    early — a flat dump would silently disable that optimisation.
    """
    sessions = src_home / "sessions"
    if not sessions.is_dir():
        return
    try:
        files = [p for p in sessions.rglob("rollout-*.jsonl") if p.is_file()]
    except OSError:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    copied = 0
    for path in files:
        if copied >= RECENT_SESSIONS_CLONE:
            break
        if _is_teammate_rollout(path):
            continue
        try:
            target = dest_home / "sessions" / path.relative_to(sessions)
        except ValueError:
            continue
        _copy_item(path, target)
        copied += 1


# Allowlisted top-level items per provider — auth and config only; bulk
# state (caches, logs, sandboxes, plugin trees) is deliberately left behind.
_CODEX_CORE_ITEMS: tuple[str, ...] = (
    "auth.json",
    "config.toml",
    "AGENTS.md",
    "instructions.md",
)
_OPENCODE_DATA_ITEMS: tuple[str, ...] = ("auth.json", "opencode.db")
_OPENCODE_CONFIG_ITEMS: tuple[str, ...] = ("opencode.json", "opencode.jsonc", "config.json")


def _seed_codex(dest: Path) -> bool:
    src = _legacy_codex_home()
    if not src.is_dir() or src.resolve() == dest.resolve():
        return False
    partial = dest.with_name(dest.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        partial.mkdir(parents=True)
    except OSError:
        return False
    for name in _CODEX_CORE_ITEMS:
        _copy_item(src / name, partial / name)
    _clone_codex_sessions(src, partial)
    return _promote(partial, dest)


def _seed_opencode(dest_data: Path, dest_config: Path) -> bool:
    src_data = _legacy_opencode_data()
    src_config = _legacy_opencode_config()
    if src_data is None and src_config is None:
        return False
    if src_data is not None and dest_data.exists() and src_data.resolve() == dest_data.resolve():
        return False
    partial = dest_data.with_name(dest_data.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        (partial / "opencode").mkdir(parents=True)
    except OSError:
        return False
    if src_data is not None:
        for name in _OPENCODE_DATA_ITEMS:
            _copy_item(src_data / name, partial / "opencode" / name)
    if not _promote(partial, dest_data):
        return False
    if src_config is not None:
        try:
            (dest_config / "opencode").mkdir(parents=True, exist_ok=True)
        except OSError:
            return True
        for name in _OPENCODE_CONFIG_ITEMS:
            _copy_item(src_config / name, dest_config / "opencode" / name)
    return True


def _promote(partial: Path, dest: Path) -> bool:
    try:
        (partial / _MARKER).write_text("", encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, dest)
        return True
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)
        return False


def ensure_provider_home(provider: str) -> bool:
    """Seed *provider*'s isolated home once, if this build has one.

    Returns True only when a seed actually ran, so the caller can log a
    one-time event. No-ops (dev checkout, provider without an isolation
    knob, home already seeded or already in use) return False and cost one
    ``exists()`` call — this is on the spawn path, which must stay cheap.

    Never raises: a provider that cannot be seeded still spawns, just with
    an empty home, which is strictly better than blocking the pane.
    """
    from . import config

    name = str(provider or "").strip().lower()
    env = config.provider_home_env(name)
    if not env:
        return False
    try:
        if name == "codex":
            dest = Path(env["CODEX_HOME"])
            if dest.exists():
                return False
            return _seed_codex(dest)
        if name == "opencode":
            dest_data = Path(env["XDG_DATA_HOME"])
            if dest_data.exists():
                return False
            return _seed_opencode(dest_data, Path(env["XDG_CONFIG_HOME"]))
    except Exception:
        _log.exception("provider home seeding failed for %s; spawning with an empty home", name)
    return False
