# #159 — blank/failed screenshot evidence not flagged

## Bug

A role could report `takkub done` with evidence "collected" via the
auto-scan (`Orchestrator._scan_done_evidence`) while one of the scanned
files was a failed capture (blank page, mid-render race, browser crash) —
e.g. a 5.0KB file sitting next to siblings at 43–105KB. The scan only ever
checked extension + mtime + settle window; file *existence* was treated as
proof of a *good* screenshot. Neither the role nor Lead had any signal the
capture was bad short of opening every image by hand.

## Fix (`src/agent_takkub/orchestrator.py`)

`_scan_done_evidence` still counts every matching file as evidence (a bad
shot is still a shot the role took — this isn't a "reject" gate), but each
path in the `📸 evidence: …` line is now annotated:

```
📸 evidence: runtime/exports/.../login.png (43.2KB), runtime/exports/.../blank.png (5.0KB ⚠small)
```

- **Size** (`_evidence_file_size`) is always shown in KB.
- **`⚠small`** — file is under `_EVIDENCE_SUSPECT_MIN_BYTES` (10KB). A real
  screenshot is essentially never this small.
- **`⚠bad-header`** — a cheap magic-byte sniff (`_evidence_looks_valid_image`)
  found the file's first bytes don't match its extension (PNG/JPEG/GIF/WEBP
  signatures). Catches a 0-byte file or an HTML error page saved with a
  `.png` extension. No image-decode dependency added — this is a header
  check only, not a full decode.
- Both reasons combine as `⚠small+bad-header` when both trip.

`_find_evidence_files` now returns `(mtime, path, size)` instead of
`(mtime, path)` — internal only, no external caller outside this class.

## Role prompts (item 3 of scope)

- **`.claude/agents/qa.md`** — added a post-`mb shot` size check
  (`stat -c%s`/`stat -f%z` fallback for cross-platform) that warns and
  re-shoots before `takkub done` if a file lands under 10KB.
- **`.claude/agents/critic.md`** — added a note at the "List + Inspect
  shots" step: an outlier-small file relative to its siblings is grounds to
  `takkub send --to lead "blocked: ..."` and ask for a re-capture, not to
  review a blank image as if it were real evidence.

## Tests

Added `TestSuspectCaptureFlagging` to `tests/test_done_evidence.py` (6
cases): size annotation on a normal file, `⚠small` on an undersized real
PNG, `⚠bad-header` on an oversized-but-wrong-content file, both flags
combined, and direct unit coverage of `_evidence_format_entry` /
`_evidence_looks_valid_image`.

All existing evidence tests (36 total in the file) still pass unchanged —
the pre-existing tests only assert substrings (`"login.png" in result`,
`.count(".png") == 10`, etc.), never an exact-match on a found-evidence
line, so appending the size/flag annotation is backward compatible. Also
reran `tests/test_stuck_recover.py` and
`tests/test_agent_role_files_have_browser_guard.py` — both green.

## Scope notes

- Threshold (10KB) and the magic-byte check are heuristics, not a hard
  gate — a suspect file is still attached and still suppresses the
  `⚠ no evidence cited` warn-role warning, since it *is* an attempt at
  evidence. The judgment of "is this actually bad, ask for a redo" is left
  to Lead, who now has the size/flag visible in the digest instead of
  having to open every file to find out.
- Did not add an image-decode dependency (e.g. Pillow) — out of scope per
  minimal-code: a magic-byte sniff already catches the two failure modes
  named in the issue (empty file, wrong content) without a new dependency.
