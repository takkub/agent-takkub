"""SettingsWindow — the unified Takkub Cockpit Settings window.

Implements the gold/IBM-Plex design system from
`docs/design-review/2026-07-10-cockpit-settings-design-system.md`: a
status strip + sidebar (PIPELINE/POLICY/ACCOUNT sections + "+ New
Role") + content (header, a 10-view ``QStackedWidget``, footer). The
original design also had a decorative faux titlebar above the status
strip — dropped 2026-07-11 (UI walkthrough #55): it duplicated the OS
title bar's "Takkub Cockpit — Settings" text with no other function.

Phase 1 wired **Providers & Roles** and **New Role**. Phase 2 wires the
remaining five:

* **MCP Matrix** / **Plugins Matrix** — native ``QGridLayout`` role×item
  toggle grids, backed by the pure per-role policy helpers in
  :mod:`pane_tools_dialog` (``build_matrix``/``matrix_to_role_items``/
  ``diff_role_items``/``discover_marketplaces``/``master_mcps``/
  ``policy_role_items`` — module-level functions, no Qt) so this view reads/
  writes :mod:`pane_tools_policy` identically to how the old standalone
  "🔧 Tools" dialog did before it was removed (2026-07-10, fully superseded
  by this window). Plugins Matrix keeps the denylist banner (security-guidance/
  remember are never toggleable — see :mod:`lead_context`'s
  ``_PANE_PLUGIN_DENYLIST``, enforced at pane-spawn time, not in this UI).
* **Role Overlap** (ROLE section) — role list + read-only mono doc viewer,
  backed by :mod:`skill_audit` (``load_all_role_docs`` + ``audit_existing_
  role`` — "✓ won't overlap" for the selected role). This is a ROLE-scope
  audit (TF-IDF), *not* a skill browser — it was mislabeled "Skill Catalog"
  before 2026-07-11. Removed 2026-08-24 in the settings-nav declutter (see
  the ``VIEW_*`` block's own comment) — a pure diagnostic that added nothing
  ``takkub doctor`` didn't already surface.
* **Skill Catalog** (SKILL section) — the real skill browser: lists
  ``.claude/skills/*/SKILL.md`` via :mod:`skill_scan` (the scanner the New
  Role picker uses) with each skill's description + which role docs mention
  it. Read-only browse.
* **Skill Matrix** (SKILL section, 2026-07-11 — #103 phase 4) — role × skill
  toggle grid backed by :mod:`skill_policy` (mirrors :mod:`pane_tools_policy`'s
  on-disk shape, a separate store/concern: which skills get PROACTIVELY
  referenced in a role's spawn-time context, not which MCP/plugin a pane can
  load). Reuses the same ``_populate_matrix_grid`` + ``pane_tools_dialog``
  matrix helpers as MCP/Plugins Matrix. Unlike those two, codex and gemini get
  rows here — `spawn_engine.spawn()` bridges a checked skill into their
  AGENTS.md as instruction-style text (no Skill tool on those CLIs); claude
  gets a lighter nudge appended to its role ``CLAUDE.md``, since its Skill
  tool already auto-discovers `.claude/skills/` on its own.
* **Pipeline Builder** / **Templates** — native hop editor + template
  list/detail, backed by :mod:`pipeline_config` directly (a from-scratch
  reimplementation of :mod:`pipeline_dialog`'s QWebEngineView page per the
  task spec, not a wrapper around it). Structural template edits (Duplicate/
  Delete) write immediately, mirroring the Add/Remove MCP pattern; in-flight
  hop edits are staged in :attr:`SettingsWindow._pb_hops` and only persist on
  Save & Apply, mirroring the toggle-matrix staging pattern.
* **New Role**'s "use default MCP+Plugins ตาม column" toggle now actually
  seeds :mod:`pane_tools_policy` on create (checked → the same MCP/plugin
  defaults the matching dev/support-column built-in roles get; unchecked →
  an explicit empty policy the operator configures via the Matrix views).
* **Users** (ACCOUNT section, 2026-07-11) — Profiles + Claude Auth tabs,
  ported from :mod:`user_actions`'s standalone ``open_user_profiles_dialog``
  modal (removed the same day — 100% superseded). Reached both as a normal
  sidebar nav item and via the 👥 Team chip's right-click "Add / Remove
  user…" entry (:meth:`user_actions.UserActionsMixin._on_add_user_clicked`),
  which now opens straight to this view instead of its own popup.

:mod:`pipeline_dialog` (the old Pipeline Settings page, still reachable via
the 👥 Team chip's right-click menu — see :mod:`user_actions`'s
``_show_pipelines_menu``) is kept alive as an alternate entry point to the
same underlying config. The old standalone "🔧 Tools" dialog
(:mod:`pane_tools_dialog`'s ``PaneToolsDialog`` — 100% redundant with this
window's Providers & Roles / MCP Matrix / Plugins Matrix / Skill Catalog /
New Role views) was removed 2026-07-10; only its pure per-role policy
helper functions remain in that module, imported below.

**Import constraint:** mirrors ``pane_tools_dialog``'s — this module MUST NOT
import ``app`` or ``cli`` (plain UI dialog, no engine/CLI coupling).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import replace as _dataclass_replace
from pathlib import Path

from PyQt6.QtCore import QLocale, QSettings, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QGuiApplication, QIcon, QPalette
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import (
    auto_issue_signals,
    autoskills_installer,
    cockpit_theme,
    config,
    custom_roles,
    pane_tools_dialog,
    pane_tools_policy,
    performance_settings,
    pipeline_config,
    project_nav,
    provider_config,
    provider_model_catalog,
    provider_models,
    provider_spec,
    provider_state,
    role_models,
    shared_dev_tools,
    skill_audit,
    skill_policy,
    skill_scan,
    theme_settings,
    token_estimate,
    user_profile,
)
from . import roles as roles_mod
from .claude_auth_config import ClaudeAuthConfig, load_claude_auth, save_claude_auth
from .lead_context import _allowed_project_roots
from .settings_accounts import AccountsSettingsMixin
from .settings_knowledge_design import KnowledgeDesignSettingsMixin
from .settings_usage import UsageSettingsMixin

# ── view indices (QStackedWidget page order) ────────────────────
# Settings diet (#515, 2026-09-07 user directive "Settings จุดไหนไม่จำเป็นแล้ว
# เอาออกได้เลย"): 17 pages / 8 sections -> 8 pages / 4 sections (GENERAL /
# TEAM / TOOLS / ACCOUNT — no ADVANCED). VIEW_* indices are NOT renumbered
# (old routes/deep-links/tests keyed off these ints keep working) — a page
# that merged into another or was dropped outright instead gets a
# `_VIEW_REDIRECTS` entry below, and its old stack slot stays a bare
# placeholder (same pattern #505's VIEW_CORE_V2_ACCOUNTS slot already used).
#
# Merged into one page (tabs, or list+builder side by side):
#   VIEW_PIPELINE_BUILDER = **Pipeline** — template list (left) + hop
#     builder (right) in one page; VIEW_TEMPLATES redirects here.
#   VIEW_MCP_MATRIX = **Tools** — MCP / Plugins tabs; VIEW_PLUGINS_MATRIX
#     redirects here.
#   VIEW_SKILL_CATALOG = **Skills** — Catalog / Matrix tabs;
#     VIEW_SKILL_MATRIX redirects here.
# Dropped outright (redundant with a live default, or duplicated `takkub
# doctor` — see issue #515's own table for the prod evidence per item):
#   VIEW_PERFORMANCE — its one still-meaningful control (the machine-aware
#     safe/balanced/maximum preset) becomes the "โหมดเครื่อง" picker on
#     General; the numeric sliders it used to expose are engine-derived now.
#   VIEW_CORE_V2_ROUTING / _BRAIN / _SCHEDULER — Core V2 flags default-on
#     since 1.0.84 and these three duplicated status `takkub doctor` already
#     surfaces (or will — see #515's own acceptance notes).
#   VIEW_NEW_ROLE — used to be a stack page reached via a persistent sidebar
#     button; now a QDialog opened from a button on "ทีม & ตำแหน่ง"
#     (`_open_new_role_dialog`) — `_goto_view` special-cases it below instead
#     of routing it through the stack at all.
# `settings_core_v2.py` (Routing/Brain/Scheduler's only callers) was deleted
# outright in the same pass — see git history for the removed views.
VIEW_PIPELINE_BUILDER = 0
VIEW_TEMPLATES = 1
VIEW_PROVIDERS_ROLES = 2
VIEW_MCP_MATRIX = 3
VIEW_PLUGINS_MATRIX = 4
VIEW_NEW_ROLE = 5
VIEW_USERS = 6
VIEW_SKILL_CATALOG = 7
VIEW_SKILL_MATRIX = 8
VIEW_KNOWLEDGE = 9
VIEW_CORE_V2_ACCOUNTS = 10
VIEW_CORE_V2_ROUTING = 11
VIEW_CORE_V2_BRAIN = 12
VIEW_CORE_V2_SCHEDULER = 13
VIEW_PERFORMANCE = 14
VIEW_GENERAL = 15
# #507 — appended after the earlier declutter pass rather than inserted in
# numeric position, so every existing VIEW_* index stays stable (external
# test references key off these numbers).
VIEW_USAGE = 16

# Legacy VIEW_* constants that no longer name a real stack page — `_goto_view`
# maps them to whichever page absorbed their content before doing anything
# else, so an old deep-link/test/bookmark still lands somewhere sensible
# instead of a blank placeholder slot (#515, same contract #505 established
# for VIEW_CORE_V2_ACCOUNTS below).
_VIEW_REDIRECTS: dict[int, int] = {
    VIEW_CORE_V2_ACCOUNTS: VIEW_USERS,  # #505
    # #515: New Role is a QDialog now (opened by a button ON this page), not
    # a stack page — a `_goto_view(VIEW_NEW_ROLE)` from old code just lands
    # on the page that owns the button, same as every other redirect below.
    VIEW_NEW_ROLE: VIEW_PROVIDERS_ROLES,
    VIEW_TEMPLATES: VIEW_PIPELINE_BUILDER,  # #515: merged into Pipeline
    VIEW_PLUGINS_MATRIX: VIEW_MCP_MATRIX,  # #515: merged into Tools
    VIEW_SKILL_MATRIX: VIEW_SKILL_CATALOG,  # #515: merged into Skills
    VIEW_PERFORMANCE: VIEW_GENERAL,  # #515: preset -> "โหมดเครื่อง" on General
    VIEW_CORE_V2_ROUTING: VIEW_GENERAL,  # #515: dropped, no direct replacement page
    VIEW_CORE_V2_BRAIN: VIEW_KNOWLEDGE,  # #515: closest still-live status page
    VIEW_CORE_V2_SCHEDULER: VIEW_GENERAL,  # #515: dropped, no direct replacement page
}

# (view index, nav label, sidebar section) — 4 sections, no ADVANCED (#515).
# New Role is reached via a button on "ทีม & ตำแหน่ง" (`_open_new_role_dialog`),
# not this list, so it isn't a normal nav item.
_NAV_VIEWS: tuple[tuple[int, str, str], ...] = (
    (VIEW_GENERAL, "ทั่วไป", "GENERAL"),
    # #512: "ทีม & ตำแหน่ง" (ex-"Providers & Roles") leads TEAM — team-size
    # preset first, roster/provider detail below it — with Pipeline grouped
    # under the same section since "which hop sequence runs" is part of the
    # same team question.
    (VIEW_PROVIDERS_ROLES, "ทีม & ตำแหน่ง", "TEAM"),
    (VIEW_PIPELINE_BUILDER, "Pipeline", "TEAM"),
    (VIEW_MCP_MATRIX, "Tools", "TOOLS"),
    (VIEW_SKILL_CATALOG, "Skills", "TOOLS"),
    (VIEW_KNOWLEDGE, "Knowledge", "TOOLS"),
    # #505: one "Accounts" page replaced both the old "Users" entry here and
    # the ADVANCED "Accounts & Pools" entry (VIEW_CORE_V2_ACCOUNTS redirects
    # to it above so old routes/constants keep working).
    (VIEW_USERS, "Accounts", "ACCOUNT"),
    (VIEW_USAGE, "Usage", "ACCOUNT"),
)

# No folded section anymore (ADVANCED was the only one — #515 dropped it
# along with everything it held). Kept as a frozenset, not deleted outright,
# since `_build_sidebar`/`_goto_view` still branch on membership — empty
# just means that branch never fires; a future folded section is still a
# one-line change here.
_FOLDABLE_SECTIONS: frozenset[str] = frozenset()

_NAV_VIEW_SECTION: dict[int, str] = {view_idx: section for view_idx, _label, section in _NAV_VIEWS}

# Same org/app pair `project_nav`'s explorer-expanded persistence already
# uses for MainWindow's own QSettings-backed per-user UI state — one shared
# settings file, namespaced by key, rather than a second small JSON store
# just for this one bool.
_SIDEBAR_SETTINGS_ORG = "agent-takkub"
_SIDEBAR_SETTINGS_APP = "cockpit"

# Pages that save through their own dedicated button (Knowledge/Design
# Tools writes each credential through immediately) or write through
# immediately on every change (Accounts' add/edit/remove/login, General's
# theme/machine-mode/toggles, same "that IS the apply" story) rather than
# ever joining the footer's shared Save & Apply transaction — Usage is a
# pure read-only report, nothing to save at all. The footer "Save & Apply" /
# "Revert unsaved changes" pair is disabled outright while one of these is
# showing so it can't be mistaken for controlling them (design critic should
# #1, pre-#515 `docs/v2/phase9-critic-review.md`).
_NO_FOOTER_SAVE_VIEWS: frozenset[int] = frozenset(
    {VIEW_KNOWLEDGE, VIEW_USERS, VIEW_GENERAL, VIEW_USAGE}
)

# Design review 2026-07-24 #1 (ROOT CAUSE) — the mockup's nav glyphs
# (⌘ ◇ ◉ ⊞ ✦ ♙) were ported 1:1 from the web mockup but IBM Plex Sans/Mono
# don't ship those code points, so every nav item tofu'd (□) on this desktop
# font stack. Swapped for resolution-independent SVG icons (precedent:
# static/icons/ already backs the QComboBox/QSpinBox arrows the same way —
# see cockpit_theme._down_arrow_svg) instead of chasing a symbol font.
# Two-tone per icon (muted/gold) so the active nav item still reads as
# accented without depending on font color inheritance through QIcon.
_NAV_ICON_NAMES: dict[int, str] = {
    VIEW_GENERAL: "diamond",
    VIEW_PROVIDERS_ROLES: "target",
    VIEW_PIPELINE_BUILDER: "pipeline",
    VIEW_MCP_MATRIX: "grid",
    VIEW_SKILL_CATALOG: "star",
    # Icon set is fixed at 6 names (diamond/grid/pipeline/star/target/user,
    # see static/icons/nav/) — reused rather than adding new SVG assets.
    VIEW_KNOWLEDGE: "star",
    VIEW_USERS: "user",
    VIEW_USAGE: "grid",
}
_NAV_ICONS_DIR = Path(__file__).resolve().parent / "static" / "icons" / "nav"


def _nav_icon(view_idx: int, *, active: bool) -> QIcon:
    name = _NAV_ICON_NAMES.get(view_idx, "diamond")
    tone = "gold" if active else "muted"
    # Per-variant icon files (#506): SVG fills are baked in, and the dark-set
    # glyphs are near-invisible on the light sidebar.
    suffix = "-light" if cockpit_theme.current_variant() == "light" else ""
    return QIcon(str(_NAV_ICONS_DIR / f"nav-{name}-{tone}{suffix}.svg"))


_VIEW_HEADERS: dict[int, tuple[str, str]] = {
    VIEW_GENERAL: (
        "ทั่วไป",
        "ธีมสี · โหมดเครื่อง · ภาษา — ทุกอย่างในหน้านี้มีผลทันทีและจำค่าไว้",
    ),
    VIEW_PROVIDERS_ROLES: (
        "ทีม & ตำแหน่ง",
        "ขนาดทีมของโปรเจคนี้ + ตำแหน่งที่เปิดจริง + provider/model ต่อ role",
    ),
    VIEW_PIPELINE_BUILDER: (
        "Pipeline",
        "template ที่บันทึกไว้ (ซ้าย) + ลาก-วาง hop/role ของ template ที่เลือก (ขวา) — ขั้นสูง",
    ),
    VIEW_MCP_MATRIX: ("Tools", "role × MCP server / plugin policy"),
    VIEW_SKILL_CATALOG: (
        "Skills",
        "skill จริงใน .claude/skills/ (SKILL.md) + role × skill ที่ inject เข้า context อัตโนมัติ",
    ),
    VIEW_USERS: (
        "Accounts",
        "บัญชีของทุก provider — ใครเข้าสู่ระบบอยู่ ใช้กับโปรเจคไหน เพิ่ม/ลบบัญชี",
    ),
    VIEW_KNOWLEDGE: (
        "Knowledge",
        "Context Strategy + สถานะ Brain / Obsidian / Graft + credential เครื่องมือ design",
    ),
    VIEW_USAGE: (
        "Usage",
        "token/quota จริงที่ provider รายงาน แยก provider→บัญชี→model — กด Refresh เพื่อ import ล่าสุด",
    ),
}


# Roles offered a per-role CLI override in "Providers & Roles". Excludes
# codex/gemini/opencode/kimi/cursor (provider_config.FORCED_ROLES — CLI IS
# the role's identity) and shell (not a pipeline-eligible role — see
# pipeline_config.valid_roles()'s own note). `lead` is prepended separately
# below: it isn't a
# pipeline_config.valid_roles() member (Lead is excluded from dev pipelines,
# a different concern — see that function's own docstring) but IS eligible
# for a CLI override since issue #101's degraded-mode unlock removed it from
# FORCED_PROVIDER. A function — not a frozen tuple — since custom roles
# register at runtime and this must reflect them the next time the Settings
# window opens (SettingsWindow is constructed fresh on every open, so a
# function called from inside a `_build_*_view()` picks up a just-created
# role with no cockpit restart; a module-level constant computed once at
# import time never would). Same freshness reasoning applies to the
# lead-unlocked check — re-forcing "lead" via FORCED_PROVIDER in a future
# change makes it disappear from this list automatically.
def _overridable_roles() -> tuple[str, ...]:
    pipeline_roles = tuple(
        r
        for r in pipeline_config.valid_roles()
        if r not in provider_config.FORCED_ROLES and r != "shell"
    )
    if "lead" not in provider_config.FORCED_ROLES:
        pipeline_roles = ("lead", *pipeline_roles)
    return pipeline_roles


def _provider_installed(provider: str) -> bool:
    """True unless `provider`'s CLI is registered with a discovery probe
    that came back empty — used only for the "สมองเสริม" chip strip's
    "ยังไม่ติดตั้ง" label (#512 item 3), a lighter question than
    `provider_config._provider_available` (which also folds in the
    disabled-providers toggle). Defaults True on a discovery error/missing
    probe — a probe failure isn't proof of absence, and this label is
    advisory only (the actual gate stays MODEL CONNECTIONS' toggle)."""
    if provider == provider_config.CLAUDE:
        return True
    spec = provider_spec.PROVIDER_REGISTRY.get(provider)
    if spec is None or spec.custom_discovery_fn is None:
        return True
    try:
        return spec.custom_discovery_fn() is not None
    except Exception:
        return True


_PROVIDER_DESC: dict[str, str] = {
    "codex": "OpenAI Codex CLI — second opinion / refactor cross-check",
    "gemini": "Google Antigravity (agy) — planning / long-context second opinion",
}

# Empty selection = don't pass `--model` at all, i.e. whatever that CLI
# defaults to for the signed-in account.
_MODEL_DEFAULT_LABEL = "(default)"

# Model shortlists per provider — a *snapshot* (2026-08-03, refreshed against
# each CLI actually installed on the dev box) offered as dropdown presets AND
# the permanent fallback for gemini/codex when live discovery hasn't run yet
# or fails (`_fill_model_combo` merges this with `provider_model_catalog`'s
# cached discovery — see #493). Every model combo stays EDITABLE regardless,
# because a free-typed id always works even for a provider this cockpit can't
# discover for at all (kimi/cursor/opencode — #103 gap, see
# `provider_model_refresh.NO_MODEL_DISCOVERY_GAPS`); re-verify this snapshot
# by hand next time it goes stale (`agy models`, `codex` — read
# `~/.codex/models_cache.json`'s "slug" fields, no CLI subcommand lists them
# — `opencode models --refresh`).
# claude/codex/gemini/opencode entries below were confirmed live against the
# CLIs installed on this box; kimi has no model-listing command or cache to
# check against (accepts free-text `-m`) and cursor's CLI wasn't installed
# here at all — both left as the prior snapshot, unverified.
_MODELS_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "claude": (
        "opus",
        "sonnet",
        "haiku",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5",
        "claude-opus-4-8",
    ),
    # Confirmed via ~/.codex/models_cache.json "slug" fields (installed CLI's
    # own cache) — gpt-5.3-codex no longer appears; gpt-5.6 now ships as 3
    # named variants instead of a bare "gpt-5.6". codex-auto-review excluded
    # (internal review-only model, not a general chat/agent model).
    "codex": (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
    ),
    # Confirmed via `agy models` (installed CLI's own lister, re-run
    # 2026-08-21) — these are the exact `--model` tokens, not agy's spaced
    # display names like the prior snapshot had. The 3.7 line was already out
    # when this list still topped out at 3.6: `provider_model_refresh` bumps a
    # PINNED model automatically, but nothing refreshes this picker, so it
    # goes stale on its own cadence and has to be re-run by hand.
    "gemini": (
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ),
    # Unverified — kimi CLI (v1.49.0) exposes no `models` subcommand or model
    # cache; kept as the prior snapshot.
    "kimi": ("k3", "k2.7", "k2.6", "k2.5"),
    # Unverified — cursor's CLI isn't installed on this box; kept as the
    # prior snapshot.
    "cursor": (
        "claude-sonnet-4.7",
        "claude-opus-4.7",
        "gpt-5.5",
        "gemini-2.5-pro",
        "grok-4",
        "composer-1",
    ),
    # anthropic/* confirmed via `opencode models` (installed CLI, its own
    # models.dev-backed lister); this box's opencode has no openai/google
    # provider logged in, so those two are cross-referenced from the same
    # underlying vendor models confirmed above (codex's gpt-5.6-sol, agy's
    # gemini-3.1-pro-high) rather than opencode's own lister — best-effort,
    # not directly confirmed by opencode itself.
    "opencode": (
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.6-sol",
        "google/gemini-3.1-pro-high",
    ),
}


def _fill_model_combo(combo: QComboBox, provider: str, current: str | None) -> None:
    """(Re)populate a model picker with *provider*'s presets — the live
    discovery cache (`provider_model_catalog`) merged over the hand-written
    snapshot, freshest ids first, snapshot filling any gap — preserving any
    free-typed value, and point it at *current* (empty/None → "(default)").
    `cached_ids` is a small local JSON read, never a subprocess call, so this
    stays safe to call inline on the Qt main thread."""
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(_MODEL_DEFAULT_LABEL, "")
    presets = provider_model_catalog.merge_catalog(
        _MODELS_BY_PROVIDER.get(provider, ()), provider_model_catalog.cached_ids(provider)
    )
    for preset in presets:
        combo.addItem(preset, preset)
    combo.lineEdit().setPlaceholderText(_MODEL_DEFAULT_LABEL)
    _select_model(combo, current)
    combo.blockSignals(False)


def _select_model(combo: QComboBox, model: str | None) -> None:
    """Point *combo* at *model* — a preset row when it matches one, otherwise
    the free-text value; empty/None falls back to the "(default)" row."""
    if not model:
        combo.setCurrentIndex(0)
        return
    idx = combo.findData(model)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    else:
        combo.setCurrentText(model)


def _combo_model(combo: QComboBox) -> str:
    """Read a model picker back: "(default)" (or blank) means no override."""
    text = combo.currentText().strip()
    return "" if text == _MODEL_DEFAULT_LABEL else text


# Empty selection = don't pass an effort argument at all, i.e. the role's
# tier default (or the TAKKUB_TEAMMATE_EFFORT env override, or the provider's
# own CLI default) applies instead — see spawn_engine._resolve_teammate_effort.
_EFFORT_DEFAULT_LABEL = "(ตามค่าเริ่มต้นของ role)"


def _fill_effort_combo(combo: QComboBox, provider: str, model: str, current: str | None) -> None:
    """(Re)populate an effort picker with the levels *provider* accepts for
    *model* — single source of truth is
    ``provider_spec.effort_levels_for`` (#103: never hardcode a level list
    or the per-model exception table here).

    When that provider/model combination can't take an effort argument at
    all, the combo is disabled rather than emptied: *current* (if any) is
    kept as the sole selectable item so a stale-but-still-recorded choice
    stays visible instead of silently vanishing — Save & Apply is what
    actually drops it (see the save handler's own note).
    """
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(_EFFORT_DEFAULT_LABEL, "")
    levels = provider_spec.effort_levels_for(provider, model)
    if levels:
        for level in levels:
            combo.addItem(level, level)
        combo.setEnabled(True)
        combo.setToolTip("reasoning effort ของ role นี้ — ว่าง = ตามค่าเริ่มต้นของ role")
    else:
        if current:
            combo.addItem(current, current)
        combo.setEnabled(False)
        combo.setToolTip(
            "provider/model นี้ไม่รองรับ reasoning effort — ตั้งค่าไม่ได้ (Save & Apply จะตัดค่าเดิมออก ถ้ามี)"
        )
    idx = combo.findData(current or "")
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


def _combo_effort(combo: QComboBox) -> str:
    """Read an effort picker back: disabled (unsupported provider/model) or
    the "(default)" row both mean no override."""
    if not combo.isEnabled():
        return ""
    text = combo.currentText().strip()
    return "" if text == _EFFORT_DEFAULT_LABEL else text


# Roles rendered as rows in the MCP/Plugins matrices — same set (and order)
# pane_tools_dialog.matrix_roles() defines, so a role's policy reads
# identically from either surface. Also a function, for the same
# fresh-per-open reason as `_overridable_roles()` above.
def _matrix_roles() -> tuple[str, ...]:
    return pane_tools_dialog.matrix_roles()


# Roles offered in the Pipeline Builder's role palette / per-hop add-role
# select. Every valid_roles() entry except "shell" — an ad-hoc terminal pane,
# not a directed pipeline participant (see `_overridable_roles()`'s own
# note). Also a function, for the same fresh-per-open reason.
def _pipeline_palette_roles() -> tuple[str, ...]:
    return tuple(r for r in pipeline_config.valid_roles() if r != "shell")


# New Role's "use default MCP+Plugins ตาม column" toggle (#6): no per-role
# policy exists yet for a freshly created custom role, so this maps the
# form's existing Dev(1)/Support(2) column choice onto the same MCP
# defaults the matching built-in roles already get in
# shared_dev_tools._ROLE_MCP_POLICY (dev column = frontend/backend/devops,
# lean; support column = qa/critic/designer, browser-driving).
_NEW_ROLE_COLUMN_MCPS: dict[int, frozenset[str]] = {
    1: frozenset(),
    2: frozenset({"playwright", "chrome-devtools"}),
}

# `.claude/agents/*.md` docs that ship in the repo but have no matching
# roles.py Role() entry, so they're unreachable except by copy-pasting their
# content by hand — offered as New Role Instructions starting points instead.
_NEW_ROLE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("analyst", "Analyst — product prioritization/spec writing"),
    ("designer", "Designer — UI/UX design review"),
    ("docs", "Docs — documentation writing"),
    ("security", "Security — security review"),
)

# Strips the curation `<!-- ... -->` comment and `---\n...\n---` frontmatter
# block that lead every `.claude/agents/*.md` file, leaving just the role
# body to seed the Instructions box with.
_AGENT_TEMPLATE_HEADER_RE = re.compile(r"^(?:<!--.*?-->\s*)?(?:---.*?---\s*)?", re.DOTALL)


# New Role's skill picker rows sit in a `maximumHeight(220)` scroll area
# (see `_build_new_role_skill_row`) — an unclamped description could run
# 4-5 lines (critic review docs/design/2026-08-13-new-role-critique.md),
# leaving room for only ~3 skills before the list needed scrolling on its
# own. ~110 chars is roughly 2 wrapped lines at this card's width; the full
# text is never lost — it's always set as the row's tooltip.
_SKILL_DESC_CLAMP_CHARS = 110


def _clamp_skill_description(description: str) -> str:
    if len(description) <= _SKILL_DESC_CLAMP_CHARS:
        return description
    return description[: _SKILL_DESC_CLAMP_CHARS - 1].rstrip() + "…"


def _append_skill_references(instructions: str, skills: list[skill_scan.SkillInfo]) -> str:
    """Embed a "## Skills ที่เกี่ยวข้อง" section listing every selected skill
    into the role's generated instructions text — applies whether
    `instructions` is the user's own typed text or the default template
    (custom_roles._default_role_template), so a selected skill is never
    silently dropped just because the Instructions box was left empty."""
    lines = "\n".join(
        f"- อ่าน skill: {s.name} — {s.description} ก่อนเริ่มงานที่เกี่ยวข้อง"
        if s.description
        else f"- อ่าน skill: {s.name} ก่อนเริ่มงานที่เกี่ยวข้อง"
        for s in skills
    )
    return f"{instructions.rstrip()}\n\n## Skills ที่เกี่ยวข้อง\n{lines}\n"


class _AutoskillsPreviewThread(QThread):
    """Runs `autoskills_installer.preview()` off the Qt main thread — it
    shells out and can block up to 60s (see that module's docstring), and
    the Skill Catalog's "ดึง skill ตาม stack" button must never freeze the
    whole window while that subprocess runs."""

    resultReady: pyqtSignal = pyqtSignal(object)  # PreviewResult

    def __init__(self, project_root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project_root = project_root

    def run(self) -> None:
        self.resultReady.emit(autoskills_installer.preview(self._project_root))


class _AutoskillsInstallThread(QThread):
    """Runs `autoskills_installer.install()` off the Qt main thread — same
    blocking-subprocess reasoning as `_AutoskillsPreviewThread`, only called
    after the user has explicitly confirmed a skill selection."""

    resultReady: pyqtSignal = pyqtSignal(object)  # InstallResult

    def __init__(
        self, project_root: Path, selected_names: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._selected_names = selected_names

    def run(self) -> None:
        self.resultReady.emit(
            autoskills_installer.install(self._project_root, self._selected_names)
        )


class _ModelCatalogRefreshThread(QThread):
    """Runs `provider_model_catalog.refresh_stale()` off the Qt main thread
    (#493) — each stale provider's discovery is a real CLI subprocess call
    (see `provider_model_refresh`'s own discovery functions), so this must
    never block opening Settings. Started once per SettingsWindow open;
    `resultReady` carries only the providers that actually got a fresh
    catalog (empty dict when everything was already fresh, undiscoverable,
    or discovery failed — see `refresh_stale`'s contract)."""

    resultReady: pyqtSignal = pyqtSignal(dict)  # {provider: [model_id, ...]}

    def run(self) -> None:
        self.resultReady.emit(provider_model_catalog.refresh_stale())


class _AutoskillsConfirmDialog(QDialog):
    """Confirms which of `autoskills`' proposed candidates actually get
    written — never auto-checked-and-fired. A skill under `.claude/skills/`
    is a prompt every pane in the project auto-loads automatically, so this
    is external content (from the skills.sh registry) entering the team's
    shared context; the user must see the list and press Install."""

    def __init__(
        self, result: autoskills_installer.PreviewResult, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-detect skills")
        self.resize(520, 480)
        self._result = result
        self._checks: list[tuple[autoskills_installer.SkillCandidate, QCheckBox]] = []

        lay = QVBoxLayout(self)
        if result.stack:
            lay.addWidget(QLabel(f"Stack ที่ตรวจพบ: {', '.join(result.stack)}", self))

        warn = QLabel(
            "⚠ skill ที่ติดตั้งคือ instruction ที่ทุก pane ในโปรเจคนี้อ่านอัตโนมัติ = "
            "เนื้อหาจากภายนอก (skills.sh registry) เข้าสู่ context ของทีม — "
            "เลือกเฉพาะ skill ที่ต้องการจริงๆ ก่อนกดติดตั้ง",
            self,
        )
        warn.setObjectName("infoBanner")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget(scroll)
        c_lay = QVBoxLayout(container)
        c_lay.setSpacing(4)
        for cand in result.skills:
            chk = QCheckBox(cand.name, container)
            # A skill the CLI itself flagged (e.g. "security check ⚠") must
            # start unchecked — the user has to make a deliberate choice to
            # include it, not un-notice a pre-ticked box.
            chk.setChecked(not cand.notes)
            c_lay.addWidget(chk)
            if cand.source:
                src = QLabel(cand.source, container)
                src.setObjectName("panelHint")
                src.setContentsMargins(22, 0, 0, 0)
                src.setWordWrap(True)
                c_lay.addWidget(src)
            if cand.notes:
                note = QLabel(f"⚠ {cand.notes}", container)
                note.setContentsMargins(22, 0, 0, 0)
                note.setWordWrap(True)
                note.setStyleSheet(f"color: {cockpit_theme.ERROR_CHIP_TEXT}; font-size: 11px;")
                c_lay.addWidget(note)
            self._checks.append((cand, chk))
        c_lay.addStretch(1)
        scroll.setWidget(container)
        lay.addWidget(scroll, 1)

        raw_row = QHBoxLayout()
        raw_btn = QPushButton("ดู raw output ของ CLI", self)
        raw_btn.clicked.connect(self._show_raw_output)
        raw_row.addWidget(raw_btn)
        raw_row.addStretch(1)
        lay.addLayout(raw_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("ติดตั้ง skill ที่เลือก")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _show_raw_output(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("autoskills raw output")
        box.setText(self._result.raw_output or "(ว่างเปล่า)")
        box.exec()

    def selected_names(self) -> list[str]:
        return [cand.name for cand, chk in self._checks if chk.isChecked()]


class SettingsWindow(
    QDialog,
    AccountsSettingsMixin,
    KnowledgeDesignSettingsMixin,
    UsageSettingsMixin,
):
    """The unified Settings window. One instance per open — construct fresh
    each time (mirrors the old, now-removed ``PaneToolsDialog``/
    ``PipelineSettingsDialog``'s no-singleton/no-caching pattern), so it
    always reflects on-disk state at open time."""

    def __init__(
        self,
        parent: QWidget | None = None,
        project: str | None = None,
        initial_view: int = VIEW_PROVIDERS_ROLES,
    ) -> None:
        super().__init__(parent)
        # H3 (cross-review 2026-09-07): every field open re-constructs a
        # fresh SettingsWindow (see class docstring) parented to the main
        # window, but nothing ever scheduled its deletion — a probe that
        # opened/closed it 5 times found all 5 instances (17 views' worth
        # of widgets each, Usage/Accounts included) still alive. `close()`
        # (accept/reject both call it) now schedules `deleteLater()`
        # instead of leaking the instance for the process's lifetime.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._project = project
        self._dirty = False
        # Which views (VIEW_* indices) have unsaved staged edits right now —
        # per-view so Reset on one view doesn't clear another view's dirty
        # state, and so a fresh dialog with nothing touched keeps Save &
        # Apply disabled (#6/#16). `_dirty` above is kept as a plain bool
        # mirror (`bool(self._dirty_views)`) since existing call sites/tests
        # read it as "is anything unsaved" — see `_refresh_dirty_indicator`.
        self._dirty_views: set[int] = set()
        # Staged provider on/off — mirrors pipeline_dialog._PipelineBridge's
        # pending_provider_disabled contract: {provider: desired_disabled},
        # only for providers whose target differs from disk. Populated on
        # Save & Apply; the caller applies it via orchestrator.toggle_provider
        # AFTER exec() returns Accepted, so live Lead panes get the same
        # broadcast a status-bar chip click produces.
        self.pending_provider_disabled: dict[str, bool] = {}
        self.pending_performance_reload = False
        # #512: staged team-size pick, applied on Save & Apply (persisted
        # inside this same transaction, see _on_save_apply_clicked) — a
        # separate flag (not folded into pending_provider_disabled) so the
        # caller (user_actions._open_legacy_settings_window) can additionally
        # route it through orchestrator.set_team_preset for the live-Lead
        # broadcast + teamPresetChanged signal, same split as
        # pending_provider_disabled/orchestrator.toggle_provider.
        self.pending_team_preset: str | None = None
        # Pipeline Builder/Templates share this in-memory copy of pipelines.json
        # (structural edits — Duplicate/Delete — write through immediately and
        # refresh it; hop edits stay staged here until Save & Apply).
        self._pipeline_payload = pipeline_config.load(self._project)

        self.setObjectName("settingsWindow")
        self.setWindowTitle("Takkub Cockpit — Settings")
        # Never open — or refuse to shrink — larger than the screen actually
        # showing this dialog. The old unconditional 1320x848 with a hard
        # 900x600 floor put the footer, and with it "Save & Apply", under the
        # taskbar on a smaller/scaled display, with no way to resize far
        # enough to reach it (reported from a teammate's laptop, 2026-08-21).
        # Every view already scrolls (`_wrap_scroll`), so a smaller window
        # costs only how much is visible at once, never reachability.
        avail = self._available_screen_size()
        self.setMinimumSize(min(900, avail.width()), min(600, avail.height()))
        self.resize(min(1320, avail.width()), min(848, avail.height()))
        self.setSizeGripEnabled(True)

        fonts = cockpit_theme.ensure_fonts_loaded()
        self._fonts = fonts
        self.setStyleSheet(cockpit_theme.build_stylesheet(str(fonts["sans"]), str(fonts["mono"])))
        # QSS `::placeholder` only styles QLineEdit (see the rule itself in
        # cockpit_theme.py) — QPlainTextEdit has no stylesheet placeholder
        # selector in Qt6, it reads QPalette.PlaceholderText instead, which
        # was never set anywhere so it fell back to Qt's default near-invisible
        # tint on GROUND_INPUT (critic finding, docs/design/
        # 2026-08-13-new-role-critique.md). Set once here so every
        # QPlainTextEdit in this window inherits it.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(cockpit_theme.TEXT_MUTED))
        self.setPalette(palette)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # UI walkthrough #55 ("Header ซ้ำ 3 ที่ใน Settings") — the OS title
        # bar (setWindowTitle above), this faux titlebar, and the status
        # strip's brand label all said "takkub cockpit / settings" back to
        # back. Dropped the faux titlebar (purely decorative — traffic-light
        # dots + a duplicate title string); the OS titlebar + status strip
        # brand still identify the window.
        outer.addWidget(self._build_status_strip())

        # H3 (cross-review 2026-09-07): every OTHER view here is built
        # eagerly at construction (matches this class's own docstring —
        # "always reflects on-disk state at open time"), which is fine for
        # a form/table read. Usage is not: `_build_usage_view()` calls
        # `_render_usage()`, which does real ledger file I/O (and, before
        # H2's separate fix, a ~0.5s `rtk gain` subprocess) — paid on
        # EVERY Settings open, even when the user never visits Usage.
        # `_lazy_view_builders` (populated in `_build_content` below,
        # consumed by `_ensure_view_built` from `_goto_view`) defers a
        # registered view's real construction to its first navigation.
        # Mixins other than Usage can opt into the same lazy pattern by
        # registering their own `VIEW_*: builder` entry here before
        # `_build_content()` runs, instead of adding an eager
        # `self._stack.addWidget(...)` call.
        self._lazy_view_builders: dict[int, Callable[[], QWidget]] = {}

        body = QWidget(self)
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(self._build_sidebar())
        body_lay.addWidget(self._build_content(), 1)
        outer.addWidget(body, 1)

        self._goto_view(initial_view)
        self._model_catalog_thread: _ModelCatalogRefreshThread | None = None
        self._start_model_catalog_refresh()

    def _start_model_catalog_refresh(self) -> None:
        """Kick off a background re-discovery of any stale provider model
        catalog (#493) — never blocks opening Settings; affected combos
        repopulate in place if/when fresh ids arrive. Skipped under
        TAKKUB_SKIP_MODEL_CATALOG_REFRESH (set in tests/conftest.py, mirrors
        every other TAKKUB_SKIP_* boot-side-effect guard there) so the test
        suite's many `SettingsWindow()` constructions never spawn a thread
        that shells out to a provider CLI."""
        if os.environ.get("TAKKUB_SKIP_MODEL_CATALOG_REFRESH"):
            return
        thread = _ModelCatalogRefreshThread(self)
        thread.resultReady.connect(self._on_model_catalog_refreshed)
        thread.finished.connect(thread.deleteLater)
        self._model_catalog_thread = thread
        thread.start()

    def _on_model_catalog_refreshed(self, results: dict[str, list[str]]) -> None:
        """Repopulate every model combo whose provider just got a fresh
        catalog, preserving whatever value (preset or free-typed) it
        currently holds — same pattern as the CLI-combo-changed handlers
        that already call `_fill_model_combo` this way."""
        if not results:
            return
        for provider, combo in self._provider_model_combos.items():
            if provider in results:
                _fill_model_combo(combo, provider, _combo_model(combo) or None)
        for role, combo in self._role_model_combos.items():
            provider_combo = self._role_provider_combos.get(role)
            role_provider = (
                provider_combo.currentData() if provider_combo else None
            ) or provider_config.CLAUDE
            if role_provider in results:
                _fill_model_combo(combo, role_provider, _combo_model(combo) or None)

    # ──────────────────────────────────────────────────────────
    # chrome: titlebar / status strip
    # ──────────────────────────────────────────────────────────

    def _build_status_strip(self) -> QWidget:
        # 2026-09-08 design review — "header ซ้ำซ้อนกับ OS title bar": the
        # brand label ("takkub COCKPIT") and version number used to render
        # here too, repeating `setWindowTitle("Takkub Cockpit — Settings")`
        # above and adding little of their own. Dropped both; the template
        # chip + provider dots below are the only per-project info this
        # strip needs (same reasoning as the earlier faux-titlebar removal,
        # see the "Header ซ้ำ 3 ที่" comment in __init__ above).
        strip = QWidget(self)
        strip.setObjectName("statusStrip")
        strip.setFixedHeight(56)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)

        payload = pipeline_config.load(self._project)
        active_id = payload.get("activeTemplate", "")
        active_name = next(
            (t["name"] for t in payload.get("templates", []) if t.get("id") == active_id), active_id
        )
        if active_name:
            # Critic visual-review round-2 #3 — a bare template name (e.g.
            # "Feature (UI+API)") read as an unlabeled, unexplained pill that
            # looked like it had leaked in from the main window's status bar
            # (the plan indicator there was an editable chip at the time;
            # #505 replaced it with a read-only badge — the visual-collision
            # concern this comment is about is unchanged either way).
            # It's a real per-project summary (see the walkthrough #56 note
            # below), so it stays — just prefixed + given a tooltip so its
            # purpose is self-evident instead of relying on the reader
            # already knowing what a "template" is in this cockpit.
            chip = cockpit_theme.gold_soft_chip(f"Template: {active_name}", strip)
            chip.setToolTip(
                "Pipeline template ที่ active อยู่สำหรับโปรเจคนี้ — เปลี่ยนได้ที่ Templates / Pipeline Builder"
            )
            lay.addWidget(chip)

        # UI walkthrough #56 — this used to also render a role-chip per
        # enabled role, duplicating the Providers & Roles view's own Roles
        # list (same names, same colors) one click away. Dropped; the active
        # template chip is the only per-project summary this strip needs.
        lay.addStretch(1)

        for provider in sorted(provider_state.TOGGLABLE):
            enabled = not provider_state.is_disabled(provider)
            color = cockpit_theme.ACCENT_GOLD if enabled else cockpit_theme.TEXT_FAINT
            # Design review 2026-07-24 #4 — a real painted dot, not the "●"
            # glyph (tofus on fonts without that code point).
            dot = cockpit_theme.color_dot(color, strip, size=8)
            dot.setToolTip(f"{provider}: {'enabled' if enabled else 'disabled'}")
            lay.addWidget(dot)

        return strip

    # ──────────────────────────────────────────────────────────
    # chrome: sidebar
    # ──────────────────────────────────────────────────────────

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(236)
        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(0)

        self._nav_buttons: dict[int, QPushButton] = {}
        self._nav_indicators: dict[int, QFrame] = {}
        # Foldable sections (ADVANCED) route their nav rows into a per-
        # section body widget instead of `lay` directly, so the whole group
        # can be hidden/shown as a unit — see `_on_nav_section_toggled`.
        self._nav_section_bodies: dict[str, QWidget] = {}
        self._nav_section_toggles: dict[str, QPushButton] = {}
        nav_settings = QSettings(_SIDEBAR_SETTINGS_ORG, _SIDEBAR_SETTINGS_APP)

        last_section: str | None = None
        target_lay: QVBoxLayout = lay
        for view_idx, label, section in _NAV_VIEWS:
            if section != last_section:
                if section in _FOLDABLE_SECTIONS:
                    # Folded by default (`13_SIMPLE_UX.md` "hide internal
                    # knobs under Advanced") — a viewer who never expanded it
                    # gets the collapsed default every time; only an explicit
                    # prior expand sticks.
                    expanded = bool(
                        nav_settings.value(f"settingsNav/{section}/expanded", False, type=bool)
                    )
                    toggle_btn = QPushButton(sidebar)
                    toggle_btn.setObjectName("sidebarSectionToggle")
                    toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    toggle_btn.setCheckable(True)
                    toggle_btn.setChecked(expanded)
                    toggle_btn.setText(f"{'▾' if expanded else '▸'} {section}")
                    lay.addWidget(toggle_btn)

                    body = QWidget(sidebar)
                    body_lay = QVBoxLayout(body)
                    body_lay.setContentsMargins(0, 0, 0, 0)
                    body_lay.setSpacing(0)
                    body.setVisible(expanded)
                    lay.addWidget(body)

                    toggle_btn.toggled.connect(
                        lambda checked, s=section: self._on_nav_section_toggled(s, checked)
                    )
                    self._nav_section_bodies[section] = body
                    self._nav_section_toggles[section] = toggle_btn
                    target_lay = body_lay
                else:
                    sec_lbl = QLabel(section, sidebar)
                    sec_lbl.setObjectName("sidebarSection")
                    lay.addWidget(sec_lbl)
                    target_lay = lay
                last_section = section
            # QPushButton treats a lone "&" as a mnemonic escape (swallows the
            # next char) — "Providers & Roles" would render as "Providers
            # _Roles". Double it so it displays literally.
            btn = QPushButton(f"  {label}".replace("&", "&&"), sidebar)
            btn.setIcon(_nav_icon(view_idx, active=False))
            btn.setIconSize(QSize(16, 16))
            btn.setObjectName("navButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _checked=False, v=view_idx: self._goto_view(v))

            # Gemini #13 — the active-view marker is a 5px rounded color bar
            # in the design spec; QSS's `border-left` can't round only one
            # side of a box, so a real QFrame stands in for it instead.
            nav_row = QWidget(sidebar)
            nav_row_lay = QHBoxLayout(nav_row)
            nav_row_lay.setContentsMargins(0, 0, 0, 0)
            nav_row_lay.setSpacing(0)
            indicator = QFrame(nav_row)
            indicator.setObjectName("navIndicator")
            indicator.setFixedWidth(5)
            indicator.setVisible(False)
            nav_row_lay.addWidget(indicator)
            nav_row_lay.addWidget(btn, 1)
            target_lay.addWidget(nav_row)

            self._nav_buttons[view_idx] = btn
            self._nav_indicators[view_idx] = indicator

        lay.addStretch(1)
        return sidebar

    def _on_nav_section_toggled(self, section: str, expanded: bool) -> None:
        body = self._nav_section_bodies.get(section)
        toggle_btn = self._nav_section_toggles.get(section)
        if body is not None:
            body.setVisible(expanded)
        if toggle_btn is not None:
            toggle_btn.setText(f"{'▾' if expanded else '▸'} {section}")
        try:
            QSettings(_SIDEBAR_SETTINGS_ORG, _SIDEBAR_SETTINGS_APP).setValue(
                f"settingsNav/{section}/expanded", expanded
            )
        except Exception:
            pass

    def _ensure_view_built(self, view_idx: int) -> None:
        """Replace `view_idx`'s placeholder with its real widget on first
        navigation there (H3, 2026-09-07) — a no-op for every index not
        registered in `_lazy_view_builders` (already built eagerly, or
        already replaced by an earlier visit)."""
        builder = self._lazy_view_builders.pop(view_idx, None)
        if builder is None:
            return
        old = self._stack.widget(view_idx)
        if old is not None:
            self._stack.removeWidget(old)
        self._stack.insertWidget(view_idx, builder())
        if old is not None:
            old.deleteLater()

    def _goto_view(self, view_idx: int) -> None:
        # Every merged/dropped page's old VIEW_* constant lands on whichever
        # page absorbed it instead of a dead placeholder slot — see
        # `_VIEW_REDIRECTS`'s own comment for the full merge/drop table.
        # VIEW_NEW_ROLE is in that map too (-> VIEW_PROVIDERS_ROLES, where
        # its dialog-opening button now lives): `_goto_view` is called from
        # `__init__` (`initial_view`) and must stay a fast, synchronous
        # page-switch — never a modal `QDialog.exec()` — so opening the New
        # Role dialog is wired ONLY to that button's own click handler
        # (`_open_new_role_dialog`), never routed through here.
        view_idx = _VIEW_REDIRECTS.get(view_idx, view_idx)
        # Jumping straight to a view inside a folded section (initial_view,
        # or the caller passing a VIEW_* constant directly) must still land
        # on a visible, reachable nav row — expand its section first so the
        # highlighted button isn't hidden behind a collapsed header.
        section = _NAV_VIEW_SECTION.get(view_idx)
        if section in _FOLDABLE_SECTIONS:
            toggle_btn = self._nav_section_toggles.get(section)
            if toggle_btn is not None and not toggle_btn.isChecked():
                toggle_btn.setChecked(True)
        for idx, btn in self._nav_buttons.items():
            is_active = idx == view_idx
            btn.setProperty("active", is_active)
            btn.setIcon(_nav_icon(idx, active=is_active))
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        for idx, indicator in self._nav_indicators.items():
            indicator.setVisible(idx == view_idx)
        self._ensure_view_built(view_idx)
        self._stack.setCurrentIndex(view_idx)
        title, sub = _VIEW_HEADERS.get(view_idx, ("", ""))
        self._content_title.setText(title)
        self._content_sub.setText(sub)
        self._refresh_dirty_indicator()

    # ──────────────────────────────────────────────────────────
    # chrome: content (header + stack + footer)
    # ──────────────────────────────────────────────────────────

    def _build_content(self) -> QWidget:
        content = QWidget(self)
        content.setObjectName("content")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header_body = QWidget(content)
        hb_lay = QVBoxLayout(header_body)
        hb_lay.setContentsMargins(24, 20, 24, 16)
        hb_lay.setSpacing(4)

        self._content_pretitle = QLabel("CONFIGURATION", header_body)
        self._content_pretitle.setObjectName("contentPreTitle")
        hb_lay.addWidget(self._content_pretitle)
        self._content_title = QLabel("", header_body)
        self._content_title.setObjectName("contentTitle")
        hb_lay.addWidget(self._content_title)
        self._content_sub = QLabel("", header_body)
        self._content_sub.setObjectName("contentSub")
        self._content_sub.setWordWrap(True)
        hb_lay.addWidget(self._content_sub)
        hb_lay.addSpacing(8)

        self._stack = QStackedWidget(header_body)
        # Index order MUST match the VIEW_* constants above. Every merged/
        # dropped page's slot (VIEW_TEMPLATES, VIEW_PLUGINS_MATRIX,
        # VIEW_SKILL_MATRIX, VIEW_CORE_V2_ACCOUNTS/_ROUTING/_BRAIN/
        # _SCHEDULER, VIEW_PERFORMANCE, VIEW_NEW_ROLE) is a bare placeholder
        # — `_goto_view` redirects the old constant elsewhere (or, for New
        # Role, opens a QDialog) before it can ever reach one of these slots
        # (#515, same pattern #505 established for VIEW_CORE_V2_ACCOUNTS).
        # Every real page is registered lazily (`_lazy_view_builders`, H3
        # 2026-09-07) rather than built here, so opening Settings never pays
        # for a page the operator doesn't visit this session.
        self._stack.addWidget(QWidget(self))  # 0  VIEW_PIPELINE_BUILDER
        self._stack.addWidget(QWidget(self))  # 1  VIEW_TEMPLATES -> redirect
        self._stack.addWidget(QWidget(self))  # 2  VIEW_PROVIDERS_ROLES
        self._stack.addWidget(QWidget(self))  # 3  VIEW_MCP_MATRIX
        self._stack.addWidget(QWidget(self))  # 4  VIEW_PLUGINS_MATRIX -> redirect
        self._stack.addWidget(QWidget(self))  # 5  VIEW_NEW_ROLE -> dialog, never built
        self._stack.addWidget(QWidget(self))  # 6  VIEW_USERS
        self._stack.addWidget(QWidget(self))  # 7  VIEW_SKILL_CATALOG
        self._stack.addWidget(QWidget(self))  # 8  VIEW_SKILL_MATRIX -> redirect
        self._stack.addWidget(QWidget(self))  # 9  VIEW_KNOWLEDGE
        self._stack.addWidget(QWidget(self))  # 10 VIEW_CORE_V2_ACCOUNTS -> redirect
        self._stack.addWidget(QWidget(self))  # 11 VIEW_CORE_V2_ROUTING -> redirect
        self._stack.addWidget(QWidget(self))  # 12 VIEW_CORE_V2_BRAIN -> redirect
        self._stack.addWidget(QWidget(self))  # 13 VIEW_CORE_V2_SCHEDULER -> redirect
        self._stack.addWidget(QWidget(self))  # 14 VIEW_PERFORMANCE -> redirect
        self._stack.addWidget(
            self._wrap_scroll(self._build_general_view())
        )  # 15 (cheap, built eagerly)
        self._stack.addWidget(QWidget(self))  # 16 VIEW_USAGE
        self._lazy_view_builders[VIEW_PIPELINE_BUILDER] = lambda: self._wrap_scroll(
            self._build_pipeline_view()
        )
        self._lazy_view_builders[VIEW_PROVIDERS_ROLES] = lambda: self._wrap_scroll(
            self._build_providers_roles_view()
        )
        self._lazy_view_builders[VIEW_MCP_MATRIX] = lambda: self._wrap_scroll(
            self._build_tools_view()
        )
        self._lazy_view_builders[VIEW_USERS] = lambda: self._wrap_scroll(self._build_users_view())
        self._lazy_view_builders[VIEW_SKILL_CATALOG] = lambda: self._wrap_scroll(
            self._build_skills_view()
        )
        self._lazy_view_builders[VIEW_KNOWLEDGE] = lambda: self._wrap_scroll(
            self._build_knowledge_tabbed_view()
        )
        self._lazy_view_builders[VIEW_USAGE] = lambda: self._wrap_scroll(self._build_usage_view())
        hb_lay.addWidget(self._stack, 1)

        outer.addWidget(header_body, 1)
        outer.addWidget(self._build_footer())
        return content

    def _available_screen_size(self) -> QSize:
        """How big this dialog is allowed to get, in pixels.

        `availableGeometry` already excludes the taskbar/dock; the extra
        margin covers the window frame and title bar, which it does not.
        Falls back to the historical fixed size when Qt can't name a screen
        (offscreen/headless platforms) so nothing here needs a real display.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return QSize(1320, 848)
        geo = screen.availableGeometry()
        return QSize(max(640, geo.width() - 48), max(420, geo.height() - 80))

    def _wrap_scroll(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        footer.setObjectName("footer")
        footer.setFixedHeight(60)
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(10)

        # Codex Medium #5 — this button reloads the CURRENT view's on-disk
        # state (i.e. discards staged edits), it does not restore factory
        # defaults. "Reset to default" over-promised; label it for what it
        # actually does.
        self._reset_btn = cockpit_theme.secondary_button("Revert unsaved changes", footer)
        self._reset_btn.clicked.connect(self._on_reset_clicked)
        lay.addWidget(self._reset_btn)
        lay.addStretch(1)

        # Design review 2026-07-24 #4 — real painted dot widget, not the "●"
        # text glyph (tofus on fonts lacking that code point).
        self._unsaved_dot = cockpit_theme.color_dot(cockpit_theme.ACCENT_GOLD, footer, size=8)
        self._unsaved_dot.setObjectName("unsavedDot")
        self._unsaved_dot.setVisible(False)
        lay.addWidget(self._unsaved_dot)
        self._unsaved_label = QLabel("มีการแก้ไขที่ยังไม่บันทึก", footer)
        self._unsaved_label.setObjectName("unsavedLabel")
        self._unsaved_label.setVisible(False)
        lay.addWidget(self._unsaved_label)

        cancel_btn = cockpit_theme.secondary_button("Cancel", footer)
        cancel_btn.clicked.connect(self.reject)
        lay.addWidget(cancel_btn)

        self._save_btn = cockpit_theme.gold_button("Save && Apply", footer)
        self._save_btn.clicked.connect(self._on_save_apply_clicked)
        # Gemini #16 — nothing staged yet at open time, so there's nothing
        # to apply; `_refresh_dirty_indicator` re-enables it the moment any
        # view goes dirty.
        self._save_btn.setEnabled(False)
        lay.addWidget(self._save_btn)

        return footer

    def _mark_dirty(self, *_args: object) -> None:
        self._dirty_views.add(self._stack.currentIndex())
        self._refresh_dirty_indicator()

    def _clear_dirty(self) -> None:
        self._dirty_views.clear()
        self._refresh_dirty_indicator()

    def _refresh_dirty_indicator(self) -> None:
        """Recompute the aggregate `_dirty` flag from `_dirty_views` and sync
        the footer's unsaved-dot/label + Save & Apply enabled state.

        Also disables Save & Apply / Revert unsaved changes outright while a
        Core V2 or Knowledge & Design page is showing (`_NO_FOOTER_SAVE_
        VIEWS`) — those pages save through their own dedicated button (or
        write through immediately) and never stage into `_dirty_views`, so
        the footer pair would otherwise sit there gold/clickable while doing
        nothing for the visible page (design critic should #1)."""
        self._dirty = bool(self._dirty_views)
        self._unsaved_dot.setVisible(self._dirty)
        self._unsaved_label.setVisible(self._dirty)
        on_self_saving_view = self._stack.currentIndex() in _NO_FOOTER_SAVE_VIEWS
        self._save_btn.setEnabled(self._dirty and not on_self_saving_view)
        self._reset_btn.setEnabled(not on_self_saving_view)

    def _on_reset_clicked(self) -> None:
        """Revert the currently-visible view's editable fields back to the
        on-disk state, clearing only THIS view's dirty flag — a different
        view's still-staged edits (#6) must survive. Skill Catalog has
        nothing staged to reset (read-only), so it no-ops; Tools/Skills
        reload BOTH their tabs since one merged view (#515) now covers what
        used to be two separately-reset pages."""
        idx = self._stack.currentIndex()
        if idx == VIEW_PROVIDERS_ROLES:
            self._reset_providers_roles_view()
        elif idx == VIEW_MCP_MATRIX:
            self._reload_mcp_matrix()
            self._reload_plugins_matrix()
        elif idx == VIEW_SKILL_CATALOG:
            self._reload_skill_matrix()
        elif idx == VIEW_PIPELINE_BUILDER and getattr(self, "_pb_template_id", None):
            self._load_pb_hops(self._pb_template_id)
        self._dirty_views.discard(idx)
        self._refresh_dirty_indicator()

    def _on_save_apply_clicked(self) -> None:
        """Persist every staged edit across all views in one Save & Apply:
        Providers & Roles, Pipeline's in-flight hop edits (rolled into the
        same pipelines.json write as rolesEnabled), and the MCP/Plugins
        matrices. A template's Duplicate/Delete commits independently (its
        own buttons), so this is a safe no-op for whichever of those the
        user never touched. New Role (#515) is a QDialog with its own
        Create button now — it never reaches this transaction at all.
        """
        # Snapshot every on-disk store this transaction can touch so a
        # failure partway through (#3) rolls back instead of leaving stores
        # inconsistent — e.g. a role-provider override written but the
        # pipelines.json write failing right after, or a tools-policy write
        # failing after providers/roles already landed.
        snapshot_paths = (
            provider_config.config_path(self._project),
            pipeline_config.path(self._project),
            pane_tools_policy.PANE_TOOLS_POLICY_FILE,
            skill_policy.SKILL_POLICY_FILE,
            # Model overrides are written first in this same Save, so they must
            # roll back too — otherwise a later pipeline/tools failure reports
            # "save failed" while the model files are already persisted.
            provider_models.path(),
            role_models.path(),
        )
        snapshots = {p: (p.read_bytes() if p.exists() else None) for p in snapshot_paths}

        def _rollback() -> None:
            for p, content in snapshots.items():
                try:
                    if content is None:
                        p.unlink(missing_ok=True)
                    else:
                        p.write_bytes(content)
                except OSError:
                    pass

        # Every dict below belongs to a page registered in `_lazy_view_builders`
        # (H3, 2026-09-07 + #515) — a Settings session that never navigated to
        # Providers & Roles (say, it only opened Tools) never built these
        # widgets at all. `getattr(..., {})` treats "page never visited" the
        # same as "page visited, nothing on it changed": there is genuinely
        # nothing staged there either way, so this transaction just skips it.
        provider_toggles = getattr(self, "_provider_toggles", {})
        provider_model_combos = getattr(self, "_provider_model_combos", {})
        role_model_combos = getattr(self, "_role_model_combos", {})
        role_provider_combos = getattr(self, "_role_provider_combos", {})
        role_effort_combos = getattr(self, "_role_effort_combos", {})
        role_toggles = getattr(self, "_role_toggles", {})
        mcp_toggles = getattr(self, "_mcp_toggles", {})
        plugin_toggles = getattr(self, "_plugin_toggles", {})
        skill_toggles = getattr(self, "_skill_toggles", {})

        try:
            self.pending_provider_disabled = {}
            for provider, toggle in provider_toggles.items():
                desired_disabled = not toggle.isChecked()
                if desired_disabled != provider_state.is_disabled(provider):
                    self.pending_provider_disabled[provider] = desired_disabled

            # Per-provider + per-role model overrides — written straight
            # through (unlike the on/off toggle they need no orchestrator
            # broadcast; the value is read at spawn time, so it lands on the
            # next pane).
            for provider, combo in provider_model_combos.items():
                provider_models.set_model(provider, _combo_model(combo))
            dropped_effort_roles: list[str] = []
            for role, combo in role_model_combos.items():
                # Bind the model to the CLI it was picked for, so switching the
                # role's provider (or a substitute kicking in) can't pass a
                # model id to a CLI that doesn't know it.
                role_provider = role_provider_combos[role].currentData() or provider_config.CLAUDE
                role_models.set_model(role, role_provider, _combo_model(combo))
                effort_combo = role_effort_combos.get(role)
                if effort_combo is not None:
                    if not effort_combo.isEnabled() and role_models.effort_for(role, role_provider):
                        # Combo is disabled because the CURRENT provider/model
                        # can't take an effort argument, yet a value is still
                        # on disk for this exact provider (stale from before
                        # the model changed under it) — drop it now rather
                        # than persist something the CLI would reject, and
                        # tell the user once instead of silently.
                        dropped_effort_roles.append(role)
                    role_models.set_effort(role, role_provider, _combo_effort(effort_combo))

            role_providers = {
                role: combo.currentData() for role, combo in role_provider_combos.items()
            }
            # scope=role_provider_combos.keys(): this page renders a control
            # only for the team_preset ROSTER (#512 — positions + lead +
            # whichever ONE role is the active checker, not the full
            # _overridable_roles() set anymore) — anything outside that (a
            # custom role's override, or the checker role NOT currently
            # active, say) must be preserved, not silently dropped by a
            # naive full-replace write.
            provider_config.save_role_overrides(
                role_providers, self._project, scope=role_provider_combos.keys()
            )

            # #512: persist a staged team-size card pick BEFORE the
            # rolesEnabled write below — note_manual_roles_change (right
            # after) diffs the saved roles against the STANDING preset on
            # disk, and the roster panel's toggles already reflect the
            # newly-picked preset's roles (see _rebuild_roster_panel), so the
            # standing preset must already be the new one or that diff would
            # see false drift and wrongly flip the project to "custom".
            from . import team_preset as _team_preset

            if self.pending_team_preset is not None:
                _team_preset.set_current(self.pending_team_preset, self._project)

            payload = pipeline_config.load(self._project)
            roles_enabled = dict(payload.get("rolesEnabled", {}))
            for role, toggle in role_toggles.items():
                roles_enabled[role] = toggle.isChecked()
            payload["rolesEnabled"] = roles_enabled

            pb_template_id = getattr(self, "_pb_template_id", None)
            if pb_template_id:
                for t in payload["templates"]:
                    if t["id"] == pb_template_id:
                        t["hops"] = [[dict(entry) for entry in hop] for hop in self._pb_hops]
                        break

            pipeline_config.save(payload, self._project)
            self._pipeline_payload = pipeline_config.load(self._project)

            # #512 acceptance: hand-toggling a role here while the project
            # sits on a FIXED team preset flips it to "custom" (carrying the
            # other resolved fields along) so the preset doesn't silently
            # overwrite the user's manual choice on its next apply.
            _team_preset.note_manual_roles_change(roles_enabled, self._project)

            updated_mcps = pane_tools_dialog.matrix_to_role_items(
                {
                    role: {item: t.isChecked() for item, t in items.items()}
                    for role, items in mcp_toggles.items()
                }
            )
            updated_plugins = pane_tools_dialog.matrix_to_role_items(
                {
                    role: {item: t.isChecked() for item, t in items.items()}
                    for role, items in plugin_toggles.items()
                }
            )
            orig_mcp_items = getattr(self, "_orig_mcp_items", {})
            orig_plugin_items = getattr(self, "_orig_plugin_items", {})
            mcp_changes = pane_tools_dialog.diff_role_items(orig_mcp_items, updated_mcps)
            plugin_changes = pane_tools_dialog.diff_role_items(orig_plugin_items, updated_plugins)
            # Write BOTH kinds for every role that changed EITHER — see
            # pane_tools_dialog._on_save_clicked's own note: set_role_items
            # seeds a fresh role entry's sibling kind to [] (an explicit deny),
            # so a plugins-only persist would silently wipe that role's MCPs.
            # set_role_items() never raises (validation/IO failures return
            # False) — check it explicitly so a failed write here triggers
            # the same rollback+error path as the other stages instead of
            # silently continuing as if it had succeeded.
            hidden = getattr(self, "_hidden_plugin_defaults", {})
            for role in set(mcp_changes) | set(plugin_changes):
                if not pane_tools_policy.set_role_items(role, "mcps", updated_mcps[role]):
                    raise OSError(f"เขียน tools policy ของ role '{role}' (mcps) ไม่สำเร็จ")
                if not pane_tools_policy.set_role_items(
                    role, "plugins", updated_plugins[role] + hidden.get(role, [])
                ):
                    raise OSError(f"เขียน tools policy ของ role '{role}' (plugins) ไม่สำเร็จ")
            self._orig_mcp_items = updated_mcps
            self._orig_plugin_items = updated_plugins

            updated_skills = pane_tools_dialog.matrix_to_role_items(
                {
                    role: {item: t.isChecked() for item, t in items.items()}
                    for role, items in skill_toggles.items()
                }
            )
            orig_skill_items = getattr(self, "_orig_skill_items", {})
            skill_changes = pane_tools_dialog.diff_role_items(orig_skill_items, updated_skills)
            for role in skill_changes:
                if not skill_policy.set_role_skills(role, updated_skills[role]):
                    raise OSError(f"เขียน skill policy ของ role '{role}' ไม่สำเร็จ")
            self._orig_skill_items = updated_skills
            if mcp_changes or plugin_changes:
                shared_dev_tools.regen_role_variants()
        except (OSError, ValueError) as e:
            _rollback()
            self._pipeline_payload = pipeline_config.load(self._project)
            QMessageBox.critical(
                self, "Save failed", f"บันทึกไม่สำเร็จ (rolled back ทุก store ที่แก้ไปแล้ว): {e}"
            )
            return
        if dropped_effort_roles:
            QMessageBox.information(
                self,
                "Effort override cleared",
                "provider/model ปัจจุบันไม่รองรับ reasoning effort — ตัดค่า effort เดิมออกจาก: "
                + ", ".join(dropped_effort_roles),
            )
        self._clear_dirty()
        self.accept()

    # ──────────────────────────────────────────────────────────
    # view: General (theme / machine mode / language — every field here
    # write-through + applies live, #506/#515. Machine mode + the pane-
    # discard/auto-issue toggles moved here from the now-removed Performance
    # page (#515: its numeric sliders were dropped — see the VIEW_* block's
    # own comment — but these three controls are still real, still-live
    # operator decisions, not values with one correct answer).
    # ──────────────────────────────────────────────────────────

    _MACHINE_MODES: tuple[tuple[str, str], ...] = (
        ("safe", "เงียบ (Safe)"),
        ("balanced", "สมดุล (Balanced)"),
        ("maximum", "เต็มที่ (Maximum)"),
    )

    def _build_general_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        theme_panel = QWidget(view)
        theme_panel.setObjectName("panel")
        theme_lay = QVBoxLayout(theme_panel)
        theme_lay.setContentsMargins(16, 16, 16, 16)
        theme_lay.setSpacing(8)

        theme_title = QLabel("ธีมสี (Theme)", theme_panel)
        theme_title.setObjectName("panelTitle")
        theme_lay.addWidget(theme_title)

        self._theme_mode_combo = QComboBox(theme_panel)
        for key, label in (
            ("system", "ตามระบบ (System)"),
            ("light", "สว่าง (Light)"),
            ("dark", "มืด (Dark)"),
        ):
            self._theme_mode_combo.addItem(label, key)
        idx = self._theme_mode_combo.findData(theme_settings.load())
        if idx >= 0:
            self._theme_mode_combo.setCurrentIndex(idx)
        self._theme_mode_combo.currentIndexChanged.connect(self._on_theme_mode_changed)
        theme_lay.addWidget(self._theme_mode_combo)

        theme_hint = QLabel(
            "มีผลทันทีและจำค่าไว้ · terminal ของแต่ละ pane คงพื้นมืดเสมอ "
            "(สี ANSI ออกแบบมาสำหรับพื้นมืด) · ส่วนภายในหน้าต่างหลักที่วาดไว้แล้ว "
            "(แถบ project/task ที่เปิดค้าง แถบแท็บของ pane) จะตามธีมครบ 100% หลัง restart cockpit",
            theme_panel,
        )
        theme_hint.setObjectName("panelHint")
        theme_hint.setWordWrap(True)
        theme_lay.addWidget(theme_hint)
        lay.addWidget(theme_panel)

        # โหมดเครื่อง (#515) — replaces the old Performance page's numeric
        # sliders (heavy-agent caps, CPU/RAM pause thresholds, render
        # cadence) with the one control an operator actually has an opinion
        # about; the engine derives the rest from CPU/RAM instead of asking
        # the user to guess (`performance_settings.preset`, already
        # machine-aware). `_set_machine_mode` is the adapter this page binds
        # to until the engine exposes a dedicated mode API (#515 backend
        # track) — swap the write there, not here, when that lands.
        mode_panel = QWidget(view)
        mode_panel.setObjectName("panel")
        mode_lay = QVBoxLayout(mode_panel)
        mode_lay.setContentsMargins(16, 16, 16, 16)
        mode_lay.setSpacing(8)

        mode_title = QLabel("โหมดเครื่อง", mode_panel)
        mode_title.setObjectName("panelTitle")
        mode_lay.addWidget(mode_title)

        self._machine_mode_combo = QComboBox(mode_panel)
        for key, label in self._MACHINE_MODES:
            self._machine_mode_combo.addItem(label, key)
        current_mode = performance_settings.load().mode
        idx = self._machine_mode_combo.findData(current_mode)
        self._machine_mode_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self._machine_mode_combo.currentIndexChanged.connect(self._on_machine_mode_changed)
        mode_lay.addWidget(self._machine_mode_combo)

        mode_hint = QLabel(
            "สมดุล เหมาะกับการใช้งานทั่วไป · เงียบ ลดแรงกดบนเครื่องที่เปิด prod อยู่ · "
            "เต็มที่ เพิ่ม throughput แต่ยังคง CPU/RAM guard ไว้ — cap จริงคำนวณจาก CPU/RAM "
            "ของเครื่องนี้ ไม่ใช่ตัวเลขตายตัว ค่า environment TAKKUB_* มีลำดับสูงกว่าเสมอ",
            mode_panel,
        )
        mode_hint.setObjectName("panelHint")
        mode_hint.setWordWrap(True)
        mode_lay.addWidget(mode_hint)
        lay.addWidget(mode_panel)

        # ภาษา (#515) — placeholder: cockpit is Thai/English mixed UI copy
        # with no i18n layer yet, so there is nothing real to switch. Kept
        # visible (disabled) rather than omitted so "where would language
        # live" has an answer, and so wiring an actual i18n layer later is a
        # one-combo change, not a new page.
        lang_panel = QWidget(view)
        lang_panel.setObjectName("panel")
        lang_lay = QVBoxLayout(lang_panel)
        lang_lay.setContentsMargins(16, 16, 16, 16)
        lang_lay.setSpacing(8)

        lang_title = QLabel("ภาษา", lang_panel)
        lang_title.setObjectName("panelTitle")
        lang_lay.addWidget(lang_title)

        lang_combo = QComboBox(lang_panel)
        lang_combo.addItem("ไทย / English (ผสม)", "th-en")
        lang_combo.setEnabled(False)
        lang_combo.setToolTip("ยังไม่รองรับเลือกภาษา — cockpit ยังไม่มี i18n layer")
        lang_lay.addWidget(lang_combo)

        lang_hint = QLabel("เร็วๆ นี้ — ยังไม่มี i18n layer ให้เลือกภาษาจริง", lang_panel)
        lang_hint.setObjectName("panelHint")
        lang_hint.setWordWrap(True)
        lang_lay.addWidget(lang_hint)
        lay.addWidget(lang_panel)

        # #364 lever 1: discard a hidden pane's Chromium renderer after it
        # sits inactive past the debounce window — frees ~60MB/pane at the
        # current pane-ceiling, at the cost of a sub-400ms reload when the
        # user switches back. Lead panes and panes with fresh PTY output are
        # always exempt regardless of this toggle.
        discard_panel = QWidget(view)
        discard_panel.setObjectName("panel")
        discard_lay = QVBoxLayout(discard_panel)
        discard_lay.setContentsMargins(16, 16, 16, 16)
        discard_lay.setSpacing(8)
        self._pane_discard_chk = QCheckBox(
            "คืน RAM ของ pane ที่ซ่อนอยู่ (discard renderer)", discard_panel
        )
        self._pane_discard_chk.setToolTip(
            "เปิดอยู่โดยค่าเริ่มต้น หลัง pane ถูกซ่อน (สลับ tab/project ไปที่อื่น) ค้างไว้สักครู่\n"
            "cockpit จะปล่อย renderer ของ pane นั้นเพื่อคืน RAM ~60MB/pane\n"
            "กลับมาดูอีกครั้งจะ reload ให้ใหม่ภายในเสี้ยววินาที — scrollback เก่าก่อน discard\n"
            "จะกลับมาเป็นข้อความล้วน (ไม่มีสี), ข้อความที่มาระหว่างซ่อนไม่หาย สีครบเหมือนเดิม\n\n"
            "Lead pane และ pane ที่เพิ่งมี output ไม่ถูก discard ไม่ว่าตั้งค่านี้ไว้อย่างไร\n"
            "ปิดได้ที่นี่ หรือตั้ง TAKKUB_PANE_DISCARD=0"
        )
        self._pane_discard_chk.setChecked(performance_settings.load().pane_discard_enabled)
        self._pane_discard_chk.toggled.connect(self._on_pane_discard_toggled)
        discard_lay.addWidget(self._pane_discard_chk)
        lay.addWidget(discard_panel)

        # #297: the switch for automatic cockpit bug reports. Lives here rather
        # than buried in a config file because it decides whether something
        # leaves the user's machine — that has to be findable and reversible.
        report_panel = QWidget(view)
        report_panel.setObjectName("panel")
        report_lay = QVBoxLayout(report_panel)
        report_lay.setContentsMargins(16, 16, 16, 16)
        report_lay.setSpacing(8)
        self._auto_issue_chk = QCheckBox("ส่งรายงานบั๊กของ cockpit อัตโนมัติ", report_panel)
        self._auto_issue_chk.setToolTip(
            "เปิดอยู่โดยค่าเริ่มต้น เมื่อ cockpit เองมีปัญหา (crash หรือสัญญาณผิดปกติ\n"
            "ใน events.log เช่น UI ค้างยาวซ้ำ / watchdog respawn ถี่ผิดปกติ)\n"
            "จะเปิด issue ให้อัตโนมัติที่ takkub/agent-takkub\n\n"
            "ส่งเฉพาะชนิดของ event + จำนวนครั้ง + เวอร์ชัน + platform\n"
            "ไม่ส่งเนื้อ task, path ของโปรเจกต์ หรือ token (scrub + redact ก่อนส่ง)\n"
            "จำกัดไม่เกิน 5 ใบ/24 ชม. และหัวข้อเดิมซ้ำได้ไม่เกิน 1 ครั้ง/24 ชม.\n\n"
            "ปิดได้ที่นี่ หรือตั้ง TAKKUB_AUTO_ISSUE=0"
        )
        report_lay.addWidget(self._auto_issue_chk)
        report_hint = QLabel(
            "ปิดสวิตช์นี้แล้ว cockpit จะไม่ส่งอะไรออกจากเครื่องเลย — ปัญหาที่เจอจะถูกเก็บไว้ในเครื่องอย่างเดียว",
            report_panel,
        )
        report_hint.setObjectName("panelHint")
        report_hint.setWordWrap(True)
        report_lay.addWidget(report_hint)
        lay.addWidget(report_panel)

        self._auto_issue_chk.setChecked(auto_issue_signals.auto_issue_enabled())
        self._auto_issue_chk.toggled.connect(self._on_auto_issue_toggled)

        lay.addStretch(1)
        return view

    def _on_machine_mode_changed(self, _index: int) -> None:
        """Write-through (#515) — see `_set_machine_mode`'s own docstring
        for the preset-adapter contract this bridges to."""
        mode = self._machine_mode_combo.currentData() or "balanced"
        self._set_machine_mode(str(mode))

    def _set_machine_mode(self, mode: str) -> None:
        """Persist `mode` via `performance_settings.preset()` (already
        machine-aware — computes caps/thresholds from this machine's actual
        CPU/RAM) — a thin adapter standing in for a dedicated engine mode
        API that doesn't exist on `main` yet. Backend's #515 track landed
        `performance_settings.get_mode()`/`set_mode(mode)` on
        `wt/backend-1788759928` (commit e7cf89d, not yet merged as of this
        commit) — once that merges, swap this body for `set_mode(mode)`
        directly (call sites stay the same). Preserves the two fields the
        preset itself doesn't own (`pane_discard_enabled`, the overload
        dead-band timeout) from whatever is already on disk, so picking a
        mode never silently resets an unrelated toggle."""
        current = performance_settings.load()
        new_settings = _dataclass_replace(
            performance_settings.preset(mode),
            pane_discard_enabled=current.pane_discard_enabled,
            overload_deadband_timeout_s=current.overload_deadband_timeout_s,
        )
        if not performance_settings.save(new_settings):
            QMessageBox.warning(self, "โหมดเครื่อง", "บันทึก performance-settings.json ไม่สำเร็จ")
            return
        self.pending_performance_reload = True

    def _on_pane_discard_toggled(self, enabled: bool) -> None:
        """Write-through (#515, moved off the old Performance page's footer-
        staged form) — same "applies live, no second confirming click"
        pattern the theme switch and auto-issue toggle already use."""
        current = performance_settings.load()
        if not performance_settings.save(_dataclass_replace(current, pane_discard_enabled=enabled)):
            QMessageBox.warning(self, "โหมดเครื่อง", "บันทึก performance-settings.json ไม่สำเร็จ")
            return
        self.pending_performance_reload = True

    def _on_theme_mode_changed(self, _index: int) -> None:
        """Write-through + live apply (#506): persist the picked mode, rebind
        the token set, then re-apply QSS on every open window exposing a
        ``retheme()`` hook (this dialog included). Deliberately never part of
        the footer Save & Apply transaction — see _NO_FOOTER_SAVE_VIEWS."""
        mode = self._theme_mode_combo.currentData() or theme_settings.DEFAULT_MODE
        try:
            theme_settings.save(mode)
        except (OSError, ValueError):
            # Persist failure must not block the live switch — the user still
            # sees the theme they picked for this session.
            pass
        cockpit_theme.apply_variant(theme_settings.resolve_variant(mode))
        cockpit_theme.retheme_open_windows()

    def retheme(self) -> None:
        """#506: re-apply this dialog's stylesheet + placeholder palette with
        the currently-bound token set (called via
        ``cockpit_theme.retheme_open_windows``).

        #505 review M9: the parent QSS cascade above does NOT reach a child
        that was styled with its own inline ``setStyleSheet(f"...{color}...")``
        at BUILD time (Accounts' account-name/plan labels, Usage's card/
        table/quota labels) — that literal color string is baked in and the
        child never re-reads ``cockpit_theme`` again on its own. Rebuilding
        those specific children from already-held state (never re-reading
        credentials or re-querying the ledger) is cheap enough to do on
        every retheme and is the only way they pick up the new variant
        before the next full Settings reopen.
        """
        fonts = self._fonts
        self.setStyleSheet(cockpit_theme.build_stylesheet(str(fonts["sans"]), str(fonts["mono"])))
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(cockpit_theme.TEXT_MUTED))
        self.setPalette(palette)
        # Nav icons are per-variant SVG files — refresh the current tones.
        for idx, btn in self._nav_buttons.items():
            btn.setIcon(_nav_icon(idx, active=bool(btn.property("active"))))
        if hasattr(self, "_accounts_rows_box"):
            self._render_accounts_rows(self._accounts_rows_cache)
        if hasattr(self, "_usage_cards_row"):
            self._retheme_usage()

    def _on_auto_issue_toggled(self, enabled: bool) -> None:
        """Applied immediately, not on Save & Apply — a privacy switch that
        needs a second confirming click is a switch people mistrust."""
        auto_issue_signals.set_auto_issue_enabled(bool(enabled))

    # ──────────────────────────────────────────────────────────
    # view: Providers & Roles (real)
    # ──────────────────────────────────────────────────────────

    def _build_providers_roles_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        # #512 — ordered per the item list: team size, then the roster it
        # governs, then the optional secondary providers, then the detailed
        # provider connections panel (unchanged, just moved down since
        # "which positions" now leads the page it used to follow).
        lay.addWidget(self._build_team_size_panel(view))

        self._roster_panel = QWidget(view)
        roster_outer = QVBoxLayout(self._roster_panel)
        roster_outer.setContentsMargins(0, 0, 0, 0)
        roster_outer.setSpacing(0)
        lay.addWidget(self._roster_panel)
        self._rebuild_roster_panel()

        lay.addWidget(self._build_secondary_brains_panel(view))

        banner = QLabel(
            "provider ที่ปิดหรือยังไม่ติดตั้ง -> Claude รับตำแหน่งแทนอัตโนมัติ "
            "(role เดิม, engine เปลี่ยนเป็น claude — เสีย model diversity)",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        provider_panel = QWidget(view)
        provider_panel.setObjectName("panel")
        pp_lay = QVBoxLayout(provider_panel)
        pp_lay.setContentsMargins(14, 12, 14, 12)
        pp_lay.setSpacing(10)
        n_enabled = sum(1 for p in provider_state.TOGGLABLE if not provider_state.is_disabled(p))
        pp_lay.addWidget(
            self._build_card_header(
                "MODEL CONNECTIONS", "Providers", f"{n_enabled} enabled", provider_panel
            )
        )

        self._provider_toggles: dict[str, cockpit_theme.ToggleSwitch] = {}
        self._provider_model_combos: dict[str, QComboBox] = {}
        for provider in sorted(provider_state.TOGGLABLE):
            row = QWidget(provider_panel)
            row.setObjectName("providerRow")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.setSpacing(10)
            provider_role = roles_mod.by_name(provider)
            color = cockpit_theme.ROLE_COLORS.get(
                provider,
                provider_role.color if provider_role else cockpit_theme.ROLE_COLOR_FALLBACK,
            )
            row_lay.addWidget(cockpit_theme.role_chip(provider.capitalize(), color, row))
            desc = QLabel(_PROVIDER_DESC.get(provider, ""), row)
            desc.setObjectName("panelHint")
            # Without wrapping, the label's minimum width is its ENTIRE text,
            # so the row — and through it the whole view — refuses to become
            # narrower than the longest description, and the pane grows a
            # horizontal scrollbar instead of reflowing. The combo/toggle to
            # its right keep their own fixed minimums; only this filler text
            # is allowed to give.
            desc.setWordWrap(True)
            row_lay.addWidget(desc, 1)

            # Per-provider default model (provider_models.json). Editable —
            # presets are only a shortcut; anything typed passes through to
            # that provider's `--model` flag verbatim.
            model_combo = QComboBox(row)
            model_combo.setEditable(True)
            model_combo.setMinimumWidth(200)
            model_combo.setAccessibleName(f"{provider.capitalize()} model")
            _fill_model_combo(model_combo, provider, provider_models.model_for(provider))
            model_combo.currentTextChanged.connect(self._mark_dirty)
            row_lay.addWidget(model_combo)
            self._provider_model_combos[provider] = model_combo

            toggle = cockpit_theme.ToggleSwitch(
                row, checked=not provider_state.is_disabled(provider)
            )
            toggle.setAccessibleName(f"{provider.capitalize()} provider")
            toggle.toggled.connect(self._mark_dirty)
            row_lay.addWidget(toggle)
            self._provider_toggles[provider] = toggle
            pp_lay.addWidget(row)
        lay.addWidget(provider_panel)

        lay.addStretch(1)
        return view

    # ── #512: team size + roster (moved to lead this page) ─────

    def _build_team_size_panel(self, parent: QWidget) -> QWidget:
        """4 selectable cards (solo-lead/pair/full/auto) — write-through is
        staged (`self.pending_team_preset`), not immediate: this page's
        other controls (roster toggles, provider CLI) already gate behind
        the footer Save & Apply transaction, so team size follows the same
        rule rather than being the one write-through control on the page.
        Picking a card re-renders the roster panel to PREVIEW that preset's
        roles (`_rebuild_roster_panel`, reading `team_preset.resolve()` —
        never disk) so the two panels can't show conflicting state while
        the dialog is still open."""
        from . import team_preset

        panel = QWidget(parent)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(10)

        standing_id = team_preset.current_preset_id(self._project)
        # Not `_build_card_header` (its tag chip is a fire-and-forget QLabel
        # built at construction time) — this header's tag must keep tracking
        # the staged pick live as cards are clicked, so it needs a widget
        # reference `_refresh_team_size_panel` can restyle in place.
        header = QWidget(panel)
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(2)
        top_row = QWidget(header)
        top_lay = QHBoxLayout(top_row)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        mono = cockpit_theme.ensure_fonts_loaded()["mono"]
        kicker_lbl = QLabel("TEAM SIZE", top_row)
        kicker_lbl.setStyleSheet(
            f'font-family: "{mono}"; font-size: 10px; font-weight: 600; '
            f"letter-spacing: 1.5px; color: {cockpit_theme.TEXT_FAINT};"
        )
        top_lay.addWidget(kicker_lbl)
        top_lay.addStretch(1)
        self._team_size_tag_chip = cockpit_theme.gold_soft_chip(
            team_preset.label(standing_id), top_row, compact=True
        )
        top_lay.addWidget(self._team_size_tag_chip)
        header_lay.addWidget(top_row)
        title_lbl = QLabel("ขนาดทีมของโปรเจคนี้", header)
        title_lbl.setObjectName("panelTitle")
        header_lay.addWidget(title_lbl)
        p_lay.addWidget(header)

        cards_row = QWidget(panel)
        cards_lay = QHBoxLayout(cards_row)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(8)
        self._team_preset_cards: dict[str, QWidget] = {}
        for pid in team_preset.QUICK_PRESET_IDS:
            card = self._build_team_preset_card(pid, cards_row)
            cards_lay.addWidget(card, 1)
            self._team_preset_cards[pid] = card
        p_lay.addWidget(cards_row)

        self._team_preset_hint = QLabel(
            "โหมดทำเอง Lead ยังต้องรัน test/screenshot ก่อนปิดงาน และ qa-gate ยังตรวจก่อน push",
            panel,
        )
        self._team_preset_hint.setObjectName("panelHint")
        self._team_preset_hint.setWordWrap(True)
        p_lay.addWidget(self._team_preset_hint)

        mono = cockpit_theme.ensure_fonts_loaded()["mono"]
        self._team_preset_exec_line = QLabel("", panel)
        self._team_preset_exec_line.setStyleSheet(
            f'font-family: "{mono}"; font-size: 10px; color: {cockpit_theme.TEXT_FAINT};'
        )
        p_lay.addWidget(self._team_preset_exec_line)

        self._selected_team_preset_id = standing_id
        self._refresh_team_size_panel()
        return panel

    def _build_team_preset_card(self, preset_id: str, parent: QWidget) -> QWidget:
        from . import team_preset

        card = QWidget(parent)
        card.setObjectName("teamPresetCard")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setAccessibleName(f"เลือกขนาดทีม {team_preset.label(preset_id)}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(4)

        title = QLabel(team_preset.label(preset_id), card)
        title.setObjectName("panelTitle")
        lay.addWidget(title)

        desc = QLabel(team_preset.description(preset_id), card)
        desc.setObjectName("panelHint")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        mono = cockpit_theme.ensure_fonts_loaded()["mono"]
        pane_note = QLabel(team_preset.pane_note(preset_id), card)
        pane_note.setStyleSheet(
            f'font-family: "{mono}"; font-size: 10px; color: {cockpit_theme.TEXT_FAINT};'
        )
        lay.addWidget(pane_note)

        # A real painted dot, not a "○"/"●" text glyph — IBM Plex Sans/Mono
        # don't ship those code points (2026-07-24 design review #4, same
        # tofu this file's other radio/dot indicators already dodge via
        # `cockpit_theme.color_dot`).
        radio = cockpit_theme.color_dot(cockpit_theme.BORDER_STRONG2, card, size=10)
        radio.setObjectName(f"teamPresetRadio_{preset_id}")
        radio_row = QHBoxLayout()
        radio_row.setContentsMargins(0, 2, 0, 0)
        radio_row.addWidget(radio)
        radio_row.addStretch(1)
        lay.addLayout(radio_row)
        card.setProperty("_radio", radio)

        def _handler(_evt: object, pid: str = preset_id) -> None:
            self._on_team_preset_card_clicked(pid)

        card.mousePressEvent = _handler  # type: ignore[method-assign]
        return card

    def _team_preset_card_style(self, selected: bool) -> str:
        if selected:
            return (
                f"QWidget#teamPresetCard {{ background:{cockpit_theme.GOLD_CHIP_BG}; "
                f"border:1px solid {cockpit_theme.GOLD_CHIP_BORDER}; "
                f"border-radius:{cockpit_theme.RADIUS_MD}px; }}"
            )
        return (
            f"QWidget#teamPresetCard {{ background:transparent; "
            f"border:1px solid {cockpit_theme.BORDER_STRONG}; "
            f"border-radius:{cockpit_theme.RADIUS_MD}px; }}"
            f"QWidget#teamPresetCard:hover {{ border-color:{cockpit_theme.BORDER_STRONG2}; }}"
        )

    def _on_team_preset_card_clicked(self, preset_id: str) -> None:
        self._selected_team_preset_id = preset_id
        self.pending_team_preset = preset_id
        self._mark_dirty()
        self._refresh_team_size_panel()
        self._rebuild_roster_panel()

    def _refresh_team_size_panel(self) -> None:
        from . import team_preset

        for pid, card in self._team_preset_cards.items():
            selected = pid == self._selected_team_preset_id
            card.setStyleSheet(self._team_preset_card_style(selected))
            radio = card.property("_radio")
            if radio is not None:
                color = cockpit_theme.ACCENT_GOLD if selected else cockpit_theme.BORDER_STRONG2
                radio.setStyleSheet(f"background: {color}; border-radius: 5px;")

        label = team_preset.label(self._selected_team_preset_id)
        self._team_size_tag_chip.setText(label)

        cfg = team_preset.resolve(self._selected_team_preset_id, self._project)
        exec_label = "แตกหลายคน" if cfg["exec_mode"] == "parallel" else "1 คน/ตำแหน่ง"
        self._team_preset_exec_line.setText(f"โหมดทำงาน: {exec_label}")

    def _rebuild_roster_panel(self) -> None:
        """(Re)builds the 'ตำแหน่งในทีม' panel for `self._selected_team_preset_id`
        — one row per POSITION_ROLES + custom role the preset roster covers,
        Lead (always, never preset-governed), and ONE row for whichever role
        is the active checker (labeled '<role> — ตัวตรวจ'), per #512 item 2.
        Called on first build AND every team-size card click (preview, no
        disk write — see `_build_team_size_panel`)."""
        from . import team_preset as _team_preset

        outer = self._roster_panel.layout()
        while outer.count():
            item = outer.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        cfg = _team_preset.resolve(self._selected_team_preset_id, self._project)
        position_roles = list(cfg["roles"].keys())
        checker_role = _team_preset.CHECKER_ROLES.get(cfg["checker"]) if cfg["checker"] else None
        display_roles = ["lead", *position_roles, *([checker_role] if checker_role else [])]

        role_providers = provider_config.role_provider_map(display_roles, self._project)

        role_panel = QWidget(self._roster_panel)
        role_panel.setObjectName("panel")
        rp_lay = QVBoxLayout(role_panel)
        rp_lay.setContentsMargins(14, 12, 14, 12)
        rp_lay.setSpacing(10)
        n_positions = len(position_roles)
        n_positions_enabled = sum(1 for r in position_roles if cfg["roles"].get(r, False))
        roster_header_row = QHBoxLayout()
        roster_header_row.addWidget(
            self._build_card_header(
                "TEAM ROSTER",
                "ตำแหน่งในทีม",
                f"{n_positions_enabled}/{n_positions} active",
                role_panel,
            ),
            1,
        )
        # #515: New Role moved off a persistent sidebar button (reached
        # regardless of which page was showing) onto this button, scoped to
        # the one page it's actually about — opens a QDialog wrapping the
        # same form/Create-role transaction unchanged.
        new_role_btn = cockpit_theme.secondary_button("+ ตำแหน่งเฉพาะโปรเจค", role_panel)
        new_role_btn.clicked.connect(self._open_new_role_dialog)
        roster_header_row.addWidget(new_role_btn)
        rp_lay.addLayout(roster_header_row)

        self._role_toggles = {}
        self._role_provider_combos = {}
        self._role_model_combos: dict[str, QComboBox] = {}
        self._role_effort_combos: dict[str, QComboBox] = {}
        self._role_provider_badges: dict[str, QLabel] = {}
        self._lead_warning_lbl: QLabel | None = None

        # Bulk provider picker — stages the same provider change through each
        # role's existing combo, so model/effort presets, availability badges,
        # Lead's capability warning, dirty tracking, and Save & Apply all keep
        # using the exact same paths as an individual row edit.
        bulk_row = QWidget(role_panel)
        bulk_row.setObjectName("providerRow")
        bulk_lay = QHBoxLayout(bulk_row)
        bulk_lay.setContentsMargins(10, 8, 10, 8)
        bulk_lay.setSpacing(10)
        bulk_label = QLabel("All roles", bulk_row)
        bulk_label.setObjectName("panelTitle")
        bulk_lay.addWidget(bulk_label)
        bulk_hint = QLabel("เปลี่ยน provider ทุก role พร้อมกัน", bulk_row)
        bulk_hint.setObjectName("panelHint")
        bulk_lay.addWidget(bulk_hint)
        bulk_lay.addStretch(1)

        self._bulk_role_provider_combo = QComboBox(bulk_row)
        self._bulk_role_provider_combo.setAccessibleName("Provider for all roles")
        self._bulk_role_provider_combo.setMinimumWidth(150)
        self._bulk_role_provider_combo.setPlaceholderText("Select provider…")
        for provider in sorted(provider_config.VALID_PROVIDERS):
            self._bulk_role_provider_combo.addItem(provider.capitalize(), provider)
        self._bulk_role_provider_combo.setCurrentIndex(-1)
        bulk_lay.addWidget(self._bulk_role_provider_combo)

        self._bulk_role_provider_btn = cockpit_theme.secondary_button("Apply to all", bulk_row)
        self._bulk_role_provider_btn.setAccessibleName("Apply provider to all roles")
        self._bulk_role_provider_btn.setEnabled(False)
        self._bulk_role_provider_combo.currentIndexChanged.connect(
            lambda index: self._bulk_role_provider_btn.setEnabled(index >= 0)
        )
        self._bulk_role_provider_btn.clicked.connect(self._apply_provider_to_all_roles)
        bulk_lay.addWidget(self._bulk_role_provider_btn)
        rp_lay.addWidget(bulk_row)

        for role in display_roles:
            r = roles_mod.by_name(role)
            base_label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            is_lead = role == "lead"
            is_checker = checker_role is not None and role == checker_role
            row = self._build_role_row(
                role,
                f"{base_label} — ตัวตรวจ" if is_checker else base_label,
                color,
                # #101: Lead is unlocked (no longer forced to claude) but is
                # NOT a pipeline participant — no enable/disable toggle for
                # it (show_enable_toggle=False below), so its description
                # explains the CLI dropdown instead of the usual empty desc.
                "Cockpit coordinator — เปลี่ยน CLI ได้ (บาง feature หายเมื่อไม่ใช่ Claude)"
                if is_lead
                else "",
                role_panel,
                locked=False,
                enabled=True if is_lead else cfg["roles"].get(role, is_checker),
                current_provider=role_providers.get(role, provider_config.CLAUDE),
                deletable=role in custom_roles.list_role_names(),
                show_enable_toggle=not is_lead,
                lead_capability_gate=is_lead,
            )
            rp_lay.addWidget(row)

        outer.addWidget(role_panel)

    def _build_secondary_brains_panel(self, parent: QWidget) -> QWidget:
        """#512 item 3 — codex/gemini/opencode/kimi/cursor as a compact chip
        row: optional second opinions, not team POSITIONS, so no toggle
        lives here (that's still the MODEL CONNECTIONS panel below, unchanged
        — this is a read-only glance, not a second control for the same
        state)."""
        panel = QWidget(parent)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(10)
        p_lay.addWidget(self._build_card_header("OPTIONAL", "สมองเสริม", "ไม่ใช่ตำแหน่งในทีม", panel))

        chips_row = QWidget(panel)
        chips_lay = QHBoxLayout(chips_row)
        chips_lay.setContentsMargins(0, 0, 0, 0)
        chips_lay.setSpacing(10)
        for provider in sorted(provider_state.TOGGLABLE - {provider_config.CLAUDE}):
            enabled = not provider_state.is_disabled(provider)
            installed = _provider_installed(provider)
            color = (
                cockpit_theme.ROLE_COLORS.get(provider, cockpit_theme.ROLE_COLOR_FALLBACK)
                if enabled and installed
                else cockpit_theme.TEXT_MUTED
            )
            label = provider.capitalize()
            if not installed:
                label += " (ยังไม่ติดตั้ง)"
            chip = cockpit_theme.role_chip(label, color, chips_row)
            chip.setToolTip(
                "ปิดอยู่ที่ MODEL CONNECTIONS ด้านล่าง"
                if not enabled
                else ("CLI ยังไม่ติดตั้ง" if not installed else "พร้อมใช้")
            )
            chips_lay.addWidget(chip)
        chips_lay.addStretch(1)
        p_lay.addWidget(chips_row)
        return panel

    def _build_card_header(self, kicker: str, title: str, tag: str, parent: QWidget) -> QWidget:
        """Card header matching the mockup pattern — uppercase kicker line,
        title, and a right-aligned tag chip (design review 2026-07-24 #4,
        e.g. 'MODEL CONNECTIONS / Providers / 3 enabled'). Styled inline
        (existing color constants only) rather than a new cockpit_theme.py
        QSS class, since that file has an in-flight parallel edit."""
        header = QWidget(parent)
        outer = QVBoxLayout(header)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        top = QWidget(header)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        mono = cockpit_theme.ensure_fonts_loaded()["mono"]
        kicker_lbl = QLabel(kicker.upper(), top)
        kicker_lbl.setStyleSheet(
            f'font-family: "{mono}"; font-size: 10px; font-weight: 600; '
            f"letter-spacing: 1.5px; color: {cockpit_theme.TEXT_FAINT};"
        )
        top_lay.addWidget(kicker_lbl)
        top_lay.addStretch(1)
        if tag:
            top_lay.addWidget(cockpit_theme.gold_soft_chip(tag, top, compact=True))
        outer.addWidget(top)

        title_lbl = QLabel(title, header)
        title_lbl.setObjectName("panelTitle")
        outer.addWidget(title_lbl)
        return header

    def _build_role_row(
        self,
        role: str,
        label: str,
        color: str,
        desc: str,
        parent: QWidget,
        *,
        locked: bool,
        enabled: bool = True,
        current_provider: str | None = None,
        deletable: bool = False,
        show_enable_toggle: bool = True,
        lead_capability_gate: bool = False,
    ) -> QWidget:
        row = QWidget(parent)
        row.setObjectName("roleRow")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(10, 8, 10, 8)
        row_lay.setSpacing(10)
        row_lay.addWidget(cockpit_theme.role_chip(label, color, row))
        desc_lbl = QLabel(desc, row)
        desc_lbl.setObjectName("panelHint")
        desc_lbl.setWordWrap(True)  # see the provider row's own comment
        row_lay.addWidget(desc_lbl, 1)

        if locked:
            locked_lbl = QLabel("Claude (fixed)", row)
            locked_lbl.setObjectName("panelHint")
            row_lay.addWidget(locked_lbl)
            toggle = cockpit_theme.ToggleSwitch(row, checked=True)
            toggle.setEnabled(False)
            # Gemini #8/#15 — the switch is now painted muted (see
            # ToggleSwitch.paintEvent's isEnabled() branch); pair that with
            # an accessible name/tooltip so keyboard/screen-reader users get
            # the same "locked on, not a live control" signal.
            toggle.setAccessibleName(f"{label} provider — always on, locked")
            toggle.setToolTip("Lead provider เป็น Claude เสมอ — ปิด/สลับไม่ได้")
            row_lay.addWidget(toggle)
            return row

        combo = QComboBox(row)
        for provider in sorted(provider_config.VALID_PROVIDERS):
            combo.addItem(provider.capitalize(), provider)
        idx = combo.findData(current_provider)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        row_lay.addWidget(combo)
        self._role_provider_combos[role] = combo

        # Per-role model override — the model this role's panes spawn with,
        # overriding the provider-level default. Presets track the role's
        # CURRENT provider (repopulated when the CLI combo changes) and the
        # box is editable for ids not in the shortlist. Empty = inherit the
        # provider default. Stored per role in role_models.json.
        model_combo = QComboBox(row)
        model_combo.setEditable(True)
        model_combo.setMinimumWidth(180)
        model_combo.setAccessibleName(f"{label} model")
        model_combo.setToolTip("model ที่ role นี้ใช้ — ว่าง = ตาม provider default")
        _role_provider_now = combo.currentData() or provider_config.CLAUDE
        _fill_model_combo(
            model_combo,
            _role_provider_now,
            role_models.model_for(role, _role_provider_now),
        )
        row_lay.addWidget(model_combo)
        self._role_model_combos[role] = model_combo

        # Per-role reasoning-effort override — same per-provider binding as
        # the model combo above, stored in the same role_models.json entry.
        # Not editable (unlike model): the offered levels are an exact enum
        # per provider (provider_spec.effort_levels_for), so free text would
        # only ever be wrong.
        effort_combo = QComboBox(row)
        effort_combo.setMinimumWidth(140)
        effort_combo.setAccessibleName(f"{label} effort")
        _fill_effort_combo(
            effort_combo,
            _role_provider_now,
            role_models.model_for(role, _role_provider_now) or "",
            role_models.effort_for(role, _role_provider_now),
        )
        effort_combo.currentIndexChanged.connect(self._mark_dirty)
        row_lay.addWidget(effort_combo)
        self._role_effort_combos[role] = effort_combo

        # A free-typed model change can also change which effort levels are
        # offered (e.g. switching to claude-haiku-4-5), so refresh Effort
        # whenever Model changes too — not just when the CLI combo does.
        model_combo.currentTextChanged.connect(self._mark_dirty)
        model_combo.currentTextChanged.connect(
            lambda _t="", r=role: self._refresh_role_effort_combo(r)
        )
        # When the role's CLI changes, refresh its model presets to that
        # provider's list (keeping any free-typed value the user had), then
        # refresh Effort against the (possibly just-changed) model.
        combo.currentIndexChanged.connect(
            lambda _i=0, r=role: _fill_model_combo(
                self._role_model_combos[r],
                self._role_provider_combos[r].currentData() or provider_config.CLAUDE,
                _combo_model(self._role_model_combos[r]) or None,
            )
        )
        combo.currentIndexChanged.connect(lambda _i=0, r=role: self._refresh_role_effort_combo(r))

        # Gemini #12 — surface when the role's configured provider would
        # actually be substituted by Claude right now (toggled off or not
        # installed), as a styled badge rather than plain banner text.
        badge = QLabel("-> Claude", row)
        badge.setObjectName("substituteBadge")
        badge.setToolTip("provider นี้ปิดหรือยังไม่ติดตั้ง — Claude รับตำแหน่งแทน")
        row_lay.addWidget(badge)
        self._role_provider_badges[role] = badge

        # #101: Lead-specific capability-degradation warning — separate from
        # the substitute badge above (that one only fires when the chosen
        # provider is unavailable). This fires whenever Lead is pointed at
        # ANY non-claude provider, available or not, because claude-only
        # plumbing (mobile mirror, --resume, remote-control history, JSONL
        # token meter) is gone the moment Lead isn't claude — the user needs
        # to know that BEFORE saving, not discover it later as a silent gap.
        if lead_capability_gate:
            warn_lbl = QLabel("! non-Claude", row)
            warn_lbl.setObjectName("capabilityWarning")
            warn_lbl.setToolTip(
                "Lead ที่ไม่ใช่ Claude เสีย: mobile mirror · --resume · "
                "remote-control history · token/limit meter (claude-only features)"
            )
            row_lay.addWidget(warn_lbl)
            self._lead_warning_lbl = warn_lbl

        combo.currentIndexChanged.connect(lambda _i=0, r=role: self._sync_role_provider_badge(r))
        combo.currentIndexChanged.connect(self._mark_dirty)
        self._sync_role_provider_badge(role)

        if show_enable_toggle:
            toggle = cockpit_theme.ToggleSwitch(row, checked=enabled)
            toggle.setAccessibleName(f"{label} role — {'enabled' if enabled else 'disabled'}")
            toggle.setToolTip(f"เปิด/ปิด role {label} ในทีม — ปิดแล้ว assign จะถูกปฏิเสธ")
            toggle.toggled.connect(self._mark_dirty)
            row_lay.addWidget(toggle)
            self._role_toggles[role] = toggle

        # Critic visual-review round-2 #1 — a custom role could be created
        # but never removed from this view (Nielsen #3, user control &
        # freedom). Built-in roles never get this button (deletable=False
        # is the caller's default), so there's no way to delete a shipped
        # role by mistake.
        if deletable:
            delete_btn = QPushButton("x", row)
            delete_btn.setFixedWidth(28)
            delete_btn.setToolTip(f"ลบ custom role '{role}'")
            delete_btn.setAccessibleName(f"Delete {label} role")
            delete_btn.clicked.connect(
                lambda _checked=False, r=role, w=row: self._on_delete_custom_role_clicked(r, w)
            )
            row_lay.addWidget(delete_btn)

        return row

    def _apply_provider_to_all_roles(self) -> None:
        """Stage the selected provider for every rendered role.

        Deliberately drive the per-role combos instead of mutating config
        directly: their existing signals keep all dependent controls and
        warnings in sync, while the footer remains the sole commit action.
        """
        provider = self._bulk_role_provider_combo.currentData()
        if provider not in provider_config.VALID_PROVIDERS:
            return
        for combo in self._role_provider_combos.values():
            index = combo.findData(provider)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _on_delete_custom_role_clicked(self, role: str, row: QWidget) -> None:
        role_file = custom_roles.role_file_path(role)
        confirm = QMessageBox.question(
            self,
            "Delete role",
            f"ลบ custom role '{role}'?\n\n"
            f"จะลบทั้ง registry entry และไฟล์ instructions ({role_file.name}) — undo ไม่ได้",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not custom_roles.delete_role(role):
            QMessageBox.critical(self, "Delete failed", f"ลบ role '{role}' ไม่สำเร็จ")
            return
        roles_mod.unregister_role(role)
        self._role_toggles.pop(role, None)
        self._role_provider_combos.pop(role, None)
        self._role_model_combos.pop(role, None)
        self._role_effort_combos.pop(role, None)
        self._role_provider_badges.pop(role, None)
        layout = row.parentWidget().layout() if row.parentWidget() else None
        if layout is not None:
            layout.removeWidget(row)
        row.deleteLater()

    def _sync_role_provider_badge(self, role: str) -> None:
        """Show/hide the "→ Claude" substitute badge for `role` based on its
        combo's CURRENT selection (not what's on disk) — reflects what would
        happen if the user saves with this selection right now."""
        combo = self._role_provider_combos.get(role)
        badge = self._role_provider_badges.get(role)
        if combo is None or badge is None:
            return
        from .provider_config import CLAUDE, _provider_available

        provider = combo.currentData()
        badge.setVisible(provider != CLAUDE and not _provider_available(provider))

        if role == "lead":
            warn_lbl = getattr(self, "_lead_warning_lbl", None)
            if warn_lbl is not None:
                warn_lbl.setVisible(provider != CLAUDE)

    def _refresh_role_effort_combo(self, role: str) -> None:
        """Repopulate `role`'s Effort combo for its CURRENT provider+model
        selection (not what's on disk) — called whenever either changes so
        the offered levels, and the disabled/unsupported state, always
        match what Save & Apply is about to persist."""
        effort_combo = self._role_effort_combos.get(role)
        if effort_combo is None:
            return
        provider = self._role_provider_combos[role].currentData() or provider_config.CLAUDE
        model = _combo_model(self._role_model_combos[role])
        _fill_effort_combo(effort_combo, provider, model, _combo_effort(effort_combo) or None)

    def _reset_providers_roles_view(self) -> None:
        self._bulk_role_provider_combo.blockSignals(True)
        self._bulk_role_provider_combo.setCurrentIndex(-1)
        self._bulk_role_provider_combo.blockSignals(False)
        self._bulk_role_provider_btn.setEnabled(False)

        for provider, toggle in self._provider_toggles.items():
            toggle.blockSignals(True)
            toggle.setChecked(not provider_state.is_disabled(provider))
            toggle.blockSignals(False)

        for provider, combo in self._provider_model_combos.items():
            combo.blockSignals(True)
            _select_model(combo, provider_models.model_for(provider))
            combo.blockSignals(False)

        # #512: the roster panel is a full rebuild (not per-widget resets)
        # since which roles/rows it even shows depends on the selected team
        # size — resetting the pick back to the on-disk standing preset and
        # rebuilding fresh from disk covers roles/toggles/provider/model/
        # effort in one pass, the same disk reads _build_providers_roles_view
        # itself does on first open.
        from . import team_preset as _team_preset

        self.pending_team_preset = None
        self._selected_team_preset_id = _team_preset.current_preset_id(self._project)
        self._refresh_team_size_panel()
        self._rebuild_roster_panel()

    # ──────────────────────────────────────────────────────────
    # view: New Role (real)
    # ──────────────────────────────────────────────────────────

    def _open_new_role_dialog(self) -> None:
        """New Role (#515) — was a stack page reached via a persistent
        sidebar button; now a plain QDialog opened from the "+ ตำแหน่ง
        เฉพาะโปรเจค" button on "ทีม & ตำแหน่ง" instead, built fresh every
        open (mirrors `SettingsWindow` itself never caching a stale instance
        — see this class's own docstring). The form's own "+ Create Role"
        button writes through immediately (`_on_create_role_clicked`) and
        never joins the footer's Save & Apply transaction, so this dialog
        needs only a Close button, not an Accept/Reject pair."""
        dlg = QDialog(self)
        dlg.setWindowTitle("+ ตำแหน่งเฉพาะโปรเจค")
        dlg.setStyleSheet(self.styleSheet())
        avail = self._available_screen_size()
        dlg.resize(560, min(760, avail.height()))
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._wrap_scroll(self._build_new_role_view()), 1)

        close_row = QHBoxLayout()
        close_row.setContentsMargins(20, 12, 20, 16)
        close_row.addStretch(1)
        close_btn = cockpit_theme.secondary_button("Close", dlg)
        close_btn.clicked.connect(dlg.accept)
        close_row.addWidget(close_btn)
        lay.addLayout(close_row)

        dlg.exec()
        # A role created while the dialog was open changes "ตำแหน่งในทีม"
        # (`_rebuild_roster_panel` reads `custom_roles`/`team_preset` fresh
        # each call) — refresh it now rather than leaving the roster stale
        # until the next full Settings reopen.
        if hasattr(self, "_roster_panel"):
            self._rebuild_roster_panel()

    def _build_new_role_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        # ── Identity card ──────────────────────────────────────
        identity = self._new_role_card("SETUP", "Identity", "1/5", view)
        id_lay = identity.layout()
        assert isinstance(id_lay, QVBoxLayout)

        name_row = QHBoxLayout()
        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Name (--role)", identity))
        self._nr_name = QLineEdit(identity)
        self._nr_name.setPlaceholderText("data-eng (a-z0-9-_ เท่านั้น)")
        name_col.addWidget(self._nr_name)
        name_row.addLayout(name_col, 1)

        label_col = QVBoxLayout()
        label_col.addWidget(QLabel("Label", identity))
        self._nr_label = QLineEdit(identity)
        self._nr_label.setPlaceholderText("Data Eng")
        label_col.addWidget(self._nr_label)
        name_row.addLayout(label_col, 1)
        id_lay.addLayout(name_row)

        id_lay.addWidget(QLabel("Accent", identity))
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        # Codex/Gemini #17 — a gray not in the selectable palette meant no
        # swatch showed as "selected" on first open; default to the
        # palette's own first color instead.
        self._nr_color = project_nav._AVATAR_COLORS[0]
        self._nr_swatch_btns: list[QPushButton] = []
        for color in project_nav._AVATAR_COLORS:
            sw = QPushButton("", identity)
            sw.setFixedSize(20, 20)
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.clicked.connect(lambda _checked=False, c=color: self._on_swatch_clicked(c))
            self._nr_swatch_btns.append(sw)
            swatch_row.addWidget(sw)
        swatch_row.addStretch(1)
        id_lay.addLayout(swatch_row)
        self._update_swatch_selection()
        lay.addWidget(identity)

        # ── Placement card ─────────────────────────────────────
        placement = self._new_role_card("BOARD", "Placement", "2/5", view)
        pl_lay = placement.layout()
        assert isinstance(pl_lay, QVBoxLayout)

        grid_row = QHBoxLayout()
        col_col = QVBoxLayout()
        col_col.addWidget(QLabel("Grid column", placement))
        self._nr_column = QComboBox(placement)
        self._nr_column.addItem("1 · Dev column", 1)
        self._nr_column.addItem("2 · Support column", 2)
        self._nr_column.setCurrentIndex(1)
        self._nr_column.currentIndexChanged.connect(self._update_new_role_grid_hint)
        col_col.addWidget(self._nr_column)
        grid_row.addLayout(col_col)

        row_col = QVBoxLayout()
        row_col.addWidget(QLabel("Grid row", placement))
        self._nr_row = QSpinBox(placement)
        # QSpinBox renders digits with the OS locale's native numeral system
        # by default — on a Thai-locale machine that's ๐-๙, not 0-9. This
        # field feeds a JSON int (custom_roles.create_role's `row` param), so
        # force ASCII digits regardless of locale.
        self._nr_row.setLocale(QLocale(QLocale.Language.C))
        self._nr_row.setRange(0, 99)
        self._nr_row.setValue(99)
        self._nr_row.valueChanged.connect(self._update_new_role_grid_hint)
        row_col.addWidget(self._nr_row)
        grid_row.addLayout(row_col)
        grid_row.addStretch(1)
        pl_lay.addLayout(grid_row)

        self._nr_grid_hint = QLabel("", placement)
        self._nr_grid_hint.setObjectName("panelHint")
        self._nr_grid_hint.setWordWrap(True)
        pl_lay.addWidget(self._nr_grid_hint)
        self._update_new_role_grid_hint()
        lay.addWidget(placement)

        # ── Tools card ─────────────────────────────────────────
        tools = self._new_role_card("ACCESS", "Tools", "3/5", view)
        tl_lay = tools.layout()
        assert isinstance(tl_lay, QVBoxLayout)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("ใช้ default MCP+Plugins ตาม column (แนะนำ)", tools), 1)
        self._nr_default_tools_toggle = cockpit_theme.ToggleSwitch(tools, checked=True)
        toggle_row.addWidget(self._nr_default_tools_toggle)
        tl_lay.addLayout(toggle_row)
        tools_hint = QLabel(
            "เปิด (แนะนำ) = role นี้ได้ default MCP/Plugins ตาม column (Dev=เปล่า, "
            "Support=playwright+chrome-devtools) · ปิด = ไม่มี MCP/Plugins เลย ตั้งเองทีหลังผ่าน "
            "MCP Matrix / Plugins Matrix",
            tools,
        )
        tools_hint.setObjectName("panelHint")
        tools_hint.setWordWrap(True)
        tl_lay.addWidget(tools_hint)
        lay.addWidget(tools)

        # ── Skills card ────────────────────────────────────────
        skills = self._new_role_card("KNOWLEDGE", "Skills", "4/5", skills_card=True, parent=view)
        sk_lay = skills.layout()
        assert isinstance(sk_lay, QVBoxLayout)
        skills_hint = QLabel(
            "สแกนจาก .claude/skills/ จริงในโปรเจค — ติ๊กเพื่อฝัง reference "
            "เข้า instructions ให้อัตโนมัติตอนกด + Create Role ด้านล่าง",
            skills,
        )
        skills_hint.setObjectName("panelHint")
        skills_hint.setWordWrap(True)
        sk_lay.addWidget(skills_hint)

        filter_row = QHBoxLayout()
        self._nr_skill_filter = QLineEdit(skills)
        self._nr_skill_filter.setPlaceholderText("ค้นหา skill…")
        self._nr_skill_filter.textChanged.connect(self._filter_new_role_skills)
        filter_row.addWidget(self._nr_skill_filter, 1)
        self._nr_skill_count = QLabel("", skills)
        self._nr_skill_count.setObjectName("panelHint")
        filter_row.addWidget(self._nr_skill_count)
        sk_lay.addLayout(filter_row)

        skills_scroll = QScrollArea(skills)
        skills_scroll.setWidgetResizable(True)
        skills_scroll.setFrameShape(QFrame.Shape.NoFrame)
        skills_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        skills_scroll.setMaximumHeight(220)
        self._nr_skills_container = QWidget(skills_scroll)
        self._nr_skills_lay = QVBoxLayout(self._nr_skills_container)
        self._nr_skills_lay.setContentsMargins(0, 2, 0, 2)
        self._nr_skills_lay.setSpacing(4)
        skills_scroll.setWidget(self._nr_skills_container)
        sk_lay.addWidget(skills_scroll)

        self._nr_skill_checks: list[tuple[skill_scan.SkillInfo, QCheckBox]] = []
        self._reload_new_role_skills()
        lay.addWidget(skills)

        # ── Instructions card ──────────────────────────────────
        instr = self._new_role_card("BEHAVIOR", "Instructions", "5/5", view)
        in_lay = instr.layout()
        assert isinstance(in_lay, QVBoxLayout)

        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("เริ่มจากเทมเพลต", instr))
        self._nr_template_combo = QComboBox(instr)
        for stem, label in _NEW_ROLE_TEMPLATES:
            self._nr_template_combo.addItem(label, stem)
        template_row.addWidget(self._nr_template_combo, 1)
        template_btn = cockpit_theme.secondary_button("ใช้เทมเพลตนี้", instr)
        template_btn.clicked.connect(self._on_use_role_template_clicked)
        template_row.addWidget(template_btn)
        in_lay.addLayout(template_row)

        self._nr_instructions = QPlainTextEdit(instr)
        self._nr_instructions.setPlaceholderText("บอก role ตัวเองว่าทำหน้าที่อะไร ขอบเขตงานคืออะไร...")
        self._nr_instructions.setMinimumHeight(90)
        in_lay.addWidget(self._nr_instructions)
        lay.addWidget(instr)

        self._nr_status = QLabel("", view)
        self._nr_status.setObjectName("panelHint")
        self._nr_status.setWordWrap(True)
        lay.addWidget(self._nr_status)

        create_row = QHBoxLayout()
        create_btn = cockpit_theme.gold_button("+ Create Role", view)
        create_btn.clicked.connect(self._on_create_role_clicked)
        create_row.addWidget(create_btn)
        create_row.addStretch(1)
        lay.addLayout(create_row)

        lay.addStretch(1)
        return view

    def _new_role_card(
        self,
        kicker: str,
        title: str,
        tag: str,
        parent: QWidget,
        *,
        skills_card: bool = False,
    ) -> QWidget:
        card = QWidget(parent)
        card.setObjectName("panel")
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(16, 14, 16, 14)
        c_lay.setSpacing(10 if not skills_card else 8)
        c_lay.addWidget(self._build_card_header(kicker, title, tag, card))
        return card

    def _update_new_role_grid_hint(self) -> None:
        column_label = self._nr_column.currentText().split("·", 1)[-1].strip() or "column"
        self._nr_grid_hint.setText(
            f"role นี้จะแสดงที่ {column_label} แถวที่ {self._nr_row.value()} ของบอร์ด role"
        )

    def _on_use_role_template_clicked(self) -> None:
        stem = self._nr_template_combo.currentData()
        path = config.AGENTS_DIR / f"{stem}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self._nr_status.setText(f"! อ่านเทมเพลตไม่ได้: {path}")
            return
        body = _AGENT_TEMPLATE_HEADER_RE.sub("", text, count=1).strip()
        if self._nr_instructions.toPlainText().strip():
            reply = QMessageBox.question(
                self,
                "แทนที่ Instructions?",
                "Instructions มีเนื้อหาอยู่แล้ว — แทนที่ด้วยเทมเพลตนี้เลยไหม?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._nr_instructions.setPlainText(body)

    def _on_swatch_clicked(self, color: str) -> None:
        self._nr_color = color
        self._update_swatch_selection()

    def _update_swatch_selection(self) -> None:
        for btn, color in zip(self._nr_swatch_btns, project_nav._AVATAR_COLORS, strict=False):
            border = cockpit_theme.ACCENT_GOLD if color == self._nr_color else "transparent"
            btn.setStyleSheet(
                f"background:{color}; border-radius:10px; border: 2px solid {border};"
            )

    def _new_role_skill_roots(self) -> list[Path]:
        """Where to look for real `.claude/skills/` — every configured path
        of the currently-active project first (so project-specific skills
        win a name collision), plus the cockpit's own checkout as a
        fallback/supplement (dogfooding: cockpit-ui-style etc. are relevant
        to any role, and this keeps the picker non-empty even when no
        project is open, e.g. in tests that construct SettingsWindow()
        bare).

        `config.REPO_ROOT` only has a real `.claude/skills/` on a dev
        checkout — on an installed (pip/npm) build it resolves to an empty
        venv ancestor. `config.ASSETS_ROOT` is the read path that actually
        has the shipped default skill bundle there (dev checkout:
        `ASSETS_ROOT == REPO_ROOT`, harmless duplicate; installed: staged
        wheel data — see `config.SKILLS_DIR`), so both are listed.

        Phase 5a (epic #309): the real files live under
        `ASSETS_ROOT/capabilities/skills` now, not `.claude/skills` — best-
        effort repairs the discovery surface first so this picker isn't
        empty on a session where no pane has spawned yet."""
        try:
            from .core.capabilities.skill_store import ensure_shipped_skill_surface

            ensure_shipped_skill_surface()
        except Exception:
            pass
        roots: list[Path] = []
        if self._project:
            roots.extend(_allowed_project_roots(self._project))
        roots.append(config.REPO_ROOT)
        roots.append(config.ASSETS_ROOT)
        return roots

    def _reload_new_role_skills(self) -> None:
        while self._nr_skills_lay.count():
            item = self._nr_skills_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # takeAt() only detaches the widget from the layout — it
                # stays a live, visible child of `_nr_skills_container` until
                # deleteLater()'s DeferredDelete event actually runs on the
                # next event-loop tick. Reload can be triggered twice in
                # close succession (initial build, then again after a New
                # Skill create at line ~2762) with no event-loop turn between
                # them, so the stale widget was still on screen when the new
                # layout placed a fresh row at the same position — rendered
                # as overlapping/garbled text. hide() removes it from the
                # screen immediately; deleteLater() still reclaims it later.
                w.hide()
                w.deleteLater()
        self._nr_skill_checks = []

        skills = skill_scan.scan_skills(self._new_role_skill_roots())
        if not skills:
            empty = QLabel("ไม่พบ skill ใน .claude/skills/ ของโปรเจคนี้", self._nr_skills_container)
            empty.setObjectName("panelHint")
            self._nr_skills_lay.addWidget(empty)
            self._update_new_role_skill_count()
            return
        writable_roots = self._writable_skill_roots()
        central_dirs = self._central_skill_dirs()
        for skill in skills:
            row = self._build_new_role_skill_row(skill, writable_roots, central_dirs)
            self._nr_skills_lay.addWidget(row)
        self._update_new_role_skill_count()

    def _build_new_role_skill_row(
        self,
        skill: skill_scan.SkillInfo,
        writable_roots: list[Path],
        central_dirs: list[Path],
    ) -> QWidget:
        """One skill = one row: checkbox (name only — packing the full
        description into the checkbox text is what made the form overflow
        horizontally, see docs/audit/2026-08-13-new-role-redesign.md) plus a
        word-wrapped description line and a small source badge."""
        row = QWidget(self._nr_skills_container)
        row_lay = QVBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(8)
        chk = QCheckBox(skill.name, row)
        chk.toggled.connect(self._update_new_role_skill_count)
        top.addWidget(chk)
        source = (
            "project"
            if skill_scan.is_writable_skill(skill.path, writable_roots, central_dirs)
            else "cockpit"
        )
        source_lbl = QLabel(f"· {source}", row)
        source_lbl.setObjectName("panelHint")
        top.addWidget(source_lbl)
        top.addStretch(1)
        row_lay.addLayout(top)

        if skill.description:
            desc = QLabel(_clamp_skill_description(skill.description), row)
            desc.setObjectName("panelHint")
            desc.setWordWrap(True)
            desc.setContentsMargins(22, 0, 0, 0)
            desc.setToolTip(skill.description)
            row_lay.addWidget(desc)

        self._nr_skill_checks.append((skill, chk))
        return row

    def _filter_new_role_skills(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._nr_skills_lay.count()):
            row = self._nr_skills_lay.itemAt(i).widget()
            if row is None:
                continue
            skill = next((s for s, chk in self._nr_skill_checks if chk.parentWidget() is row), None)
            if skill is None:
                continue
            hay = f"{skill.name} {skill.description}".lower()
            row.setVisible(not needle or needle in hay)

    def _update_new_role_skill_count(self, *_args: object) -> None:
        total = len(self._nr_skill_checks)
        checked = sum(1 for _skill, chk in self._nr_skill_checks if chk.isChecked())
        self._nr_skill_count.setText(f"{checked}/{total} skill ที่เลือก" if total else "")

    def _selected_new_role_skills(self) -> list[skill_scan.SkillInfo]:
        return [skill for skill, chk in self._nr_skill_checks if chk.isChecked()]

    def _on_create_role_clicked(self) -> bool:
        """Validate + persist the New Role form. Returns True iff the role
        was actually created — the footer Save & Apply button (#2) uses this
        return value to decide whether it's safe to close the dialog; an
        invalid/incomplete form must not accept() and discard typed input."""
        name = self._nr_name.text().strip().lower()
        label = self._nr_label.text().strip()
        column = self._nr_column.currentData()
        row = self._nr_row.value()
        instructions_text = self._nr_instructions.toPlainText().strip()
        selected_skills = self._selected_new_role_skills()
        if selected_skills:
            base = instructions_text or custom_roles._default_role_template(
                name, label or name.capitalize()
            )
            instructions = _append_skill_references(base, selected_skills)
        else:
            instructions = instructions_text or None

        ok, err = custom_roles.create_role(name, label, self._nr_color, column, row, instructions)
        if not ok:
            self._nr_status.setText(f"! {err}")
            return False

        # Register in THIS process immediately so `--role <name>` spawns
        # without waiting for a cockpit restart (roles.py otherwise only
        # loads custom-roles.json at boot) — same pattern pane_tools_dialog
        # uses for the same reason.
        role = custom_roles.load_custom_roles().get(name)
        if role is not None:
            roles_mod.register_role(role)

        tools_ok = self._apply_new_role_tools_policy(name, column)

        status = (
            f"OK: สร้าง role '{name}' แล้ว — spawn ได้ทันทีด้วย "
            f'`takkub assign --role {name} "..."` (ไม่ต้อง restart cockpit)'
        )
        if not tools_ok:
            status += "\n! แต่บันทึก MCP/Plugins default ไม่สำเร็จ — ตั้งเองผ่าน MCP/Plugins Matrix"
        self._nr_status.setText(status)
        self._reset_new_role_form(clear_status=False)
        return True

    def _apply_new_role_tools_policy(self, name: str, column: int) -> bool:
        """Seed pane_tools_policy for a freshly created role. Checked "use
        default" → the same MCP defaults the matching dev/support-column
        built-in role gets, plus the lean teammate plugin set; unchecked →
        an explicit empty policy the operator fills in later via the
        MCP/Plugins Matrix views.

        Returns True iff both writes succeeded — `set_role_items()` never
        raises (validation/IO failures return False), so the caller must
        check this explicitly rather than assume the seed always lands (and
        `regen_role_variants()` only runs when it actually did)."""
        if self._nr_default_tools_toggle.isChecked():
            from .lead_context import _TEAMMATE_PLUGINS

            mcps = list(_NEW_ROLE_COLUMN_MCPS.get(column, frozenset()))
            plugins = list(_TEAMMATE_PLUGINS)
        else:
            mcps, plugins = [], []
        mcps_ok = pane_tools_policy.set_role_items(name, "mcps", mcps)
        plugins_ok = pane_tools_policy.set_role_items(name, "plugins", plugins)
        if mcps_ok and plugins_ok:
            shared_dev_tools.regen_role_variants()
        return mcps_ok and plugins_ok

    def _reset_new_role_form(self, clear_status: bool = True) -> None:
        # Block signals while programmatically resetting — every field is
        # wired to _mark_dirty (#6) so a plain .clear()/.setValue() here
        # would immediately re-mark the view dirty right after a successful
        # create, or right after the user asked to revert it.
        for w in (
            self._nr_name,
            self._nr_label,
            self._nr_instructions,
            self._nr_column,
            self._nr_row,
            self._nr_default_tools_toggle,
        ):
            w.blockSignals(True)
        try:
            self._nr_name.clear()
            self._nr_label.clear()
            self._nr_instructions.clear()
            self._nr_column.setCurrentIndex(1)
            self._nr_row.setValue(99)
            self._nr_default_tools_toggle.setChecked(True)
        finally:
            for w in (
                self._nr_name,
                self._nr_label,
                self._nr_instructions,
                self._nr_column,
                self._nr_row,
                self._nr_default_tools_toggle,
            ):
                w.blockSignals(False)
        for _skill, chk in self._nr_skill_checks:
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self._nr_color = project_nav._AVATAR_COLORS[0]
        self._update_swatch_selection()
        self._update_new_role_grid_hint()
        self._update_new_role_skill_count()
        self._nr_skill_filter.blockSignals(True)
        self._nr_skill_filter.clear()
        self._nr_skill_filter.blockSignals(False)
        self._filter_new_role_skills("")
        if clear_status:
            self._nr_status.setText("")

    # ──────────────────────────────────────────────────────────
    # view: Users (real — ported from user_actions.open_user_profiles_dialog,
    # 2026-07-11: previously a standalone modal QDialog reached only via the
    # 👥 Team chip's right-click menu; the user wanted it back as a directly
    # visible Team tab, not a popup). Every action here writes through
    # immediately (add/remove/share profile, save auth) — same "list ธรรมดา,
    # ไม่มี OK/Cancel" browse-and-act pattern as Skill Catalog, so this view
    # never participates in the footer's dirty-tracking/Save & Apply (no
    # _mark_dirty calls below, mirroring Skill Catalog/Templates).
    # ──────────────────────────────────────────────────────────

    def _build_users_view(self) -> QWidget:
        """The unified **Accounts** page (#505) — provider rows + account
        cards (built by `AccountsSettingsMixin`), with the per-profile Claude
        API override form kept as an advanced second tab."""
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(10)

        self._up_profiles: list[dict] = user_profile.list_profiles()

        tabs = QTabWidget(view)
        tabs.addTab(self._build_accounts_tab(tabs), "บัญชี")
        tabs.addTab(self._build_users_auth_tab(tabs), "การเชื่อมต่อ API (ขั้นสูง)")
        lay.addWidget(tabs, 1)

        self._up_status = QLabel("", view)
        self._up_status.setObjectName("panelHint")
        self._up_status.setWordWrap(True)
        lay.addWidget(self._up_status)

        return view

    def _users_status(self, msg: str) -> None:
        self._up_status.setText(msg)

    def _build_users_auth_tab(self, parent: QWidget) -> QWidget:
        tab = QWidget(parent)
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        intro = QLabel(
            "Point a profile's Claude Code panes at a different backend — DeepSeek,\n"
            "OpenRouter, a local model — instead of Anthropic. These settings are\n"
            "saved *per profile*: leave them blank and that profile keeps its normal\n"
            "Claude login; set a base URL and only that profile's panes use the API.\n"
            "Applies to the next pane you spawn (restart open panes to pick it up).",
            tab,
        )
        intro.setObjectName("panelHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        panel = QWidget(tab)
        panel.setObjectName("panel")
        self._up_auth_panel = panel
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(8)

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Settings for profile:", panel))
        self._up_auth_combo = QComboBox(panel)
        for p in self._up_profiles:
            self._up_auth_combo.addItem(p["name"])
        self._up_auth_combo.setToolTip(
            "Each profile has its own auth. Switching reloads that profile's saved\n"
            "values from disk — Save before switching to keep unsaved edits."
        )
        sel_row.addWidget(self._up_auth_combo, 1)
        p_lay.addLayout(sel_row)

        auth_form = QFormLayout()
        auth_form.setHorizontalSpacing(16)
        auth_form.setVerticalSpacing(8)
        p_lay.addLayout(auth_form)

        self._up_base_url = QLineEdit(panel)
        self._up_base_url.setPlaceholderText(
            "blank = Anthropic  ·  e.g. https://api.deepseek.com/anthropic"
        )
        auth_form.addRow("Base URL:", self._up_base_url)

        self._up_api_key = QLineEdit(panel)
        self._up_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._up_api_key.setPlaceholderText("your provider's API key  ·  blank = none")
        auth_form.addRow("API key:", self._up_api_key)

        self._up_auth_token = QLineEdit(panel)
        self._up_auth_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._up_auth_token.setPlaceholderText(
            "usually blank — the API key above is reused as the bearer token"
        )
        auth_form.addRow("Auth token:", self._up_auth_token)

        note = QLabel(
            "Examples:\n"
            "• DeepSeek — Base URL: https://api.deepseek.com/anthropic + API key: your DeepSeek key\n"
            "• OpenRouter — Base URL: https://openrouter.ai/api + Auth token: your OpenRouter key\n"
            "  (then add ANTHROPIC_DEFAULT_SONNET_MODEL below to choose the model)",
            panel,
        )
        note.setObjectName("panelHint")
        note.setWordWrap(True)
        p_lay.addWidget(note)

        env_label = QLabel(
            "Extra environment variables — sent to every pane. Use for a provider key,\n"
            "or to pick a model (e.g. ANTHROPIC_DEFAULT_SONNET_MODEL = qwen/qwen3-coder:free):",
            panel,
        )
        env_label.setObjectName("panelHint")
        env_label.setWordWrap(True)
        p_lay.addWidget(env_label)

        self._up_env_rows: list[tuple[QLineEdit, QLineEdit, QWidget]] = []
        self._up_env_rows_box = QVBoxLayout()
        self._up_env_rows_box.setSpacing(4)
        p_lay.addLayout(self._up_env_rows_box)

        add_env_btn = cockpit_theme.secondary_button("+ Add variable", panel)
        add_env_btn.clicked.connect(lambda: self._add_users_env_row())
        p_lay.addWidget(add_env_btn)

        save_row = QHBoxLayout()
        save_btn = cockpit_theme.gold_button("Save", panel)
        save_btn.clicked.connect(self._on_users_save_auth_clicked)
        save_row.addWidget(save_btn)
        save_row.addStretch(1)
        p_lay.addLayout(save_row)

        lay.addWidget(panel)
        lay.addStretch(1)

        self._up_auth_combo.currentTextChanged.connect(self._load_users_auth_profile)
        self._load_users_auth_profile(self._up_auth_combo.currentText())

        return tab

    def _on_users_save_auth_clicked(self) -> None:
        profile_name = self._up_auth_combo.currentText()
        env_dict: dict[str, str] = {}
        for name_ed, value_ed, _row in self._up_env_rows:
            name = name_ed.text().strip()
            if name:
                env_dict[name] = value_ed.text()
        try:
            save_claude_auth(
                ClaudeAuthConfig(
                    base_url=self._up_base_url.text(),
                    api_key=self._up_api_key.text(),
                    auth_token=self._up_auth_token.text(),
                    extra_env=env_dict,
                ),
                self._users_auth_dir(profile_name),
            )
            self._users_status(
                f"Claude auth saved for profile '{profile_name}' — respawn its "
                "panes to use the new settings."
            )
        except OSError as e:
            QMessageBox.critical(
                self, "Save failed", f"Couldn't write takkub-claude-auth.json:\n{e}"
            )

    def _users_auth_dir(self, profile_name: str) -> Path | None:
        """config_dir for *profile_name* (None → default ~/.claude)."""
        for p in self._up_profiles:
            if p["name"] == profile_name:
                return Path(p["config_dir"])
        return None

    def _reload_users_auth_combo(self) -> None:
        """Refresh the Claude Auth tab's profile combo after Profiles-tab
        add/remove — keeps the current selection if it still exists,
        otherwise falls back to row 0 (matches _build_role_row's
        find-or-default pattern used elsewhere in this window)."""
        combo = self._up_auth_combo
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for p in self._up_profiles:
            combo.addItem(p["name"])
        idx = combo.findText(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        self._load_users_auth_profile(combo.currentText())

    def _load_users_auth_profile(self, profile_name: str) -> None:
        """Populate the auth fields from *profile_name*'s saved config."""
        loaded = load_claude_auth(self._users_auth_dir(profile_name))
        self._up_base_url.setText(loaded.base_url)
        self._up_api_key.setText(loaded.api_key)
        self._up_auth_token.setText(loaded.auth_token)
        self._clear_users_env_rows()
        for name, value in loaded.extra_env.items():
            self._add_users_env_row(name, value)
        if not self._up_env_rows:
            self._add_users_env_row()

    def _add_users_env_row(self, name: str = "", value: str = "") -> None:
        row = QWidget(self._up_auth_panel)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        name_edit = QLineEdit(name, row)
        name_edit.setPlaceholderText("NAME — e.g. ANTHROPIC_DEFAULT_SONNET_MODEL")
        value_edit = QLineEdit(value, row)
        value_edit.setPlaceholderText("value — e.g. qwen/qwen3-coder:free")
        remove_btn = QPushButton("x", row)
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip("Remove this variable")

        h.addWidget(name_edit, 2)
        h.addWidget(value_edit, 3)
        h.addWidget(remove_btn, 0)

        entry = (name_edit, value_edit, row)
        self._up_env_rows.append(entry)
        self._up_env_rows_box.addWidget(row)

        def _remove() -> None:
            if entry in self._up_env_rows:
                self._up_env_rows.remove(entry)
            self._up_env_rows_box.removeWidget(row)
            row.deleteLater()

        remove_btn.clicked.connect(_remove)

    def _clear_users_env_rows(self) -> None:
        for _n, _v, row in list(self._up_env_rows):
            self._up_env_rows_box.removeWidget(row)
            row.deleteLater()
        self._up_env_rows.clear()

    # ──────────────────────────────────────────────────────────
    # shared: role×item toggle matrix (MCP Matrix / Plugins Matrix)
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _clear_grid(grid: QGridLayout) -> None:
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _populate_matrix_grid(
        self,
        grid: QGridLayout,
        roles: tuple[str, ...],
        items: list[str],
        matrix: dict[str, dict[str, bool]],
        *,
        column_category: str = "",
        item_costs: dict[str, int] | None = None,
    ) -> dict[str, dict[str, cockpit_theme.ToggleSwitch]]:
        """Fill *grid* (already parented to a panel widget) with a role×item
        toggle matrix: col 0 = 180px role-chip column, cols 1..N = 1fr item
        columns of centered ``ToggleSwitch`` cells. Clears any prior content
        first, so this doubles as the reload/refresh path.

        *column_category* (design review 2026-07-24 #3) — a second line
        under each item header naming what kind of column it is (e.g. "MCP"
        / "Plugin"), so the matrix doesn't rely on the reader already
        knowing what the sidebar section implied. Each role row also gets a
        hairline bottom-border (inline, not a new global QSS class) so the
        eye can track a row across many toggle columns.

        *item_costs* (#516) — optional estimated boot-token cost per item
        (see `token_estimate`/`pane_tools_dialog.marketplace_token_costs`),
        shown as a third header line next to the item's name so an operator
        sees what enabling it actually costs every pane of that role."""
        self._clear_grid(grid)
        panel = grid.parentWidget()
        grid.setColumnMinimumWidth(0, 160)
        grid.setColumnStretch(0, 0)
        role_header = QLabel("Role", panel)
        role_header.setObjectName("matrixHeaderCell")
        grid.addWidget(role_header, 0, 0)
        for col, item in enumerate(items, start=1):
            cost = (item_costs or {}).get(item)
            header = self._build_matrix_column_header(
                item, column_category, panel, cost_tokens=cost
            )
            grid.addWidget(header, 0, col)
            grid.setColumnStretch(col, 1)

        row_hairline = f"border-bottom: 1px solid {cockpit_theme.BORDER_HAIRLINE};"
        boxes: dict[str, dict[str, cockpit_theme.ToggleSwitch]] = {}
        for row, role in enumerate(roles, start=1):
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            chip_cell = QWidget(panel)
            chip_cell_lay = QHBoxLayout(chip_cell)
            chip_cell_lay.setContentsMargins(0, 6, 0, 6)
            chip_cell.setStyleSheet(row_hairline)
            chip_cell_lay.addWidget(cockpit_theme.role_chip(label, color, chip_cell))
            grid.addWidget(chip_cell, row, 0)
            boxes[role] = {}
            for col, item in enumerate(items, start=1):
                checked = matrix.get(role, {}).get(item, False)
                toggle = cockpit_theme.ToggleSwitch(panel, checked=checked)
                toggle.toggled.connect(self._mark_dirty)
                cell = QWidget(panel)
                cell.setStyleSheet(row_hairline)
                cell_lay = QHBoxLayout(cell)
                cell_lay.setContentsMargins(0, 4, 0, 4)
                cell_lay.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(cell, row, col)
                boxes[role][item] = toggle
        return boxes

    def _build_matrix_column_header(
        self,
        item: str,
        category: str,
        parent: QWidget,
        *,
        cost_tokens: int | None = None,
    ) -> QWidget:
        """One matrix header cell: item name + a muted category sublabel
        underneath (design review 2026-07-24 #3), plus an estimated
        boot-token cost line when *cost_tokens* is given (#516)."""
        cell = QWidget(parent)
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        name_lbl = QLabel(item, cell)
        name_lbl.setObjectName("matrixHeaderCell")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setToolTip(item)
        lay.addWidget(name_lbl)
        if cost_tokens is not None:
            cost_lbl = QLabel(token_estimate.format_tokens(cost_tokens), cell)
            cost_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cost_lbl.setToolTip(
                "Estimated boot-context cost — every pane of a role with this "
                "enabled loads it at spawn (#516)"
            )
            cost_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_FAINT}; font-size: 10px;")
            lay.addWidget(cost_lbl)
        if category:
            cat_lbl = QLabel(category, cell)
            cat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cat_lbl.setStyleSheet(
                f"color: {cockpit_theme.TEXT_FAINT}; font-size: 10px; "
                f"border-bottom: 1px solid {cockpit_theme.BORDER_HAIRLINE}; padding-bottom: 6px;"
            )
            lay.addWidget(cat_lbl)
        return cell

    def _build_matrix_legend(self, parent: QWidget) -> QWidget:
        """Legend footer under a role×item matrix — 'Allowed'/'Blocked' dot
        key + a validation note, matching the mockup (design review
        2026-07-24 #2 in the '➕ เพิ่ม' section: 'Matrix legend footer')."""
        legend = QWidget(parent)
        lay = QHBoxLayout(legend)
        lay.setContentsMargins(4, 6, 4, 0)
        lay.setSpacing(6)

        lay.addWidget(cockpit_theme.color_dot(cockpit_theme.ACCENT_GOLD, legend, size=8))
        allowed_lbl = QLabel("Allowed", legend)
        allowed_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(allowed_lbl)

        lay.addSpacing(10)
        lay.addWidget(cockpit_theme.color_dot(cockpit_theme.TEXT_FAINT, legend, size=8))
        blocked_lbl = QLabel("Blocked", legend)
        blocked_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(blocked_lbl)

        lay.addStretch(1)
        note_lbl = QLabel("Security policy จะถูก validate ก่อน Apply", legend)
        note_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_FAINT}; font-size: 11px;")
        lay.addWidget(note_lbl)
        return legend

    # ──────────────────────────────────────────────────────────
    # view: Tools (#515 — merged MCP Matrix + Plugins Matrix into one page,
    # tabbed: same "one role × policy grid" shape, just two tabs on it
    # instead of two separate nav entries)
    # ──────────────────────────────────────────────────────────

    def _build_tools_view(self) -> QWidget:
        tabs = QTabWidget(self)
        tabs.addTab(self._build_mcp_matrix_view(), "MCP")
        tabs.addTab(self._build_plugins_matrix_view(), "Plugins")
        return tabs

    # ──────────────────────────────────────────────────────────
    # view: MCP Matrix (real)
    # ──────────────────────────────────────────────────────────

    def _build_mcp_matrix_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        add_row = QHBoxLayout()
        add_btn = cockpit_theme.secondary_button("+ Add MCP server", view)
        add_btn.clicked.connect(self._on_add_mcp_server_clicked)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        lay.addLayout(add_row)

        # Design review 2026-07-24 #3 — an empty MCP registry used to show a
        # small hint line ABOVE a matrix panel that still rendered (just a
        # bare role-name list, no columns). Now the panel hides entirely and
        # this becomes the dashed-border empty-state block (reuses
        # cockpit_theme's `placeholderBadge` QSS — same class the other
        # not-yet-real views already use, so no new stylesheet class needed).
        self._mcp_empty = QLabel("ยังไม่มี MCP server — กด “+ Add MCP server” ด้านบนเพื่อเริ่ม", view)
        self._mcp_empty.setObjectName("placeholderBadge")
        self._mcp_empty.setWordWrap(True)
        self._mcp_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mcp_empty.hide()
        lay.addWidget(self._mcp_empty)

        matrix_panel = QWidget(view)
        matrix_panel.setObjectName("panel")
        self._mcp_matrix_panel = matrix_panel
        panel_lay = QVBoxLayout(matrix_panel)
        panel_lay.setContentsMargins(14, 12, 14, 12)
        panel_lay.setSpacing(4)
        grid_host = QWidget(matrix_panel)
        self._mcp_grid = QGridLayout(grid_host)
        self._mcp_grid.setContentsMargins(0, 0, 0, 0)
        self._mcp_grid.setHorizontalSpacing(6)
        self._mcp_grid.setVerticalSpacing(4)
        panel_lay.addWidget(grid_host)
        panel_lay.addWidget(self._build_matrix_legend(matrix_panel))
        lay.addWidget(matrix_panel)
        lay.addStretch(1)

        self._reload_mcp_matrix()
        return view

    def _reload_mcp_matrix(self) -> None:
        items = pane_tools_dialog.master_mcps()
        self._orig_mcp_items = pane_tools_dialog.policy_role_items(_matrix_roles(), "mcps")
        matrix = pane_tools_dialog.build_matrix(_matrix_roles(), items, self._orig_mcp_items)
        self._mcp_toggles = self._populate_matrix_grid(
            self._mcp_grid, _matrix_roles(), items, matrix, column_category="MCP"
        )
        self._mcp_empty.setVisible(not items)
        self._mcp_matrix_panel.setVisible(bool(items))

    def _on_add_mcp_server_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("+ Add MCP server")
        dlg.setStyleSheet(self.styleSheet())
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        form.setContentsMargins(18, 18, 18, 14)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        name_edit = QLineEdit(dlg)
        command_edit = QLineEdit(dlg)
        args_edit = QLineEdit(dlg)
        args_edit.setPlaceholderText("-y some-mcp-package (เว้นวรรคคั่น)")
        form.addRow("ชื่อ", name_edit)
        form.addRow("Command", command_edit)
        form.addRow("Args", args_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        parsed = pane_tools_dialog.parse_install_form(
            name_edit.text(), command_edit.text(), args_edit.text()
        )
        if parsed is None:
            QMessageBox.warning(self, "Add MCP server", "กรอกชื่อและ command ให้ครบ")
            return
        name, cfg = parsed
        try:
            if not shared_dev_tools.add_mcp_server(name, cfg):
                QMessageBox.warning(self, "Add MCP server ไม่สำเร็จ", f"เพิ่ม '{name}' ไม่ได้")
                return
        except Exception as e:
            QMessageBox.warning(self, "Add MCP server ไม่สำเร็จ", str(e))
            return
        self._reload_mcp_matrix()
        self._mark_dirty()

    # ──────────────────────────────────────────────────────────
    # view: Plugins Matrix (real)
    # ──────────────────────────────────────────────────────────

    def _build_plugins_matrix_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        banner = QLabel(
            "security-guidance และ remember ถูก denylist ปิดเสมอทุก pane "
            "(hook หนัก ทำ spawn ช้า) — policy นี้เปิดให้ไม่ได้",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        # #516: a plugin's per-role toggle isn't free — every pane of a role
        # with it enabled loads it at spawn, every time.
        cost_hint = QLabel(
            "plugin ที่เปิดถูกโหลดเข้าทุก pane ของตำแหน่งนี้ตอนเริ่ม — "
            "ตัวเลขใต้ชื่อคือ token cost โดยประมาณต่อการเปิด (#516)",
            view,
        )
        cost_hint.setObjectName("panelHint")
        cost_hint.setWordWrap(True)
        lay.addWidget(cost_hint)

        self._plugins_empty = QLabel(
            "ไม่พบ marketplace plugin — ยังไม่มีอะไรใน installed_plugins.json", view
        )
        self._plugins_empty.setObjectName("placeholderBadge")
        self._plugins_empty.setWordWrap(True)
        self._plugins_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plugins_empty.hide()
        lay.addWidget(self._plugins_empty)

        matrix_panel = QWidget(view)
        matrix_panel.setObjectName("panel")
        self._plugins_matrix_panel = matrix_panel
        panel_lay = QVBoxLayout(matrix_panel)
        panel_lay.setContentsMargins(14, 12, 14, 12)
        panel_lay.setSpacing(4)
        grid_host = QWidget(matrix_panel)
        self._plugins_grid = QGridLayout(grid_host)
        self._plugins_grid.setContentsMargins(0, 0, 0, 0)
        self._plugins_grid.setHorizontalSpacing(6)
        self._plugins_grid.setVerticalSpacing(4)
        panel_lay.addWidget(grid_host)
        panel_lay.addWidget(self._build_matrix_legend(matrix_panel))
        lay.addWidget(matrix_panel)
        lay.addStretch(1)

        self._reload_plugins_matrix()
        return view

    def _reload_plugins_matrix(self) -> None:
        # Marketplace-granular columns (NOT name@marketplace) — see
        # pane_tools_dialog.discover_marketplaces's own note: the policy
        # stores marketplace names, so only these make a checkbox's identity
        # match what gets read back on Save.
        items = pane_tools_dialog.discover_marketplaces()
        full_orig = pane_tools_dialog.policy_role_items(_matrix_roles(), "plugins")
        rendered = set(items)
        # A role's built-in default can name a marketplace with no column
        # here (not installed on this machine) — stash it so Save re-adds it
        # instead of silently dropping it as "unchecked".
        self._hidden_plugin_defaults = {
            r: [m for m in v if m not in rendered] for r, v in full_orig.items()
        }
        self._orig_plugin_items = {r: [m for m in v if m in rendered] for r, v in full_orig.items()}
        matrix = pane_tools_dialog.build_matrix(_matrix_roles(), items, self._orig_plugin_items)
        costs = pane_tools_dialog.marketplace_token_costs(items)
        self._plugin_toggles = self._populate_matrix_grid(
            self._plugins_grid,
            _matrix_roles(),
            items,
            matrix,
            column_category="Plugin",
            item_costs=costs,
        )
        self._plugins_empty.setVisible(not items)
        self._plugins_matrix_panel.setVisible(bool(items))

    # ──────────────────────────────────────────────────────────
    # view: Skills (#515 — merged Skill Catalog + Skill Matrix into one
    # page, tabbed: browse (Catalog) and per-role policy (Matrix) are the
    # same underlying skill set, just two different questions about it)
    # ──────────────────────────────────────────────────────────

    def _build_skills_view(self) -> QWidget:
        tabs = QTabWidget(self)
        tabs.addTab(self._build_skill_catalog_view(), "Catalog")
        tabs.addTab(self._build_skill_matrix_view(), "Matrix")
        return tabs

    # ──────────────────────────────────────────────────────────
    # view: Skill Catalog (real — SKILL section)
    #
    # The genuine skill browser: lists real Claude Code skills scanned from
    # `.claude/skills/*/SKILL.md` (same `skill_scan` the New Role picker uses,
    # across the active project's roots + the cockpit checkout). For each
    # skill it shows name + description + which ROLE instruction docs mention
    # it (substring match on the skill name over `skill_audit.load_all_role_
    # docs()`), so an operator sees who already relies on a given skill.
    # Read-only browse — no dirty-tracking / Save.
    # ──────────────────────────────────────────────────────────

    def _build_skill_catalog_view(self) -> QWidget:
        view = QWidget(self)
        outer = QVBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 16)
        outer.setSpacing(12)

        lay = QHBoxLayout()
        lay.setSpacing(12)

        self._catalog_role_docs = skill_audit.load_all_role_docs()

        list_panel = QWidget(view)
        list_panel.setObjectName("panel")
        list_panel.setFixedWidth(220)
        list_lay = QVBoxLayout(list_panel)
        list_lay.setContentsMargins(6, 6, 6, 6)
        self._catalog_list = QListWidget(list_panel)
        self._catalog_list.setFrameShape(QFrame.Shape.NoFrame)
        self._catalog_list.currentItemChanged.connect(self._on_catalog_skill_selected)
        list_lay.addWidget(self._catalog_list)
        lay.addWidget(list_panel)

        detail_panel = QWidget(view)
        detail_panel.setObjectName("panel")
        detail_lay = QVBoxLayout(detail_panel)
        detail_lay.setContentsMargins(14, 12, 14, 12)
        detail_lay.setSpacing(8)

        self._catalog_name = QLabel("", detail_panel)
        self._catalog_name.setObjectName("panelTitle")
        detail_lay.addWidget(self._catalog_name)
        self._catalog_desc = QLabel("", detail_panel)
        self._catalog_desc.setObjectName("panelHint")
        self._catalog_desc.setWordWrap(True)
        detail_lay.addWidget(self._catalog_desc)
        self._catalog_roles = QLabel("", detail_panel)
        self._catalog_roles.setObjectName("panelHint")
        self._catalog_roles.setWordWrap(True)
        detail_lay.addWidget(self._catalog_roles)
        self._catalog_path = QLabel("", detail_panel)
        self._catalog_path.setObjectName("panelHint")
        self._catalog_path.setWordWrap(True)
        detail_lay.addWidget(self._catalog_path)

        # Critic round — Skill Catalog was read-only (browse only); a skill a
        # user deletes from the UI must actually disappear, mirroring the
        # custom-role delete button (_on_delete_custom_role_clicked). Only
        # shown for a skill under the active project's own writable roots —
        # a bundled cockpit-checkout skill (skill_scan.is_writable_skill ==
        # False) never gets one, same guard `deletable=` uses for built-in
        # roles.
        del_row = QHBoxLayout()
        self._catalog_delete_btn = QPushButton("x ลบ skill นี้", detail_panel)
        self._catalog_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._catalog_delete_btn.clicked.connect(self._on_delete_skill_clicked)
        self._catalog_delete_btn.hide()
        del_row.addWidget(self._catalog_delete_btn)
        del_row.addStretch(1)
        detail_lay.addLayout(del_row)
        detail_lay.addStretch(1)
        lay.addWidget(detail_panel, 1)
        outer.addLayout(lay)

        outer.addWidget(self._build_autoskills_panel(view))
        outer.addWidget(self._build_new_skill_form(view))

        self._reload_skill_catalog()
        return view

    def _build_autoskills_panel(self, parent: QWidget) -> QWidget:
        """ "ดึง skill ตาม stack" — bridges :mod:`autoskills_installer` (scans
        the project via the `autoskills` CLI, proposes matching skills from
        the skills.sh registry) into the Skill Catalog. Confirm-before-write
        end to end: `preview()`/`install()` both run on a worker thread
        (`_AutoskillsPreviewThread`/`_AutoskillsInstallThread` — the CLI call
        can block up to ~60s) and nothing is written until the user ticks a
        selection in `_AutoskillsConfirmDialog` and presses Install."""
        panel = QWidget(parent)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(16, 14, 16, 14)
        p_lay.setSpacing(8)
        p_lay.addWidget(QLabel("⚡ Auto-detect skills", panel))
        hint = QLabel(
            "สแกน stack ของโปรเจคด้วย autoskills CLI แล้วเสนอ skill ที่เข้ากับ stack — "
            "ไม่เขียนไฟล์จนกว่าจะเลือกและกดยืนยัน",
            panel,
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        p_lay.addWidget(hint)

        row = QHBoxLayout()
        self._as_scan_btn = cockpit_theme.gold_button("ดึง skill ตาม stack", panel)
        self._as_scan_btn.clicked.connect(self._on_autoskills_scan_clicked)
        row.addWidget(self._as_scan_btn)
        self._as_status = QLabel("", panel)
        self._as_status.setObjectName("panelHint")
        row.addWidget(self._as_status)
        row.addStretch(1)
        p_lay.addLayout(row)
        return panel

    def _on_autoskills_scan_clicked(self) -> None:
        roots = self._writable_skill_roots()
        if not roots:
            self._as_status.setText("! ไม่มี active project ให้สแกน")
            return
        self._as_scan_btn.setEnabled(False)
        self._as_status.setText("กำลังสแกน stack…")
        # Kept on self so the QThread object isn't garbage-collected mid-run
        # (a local variable going out of scope here would stop the thread).
        self._as_preview_thread = _AutoskillsPreviewThread(roots[0], self)
        self._as_preview_thread.resultReady.connect(self._on_autoskills_preview_ready)
        self._as_preview_thread.start()

    def _on_autoskills_preview_ready(self, result: autoskills_installer.PreviewResult) -> None:
        self._as_scan_btn.setEnabled(True)
        self._as_status.setText("")
        if not result.ok:
            QMessageBox.warning(self, "Auto-detect skills", result.error or "สแกนไม่สำเร็จ")
            return
        if not result.skills:
            if result.no_skills_for_stack:
                extra = f"\n\nstack ที่ตรวจพบ: {', '.join(result.stack)}" if result.stack else ""
                QMessageBox.information(
                    self,
                    "Auto-detect skills",
                    f"autoskills ไม่พบ skill ที่เข้ากับ stack ของโปรเจคนี้{extra}",
                )
            else:
                # autoskills returned real output but this module's parser
                # didn't recognize it (e.g. CLI format changed again) — never
                # claim "not found" here, show what the CLI actually said.
                raw = result.raw_output.strip() or "(ไม่มี output)"
                if len(raw) > 4000:
                    raw = raw[:4000] + "\n… (ตัดข้อความ)"
                QMessageBox.warning(
                    self,
                    "Auto-detect skills",
                    "แปลผลลัพธ์จาก autoskills ไม่สำเร็จ (รูปแบบ output อาจเปลี่ยนไป) "
                    f"— นี่คือ output ดิบจาก CLI:\n\n{raw}",
                )
            return
        dialog = _AutoskillsConfirmDialog(result, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_names()
        if not selected:
            return
        roots = self._writable_skill_roots()
        if not roots:
            self._as_status.setText("! ไม่มี active project ให้ติดตั้ง")
            return
        self._as_scan_btn.setEnabled(False)
        self._as_status.setText("กำลังติดตั้ง…")
        self._as_install_thread = _AutoskillsInstallThread(roots[0], selected, self)
        self._as_install_thread.resultReady.connect(self._on_autoskills_install_ready)
        self._as_install_thread.start()

    def _on_autoskills_install_ready(self, result: autoskills_installer.InstallResult) -> None:
        self._as_scan_btn.setEnabled(True)
        self._as_status.setText("")
        lines: list[str] = []
        if result.written:
            lines.append(f"ติดตั้งแล้ว: {', '.join(result.written)}")
        if result.skipped:
            lines.append(f"ข้าม (ไม่ได้เลือก): {', '.join(result.skipped)}")
        if result.overwritten:
            lines.append(f"⚠ เขียนทับ skill เดิม: {', '.join(result.overwritten)}")
        if result.overwrite_failed:
            lines.append(
                f"‼ เขียนทับและกู้คืนไม่สำเร็จ (data loss จริง): {', '.join(result.overwrite_failed)}"
            )
        if result.error:
            lines.append(f"error: {result.error}")
        body = "\n".join(lines) if lines else "ไม่มีอะไรเปลี่ยนแปลง"
        if not result.ok or result.overwrite_failed:
            QMessageBox.critical(self, "Auto-detect skills", body)
        else:
            QMessageBox.information(self, "Auto-detect skills", body)
        self._reload_skill_catalog()

    def _build_new_skill_form(self, parent: QWidget) -> QWidget:
        """+ New Skill — closes the create-half of the Skill Catalog's
        lifecycle loop (list/select already existed; create/delete did not,
        forcing a user to hand-author `.claude/skills/<name>/SKILL.md`
        themselves). Writes immediately on click, same "no dirty-tracking /
        no Save & Apply" pattern as Templates' Duplicate/Delete and the
        custom-role delete button — there's nothing to stage."""
        form = QWidget(parent)
        form.setObjectName("panel")
        f_lay = QVBoxLayout(form)
        f_lay.setContentsMargins(16, 14, 16, 14)
        f_lay.setSpacing(10)
        f_lay.addWidget(QLabel("+ New Skill", form))

        name_row = QHBoxLayout()
        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Name", form))
        self._ns_name = QLineEdit(form)
        self._ns_name.setPlaceholderText("my-new-skill (a-z0-9-_ เท่านั้น)")
        name_col.addWidget(self._ns_name)
        name_row.addLayout(name_col, 1)

        desc_col = QVBoxLayout()
        desc_col.addWidget(QLabel("Description (frontmatter, 1 บรรทัด)", form))
        self._ns_desc = QLineEdit(form)
        self._ns_desc.setPlaceholderText("ใช้เมื่อไหร่ — Skill tool ใช้เลือก skill จากบรรทัดนี้")
        desc_col.addWidget(self._ns_desc)
        name_row.addLayout(desc_col, 1)
        f_lay.addLayout(name_row)

        f_lay.addWidget(QLabel("Instructions", form))
        self._ns_instructions = QPlainTextEdit(form)
        self._ns_instructions.setPlaceholderText(
            "เนื้อหา skill (markdown) ที่ role จะอ่านตอนใช้ skill นี้..."
        )
        self._ns_instructions.setMinimumHeight(80)
        f_lay.addWidget(self._ns_instructions)

        self._ns_status = QLabel("", form)
        self._ns_status.setObjectName("panelHint")
        self._ns_status.setWordWrap(True)
        f_lay.addWidget(self._ns_status)

        create_row = QHBoxLayout()
        create_btn = cockpit_theme.gold_button("+ Create Skill", form)
        create_btn.clicked.connect(self._on_create_skill_clicked)
        create_row.addWidget(create_btn)
        # #419: cockpit-level scope — real file lands in GLOBAL_SKILLS_HOME
        # and every project (every provider) gets the junction on open/spawn.
        self._ns_global = QCheckBox("Global skill (ทุกโปรเจค ทุก provider)", form)
        self._ns_global.setToolTip(
            f"เขียนไว้ที่ {config.GLOBAL_SKILLS_HOME} — ไม่ผูกกับ project ไหน "
            "โปรเจคอื่นเห็นอัตโนมัติตอนเปิด tab / spawn ครั้งถัดไป"
        )
        create_row.addWidget(self._ns_global)
        create_row.addStretch(1)
        f_lay.addLayout(create_row)

        return form

    def _writable_skill_roots(self) -> list[Path]:
        """Project paths a New-Skill create/delete may actually write to —
        NOT `config.REPO_ROOT`/`ASSETS_ROOT` (the cockpit's own bundled
        skills, always read-only from this UI)."""
        return _allowed_project_roots(self._project) if self._project else []

    def _central_skill_dirs(self) -> list[Path]:
        """Central `project_skills_dir` for the active project — where a
        New-Skill create writes the real file (the project path only holds a
        junction). Passed to `is_writable_skill` so a junctioned skill, whose
        `SkillInfo.path` resolves into the central store, still shows a delete
        button. Empty when no project is active."""
        dirs: list[Path] = [config.GLOBAL_SKILLS_HOME]  # #419 global store
        if not self._project:
            return dirs
        try:
            dirs.append(config.project_skills_dir(self._project))
        except ValueError:
            pass
        return dirs

    def _reload_skill_catalog(self) -> None:
        """(Re)populate the skill list from disk — called at view build time
        and again after a create/delete so the list, New Role picker (#3)
        and Skill Matrix all reflect the change immediately without a
        cockpit restart."""
        self._catalog_skills = skill_scan.scan_skills(self._new_role_skill_roots())
        self._catalog_list.blockSignals(True)
        self._catalog_list.clear()
        for skill in self._catalog_skills:
            item = QListWidgetItem(skill.name, self._catalog_list)
            item.setData(Qt.ItemDataRole.UserRole, skill.name)
            item.setToolTip(skill.description or skill.name)
        self._catalog_list.blockSignals(False)

        if self._catalog_list.count():
            self._catalog_list.setCurrentRow(0)
            self._on_catalog_skill_selected(self._catalog_list.currentItem())
        else:
            self._catalog_name.setText("— ไม่พบ skill —")
            self._catalog_desc.setText(
                "ยังไม่มี .claude/skills/*/SKILL.md ในโปรเจคนี้หรือ cockpit checkout"
            )
            self._catalog_roles.setText("")
            self._catalog_path.setText("")
            self._catalog_delete_btn.hide()

        # New Role's checklist + Skill Matrix scan the same `.claude/skills/`
        # roots — refresh both so a just-created/deleted skill shows up
        # there too without reopening the Settings window.
        if hasattr(self, "_nr_skills_lay"):
            self._reload_new_role_skills()
        if hasattr(self, "_skill_matrix_grid"):
            self._reload_skill_matrix()

    def _roles_referencing_skill(self, skill_name: str) -> list[str]:
        """Role instruction docs that reference `skill_name` as a whole word
        (case-insensitive, word-boundary regex — NOT a raw substring). This
        matches both the generated ``อ่าน skill: <name>`` marker that
        `_append_skill_references` embeds and hand-authored prose that names the
        skill directly, while a short/common name (``git``, ``test``) no longer
        false-positives on unrelated words like "github" or "latest"."""
        pattern = re.compile(rf"\b{re.escape(skill_name)}\b", re.IGNORECASE)
        hits = [role for role, doc in self._catalog_role_docs.items() if pattern.search(doc)]
        return sorted(hits)

    def _on_catalog_skill_selected(self, current: QListWidgetItem | None, *_args: object) -> None:
        if current is None:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        skill = next((s for s in self._catalog_skills if s.name == name), None)
        if skill is None:
            return
        cost = token_estimate.estimate_file_tokens(skill.path)
        self._catalog_name.setText(f"{skill.name}  ·  {token_estimate.format_tokens(cost)}")
        self._catalog_desc.setText(skill.description or "(ไม่มี description ใน frontmatter)")
        refs = self._roles_referencing_skill(skill.name)
        if refs:
            labels = ", ".join(
                (roles_mod.by_name(r).label if roles_mod.by_name(r) else r) for r in refs
            )
            self._catalog_roles.setText(f"อ้างถึงโดย role: {labels}")
        else:
            self._catalog_roles.setText("ยังไม่มี role ไหนอ้างถึง skill นี้")
        self._catalog_path.setText(f"{skill.path}")
        self._catalog_delete_btn.setVisible(
            skill_scan.is_writable_skill(
                skill.path,
                self._writable_skill_roots(),
                extra_dirs=self._central_skill_dirs(),
            )
        )

    def _on_create_skill_clicked(self) -> None:
        name = self._ns_name.text().strip().lower()
        description = self._ns_desc.text().strip()
        instructions = self._ns_instructions.toPlainText()

        root = self._writable_skill_roots()
        if not root:
            self._ns_status.setText("! ไม่มี active project ให้เขียน skill ลงไป")
            return

        existing = {s.name for s in self._catalog_skills}
        is_global = bool(getattr(self, "_ns_global", None) and self._ns_global.isChecked())
        ok, err = skill_scan.create_skill(
            root[0],
            name,
            description,
            instructions,
            project_ns=self._project,
            existing=existing,
            scope="global" if is_global else "project",
        )
        if not ok:
            self._ns_status.setText(f"! {err}")
            return

        self._ns_status.setText(f"OK: สร้าง{' global ' if is_global else ' '}skill '{name}' แล้ว")
        self._ns_name.clear()
        self._ns_desc.clear()
        self._ns_instructions.clear()
        self._reload_skill_catalog()

    def _on_delete_skill_clicked(self) -> None:
        current = self._catalog_list.currentItem()
        if current is None:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        skill = next((s for s in self._catalog_skills if s.name == name), None)
        if skill is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete skill",
            f"ลบ skill '{skill.name}'?\n\nจะลบทั้งโฟลเดอร์ ({skill.path.parent}) — undo ไม่ได้",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not skill_scan.delete_skill(skill.path):
            QMessageBox.critical(self, "Delete failed", f"ลบ skill '{skill.name}' ไม่สำเร็จ")
            return
        self._reload_skill_catalog()

    # ──────────────────────────────────────────────────────────
    # view: Skill Matrix (real — SKILL section, #103 phase 4)
    #
    # role × skill toggle grid, same shape as MCP Matrix / Plugins Matrix
    # (reuses `_populate_matrix_grid` + `pane_tools_dialog.build_matrix`/
    # `matrix_to_role_items`/`diff_role_items` verbatim — this is a THIRD
    # kind of policy those helpers already generalize over). Unlike the
    # MCP/Plugins matrices, codex and gemini get rows here — a checked cell
    # makes `spawn_engine.spawn()` bridge that skill into their AGENTS.md as
    # instruction-style text (`skill_policy.render_skill_appendix`, see that
    # module's docstring for the claude-vs-codex/gemini distinction). Persists
    # to `skill_policy` (~/.takkub/skill-policy.json), a policy store separate
    # from `pane_tools_policy` — different concern (context injection, not
    # MCP/plugin access) even though the on-disk shape rhymes.
    # ──────────────────────────────────────────────────────────

    def _build_skill_matrix_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        banner = QLabel(
            "ติ๊ก = spawn ครั้งหน้า role นั้นจะเห็น reference ของ skill นี้ใน context ทันที "
            "(claude = นัดให้อ่าน skill ที่มีอยู่แล้ว · codex/gemini = ฝังเนื้อหาเป็น "
            "instruction-style ใน AGENTS.md เพราะไม่มี Skill tool)",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        self._skill_matrix_empty = QLabel(
            "ไม่พบ .claude/skills/*/SKILL.md ในโปรเจคนี้หรือ cockpit checkout", view
        )
        self._skill_matrix_empty.setObjectName("panelHint")
        self._skill_matrix_empty.setWordWrap(True)
        self._skill_matrix_empty.hide()
        lay.addWidget(self._skill_matrix_empty)

        matrix_panel = QWidget(view)
        matrix_panel.setObjectName("panel")
        self._skill_matrix_grid = QGridLayout(matrix_panel)
        self._skill_matrix_grid.setContentsMargins(14, 12, 14, 12)
        self._skill_matrix_grid.setHorizontalSpacing(6)
        self._skill_matrix_grid.setVerticalSpacing(4)
        lay.addWidget(matrix_panel)
        lay.addStretch(1)

        self._reload_skill_matrix()
        return view

    def _reload_skill_matrix(self) -> None:
        skills = [
            s
            for s in skill_scan.scan_skills(self._new_role_skill_roots())
            if skill_policy._validate_name(s.name)
        ]
        items = [s.name for s in skills]
        # #516: per-skill boot-token cost, read straight from each skill's own
        # SKILL.md — cheap and exact (no marketplace-cache walk needed here).
        costs = {s.name: token_estimate.estimate_file_tokens(s.path) for s in skills}
        roles = skill_policy.skill_matrix_roles()
        self._orig_skill_items = {role: skill_policy.effective_skills(role) for role in roles}
        matrix = pane_tools_dialog.build_matrix(roles, items, self._orig_skill_items)
        self._skill_toggles = self._populate_matrix_grid(
            self._skill_matrix_grid, roles, items, matrix, item_costs=costs
        )
        self._skill_matrix_empty.setVisible(not items)

    # ──────────────────────────────────────────────────────────
    # view: Pipeline (#515 — merged the old Pipeline Builder + Templates
    # pages into one: the template list on the left drives the hop builder
    # on the right directly, so selecting a row IS "start editing this
    # template's hops" — no separate "Editing template:" combo or
    # "Edit hops ->" detour needed anymore.)
    # ──────────────────────────────────────────────────────────

    def _build_pipeline_view(self) -> QWidget:
        view = QWidget(self)
        lay = QHBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        # ── left: saved templates ────────────────────────────
        list_panel = QWidget(view)
        list_panel.setObjectName("panel")
        list_panel.setFixedWidth(220)
        list_lay = QVBoxLayout(list_panel)
        list_lay.setContentsMargins(6, 6, 6, 6)
        list_lay.setSpacing(6)
        self._tpl_list = QListWidget(list_panel)
        self._tpl_list.setFrameShape(QFrame.Shape.NoFrame)
        self._tpl_list.currentItemChanged.connect(self._on_template_selected)
        list_lay.addWidget(self._tpl_list)

        tpl_btn_row = QHBoxLayout()
        self._tpl_duplicate_btn = cockpit_theme.secondary_button("Duplicate", list_panel)
        self._tpl_duplicate_btn.clicked.connect(self._on_template_duplicate_clicked)
        tpl_btn_row.addWidget(self._tpl_duplicate_btn)
        self._tpl_delete_btn = cockpit_theme.secondary_button("Delete", list_panel)
        self._tpl_delete_btn.clicked.connect(self._on_template_delete_clicked)
        tpl_btn_row.addWidget(self._tpl_delete_btn)
        list_lay.addLayout(tpl_btn_row)
        lay.addWidget(list_panel)
        lay.setAlignment(list_panel, Qt.AlignmentFlag.AlignTop)

        # ── right: hop builder for whichever template is selected ────
        builder_panel = QWidget(view)
        b_lay = QVBoxLayout(builder_panel)
        b_lay.setContentsMargins(0, 0, 0, 0)
        b_lay.setSpacing(12)

        palette_panel = QWidget(builder_panel)
        palette_panel.setObjectName("panel")
        pal_lay = QHBoxLayout(palette_panel)
        pal_lay.setContentsMargins(12, 10, 12, 10)
        pal_lay.setSpacing(6)
        pal_hint = QLabel("+ hop เดี่ยวจาก role:", palette_panel)
        pal_hint.setObjectName("panelHint")
        pal_lay.addWidget(pal_hint)
        for role in _pipeline_palette_roles():
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            btn = QPushButton(label, palette_panel)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {color};"
                f" border-radius: 999px; color: {color}; padding: 3px 10px; font-weight: 600;"
                f" font-size: 11px; }}"
                f"QPushButton:hover {{ background: rgba(255,255,255,0.06); }}"
            )
            btn.clicked.connect(
                lambda _checked=False, role_=role: self._on_palette_role_clicked(role_)
            )
            pal_lay.addWidget(btn)
        pal_lay.addStretch(1)
        b_lay.addWidget(palette_panel)

        self._pb_hops_container = QWidget(builder_panel)
        self._pb_hops_lay = QVBoxLayout(self._pb_hops_container)
        self._pb_hops_lay.setContentsMargins(0, 0, 0, 0)
        self._pb_hops_lay.setSpacing(4)
        b_lay.addWidget(self._pb_hops_container)

        add_hop_btn = QPushButton("+ Add hop", builder_panel)
        add_hop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_hop_btn.setStyleSheet(
            f"QPushButton {{ border: 1px dashed {cockpit_theme.BORDER_STRONG}; border-radius:"
            f" {cockpit_theme.RADIUS_SM}px; color: {cockpit_theme.TEXT_MUTED}; padding: 8px; }}"
            f"QPushButton:hover {{ color: {cockpit_theme.TEXT_PRIMARY}; border-color:"
            f" {cockpit_theme.ACCENT_GOLD}; }}"
        )
        add_hop_btn.clicked.connect(self._on_add_hop_clicked)
        b_lay.addWidget(add_hop_btn)
        b_lay.addStretch(1)
        lay.addWidget(builder_panel, 1)

        # Populates the list AND (via `_on_template_selected`, wired above)
        # loads the first/active template's hops into the builder — no
        # separate "load active template" call needed.
        self._reload_templates_list()
        return view

    def _load_pb_hops(self, template_id: str) -> None:
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == template_id), None)
        self._pb_template_id = template_id
        self._pb_hops: list[list[dict]] = (
            [[dict(entry) for entry in hop] for hop in tpl["hops"]] if tpl else []
        )
        self._render_pb_hops()

    def _render_pb_hops(self) -> None:
        while self._pb_hops_lay.count():
            item = self._pb_hops_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for idx, hop in enumerate(self._pb_hops):
            panel = QWidget(self._pb_hops_container)
            panel.setObjectName("panel")
            p_lay = QVBoxLayout(panel)
            p_lay.setContentsMargins(12, 10, 12, 10)
            p_lay.setSpacing(6)

            head_row = QHBoxLayout()
            head_lbl = QLabel(f"HOP {idx + 1}", panel)
            head_lbl.setObjectName("panelTitle")
            head_row.addWidget(head_lbl)
            if len(hop) > 1:
                chip = QLabel("parallel", panel)
                chip.setStyleSheet(
                    f"background: {cockpit_theme.PARALLEL_CHIP_BG}; border: 1px solid"
                    f" {cockpit_theme.PARALLEL_CHIP_BORDER}; border-radius: 999px; color:"
                    f" {cockpit_theme.PARALLEL_CHIP_TEXT}; padding: 2px 8px; font-size: 11px;"
                    f" font-weight: 600;"
                )
                head_row.addWidget(chip)
            head_row.addStretch(1)
            remove_hop_btn = QPushButton("x", panel)
            remove_hop_btn.setFixedSize(22, 22)
            remove_hop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_hop_btn.setToolTip("ลบ hop นี้")
            remove_hop_btn.clicked.connect(
                lambda _checked=False, i=idx: self._on_remove_hop_clicked(i)
            )
            head_row.addWidget(remove_hop_btn)
            p_lay.addLayout(head_row)

            roles_row = QHBoxLayout()
            roles_row.setSpacing(6)
            for entry in hop:
                role = entry["role"]
                r = roles_mod.by_name(role)
                label = r.label if r else role.capitalize()
                color = cockpit_theme.ROLE_COLORS.get(
                    role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
                )
                pill = QWidget(panel)
                pill.setStyleSheet("background: rgba(255,255,255,0.05); border-radius: 999px;")
                pill_lay = QHBoxLayout(pill)
                pill_lay.setContentsMargins(8, 3, 4, 3)
                pill_lay.setSpacing(4)
                pill_lay.addWidget(cockpit_theme.role_chip(label, color, pill))
                rm_btn = QPushButton("x", pill)
                rm_btn.setFixedSize(16, 16)
                rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                rm_btn.setStyleSheet(
                    "QPushButton { border: none; background: transparent; font-size: 10px; }"
                )
                rm_btn.clicked.connect(
                    lambda _checked=False, i=idx, ro=role: self._on_remove_hop_role_clicked(i, ro)
                )
                pill_lay.addWidget(rm_btn)
                roles_row.addWidget(pill)

            add_combo = QComboBox(panel)
            add_combo.addItem("+ add role", None)
            used = {e["role"] for e in hop}
            for role in _pipeline_palette_roles():
                if role in used:
                    continue
                r = roles_mod.by_name(role)
                add_combo.addItem(r.label if r else role.capitalize(), role)
            add_combo.currentIndexChanged.connect(
                lambda _index=0, i=idx, combo=add_combo: self._on_hop_add_role_selected(i, combo)
            )
            roles_row.addWidget(add_combo)
            roles_row.addStretch(1)
            p_lay.addLayout(roles_row)

            self._pb_hops_lay.addWidget(panel)

            if idx < len(self._pb_hops) - 1:
                # 2026-09-08 design review: was a literal ASCII "v" doing
                # double duty as a flowchart arrow. U+2193 (↓), not the
                # more obvious ▼/▾ triangle glyphs — pixel/glyph-coverage
                # check against the bundled IBM Plex fonts showed neither
                # triangle is actually present (renders as a tofu box),
                # while U+2193 is.
                conn = QLabel("↓ wait for all", self._pb_hops_container)
                conn.setObjectName("panelHint")
                conn.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._pb_hops_lay.addWidget(conn)

    def _on_add_hop_clicked(self) -> None:
        self._pb_hops.append([])
        self._render_pb_hops()
        self._mark_dirty()

    def _on_remove_hop_clicked(self, idx: int) -> None:
        if 0 <= idx < len(self._pb_hops):
            del self._pb_hops[idx]
            self._render_pb_hops()
            self._mark_dirty()

    def _on_remove_hop_role_clicked(self, idx: int, role: str) -> None:
        if 0 <= idx < len(self._pb_hops):
            self._pb_hops[idx] = [e for e in self._pb_hops[idx] if e["role"] != role]
            self._render_pb_hops()
            self._mark_dirty()

    def _on_hop_add_role_selected(self, idx: int, combo: QComboBox) -> None:
        role = combo.currentData()
        if role is None or not (0 <= idx < len(self._pb_hops)):
            return
        self._pb_hops[idx].append(
            {"role": role, "cwd": "", "requiresCommit": False, "autoChain": False}
        )
        self._render_pb_hops()
        self._mark_dirty()

    def _on_palette_role_clicked(self, role: str) -> None:
        self._pb_hops.append(
            [{"role": role, "cwd": "", "requiresCommit": False, "autoChain": False}]
        )
        self._render_pb_hops()
        self._mark_dirty()

    # ──────────────────────────────────────────────────────────
    # Templates (real) — the list panel built above, in `_build_pipeline_view`
    # ──────────────────────────────────────────────────────────

    _TPL_ROW_HEIGHT = 34
    # list_panel.setFixedWidth(220) in `_build_pipeline_view` — the panel's
    # width never changes, so the label's available width can be computed
    # once here instead of chasing live resize events.
    _TPL_LIST_PANEL_WIDTH = 220
    _TPL_LIST_ROW_PADDING = 6 + 6 + 6 + 6 + 2  # list_lay margins + row_lay margins + border

    @staticmethod
    def _compact_chip_width(metrics: QFontMetrics, text: str) -> int:
        """Predicted pixel width of a `gold_soft_chip(..., compact=True)` —
        mirrors its QSS padding/border so callers can reserve layout space
        for it without needing a shown widget (critic #2026-07-10 v2:
        BUILT-IN chip was crowding out the template name)."""
        return (
            metrics.horizontalAdvance(text)
            + cockpit_theme.COMPACT_CHIP_HPAD
            + cockpit_theme.COMPACT_CHIP_BORDER
        )

    @staticmethod
    def _elide_template_name(metrics: QFontMetrics, name: str, avail_width: int) -> str:
        """Ellipsize `name` to fit `avail_width` px — critic #2026-07-10 v2:
        the raw QLabel used to hard-clip mid-glyph ("Feature (UI+API)" -> "Feature (UI+AP")
        instead of showing "…"."""
        return metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(avail_width, 0))

    def _reload_templates_list(self) -> None:
        self._tpl_list.blockSignals(True)
        self._tpl_list.clear()
        metrics = QFontMetrics(self._tpl_list.font())
        row_spacing = 6
        for t in self._pipeline_payload.get("templates", []):
            item = QListWidgetItem(self._tpl_list)
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setSizeHint(QSize(0, self._TPL_ROW_HEIGHT))
            row = QWidget(self._tpl_list)
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(6, 0, 6, 0)
            row_lay.setSpacing(row_spacing)

            builtin = bool(t.get("builtin"))
            chip_width = (
                self._compact_chip_width(metrics, "BUILT-IN") + row_spacing if builtin else 0
            )
            avail = self._TPL_LIST_PANEL_WIDTH - self._TPL_LIST_ROW_PADDING - chip_width
            name_label = QLabel(self._elide_template_name(metrics, t["name"], avail), row)
            name_label.setToolTip(t["name"])
            row_lay.addWidget(name_label, 1)
            if builtin:
                row_lay.addWidget(cockpit_theme.gold_soft_chip("BUILT-IN", row, compact=True))
            self._tpl_list.setItemWidget(item, row)
        self._tpl_list.blockSignals(False)
        # Cap the list's height to fit its item count instead of stretching
        # to the row's full height (critic #2026-07-10: 600px panel with 3
        # items) — the +8 covers the panel's 6px top/bottom content margins.
        count = self._tpl_list.count()
        self._tpl_list.setMaximumHeight(max(count, 1) * self._TPL_ROW_HEIGHT + 8)
        if count:
            self._tpl_list.setCurrentRow(0)
        else:
            self._on_template_selected(None, None)

    def _on_template_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        """Selecting a template in the list (#515) loads its hops straight
        into the builder on the right — no separate "Edit hops ->" detour."""
        if current is None:
            self._tpl_selected_id = None
            self._tpl_delete_btn.setEnabled(False)
            self._pb_template_id = None
            self._pb_hops = []
            self._render_pb_hops()
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None:
            return
        self._tpl_selected_id = tid
        self._tpl_delete_btn.setEnabled(not tpl.get("builtin"))
        self._load_pb_hops(tid)

    def _on_template_duplicate_clicked(self) -> None:
        tid = getattr(self, "_tpl_selected_id", None)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None:
            return
        base = f"{tpl['id']}-copy"
        existing_ids = {t["id"] for t in self._pipeline_payload["templates"]}
        new_id = base
        n = 2
        while new_id in existing_ids:
            new_id = f"{base}{n}"
            n += 1
        new_tpl = {
            "id": new_id,
            "name": f"{tpl['name']} (copy)",
            "builtin": False,
            "hops": [[dict(e) for e in hop] for hop in tpl["hops"]],
        }
        self._pipeline_payload["templates"].append(new_tpl)
        if not self._persist_pipeline_payload():
            return
        self._reload_templates_list()

    def _on_template_delete_clicked(self) -> None:
        tid = getattr(self, "_tpl_selected_id", None)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None or tpl.get("builtin"):
            return
        confirm = QMessageBox.question(self, "Delete template", f"ลบ template '{tpl['name']}'?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._pipeline_payload["templates"] = [
            t for t in self._pipeline_payload["templates"] if t["id"] != tid
        ]
        if self._pipeline_payload.get("activeTemplate") == tid:
            self._pipeline_payload["activeTemplate"] = self._pipeline_payload["templates"][0]["id"]
        if not self._persist_pipeline_payload():
            return
        self._reload_templates_list()

    def _persist_pipeline_payload(self) -> bool:
        """Write ``self._pipeline_payload`` (Duplicate/Delete's immediate-commit
        path — same "writes right away" pattern as Add/Remove MCP) then
        re-read it back so built-in-hop-normalization stays in sync."""
        try:
            pipeline_config.save(self._pipeline_payload, self._project)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return False
        self._pipeline_payload = pipeline_config.load(self._project)
        return True
