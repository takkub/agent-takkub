# PyQt6 Task Dock Responsive & Visual Polish Specification

This design document outlines the technical specification and code recipes to address responsiveness, word-wrapping, and visual consistency inside the **Task List dock (`TaskDockWidget`)** and **Team & Roles dialog (`PaneToolsDialog`)**.

---

## 1. Word-Wrap in QTreeWidget (Goal / Feature / Task Rows)

### The Problem
Currently, the `QTreeWidget` disables elision (`ElideNone`) and relies on `ScrollBarAsNeeded`. When project/goal names are long (e.g. `'จบ roadmap รอบสุดท้าย: A6 role/skill manager · A8 task-tree dock · B2 per-option picker'`), the columns resize to fit the longest line, forcing a horizontal scrollbar. When the dock is narrow, this cuts off visibility and ruins responsiveness.

### Technical Recipes & Trade-offs

We have two primary methods to enable word-wrap inside standard `QTreeWidget` rows (non-item widgets like Goal, Feature, and Task rows):

#### Method A: Built-in `setWordWrap(True)` + Layout Invalidation (Recommended)
This leverages the native Qt layout engine. However, `QTreeView` caches row heights for performance and will **not** automatically recalculate row heights when the column width changes (i.e. when the dock is resized), leading to overlapping text. We must force-invalidate the geometries cache on column/viewport resize.

*   **Implementation Recipe**:
    1. Set the tree widget properties to wrap text and disable horizontal scrollbars.
    2. Set the header section resize mode to `Stretch` so the single column stretches/shrinks dynamically to match the dock width.
    3. Connect the header's `sectionResized` signal to `updateGeometries()` to clear the height cache and force recalculations during resize.

```python
# In TaskDockWidget.__init__
self._tree.setWordWrap(True)
self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

# Configure the header to stretch the main column
header = self._tree.header()
header.setStretchLastSection(True)
header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

# Force row height recalculations when the column width changes
header.sectionResized.connect(lambda: self._tree.updateGeometries())
```

*   **Trade-offs**:
    *   **Pros**: Extremely simple (only 2 lines of code), lightweight, and respects all standard stylesheets, selections, and item decorations (status icons).
    *   **Cons**: Default wrapping utilizes standard QTextOption wrapping, which might wrap slightly early if the indentation depth is large (as the indent width reduces available text space, but the size-hint calculation sometimes ignores it).

#### Method B: Custom `QStyledItemDelegate` with `QTextDocument`
For complete control over text wrapping, spacing, and HTML/markdown tags within the rows, a custom delegate can calculate size hints and render text manually.

*   **Implementation Recipe**:
    ```python
    from PyQt6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle
    from PyQt6.QtGui import QTextDocument
    from PyQt6.QtCore import QSize

    class TreeWordWrapDelegate(QStyledItemDelegate):
        def paint(self, painter, option, index):
            # 1. Draw standard background, selection, and icons
            opts = QStyleOptionViewItem(option)
            self.initStyleOption(opts, index)
            opts.text = ""  # Hide text so super doesn't draw it
            
            painter.save()
            self.parent().style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opts, painter)
            
            # 2. Draw word-wrapped text with QTextDocument
            text_rect = self.parent().style().subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, opts, None
            )
            
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(option.text)
            doc.setTextWidth(text_rect.width())
            
            painter.translate(text_rect.topLeft())
            painter.setClipRect(text_rect.translated(-text_rect.topLeft()))
            doc.drawContents(painter)
            painter.restore()

        def sizeHint(self, option, index):
            opts = QStyleOptionViewItem(option)
            self.initStyleOption(opts, index)
            
            # Calculate dynamic width available for text based on column width and indentation
            column_width = self.parent().columnWidth(index.column())
            indent = self.parent().indentation() * self._get_depth(index)
            icon_w = option.decorationSize.width() + 8 if not option.icon.isNull() else 0
            text_w = max(50, column_width - indent - icon_w - 16)
            
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            doc.setHtml(option.text)
            doc.setTextWidth(text_w)
            
            height = int(doc.size().height()) + 8  # vertical padding
            return QSize(column_width, max(height, option.decorationSize.height() + 8))

        def _get_depth(self, index):
            depth = 0
            parent = index.parent()
            while parent.isValid():
                depth += 1
                parent = parent.parent()
            return depth
    ```

*   **Trade-offs**:
    *   **Pros**: Flawless, pixel-perfect text wrapping. Exact height calculations matching actual font metrics and indent depths. Supports HTML formatting (e.g., coloring the role vs. summary).
    *   **Cons**: High implementation complexity. Requires manual text bounding-box mathematics and manual state handling for selection highlights/focus rects.

---

## 2. Project Card (Item Widget) Word-Wrap & Responsiveness

### The Problem
Project cards are set using `setItemWidget()`. Because these widgets are overlaid on top of tree items, **`QTreeWidget` does not query the widget's layout size to determine row heights**. If a card's layout wraps and increases in height, it will overlap with the row below. 
Additionally, at narrow dock widths (e.g. 120px - 240px), the progress bar, badge, and open buttons leave no space for the project name.

### Technical Recipes

We must refactor the local widget construction in `_mount_project_row` to a dedicated `ProjectCardWidget(QWidget)` subclass. This subclass will:
1.  Manage the visibility of secondary controls depending on widget width (`resizeEvent`).
2.  Enable word wrap on the project name `QLabel`.
3.  Propagate its dynamically calculated height back to the `QTreeWidgetItem` size hint, triggering a tree repaint.

#### Code Recipe: `ProjectCardWidget` Subclass

```python
class ProjectCardWidget(QWidget):
    def __init__(self, item: QTreeWidgetItem, tree: QTreeWidget, project: str, state: dict, parent: QWidget = None):
        super().__init__(parent)
        self.item = item
        self.tree = tree
        self.project = project
        self.setObjectName("taskProjectCard")
        
        self.setStyleSheet(
            "#taskProjectCard {"
            " background: transparent;"  # Essential for visual polish (see Section 3)
            " border: 1px solid #27272a;"
            " border-radius: 10px;"
            "}"
        )
        
        self.row_layout = QHBoxLayout(self)
        self.row_layout.setContentsMargins(8, 6, 8, 6)
        self.row_layout.setSpacing(8)
        
        # Chevron
        self.chevron = QToolButton()
        self.chevron.setText("▸")
        self.chevron.setAutoRaise(True)
        self.chevron.setFixedSize(16, 16)
        self.chevron.setStyleSheet(
            "QToolButton { color: #71717a; background: transparent; border: none; font-size: 10px; font-weight: 700; }"
            "QToolButton:hover { color: #d4d4d8; }"
        )
        self.chevron.clicked.connect(lambda: self.item.setExpanded(not self.item.isExpanded()))
        self.row_layout.addWidget(self.chevron)
        
        # Initials Avatar
        self.avatar = QLabel(_initials(project))
        self.avatar.setFixedSize(24, 24)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setStyleSheet(
            f"background: {_avatar_color(project)}; color: #ffffff; font-size: 10px;"
            f" font-weight: 800; border-radius: 12px;"
        )
        self.row_layout.addWidget(self.avatar)
        
        # Project Name (Word Wrapped)
        self.name_label = QLabel(project)
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("color: #e4e4e7; font-size: 13px; font-weight: 700;")
        self.row_layout.addWidget(self.name_label, 1)
        
        # Progress Badge & Mini Bar (Grouped in a sub-widget for easy toggling)
        self.progress_container = QWidget()
        prog_lay = QHBoxLayout(self.progress_container)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        prog_lay.setSpacing(6)
        
        done, total = project_progress(state)
        ratio = (done / total) if total else 0.0
        color = usage_color(ratio) if total else "#52525b"
        
        self.badge = QLabel(f"{done}/{total}")
        self.badge.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 700;")
        prog_lay.addWidget(self.badge)
        
        self.bar = QProgressBar()
        self.bar.setObjectName("taskMiniBar")
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(48)
        self.bar.setFixedHeight(6)
        self.bar.setStyleSheet(
            "QProgressBar#taskMiniBar { background: #27272a; border: none; border-radius: 3px; }"
            f"QProgressBar#taskMiniBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )
        prog_lay.addWidget(self.bar)
        self.row_layout.addWidget(self.progress_container)
        
        # Open INDEX.md Button
        self.open_btn = QToolButton()
        self.open_btn.setText("↗")
        self.open_btn.setFixedSize(20, 20)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.setStyleSheet(
            "QToolButton { color: #71717a; background: transparent; border: none; border-radius: 6px; }"
            "QToolButton:hover { background: #27272a; color: #d4d4d8; }"
        )
        self.open_btn.clicked.connect(lambda: TaskDockWidget._open_index(project))
        self.row_layout.addWidget(self.open_btn)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = event.size().width()
        
        # 1. Responsive layout adaptations (Width-based hiding)
        # If the dock is compressed, hide progress graphics and open button to make room for text.
        is_narrow = width < 230
        self.progress_container.setVisible(not is_narrow)
        self.open_btn.setVisible(not is_narrow)
        
        if is_narrow:
            self.row_layout.setContentsMargins(4, 4, 4, 4)
            self.row_layout.setSpacing(4)
        else:
            self.row_layout.setContentsMargins(8, 6, 8, 6)
            self.row_layout.setSpacing(8)
            
        # 2. Dynamic height propagation to the Tree View
        # Force the layout to activate to calculate the actual height needed under the current width boundary.
        self.row_layout.activate()
        needed_height = self.row_layout.sizeHint().height()
        
        # Update the QTreeWidgetItem sizeHint so QTreeWidget allocates enough height for this row
        current_hint = self.item.sizeHint(0)
        if current_hint.height() != needed_height:
            self.item.setSizeHint(0, QSize(0, needed_height))
            
            # Defer updating tree geometries via a single-shot timer to avoid recursion crash during resizeEvent
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self.tree.updateGeometries)
```

---

## 3. Visual Polish & Design Language Parity

To match the clean look of the left PROJECTS sidebar (`project_nav.py`), we must resolve card overlay issues.

### The Problem: Opaque Overlay Blocking Hover/Selection State
Currently, `taskProjectCard` is styled with `background: #1a1a1e;`. This opaque fill sits on top of the tree item and hides the native `QTreeWidget#taskTree::item:hover` (`#18181b`) and `:selected` (`#1e1b2e`) rounded background colors, making the hover states look broken and static.

### The Solution
1.  **Set card background to transparent**: Make the card background `transparent` in stylesheet. Let the underlying `QTreeWidget::item` styles render the hover/selection states underneath, matching `_ProjectRow` behavior:
    ```css
    #taskProjectCard {
        background: transparent;
        border: 1px solid #27272a;
        border-radius: 10px;
    }
    ```
2.  **Card Border Highlights**: Highlight the card border when selected by subclassing selection behaviors or dynamically checking state. Since QTreeWidget selection changes trigger redraws, we can use QSS properties or catch selection signals to recolor the border (e.g. to accent indigo `#6366f1` when active).

---

## 4. Team & Roles Dialog (`PaneToolsDialog`) Responsiveness

### The Problem
The guided custom-role step-2 tools section uses a static 2-column grid layout for tool cards (`mcp_grid` and `plugin_grid`). When the dialog is scaled down, cards overlap or clip names.

### The Solution: Dynamic Grid Adjustment
Intercept `resizeEvent` on `PaneToolsDialog` to adapt the grid column count between 1 column (small layouts) and 2 columns (wider layouts).

```python
class PaneToolsDialog(QDialog):
    # ...
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_grids()
        
    def _adjust_grids(self):
        width = self.width()
        cols = 1 if width < 600 else 2
        
        self._regrid_cards(self._mcp_table_layout or self.mcp_grid, self._nr_mcp_boxes, cols)
        self._regrid_cards(self._plugin_table_layout or self.plugin_grid, self._nr_plugin_boxes, cols)

    def _regrid_cards(self, grid_layout, boxes_dict, target_cols):
        # Temporarily detach widgets
        widgets = []
        for name, box in boxes_dict.items():
            card = box.parentWidget()  # The _ToolCard wrapper
            if card:
                grid_layout.removeWidget(card)
                widgets.append(card)
                
        # Re-insert into grid with target columns
        for idx, card in enumerate(widgets):
            row = idx // target_cols
            col = idx % target_cols
            grid_layout.addWidget(card, row, col)
```

---

## 5. Implementation Summary Checklist for Backend

1.  [ ] **Task List Tree Config**:
    *   Set `setWordWrap(True)` and scrollbar behavior on `taskTree`.
    *   Connect header `sectionResized` to `self._tree.updateGeometries()`.
2.  [ ] **Project Row Item Widget**:
    *   Implement `ProjectCardWidget` inheriting `QWidget`.
    *   Use `setWordWrap(True)` on the project name.
    *   Propagate the card widget height to the tree item's `setSizeHint(0, QSize(0, H))` safely inside `resizeEvent`.
    *   Dynamically hide `progress_container` and `open_btn` when `width < 230`.
3.  [ ] **QSS / Aesthetics**:
    *   Change `#taskProjectCard` background to `transparent`.
    *   Ensure margins/padding match `project_nav.py` guidelines.
4.  [ ] **Dialog Cards**:
    *   Implement dynamic column rearrangement on `PaneToolsDialog.resizeEvent`.
