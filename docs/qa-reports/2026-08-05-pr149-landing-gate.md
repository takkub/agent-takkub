# QA batch gate — PR #149 landing (terminal input path)

**Date:** 2026-08-05
**Scope:** `terminal.html` key handling + `ProviderSpec.multiline_newline_seq` + `terminal_widget.py` focus handlers
**Verdict:** PASS — no blockers found

## 1. Full suite

| Check | Result |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | exit 0, 0 failures (only expected `s` skips, no `F`) |
| `ruff check src tests` | No issues found |
| `ruff format --check src tests` | 373 files already formatted |
| `.venv/Scripts/lint-imports` | 23/23 contracts kept, 0 broken |

## 2. Structural checks (read-only, no GUI)

### test_terminal_widget.py — AST regression test for `__init__` splice bug
Read `TestTerminalWidgetInitStructure` directly (not just executed):
- `test_init_assigns_attributes_past_the_bridge_setup` walks the AST of `TerminalWidget.__init__`
  and asserts `self._page_ready`, `self._utf8_decoder`, `self._input_locked`, `self._pending_writes`
  appear as `Store`-context `ast.Attribute` nodes **descending from the `__init__` FunctionDef node
  itself**. If `focusInEvent`/`mousePressEvent` were ever spliced back into the middle of `__init__`
  (the PR #149 review blocker #1 regression), everything after the splice reparents under the
  sibling method's body instead of `__init__`'s — these attrs would silently disappear from the
  walked set and the assertion would go red.
- `test_focus_and_mousepress_handlers_are_not_nested_in_init` separately asserts `focusInEvent` and
  `mousePressEvent` are not nested `FunctionDef`s inside `__init__`.
- Confirmed structurally sound — this is a real AST-shape check, not an execution smoke test, and
  would correctly catch a re-introduced splice.
- Cross-checked against current `terminal_widget.py`: all four attrs (`_utf8_decoder` L303,
  `_page_ready` L307, `_input_locked` L316, `_pending_writes` init) live inside `__init__` (~L286-375,
  ending at `self._view.load(...)` L375), confirming the test currently passes for the right reason.

### terminal.html — Enter/IME/Mac key handling
- `grep` for `ev.ctrlKey || ev.metaKey` near the Enter intercept: **zero matches**. The Enter
  intercept gate at L161 is `isEnter && (ev.shiftKey || ev.altKey)` only — no ctrl/meta combo blocks
  it.
- `isComposing` guard present and wired through compositionstart/beforeinput/input/compositionend
  (L128, 184, 195, 224, 228, 239, 244) — IME composition state is not clobbered by CapsLock handling
  (L182-184 comment explicitly calls this out as a #149 fix).
- `pendingKeystrokes` queue present (L129) — buffers input typed before the QWebChannel bridge
  connects, flushed in order once `bridge` is available (L149, L300-304).
- `IS_MAC` gate present (L139) and used to scope the CapsLock-as-layout-toggle interception to macOS
  only (L177) — Windows/Linux CapsLock is untouched.

### provider_spec.py — multiline_newline_seq per provider
- Dataclass default: `None` (L114).
- `claude` (L263 block): explicit `multiline_newline_seq="\x1b\r"` (L300).
- `gemini` (L426 block): explicit `multiline_newline_seq="\x1b\r"` (L473).
- `codex` (L330 block): explicitly documented as staying at default `None` (L380 comment — ratatui UI
  treats bare ESC differently, intentional).
- `opencode` (L509), `kimi` (L579), `cursor` (L649): no `multiline_newline_seq` override in any of
  their blocks → inherit dataclass default `None`.
- Matches spec exactly: claude+gemini get `'\x1b\r'`, codex/opencode/kimi/cursor = `None`.

## Conclusion
No blockers. All full-suite gates green, all three structural risk areas verified by direct code
read (not just test execution) to be implemented correctly and to actually catch their target
regressions. Clear to land.
