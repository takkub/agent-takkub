# Settings UI Audit Report (2026-07-20)

**Scope:** Evidence-based audit of the live UI state across the new `👥 Team` window, the legacy `Pipeline Settings` window, and the `Status Bar`, specifically focusing on the provider toggles, CLI assignment, and the new Model field feature.

## 1. 👥 Team → Providers Window
- **Model Field Presence**: Confirmed. The `Model` text input field successfully appears and is enabled for **all 6 providers** (`claude`, `codex`, `gemini`, `opencode`, `kimi`, `cursor`).
  - *Evidence*: `02-team-provider-claude.png`, `02-team-provider-codex.png`, `02-team-provider-gemini.png`, `02-team-provider-opencode.png`, `02-team-provider-kimi.png`, `02-team-provider-cursor.png`
- **Round-Trip Persistence**: Confirmed. Tested modifying the `gemini` provider's model string to `test-model-abc`, saving, and refreshing. The state survived the round-trip properly. Clearing the field and saving also wiped it correctly.
  - *Evidence*: `03-team-roundtrip-save.png`, `04-team-roundtrip-clear.png`
- **Toggle State Accuracy**: Confirmed. The `opencode`, `kimi`, and `cursor` switches are in the OFF state, which correctly mirrors the live `~/.takkub/disabled-providers.json` config (where they are set to `true` / disabled).

## 2. Legacy Pipeline Settings → Providers & Roles
- **Model Field Gap**: **CONFIRMED**. There is no "Model" input field present anywhere on this legacy view. This explains the user's issue: the feature was only added to the new Team window and skipped the legacy one.
  - *Evidence*: `05-legacy-settings.png`
- **Dropdown completeness**: The per-role provider selection dropdown *does* successfully list all 6 providers.
  - *Evidence*: `06-legacy-combo-lead.png`
- **Stale Copy**: **CONFIRMED**. The sub-header text reads: `"เปิด/ปิด provider (codex/gemini) + กำหนด CLI ต่อ role"`. This copy fails to mention the newer providers (`opencode`, `kimi`, `cursor`, `claude`) and needs updating.

## 3. Status Bar
- **Provider Chips**: Confirmed removed. The provider toggle dots/chips are entirely absent from the status bar.
- **Team Chip**: Confirmed present. The `👥 Team` button remains on the right side and is clickable.
  - *Evidence*: `07-status-bar.png`

## Summary Table

| UI Surface | Element | Status | Notes |
| :--- | :--- | :--- | :--- |
| **👥 Team -> Providers** | Model Input Field | ✅ Present (All 6) | Works correctly with save/clear |
| **👥 Team -> Providers** | Provider Toggles | ✅ Accurate | Matches disabled-providers.json |
| **Legacy -> Providers & Roles** | Model Input Field | ❌ Missing | **CONFIRMED GAP** |
| **Legacy -> Providers & Roles** | Roles Dropdown | ✅ Present (All 6) | |
| **Legacy -> Providers & Roles** | Page Copy | ❌ Stale | Still says `(codex/gemini)` |
| **Status Bar** | Provider Chips | ❌ Removed | Expected behavior |
| **Status Bar** | 👥 Team Chip | ✅ Present | |

*Screenshots have been generated and saved alongside this report in the `docs/qa-reports/` directory.*
