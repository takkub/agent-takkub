---
name: cockpit-ui-style
description: The single design system for the Takkub Cockpit PyQt6 UI — gold accent + IBM Plex, TWO theme variants (dark + light, #506) with every token in cockpit_theme.py. Read BEFORE touching ANY .py file that sets a color, font, border, radius, or QSS/stylesheet in src/agent_takkub/ (status_header, project_nav, task_dock, agent_pane, update_panel, main_window, user_actions, meters, dialogs, etc.). Trigger whenever styling cockpit UI, adding/restyling a widget, chip, button, list, dialog, or toggle, or when the user says the UI "doesn't match / looks inconsistent / ไม่เป็นไปในทางเดียวกัน".
---

# Cockpit UI Style

**One design system for the whole cockpit — in TWO variants.** The canonical
tokens live in `src/agent_takkub/cockpit_theme.py` (gold accent + IBM Plex).
Since #506 the cockpit has a **dark** variant (the historical gold-on-tinted-dark
look) and a **light** variant (paper-light grounds, white cards, darkened gold),
switched at runtime from Settings → General (ตามระบบ/สว่าง/มืด, persisted in
`theme_settings.py`). `settings_window.py` is the reference implementation —
every widget there is built the right way.

**Rule: never invent a hex, font, radius, or spacing value. Import the constant
from `cockpit_theme`.** If the value you need isn't there, add it to
`cockpit_theme.py` — as a dark constant **plus a `LIGHT_TOKENS` entry plus its
name in `_THEMED_TOKEN_NAMES`** — and import it. Never inline a literal:
`tests/test_no_hardcoded_ui_colors.py` fails the build on any new `#rrggbb`
outside `cockpit_theme.py` (allowlisted files carry a written reason).

```python
from . import cockpit_theme as theme   # ALWAYS the module object
```

## ⚠️ Theme-variant rules (dark-only code is a bug now)

1. **Import the module, read attributes late.** `from .cockpit_theme import
   ACCENT_GOLD` freezes the dark value forever — `apply_variant()` rebinds
   *module* globals. Always `cockpit_theme.ACCENT_GOLD` at style-build time.
2. **No module-level f-string QSS / token captures.** A module-level
   `_QSS = f"...{cockpit_theme.X}..."` (or a status→color dict) snapshots
   whichever variant was bound at import. Make it a function called at widget
   construction (see `project_nav._sidebar_qss()`, `task_dock._dock_qss()`).
   `app.py` binds the persisted variant *before* importing `main_window`, so
   remaining import-time captures at least get the right variant per process —
   but they will not follow a live switch; prefer functions.
3. **Live switch:** `theme_settings.save(mode)` → `cockpit_theme.apply_variant(
   theme_settings.resolve_variant(mode))` → `cockpit_theme.retheme_open_windows()`.
   A long-lived window opts in by exposing `retheme()` that re-applies its
   stylesheet (SettingsWindow, MainWindow, SettingsManagementWindow do).
4. **Never theme identity colors:** `ROLE_COLORS`, `PROVIDER_*`, `AVATAR_TINTS`
   are the same in both variants on purpose (a role keeps one hue).
5. **Terminal panes stay dark in both variants** (xterm ANSI palette in
   `static/terminal.html`) — deliberate; do not "fix" it.
6. **Baked-color SVGs need a `-light` twin** (see `static/icons/*-light.svg` and
   `nav/*-light.svg`); pick via `cockpit_theme.current_variant()` — precedent:
   `build_stylesheet`'s spin/combo arrows, `settings_window._nav_icon`.

---

## When to read this skill

Read it **before** you write or edit any of these in `src/agent_takkub/`:
- a `setStyleSheet(...)` / QSS string / `.qss`
- a hex color, `rgba(...)`, `qlineargradient(...)`
- a `font-family`, `QFont(...)`, font size/weight
- a border, `border-radius`, padding/margin on a styled widget
- a new button / chip / badge / list / dialog / toggle / panel

If you're about to type `#` followed by 6 hex digits inside a `.py` UI file —
stop and check whether a `cockpit_theme` constant already means that.

---

## Token reference (import these — values shown for orientation only)

All names below are real constants in `cockpit_theme.py`. **Reference by name;
the hex is shown so you recognize it, not to retype.**

### Grounds (backgrounds, darkest → lightest)
| Constant | Value | Use for |
|---|---|---|
| `GROUND_BODY` | `#050608` | app body / deepest backdrop |
| `GROUND_TITLEBAR` | `#0f1114` | title bars |
| `GROUND_SIDEBAR` | `#101216` | sidebars, footers |
| `GROUND_WINDOW` | `#15171c` | window / content surface |
| `GROUND_PANEL` | `#181b21` | cards, panels, lists |
| `GROUND_PANEL_ALT` | `#191c22` | alternating panel |
| `GROUND_INPUT` | `#1c1f26` | input fields, list hover |
| `GROUND_SELECT` | `#232732` | selected row, dropdown menu, toggle-off track |
| `STATUS_STRIP_GRAD_TOP` / `_BOTTOM` | `#181b21`→`#141519` | status-strip vertical gradient |

### Borders (always white-overlay rgba — never a solid zinc grey)
| Constant | Value | Use for |
|---|---|---|
| `BORDER_HAIRLINE` | `rgba(255,255,255,0.06)` | section dividers, panel edges |
| `BORDER_MED` | `rgba(255,255,255,0.09)` | input borders |
| `BORDER_STRONG` | `rgba(255,255,255,0.12)` | secondary-button border, scrollbar handle |
| `BORDER_STRONG2` | `rgba(255,255,255,0.14)` | strongest hairline |
| `RADIUS_SM` / `RADIUS_MD` / `RADIUS_LG` | `8` / `10` / `14` px | small controls / cards / large surfaces |

### Accent — gold (the ONE primary accent)
| Constant | Value | Use for |
|---|---|---|
| `ACCENT_GOLD` | `#E3B341` | active nav indicator, focus ring, selection, checkbox-on, toggle-on |
| `GOLD_GRAD_TOP` / `GOLD_GRAD_BOTTOM` | `#EEC25A`→`#E3B341` | primary CTA gradient |
| `GOLD_TEXT_ON` | `#241a00` | text/knob on a gold fill |
| `GOLD_CHIP_BG` / `GOLD_CHIP_BORDER` / `GOLD_CHIP_TEXT` | `rgba(227,179,65,.12)` / `.35` / `#ECCB6A` | soft gold chip / active-state pill |

> Indigo (`#6366f1`), violet (`#a855f7`), and generic amber (`#f59e0b`) are **not**
> accents in this system. If existing code uses them for "active / selected /
> primary," that's legacy drift → migrate to gold.

### Text (primary → faint)
| Constant | Value | Use for |
|---|---|---|
| `TEXT_PRIMARY` | `#f2f3f5` | body text on dark |
| `TEXT_PRIMARY_ALT` | `#e9ebef` | headings |
| `TEXT_SECONDARY` | `#c7ccd4` | labels, nav text |
| `TEXT_MUTED` | `#7b828f` | hints, sub-labels |
| `TEXT_FAINT` | `#5b626e` | disabled, version strings, dots |

(Also `TEXT_SECONDARY_ALT`, `TEXT_MUTED_ALT`, `TEXT_FAINT_ALT` for near-neighbors —
prefer the base name unless matching an existing pixel.)

### Light variant (#506) — same token NAMES, different values

The tables above show the **dark** values (the module defaults). The light set
lives in `cockpit_theme.LIGHT_TOKENS` — same key set (guarded by
`test_cockpit_theme.py::TestThemeVariants`), so code that reads tokens by name
needs zero changes. Orientation values (do not retype — read the dict):

| Token | Light value | Note |
|---|---|---|
| `GROUND_BODY` / `GROUND_WINDOW` / `GROUND_PANEL` | `#e8eaee` / `#f5f6f8` / `#ffffff` | paper grounds, white cards |
| `GROUND_SIDEBAR` / `GROUND_INPUT` / `GROUND_SELECT` | `#eef0f3` / `#f1f3f6` / `#e3e7ee` | |
| `BORDER_*` / `HOVER_*` | `rgba(16,24,40,…)` | dark overlays (white overlays are invisible on light) |
| `ACCENT_GOLD` | `#a87b16` | darkened fill accent — 3.8:1 on white (3:1 component minimum) |
| `ACCENT_GOLD_TEXT` / `GOLD_CHIP_TEXT` | `#7a5a10` | gold-as-TEXT darkens further (4.5:1 small-text minimum) |
| `TEXT_PRIMARY` → `TEXT_FAINT` | `#1c2026` → `#8a919c` | dark-on-light ramp |
| `STATE_*` / `CHIP_*` / `USAGE_*` | darker hues | e.g. warn `#b45309`, ok `#1f7a3d` — readable as text on white |
| `BANNER_*` triples | pastel bg + dark text | e.g. warn `#fef3c7`/`#d97706`/`#92400e` |

New-token checklist: dark constant → name in `_THEMED_TOKEN_NAMES` → entry in
`LIGHT_TOKENS` (contrast-checked on `#f5f6f8`/white, not a naive inversion).

### Semantic colors (keep the *meaning*, tokenize the *value*)
These are intentional and must survive migration — do **not** turn them gold.
They currently live as literals; where a token exists, use it, otherwise add one:
- **Role identity** → `ROLE_COLORS[name]` (see Role colors below)
- **Provider brand** → codex teal `#10a37f`, gemini blue `#4285f4` (status_header chips)
- **Status states** → error red, warn amber, ok green (badges/meters)
- **Anthropic clay** `#d97757` / `#e08968` → the token/usage-meter accent (a real
  fourth brand color, distinct from gold — keep it for meters only)
- **Substitute / parallel badges** → `SUBSTITUTE_BADGE_*`, `PARALLEL_CHIP_*`

### Role colors — use `ROLE_COLORS`, not a second palette
`cockpit_theme.ROLE_COLORS` is the canonical role palette (`lead=#E3B341`,
`frontend=#34B7AC`, `backend=#4E86F7`, `mobile=#A472F0`, `devops=#43B562`,
`qa=#E39A3C`, `reviewer=#F26D6D`, `critic=#F0619A`, …). Custom roles not in the
dict fall back to `roles.Role.color` at the call site.

> ⚠️ `roles.py Role.color` is a **different, older role palette** (`lead=#f5c542`,
> `frontend=#22d3ee` cyan, …) that feeds the main grid. It disagrees with
> `ROLE_COLORS` on every role. Until they're reconciled (see migration audit),
> when you need a role color in a **new** surface, pull from
> `cockpit_theme.ROLE_COLORS` and only fall back to `Role.color` for unknown
> custom roles.

### Fonts — IBM Plex, always resolved through the helper
Never hardcode `"Segoe UI"`, `"Consolas"`, or a bare `QFont()`. Resolve the real
family (bundled IBM Plex or a per-platform fallback) once:

```python
fonts = theme.ensure_fonts_loaded()   # {"sans": ..., "mono": ..., "bundled": bool}
sans, mono = fonts["sans"], fonts["mono"]
```

- **Sans** (`fonts["sans"]`) → all UI body/labels/buttons
- **Mono** (`fonts["mono"]`) → chips, badges, version strings, brand marks,
  titlebar labels, anything code-like or "status readout" flavored

---

## Component patterns (copy from `settings_window.py` / `cockpit_theme.py`)

Prefer the **helpers** and **objectName-driven QSS** in `cockpit_theme.py` over
per-widget inline stylesheets. The full QSS is `theme.build_stylesheet(sans, mono)`
— apply it once at the window level, then just set `objectName`s.

### Ready-made widget helpers (import + call)
| Helper | Produces |
|---|---|
| `theme.gold_button(text)` | primary CTA — gold gradient, dark bold text, drop-shadow glow |
| `theme.secondary_button(text)` | transparent + `BORDER_STRONG` outline |
| `theme.role_chip(label, color)` | colored dot + mono label (role identity) |
| `theme.gold_soft_chip(text, compact=?)` | rounded soft-gold pill (active-template badge, status chip) |
| `theme.ToggleSwitch(checked=?)` | sliding track+knob toggle; **on = gold**, disabled = muted |

### objectName → styled by `build_stylesheet` (set the name, don't restyle)
`goldButton`, `secondaryButton`, `navButton` (`[active="true"]` variant),
`newRoleButton`, `panel` / `panelAlt`, `panelTitle` / `panelHint`, `infoBanner`,
`substituteBadge`, `contentTitle` / `contentSub`, `titlebar` / `titlebarLabel`,
`statusStrip` / `statusBrand` / `statusVersion`, `sidebar` / `sidebarSection`,
`navIndicator`, `unsavedDot` / `unsavedLabel`, `placeholderBadge`.
Plain `QLineEdit / QComboBox / QSpinBox / QListWidget / QCheckBox / QTabBar /
QScrollBar` are all themed generically — no per-widget QSS needed inside a
theme-stylesheet window.

### Component rules of thumb
- **Buttons:** primary action → `gold_button`. Everything else → `secondary_button`.
  Never a raw `QPushButton` with an inline color.
- **Chips / badges:** mono font, `999px` radius (pill), soft-tinted bg + matching
  border at ~12–35% opacity. Gold status → `gold_soft_chip`. Role → `role_chip`.
- **Selection / focus / active:** gold. Selected list row → `GROUND_SELECT` bg.
  Focus ring / active nav indicator / checkbox-checked / toggle-on → `ACCENT_GOLD`.
- **Lists:** `GROUND_PANEL` bg, `BORDER_MED` border, `RADIUS_SM`, hover =
  `GROUND_INPUT`, selected = `GROUND_SELECT`.
- **Dialogs / panels:** `GROUND_WINDOW` (window) / `GROUND_PANEL` (card),
  `BORDER_HAIRLINE`, `RADIUS_MD`. Title = `TEXT_PRIMARY_ALT` 20/700.
- **Radii:** only `RADIUS_SM/MD/LG` (8/10/14) or `999px` for pills. No stray `6px`.

---

## Known inconsistencies — MIGRATED (historical table below)

**Status 2026-09-07 (#506): the hex migration is DONE.** Every file below now
reads `cockpit_theme` tokens — `tests/test_no_hardcoded_ui_colors.py` enforces
zero `#rrggbb` literals outside `cockpit_theme.py` (allowlist: roles.py /
custom_roles.py role-identity data, issues.py GitHub label colors,
design_review_html.py browser artifact, roles_page.py's default color text).
The table is kept only as a map of which file owns which *semantic* colors.
Full per-file audit: `docs/design-review/2026-07-11-cockpit-ui-consistency-audit.md`.

| File | What's off vs. this system |
|---|---|
| `status_header.py` | zinc greys (`#3f3f46`,`#71717a`,`#27272a`,`#52525b`), amber `#d97706`/`#f59e0b`, ad-hoc role dots; separators use solid `#3f3f46` not `BORDER_*` |
| `project_nav.py` | **indigo/violet accent** (`#6366f1`,`#818cf8`,`#8b5cf6`) for active/selected instead of gold; zinc grounds `#0e0e10`/`#18181b`; own 10-color avatar palette (avatar tint is fine to keep — but it's a 3rd palette) |
| `task_dock.py` | own status palette (`#facc15`/`#22c55e`/`#ef4444`), violet `#a78bfa`, zinc greys + `#1e1b2e` indigo-tint grounds |
| `agent_pane.py` | own state dots (green/gold/blue/orange/red), zinc grounds `#18181b`/`#1c1c20`, `#e5e7eb` text |
| `update_panel.py` | blue CTA (`#2563eb`/`#1d4ed8`) instead of gold; amber/green/red banner sets as literals |
| `user_actions.py` | green (`#16a34a`) + blue (`#2563eb`) buttons instead of `gold_button`/`secondary_button`; zinc greys |
| `main_window.py` | zinc grounds `#09090b`/`#18181b`/`#27272a`/`#3f3f46` as base app chrome |
| `project_tab.py` | indigo `#6366f1` active accent, zinc grounds |
| `token_meter.py` / `usage_meter.py` / `logs_panel.py` / `limit_panel.py` | Anthropic clay `#d97757`/`#e08968` + amber/green/red — **semantic, keep**, but tokenize (add meter/state tokens to `cockpit_theme`) |
| `tutorial_overlay.py` | zinc greys + clay `#e08968` |
| `roles.py` | `Role.color` is a **second role palette** disagreeing with `ROLE_COLORS` on every role → reconcile |
| `issues.py` | GitHub-label hexes (`#d73a4a` etc.) — external data, **not** cockpit chrome; leave |

`project_wizard.py` has **no** hardcoded colors — it inherits the parent window's
QSS. Give its parent the theme stylesheet and it comes along for free.

---

## The one-line litmus test

> About to type a raw `#rrggbb`, a font name, or a radius in a cockpit `.py`?
> → Find the `cockpit_theme` constant that means it and import that instead.
> No constant exists? → Add it to `cockpit_theme.py`, then import. Never inline.
