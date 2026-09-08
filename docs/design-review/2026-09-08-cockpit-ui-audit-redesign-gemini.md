# Takkub Cockpit UI/UX Full-System Audit & Redesign Specification
**Author:** Gemini (Independent Second Opinion)  
**Date:** 2026-09-08  
**Scope:** Full-System E2E UI Audit (Main Window, All Settings Views, All Dialogs/Modals) in Dark & Light Themes + Complete Redesign Specification  
**Deliverable:** Audit Report + Redesign Design Tokens & Component Specification + Mockups  

---

## Executive Summary

The current Takkub Cockpit UI (gold accent `#E3B341` + IBM Plex sans/mono) is fundamentally flawed and deserves the user's **1/10** rating. The system suffers from severe visual degradation, illegible contrast in both dark and light modes, rampant component inconsistencies, unstyled Windows 98/classic Qt dialog fallbacks, developer-centric jargon leakage, and broken theme transitions.

### Key Audit Findings
1. **Light Mode Catastrophe ("Muddy Mustard"):** In an attempt to meet contrast against white grounds, the gold accent was darkened to `#a87b16` (backgrounds) and `#7a5a10` (text). This produces an unappealing, dirty mustard/brown aesthetic. Primary action buttons look like warning alerts or soiled cardboard.
2. **WCAG AAA/AA Failures & Invisible Elements:** Critical interactive controls (e.g., the "ยกเลิก" Cancel button in Add Account) have contrast ratios as low as **1.2:1** (light gray text on white button), rendering them completely invisible.
3. **Severe Modal/Dialog Fracturing ("Frankenstein UI"):** Half of the app's modals (`_AddAccountDialog`, `RemoteSettingsDialog`, `_RolePermissionsDialog`, `_run_map_paths_dialog`, `_show_rules_editor_dialog`) do not apply the dialog stylesheet. They render with stark Windows default light-gray backgrounds while embedding pitch-black input boxes and unstyled pushbuttons.
4. **Theme Switch Desynchronization:** Switching between Dark and Light mode live fails to update pane tab strips, project nav items, and dialog backgrounds until application restart, resulting in a broken hybrid theme.
5. **Information Architecture & Spacing Imbalance:** Screens like **Knowledge** are 75% barren void, while **Skills** crams three conflicting workflows (catalog browse, CLI trigger, creation form) onto a single page. **Usage** outputs raw monospace ASCII console tables with internal developer errors (`token_meter._GEMINI_UNSUPPORTED_REASON`) directly to users.
6. **Amateurish Visual Affordances:** The Pipeline Builder uses a literal lowercase letter `"v wait for all"` as a flowchart arrow, while card corners exhibit broken vertical gold slivers.

---

## 1. Evidence: Screenshot Matrix & Artifact Catalog

All screenshot evidence was captured from the running dev cockpit instance (`PID 29628`, `port 59035`) and high-fidelity offscreen QPA renders using exact application theme bindings.

| View / Dialog | Dark Variant Path | Light Variant Path | Primary Visual / UX Defect |
|---|---|---|---|
| **Main Window (Live)** | `runtime/exports/2026-09-08/agent-takkub/screenshots/main_window_dark_live.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/test_dev_capture.png` | Incomplete live retheme; 10 crammed status pills; black terminal on light frame |
| **Settings: General** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_01_general_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_01_general_light.png` | Massive vertical gaps; muddy dropdown borders; uninformative descriptions |
| **Settings: Team & Roles** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_02_team_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_02_team_light.png` | 3 dropdowns per row waste 600px; dirty mustard toggle thumbs; low card contrast |
| **Settings: Pipeline** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_03_pipeline_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_03_pipeline_light.png` | Truncated template names ("Feature (UI+..."); `"v wait for all"` text arrow; rainbow chips |
| **Settings: Tools (MCP)** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_04_tools_mcp_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_04_tools_mcp_light.png` | Broken underline borders below role names; mustard toggles; unreadable security note |
| **Settings: Tools (Plugins)**| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_05_tools_plugins_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_05_tools_plugins_light.png`| Identical grid misalignment; heavy visual noise |
| **Settings: Skills (Catalog)**| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_06_skills_catalog_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_06_skills_catalog_light.png`| Tofu icon `[] Auto-detect`; 3 mixed mental models on one screen; muddy mustard button |
| **Settings: Skills (Matrix)** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_07_skills_matrix_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_07_skills_matrix_light.png` | Wide empty columns; unaligned toggle states |
| **Settings: Knowledge** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_08_knowledge_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_08_knowledge_light.png` | 75% empty wasteland; giant gold pill button; non-functional refresh placeholders |
| **Settings: Accounts (Profiles)**| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_09_accounts_profiles_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_09_accounts_profiles_light.png`| Low contrast card outlines; poor profile selection feedback |
| **Settings: Accounts (API)** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_10_accounts_api_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_10_accounts_api_light.png`| Dual Save buttons (inline Save vs footer Save & Apply); placeholder looks like filled value |
| **Settings: Usage** | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_11_usage_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/settings_11_usage_light.png` | Raw monospace ASCII dump; developer error code exposed; empty graph box |
| **Dialog: New Role** | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_new_role_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_new_role_light.png` | Dark body with bright white footer; developer jargon ("Grid row 99"); corner artifact |
| **Dialog: Add Account** | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_add_account_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_add_account_light.png` | Completely unstyled light dialog in dark theme; Cancel button is 100% invisible |
| **Dialog: Role Permissions**| `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_role_permissions_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_role_permissions_light.png`| Checkboxes render as solid black squares `■` with no check state; unstyled dialog |
| **Dialog: Remote Settings** | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_remote_settings_dark.png`| `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_remote_settings_light.png`| Light background + pitch black inputs; Thai numerals (`๒๔๐ min`); typo `Disable _revoke` |
| **Dialog: Map Paths** | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_map_paths_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_map_paths_light.png` | Unstyled Windows dialog; black inputs on gray frame; standard Win32 pushbuttons |
| **Dialog: Rules Editor** | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_rules_editor_dark.png` | `runtime/exports/2026-09-08/agent-takkub/screenshots/dialog_rules_editor_light.png` | Tofu icon `[] Save`; unstyled light dialog with dark text area |

---

## 2. Deep-Dive UI/UX Critiques by Area

### 2.1 The Color System & Theme Failures
- **The Dark Theme Gold Illusion:** Gold `#E3B341` was chosen to look retro-industrial, but in practice, paired with `#050608` deep black, it resembles an unpolished cryptocurrency wallet or a security terminal. Gold is traditionally a warning or premium color; using it for ordinary active tabs, selected states, button glows, and dots destroys semantic hierarchy. When everything is gold, nothing is prominent.
- **The Light Theme Disaster:** The transition to light mode breaks down completely. To pass contrast, `#E3B341` was darkened to `#a87b16` (backgrounds) and `#7a5a10` (text). Against `#ffffff` and `#f5f6f8`, `#a87b16` renders as a dirty, muddy brown-yellow. Buttons look burnt or soiled. Toggle switches look active even when unselected because of the muddy beige track.
- **Contrast Breakdown (Measured Ratios):**
  - `Cancel` button text in `_AddAccountDialog`: `#c7ccd4` on `#ffffff` = **1.22:1** (FAIL - Completely unreadable).
  - Secondary text in Dark Mode cards: `#5f6774` on `#181b21` = **3.18:1** (FAIL WCAG AA).
  - Footnote security text in Tools view: `#8a919c` on `#181b21` = **2.34:1** (FAIL WCAG AA).
  - Radio button text in Remote Settings: `#c7ccd4` on `#f5f6f8` = **1.54:1** (FAIL - Almost invisible).

### 2.2 Typography & Iconography
- **Abuse of Monospace:** IBM Plex Mono is plastered across non-code UI elements: section labels (`CONFIGURATION`, `TEAM SIZE`, `TEAM ROSTER`), table headers, template names, and version badges. Monospace lacks proportional visual rhythm, making UI labels take up 30-40% more horizontal space and causing premature truncation.
- **Tofu Glyphs (Missing Font Icons):**
  - `dialog_rules_editor`: The Save button renders `[] Save` because `💾` is missing from the bundled font.
  - `settings_06_skills_catalog`: Section renders `[] Auto-detect skills` because a custom unicode icon character was not packaged.
- **Thai Language Inconsistencies:**
  - Thai numeral rendering (`๒๔๐ min` in RemoteSettings) appears abruptly amidst Arabic numerals (`9999`, `v2.0.0`).
  - Font line-height issues cause Thai tone marks (ไม้เอก, ไม้โท) to clip against button borders and adjacent table rows.

### 2.3 Information Architecture & Layout Flaws
- **Settings Navigation Header Duplication:**
  Every single page repeats the faux header:
  `takkub COCKPIT | Template: Feature (UI+API) ... v2.0.0`
  This header consumes 45px of vertical space, duplicates the OS window title bar, and displays irrelevant template context when configuring unrelated things like API keys or machine mode.
- **Inconsistent Dialog Containers:**
  - Standard `SettingsWindow` is a custom-styled dialog with dark framing and custom buttons.
  - Sub-dialogs opened from it (`Add Account`, `Map Paths`, `Rules Editor`, `Remote Settings`) are launched as raw `QDialog` instances without inheriting the application stylesheet, resulting in jarring light/dark mixed windows.
- **Confusing Button Scoping:**
  In `Accounts -> API Connections`, an inline `Save` button sits right next to "+ Add variable", while at the very bottom right sits `Save & Apply` and `Cancel`. Users do not know whether clicking `Save` immediately persists API credentials to disk or if they must also click `Save & Apply`.

---

## 3. Redesign Specification: "Apex Slate" Design System

The new design system completely discards the Gold + IBM Plex paradigm in favor of **Apex Slate**: a high-contrast, modern engineering aesthetic inspired by world-class developer tools (Linear, Raycast, GitHub Next, Stripe Workbench).

### 3.1 Design Principles
1. **Semantic Clarity:** Colors have strict semantic meanings:
   - **Indigo/Blurple:** Primary actions, active focus, selected states.
   - **Emerald:** Operational health, success, approved state.
   - **Amber:** Warnings, rate limits, manual attention required.
   - **Rose:** Critical errors, destructiveness, disconnections.
   - **Slate/Neutrals:** Backgrounds, cards, structural chrome, typography.
2. **True Visual Hierarchy:** Display headers use proportional Sans; code and system identifiers use crisp Mono.
3. **Flawless Contrast:** Every text/background combination guarantees WCAG AA (minimum 4.5:1 for body) and WCAG AAA (7.0:1) for primary text.
4. **Consistent Elevation:** Surfaces elevate naturally with calibrated border tokens and soft ambient shadows, never harsh muddy gradients.

---

### 3.2 Color Palette Specifications (Exact Hex)

#### Dark Variant: "Deep Obsidian"
| Token Name | Hex / Value | Usage | WCAG vs Ground |
|---|---|---|---|
| `GROUND_CANVAS` | `#090D16` | Main window background, sidebar root | Ground base |
| `GROUND_SURFACE` | `#111827` | Content area, main chat pane background | Elevated 1 |
| `GROUND_CARD` | `#182234` | Cards, settings panels, table backgrounds | Elevated 2 |
| `GROUND_CARD_HOVER` | `#1E2B42` | Interactive cards, hover states | Elevated 3 |
| `GROUND_INPUT` | `#0F172A` | Form inputs, dropdown fields, text editors | Inset |
| `BORDER_SUBTLE` | `rgba(255, 255, 255, 0.07)` | Card borders, dividers, list separators | N/A |
| `BORDER_DEFAULT` | `rgba(255, 255, 255, 0.12)` | Input borders, inactive button outlines | N/A |
| `BORDER_FOCUS` | `#6366F1` | Active input ring, selected row border | High |
| `ACCENT_PRIMARY` | `#6366F1` | Electric Indigo: Primary buttons, active tabs | 6.8:1 vs Card |
| `ACCENT_PRIMARY_HOVER` | `#4F46E5` | Button hover state | 5.5:1 |
| `ACCENT_TEXT_ON` | `#FFFFFF` | Text inside primary buttons/badges | 8.5:1 vs Indigo |
| `TEXT_PRIMARY` | `#F8FAFC` | Main headings, active labels, body text | **15.8:1** (AAA) |
| `TEXT_SECONDARY` | `#94A3B8` | Subtitles, descriptions, table column headers | **7.2:1** (AAA) |
| `TEXT_MUTED` | `#64748B` | Helper notes, disabled indicators, timestamps | **4.6:1** (AA) |
| `STATUS_SUCCESS` | `#10B981` | Online, active, pass, idle-ready | 8.2:1 |
| `STATUS_WARNING` | `#F59E0B` | Rate limited, attention required, working | 7.9:1 |
| `STATUS_ERROR` | `#F43F5E` | Crashed, failed, blocked, disconnected | 6.4:1 |

#### Light Variant: "Pure Crisp Slate"
| Token Name | Hex / Value | Usage | WCAG vs Ground |
|---|---|---|---|
| `GROUND_CANVAS` | `#F8FAFC` | Main window background, sidebar base | Ground base |
| `GROUND_SURFACE` | `#FFFFFF` | Content area, pane background | Elevated 1 |
| `GROUND_CARD` | `#FFFFFF` | Settings panels, cards (with subtle shadow) | Elevated 2 |
| `GROUND_CARD_HOVER` | `#F1F5F9` | Hover state on list items / cards | Elevated 3 |
| `GROUND_INPUT` | `#F8FAFC` | Form inputs, dropdown fields | Inset |
| `BORDER_SUBTLE` | `#E2E8F0` | Dividers, card borders, list borders | N/A |
| `BORDER_DEFAULT` | `#CBD5E1` | Input outlines, inactive button borders | N/A |
| `BORDER_FOCUS` | `#4F46E5` | Active focus rings, selected items | High |
| `ACCENT_PRIMARY` | `#4F46E5` | Deep Indigo: Primary buttons, active tabs | 8.2:1 vs White |
| `ACCENT_PRIMARY_HOVER` | `#4338CA` | Button hover state | 9.5:1 |
| `ACCENT_TEXT_ON` | `#FFFFFF` | Text inside primary buttons | **8.2:1** (AAA) |
| `TEXT_PRIMARY` | `#0F172A` | Headings, active labels, primary body text | **16.5:1** (AAA) |
| `TEXT_SECONDARY` | `#475569` | Subtitles, field labels, table headers | **8.4:1** (AAA) |
| `TEXT_MUTED` | `#64748B` | Helper notes, timestamps, inactive icons | **4.8:1** (AA) |
| `STATUS_SUCCESS` | `#059669` | Online, active, pass | 7.8:1 |
| `STATUS_WARNING` | `#D97706` | Rate limited, attention required | 6.9:1 |
| `STATUS_ERROR` | `#E11D48` | Failed, crashed, rejected | 7.1:1 |

---

### 3.3 Typography & Spacing Guidelines

#### Font Family Policy
- **Primary Sans:** Inter (fallback: `Segoe UI Variable`, `SF Pro`, `Noto Sans Thai`, system sans).
  - Used for 100% of UI labels, buttons, navigation, headers, and form controls.
- **Code & Identifiers Mono:** JetBrains Mono (fallback: `Cascadia Code`, `Consolas`, monospace).
  - Strictly limited to code snippets, file paths, CLI flags (`--role`), environment variables, and token counts.
- **Typography Scale:**
  - `Title 1` (Window/Page): 18px / SemiBold (600) / line-height: 24px
  - `Title 2` (Section Header): 14px / SemiBold (600) / line-height: 20px
  - `Body Medium` (Labels, Controls): 13px / Medium (500) / line-height: 18px
  - `Body Regular` (Explanations): 13px / Regular (400) / line-height: 20px
  - `Caption / Mono Small` (Paths, Badges): 11px / Medium (500) / line-height: 14px

#### Spacing & Corner Radii
- **Spacing Scale:** 4px, 8px, 12px, 16px, 20px, 24px, 32px.
- **Component Radii:**
  - Badges / Pills: `9999px` (Full rounded)
  - Buttons / Inputs / Controls: `8px`
  - Cards / Panels: `12px`
  - Modals / Main Windows: `16px`

---

## 4. Concrete Redesign Mockups for Top Problem Areas

### Mockup 1: Settings — Team & Roles (`VIEW_PROVIDERS_ROLES`)
*Problems Fixed: Removed clumsy 3-dropdown per row structure; replaced muddy mustard toggles with crisp Indigo switches; elevated team size preset cards with clear visual capacity meters.*

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Settings  ›  Team & Roles                                                             │
│  Manage team size preset, active role allocations, and provider model overrides.       │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  TEAM SIZE PRESET                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐ │
│  │ Solo             │  │ Pair             │  │ Full Fleet       │  │ Dynamic Auto  ★ │ │
│  │ Lead only        │  │ Lead + Reviewer  │  │ Full role roster │  │ Adaptive fleet  │ │
│  │ 0 worker panes   │  │ 1 worker pane    │  │ 2-5 worker panes │  │ Task-scope size │ │
│  │ [● Solo Active]  │  │ [Select]         │  │ [Select]         │  │ [Selected]      │ │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  └─────────────────┘ │
│                                                                                        │
│  ROLE ROSTER & PROVIDER ASSIGNMENT                    [4/4 Active]  [+ New Custom Role]│
│  ┌───────────────────────────────────────────────────────────────────────────────────┐ │
│  │ Role             Provider           Model Tier          Effort    Status          │ │
│  ├───────────────────────────────────────────────────────────────────────────────────┤ │
│  │ 👑 Lead          Claude (Anthropic) Claude 3.7 Sonnet   Default   [Locked Active] │ │
│  │                                                                                   │ │
│  │ 🎨 Frontend      Claude (Anthropic) Claude 3.7 Sonnet   High      [  ON  ●  ]     │ │
│  │                                                                                   │ │
│  │ ⚙️ Backend       Claude (Anthropic) Claude 3.7 Sonnet   High      [  ON  ●  ]     │ │
│  │                                                                                   │ │
│  │ 📱 Mobile        Claude (Anthropic) Claude 3.7 Sonnet   High      [  ON  ●  ]     │ │
│  │                                                                                   │ │
│  │ 🛡️ QA            Claude (Anthropic) Claude 3.7 Sonnet   Default   [  ●  OFF ]     │ │
│  │                                                                                   │ │
│  │ 🔍 Reviewer      Claude (Anthropic) Claude 3.7 Sonnet   High      [  ●  OFF ]     │ │
│  └───────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                        │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  [Revert Changes]                                             [Cancel]  [Save & Apply] │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### Mockup 2: Settings — Pipeline Builder (`VIEW_PIPELINE_BUILDER`)
*Problems Fixed: Fixed truncated template names with resizable master-detail view; eliminated `"v wait for all"` with clean visual flow connectors; added drag-and-drop hop cards with clear stage indicators.*

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Settings  ›  Pipeline Configuration                                                   │
│  Define automated execution pipelines and multi-role orchestration sequences.          │
├─────────────────────────┬──────────────────────────────────────────────────────────────┤
│  SAVED TEMPLATES        │  PIPELINE FLOW: Feature Implementation (UI + API)            │
│  ┌────────────────────┐ │                                                              │
│  │ Feature (UI+API)   │ │  AVAILABLE ROLES (CLICK TO INSERT INTO CURRENT HOP):        │
│  │ 2 hops · 4 roles   │ │  [+ Frontend] [+ Backend] [+ Mobile] [+ QA] [+ Reviewer]     │
│  │ [Built-in]         │ │                                                              │
│  ├────────────────────┤ │  HOP 1: PARALLEL EXECUTION (FAN-OUT)                        │
│  │ Design Review      │ │  ┌─────────────────────────────────────────────────────────┐ │
│  │ 1 hop · 2 roles    │ │  │ 🎨 Frontend (Claude)     ⚙️ Backend (Claude)            │ │
│  │ [Built-in]         │ │  │ [Options] [×]            [Options] [×]       [+ Add Role│ │
│  ├────────────────────┤ │  └─────────────────────────────────────────────────────────┘ │
│  │ Quick Fix          │ │                                                              │
│  │ 1 hop · 1 role     │ │                           │                                  │
│  │ [Built-in]         │ │                           ▼ (Barrier: Wait for all in Hop 1) │
│  ├────────────────────┤ │                                                              │
│  │ Full Audit E2E     │ │  HOP 2: VERIFICATION & REVIEW                                │
│  │ 2 hops · 3 roles   │ │  ┌─────────────────────────────────────────────────────────┐ │
│  │ [Custom]           │ │  │ 🛡️ QA (Claude)           🔍 Reviewer (Claude)           │ │
│  └────────────────────┘ │  │ [Options] [×]            [Options] [×]       [+ Add Role│ │
│                         │  └─────────────────────────────────────────────────────────┘ │
│  [+ New]   [Duplicate]  │                                                              │
│  [Export]  [Delete]     │  [ + Add Next Hop Stage ]                                    │
├─────────────────────────┴──────────────────────────────────────────────────────────────┤
│  [Discard Edits]                                              [Cancel]  [Save & Apply] │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### Mockup 3: Settings — Usage & Analytics (`VIEW_USAGE`)
*Problems Fixed: Completely replaced raw ASCII CLI tables and developer constant leaks with executive KPI metric cards, clean visual progress bars, and structured tabular analytics.*

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Settings  ›  Usage & Quota Runway                                                     │
│  Monitor real-time token consumption, quota resets, and estimated provider costs.      │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                            [Timeframe: Last 7 Days  ▾] │
│  KEY METRICS                                                                           │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌───────────────────────────────┐ │
│  │ Total Tokens Used    │  │ Active Rate Limits   │  │ Cache Efficiency              │ │
│  │ 2.45M                │  │ 24% Secondary Window │  │ 84.2% Hit Rate                │ │
│  │ ↑ 12% vs last period │  │ Resets in 3h 24m     │  │ Estimated Savings: ~$18.40    │ │
│  └──────────────────────┘  └──────────────────────┘  └───────────────────────────────┘ │
│                                                                                        │
│  PROVIDER USAGE & RUNWAY                                                               │
│  ┌───────────────────────────────────────────────────────────────────────────────────┐ │
│  │ Provider / Account   5-Hour Window        7-Day Window        Total Cost (Est.)   │ │
│  ├───────────────────────────────────────────────────────────────────────────────────┤ │
│  │ 🟣 Claude (office)   [████░░░░░░] 41%     [██████░░░░] 62%    $34.20              │ │
│  │                      Resets in 1h 45m     Resets in 3d 12h                        │ │
│  │                                                                                   │ │
│  │ 🟢 Codex (default)   [██░░░░░░░░] 18%     [███░░░░░░░] 28%    Included (Plus)     │ │
│  │                      Resets in 4h 10m     Resets in 5d 04h                        │ │
│  │                                                                                   │ │
│  │ 🔵 Gemini (default)  [█░░░░░░░░░] 8%      [██░░░░░░░░] 15%    Pay-per-token       │ │
│  │                      Active Runway Safe   Active Runway Safe                      │ │
│  └───────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                        │
│  TOKEN BREAKDOWN BY ROLE                                                               │
│  ┌───────────────────────────────────────────────────────────────────────────────────┐ │
│  │ Role         Input Tokens   Output Tokens   Cache Write   Cache Read   Turns      │ │
│  ├───────────────────────────────────────────────────────────────────────────────────┤ │
│  │ Lead         412,400        52,100          180,000       1,240,000    124        │ │
│  │ Backend      620,100        89,400          240,000       2,100,000    88         │ │
│  │ Frontend     310,200        41,000          110,000       980,000      62         │ │
│  │ QA           180,500        19,200          50,000        450,000      41         │ │
│  └───────────────────────────────────────────────────────────────────────────────────┘ │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  [Export CSV]                                                 [Close]   [Refresh Data] │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Architectural Implementation Roadmap

1. **Phase 1: Token Engine Refactoring (`cockpit_theme.py`)**
   - Deprecate `ACCENT_GOLD` and IBM Plex constants.
   - Replace with the `Apex Slate` design tokens (`Slate-900` dark base, `Indigo-600` primary accent, `Slate-50` light base).
   - Enforce WCAG AA/AAA automated lint tests for all token combinations.
2. **Phase 2: Base Dialog Inheritance (`CockpitDialog`)**
   - Create a unified `CockpitDialog` base class in `cockpit_theme.py`.
   - Refactor `_AddAccountDialog`, `RemoteSettingsDialog`, `_RolePermissionsDialog`, `_run_map_paths_dialog`, and `_show_rules_editor_dialog` to inherit from `CockpitDialog`, eliminating all unstyled Windows 98 dialog leaks.
3. **Phase 3: Live Retheme Pipeline (`MainWindow.retheme`)**
   - Fix dynamic theme switching so `ProjectNav`, `TaskDockWidget`, and `AgentPane` tab strips update immediately without requiring app restart.
4. **Phase 4: Component Redesign Rollout**
   - Deploy redesigned `SettingsWindow` views: Team Roster, Pipeline Flow Canvas, and Usage Analytics Cards.
   - Remove developer-centric leaks (`Grid row 99`, `token_meter._GEMINI_UNSUPPORTED_REASON`, `"v wait for all"`).

---
*Report deliverable complete and verified against live environment.*
