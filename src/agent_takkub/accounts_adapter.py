"""Accounts-page storage adapter (#505 stage 1).

The ONE layer the Settings "Accounts" page reads/writes through. It fronts
today's three account-related stores so the UI never touches them directly:

* ``user_profile.py`` — the live registry (``user-profiles.json`` + per-project
  ``projects/<slug>/user-profile.json`` selection). This is the store spawn
  actually honors today (claude only).
* ``core.accounts.registry.AccountRegistry`` — the V2 JSONL registry the old
  "Accounts & Pools" page edited. Read here so existing records stay visible
  after that page's removal; deliberately NOT written on add (mirroring every
  add into a second store would just duplicate state ahead of #504 — the V2
  facade already reads user_profile through its LegacyReader).
* ``config`` — provider home locations (``default_claude_config_dir`` /
  ``_PROVIDER_HOME_SUBDIRS``) and ``PROVIDER_ISOLATION_GAPS``.

When #504 lands (``providers/<p>/<account>/`` disk layout), only this module's
internals change — the page keeps calling the same functions.

Login/plan detection policy (issue #505): only report what a provider's own
credential store proves. claude = ``.claude.json`` ``oauthAccount`` /
``.credentials.json`` access token (macOS default profile: login Keychain,
same source ``limit_status`` uses). codex = ``auth.json`` presence (same
heuristic as ``doctor._codex_auth_finding``). Everything else = "unknown" —
never guessed (mirrors ``doctor.check_provider_auth``).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config, user_profile

# LoginStatus.state values
LOGGED_IN = "in"
LOGGED_OUT = "out"
UNKNOWN = "unknown"

# `.claude.json` can grow to many MB (project history) — reading it is only
# worth it for the account email, so an oversized file is skipped rather than
# parsed on the UI path. ponytail: no partial/streaming parse (ceiling: no
# email shown for a >8MB .claude.json; login state still comes from the
# credential file, which stays small).
_CLAUDE_JSON_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LoginStatus:
    state: str  # LOGGED_IN | LOGGED_OUT | UNKNOWN
    detail: str = ""  # email, or the reason the state is unknown
    plan: str | None = None  # "Max 20x" / "Pro" / codex planType — None = unknown
    plan_note: str = ""  # non-empty when `plan` came from a cache, with its date


@dataclass
class AccountInfo:
    provider: str
    name: str
    config_dir: str  # "" = the provider's own machine-wide default home
    is_default: bool
    login: LoginStatus
    projects: list[str] = field(default_factory=list)  # explicit per-project picks
    origin: str = "profile"  # "profile" (user_profile) | "v2" (AccountRegistry)


@dataclass
class ProviderRow:
    provider: str
    display_name: str
    gap_reason: str  # non-empty = provider cannot isolate accounts (#103)
    accounts: list[AccountInfo]
    can_add: bool
    add_hint: str  # why adding is unavailable, when can_add is False


# ── login / plan detection ─────────────────────────────────────────────────


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _claude_email(config_dir: Path) -> str:
    """Account email from ``<config_dir>/.claude.json`` ``oauthAccount``, or ""."""
    path = config_dir / ".claude.json"
    try:
        if not path.is_file() or path.stat().st_size > _CLAUDE_JSON_MAX_BYTES:
            return ""
    except OSError:
        return ""
    data = _read_json(path)
    account = (data or {}).get("oauthAccount")
    if isinstance(account, dict):
        return str(account.get("emailAddress") or "").strip()
    return ""


def _claude_plan_from_usage_cache(config_dir: Path) -> tuple[str | None, str]:
    """Last-known plan from `limit_status`'s shared usage-state file for this
    home (`takkub-usage-state.json` — the same cache the usage meter shows).
    Used only when the credential itself carries no readable tier (e.g. a
    Windows claude storing its token outside `.credentials.json`). The note
    carries the fetch date so a stale value is never presented as live."""
    state = _read_json(config_dir / "takkub-usage-state.json")
    data = (state or {}).get("data") or {}
    plan = data.get("plan")
    if not plan or plan == "Unknown":
        return None, ""
    fetched = str(data.get("fetched_at") or "")[:10]
    note = "จาก usage cache ล่าสุดของบัญชีนี้" + (f" ({fetched})" if fetched else "")
    return str(plan), note


def claude_login_status(config_dir: Path, *, is_default: bool = False) -> LoginStatus:
    """Login + plan for one claude home, from claude's own credential files.

    macOS keeps the DEFAULT profile's OAuth blob in the login Keychain
    (`limit_status._read_keychain_credentials`); a named profile's Keychain
    entry is indistinguishable from the default's, so a named profile with no
    credential file reports UNKNOWN there rather than borrowing the default
    account's state.
    """
    from .limit_status import _normalize_credentials, _read_keychain_credentials

    email = _claude_email(config_dir)
    raw = _read_json(config_dir / ".credentials.json")
    # Keychain consult is gated on the home dir actually existing: a signed-in
    # claude always has its config dir, and skipping the `security` subprocess
    # for a non-existent dir keeps page builds (and tests) cheap.
    if raw is None and is_default and sys.platform == "darwin" and config_dir.is_dir():
        blob = _read_keychain_credentials()
        if blob:
            try:
                parsed = json.loads(blob)
                raw = parsed if isinstance(parsed, dict) else None
            except ValueError:
                raw = None

    plan: str | None = None
    has_token = False
    if raw is not None:
        try:
            normalized = _normalize_credentials(raw)
            has_token = bool(normalized.get("access_token"))
            plan = normalized.get("plan")
            if plan == "Unknown":
                plan = None
        except (AttributeError, TypeError, ValueError):
            pass

    plan_note = ""
    if plan is None:
        plan, plan_note = _claude_plan_from_usage_cache(config_dir)

    if has_token or email:
        # `email` without a readable credential file still means an account
        # is signed in — e.g. Windows Credential Manager / a named profile's
        # macOS Keychain entry holds the token, not a file.
        return LoginStatus(LOGGED_IN, email, plan, plan_note)
    if raw is not None:
        return LoginStatus(LOGGED_OUT, "ไฟล์ credential ไม่มี token — login ใหม่", plan, plan_note)
    if sys.platform == "darwin" and not is_default:
        return LoginStatus(UNKNOWN, "อาจอยู่ใน macOS Keychain — ตรวจแยกบัญชีไม่ได้")
    if sys.platform == "win32":
        return LoginStatus(UNKNOWN, "ไม่พบไฟล์ credential — อาจอยู่ใน Credential Manager")
    return LoginStatus(LOGGED_OUT, "ยังไม่ได้เข้าสู่ระบบ")


def codex_login_status(home: Path, *, is_default: bool = False) -> LoginStatus:
    """Presence-only ``auth.json`` check (same heuristic doctor uses) + the
    cached ``planType`` from a running usage store, if one exists — never a
    fresh probe (a `codex app-server` round trip is too heavy for a page
    build)."""
    auth = home / "auth.json"
    plan = _codex_plan_cached(home, is_default=is_default)
    if not auth.is_file():
        return LoginStatus(LOGGED_OUT, "ยังไม่ได้เข้าสู่ระบบ (ไม่มี auth.json)", plan)
    if _read_json(auth) is None:
        return LoginStatus(UNKNOWN, "auth.json อ่านไม่ได้ — ลอง login ใหม่", plan)
    return LoginStatus(LOGGED_IN, "", plan)


def _codex_plan_cached(home: Path, *, is_default: bool) -> str | None:
    """codex ``planType`` from the already-running ProviderUsageStore cache.
    Peek only — never starts the store's poll thread (that belongs to the
    usage meter, not this page).

    #505 review finding M3 (2026-09-07): the store's provider-wide cache
    (``store.get("codex")``, no ``config_dir``) is only ever populated by a
    fetch against the machine's DEFAULT codex home — reading it for a NAMED
    account borrowed that account's plan from whichever identity actually
    polled, with no evidence it applied to THIS home. A named account reads
    only its own per-account cache (``config_dir=home``) — that cache entry
    exists only once something has actually fetched for this exact home
    (e.g. an explicit ``refresh_now(provider, config_dir)``), so an
    unfetched named account correctly reports unknown rather than a
    borrowed number.
    """
    try:
        from . import provider_usage

        store = provider_usage.peek_store()
        if store is None:
            return None
        usage = store.get("codex", config_dir=None if is_default else home)
        return usage.plan if usage is not None and usage.plan else None
    except Exception:
        return None


def login_status(provider: str, config_dir: str, *, is_default: bool = False) -> LoginStatus:
    """Best-known login status for one account. Unverifiable providers
    (everything but claude/codex — see module docstring) report UNKNOWN."""
    if provider == "claude":
        base = Path(config_dir) if config_dir else user_profile._DEFAULT_CONFIG_DIR
        return claude_login_status(base, is_default=is_default)
    if provider == "codex":
        if config_dir:
            home = Path(config_dir)
        else:
            from .codex_helper import codex_home

            home = codex_home()
        return codex_login_status(home, is_default=is_default)
    return LoginStatus(UNKNOWN, "ยังไม่มีวิธีตรวจ credential ของ provider นี้ที่ยืนยันแล้ว")


# ── per-project selection (read side) ──────────────────────────────────────


def projects_by_selection() -> dict[tuple[str, str], list[str]]:
    """Which projects explicitly picked which account: {(provider, name):
    [project_slug, ...]}. Reads every ``projects/<slug>/user-profile.json``
    under user_profile's base dir; both the old ``{"name": ...}`` (claude)
    shape and the current ``{"providers": {...}}`` shape are understood."""
    out: dict[tuple[str, str], list[str]] = {}
    projects_dir = user_profile._BASE_DIR / "projects"
    try:
        entries = sorted(projects_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        data = _read_json(entry / "user-profile.json")
        if not data:
            continue
        picks: dict[str, str] = {}
        providers = data.get("providers")
        if isinstance(providers, dict):
            for provider, name in providers.items():
                picks[user_profile.normalize_provider(provider)] = str(name).strip()
        elif "name" in data:
            picks["claude"] = str(data.get("name") or "").strip()
        for provider, name in picks.items():
            if name:
                out.setdefault((provider, name), []).append(entry.name)
    return out


# ── row assembly ───────────────────────────────────────────────────────────


def _v2_registry_accounts(provider: str, known_names: set[str]) -> list[AccountInfo]:
    """Leftover V2 AccountRegistry rows for *provider* not already covered by
    a user_profile entry — shown so removing the old Accounts & Pools page
    doesn't silently hide them. Never raises (registry is optional state)."""
    try:
        from .core.accounts.registry import AccountRegistry

        records = AccountRegistry().for_provider(provider)
    except Exception:
        return []
    out: list[AccountInfo] = []
    for record in records:
        label = record.label or record.id
        if label in known_names or record.id in known_names:
            continue
        out.append(
            AccountInfo(
                provider=provider,
                name=record.id,
                config_dir=str(record.config_dir or ""),
                is_default=False,
                login=LoginStatus(UNKNOWN, "รายการจาก V2 registry — ไม่มีโฟลเดอร์ credential ให้ตรวจ"),
                origin="v2",
            )
        )
    return out


def can_add_account(provider: str) -> tuple[bool, str]:
    """Stage 1 (#505): only claude accounts are add-able — it is the only
    provider whose per-project selection + spawn env injection already work
    end-to-end. Gap providers explain themselves via PROVIDER_ISOLATION_GAPS."""
    gap = config.PROVIDER_ISOLATION_GAPS.get(provider)
    if gap:
        return False, gap
    if provider == "claude":
        return True, ""
    return False, f"การแยกหลายบัญชีของ {provider} จะเปิดใช้ในขั้นถัดไปของ #505 (ขั้น 2)"


def provider_rows() -> list[ProviderRow]:
    """Everything the Accounts page renders, in PROVIDER_REGISTRY order.

    #505 review M7: a provider with an isolation gap (gemini/cursor — no
    per-account credential separation, `PROVIDER_ISOLATION_GAPS`) used to
    skip listing its accounts ENTIRELY here, even though `user_profile`/the
    V2 registry can perfectly well hold named profiles for it (adding one
    is only blocked by `can_add_account`, a separate check). That silently
    hid an existing account from the one page meant to show every account
    of every provider. The gap only ever governs add/login affordances now
    — accounts always list.
    """
    from .provider_spec import PROVIDER_REGISTRY

    selections = projects_by_selection()
    rows: list[ProviderRow] = []
    for provider, spec in PROVIDER_REGISTRY.items():
        gap = config.PROVIDER_ISOLATION_GAPS.get(provider, "")
        accounts: list[AccountInfo] = []
        profiles = user_profile.profiles_for_provider(provider)
        for entry in profiles:
            name = entry["name"]
            is_default = name == user_profile.DEFAULT_PROFILE
            accounts.append(
                AccountInfo(
                    provider=provider,
                    name=name,
                    config_dir=entry.get("config_dir") or "",
                    is_default=is_default,
                    login=login_status(
                        provider, entry.get("config_dir") or "", is_default=is_default
                    ),
                    projects=list(selections.get((provider, name), [])),
                )
            )
        accounts.extend(_v2_registry_accounts(provider, {a.name for a in accounts}))
        addable, add_hint = can_add_account(provider)
        rows.append(
            ProviderRow(
                provider=provider,
                display_name=spec.display_name or provider.capitalize(),
                gap_reason=gap,
                accounts=accounts,
                can_add=addable,
                add_hint=add_hint,
            )
        )
    return rows


# ── write side ─────────────────────────────────────────────────────────────


def default_account_home(provider: str, name: str) -> Path:
    """Where a NEW named account's home lives, derived from the same base
    config.py defines for the provider's default home — dev checkout:
    ``~/.claude-<name>``; installed build: ``DATA_HOME/claude-config-<name>``.
    (#504 will move this under ``providers/<p>/<account>/`` at boot — this
    function is the single place that changes.)"""
    if provider == "claude":
        base = user_profile._DEFAULT_CONFIG_DIR
        return base.with_name(f"{base.name}-{name}")
    raise ValueError(
        f"provider {provider!r} does not support named account homes yet (#505 stage 2)"
    )


def add_account(
    provider: str, name: str, config_dir: str = "", share_sessions: bool = True
) -> tuple[Path, list[str]]:
    """Register a new account. Blank *config_dir* → the provider-conventional
    home from :func:`default_account_home`. Returns (home, linked_items).
    Raises ``ValueError`` on invalid/duplicate names (from user_profile)."""
    name = str(name).strip()
    home = (
        Path(config_dir).expanduser()
        if config_dir.strip()
        else default_account_home(provider, name)
    )
    linked = user_profile.add_profile(
        name, str(home), share_sessions=share_sessions, provider=provider
    )
    return home, linked


def remove_account(account: AccountInfo) -> None:
    """Remove one account from whichever store owns it. Raises ``ValueError``
    for the reserved default profile (from user_profile)."""
    if account.origin == "v2":
        from .core.accounts.registry import AccountRegistry

        AccountRegistry().delete(account.name)
        return
    user_profile.remove_profile(account.name)
    if account.config_dir:
        try:
            user_profile.cleanup_profile_links(account.config_dir)
        except Exception:
            pass


# ── login pane launch spec ─────────────────────────────────────────────────


def login_launch(provider: str, config_dir: str) -> tuple[list[str], dict[str, str]] | None:
    """(argv, extra_env) to open an interactive login pane for one account,
    or None when this provider has no known login flow the cockpit can host.
    The env override is the SAME knob spawn uses for panes
    (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` — config._PROVIDER_HOME_SUBDIRS),
    so logging in here lands the credential exactly where panes will read it.
    """
    if provider == "claude":
        try:
            exe = config.find_claude_executable()
        except RuntimeError:
            # Not installed / not resolvable right now (e.g. CI, a fresh
            # machine) — the card page builds every account's row through
            # this on every render (panel build + retheme), so this must
            # report "no login flow available" rather than blow up the
            # whole Accounts page for every provider (#505 regression).
            return None
        home = config_dir or str(user_profile._DEFAULT_CONFIG_DIR)
        return [exe], {"CLAUDE_CONFIG_DIR": home}
    if provider == "codex":
        from .codex_helper import codex_home, find_codex_executable

        exe = find_codex_executable()
        if not exe:
            return None
        home = config_dir or str(codex_home())
        return [exe, "login"], {"CODEX_HOME": home}
    return None
