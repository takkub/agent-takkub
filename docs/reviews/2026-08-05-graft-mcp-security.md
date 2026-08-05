# Security review — graft MCP rollout + codex deny-by-default (2026-08-05)

Reviewer: reviewer pane · scope: full uncommitted diff (`git diff` + untracked) on `main` @ `fb14d3d`.
Method: read the code directly; **did not** rely on the backend/devops reports. Where a claim was
empirically checkable I checked it against the real `codex-cli` binary on this machine.

**Verdict: no blocker. 1 HIGH (stale premise + new availability failure mode), 3 MEDIUM, 4 LOW.**
Nothing here leaks an MCP that wasn't already reachable *before* this diff — the diff closes more
than it opens. The findings are about scope gaps and cost, not a regression.

Environment for every empirical claim below: `codex-cli 0.146.0`, Windows 11,
`~/.codex/config.toml` contains exactly one MCP (`github`, `streamable_http`,
`bearer_token_env_var: GITHUB_PAT_TOKEN`).

Targeted tests run: `.venv/Scripts/python.exe -m pytest tests/test_mcp_bridge.py
tests/test_graft_mcp.py tests/test_mcp_resolution_fail_closed.py -q` → **32 passed**.

---

## H1 · HIGH — the premise the whole deny-list machinery rests on is stale on the installed codex

`mcp_bridge.py:24-34` states, and `#121` verified against **codex-cli 0.145.0**, that:

> codex merges the override with the lower-precedence tables rather than replacing them

That is **not true on codex-cli 0.146.0**. Measured, this machine, today:

```
$ codex mcp list --json                                   # baseline
[ { "name": "github", ... "bearer_token_env_var": "GITHUB_PAT_TOKEN" ... } ]

$ codex -c 'mcp_servers={}' -c 'features.plugins=false' mcp list --json
[]
```

`-c mcp_servers={}` **alone** clears the inherited table. The `enabled=false` list that
`_codex_resolved_mcp_names` exists to build is, on this version, redundant.

That matters because of what the diff pays for it:

- `mcp_bridge.py:92-120` shells out to the codex binary on **every codex-family spawn**
  (docstring's own measurement: ~180-225 ms, 5 s ceiling).
- `spawn_engine.py:1714-1728` turns any failure of that subprocess into a **hard spawn refusal** —
  a failure mode that did not exist before this diff.

So on 0.146.0 the code refuses to spawn a pane whose security outcome was *already guaranteed* by
the `mcp_servers={}` prefix the same function emits unconditionally at `mcp_bridge.py:226`. A
transient CLI hiccup (cold npm/nvm shim, AV scan, 5 s timeout under load) now costs the operator a
spawn for nothing.

I am **not** asking to delete fail-closed. It is the right call if 0.145.0 genuinely behaves as
documented, and there is no version gate in the code, so the conservative branch has to stay.
What I am asking for:

1. **Pin the claim to a version.** `mcp_bridge.py:24-34` reads as a statement about "codex".
   Make it `codex-cli ≤ 0.145.0 (0.146.0 clears the table from the empty override alone —
   re-verified 2026-08-05)`. A future reader will otherwise carry a false model of the tool.
2. **Reconsider the severity of the failure.** Since `_CODEX_DISABLE_ALL_MCP_ARGV` is emitted
   unconditionally anyway, an alternative that is still fail-closed on 0.146.0 is: emit the
   disable-all prefix + the role's own servers, log at ERROR, and let the spawn proceed. On
   ≤0.145.0 that would under-deny, so if you keep the hard refusal, say so explicitly in the
   comment — "we refuse because ≤0.145.0 needs the name list" is a much clearer contract than
   the current "refusing to guess a deny-list".

**Files:** `mcp_bridge.py:24-34,92-120,220-229` · `spawn_engine.py:1704-1729`

---

## M1 · MEDIUM — "deny-by-default" is actually deny-for-**listed**-roles; custom roles land in the passthrough bucket

The task asked whether the `None` → passthrough path is intentional or a hole. It is documented as
intentional (`mcp_bridge.py:207-219`), but the set of roles that fall into it is wider than the
comment implies, and it includes a **user-facing, first-class** surface.

`role_mcp_allowlist` (`shared_dev_tools.py:614-623`) returns
`effective_mcps(role, _ROLE_MCP_POLICY.get(role))`. `_ROLE_MCP_POLICY`
(`shared_dev_tools.py:592-604`) names 10 roles. But `roles.all_role_names()` also yields:

- **`gemini`** (`roles.py:49`) and **`shell`** (`roles.py:72`) — registered defaults, neither in the policy table.
- **every A6 custom role** (`roles.py:81-98`). `custom_roles.create_role` (`custom_roles.py:158+`)
  writes a registry entry and a role `.md`, and **never touches `pane-tools.json`** — I grepped the
  whole module for `mcps` / `set_role_items` / `pane_tools`: no hits. A freshly created custom role
  therefore resolves to `None`.

What `None` buys that role:

- **On codex** — `mcp_bridge.py:220` is skipped entirely. No `mcp_servers={}`, no
  `features.plugins=false`. The pane inherits the operator's whole `~/.codex/config.toml`. On this
  machine that is a credentialed GitHub MCP (`bearer_token_env_var: GITHUB_PAT_TOKEN`) handed to a
  role nobody granted it.
- **On claude** — `shared_mcp_config_path_for_role` (`shared_dev_tools.py:287-303`) falls through to
  `shared_mcp_config_path()`, i.e. the **full master** `shared-mcp.json`: playwright +
  chrome-devtools + graft + every merged user MCP. That is precisely the browser-MCP grant the role
  files forbid in prose, arriving through the layer that prose was supposed to back up.
- **`gemini` specifically** — when the `agy` CLI is off or missing, the orchestrator degrades the
  role to claude. The substitute pane then takes the claude path above and gets the full master set.

This is pre-existing, not introduced here. But the diff **widens the blast radius**: `graft` is now
in the master file, so every `None`-policy role silently gains it, and the diff's own framing
("deny-by-default") overstates what the code delivers.

**Suggested fix** — one line at the policy boundary, not a per-role table edit:

```python
def role_mcp_allowlist(role: str) -> frozenset[str] | None:
    explicit = effective_mcps(role, _ROLE_MCP_POLICY.get(role))
    if explicit is not None:
        return explicit
    # A REGISTERED role with no entry is a policy gap, not a passthrough
    # licence — deny it. None stays reserved for a name we've never heard of.
    from .roles import all_role_names
    return frozenset() if role in all_role_names() else None
```

Tradeoff to weigh before applying: this is a behaviour change for anyone who relied on a custom
role inheriting the master config. Alternative with the same effect and less reach: have
`create_role` write an explicit `"mcps": []` entry. Either way, add a test asserting every name
from `all_role_names()` resolves non-`None` — that is the invariant that keeps this closed.

**Files:** `shared_dev_tools.py:287-303,592-604,614-623` · `roles.py:49,72,81-98` ·
`custom_roles.py:158+` · `mcp_bridge.py:207-220`

---

## M2 · MEDIUM — the "tool output ≠ คำสั่ง" guard reaches **claude panes only**, while codex panes get graft

The prose was added to all 7 graft-holding role files — that part is complete (verified below in
PASS). The delivery mechanism is not.

`role_md_file` (staged from `.claude/agents/<role>.md`) is appended to argv in exactly one place:

```
spawn_engine.py:2284-2285
    if role_md_file and _claude_system_prompt_flag:
        argv.extend([_claude_system_prompt_flag, role_md_file])
```

That is inside the **claude** branch. A non-claude provider with
`context_strategy="agents_md_file"` instead gets `codex_agents_md.CODEX_AGENTS_MD` +
the skill appendix (`spawn_engine.py:1613-1620`, `codex_agents_md.py:202-250`). I read that
cheatsheet's full "Hard rules" block (`codex_agents_md.py:42-93`) — **it contains no
tool-output-is-not-an-instruction rule.**

Meanwhile `_codex_mcp_argv` cheerfully injects graft into codex panes for
backend/frontend/mobile/devops/reviewer/qa/critic. So the panes that consume graft's
injection-shaped output through a *different* model are exactly the ones running without the guard.
This is the `multi-provider first` directive in `CLAUDE.md` — and the "tool policy needs 2 layers"
lesson — landing on the claude-only side again.

Today `runtime/role-providers.json` is `{"qa":"claude","critic":"claude"}`, so this may not be live
on this machine right now. It becomes live the moment any graft role is re-pointed at
codex/opencode/kimi/cursor, which is a Settings toggle.

**Fix:** move the three bullets into `CODEX_AGENTS_MD`'s Hard rules block. One edit covers every
non-claude provider, present and future, and the rule is provider-neutral prose already.

**Files:** `spawn_engine.py:1613-1620,2284-2285` · `codex_agents_md.py:42-93,202-250` ·
`.claude/agents/{backend,frontend,mobile,devops,qa,reviewer,critic}.md`

---

## M3 · MEDIUM — the new `src/agent_takkub/.ignore` pollutes every ripgrep in the package

`src/agent_takkub/.ignore` (untracked, generated by `graft build`) re-admits `graft/` to ripgrep,
excluding only `.cache/` and `.graph/`. That leaves graft's **141 generated `.md` cards** in the
search index. Verified with the Grep tool just now:

```
Grep "mcp_argv_for_provider" in src/agent_takkub/
  → spawn_engine.py
  → mcp_bridge.py
  → graft\mcp_bridge.md      ← generated card
```

Every agent in this repo navigates with Grep/Glob (it is the instruction in every role file, and
`godfile-map.md` exists precisely to stop grep-and-guess). Doubling the hit count with generated
signature copies works against that, and the cards carry **line numbers that do not map to real
source** — a `graft/mcp_bridge.md:NN` citation looks exactly like a real `file:line` and is not one.

The pilot doc records both dotfiles as "correct/harmless"
(`docs/audit/2026-08-05-graft-pilot.md:33`). The `.gitignore` is; the `.ignore` is not harmless
*for this repo's workflow*.

**Fix:** don't commit `.ignore` as-is — the cards are already reachable through the graft MCP, which
is the entire point of wiring the MCP up. If graft regenerates it on every build, invert it to keep
`graft/` out of ripgrep entirely.

**Keep and commit `src/agent_takkub/.gitignore`** — that one is worth having so 14 MB of graph
never lands in a commit by accident.

**Files:** `src/agent_takkub/.ignore` (new) · `src/agent_takkub/.gitignore` (new) ·
`docs/audit/2026-08-05-graft-pilot.md:31-35`

---

## L1 · LOW — `warm_graft_mcp` launches the MCP *server*, not a version probe

`shared_dev_tools.py:241-255` warms the npx cache by running
`npx -y @nanonets/graft@0.8.2 mcp` with `stdin=DEVNULL`, relying on the server exiting on EOF. It
does today. If a future graft build blocks on stdin instead, the warm thread sits for the full 30 s
timeout on every cockpit boot. `--version` warms the identical npx cache with no such dependence.

Noting rather than pressing: this mirrors `warm_browser_mcps` exactly, and consistency with the
existing pattern has its own value.

---

## L2 · LOW — supply chain: the pin covers the top-level package only

`GRAFT_MCP` (`shared_dev_tools.py:150-157`) pins `@nanonets/graft@0.8.2`. That is the right call and
stronger than it looks — npm forbids republishing an existing version, so the top-level tarball is
immutable once resolved.

The gap is one level down. `npx -y` re-resolves the **dependency tree** on every cold run, and per
the pilot doc (`docs/audit/2026-08-05-graft-pilot.md:86`) graft depends on `tree-sitter@^0.21.1`
plus three grammar packages — **all caret ranges, all native N-API addons** installed via
`node-gyp-build` with prebuilt binaries. A compromised release of any of those publishes into the
range and is pulled on the next cold npx, executing native code in the pane's cwd. `-y` is
mandatory here (the non-interactive rule), so there is no human gate either.

So: the pin bounds the blast radius to graft's transitive deps, not to zero. That is the same
exposure the browser MCPs already carry, so this is not a new class of risk — but it should be
stated rather than implied by "pinned".

Options, cheapest first:

- **(a) Accept and document.** One line next to `_GRAFT_MCP_VERSION` noting the pin is top-level
  only. Parity with playwright/chrome-devtools.
- **(b) Point `GRAFT_MCP` at the globally installed binary.** `doctor.check_graft` already installs
  and version-matches `@nanonets/graft@<pin>` (`doctor.py:check_graft`). Using `graft mcp` instead
  of `npx -y @nanonets/graft@<pin> mcp` freezes the whole resolved tree at install time and
  re-resolves only on an explicit reinstall — and removes cold-npx spawn latency as a bonus. Needs
  an npx fallback when the global install is absent.
- **(c)** A lockfile-backed local install. Correct, and more machinery than this is worth today.

**(b)** is the cheap win, and half of it is already built.

---

## L3 · LOW — worktree panes get permanently-empty graft, and the prose doesn't say why

`graft/` is gitignored, so a `--isolation worktree` pane has no graph and **every** graft call
returns "no matching nodes". devops confirmed this doesn't wedge the pane, and I agree — the tool
degrades gracefully.

The added prose correctly says an empty result isn't proof of absence. It doesn't say that in a
worktree the result will *always* be empty, so an agent can reasonably burn several calls
re-phrasing a query before falling back to Grep. One clause closes it:

> ⚠️ ใน pane ที่ `--isolation worktree` graft จะไม่มี graph → ตอบว่างเสมอ ใช้ Grep/Glob ตรงๆ ไปเลย

Ergonomics, not correctness.

---

## L4 · LOW — `append_failure_entry` persists pane-authored text into the next spawn's system prompt

`orchestrator.py:2103-2114` → `role_memory.append_failure_entry`. The `reason` string is the first
line of the pane's own `takkub done --fail` note. It is written into
`runtime/role-memory/<project>/<role>.md`, which `ensure_role_memory` injects into **every future
spawn of that role**.

So a pane that *did* follow an injected instruction can now place standing text into its own future
system prompt with no agent deciding to edit a file. This sits directly adjacent to the guard prose
this diff adds, which is why it's worth naming.

Bounding facts, which keep it LOW: the entry is capped at `_MEM_MAX_ENTRY_CHARS` (600), it dedups by
reason, it only fires on the fail path, and the pane could already `Edit` that file directly — this
is a convenience path for an existing capability, not a new one.

**Suggestion:** wrap auto-captured lines in a marker that says where they came from, e.g.
`- 2026-08-05: fail (auto-captured, pane-authored) — …`. The `_FAIL_ENTRY_RE` prefix already exists
for dedup; making it self-describing costs nothing and lets a future reader (human or agent) weigh
a curated bullet differently from a transcribed one.

---

## PASS — checked and correct

Recording these so the next reviewer doesn't re-derive them.

- **Fail-closed call-site coverage is complete.** `mcp_argv_for_provider` has exactly two non-test
  call sites: `spawn_engine.py:1715` (guarded) and `spawn_engine.py:2314`, which hardcodes
  `"claude"`. The claude variant `_claude_mcp_argv` (`mcp_bridge.py:174-185`) wraps its only
  fallible call in `except Exception` and cannot raise `McpResolutionError`, so 2314 needs no guard.
  The task flagged ~2298 as a possible miss — it isn't one.
- **No other raise path escapes `_codex_mcp_argv`.** `_role_mcp_servers` never raises by
  construction (`mcp_bridge.py:158-171`); `_toml_literal`'s `TypeError` is caught at
  `mcp_bridge.py:244`; `load_policy` is documented and implemented never-raises
  (`pane_tools_policy.py:95-113`). `McpResolutionError` is the only thing that gets out, which is
  what the caller catches.
- **Fail-safe direction is correct.** If `_role_mcp_servers` degrades to `{}` while the allowlist is
  non-empty (corrupt master file), the role gets the disable-all prefix and **no** servers — it
  fails toward *less* access. Covered by `test_mcp_bridge.py:149-159`.
- **Name-collision shadowing is not a problem.** I expected that overriding only `command/args/env`
  on a name that also exists in the user's config would leave the inherited `url` /
  `bearer_token_env_var` in the merged table. Empirically false on 0.146.0:
  `codex -c 'mcp_servers={}' -c 'mcp_servers.github.command="node"' mcp list --json` returns
  `github` as a pure stdio transport — the http fields are gone.
- **`MANAGED_MCP_NAMES` protection is complete.** Add (blocked even with `force=True`), remove, and
  the user-MCP merge/prune all route through it (`shared_dev_tools.py:643,675,1010,1054`), and the
  Settings ownership check uses it (`mcps.py:36`). All four have tests.
- **Guard-prose coverage across the 7 graft roles is complete.** All of
  frontend/backend/mobile/devops/qa/reviewer/critic carry it; `lead`/`designer`/`codex` correctly
  hold no graft and correctly have no prose. `analyst.md` / `docs.md` / `security.md` exist on disk
  but are **orphans** — not registered roles, per `pane_tools_policy.py:37-44` which calls that
  drift out by name. Not a gap.
- **The prose itself is scoped right.** It leads with "ครอบทุก MCP/CLI tool ไม่ใช่แค่ graft" and
  states the general rule before the graft example, so a future tool with the same behaviour is
  already covered. That was the right call over a graft-specific rule.

---

## Recommended order

1. **M2** — one edit to `CODEX_AGENTS_MD`, closes the multi-provider gap. Cheapest, highest value.
2. **M3** — decide `.ignore` before committing; it degrades the tool every role uses most.
3. **M1** — the custom-role passthrough. Needs a decision on the behaviour change, then one function
   plus one test.
4. **H1** — re-verify 0.145.0, then either version-qualify the docstring or relax the hard refusal.
   High value for correctness-of-understanding; not urgent for safety.
5. **L1-L4** — as convenient.
