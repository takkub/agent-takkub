# Report builder kit (#626)

Source of truth for the report toolkit shipped to every installed build via
setup.py's `_stage_assets` → `src/agent_takkub/_assets/report/` (see package_data
in pyproject.toml). A dev checkout reads these files straight from this repo-root
directory; `report_builder.report_asset_root()` picks whichever applies.

- `template.html` — base HTML template (CSS + lightbox) for all 3 report types
  (customer/dev/boss). `report_builder.build()` injects its content CSS + the
  content body into it; EXTRA_CSS (mobile overflow fixes) lives in
  `report_builder.py` itself. If this file is missing, `takkub report build`
  silently falls back to a minimal template — keep it shipped.
- `mobile-check.cjs` / `tables-check.cjs` — Node/Playwright reference checks for
  viewport overflow (360/390/768) and side-scrolling tables. `report_builder.py`
  also exposes the same logic inline as `check_mobile()`. Both files are the
  reference point the #626 mobile lesson is based on.

Usage of the report builder (content rules, image mapping, lint) →
`docs/lead/report-publish.md`.