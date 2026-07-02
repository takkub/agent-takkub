# Codex Cross-Check: Pane Tools Dialog

Date: 2026-07-02
Focus: `src/agent_takkub/pane_tools_dialog.py`
Scope: design blind spots in `_DIALOG_QSS`, `_fill_matrix_table`, `__init__`, and `_on_remove_mcp_clicked`

## Findings, Sorted By Impact

1. Add a real checked-state glyph, not only a solid blue square.
   - Where: `_DIALOG_QSS`, checkbox indicator rules around lines 109-117.
   - Risk: `QCheckBox::indicator:checked` becomes a filled `#2563eb` square with no checkmark. In a dense role x item matrix, that can read as "selected cell" or "active highlight", not necessarily "enabled policy". It also fails for users who rely on shape more than color.
   - Concrete fix: use a checkmark asset/icon for `QCheckBox::indicator:checked`, or draw a tiny white tick via a Qt-supported indicator image. If avoiding assets, make checked state visibly different by adding a white inner mark or using text/icon in the cell. Keep the blue fill as accent, but do not make color the only state cue.

2. Replace the remove-MCP "select a column first" workflow with per-column actions.
   - Where: `_build_mcp_tab` button setup around lines 297-300 and `_on_remove_mcp_clicked` around lines 483-488.
   - Risk: the current flow depends on users discovering that horizontal header clicks select columns. The tooltip helps only after hovering the button, but the primary table affordance is checkbox toggling, not column selection. This makes destructive MCP removal feel hidden and error-prone.
   - Concrete fix: put a small delete/trash action in each MCP header or show a context menu on header right-click with "Remove MCP". A safer variant is selecting a column on header click and showing an inline header affordance plus enabling the remove button. The best UX is a visible per-column action because it binds the destructive command to the MCP name.

3. Change horizontal sizing strategy for 10+ MCP/plugin columns.
   - Where: `_fill_matrix_table`, `hheader.setMinimumSectionSize(104)` and `Stretch` mode around lines 407-416.
   - Risk: `Stretch` makes the matrix look balanced with a few columns, but with 10+ columns it either compresses labels until elided or depends on a minimum-size overflow that may not produce the expected readable header experience on every platform. Long MCP package names will become indistinguishable.
   - Concrete fix: use `ResizeToContents` or `Interactive` with a sensible default width, keep horizontal scrolling explicit, and set `horizontalHeaderItem(col).setToolTip(item)` for full names. For many columns, consider rotated/vertical header text only if names are short; for real MCP names, tooltip + horizontal scroll is more reliable.

4. Add an empty state for zero MCPs or zero plugins.
   - Where: `_reload_mcp_table`, `_reload_plugin_table`, and `_fill_matrix_table` before/after table setup.
   - Risk: when `items` is empty, the dialog shows a blank table with role headers but no actionable explanation. That can look like a rendering bug or failed load, especially for plugins where missing registry is a normal state.
   - Concrete fix: show a centered empty-state label in the tab body: for MCP, "No MCP servers configured yet" with the Add MCP button still visible; for Plugins, "No marketplace plugins found in installed_plugins.json". If preserving the table, hide it while empty and show the label instead.

5. Make role rows easier to scan before adding decorative role icons.
   - Where: vertical header styling in `_DIALOG_QSS` lines 94-103 and `setVerticalHeaderLabels(list(ROLES))` around line 389.
   - Risk: role names are readable, but every row has the same visual weight. In a policy matrix, row identity is the anchor for decisions; users will scan across from a role to several checkboxes. Icons could help, but generic icons per role can add noise and create false hierarchy.
   - Concrete fix: first improve labels to title case or display names (`Lead`, `Frontend`, `Backend`, etc.), make the vertical header width fit the longest label without truncation, and keep alternating rows. Add color only as a thin left stripe for broad category groups if there is a stable role taxonomy; avoid one unique color per role.

6. Distinguish checkbox toggling from table/column selection.
   - Where: `_fill_matrix_table`, selection setup around lines 391-400 and checkbox containers around lines 430-434.
   - Risk: the table has two competing interaction models: cell checkboxes for policy edits and column selection for removal. Because checkbox widgets are small and centered, clicks around them select columns rather than toggling policy. This makes the remove workflow more discoverable only by making normal editing more fragile.
   - Concrete fix: let the entire cell toggle the checkbox, and reserve column selection for header clicks only. If per-column delete actions are added, remove table column selection entirely for normal cells.

7. Treat forced-denylisted plugins as a separate visual state.
   - Where: disabled item handling around lines 424-427 and disabled checked style around line 116.
   - Risk: currently forced plugins are checked and disabled, which says "enabled but locked" even though the copy says they are blocked. A solid disabled blue square compounds the no-checkmark issue.
   - Concrete fix: render forced-denylisted plugins unchecked and disabled, or replace the checkbox with a locked/blocked glyph plus tooltip. If the product meaning is "present but policy cannot enable it", the state should be "blocked", not "checked".

## Net Recommendation

Prioritize interaction semantics before polish: add a real checked glyph, remove the hidden column-selection dependency for deletion, and switch from stretch-to-fit columns to scrollable/readable headers. Empty states and role label polish are smaller changes, but they prevent the dialog from looking broken in fresh installs or sparse configurations.
