CLEAN — no new findings

Scope reviewed: accumulated cross-platform diff around path/FS/encoding and provider-adjacent spawn behavior, with emphasis on:

- `src/agent_takkub/token_meter.py::encode_path_for_claude` and `session_project_dir_for_cwd`
- `src/agent_takkub/spawn_engine.py::_SAFE_SESSION_UUID_RE`, `_resume_uuid_matches_cwd`, and `_normalize_cwd_for_compare`
- `src/agent_takkub/_pty_backend.py::_WinptyBackend.spawn`
- `src/agent_takkub/config.py::lead_cwd`
- resume picker / remote resume / non-claude env / codex / gemini helper diffs

Notes:

- `encode_path_for_claude` now matches the observed Claude session directory convention by encoding every non-ASCII-alnum character to `-` after `Path.resolve()`. The new direct `session_project_dir_for_cwd(config_dir, cwd)` use removes the lossy scan-and-decode identity bug for `-`, `_`, `.`, and spaces. I did not find a concrete regression for UNC paths, long paths, or trailing dot/space names beyond ordinary Windows filesystem caveats. Tests cover separator/drive, `_`, `.`, space, and real encoded lookup call sites; no new blocker found.
- `_SAFE_SESSION_UUID_RE = ^[0-9A-Za-z_-]+$` is intentionally narrower than a path segment and blocks traversal before the filesystem join. Real session files present under this machine's `~/.claude/projects` are standard UUID filenames matching this charset. Existing tests cover hyphen and underscore shard-like IDs plus separator/dotdot rejection. I found no evidence of a real Claude session filename format that would be false-rejected.
- `_normalize_cwd_for_compare` normalizes both sides symmetrically with `Path.resolve()` and `os.path.normcase()`, and degrades to normalized raw string on `OSError`. That fallback cannot prove identity for a failing side, but it preserves old exact-string behavior instead of crashing. The new `bool(prior_uuid_cwd)` guard avoids the known empty-string-to-process-cwd trap. No new compare bug found.
- `_WinptyBackend.spawn` now passes argv as a list to pywinpty. That is the right boundary for spaced executable paths and quoted/spaced args: pywinpty receives `argv[0]` verbatim and handles remaining argv quoting internally. POSIX path stays list-based through `ptyprocess`. No args-with-space/quote issue found in the diff.
- Provider parity looks intact in the reviewed areas: non-interactive/color/MCP env moved into the shared env builders used by shell/codex/gemini/claude; codex and gemini helper changes are platform-gated; claude-only resume validation stays scoped to claude resume semantics and is prevalidated before closing the Lead pane.

Residual test gap, not a finding: there are no dedicated regression tests for UNC shares, Windows `\\?\` long-path spelling, or APFS case-insensitive-but-case-preserving aliases. Based on the code reviewed, I do not see a concrete failing path for those cases that rises above plausible-but-unproven.
