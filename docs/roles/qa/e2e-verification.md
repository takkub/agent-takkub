# QA: verdict rubric, browser automation, screenshot convention

Read this before you smoke/e2e a web app or write the `takkub done` report.

## Verdict rubric (required in the done report every time you smoke/e2e a web app)

Every smoke/e2e ends with a **1–5 score** defensible with evidence, never a
bare "pass/fail" — **task completion beats prettiness**: a broken submit
button = **1** even if the page looks great · cosmetic/console noise on an
otherwise-working flow caps at **3–4**, never 5.

| Score | Criteria |
|---|---|
| **5** | Finishes cleanly, no issues, responsive + polished |
| **4** | Finishes, minor cosmetic/UX nits, none blocking |
| **3** | Finishes but has real friction (slow / no spinner / confusing copy) |
| **2** | Only partly works, most users get stuck |
| **1** | Doesn't finish — critical failure |

Tag every issue: `[blocker?]` (stops the flow? — caps the score at ≤2) ·
`[console]` (JS error/network≥400) · `[ux]` (friction)

**Always scan non-visual signals** (a pretty screenshot doesn't mean
there's no error):
```bash
mb logs
mb js "JSON.stringify(performance.getEntriesByType('resource').filter(r=>r.responseStatus>=400).map(r=>r.name))"
```
Found an error/network≥400 → tag `[console]`, never give a 5 · prompt is
ambiguous → pick the clearest happy path, restate it as user steps + define
success before you start.

Every verdict must cite evidence as a real path (shots/log) or actual run
output in the done note — a note with no path gets tagged `⚠ no evidence
cited`.

Output (`takkub done`): คะแนน → task → ผล → worked → issues (tagged,
เจาะจง) → edge case → evidence path:
```bash
takkub done "score 2/5 · login flow · [blocker?] submit เงียบ logged 500 (POST /auth/login) · [console] TypeError auth.js:42 · edge: empty/invalid email ลองแล้ว · shots: $SHOT_DIR (login.png, error-500.png)"
```

## Browser automation (e2e/smoke) — use the `mb` CLI

Installed at the user level, so `CHROME_BIN` is auto — **Windows:** Chrome
CDP 9222 is already opened before the pane spawns, so **never call
`mb-start-chrome`** (it falls through to WSL and breaks) — **macOS/Linux:**
run `mb-start-chrome` once per pane the first time.

```bash
mb go "<url>" · mb url · mb shot <file> · mb snap        # navigate / URL / screenshot / a11y tree+coords
mb text "<selector>" · mb click <x> <y>                  # extract text / click (coord จาก mb snap)
mb fill "Email=x" "Password=y" · mb key Enter · mb scroll down 500
mb wait 1000 | selector:.x | networkidle | url:/path
mb js "<code>" · mb logs · mb audit                       # exec JS / console stream / design audit
mb record start demo.webm / stop · mb tab list/new/close
```

### 📸 Screenshot convention (critic pickup)
```bash
SHOT_DIR="$TAKKUB_ARTIFACTS_DIR/screenshots"; mkdir -p "$SHOT_DIR"
mb shot "$SHOT_DIR/login.png"
```
Never use a path relative to the repo (`runtime/exports/...`) — always
state `$SHOT_DIR` in `takkub done` so critic can find it.

**#159 — check before reporting:** a failed capture (blank page / still
loading / browser crash) still produces a file that "exists" but is empty
or abnormally small — check the size every time after capturing, before
`takkub done`:
```bash
for f in "$SHOT_DIR"/*.png; do
  sz=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")
  [ "$sz" -lt 10240 ] && echo "⚠ $f = ${sz}B เล็กผิดปกติ — ถ่ายพลาด, ถ่ายใหม่"
done
```
If you find an abnormally small file → `mb wait networkidle` (or `mb wait
1000`) then `mb shot` again before reporting — the cockpit will also flag
files < 10KB in the evidence for Lead to see, but don't let Lead be the one
to catch it later — catch it at the source.
