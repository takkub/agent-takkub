"""Live probe (#809 follow-up, backlog 7962a2e9; results in
docs/audit/2026-10-07-busy-input-steer-vs-queue.md): what does Enter do while a
provider CLI is mid-turn — steer into the running turn, queue for after it, or
leave a draft? Usage (repo root): python tools/steer_probe.py <provider> <out_dir> -- argv...
e.g. `-- claude --dangerously-skip-permissions`, `-- agy --dangerously-skip-permissions`,
`-- opencode --auto`. Spends a few cents of the provider quota per run."""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

import pyte

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from agent_takkub._pty_backend import spawn_pty_bounded
from agent_takkub.pty_session import _DimAwareScreen, _safe_screen_display

ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")
TASK = (
    "Use your shell tool to run the command `sleep 5` four separate times, strictly one "
    "call at a time (never in parallel). Right after each call finishes, print one line "
    "STEP-n-DONE where n is 1, 2, 3, 4. Do nothing else."
)
STEER = (
    "Change of plan for the remaining steps: after every remaining STEP line also print "
    "a line with the fruit word made of PINE immediately followed by APPLE, in uppercase."
)

provider, out_dir = sys.argv[1], sys.argv[2]
argv = sys.argv[sys.argv.index("--") + 1 :]
argv[0] = shutil.which(argv[0]) or argv[0]
env = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith(("TAKKUB_", "AGENT_TAKKUB", "CLAUDECODE", "CLAUDE_CODE_"))
}
cwd = tempfile.mkdtemp(prefix=f"steer-{provider}-")
cols, rows = 160, 50
screen = _DimAwareScreen(cols, rows)
stream = pyte.ByteStream(screen)
lock = threading.Lock()
raw = bytearray()
t0 = time.monotonic()
events: list[tuple[float, str]] = []


def ev(msg):
    events.append((round(time.monotonic() - t0, 1), msg))
    print(f"[{provider} {events[-1][0]:6.1f}s] {msg}", flush=True)


proc = spawn_pty_bounded(argv, cwd=cwd, env=env, rows=rows, cols=cols, timeout_sec=30)


def reader():
    while True:
        try:
            data = proc.read(65536)
        except Exception:
            return
        if not data:
            continue
        with lock:
            raw.extend(data)
            stream.feed(data)


threading.Thread(target=reader, daemon=True).start()


def text():
    with lock:
        return ANSI.sub(b"", bytes(raw)).decode("utf-8", "replace")


def scr():
    with lock:
        return "\n".join(_safe_screen_display(screen))


def type_and_enter(s):
    proc.write(s)
    time.sleep(1.0)
    proc.write("\r")


def wait_quiet(quiet_s, max_s):
    end = time.monotonic() + max_s
    last, since = None, time.monotonic()
    while time.monotonic() < end:
        cur = scr()
        if cur != last:
            last, since = cur, time.monotonic()
        low = cur.lower()
        if "trust" in low and ("folder" in low or "directory" in low or "workspace" in low):
            sel = [ln.strip() for ln in low.splitlines() if ln.strip()[:1] in ("❯", ">", "›")]
            key = "\x1b[B\r" if any(s.lstrip("❯>› ").startswith("no") for s in sel) else "\r"
            ev(f"trust prompt -> {key!r}\n" + cur.strip()[-600:])
            proc.write(key)
            time.sleep(3)
            since = time.monotonic()
        if time.monotonic() - since >= quiet_s and len(cur.strip()) > 20:
            return True
        time.sleep(0.5)
    return False


wait_quiet(6, 90)
ev("boot settled")
type_and_enter(TASK)
ev("task sent")
end = time.monotonic() + 120
while time.monotonic() < end and "STEP-1-DONE" not in text().replace(" ", ""):
    time.sleep(0.5)
seen1 = "STEP-1-DONE" in text().replace(" ", "")
ev(f"STEP-1 seen={seen1}")
type_and_enter(STEER)
ev("steer sent (Enter)")
time.sleep(3)
ev("screen 3s after steer Enter:\n" + "\n".join(scr().splitlines()[-14:]))

marks = {}
end = time.monotonic() + 240
quiet_since = None
last = None
while time.monotonic() < end:
    t = text().replace(" ", "")
    for k in ("STEP-2-DONE", "STEP-3-DONE", "STEP-4-DONE", "PINEAPPLE"):
        if k not in marks and k in t:
            marks[k] = t.index(k)
            ev(f"first {k} at stream pos {marks[k]}")
    cur = scr()
    if cur != last:
        last, quiet_since = cur, time.monotonic()
    if "STEP-4-DONE" in marks and "PINEAPPLE" in marks and time.monotonic() - quiet_since > 15:
        break
    if "STEP-4-DONE" in marks and time.monotonic() - quiet_since > 40:
        break
    time.sleep(0.5)

p, s4 = marks.get("PINEAPPLE"), marks.get("STEP-4-DONE")
if p is None:
    verdict = "lost-or-draft"
elif s4 is None or p < s4:
    verdict = "steer (applied inside the running turn)"
else:
    verdict = "queue (applied after the turn ended)"
ev(f"VERDICT {verdict}")
final = scr()
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, f"{provider}.json"), "w", encoding="utf-8") as f:
    json.dump(
        {
            "provider": provider,
            "argv": argv,
            "events": events,
            "marks": marks,
            "verdict": verdict,
            "final_screen": final,
        },
        f,
        ensure_ascii=False,
        indent=1,
    )
with open(os.path.join(out_dir, f"{provider}.raw"), "wb") as f:
    f.write(bytes(raw))
try:
    proc.terminate(force=True)
except Exception:
    pass
os._exit(0)
