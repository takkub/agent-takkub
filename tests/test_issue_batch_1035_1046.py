"""2.2.60 batch: #1035 Git usr\\bin on a Windows pane PATH · #1036 opencode
model through config, not argv · #1046 a closing Lead warns nobody and old
"[role closing]" notices are not replayed into a new session."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_takkub import lead_inbox, pane_env, pane_instructions
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY


# ── #1035 ────────────────────────────────────────────────────────────────
def test_git_usr_bin_is_appended_last_once_and_only_on_windows():
    # Drive-less on purpose: the CI's POSIX runners split PATH on ":".
    usr = r"\Program Files\Git\usr\bin"
    env = {"PATH": os.pathsep.join([r"\Windows\System32", r"\Program Files\Git\cmd"])}
    pane_env.append_git_usr_bin(env, platform="win32", usr_bin=usr)
    parts = env["PATH"].split(os.pathsep)
    assert parts[-1] == usr and parts[0] == r"\Windows\System32"
    pane_env.append_git_usr_bin(env, platform="win32", usr_bin=usr + "\\")
    assert env["PATH"].split(os.pathsep).count(usr) == 1

    mac = {"PATH": "/usr/bin"}
    pane_env.append_git_usr_bin(mac, platform="darwin", usr_bin=usr)
    assert mac == {"PATH": "/usr/bin"}


def test_git_usr_bin_is_found_next_to_the_git_on_path(tmp_path, monkeypatch):
    (tmp_path / "Git" / "cmd").mkdir(parents=True)
    (tmp_path / "Git" / "usr" / "bin").mkdir(parents=True)
    git = tmp_path / "Git" / "cmd" / "git.exe"
    git.write_bytes(b"")
    (tmp_path / "Git" / "usr" / "bin" / "sh.exe").write_bytes(b"")
    monkeypatch.setattr(pane_env, "_git_usr_bin_cache", {})
    monkeypatch.setattr("shutil.which", lambda name, path=None: str(git))
    assert pane_env._git_for_windows_usr_bin("x") == str(
        (tmp_path / "Git" / "usr" / "bin").resolve()
    )


# ── #1036 ────────────────────────────────────────────────────────────────
def test_opencode_model_rides_the_config_env_and_keeps_the_instructions():
    spec = PROVIDER_REGISTRY["opencode"]
    assert spec.model_config_env == "OPENCODE_CONFIG_CONTENT"
    raw = json.dumps({"instructions": ["a.md"]})
    merged = json.loads(pane_instructions.merge_config_value(raw, "model", "opencode-go/x"))
    assert merged == {"instructions": ["a.md"], "model": "opencode-go/x"}
    assert json.loads(pane_instructions.merge_config_value("not json", "model", "m")) == {
        "model": "m"
    }
    # every other provider still takes its model on argv
    assert [n for n, s in PROVIDER_REGISTRY.items() if s.model_config_env] == ["opencode"]


# ── #1046 ────────────────────────────────────────────────────────────────
def _orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch._notify_lead = MagicMock()
    orch._pane_state = {}
    return orch


def test_closing_the_lead_itself_sends_no_subprocess_warning(monkeypatch):
    orch = _orch()
    monkeypatch.setattr(orch, "_live_non_scaffolding_children", lambda *a, **k: ["git.exe"])
    monkeypatch.setattr(orch, "_project_panes", lambda _p: {"lead": SimpleNamespace(state="idle")})
    orch._warn_if_live_children("p", "lead", MagicMock())
    orch._notify_lead.assert_not_called()
    monkeypatch.setattr(
        orch, "_project_panes", lambda _p: {"backend": SimpleNamespace(state="working")}
    )
    orch._warn_if_live_children("p", "backend", MagicMock())
    assert orch._notify_lead.call_args.kwargs["kind"] == "subprocess-kill-warning"


def test_an_old_pane_closing_notice_is_dropped_but_a_fresh_one_is_kept():
    orch = _orch()
    body = "⚠️ [lead closing] 1 subprocess(es) still running under this pane are about to be killed"
    now = time.time()
    assert orch._revalidate_system_notice("p", body, queued_ts=now - 30 * 3600) == ""
    assert orch._revalidate_system_notice("p", body, queued_ts=now - 60) == body
    cleanup = "🧹 [qa closing] Cleaned up 2 child subprocess(es) after completion."
    assert orch._revalidate_system_notice("p", cleanup, queued_ts=now - 2 * 3600) == ""
    done = "[backend done] closing the ticket"
    assert orch._revalidate_system_notice("p", done, queued_ts=now - 30 * 3600) == done
    assert lead_inbox._CLOSING_NOTICE_MAX_AGE_S == 3600.0
