# #246 fix — ruff pin drift (pyproject.toml vs .pre-commit-config.yaml)

## Root cause

`pyproject.toml`'s dev-dependency pin (`ruff==0.16.2`, used by CI and any
fresh `pip install -e .[dev]`) and `.pre-commit-config.yaml`'s
`ruff-pre-commit` `rev` (was `v0.16.0`, used by the local pre-commit gate)
had drifted two point releases apart. Both files carry a *deliberate* pin —
`pyproject.toml`'s own comment says to bump them together — but nothing
enforced that, so dependabot's PR #155 (`ruff==0.16.1` → `0.16.2`) touched
only `pyproject.toml` and the pre-commit `rev` was left behind.

## Fix

1. `.pre-commit-config.yaml`: `rev: v0.16.0` → `rev: v0.16.2`, matching the
   pyproject pin. Ran both `ruff` hooks against the whole tree with the new
   rev — no rule newly introduced in 0.16.1/0.16.2 flagged existing code
   (`git status` after the run showed zero unintended changes).
2. Added `tests/test_ruff_pin_matches_pre_commit_rev` to
   `tests/test_version_sync.py`, following the existing pattern in that file
   (`test_python_and_npm_versions_match_pyproject`): parses `pyproject.toml`'s
   `ruff==X` dev pin and `.pre-commit-config.yaml`'s `ruff-pre-commit` rev,
   asserts they're the same version. Verified it actually catches drift by
   reverting the rev to `v0.16.0` locally and confirming the test fails with
   a clear `'0.16.2' == '0.16.0'` diff, then restored the fix.
3. Swept for the same class of dual-pin (a version string duplicated across
   `.pre-commit-config.yaml` and something dependabot/CI reads independently)
   and found one more: **gitleaks**. `.pre-commit-config.yaml`'s gitleaks
   `rev` and `.github/workflows/security.yml`'s `VER=` (used to download the
   gitleaks binary for the CI secret-scan job) are two more independent
   copies of the same version, currently in sync (`8.18.4`/`v8.18.4`) but
   with no gate — the exact same silent-drift shape as the ruff case, just
   not yet triggered. Added `test_gitleaks_pin_matches_security_workflow`
   covering it too, same file.

No other dual-pin was found: CI's ruff step (`ci.yml`) installs via
`pip install -e .[dev]`, so it always reads `pyproject.toml`'s pin directly
— no second copy to drift. `Dockerfile`'s `npm install -g` packages and Qt
pins are single-sourced (only appear once each), not duplicated across
files.

## Files changed

- `.pre-commit-config.yaml` — `ruff-pre-commit` rev `v0.16.0` → `v0.16.2`,
  plus a comment pointing at the new guard test.
- `tests/test_version_sync.py` — `test_ruff_pin_matches_pre_commit_rev` and
  `test_gitleaks_pin_matches_security_workflow` (+ a small `_pre_commit_rev`
  helper shared by both), reading `.pre-commit-config.yaml` via `yaml.safe_load`
  (`pyyaml` is already a core dependency).

## Verification run (this worktree)

```
.venv\Scripts\python.exe -m pytest tests/test_version_sync.py -v
# 4 passed

.venv\Scripts\python.exe -m ruff check src/ tests/
.venv\Scripts\python.exe -m ruff format --check src/ tests/
.venv\Scripts\lint-imports.exe
# all clean, 25/25 import-linter contracts kept

.venv\Scripts\pre-commit.exe run ruff --all-files
.venv\Scripts\pre-commit.exe run ruff-format --all-files
# both Passed, git status --short showed only the two intended files changed
```

Full suite deferred to qa's batch gate per project convention (targeted
tests only mid-flight).
