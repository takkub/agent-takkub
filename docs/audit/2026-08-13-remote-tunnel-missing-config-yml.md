# Bug: friend's machine "remote-control ไม่ได้" — no `runtime/tunnel/config.yml`

## Symptom

A friend's machine couldn't get remote-control working; the failure trail
pointed at a missing `runtime/tunnel/config.yml`.

## What was ruled out

`runtime/tunnel/config.yml` is **generated**, never hand-authored or read
as a precondition:

- `tunnel.py::_write_named_config` always writes it fresh, immediately
  before spawning `cloudflared`, and `RUNTIME_DIR / "tunnel"` is created
  with `mkdir(parents=True, exist_ok=True)` right before the write.
- The only place that *reads* a `config.yml` (`settings_dialog.py::
  derive_hostname`) is a best-effort sibling-file peek next to the user's
  chosen credentials `.json`, already guarded with `is_file()` — a missing
  file there just means "no autofill", never an error.
- Every "Mode A requires X" precondition (`credentials_json` present,
  absolute path, valid `TunnelID`) is checked *before* touching the
  filesystem for `config.yml`, and raises a clean `TunnelError`.

So no code path reads a pre-existing `runtime/tunnel/config.yml` and
crashes when it's absent — hypothesis (a) in the task brief (`unchecked
read`) doesn't hold for the current code.

## Root cause (proven)

Two compounding gaps, both real and reachable:

1. **`docs/guides/2026-07-11-headless-docker.md`** told users to bring
   remote-control up on a fresh headless/friend's machine by *copying
   `remote.json` from a desktop install* ("seed the file yourself or copy
   one from a desktop install that has it configured"). If that desktop
   install used **Named tunnel** (`tunnel.type: "cloudflared"`), the copied
   `credentials_json`/`cloudflared_bin` fields are **absolute paths on the
   original machine** — they do not exist on the friend's machine/container.
   `runtime/tunnel/config.yml` itself was never meant to be copied (it's
   regenerated on every start) — but the guide didn't say so, and didn't
   warn that the credential paths need to be re-pointed.

2. **Total silence on tunnel failure.** Given a `credentials_json` that
   doesn't resolve on the new machine, or a `cloudflared` process that
   itself exits immediately (bad/expired cert, tunnel deleted server-side,
   `--config`/`--credentials-file` it can't parse — any reason at all):
   - `tunnel.py`'s `_drain_output` (named-tunnel mode) **discarded**
     cloudflared's entire stdout/stderr with no logging.
   - Nothing checked whether the spawned `cloudflared` process was still
     alive — `_spawn`'s `Popen(...)` only raises if the executable itself
     can't launch, so a same-instant exit looked identical to "started
     fine" all the way up the call chain.
   - `RemoteControl._start()` only catches the *pre-flight* `TunnelError`s
     (missing/unreadable credentials, bad `TunnelID`) and logged them via
     `_log.exception(...)` — never kept the reason anywhere a caller could
     read it.
   - `user_actions.py::_apply_remote_config` reported **Enable = success**
     the instant `RemoteControl.maybe_start()` returned non-`None`, without
     checking whether the tunnel itself (`self._remote._tunnel`) actually
     came up. The user got a pairing URL/QR that would never connect —
     and, on the auto-boot path (headless boot, desktop autostart), even
     less: a bare log line nobody sees.

That combination is what produces "friend's machine can't run
remote-control, and the only trace is something about a missing file deep
in the tunnel machinery" — the file itself isn't literally the crash, but
the class of "Mode-A prerequisite missing on this machine, and the failure
is invisible" is exactly what was reported.

## Fix

- `remote/tunnel.py`: named-tunnel mode now does a short (~0.4s)
  liveness check after spawning `cloudflared`, and captures a bounded tail
  of its stdout/stderr instead of discarding it. If the process already
  exited, `Tunnel.start()` raises `TunnelError` with the captured reason
  (falls back to the exit code if nothing was printed).
- `remote/__init__.py`: `RemoteControl` now keeps `self.tunnel_error: str
  | None` — the message from a `TunnelError` that stopped the tunnel from
  starting — instead of only logging it.
- `user_actions.py::_apply_remote_config`: if a tunnel was actually
  requested (per the same gate `RemoteControl._start()` uses) but never
  came up, the just-started remote-control server is rolled back
  (`stop()`'d) and Enable is reported as **failed**, with a message that
  includes the real reason and points at Settings → 🌐 Remote / Quick
  tunnel as the zero-setup fallback — instead of a silent "success" with a
  dead pairing URL.
- `docs/guides/2026-07-11-headless-docker.md`: the remote-control seeding
  section now explicitly warns that a copied `remote.json`'s Named-tunnel
  paths are host-specific, and recommends switching to `tunnel.type:
  "quick"` (no domain/credentials needed) as the low-friction path for a
  machine that never went through Mode A setup itself.

## Tests added

- `tests/test_remote_tunnel.py::TestNamedTunnelLivenessCheck` — alive
  process doesn't raise; dead process raises with captured output; dead
  process with no output falls back to the exit code.
- `tests/test_remote_scaffold.py::test_named_mode_tunnel_error_is_recorded_not_just_logged`
  — `RemoteControl.tunnel_error` is populated, not just logged.
- `tests/test_remote_chip.py::test_tunnel_failure_rolls_back_and_reports_reason`,
  `::test_quick_mode_tunnel_failure_also_rolls_back` — `_apply_remote_config`
  reports failure + rolls back instead of a false-success pairing URL.

All pre-existing tests in the four touched suites still pass (129/129) —
the note-worthy nuance verified in the passing suite is that `MagicMock()`'s
default (unset) `_tunnel` attribute is itself a `MagicMock`, not `None`, so
none of the pre-existing "successful enable" tests spuriously trip the new
rollback branch; it only fires when `_tunnel` is explicitly `None`.
