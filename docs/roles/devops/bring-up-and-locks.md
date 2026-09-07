# DevOps: shared resources, long-lived services, pre-QA bring-up

Read this before a build into a shared output dir, before starting a
service that must outlive your pane, or when Lead asks you to bring the
stack up before QA.

## Shared resources & long-lived services (#429/#430)

- **Never build/test into a shared output dir without a lock** — two panes
  running `next build` into the same `web/.next` starve each other for
  20+ min and look hung. Wrap it: `takkub lock web-build --wait 600 --note
  "npm run dist"` → build → `takkub unlock web-build`. Locked by someone
  else = exit 3 with the holder's role: wait, or tell Lead (`takkub send
  --to lead "blocked: web-build locked by qa"`) — never fight over it.
  `takkub lock --list` shows what's held.
- **`takkub done` / `close` kill every process under your pane — detached
  children included** (Node `detached:true` plus child `.unref` is not enough on
  Windows). A service that must outlive your task (cloudflared tunnel, dev
  API, worker) is started with `takkub spawn-service --name <n> -- <cmd>
  [args]`: the cockpit spawns it outside your pane's tree, logs to
  `runtime/services/<project>/<n>.log`, and only Lead stops it (`takkub
  service-stop --name <n>`). Never start such a service directly from your
  shell.
- Need a process under **another** pane killed? Don't — send Lead the
  PID/role; Lead runs `takkub kill --role <role> [--pid N]`.

## Pre-QA local bring-up (port-safe)

When Lead tells you to "bring up the stack before QA" (the verify gate: all
DEV done → devops brings the stack up → QA tests last), follow this:

1. **Check which ports are already in use first** (never collide with
   docker already running — this machine often has several stacks running
   at once):
   ```bash
   docker ps --format '{{.Names}}\t{{.Ports}}'
   docker ps --format '{{.Ports}}' | grep -oE '0\.0\.0\.0:[0-9]+' | grep -oE '[0-9]+$' | sort -un
   ```
2. **Pick free ports** — don't use the defaults if they collide, offset
   them instead (e.g. web 3000→3900, api 3001→3901, db 5432→5932), then
   **publish via env/override without touching the original compose
   file**:
   ```bash
   WEB_PORT=3900 API_PORT=3901 DB_PORT=5932 \
     docker compose -p <project>-qa up -d --wait
   # if compose doesn't parametrize ports → write a temporary
   # docker-compose.override.yml (ports only)
   ```
   If compose hardcodes ports and it can't be fixed quickly →
   `takkub send --to lead` and ask for a decision — never overwrite a stack
   that's already running.
3. **Always detach, never foreground** — `up -d` (`--wait` waits for
   healthy) — never bare `docker compose up` (blocks forever).
4. **Verify it's genuinely healthy** before done:
   ```bash
   docker compose -p <project>-qa ps --format json   # check the health column
   curl -fsS http://localhost:3900/health             # or the real endpoint
   ```
5. **Report the live ports/URLs in `takkub done`** — QA needs to know where
   to test:
   ```bash
   takkub done "stack up (project <project>-qa): web http://localhost:3900 · api :3901 · db :5932 · ทุก service healthy — QA เทสที่ URL พวกนี้"
   ```

After QA is done, Lead may tell you to `docker compose -p <project>-qa
down` to free up RAM/ports — only do this when instructed.
