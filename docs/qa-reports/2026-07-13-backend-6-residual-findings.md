# Backend #6 residual findings

Date: 2026-07-13

## Outcome by file

- `provider_config.py`: already fixed. `provider_for()` normalizes a trailing
  numeric shard suffix before both forced-provider and configured-provider
  lookup. Existing shard tests cover `provider_for()` and
  `effective_provider_for()`.
- `limit_autoresume.py`: already fixed. Non-Claude panes park on their own
  rate-limit signal without querying Anthropic usage, while Claude panes retain
  the usage-confirmation path. The worker also rechecks the effective provider.
- `limit_status.py`: fixed. `unregister()` now preserves the per-config 429
  deadline, and a new `register()` suppresses its immediate fetch while that
  deadline remains active.
- `token_meter.py`: fixed as a documentation/correctness clarification. The
  `TAKKUB_CONTEXT_LIMIT` fallback is now explicitly process-wide; the existing
  pane-specific `base` path remains the documented mechanism for spawn metadata.
- `custom_roles.py`: fixed. Both atomic writers record the temporary path before
  their first write; `create_role()` now also removes the temp file when the
  initial write or close fails.
- `role_memory.py`: fixed. A non-seeded section survives curation whenever any
  nonblank content remains, including a `###` sub-heading with no bullets.
- `codex_helper.py`: fixed. On Windows, one-shot execution refuses `.cmd` and
  `.bat` shims, so untrusted prompt text is never passed as a command-shim
  positional argument. Native `codex.exe` and non-Windows executables are
  unchanged.
- `codex_agents_md.py`: fixed. Empty and whitespace-only existing files are
  plantable, while any real non-marker content remains user-owned.
- `skill_scan.py`: fixed for both allowlisted writers. `create_skill()` and
  `update_skill()` track temp paths before writing and clean failures;
  `update_skill()` distinguishes an actual read failure from empty frontmatter
  and synthesizes the immutable name from nested or flat layout.
- `skill_audit.py`: fixed. Directory enumeration and individual markdown reads
  are guarded and failed entries are skipped; the public contract now states
  these filesystem failures never escape.

## Scoped follow-up

The same temp-path finding names `skill_policy.save_policy`, but its source file
(`src/agent_takkub/skill_policy.py`) is outside this pane's explicit edit
allowlist. Its assignment still occurs after `json.dump`, so that residual was
reported to Lead for the owning pane rather than edited here.

## Verification

Targeted pytest command:

```text
python -m pytest -q tests/test_provider_config.py tests/test_limit_autoresume.py tests/test_limit_status.py tests/test_token_meter.py tests/test_custom_roles.py tests/test_role_memory.py tests/test_codex_helper.py tests/test_codex_agents_md.py tests/test_skill_scan.py tests/test_skill_audit.py
```

Result: all collected tests passed.

Targeted lint command:

```text
python -m ruff check src/agent_takkub/limit_status.py src/agent_takkub/token_meter.py src/agent_takkub/custom_roles.py src/agent_takkub/role_memory.py src/agent_takkub/codex_helper.py src/agent_takkub/codex_agents_md.py src/agent_takkub/skill_scan.py src/agent_takkub/skill_audit.py
```

Result: all checks passed.
