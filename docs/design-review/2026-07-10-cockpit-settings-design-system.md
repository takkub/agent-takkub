# Takkub Cockpit — Settings Window Design System
Source: `Takkub Cockpit.dc.html` (claude.ai design, user's canonical design) — extracted 2026-07-10
**นี่คือ design จริงที่ต้อง implement** — ก่อนหน้านี้ทำ teal/indigo ผิด · ของจริง = **gold #E3B341 + IBM Plex**

## 🎨 Tokens (exact hex)
**Fonts:** `IBM Plex Sans` (400/500/600/700) UI · `IBM Plex Mono` (400/500/600) labels/code/badges
→ PyQt: bundle IBM Plex `.ttf` (QFontDatabase.addApplicationFont) หรือ fallback `Segoe UI`/`Cascadia Mono` + flag

**Grounds:** body `#050608` · window `#15171c` · titlebar `#0f1114` · status-strip gradient `#181b21→#141519` · sidebar `#101216` · panel `#181b21` · panel-alt `#191c22` · input `#1c1f26` · select `#232732`
**Borders:** hairline `rgba(255,255,255,0.06)` · med `0.09` · strong `0.12`/`0.14` · radius 8–14px
**Accent GOLD:** solid `#E3B341` · **gradient button** `linear-gradient(180deg,#EEC25A,#E3B341)` text `#241a00` weight700 · glow `0 6px 18px -6px rgba(227,179,65,0.6)` · soft-chip bg `rgba(227,179,65,0.12)` border `rgba(227,179,65,0.35)` text `#EccB6a`
**Text:** primary `#f2f3f5`/`#e9ebef` · secondary `#c7ccd4`/`#cfd3da` · muted `#7b828f`/`#828a95` · faint `#5b626e`/`#6b7280`
**Role colors:** lead `#E3B341` · frontend `#34B7AC` · backend `#4E86F7` · mobile `#A472F0` · devops `#43B562` · qa `#E39A3C` · reviewer `#F26D6D` · critic `#F0619A` · designer `#C77DF0` · analyst `#45C4D6` · security `#E0574F` · docs `#8FA3B8`
**Provider substitute badge:** `→ Claude` text `#E9A876` border `rgba(217,119,87,0.4)`
**hop parallel chip:** purple `rgba(164,114,240,.14)` border `.3` text `#c39cf5`

## 🏗️ Structure (1320×848 window)
1. **Titlebar 38px** `#0f1114` — gold square + "takkub cockpit — settings" (mono) · 3 dots ขวา
2. **Status strip 56px** — "takkub COCKPIT" + active-template gold chip + role chips (dot+label) · ขวา: provider dots + version
3. **Body** = **sidebar 236px** `#101216` + **content** `#15171c`
   - sidebar: section "PIPELINE" (nav buttons: 5px color bar + label + badge) · section "POLICY" (nav) · spacer · **"+ New Role"** button (bottom)
   - content: header (title 20px + sub 13px) → scroll region (active view) → **footer 60px** `#101216`: "↺ Reset to default" ซ้าย · "unsaved changes" gold dot + Cancel + **"Save & Apply"** gold gradient ขวา

## 📑 Views (7 — sidebar สลับ)
| View | มีอยู่แล้วที่ | หมายเหตุ |
|---|---|---|
| **Pipeline Builder** | pipeline_dialog.py | hops (HOP N + parallel chip) · role palette · +Add hop dashed · connector "↓ wait for all" |
| **Templates** | pipeline_dialog.py | list ซ้าย (BUILT-IN gold badge) + detail ขวา (hops + Edit/Duplicate/Delete) |
| **Providers & Roles** | pane_tools_dialog + provider_config | info banner (→Claude) · provider rows + toggle · role rows (dot+label+desc+provider select+toggle · lead locked) |
| **MCP Matrix** | pane_tools_dialog | grid role×server (playwright/chrome-devtools/context7) cell toggle ✓ |
| **Plugins Matrix** | pane_tools_dialog | grid role×plugin (5) · denylist banner |
| **Skill Catalog** | pane_tools_dialog + skill_audit | list ซ้าย + mono text ขวา · "✓ won't overlap" |
| **New Role** | pane_tools_dialog (custom_roles) | form: Name/Label/Grid(col select+row)/Accent swatches/Default MCP+Plugins/Instructions textarea/+Create Role gold |

## Components
- **nav button**: 5px rounded color-bar + flex label + optional mono count badge · active = brighter bg
- **toggle**: track (rounded) + knob · on=gold
- **matrix cell**: small button ✓/blank tinted
- **role chip**: colored dot + label (weight600 in role color) · optional ✕ remove
- **gold primary button**: gradient + `#241a00` text + glow · secondary = transparent + border

## Implement notes
- PyQt6 — QSS ต่อ widget · matrix = QGridLayout · toggle = custom QAbstractButton หรือ styled checkbox · scroll region = QScrollArea/QStackedWidget ต่อ view
- unify pipeline_dialog (Pipeline/Templates) + pane_tools_dialog (Policy views) เข้า window เดียว sidebar สลับ (หรือคง 2 dialog แต่ reskin ให้ token ตรง — ตัดสินใจตอน plan)
- IBM Plex font: bundle หรือ fallback + flag (cross-platform)
- Task List dock = คนละอัน ไม่อยู่ใน design นี้ (คงงานเดิม)
