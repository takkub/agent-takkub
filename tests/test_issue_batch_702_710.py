"""2026-09-22 batch — #702/#704 (quota detector poisoned by the cockpit's own
notices: Lead force-respawned three times in ten minutes), #703 (digested
done reports never cleared `_done_unread` → false "missing"), #705 (stale
`takkub send` records replayed into a fresh assign), #706 (false
idle-at-prompt on a pane babysitting native subagents), #707 (assign-time
git-instruction warning + `worktree add --detach <tmp>` carve-out),
#708 (`takkub tail` rendered through pyte), #709 (Codex duplicate cards
back because `resets_at` drifts per probe), #710 (cloudflared
auto-provision for Quick tunnel).
"""

from __future__ import annotations

import os
import time
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub import cli as cli_mod
from agent_takkub import limit_autoresume as la
from agent_takkub import pane_guard, role_messages
from agent_takkub import provider_usage as pu
from agent_takkub import pty_session as pty
from agent_takkub.lead_inbox import done_roles_in_notice
from agent_takkub.orchestrator_text import _render_pty_tail, defang_quota_markers
from agent_takkub.remote import cloudflared_install as cfi

# ── #704: the cockpit must never poison its own quota detector ──────────────


class TestQuotaFalsePositive:
    LEAD_SCREEN: ClassVar[list[str]] = [
        "● รับทราบ qa ชนโควตา",
        '[system] qa (codex) hit quota ("hit your usage limit") — resets in 5h',
        '{"ts": "2026-09-22T17:42:51", "event": "rate_limit_detected", "marker": "hit your usage limit"}',
        "❯",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]

    def test_notice_and_log_lines_are_not_a_banner(self) -> None:
        text = pty._strip_cockpit_notice_lines(self.LEAD_SCREEN)
        assert "hit your usage limit" not in text
        assert pty._parse_rate_limit_reset(text, time.time(), ("hit your usage limit",)) is None

    def test_real_banner_still_detected(self) -> None:
        screen = ["You've hit your usage limit · resets 3pm", "❯"]
        text = pty._strip_cockpit_notice_lines(screen)
        assert pty._parse_rate_limit_reset(text, time.time(), ("hit your usage limit",))

    def test_defang_rewrites_every_provider_marker(self) -> None:
        out = defang_quota_markers(
            "codex: Rate limit reached · claude: You've hit your usage limit · gemini: individual quota reached"
        )
        low = out.lower()
        for marker in ("rate limit reached", "hit your usage limit", "individual quota reached"):
            assert marker not in low
        assert out.count("[usage-limit banner]") == 3

    def test_defang_leaves_plain_text_alone(self) -> None:
        assert defang_quota_markers("nothing to see here") == "nothing to see here"
        assert defang_quota_markers("") == ""

    def test_output_tail_is_defanged(self) -> None:
        pane = MagicMock()
        pane.session.display_lines.return_value = ["Usage limit reached · resets 3pm", "❯"]
        tail = la._pane_output_tail(pane)
        assert "limit reached" not in tail.lower()

    def test_notify_quota_hit_never_quotes_the_marker(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o._notify_lead = MagicMock()
        pane = MagicMock()
        pane.session.current_model_label.return_value = None
        o._notify_quota_hit("proj", "qa", "codex", time.time() + 3600, "hit your usage limit", pane)
        body = o._notify_lead.call_args[0][1]
        assert "hit your usage limit" not in body
        assert "hit quota" in body

    def test_usage_denies_limit_needs_a_known_figure(self) -> None:
        from agent_takkub.limit_status import LimitWindow, UsageData

        low = UsageData(
            plan="Max",
            windows=[LimitWindow(name="five_hour", utilization=3.0, resets_at=None)],
            extra_usage_enabled=False,
        )
        assert la._usage_denies_limit(low) == (True, 3.0)
        assert la._usage_denies_limit(None) == (False, 0.0)
        unknown = UsageData(
            plan="Max",
            windows=[LimitWindow(name="five_hour", utilization=None, resets_at=None)],
            extra_usage_enabled=False,
        )
        assert la._usage_denies_limit(unknown) == (False, 0.0)

    def test_confirm_verdict_claude_tri_state(self, tmp_path) -> None:
        from agent_takkub.limit_status import LimitWindow, UsageData

        def usage(pct):
            return UsageData(
                plan="Max",
                windows=[LimitWindow(name="five_hour", utilization=pct, resets_at=None)],
                extra_usage_enabled=False,
            )

        with patch.object(la, "fetch_usage_shared", return_value=usage(3.0)):
            assert la.confirm_verdict_for_provider("claude", tmp_path) == ("denied", 3.0)
        with patch.object(la, "fetch_usage_shared", return_value=usage(99.0)):
            assert la.confirm_verdict_for_provider("claude", tmp_path)[0] == "confirmed"
        with patch.object(la, "fetch_usage_shared", side_effect=OSError("offline")):
            assert la.confirm_verdict_for_provider("claude", tmp_path) == ("unknown", 0.0)

    def test_confirm_verdict_codex_uses_provider_probe(self) -> None:
        row = pu.ProviderUsage(provider="codex", status="active", utilization=12.0, plan="plus")
        with patch("agent_takkub.provider_usage.fetch_provider_usage", return_value=row):
            assert la.confirm_verdict_for_provider("codex", None) == ("denied", 12.0)
        err = pu.ProviderUsage(provider="codex", status="error", error="x")
        with patch("agent_takkub.provider_usage.fetch_provider_usage", return_value=err):
            assert la.confirm_verdict_for_provider("codex", None) == ("unknown", 0.0)

    def test_denied_clears_stall_and_arms_latch(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o._pane_state = {}
        o._notify_lead = MagicMock()
        ps = o._ps("proj::lead")
        ps.rate_limited_until = time.time() + 18000
        ps.quota_marker = "hit your usage limit"
        ps.quota_provider = "claude"
        ps.limit_confirm_pending = True
        with patch.object(la, "_log_event") as log:
            o._on_limit_usage_denied("proj", "lead", 3.0)
        assert ps.rate_limited_until == 0.0
        assert ps.quota_false_positive_armed is True
        assert ps.limit_confirm_pending is False
        assert log.call_args[0][0] == "rate_limit_false_positive"
        body = o._notify_lead.call_args[0][1]
        assert "ไม่ได้ชนโควตา" in body
        assert "hit your usage limit" not in body

    def test_armed_latch_skips_redetection_until_text_scrolls_off(self, monkeypatch) -> None:
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o._pane_state = {}
        ps = o._ps("proj::lead")
        ps.quota_false_positive_armed = True
        pane = MagicMock()
        pane.session.is_alive = True
        pane.model.provider_name = "claude"
        pane.session.rate_limit_reset_at.return_value = time.time() + 18000
        assert o._rate_limit_suppressed("proj", "lead", pane, time.time()) is False
        assert ps.rate_limited_until == 0.0
        assert ps.quota_false_positive_armed is True
        pane.session.rate_limit_reset_at.return_value = None
        assert o._rate_limit_suppressed("proj", "lead", pane, time.time()) is False
        assert ps.quota_false_positive_armed is False


# ── #706: background subagents are not "idle at prompt" ─────────────────────


class TestBackgroundWorkEvidence:
    @pytest.mark.parametrize(
        "footer",
        [
            "⏵⏵ bypass permissions on · 1 shell · /tasks to see subagents · esc to interrupt · ← for agents",
            "✻ waiting for 1 background agent to finish\n❯ check subagent status\n← for agents · ↓ to manage",
            "· 2 agents · ← for agents · ↓ to manage",
        ],
    )
    def test_explicit_count_or_wait_is_evidence_without_footer_gate(self, footer: str) -> None:
        assert pty._has_background_work_marker(footer.lower()) is True

    def test_for_agents_alone_is_permanent_chrome_not_evidence(self) -> None:
        assert pty._has_background_work_marker("❯\n← for agents · ↓ to manage") is False

    def test_prose_shell_count_is_not_evidence(self) -> None:
        assert pty._has_background_work_marker("ran 1 shell command\n❯") is False


# ── #703: digested done reports clear the unread marker ─────────────────────


class TestDigestClearsUnread:
    DIGEST = (
        "📬 [Lead Inbox Digest — 3 updates]\n"
        "• [14:50:09 · 12s ago][qa] PASS [ref #641] · no branch · 2 files\n"
        "   ↳ Chromium login reached /th/dashboard\n"
        "• [14:58:49 · 3s ago][devops] done: ตรวจ incident แบบ read-only แล้ว\n"
        "• [15:06:08 · 1s ago][frontend] FAILED: build broke\n"
        "• [CC from backend -> lead]: fyi"
    )

    def test_roles_from_digest_and_raw_tag(self) -> None:
        assert done_roles_in_notice(self.DIGEST) == {"qa", "devops", "frontend"}
        assert done_roles_in_notice("[backend done] all good") == {"backend"}
        assert done_roles_in_notice("• [qa] PASS outside a digest") == set()

    def test_mark_delivered_clears_every_digested_role(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o._done_unread = {
            ("proj", "qa"): 1.0,
            ("proj", "devops"): 1.0,
            ("proj", "frontend"): 1.0,
            ("proj", "backend"): 1.0,
        }
        o._mark_done_notices_delivered("proj", self.DIGEST)
        assert o._done_unread == {("proj", "backend"): 1.0}


# ── #705: stale sends must not follow a new assign ──────────────────────────


class TestStaleSendsRetired:
    def test_abandon_unconfirmed_by_state(self, tmp_path) -> None:
        role_messages.append(
            tmp_path, "p", to_role="fe", from_role="lead", body="old", generation=1
        )
        role_messages.append_queued_no_pane(tmp_path, "p", to_role="fe", from_role="lead", body="q")
        role_messages.append(
            tmp_path, "p", to_role="be", from_role="lead", body="other", generation=1
        )
        n = role_messages.abandon_unconfirmed_for_role(
            tmp_path, "p", "fe", "superseded_by_assign", states=("sent",)
        )
        assert n == 1
        recs = {r["body"]: r for r in role_messages.read(tmp_path, "p")}
        assert recs["old"]["state"] == "abandoned"
        assert recs["old"]["abandoned_reason"] == "superseded_by_assign"
        assert recs["q"]["state"] == "queued_no_pane"
        assert recs["other"]["state"] == "sent"
        assert role_messages.undelivered_in(role_messages.read(tmp_path, "p"), "fe", 2) == []
        assert (
            role_messages.abandon_unconfirmed_for_role(tmp_path, "p", "fe", "dropped_by_lead") == 1
        )
        assert all(r["state"] == "abandoned" for r in role_messages.read(tmp_path, "p", role="fe"))

    def test_assign_notice_counts_superseded_sends(self, tmp_path, monkeypatch) -> None:
        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import Orchestrator

        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        role_messages.append(
            tmp_path, "p", to_role="fe", from_role="lead", body="old", generation=1
        )
        role_messages.append(
            tmp_path, "p", to_role="fe", from_role="lead", body="old2", generation=1
        )
        role_messages.append_queued_no_pane(tmp_path, "p", to_role="fe", from_role="lead", body="q")
        o = Orchestrator.__new__(Orchestrator)
        note = o._queued_no_pane_notice("p", "fe")
        assert "message ค้าง 1" in note
        assert "ยกเลิก 2 ข้อความ" in note
        assert "--drop" in note

    def test_cli_badge_for_new_reasons(self) -> None:
        lines = role_messages.format_for_cli(
            [
                {
                    "ts": 0,
                    "to": "fe",
                    "from": "lead",
                    "body": "x",
                    "state": "abandoned",
                    "abandoned_reason": "superseded_by_assign",
                },
                {
                    "ts": 0,
                    "to": "fe",
                    "from": "lead",
                    "body": "y",
                    "state": "abandoned",
                    "abandoned_reason": "dropped_by_lead",
                },
            ]
        )
        assert "งานเก่า" in lines[0] and "--drop" in lines[1]


# ── #707: git instructions caught at assign time + worktree carve-out ───────


class TestGitTaskWarning:
    def test_names_every_blocked_verb(self) -> None:
        task = "แก้ bug แล้ว git commit ใน web repo (ห้าม push) · ถ้า lockfile เปลี่ยนให้ git checkout -- package-lock.json"
        note = cli_mod._self_commit_isolation_warning(task, "shared")
        assert "#707" in note
        assert "git commit" in note and "git checkout" in note
        assert "git push" not in note  # negated ("ห้าม push")

    def test_boilerplate_prohibition_stays_silent(self) -> None:
        assert (
            cli_mod._self_commit_isolation_warning(
                "ห้าม git commit เอง · Lead จะ git push ให้", "shared"
            )
            == ""
        )
        assert cli_mod._self_commit_isolation_warning("git commit the result", "worktree") == ""

    def test_worktree_add_detach_hint(self) -> None:
        note = cli_mod._self_commit_isolation_warning(
            "git worktree add /tmp/x HEAD แล้ว build", "shared"
        )
        assert "--detach" in note


class TestWorktreeAddDetachCarveOut:
    def test_shapes(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "TEMP", r"C:\Users\x\AppData\Local\Temp" if os.name == "nt" else "/tmp/tt"
        )
        tmp = os.environ["TEMP"]
        ok = pane_guard._worktree_tail_allowed
        assert ok(["list"])
        assert ok(["add", "--detach", os.path.join(tmp, "snap"), "HEAD"])
        assert ok(["add", os.path.join(tmp, "snap"), "--detach"])
        assert not ok(["add", os.path.join(tmp, "snap")])  # no --detach
        assert not ok(["add", "--detach", "-b", "x", os.path.join(tmp, "snap")])
        assert not ok(["add", "--detach", "--force", os.path.join(tmp, "snap")])
        assert not ok(["add", "--detach", "../elsewhere"])
        assert not ok(["add", "--detach", os.path.join(tmp, "a"), "HEAD", "extra"])
        assert not ok(["remove", os.path.join(tmp, "snap")])

    def test_classify_end_to_end(self, monkeypatch) -> None:
        tmp = r"C:\Users\x\AppData\Local\Temp" if os.name == "nt" else "/tmp/tt"
        monkeypatch.setenv("TEMP", tmp)
        monkeypatch.setenv("TMPDIR", tmp)
        shared_cwd = r"C:\proj\api" if os.name == "nt" else "/home/u/proj/api"
        snap = os.path.join(tmp, "snap-123")
        v = pane_guard.classify(f"git worktree add --detach {snap} HEAD", "devops", cwd=shared_cwd)
        assert v.allowed, v.reason
        v2 = pane_guard.classify(f"git worktree add -b feat {snap}", "devops", cwd=shared_cwd)
        assert not v2.allowed and v2.rule == "git_shared_default_deny:worktree"
        v3 = pane_guard.classify(f"git worktree remove {snap}", "devops", cwd=shared_cwd)
        assert not v3.allowed


# ── #708: tail renders like a terminal ──────────────────────────────────────


class TestRenderedTail:
    def test_cursor_jumps_do_not_glue_rows(self) -> None:
        raw = (
            b"\x1b[2J\x1b[1;1Hfirst row here\r\n"
            b"\x1b[3;1Hthird row \xe0\xb8\x97\xe0\xb8\x94\xe0\xb8\xaa\xe0\xb8\xad\xe0\xb8\x9a\r\n"
            b"\x1b[2;1Hsecond row inserted later\x1b[5;1H\xed\x95\x9c\xea\xb5\xad\xec\x96\xb4 row\r\n"
        )
        rows = _render_pty_tail(raw, max_lines=10, cols=60)
        assert rows == [
            "first row here",
            "second row inserted later",
            "third row ทดสอบ",
            "한국어 row",
        ]

    def test_partial_escape_at_start_is_harmless(self) -> None:
        rows = _render_pty_tail(b"[0mtail of cut sequence\r\nnext", max_lines=5)
        assert rows[-1] == "next"

    def test_orchestrator_prefers_live_screen(self, tmp_path) -> None:
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o._resolve_project = lambda p: "proj"
        o.resolve_pane_role = lambda r, p: r
        t = tmp_path / "x.transcript.log"
        t.write_bytes(b"garbage")
        o._find_latest_transcript_path = lambda p, r: t
        pane = MagicMock()
        pane.session.is_alive = True
        pane.session.display_lines.return_value = ["row a   ", "", "row b"]
        o._project_panes = lambda p: {"backend": pane}
        ok, msg, payload = o.tail_role_transcript("backend", lines=5)
        assert ok and msg == "ok (live screen)"
        assert payload["lines"] == ["row a", "row b"]


# ── #709: same-account Codex cards merge despite resets_at drift ────────────


class TestCodexCardMergeTolerance:
    @staticmethod
    def _row(account: str, secondary_pct: float, primary_reset: str, secondary_reset: str):
        return pu.ProviderUsage(
            provider="codex",
            status="active",
            plan="plus",
            utilization=0.0,
            account=account,
            windows=[
                {"name": "primary", "utilization": 0.0, "resets_at": primary_reset},
                {"name": "secondary", "utilization": secondary_pct, "resets_at": secondary_reset},
            ],
        )

    def test_one_second_drift_still_merges(self) -> None:
        rows = [
            self._row("default", 100.0, "2026-09-22T16:39:13+00:00", "2026-09-24T02:26:40+00:00"),
            self._row("monchai500", 31.0, "2026-09-22T16:39:14+00:00", "2026-09-28T03:47:03+00:00"),
            self._row(
                "local (~/.codex)", 100.0, "2026-09-22T16:39:15+00:00", "2026-09-24T02:26:40+00:00"
            ),
            self._row(
                "local (.codex-monchai500)",
                31.0,
                "2026-09-22T16:39:16+00:00",
                "2026-09-28T03:47:03+00:00",
            ),
        ]
        merged = pu._merge_identical_account_rows(rows)
        assert [r.account for r in merged] == [
            "default + local (~/.codex)",
            "monchai500 + local (.codex-monchai500)",
        ]

    def test_large_drift_or_other_utilization_stays_separate(self) -> None:
        a = self._row("a", 31.0, "2026-09-22T16:39:13+00:00", "2026-09-28T03:47:03+00:00")
        b = self._row("b", 31.0, "2026-09-22T16:39:13+00:00", "2026-09-28T03:57:03+00:00")
        assert len(pu._merge_identical_account_rows([a, b])) == 2
        c = self._row("c", 32.0, "2026-09-22T16:39:13+00:00", "2026-09-28T03:47:03+00:00")
        assert len(pu._merge_identical_account_rows([a, c])) == 2


# ── #710: cloudflared provisioning ──────────────────────────────────────────


class TestCloudflaredInstall:
    def test_release_asset_mapping(self) -> None:
        assert cfi.release_asset_for("Windows", "AMD64") == "cloudflared-windows-amd64.exe"
        assert cfi.release_asset_for("Darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
        assert cfi.release_asset_for("Darwin", "x86_64") == "cloudflared-darwin-amd64.tgz"
        assert cfi.release_asset_for("Linux", "x86_64") == "cloudflared-linux-amd64"
        with pytest.raises(cfi.CloudflaredInstallError):
            cfi.release_asset_for("Plan9", "mips")

    def test_resolve_order_and_no_download_by_default(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cfi.shutil, "which", lambda _n: None)
        monkeypatch.setattr(cfi, "install_dir", lambda: tmp_path / "bin")
        assert cfi.resolve_cloudflared("") is None
        explicit = tmp_path / "cf.exe"
        explicit.write_bytes(b"x")
        assert cfi.resolve_cloudflared(str(explicit)) == str(explicit)
        local = cfi.installed_path()
        local.parent.mkdir(parents=True)
        local.write_bytes(b"x")
        assert cfi.resolve_cloudflared("") == str(local)
        called = []
        monkeypatch.setattr(
            cfi, "download_cloudflared", lambda progress=None: called.append(1) or local
        )
        local.unlink()
        assert cfi.resolve_cloudflared("", download=True) == str(local) and called == [1]
