# Boot-update splash — UI review

**Module:** `src/agent_takkub/boot_update_window.py` (merged into main via #313/boot-update-policy)
**Reviewer:** critic (design)
**Reviewed against:** `.claude/skills/cockpit-ui-style/SKILL.md` (full)
**Method:** full-file read + live re-render on this machine (`QT_QPA_PLATFORM=offscreen`,
real bundled fonts, `app.setFont(QFont("Segoe UI", 10))` matching `app.py:865` exactly) +
`QFontMetrics.inFontUcs4()` glyph-coverage checks against the actual bundled `IBM Plex Sans`/`IBM Plex Mono`.
Screenshots: backend's `runtime/exports/2026-08-20/agent-takkub/backend/boot-update-splash.png` +
my own re-renders (scratchpad, not committed).

User's bar for this window: **"สวย ดูดี ทันสมัย" — premium.** It is also the very first thing
a user sees on every boot, frameless, and the only genuinely frameless top-level window in
the whole cockpit (no other window in the codebase uses `FramelessWindowHint`) — so none of
this module's layout/window-chrome choices had an existing pattern to copy from.

---

## 🔴 Must-fix before publish

### 1. `✗` and `⬆` are not in the bundled font — confirmed, not sandbox-only

Backend flagged empty boxes in their offscreen render and guessed "font fallback." I
reproduced the **identical** empty boxes on this real dev machine, with the real bundled
fonts and the app's real `app.setFont(QFont("Segoe UI", 10))` applied — this is not an
artifact of a stripped-down CI sandbox missing font files.

Proof (`QFontMetrics.inFontUcs4`, run against the actual fonts this app loads):

| glyph | used for | in `IBM Plex Sans` | in `IBM Plex Mono` |
|---|---|---|---|
| `✓` U+2713 | done/updated (`_ProviderRow.set_status`, line 181) | ✅ yes | ✅ yes |
| `✗` U+2717 | failed (line 183) | ❌ **no** | ❌ **no** |
| `⬆` U+2B06 | model-catalog bump note (`_model_note`, line 105) | ❌ **no** | ❌ **no** |

Root cause: `✓`/`✗`/`⬆` sit in the Dingbats / Miscellaneous-Symbols-and-Arrows Unicode
blocks, which general-purpose text fonts (IBM Plex included) routinely don't cover — this
is the *same class of bug* the codebase already hit and fixed once for Thai glyphs
(`cockpit_theme.py:256-267`, "declaring only `sans_family`... left every Thai string
rendering as tofu"). It also matches the project's own established workaround: QSS
`url()` icons on disk are used for the spinbox up/down arrows specifically **because**
Qt/QSS glyph rendering for arrow-ish symbols was already proven unreliable
(`cockpit_theme.py:401-403`, "proven by pixel measurement"). This module didn't apply
that lesson to its own status glyphs.

Also note: `name_label`/`_status_label`/dot/footer never call `.setFont(...)` at all (see
finding #4) — they render in whatever the app-wide default font resolves to, not
`IBM Plex`. Whichever font actually wins, the specific glyphs above are the fragile part;
picking Unicode-safe replacements only helps if paired with #4.

**Fix — two options, ranked:**
- **(preferred, matches "premium")** Stop depending on font glyph coverage for status at
  all: paint small fixed-size vector icons (checkmark / x mark in a filled circle, or
  reuse the on-disk-SVG pattern `cockpit_theme.py` already uses for the spinbox arrows) at
  a `QPainter`/`QLabel(pixmap)` level. Guarantees pixel-identical rendering on every OS/DPI
  regardless of installed fonts — and looks more like a native installer status list than a
  string with a glyph glued to it.
- **(minimal patch)** Swap to glyphs confirmed present in `IBM Plex Sans` (tested just now,
  same method): `×` (U+00D7, replaces `✗`) and `↑` (U+2191, replaces `⬆` — thin arrow, not
  the heavy `⬆`). Must be paired with #4 (actually apply the IBM Plex font to these labels) —
  otherwise you're still gambling on whatever font the label falls back to.

### 2. Fixed-height window overlaps text when multiple rows get long — reproduced

`self.setFixedSize(480, 60 + 56 * len(PROVIDER_REGISTRY) + 70)` bakes in "56px/row, with
headroom for **the occasional** second line" (the code comment's own words). I stress-tested
the very case the comment says is rare — **all 6 providers** getting a model-catalog bump
note in the same boot (a perfectly ordinary boot, not a contrived edge case) — and every
row grows to 3 lines (status line + 2-line-wrapped model note) while the window height stays
locked. Result: **rows visually overlap and become unreadable**, confirmed by screenshot —
row N's third line prints directly through row N+1's name/status line.

This isn't a rare edge case: any boot where ≥2 providers both update their binary *and* have
a model-catalog bump will trigger it, and that only gets more likely as `PROVIDER_REGISTRY`
grows (multi-provider directive — #103 — expects more providers over time, not fewer).

**Fix:** don't hardcode the window height from a row-count formula. Either (a) drop
`setFixedSize` for height and let the `QVBoxLayout` size the window from actual content
(`setFixedWidth(480)` + `adjustSize()`/`sizeHint()`-driven height, recomputed after every
`set_status()` call), or (b) keep the detail+model-note fully in the tooltip (already wired,
`self._status_label.setToolTip(detail or "")`) and cap the visible line to one line with
elision, never letting a row grow past 2 lines in the first place.

### 3. `WA_TranslucentBackground(False)` + `border-radius:14px` on a frameless window — verify, likely wrong

```python
self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)   # ← this line
...
self.setStyleSheet(f"#bootUpdateWindow {{ ... border-radius: {theme.RADIUS_LG}px; }}")
```

This is a well-known Qt pitfall: a `border-radius` painted on a top-level widget's own
background only looks rounded if the **native window surface itself** is translucent —
otherwise the underlying opaque rectangular surface shows through as square corner-artifacts
outside the rounded fill (a "halo" at each of the 4 corners). Explicitly forcing
`WA_TranslucentBackground` to `False` on the one frameless window in the whole app, while
also giving it a rounded QSS background, is the textbook setup for that artifact. I can't
prove the pixel-level corner halo from an offscreen `grab()` (it paints the widget tree only,
not real DWM compositing) — **this needs a live-display check** — but given there's no
comment explaining why translucency was deliberately disabled here, and no other window in
the codebase to have copied the choice from, this reads like an oversight rather than an
intentional decision.

**Fix (if confirmed live):** `WA_TranslucentBackground(True)`, matching what the
rounded-corner styling already implies.

### 4. Provider-row text never resolves the theme font at all

`title`/`subtitle` correctly call `theme.ensure_fonts_loaded()` and `.setFont(_font(sans, ...))`.
Every other label in the window — `name_label`, `_status_label`, the role-color `dot`,
`_footer_label` — only ever calls `.setStyleSheet(f"color: ...")`, never `.setFont(...)` and
never a `font-family` in the QSS string. They fall through to whatever
`QApplication.setFont(QFont("Segoe UI", 10))` (`app.py:865`) set as the app-wide default —
**not** `IBM Plex Sans`. So on the very first screen a user sees, the title is IBM Plex and
almost everything else — 5 of 6 provider names, every status line, the footer — is plain OS
Segoe UI at a smaller, unspecified size. This directly contradicts the design system's "one
font system" rule and is also *why* finding #1's glyph gamble is unverifiable — you don't
actually know which font is rendering these labels without tracing app.py.

**Fix:** resolve `sans = theme.ensure_fonts_loaded()["sans"]` once (already done at line 236,
just unused past the header) and call `.setFont(_font(sans, <size>, <weight>))` on
`name_label`, `_status_label`, `dot`, and `_footer_label`, picking sizes off the same 13px
body-text scale `build_stylesheet` uses elsewhere (`QDialog#settingsWindow { font-size: 13px }`)
rather than leaving size unset.

---

## 🟡 Nice-to-have (worth doing to actually earn "premium")

- **Real icon glyphs, not ASCII-adjacent characters.** Even after fixing #1, a bare `✓`/`×`
  character next to text reads as "quick patch," not "premium." A small painted status
  dot/badge (filled circle for done, ring for pending, x-in-circle for failed — reusing the
  existing colored role-dot idiom already in this file) would read more like a native
  installer and sidesteps font risk entirely. Same underlying fix as #1's preferred option.
- **No entrance/exit fade for the window itself.** Every row already gets a tasteful
  `QPropertyAnimation` opacity fade on status transitions (lines 199-206, explicitly called
  out as "the premium touch" in the code comment) — but the splash itself just snaps into
  existence on `show()` and vanishes instantly on `close()` (`run_boot_update_gate`,
  `splash.close()`). Bookending with the same cheap `QGraphicsOpacityEffect` +
  `QPropertyAnimation` pattern (already imported, already the established idiom in this exact
  file) would make the whole window seams-free, not just its rows.
- **No aggregate progress indicator.** "เสร็จ 4/5 ตัว" is footer text only — a slim full-width
  gold progress bar (0→total eligible) across the top or bottom would read at a glance without
  parsing Thai text, and matches how native installers signal overall progress vs. per-item
  status.
- **No brand mark.** Header is plain text ("Takkub Cockpit") with no icon/wordmark. If the app
  icon or a wordmark asset already exists elsewhere (window icon, `status_header.py` brand
  chip), echoing a small version of it next to the title would reinforce identity on first
  paint rather than reading as a generic Qt dialog.
- **`name_label` has no wrap/elide.** `setFixedWidth(90)` with no `setWordWrap(True)` and no
  elide mode — today's 6 provider names all fit, but the multi-provider directive
  (`CLAUDE.md` — "ทุก feature ต้องคำนึงถึงทุก provider... future") means this list only grows.
  A future provider with a longer display name will silently clip or overflow rather than
  wrap, since only `_status_label` gets `setWordWrap(True)` (line 157).
- **Progress bar is visually an afterthought.** 70px wide, right-aligned, appears only on the
  "updating" row — easy to miss next to a Thai status string. Consider either widening it or
  moving status text weight down (progress bar is the primary "something is happening"
  signal, text is secondary) — currently they compete for the same 6px-tall sliver of
  attention.
- **Copy polish (low-confidence, verify before touching):** version-bump text like
  `"v2.1.60 -> v2.1.61"` uses a literal ASCII `->` rather than a real arrow glyph. `→`
  (U+2192) tested present in the bundled `IBM Plex Sans` (see glyph table method above) if
  someone wants to upgrade it — but this string may originate in `provider_update.py`, not
  this module, so scope it correctly before editing.

## ⚪ Checked, not a defect

- **DPI scaling (125%/150%).** Can't be verified from an offscreen render (the offscreen QPA
  platform reports a synthetic fixed DPI, not real Windows scaling) — but `app.py` never
  overrides `AA_EnableHighDpiScaling` or the DPI rounding policy, so Qt6's default
  proportional HiDPI handling should apply to this window's fixed logical-pixel sizes like
  everywhere else in the app. **Recommend a real spot-check on a 125%/150% display before
  sign-off**, but nothing in the code suggests it's broken.
- **Dark-theme-only.** Consistent with the rest of the cockpit — `cockpit_theme.py` has no
  light-mode token set anywhere, so this window matching that is expected, not a gap specific
  to this module.
- **Semantic color tokens.** `theme.STATE_OK` / `theme.STATE_ERROR` / `theme.TEXT_FAINT` /
  `theme.ACCENT_GOLD` are used correctly for done/failed/skipped/updating — matches the design
  system's "status states" rule, no drift here.
- **Role-color dots.** Pulled from `theme.ROLE_COLORS` (the canonical palette), not the legacy
  `roles.py Role.color` — matches the migration guidance in the skill doc.
- **Progress-bar radius (3px).** Looks like a stray non-token value at first glance, but for a
  6px-tall bar, `radius = height / 2` is exactly a full pill shape — correct, not a violation.

---

## Priority summary for Lead

| # | Finding | Severity |
|---|---|---|
| 1 | `✗`/`⬆` glyphs missing from bundled font → empty boxes | 🔴 must-fix |
| 2 | Fixed window height → text overlap with ≥2 long rows | 🔴 must-fix |
| 3 | `WA_TranslucentBackground(False)` + rounded frameless window → likely corner artifact (verify live) | 🔴 must-fix (pending live verify) |
| 4 | Provider-row text never applies `IBM Plex` — falls back to OS Segoe UI | 🔴 must-fix |
| 5 | ASCII glyphs vs. real painted status icons | 🟡 nice-to-have |
| 6 | No window-level fade in/out | 🟡 nice-to-have |
| 7 | No aggregate progress bar | 🟡 nice-to-have |
| 8 | No brand mark in header | 🟡 nice-to-have |
| 9 | `name_label` no wrap/elide for future long provider names | 🟡 nice-to-have |
| 10 | Progress bar visually undersized vs. its signal importance | 🟡 nice-to-have |
| 11 | `->` vs `→` copy polish (verify string ownership first) | 🟡 nice-to-have |

Findings #1 and #2 are both concretely reproduced (not speculative) via local re-render on
this machine using the app's real fonts and real `app.setFont()` call — screenshots kept in
scratchpad, not committed (temp evidence per role convention).
