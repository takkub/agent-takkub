# Code Review: takkub issue CLI — 2026-05-22

## Verdict
**APPROVE WITH CHANGES** — two blocking issues must be fixed before merge; remaining findings are non-blocking.

---

## Findings (severity-ranked)

### [HIGH] Path traversal — issue_id not validated before path construction
**File:** `src/agent_takkub/issues.py:179, 199`
**Issue:** `close_issue()` and `show_issue()` build a path directly from the caller-supplied `issue_id` string:
```python
path = issues_dir / f"{issue_id}.md"
```
`_ID_RE = re.compile(r"^(\d{8})-(\d{3})$")` exists but is only used in `next_id()` for parsing existing filenames — it is **never applied to inbound `issue_id` arguments**. A caller can pass `"../../../../tmp/secret"` and the code will attempt to read/write a file resolved as `issues_dir` joined with that traversal payload plus the `.md` suffix — escaping the intended directory.

The `.md` suffix limits the attack surface (can't read arbitrary files without the `.md`), but it still allows reading or corrupting files outside the issues directory.

**Affected commands:** `takkub issue close`, `takkub issue show`

**Suggested fix:**
```python
def _validate_id(issue_id: str) -> None:
    if not _ID_RE.match(issue_id):
        raise ValueError(f"invalid issue ID {issue_id!r} — expected YYYYMMDD-NNN")
```
Call at the top of `close_issue()` and `show_issue()` before any path use.

---

### [HIGH] Race condition in ID generation — scan-then-write TOCTOU
**File:** `src/agent_takkub/issues.py:73-90`
**Issue:** `next_id()` scans existing files, computes the next NNN, then returns. The caller writes the file separately. Two concurrent panes (e.g., backend + qa both hitting a bug simultaneously) can scan at the same moment, both compute `20260522-002`, and one silently overwrites the other's issue.

**Severity context:** Low-probability in a 1-pane-at-a-time workflow, but concurrent pane reports are the exact scenario where this tracker is used.

**Suggested fix (codex pattern, also correct):** use exclusive-create `open("x")` to atomically reserve the file, retry on `FileExistsError`:
```python
def _reserve_issue_path(issues_dir: Path, date_str: str) -> tuple[str, Path]:
    for n in range(1, 1000):
        issue_id = f"{date_str}-{n:03d}"
        path = issues_dir / f"{issue_id}.md"
        try:
            path.open("x").close()          # atomic exclusive create
            return issue_id, path
        except FileExistsError:
            continue
    raise RuntimeError(f"no issue ID available for {date_str}")
```
Then `new_issue()` calls `_reserve_issue_path()` and writes the real content into the already-created file.

---

### [MED] `$EDITOR` with spaces fails silently
**File:** `src/agent_takkub/issues.py:233`
**Issue:** `subprocess.call([editor, tmppath])` treats `editor` as a single executable name. If `EDITOR="code --wait"` (common VS Code setup), the call tries to exec a binary literally named `"code --wait"` and fails with `FileNotFoundError`. The exception is not caught, so `body` remains `""` and an empty-body issue is created.

**Suggested fix:**
```python
import shlex
cmd = shlex.split(editor) + [tmppath]
ret = subprocess.call(cmd)
if ret != 0:
    return {"ok": False, "msg": f"editor exited with code {ret}"}
```

---

### [MED] Dead condition — `not body and not args.body` (line 219)
**File:** `src/agent_takkub/issues.py:217-219`
```python
body: str = args.body or ""        # line 217 — body is always a str here
if not body and not args.body:     # line 219 — args.body is already "" or None
```
`body = args.body or ""` guarantees `body` is `""` whenever `args.body` is falsy. The extra `not args.body` check is always true when `not body` is true — the condition is redundant and misleading.

**Suggested fix:** `if not body:` (single condition)

---

### [MED] `--issues-dir` option not wired in argparse — dead CLI path
**File:** `src/agent_takkub/cli.py:492-516`
**Issue:** All four `cmd_issue_*` handlers call `getattr(args, "issues_dir", None)` to allow overriding the issues directory. However, none of the four subparsers (`sin`, `sil`, `sic`, `sis`) register `--issues-dir` as an argument. In production CLI use the value is always `None`, so the default `docs/issues/` is hardcoded in practice. The option path is tested only because unit tests inject it directly via `types.SimpleNamespace`.

**Impact:** Not a bug today (default path is correct), but `_resolve_issues_dir` logic is untested in the real CLI flow.

**Suggested fix:** Add to each subparser (or a shared parent parser):
```python
for sp in (sin, sil, sic, sis):
    sp.add_argument("--issues-dir", dest="issues_dir", default=None, metavar="PATH")
```

---

### [MED] Malformed issue files silently skipped in `list_issues`
**File:** `src/agent_takkub/issues.py:151-152`
```python
except ValueError:
    continue  # skip malformed files silently in list
```
A corrupt issue file (hand-edited YAML mistake, partial write) becomes invisible. The user has no indication that an issue is missing from the list.

**Codex flagged this.** Backend's comment says "silently in list" — the intent is clear but the outcome (user debugging a "missing" issue without a clue) is bad.

**Suggested fix:** collect errors and print a warning line to stderr after the table:
```python
warnings = []
for path in sorted(issues_dir.glob("*.md")):
    try:
        fm, _ = _parse_file(path)
    except ValueError as exc:
        warnings.append(f"  warn: {path.name}: {exc}")
        continue
    ...
if warnings:
    print("\n".join(warnings), file=sys.stderr)
```

---

### [MED] Embedded newline in title breaks table output
**File:** `src/agent_takkub/issues.py:285-291`
**Issue:** `cmd_issue_list` prints `fm.get('title', '')` directly in a formatted row. If a title contains `\n` (possible via hand-edit or a buggy `--body` injection into the wrong field), the table layout breaks. No sanitization applied.

**Suggested fix:**
```python
def _safe_col(val: str, width: int) -> str:
    return val.replace("\n", " ").replace("\r", "")[:width]
```

---

## Cross-check vs codex review

| Finding | Codex | This review | Status |
|---|---|---|---|
| Atomic ID reservation (scan-then-write race) | HIGH | HIGH | **Both flagged — not fixed, blocker** |
| Malformed frontmatter tolerance | HIGH | MED | Backend handles parse/list; silent skip still poor UX |
| Editor launch failure modes | HIGH | MED | Non-TTY handled ✓; EDITOR-with-spaces not handled ✗ |
| Timezone specification | MED | LOW | `datetime.now().astimezone()` is consistent; doc gap only |
| Reopen lifecycle | MED | n/a | Out of scope for this PR |
| Field validation / control chars | MED | MED | This review adds newline-in-title; codex broader |
| Missing `docs/issues/` dir | LOW | — | **Backend handled this correctly** (`mkdir(parents=True)`) |
| No cache needed | LOW | — | Agreed; O(N) scan acceptable |
| **Path traversal** | ❌ not flagged | HIGH | **Codex blind spot — this review adds it** |
| **`--issues-dir` dead in CLI** | ❌ not flagged | MED | **Codex blind spot — this review adds it** |
| **Dead condition line 219** | ❌ not flagged | MED | **Codex blind spot — this review adds it** |

**Backend handled better than codex expected:**
- `list_issues` correctly returns `[]` for non-existent dir (codex flagged as risk; backend got it right)
- `yaml.safe_load` used throughout (codex's required condition — met)
- Body content preserved across close round-trip (tested + confirmed)

---

## Approval blockers (must fix before merge)

1. **Path traversal** — validate `issue_id` against `_ID_RE` at entry of `close_issue()` and `show_issue()` before any `Path` construction
2. **Race condition** — replace scan-then-write `next_id()` with exclusive-create `open("x")` retry pattern

---

## Nice-to-have (non-blocking)

- Fix `EDITOR` with spaces: `shlex.split(editor) + [tmppath]` + check exit code
- Remove dead condition on line 219: `if not body and not args.body:` → `if not body:`
- Wire `--issues-dir` in argparse subparsers so the option is actually reachable from CLI
- Print a stderr warning for malformed files in `list_issues` instead of silent skip
- Sanitize `\n` in title field before table output
- `body = parts[2].lstrip("\n")` silently strips leading blank lines — consider `lstrip("\n")` only stripping the separator newline (i.e., `[1:]` after one leading `\n` is expected by the format)

---

## Test suite assessment

37 tests — solid coverage of the happy path and error cases. Specific gaps:
- No test for path traversal attempt (`"../../secret"` as issue_id)
- No test for `EDITOR` with spaces
- `--issues-dir` tested only via `types.SimpleNamespace` injection, not via CLI parse
- No test that `list_issues` emits a warning for a malformed file in a directory with other valid files
