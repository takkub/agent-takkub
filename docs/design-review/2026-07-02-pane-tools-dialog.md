# Design & UX Review: Cockpit Tools Dialog (PyQt6)

**Date:** July 2, 2026  
**Focus File:** [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py)  
**Theme Context:** Cockpit Zinc Dark (Background `#09090b`, Surface `#18181b`/`#27272a`, Text zinc-ramp, Accent `#2563eb`)

---

## 1. Summary of Critique

While the recent addition of QSS stylesheets has significantly elevated the Tools Dialog from a default, unstyled Windows widget to a modern dark-mode dialog, several design-to-implementation gaps and PyQt-specific layout quirks limit its usability and visual polish:

*   **Affordance & Usability (High Impact):** The matrix checkboxes are nested inside centered cell container widgets. This restricts the clickable toggle area to a tiny 16x16px bounding box, while clicking the surrounding cell selects the whole column instead of toggling the checkbox.
*   **Visual Logic Bug (High Impact):** Forced-denylisted plugins are displayed as *checked* but *disabled*, which visually indicates they are active, even though they are completely blocked.
*   **Visual Hierarchy & Proximity (Medium Impact):** Header elements (Title and Subtitle) are spaced equally to the content tabs, violating basic proximity principles. Tab selected state background colors clash with the underlying tab pane.
*   **Accessibility (Low-Medium Impact):** Helper and status text use `#71717a` (zinc-500) which fails the WCAG AA 4.5:1 contrast ratio against the `#09090b` background.

---

## 2. Actionable Recommendations (Sorted by Impact)

### 1. [BUG/UX] Correct Forced Denylist Checkbox State
*   **Where to modify:** `_fill_matrix_table` method (lines 424-426 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L424-L426))
*   **Why:** Currently, denylisted plugins like `security-guidance` and `remember` are set to `setChecked(True)` and disabled. This displays a filled blue/gray checkmark. To the user, a checkmark implies "active/enabled". If a plugin is blocked on the denylist ("ปิดเสมอ"), it must be **unchecked** (`setChecked(False)`).
*   **Actionable Change:**
    ```python
    # Change:
    if item in disabled_items:
        box.setChecked(True)
        box.setEnabled(False)
    
    # To:
    if item in disabled_items:
        box.setChecked(False)  # Keep unchecked to signify disabled/inactive status
        box.setEnabled(False)
    ```

---

### 2. [UX] Expand Checkbox Click Target to the Entire Table Cell
*   **Where to modify:** `_fill_matrix_table` method (lines 430-434 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L430-L434))
*   **Why:** Nesting the `QCheckBox` inside a `QWidget` centered container swallows mouse events on the outer cell. A user clicking slightly outside the 16x16px indicator box fails to toggle the setting and accidentally selects the whole column. 
*   **Actionable Change:** Connect the QTableWidget's `cellClicked(row, col)` signal to a toggle handler:
    ```python
    # In __init__ or _fill_matrix_table:
    table.cellClicked.connect(self._on_cell_clicked)

    # Handler method:
    def _on_cell_clicked(self, row: int, col: int):
        # Retrieve the checkbox from the layout
        widget = self._mcp_table.cellWidget(row, col) # (or self._plugin_table)
        if widget:
            box = widget.findChild(QCheckBox)
            if box and box.isEnabled():
                box.setChecked(not box.isChecked())
    ```

---

### 3. [Visual] Unify Selected Tab and Tab Pane Backgrounds
*   **Where to modify:** `_DIALOG_QSS` (lines 71-73 and 79-82 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L71-L82))
*   **Why:** `QTabWidget::pane` background is `#0c0c0e`, but `QTabBar::tab:selected` is `#18181b`. The selected tab has `border-bottom-color: #18181b` which bleeds into the darker `#0c0c0e` pane, breaking the visual illusion of a connected physical card.
*   **Actionable Change:** Make both share the same background color (`#18181b` or `#0c0c0e`). Using `#18181b` creates a cleaner, more premium surface appearance:
    ```css
    QTabWidget::pane {
        border: 1px solid #27272a; border-radius: 8px; top: -1px; background: #18181b; /* Match selected tab */
    }
    QTabBar::tab:selected {
        background: #18181b; color: #fafafa;
        border: 1px solid #27272a; border-bottom-color: #18181b;
    }
    ```

---

### 4. [Visual] Eliminate Grid Clutter in QTableWidget
*   **Where to modify:** `_fill_matrix_table` method (line 395 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L395)) & `_DIALOG_QSS`
*   **Why:** Explicit vertical and horizontal gridlines (`setShowGrid(True)`) combined with alternating row colors look busy. Modern clean UI design leans on alternating backgrounds or simple horizontal dividers instead of grid boxes.
*   **Actionable Change:** Turn off grid lines and style the table headers to act as the primary dividers:
    ```python
    # In _fill_matrix_table:
    table.setShowGrid(False) # Turn off grid lines
    ```
    And add simple bottom borders to table cells in `_DIALOG_QSS`:
    ```css
    QTableWidget::item { 
        border-bottom: 1px solid #1f1f23; /* Subtle horizontal separator */
    }
    ```

---

### 5. [UX] Proactive State Management for the "Remove MCP" Button
*   **Where to modify:** `_build_mcp_tab` (line 297) & button click handling in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L297)
*   **Why:** The `_btn_remove_mcp` button is always active. Clicking it without a selection shows an annoying "เลือกคอลัมน์ MCP ที่ต้องการลบก่อน" warning dialog.
*   **Actionable Change:** Disable the button by default and enable it dynamically when a column header/cell is selected.
    ```python
    # In _build_mcp_tab:
    self._btn_remove_mcp.setEnabled(False)
    self._mcp_table.selectionModel().selectionChanged.connect(self._on_mcp_selection_changed)

    # Toggle state:
    def _on_mcp_selection_changed(self):
        has_selection = len(self._mcp_table.selectionModel().selectedColumns()) > 0
        self._btn_remove_mcp.setEnabled(has_selection)
    ```

---

### 6. [Accessibility] Elevate Contrast of Subtitle and Helper Text
*   **Where to modify:** `_DIALOG_QSS` (line 69 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L69))
*   **Why:** `QLabel#toolsSubtitle` uses `#71717a` (zinc-500) on a `#09090b` (zinc-950) background. This gives a contrast ratio of **4.0:1**, failing WCAG AA (which requires 4.5:1 for small text). This text contains critical instruction details.
*   **Actionable Change:** Brighten the secondary label text to `#a1a1aa` (zinc-400), which yields a contrast ratio of **6.5:1**:
    ```css
    QLabel#toolsSubtitle { color: #a1a1aa; font-size: 11px; }
    ```

---

### 7. [Visual] Tighten Header Hierarchy (Proximity Principle)
*   **Where to modify:** `__init__` (lines 242-259 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L242-L259))
*   **Why:** The dialog title and subtitle are added directly to the main layout with a spacing of `12px`. This makes the gap between title and subtitle identical to the gap between subtitle and the tab widget, making them feel like separate blocks rather than a cohesive header group.
*   **Actionable Change:** Wrap the title and subtitle in a sub-layout with tight spacing (`4px`) to visually group them:
    ```python
    header_layout = QVBoxLayout()
    header_layout.setSpacing(4)  # Group title and subtitle closer
    
    title = QLabel("🔧 Pane Tools", self)
    title.setObjectName("toolsTitle")
    subtitle = QLabel("เปิด/ปิด MCP และ plugin ต่อ role — ติ๊กช่องแล้วกด Save...", self)
    subtitle.setObjectName("toolsSubtitle")
    subtitle.setWordWrap(True)
    
    header_layout.addWidget(title)
    header_layout.addWidget(subtitle)
    
    layout.addLayout(header_layout)
    ```

---

### 8. [Visual] Button Size Affordance
*   **Where to modify:** `_DIALOG_QSS` (lines 125-128 in [pane_tools_dialog.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/pane_tools_dialog.py#L125-L128))
*   **Why:** Padding of `6px 14px` on `12px` font results in buttons that are ~28px high. This feels cramped and like a legacy desktop application. Modern desktop applications targeting pointing devices are much more comfortable with 32px to 36px high buttons.
*   **Actionable Change:** Adjust the padding to `8px 16px` or `10px 18px` to give buttons a more prominent and modern visual weight:
    ```css
    QPushButton {
        background: #18181b; color: #d4d4d8; border: 1px solid #27272a;
        border-radius: 6px; padding: 8px 16px; font-weight: 500;
    }
    ```
