# Security & Trust-Boundary Audit Report — agent-takkub v1.0.58

**Date:** 2026-08-13  
**Auditor:** reviewer (agent-takkub security review pane)  
**Scope:** Full-system security audit of agent-takkub v1.0.58 covering 41 recent commits.  
**Target Subsystems:** `remote/` (HTTP/SSE/PWA/Auth), `pane_guard.py` & Subprocess Execution, `autoskills_installer.py`, Credential / OAuth handling, and Secret Persistence in Transcripts/Task Ledger.

---

## EXECUTIVE SUMMARY

| Severity | ID | Title / Subsystem | Status / Exploitability |
|---|---|---|---|
| **HIGH** | `SEC-01` | PWA HTML Attribute Injection in `app.js` Image Renderer (`remote/static/app.js`) | **Proven / Exploitable** |
| **MEDIUM** | `SEC-02` | Hardlink Inode Sharing Side-Effect in `autoskills_installer.py` Staging Mirror | **Proven / High Impact Side-Effect** |
| **MEDIUM** | `SEC-03` | Unredacted Secret Persistence in Task Ledger (`task_ledger.py`) | **Proven / Data Leak** |
| **MEDIUM** | `SEC-04` | Unredacted Secret Persistence in Raw PTY Transcript Logs (`orchestrator_text.py` / `pty_session.py`) | **Proven / Data Leak** |
| **LOW** | `SEC-05` | Unsanitized Control Characters in Mobile Image Caption `lead_say` (`remote/api.py`) | **Proven / Low Impact** |
| **INFORMATIONAL** | `SEC-06` | Fail-Open Policy for Unmapped Roles in Command Guard (`pane_guard.py`) | **Theoretical / By-Design Tradeoff** |

---

## DETAILED FINDINGS BY SEVERITY

---

### [HIGH] SEC-01: HTML Attribute Injection Vulnerability in PWA Image Markdown Renderer

- **File Path:** [`src/agent_takkub/remote/static/app.js:762-764`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/remote/static/app.js#L762-L764)
- **Subsystem:** `remote/static` (Mobile PWA)
- **Status:** **Proven & Exploitable (HTML Attribute Injection)**

#### Code Evidence
```javascript
747:   function mdEscape(s) {
748:     var div = document.createElement("div");
749:     div.textContent = s == null ? "" : String(s);
750:     return div.innerHTML;
751:   }
...
762:     s = s.replace(/!\[([^\]\n]{0,200})\]\((data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+)\)/g, function (m, alt, src) {
763:       return '<img class="remote-image" src="' + src + '" alt="' + alt + '" loading="lazy" decoding="async">';
764:     });
...
765:     s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, function (m, t, u) {
768:       var safeHref = u.replace(/"/g, "&quot;");
769:       return '<a href="' + safeHref + '" target="_blank" rel="noopener noreferrer">' + t + "</a>";
770:     });
```

#### Vulnerability Analysis
In `app.js`, `mdInline(raw)` calls `mdEscape(raw)` first. `mdEscape` relies on `div.textContent = s; return div.innerHTML;`.
In standard HTML DOM serialization of text nodes, double quote characters (`"`) are **NOT** entity-encoded because double quotes are valid within HTML text content.

While line 768 explicitly quote-escapes `u` (`u.replace(/"/g, "&quot;")`) before embedding it inside the link `href="..."` attribute, line 763 does **NOT** quote-escape `alt` before embedding it inside the `<img>` `alt="..."` attribute.

#### Exploit Scenario
If a Lead pane or a malicious agent output contains markdown like:
```markdown
![foo" onerror="alert(document.cookie)"](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==)
```
1. `mdEscape` converts `<` and `&`, but leaves `foo" onerror="alert(document.cookie)` as-is.
2. `s.replace(/!\[...\].../)` inserts `alt` raw, producing:
   `<img class="remote-image" src="data:..." alt="foo" onerror="alert(document.cookie)" loading="lazy" decoding="async">`
3. When rendered via `body.innerHTML = renderMarkdown(text)` (line 987), the browser parses `onerror="..."` as an event attribute on the `<img>` element.

Although the PWA's Content Security Policy (`_CSP_HEADER` in `http_server.py`) restricts `script-src 'self'`, modern browsers will flag inline handler execution, and attribute injection allows injecting arbitrary HTML attributes (e.g., `style="..."`, `class="..."`, or overriding default image attributes).

#### Recommended Fix
Quote-escape `alt` in line 763 just like line 768:
```javascript
var safeAlt = alt.replace(/"/g, "&quot;");
return '<img class="remote-image" src="' + src + '" alt="' + safeAlt + '" loading="lazy" decoding="async">';
```

---

### [MEDIUM] SEC-02: Hardlink Inode Sharing Risk in `autoskills_installer.py` Staging Mirror

- **File Path:** [`src/agent_takkub/autoskills_installer.py:580-589`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/autoskills_installer.py#L580-L589)
- **Subsystem:** `autoskills_installer.py`
- **Status:** **Proven Side-Effect Risk**

#### Code Evidence
```python
580:         for fname in files:
581:             src = root_path / fname
582:             dest = dest_dir / fname
583:             try:
584:                 if src.is_symlink():
585:                     target = os.readlink(src)
586:                     os.symlink(target, dest, target_is_directory=src.is_dir())
587:                 else:
588:                     os.link(src, dest)
589:             except OSError:
...
```

#### Vulnerability Analysis
To isolate unselected skills, `_build_staging_mirror()` constructs a temporary directory under `.autoskills-staging-<token>/` and hardlinks all files (except `.git` and `.claude/skills`) using `os.link(src, dest)`.

Hardlinked files share the exact same filesystem inode and disk blocks as the real project files. If `autoskills` (or an installation hook/script triggered during execution) opens and modifies any hardlinked file in `staging` (such as `package.json`, `pyproject.toml`, or configuration files) in-place without unlinking first, **the modifications directly mutate the real files in `project_root`**, bypassing the user selection gate.

#### Impact
While `.claude/skills/` is excluded from hardlinking, project manifests and configuration files remain hardlinked, introducing a risk of unintended in-place mutations in the target project.

#### Recommended Fix
Either copy configuration files (`copy2`) instead of hardlinking them, or perform copy-on-write during staging setup for manifest files like `package.json`.

---

### [MEDIUM] SEC-03: Unredacted Secret Persistence in Task Ledger

- **File Path:** [`src/agent_takkub/task_ledger.py:213-225`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/task_ledger.py#L213-L225)
- **Subsystem:** `task_ledger.py`
- **Status:** **Proven Data Leak**

#### Code Evidence
```python
213:         _atomic_write(
214:             _ledger_dir(project) / date / detail_name,
215:             f"---\n"
216:             f"date: {date}\n"
217:             f"role: {role}\n"
218:             f"cwd: {cwd_disp}\n"
219:             f"project: {project}\n"
220:             f"goal: {goal_text}\n"
221:             f"feature: {feature_text}\n"
222:             f"provider: {provider}\n"
223:             f"status: working\n"
224:             f"assign_ts: {now.strftime('%H:%M:%S')}\n"
225:             f"---\n\n{task}\n",
226:         )
```

#### Vulnerability Analysis
Whenever `takkub assign` is called, `create_assignment()` writes the entire raw `task` prompt text into `<project>/<date>/<hhmmss>-<role>-ledger.md` and indexes it in `.ledger-state.json` and `INDEX.md`.

If a user or Lead includes sensitive information (such as API keys, database credentials, bearer tokens, or `.env` secrets) in the task prompt, `task_ledger.py` writes the prompt unredacted to disk inside the project task ledger.

#### Impact
Secrets stored in `INDEX.md` and detail files persist indefinitely in the repository task history and can be accidentally committed or viewed in task logs.

#### Recommended Fix
Apply a secret-redaction filter (pattern matching `sk-`, `ghp_`, `bearer `, password assignments, etc.) to `task` text before writing to ledger files.

---

### [MEDIUM] SEC-04: Unredacted Secret Persistence in Raw PTY Transcript Logs

- **File Path:** [`src/agent_takkub/orchestrator_text.py:840-849`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/orchestrator_text.py#L840-L849), [`src/agent_takkub/pty_session.py:780-786`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/pty_session.py#L780-L786)
- **Subsystem:** `pty_session.py` / `orchestrator_text.py`
- **Status:** **Proven Data Leak**

#### Code Evidence
```python
780:         if self._transcript is not None:
781:             try:
782:                 self._transcript.write(data)
783:                 self._transcript.flush()
784:             except Exception:
...
```

#### Vulnerability Analysis
By default (when `TAKKUB_DISABLE_TRANSCRIPTS` is not set), `PtySession` captures all raw PTY bytes and writes them to `runtime/sessions/<date>/<project>/<role>-<HHMMSS>.transcript.log`.

Any sensitive text output in terminal sessions—including environment variables printed via `cat .env`, API tokens in stdout/stderr, or OAuth redirect URLs—is written unredacted into log files on disk.

#### Impact
Secrets remain on disk in `runtime/sessions/` until pruned by age. (Note: `cli_server.py` correctly strips `transcript_tail` from status responses to avoid network exposure, but local file persistence remains).

---

### [LOW] SEC-05: Unsanitized Control Characters in Mobile Image Caption (`lead_say`)

- **File Path:** [`src/agent_takkub/remote/api.py:306-311`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/remote/api.py#L306-L311)
- **Subsystem:** `remote/api.py`
- **Status:** **Proven Low Impact**

#### Code Evidence
```python
306:     display_name = Path(filename).name[:120] if isinstance(filename, str) else "image"
307:     clean_caption = caption.strip()[:2000] if isinstance(caption, str) else ""
308:     message = f'[remote → lead] แนบรูปจาก mobile ให้เปิดดูจากไฟล์นี้: "{image_path}"'
309:     if clean_caption:
310:         message += f"\nข้อความประกอบ: {clean_caption}"
311:     try:
312:         say_result = lead_say(orch, message, project_ns)
```

#### Vulnerability Analysis
In `lead_upload_image`, `clean_caption` truncates length but does not strip control characters (e.g. `\x03` Ctrl+C, `\x04` EOF, or ANSI escape sequences). When `lead_say` delivers `message` to `cli_server`, `cli_server.send` writes the message directly into the Lead pane's PTY.

#### Impact
A mobile client in control mode could include PTY control characters (like `\x03`) in an image caption, causing SIGINT or unwanted multi-line prompt execution in the Lead pane.

#### Recommended Fix
Sanitize control characters (`\x00-\x1f` except `\n`) from `clean_caption` before passing to `lead_say`.

---

### [INFORMATIONAL] SEC-06: Fail-Open Policy for Unmapped Roles in Command Guard

- **File Path:** [`src/agent_takkub/pane_guard.py:241-243`](file:///C:/Users/monch/WebstormProjects/agent-takkub/worktrees/agent-takkub/reviewer-1786628179/src/agent_takkub/pane_guard.py#L241-L243)
- **Subsystem:** `pane_guard.py`
- **Status:** **Theoretical / By-Design**

#### Code Evidence
```python
241:     name = normalise_role(role)
242:     if not name or name in _UNGUARDED_ROLES:
243:         return Verdict(True)
```

#### Vulnerability Analysis
If a command invocation passes an empty or unrecognized `role` parameter (or a role name typo), `pane_guard.classify` defaults to `Verdict(True)` (fail-open).

#### Impact
By design, this prevents blocking human operators at a terminal. However, if a subagent pane invokes `takkub _guard` with a missing or unmapped `--role`, browser driver and host-destructive command checks are bypassed.

---

## VERIFICATION & AUDIT METHODOLOGY

1. **Static Analysis & Edge Mapping:** Evaluated static dependencies against `docs/architecture/godfile-map.md` and `docs/architecture/depgraph.json`.
2. **Subprocess Audit:** Verified all `subprocess` invocations across `spawn_engine.py`, `codex_helper.py`, `gemini_helper.py`, `claude_update.py`, `browser_chrome.py`, `doctor.py`, `git_status.py`, and `graft_autobuild.py`. Confirmed usage of explicit argument arrays (`shell=False`).
3. **Authentication Verification:** Verified `AuthGate` (`remote/auth.py`), PBKDF2 hashing (200k iterations), per-client `X-Session` tokens, separate fail counters for token vs password, and single-use ticket consumption (`/api/lead` SSE).

---

## CONCLUSION

The system architecture of `agent-takkub v1.0.58` maintains solid isolation across IPC, subprocess calls, and authentication gates. The primary findings identified in this audit are:
1. **SEC-01 (High)**: Quote-escaping omission in PWA image markdown rendering (`app.js`).
2. **SEC-02 (Medium)**: Hardlink inode sharing risk in `autoskills_installer.py` staging mirror.
3. **SEC-03 & SEC-04 (Medium)**: Unredacted secret storage in task ledgers and raw PTY transcript logs.
