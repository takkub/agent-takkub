# Issue Tracker Design Review - 2026-05-22

## Blind spots (severity-ranked)
1. [HIGH] `issue new` needs an atomic create path. A per-day ID computed by scanning `docs/issues/` and then writing `YYYYMMDD-NNN.md` races when two panes report bugs together: both can choose the same next suffix. Use exclusive file creation and retry the next suffix after a collision, or protect the directory with a lock that is reliable on the supported platforms. Do not overwrite an existing issue file as the collision fallback.
2. [HIGH] Hand-edited markdown is the storage API, so corrupt YAML must be a first-class condition. `list` should not crash on malformed frontmatter, missing frontmatter, duplicate IDs, a filename/frontmatter ID mismatch, an invalid status, or an unreadable timestamp. Surface invalid files with path and parse error, keep listing valid files, and make `show` preserve raw content for recovery.
3. [HIGH] Editor launch is a failure-prone create flow. If `--body` is absent and `$EDITOR` is unset, misquoted, exits nonzero, or no interactive TTY is present, `issue new` needs a deterministic outcome. In noninteractive contexts it should fail with guidance to pass `--body` or configure an editor; it should not hang a cockpit pane waiting on an editor.
4. [MED] The day portion of `YYYYMMDD-NNN` is underspecified. Midnight rollover and pane timezones can produce surprising ordering and duplicate expectations if one code path uses local time and another uses UTC. Pick one clock for ID generation, document it, and store `created_at` as an offset-aware timestamp so the ID is a label rather than the only time source.
5. [MED] Lifecycle is too narrow if only `new`, `list`, `show`, and `close` exist. A mistaken close needs an explicit `reopen` path or a documented manual-edit recovery rule that clears `closed_at` and `closed_note` consistently. Without that, status fields drift and automation cannot tell a reopened issue from a corrupt closed issue.
6. [MED] Wiring `issue` into the CLI should follow the existing command registration, argument parsing, output/error style, and exit-code conventions in `src/agent_takkub/cli.py`. Before implementation, verify that `issue` does not conflict with any current top-level parser command, alias, command dispatch table, or tests that assume a fixed subcommand set. Source inspection for that check was blocked during this review because local PowerShell commands failed before startup.
7. [MED] Field validation needs a forward-compatible boundary. Unicode titles and bodies should remain UTF-8 text; very long bodies should be streamed or read as text without arbitrary truncation; list output should sanitize embedded newlines/control characters from title-like fields so a malformed file cannot break terminal rows.
8. [MED] `list` usability will degrade before filesystem scale does. Fifty open and closed rows are readable only with filters and compact columns. Default to the actionable set, support status/severity/tag filters and stable sorting, and keep a terse one-line format for cockpit use.
9. [LOW] Missing `docs/issues/` should be handled intentionally. `new` can create it, while `list` should return an empty result without a stack trace. File discovery should ignore unrelated files and report issue-like markdown files that cannot be parsed.
10. [LOW] A linear scan of 100 or a few hundred markdown files is acceptable for this local tracker. A cache adds invalidation failure modes around manual edits and concurrent writers; only add an index after measurements show the scan is a real cockpit cost.

## Suggested additions/removals
- Add an explicit schema version or parser policy now. A small `schema_version` field is useful if frontmatter evolves; otherwise state that unknown fields are preserved and ignored.
- Add `updated_at` only if commands beyond create/close/reopen will mutate issues. It is more generally useful than narrow fields such as `related_commit`.
- Consider `reopen` as a command, not a hidden manual edit convention.
- Consider `--status`, `--severity`, `--tag`, and `--limit` on `list`; keep the default output compact and sort by newest actionable issue first.
- Keep `assignee` out until ownership exists in cockpit workflow. The current `role` field already captures the reporting context and stale assignees would add process without enforcement.
- Keep `repro_steps_url` out of the core schema. Repro steps belong in the markdown body; a URL can live there unless a consumer needs structured links.
- Keep `related_commit` out unless Lead has a query that depends on it. A markdown investigation log can mention commits without coupling local bug capture to VCS lifecycle.
- Define closed-field invariants: open issues have empty `closed_at` and `closed_note`; closed issues have `closed_at`, while `closed_note` may be optional only if that choice is explicit.
- Preserve unknown frontmatter fields and body bytes as much as the YAML/markdown library allows when updating status, so recovery edits and later schema fields are not erased by `close`.

## Concrete code patterns to consider
```python
from pathlib import Path
from datetime import datetime


def reserve_issue_path(issues_dir: Path, clock) -> tuple[str, Path]:
    issues_dir.mkdir(parents=True, exist_ok=True)
    day = clock().strftime("%Y%m%d")  # clock policy must be one documented timezone
    for suffix in range(1, 1000):
        issue_id = f"{day}-{suffix:03d}"
        path = issues_dir / f"{issue_id}.md"
        try:
            with path.open("x", encoding="utf-8", newline="\n"):
                pass
        except FileExistsError:
            continue
        return issue_id, path
    raise RuntimeError(f"no issue ID available for {day}")
```

```python
def load_issue_for_listing(path):
    try:
        issue = parse_issue_markdown(path)
        validate_issue(issue, path)
        return issue, None
    except (OSError, ValueError, YamlError) as exc:
        return None, f"{path}: {exc}"


def new_body_from_editor(editor, *, stdin_is_tty):
    if not editor or not stdin_is_tty:
        raise UsageError("pass --body or run interactively with EDITOR configured")
    # Parse EDITOR with platform-aware argv handling and check editor exit status.
```

## Overall verdict
proceed with changes - markdown files under `docs/issues/` are a pragmatic local bug tracker for cockpit defects, and an O(N) scan is adequate at the stated size. The design needs atomic ID reservation, tolerant parsing/recovery behavior, a noninteractive editor policy, documented timezone semantics, and a reopen lifecycle before it is robust enough to wire into the CLI.
