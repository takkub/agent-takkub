# Installed Build Audit: Dev-Checkout Assumptions

This audit lists all locations in the `agent-takkub` codebase where assumptions are made about running from a development source checkout (e.g., assuming `REPO_ROOT` is a Git repository, writing files inside `REPO_ROOT`, or resolving paths relative to the source structure). These assumptions fail when the app is installed via `npm` or `pip wheel` (where `REPO_ROOT` resolves to `venv/Lib` or another python site-packages location which is empty, read-only, or lacks Git metadata).

## Severity Summary

| Severity | Count |
|---|---|
| Blocker | 0 |
| Major | 4 |
| Minor | 4 |
| Benign | 1 |
| **Total** | **9** |

---

## Findings Table

| File & Line | Prod Behavior | Severity | Short Fix |
|---|---|---|---|
| [app.py:L19](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/app.py#L19) | Attempts to write `boot.log` into `venv/Lib/runtime/boot.log`. Failing silently on read-only system environments or cluttering internal venv directories. | **Major** | Use `config.RUNTIME_DIR / "boot.log"` (or clone its fallback logic to avoid circular import). |
| [status_header.py:L670](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/status_header.py#L670) | Attempts to write `rtk_button.log` into `venv/Lib/runtime/rtk_button.log`. Failing silently on read-only environments. | **Major** | Use `config.RUNTIME_DIR / "rtk_button.log"`. |
| [issues.py:L204](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/issues.py#L204) | Fallback issues DB is read/written inside `venv/Lib/.takkub_issues.json` when `cockpit_bug=True` (default). Lost on package updates. | **Major** | Use `config.DATA_HOME / ".takkub_issues.json"` as the fallback destination when `cockpit_bug=True`. |
| [spawn_engine.py:L905](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/spawn_engine.py#L905) (also L972, L1018, L1102 & [orchestrator.py:L1518](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator.py#L1518)) | Falls back to `REPO_ROOT` (`venv/Lib`) as the working directory when no active project cwd is found, causing Claude panes to start inside `venv/Lib`. | **Major** | Fallback to `os.getcwd()` or `DATA_HOME` instead of `REPO_ROOT`. |
| [cli.py:L554](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/cli.py#L554) (also [skill_audit.py:L55](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/skill_audit.py#L55), L119) | Auditing tools check `.claude/agents` under the current directory instead of using `config.AGENTS_DIR`. Returns empty result or fails. | **Minor** | Resolve `skills_dir` against `config.AGENTS_DIR`. |
| [update_worker.py:L223](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/update_worker.py#L223) | Attempts to write `startup_pull.log` under `REPO_ROOT / "runtime"`. Shielded by `is_git_repo()`, so it exits early, but still incorrect. | **Minor** | Use `config.RUNTIME_DIR / "startup_pull.log"`. |
| [doctor.py:L973](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/doctor.py#L973) | Tells user to click "Enable updates" which doesn't exist on installed builds (they see "Update via npm"). | **Minor** | Check `is_installed_package()` and suggest `npm update -g agent-takkub`. |
| [install.ps1:L385](file:///C:/Users/alice/WebstormProjects/agent-takkub/scripts/install.ps1#L385) | Ignores the user's current clone path and installs into hardcoded `~/WebstormProjects/agent-takkub`. | **Minor** | Resolve `$cockpitDir` dynamically using `$PSScriptRoot\..` (parity with `install.sh`). |
| [cli.py:L694](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/cli.py#L694) | `takkub release` executes `release()` with `REPO_ROOT` which crashes with `FileNotFoundError` (no `pyproject.toml`). | **Benign** | Fail gracefully with a clear message: "takkub release is only available in dev checkouts". |

---

## Detailed Findings & Fixes

### 1. Boot Log Path
* **File & Line:** [app.py:L19](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/app.py#L19)
* **Code snippet:**
  ```python
  _BOOT_LOG = Path(__file__).resolve().parents[2] / "runtime" / "boot.log"
  ```
* **Prod Behavior:** During bootstrap, it attempts to resolve `_BOOT_LOG` under the package parent's `runtime/` folder. For a global npm/pip installation, this maps to `venv/Lib/runtime/boot.log`. If that folder is read-only (which it is for global installs or when permissions are restricted), the write fails silently (since it's wrapped in `try..except Exception:`). The cockpit keeps running, but diagnostic boot logging is completely lost. If writable, it clutters the internal `venv/Lib` directory.
* **Short Fix:** Import `DATA_HOME` or `RUNTIME_DIR` from `.config` to resolve the path. To avoid circular imports (since `app.py` is the main GUI script), duplicate the logic or resolve it dynamically at startup.

### 2. RTK Button Diagnostic Log
* **File & Line:** [status_header.py:L670](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/status_header.py#L670)
* **Code snippet:**
  ```python
  log = _P(__file__).resolve().parents[2] / "runtime" / "rtk_button.log"
  ```
* **Prod Behavior:** Attempts to write to `venv/Lib/runtime/rtk_button.log`. On read-only systems, this fails silently, discarding diagnostic information regarding the visibility of the RTK button.
* **Short Fix:** Replace the `__file__`-relative resolution with `config.RUNTIME_DIR / "rtk_button.log"`.

### 3. Issues Fallback Directory
* **File & Line:** [issues.py:L204](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/issues.py#L204)
* **Code snippet:**
  ```python
  detect_cwd: str | Path | None = str(REPO_ROOT) if cockpit_bug else cwd
  ```
* **Prod Behavior:** If `cockpit_bug=True` (the default for internal cockpit bugs), it detects the git remote of `detect_cwd` (which resolves to `REPO_ROOT` -> `venv/Lib`). Since `venv/Lib` is not a Git repository, it raises an exception and sets `use_local = True`. It then tries to read/write the fallback issues JSON database at `REPO_ROOT / ".takkub_issues.json"` (i.e. `venv/Lib/.takkub_issues.json`). These local issues will be wiped when the cockpit is updated (since pip wheel re-installs clean).
* **Short Fix:** In [issues.py](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/issues.py), use `config.DATA_HOME / ".takkub_issues.json"` for local fallback storage when `cockpit_bug=True` and we are in an installed build.

### 4. Pane Working Directory Fallback
* **File & Line:** [spawn_engine.py:L905](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/spawn_engine.py#L905) (also L972, L1018, L1102 & [orchestrator.py:L1518](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/orchestrator.py#L1518))
* **Code snippet:**
  ```python
  spawn_cwd = cwd or default_cwd_for_role(role_name, project=project_ns) or str(REPO_ROOT)
  ```
* **Prod Behavior:** When a pane spawns and has no valid active project working directory, it falls back to spawning inside `REPO_ROOT`. In an installed build, this spawns Claude/teammate processes inside `venv/Lib`, which is highly confusing, and may cause the agents to write `.claude` session states and memories there.
* **Short Fix:** Change the fallback value from `str(REPO_ROOT)` to `os.getcwd()` or `str(DATA_HOME)`.

### 5. Auditing Skills Directory
* **File & Line:** [cli.py:L554](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/cli.py#L554) (also [skill_audit.py:L55](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/skill_audit.py#L55), L119)
* **Code snippet:**
  ```python
  skills_dir = Path(".claude/agents")
  ```
* **Prod Behavior:** The command `takkub audit-skills` tries to read `.claude/agents` from the current working directory. In production, this directory is usually absent, so the command returns 0 overlap pairs or fails, instead of auditing the cockpit's built-in teammate roles.
* **Short Fix:** Resolve `skills_dir` against `config.AGENTS_DIR` if the folder doesn't exist in the current working directory.

### 6. Silent Update Log Path
* **File & Line:** [update_worker.py:L223](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/update_worker.py#L223)
* **Code snippet:**
  ```python
  log_path = REPO_ROOT / "runtime" / "startup_pull.log"
  ```
* **Prod Behavior:** If silent updates run, it writes `startup_pull.log` inside `REPO_ROOT`. However, `try_silent_self_update()` calls `is_git_repo()` which returns `False` in site-packages, skipping execution. But the path is still structurally incorrect and assumes a dev layout.
* **Short Fix:** Use `config.RUNTIME_DIR / "startup_pull.log"`.

### 7. Version Tracking Help Message
* **File & Line:** [doctor.py:L973](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/doctor.py#L973)
* **Code snippet:**
  ```python
  "convert via the cockpit's update chip ('Enable updates') to enable updates"
  ```
* **Prod Behavior:** When `is_git_repo()` is `False`, the doctor suggests using the "Enable updates" button. But in an installed build, the update button is mapped to `Update via npm`, not `Enable updates`. The advice is therefore misleading to package users.
* **Short Fix:** Check `is_installed_package()` and recommend `npm update -g agent-takkub` instead.

### 8. Hardcoded Development Install Directory (PowerShell)
* **File & Line:** [install.ps1:L385](file:///C:/Users/alice/WebstormProjects/agent-takkub/scripts/install.ps1#L385)
* **Code snippet:**
  ```powershell
  $cockpitDir = Join-Path $env:USERPROFILE "WebstormProjects\agent-takkub"
  ```
* **Usage Behavior:** Unlike the bash version `install.sh` which resolves `REPO_ROOT` dynamically from `$BASH_SOURCE`, the PowerShell version hardcodes `$cockpitDir` to `~/WebstormProjects/agent-takkub`. If a developer cloned the repository to a different folder (e.g. `C:\Projects\agent-takkub`) and runs the script, the script clones a fresh copy to `WebstormProjects` and installs that one, which is unexpected.
* **Short Fix:** Resolve `$cockpitDir` dynamically using `$PSScriptRoot\..` or check if the current directory is already the repository.

### 9. Version Release Ceremony Command
* **File & Line:** [cli.py:L694](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/cli.py#L694)
* **Code snippet:**
  ```python
  res = release(REPO_ROOT, ...)
  ```
* **Prod Behavior:** If `takkub release` is executed in an installed build, it attempts to read `venv/Lib/pyproject.toml` and crashes with a `FileNotFoundError`.
* **Short Fix:** Add a check in `cmd_release` that exits gracefully with a message: "Release command is only available in dev checkouts."
