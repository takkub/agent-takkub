# Usage popover: quota bars (2026-08-16)

Task: add an at-a-glance progress bar to each provider card in the usage
popover (`UsageMeter` → click the corner chip → `_ProviderDetailPopup`).
Presentation-only — `fetch_usage_shared()` / `LimitStore` untouched.

File touched: `src/agent_takkub/usage_meter.py`.

## Before

Every provider card was text-only, and the *meaning* of the number wasn't
even consistent across providers:

- Claude · Pro → `5h: 99% · reset ใน 20น.` / `7d: 75% · reset ใน 15น 10ชม.`
  (quotes the raw `utilization` field, i.e. **% used**)
- Codex · plus → `เหลือ 67% (ใช้ไป 33%) · reset ใน 4วัน 22ชม.` (leads with
  **% left**)
- OpenCode → `ใช้ไปแล้ว 775,045 tokens (ไม่มีโควต้า)` (no denominator at all)
- Gemini → `เหลือ 100% (ใช้ไป 0%) · reset ใน now` + `ข้อมูลเมื่อ 5 เดือนที่แล้ว`
  (stale cache, would've read as "all clear" if bar-ified naively)
- Kimi / Cursor → `ไม่มีข้อมูลให้ดู (provider นี้ไม่รองรับ)`

Reading any card required parsing the number; there was no shape to skim.

## After

Added a thin (`_UsageBar`, 6px, rounded, `GROUND_SELECT` track) meter above
the existing text line, only where a real quota fraction exists:

- **Claude**: two labelled bars, `5h` and `7d`, each independent — the two
  rolling windows never collapse into one bar.
- **Codex / any single-window `utilization` provider**: one bar.
- **OpenCode**: **no bar.** It only has a self-tallied token count
  (`spend`), never a quota denominator — a bar would fabricate a "% full"
  that doesn't exist. Kept as the plain token/cost line it already was.
- **Kimi / Cursor (`unsupported`) / `loading` / `error`**: no bar, text
  unchanged.
- **Stale data (Gemini's 5-month-old cache, or any `status == "stale"`)**:
  the bar still renders (so the shape/severity is still skimmable) but the
  fill uses a diagonal hatch brush (`Qt.BrushStyle.BDiagPattern`) instead of
  a flat fill, so a long-stale "100% left" can never paint as a solid,
  confident all-clear green bar. The existing `ข้อมูลเมื่อ …` age line stays
  directly under it.

### Convention: bar fill = "% of quota already used"

`ProviderUsage.utilization` (headline and per-window, per
`provider_usage.py`'s own docstring: *"utilization... quota-percentage
semantics"*, backed by `used_percent`) is **already defined as % used**
everywhere in the codebase — Claude's `5h: 99%` line was already quoting it
directly with no left/right flip, and Codex's `ใช้ไป 33%` is the same field.
So the bar fill is that number, unflipped: `severity_color(pct_used)` picks
the same color for both the bar and the pre-existing text, and a
99%-utilization Claude window paints a bar that's 99% full (near-empty
quota, "watch out"), not 99% empty. The only surface-level inconsistency was
that Codex's text *led* with "เหลือ" (left) before "(ใช้ไป …)" (used) — the
bar makes the shared "used" meaning visually explicit across every card
without having to reword the existing Thai copy.

Colors are 100% `severity_color()` → `cockpit_theme.{METER_CLAY,
METER_AMBER, STATE_ERROR_BRIGHT, TEXT_MUTED}` — no new hex literals. Track
color is `GROUND_SELECT` (existing token). No dark/light branching needed:
this whole surface is a fixed dark popup (`GROUND_WINDOW` frame), consistent
with the rest of the cockpit's dark-only chrome.

### Width

`_ProviderDetailPopup` is already clamped to
`min(380px, screen_width - 2*margin)` (`_popup_position` /
`mousePressEvent`), so on a small screen the popup — and every bar inside it
— shrinks with it. No new overflow risk; `_UsageBar` has `setMinimumWidth(60)`
only as a floor, not a fixed size.

## Verification

- Targeted tests (no full suite — mid-task tier per project convention):
  `tests/test_usage_meter_bars.py` (new — 5 cases: Claude's two labelled
  bars, a window with `utilization=None` gets no bar, generic-provider bar
  quotes % used not % left, `stale` flag reaches the bar, unsupported/
  loading/error never get a bar), `tests/test_usage_meter_spend.py` (updated
  for the renamed `_provider_body_lines` → `_provider_body_entries` API +1
  new case asserting OpenCode never gets a bar), `test_usage_meter_lead_provider.py`,
  `test_usage_meter_popup.py`. All 18 green via
  `PYTHONPATH=<this repo>/src` override (worktree's shared-venv editable
  install points at the base checkout, see `tests/conftest.py`'s own error
  message / `docs/*` #202 note — did not run `pip install -e .` from inside
  the worktree, per pane policy).
- Offscreen PyQt smoke render (`QT_QPA_PLATFORM=offscreen`, no
  Playwright/browser — native `QWidget.grab()`) confirms the actual painted
  layout: Claude card shows 2 bars + labels, Codex 1 bar, OpenCode 0 bars,
  Gemini 1 hatched (stale) bar, Kimi 0 bars. Screenshot (Thai glyphs render
  as tofu boxes in this headless run because the bundled IBM Plex font isn't
  loaded outside the real app's `ensure_fonts_loaded()` — bar shapes/colors
  are what's being checked here, not text legibility):
  `docs/audit/assets/2026-08-16-usage-popover-bars-offscreen.png`

## Not done / out of scope

- Did not touch `fetch_usage_shared` / `LimitStore` / any fetch logic, per
  task constraint.
- Did not attempt a live in-app screenshot (would need the real cockpit
  running with logged-in providers to populate real usage — flagged instead
  of guessing at real account numbers).
