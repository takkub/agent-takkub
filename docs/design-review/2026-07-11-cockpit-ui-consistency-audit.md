---
date: 2026-07-11
project: agent-takkub
reviewer: critic
shots: []
---

# UI consistency audit · cockpit design system · 2026-07-11

## 📸 Scope

Not a screenshot review — a **code-level color/font audit** of every PyQt6 UI
module in `src/agent_takkub/`, tracing the user's complaint that the cockpit
"ไม่เป็นไปในทางเดียวกันเลย." Root cause confirmed: `cockpit_theme.py` (built
2026-07-10, gold `#E3B341` + IBM Plex, pixel-verified 4 rounds) is a complete
design system **scoped to `settings_window.py` only**. Every other UI file
predates it and hardcodes its own palette. Every hex below was read directly from
source (grep `#[0-9a-fA-F]{6}` per file) — none guessed. Deliverables: this audit
+ migration spec, and the skill `.claude/skills/cockpit-ui-style/SKILL.md`.

## ✅ ของดีที่ควรเก็บไว้

- **`cockpit_theme.py` is genuinely complete** — grounds, borders, text ramp,
  gold accent family, role palette, fonts-with-fallback, reusable widget helpers
  (`gold_button`, `secondary_button`, `role_chip`, `gold_soft_chip`,
  `ToggleSwitch`), and a full objectName-driven `build_stylesheet()`. It is the
  right single source of truth. *impact: high*
- **Font resolution is already cross-platform** — `ensure_fonts_loaded()` bundles
  IBM Plex with a per-OS fallback chain and never blocks boot. Reuse it verbatim;
  don't re-solve fonts anywhere. *impact: med*
- **Semantic color intent is sound** — provider brand (codex teal / gemini blue),
  status states (red/amber/green), and the Anthropic clay meters are meaningful
  distinctions, not noise. Migration must **preserve meaning, tokenize value** —
  not flatten everything to gold. *impact: high*

## 🎯 Decision: `cockpit_theme.py` is the single source of truth

Newest, most complete, pixel-verified 4 rounds, and already the reference the
user signed off on for Settings. All other UI files migrate **to** it, not the
reverse. Migration is **mechanical + semantic-preserving**: swap literal greys →
`GROUND_*`/`TEXT_*`, solid zinc borders → `BORDER_*`, the *primary* accent
(active/selected/focus/CTA) → gold, fonts → `ensure_fonts_loaded()`. Keep
role/provider/state/meter colors as **tokens**, not as gold.

## ➖ ลบ (competing systems to delete/collapse)

- **Indigo/violet accent as "active/primary"** — `#6366f1`,`#818cf8`,`#8b5cf6`,
  `#a855f7` in `project_nav.py` (active nav/selection) and `project_tab.py`
  (`#6366f1` active). This is a whole second accent system competing with gold.
  Replace with `ACCENT_GOLD` / `GOLD_CHIP_*` for all active/selected/focus states.
  *impact: high*
- **Blue/green primary buttons** — `update_panel.py` CTA `#2563eb`/`#1d4ed8`,
  `user_actions.py` `#16a34a` + `#2563eb`. Delete the inline QSS; use
  `theme.gold_button()` (primary) / `theme.secondary_button()`. *impact: high*
- **Second role palette** — `roles.py Role.color` (`lead=#f5c542`,
  `frontend=#22d3ee` cyan …) disagrees with `cockpit_theme.ROLE_COLORS`
  (`lead=#E3B341`, `frontend=#34B7AC` teal …) on **every** role. Two palettes for
  the same concept = guaranteed mismatch between grid and Settings. Collapse to
  one. *impact: high*

## 🔧 ปรับ — per-file migration spec (actionable for frontend/backend)

Each row: the file, the literals found (verified), and the exact token to swap to.
"Grounds→`GROUND_*`, greys→`TEXT_*`, borders→`BORDER_*`" is the standard swap;
specifics called out where they differ.

- **`status_header.py`** — greys `#3f3f46`/`#71717a`/`#27272a`/`#52525b`/`#a1a1aa`
  /`#d4d4d8`/`#e4e4e7` → `TEXT_*`; separator `_make_status_separator()` uses solid
  `#3f3f46` (line 44) → `BORDER_STRONG`; amber `#d97706`/`#f59e0b` provider-warn →
  keep as a **state-amber token** (add `STATE_WARN = "#d97706"`); provider chips
  codex `#10a37f`/gemini `#4285f4` (line 77-78) → keep, move to
  `PROVIDER_CODEX`/`PROVIDER_GEMINI` tokens; radii `10px`→`RADIUS_MD`. *impact: high*
- **`project_nav.py`** — grounds `#0e0e10`/`#18181b`/`#0b0b0d`/`#1c1c1f` →
  `GROUND_*`; **indigo active accent** `#6366f1`/`#818cf8`/`#8b5cf6`/`#a5b4fc`
  (lines 68-83,112-113) → `ACCENT_GOLD`/`GOLD_CHIP_*`; text `#52525b`/`#a1a1aa`/
  `#d4d4d8`/`#ffffff` → `TEXT_*`. **Keep** `_AVATAR_COLORS` (lines 111-121) — it's
  a deterministic per-project avatar tint (hash→palette), a distinct purpose;
  optionally relocate to a named `AVATAR_TINTS` token so it reads as intentional.
  *impact: high*
- **`task_dock.py`** — status dots `#facc15`/`#22c55e`/`#ef4444` (lines 53-55) →
  state tokens (warn/ok/error); violet `#a78bfa` (line 57) → `ROLE_COLORS`/gold as
  appropriate; grounds `#0e0e10`/`#18181b`/`#1e1b2e` → `GROUND_*` (the `#1e1b2e`
  indigo-tint ground has no equivalent — use `GROUND_PANEL`); greys → `TEXT_*`.
  *impact: high*
- **`agent_pane.py`** — state dots green/gold/blue/orange/red `#22c55e`/`#facc15`/
  `#0ea5e9`/`#f97316`/`#ef4444` (lines 46-50) → state tokens; grounds `#18181b`/
  `#1c1c20`/`#27272a` → `GROUND_*`; text `#e5e7eb`/`#9ca3af`/`#6b7280`/`#525252` →
  `TEXT_*`. *impact: med*
- **`update_panel.py`** — CTA `#2563eb`/`#1d4ed8` → `gold_button`; banner sets
  amber `#fde047`/`#422006`/`#a16207`, green `#4ade80`/`#166534`, red `#fca5a5`/
  `#7f1d1d`, blue `#1e3a8a`/`#93c5fd` → **state banner tokens** (add
  `BANNER_WARN/OK/ERROR/INFO` bg+border+text triples). *impact: med*
- **`user_actions.py`** — buttons `#16a34a`(green)/`#2563eb`(blue) →
  `gold_button`/`secondary_button`; greys `#6b7280`/`#3f3f46`/`#27272a`/`#71717a`/
  `#a1a1aa`/`#d4d4d8` → `TEXT_*`/`BORDER_*`; grounds `#18181b` → `GROUND_PANEL`.
  *impact: med*
- **`main_window.py`** — app-chrome grounds `#09090b`/`#18181b`/`#27272a`/
  `#3f3f46` (lines 183-186) → `GROUND_BODY`/`GROUND_PANEL`/`BORDER_*`; `#a1a1aa`/
  `#94a3b8` → `TEXT_MUTED`/`TEXT_SECONDARY`. This sets base chrome, so fixing it
  cascades to inheritors like `project_wizard.py`. *impact: high*
- **`project_tab.py`** — indigo `#6366f1` active (line 55) → `ACCENT_GOLD`;
  grounds `#0e0e10`/`#18181b` → `GROUND_*`; `#71717a`/`#d4d4d8`/`#ffffff` →
  `TEXT_*`; error `#ef4444` → state token. *impact: med*
- **`token_meter.py` / `usage_meter.py` / `limit_panel.py`** — Anthropic clay
  `#d97757` + amber `#fbbf24`/`#a16207` + red `#f87171`/`#ef4444` + green
  `#22c55e` are **semantic meter fills — keep the meaning**. Add tokens
  `METER_CLAY = "#d97757"` + the state ramp to `cockpit_theme`, then import.
  Greys → `TEXT_*`. *impact: low*
- **`logs_panel.py`** — clay `#d97757` + status `#22c55e`/`#facc15`/`#22d3ee`/
  `#0ea5e9`/`#f97316` → state/provider tokens; grounds/greys → `GROUND_*`/`TEXT_*`.
  *impact: low*
- **`tutorial_overlay.py`** — greys `#18181b`/`#3f3f46`/`#27272a`/`#a1a1aa`/
  `#71717a`/`#d4d4d8` → `GROUND_*`/`TEXT_*`/`BORDER_*`; clay `#e08968` →
  `METER_CLAY` neighbor / accent per context. *impact: low*
- **`roles.py`** — reconcile `Role.color` with `cockpit_theme.ROLE_COLORS`.
  Recommended: keep `roles.py` structural (name/label/column/row) and source the
  color from `ROLE_COLORS`, with `Role.color` as fallback for custom roles only —
  so grid and Settings render identical role hues. **Flag for user sign-off:** the
  main-grid role colors are long-familiar; changing them is user-visible. *impact: high*
- **`settings_window.py`** — already on-system, but has 5 stray `#94a3b8`
  (lines 664,709,1503,1749,1856) not in the token set → map to `TEXT_SECONDARY`
  or add `TEXT_SLATE` if the slate is intentional. *impact: low*
- **`issues.py`** — `#d73a4a`/`#fbca04`/`#c5def5`/`#e4e669` are **GitHub label
  colors** (external data), not cockpit chrome → **leave as-is**. *impact: low*

## 🚩 Heuristic violations (Nielsen)

- **#4 Consistency & standards** — the core violation: same concepts (active
  state, primary button, role identity) rendered differently across surfaces
  (gold vs indigo vs blue; `#E3B341` vs `#f5c542`). Fix = one token set. *impact: high*
- **#8 Aesthetic & minimalist design** — four parallel accent systems (gold /
  indigo / amber / clay) create visual noise with no semantic payoff for three of
  them. Collapse to gold-primary + kept semantics. *impact: med*

## 🎯 Recommended next steps (สำหรับ Lead)

1. **[high]** Extend `cockpit_theme.py` with the missing **semantic tokens** first
   (state ramp `STATE_WARN/OK/ERROR/INFO`, `BANNER_*` triples, `PROVIDER_CODEX/
   GEMINI`, `METER_CLAY`, optional `AVATAR_TINTS`). This unblocks every file
   migration below without new literals. → **backend** (leaf module, no UI risk).
2. **[high]** Migrate the high-impact chrome files (`main_window.py`,
   `status_header.py`, `project_nav.py`, `task_dock.py`, `update_panel.py`,
   `user_actions.py`) to import tokens per the spec above. → **frontend**,
   one file per commit, pixel-diff each against before.
3. **[high]** Reconcile the role palette (`roles.py` ↔ `ROLE_COLORS`) — **get
   user sign-off** on the final role hues before changing the familiar grid.
4. **[med]** Migrate the meters/overlay (`token_meter`, `usage_meter`,
   `limit_panel`, `logs_panel`, `tutorial_overlay`) once semantic tokens land.
5. **[low]** Clean `settings_window.py`'s 5 stray `#94a3b8`; give
   `project_wizard.py`'s parent the theme stylesheet so it inherits for free.
6. **[low]** After migration, add an import-linter/grep guard: no raw
   `#[0-9a-fA-F]{6}` in UI `.py` files except `cockpit_theme.py` + `issues.py`
   (external data) — keeps drift from re-entering.
