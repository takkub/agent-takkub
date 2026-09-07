# Mobile: UI self-verify + browser access (#433)

Read this before `takkub done` on any task that touched UI.

## Browser access

This role is granted browser access for **self-verifying its own UI work**
only (#433) — use the project's own tooling (`npx playwright screenshot
...`, a screenshot script the project already ships, or the Playwright MCP
if Lead attached one). Don't `npx playwright install` a new Chromium while
an existing one works (cache once bloated to 2.88GB across 4 builds) —
check `ls ~/AppData/Local/ms-playwright` / `~/.cache/ms-playwright` first.
Full e2e / regression suites are still qa's job — you take screenshots of
what *you* changed, nothing more.

## Self-verify steps (required before `takkub done`)

Every UI change must be seen with your own eyes before you report done —
never hand a "please have qa look" note to Lead:

1. Run the app (dev server / simulator / emulator — whatever the project
   uses).
2. Screenshot every screen/component you touched at mobile 390px and
   desktop 1440px (e.g. `npx playwright screenshot
   --viewport-size=390,844 http://localhost:3000/page
   $TAKKUB_ARTIFACTS_DIR/screenshots/page-390.png`), or the simulator's own
   screenshot tool when the change is native-only (Swift/Kotlin).
3. Open the shots (Read tool) and compare against the task — fix and
   re-shoot until it matches.
4. Put the file path of each screenshot on its own line in the done note
   (path only — never embed the image; Lead/user open it themselves).
   `$TAKKUB_ARTIFACTS_DIR/screenshots/` is where the cockpit and the Remote
   app already look for them.

`takkub done` for a mobile UI task is rejected when the note carries no
screenshot path, the path doesn't exist, or the note says "not opened yet /
routed to qa". A task with zero visual impact (pure logic, config, types) →
write `[no-ui]` in the note instead.

## Test tier reminder (#485)

Do not run `takkub qa-gate` yourself — the gate runs once per batch at the
qa stage. For a logic change, run at most the targeted spec/test of the
files you touched; for a style/asset/i18n/wording change run nothing. Write
the regression test in the same diff (#478) and let qa's single batch gate
+ CI prove the rest.

## Project conventions

Before writing code, check the project's stack conventions first — some
projects allow Expo, others forbid it (pure RN / community packages only).
If unsure, check that project's `package.json` + README first.
