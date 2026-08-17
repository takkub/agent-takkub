"""#287 — `pane_guard`'s `pane_poll_loop` rule.

`docs/lead/role-and-workflow.md` has banned hand-rolled pane-polling loops
since #242, and Lead kept writing them anyway, because prose was the only
layer: `lead` sits in `pane_guard._UNGUARDED_ROLES`, so `classify()` returned
`Verdict(True)` before any rule ran. These tests pin the two halves of the
fix — that the rule fires for `lead` specifically, and that it stays narrow
enough not to swallow the polls the docs explicitly bless.
"""

from __future__ import annotations

import pytest

from agent_takkub.pane_guard import POLL_LOOP_RULE_TEXT, classify

# Verbatim from the Lead pane, 2026-08-17, 4m53s into a blocked foreground
# turn while the user had seven queued lines waiting behind it.
OBSERVED_LOOP = (
    'for i in $(seq 1 40); do s=$(takkub list 2>/dev/null | grep -E "^\\s+backend\\s" '
    '| awk \'{print $2}\'); if [ "$s" != "working" ]; then echo "backend: $s (poll $i)"; '
    "break; fi; sleep 20; done; git status --short"
)


class TestPollLoopDenied:
    def test_the_observed_lead_loop_is_denied(self) -> None:
        verdict = classify(OBSERVED_LOOP, "lead")
        assert verdict.allowed is False
        assert verdict.rule.startswith("pane_poll_loop:")

    def test_denial_reason_carries_the_shared_prose(self) -> None:
        """The verdict text is what the pane actually reads, so it has to
        carry the instruction ("จบเทิร์นไปเลย" / `takkub wait`), not just a
        refusal — otherwise the pane retries a variant of the same loop."""
        reason = classify(OBSERVED_LOOP, "lead").reason
        assert POLL_LOOP_RULE_TEXT in reason
        assert "takkub wait" in reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "while takkub status | grep -q working; do sleep 30; done",
            'until [ -z "$(takkub list | grep working)" ]; do sleep 10; done',
            "for i in 1 2 3; do takkub inbox; sleep 5; done",
            "1..40 | ForEach-Object { takkub list; Start-Sleep -Seconds 20 }",
        ],
    )
    def test_loop_shapes(self, cmd: str) -> None:
        assert classify(cmd, "lead").allowed is False

    @pytest.mark.parametrize("role", ["lead", "shell", "devops", "qa#2"])
    def test_applies_to_every_pane_role_including_the_unguarded_ones(self, role: str) -> None:
        """`lead` and `shell` are exempt from every OTHER rule
        (`_UNGUARDED_ROLES`). This one is checked ahead of that exit on
        purpose — Lead is the role that owns teammates to poll, so exempting
        it is exempting the only offender."""
        assert classify(OBSERVED_LOOP, role).allowed is False


class TestPollLoopAllowed:
    def test_unknown_role_fails_open(self) -> None:
        """An empty role means the CLI ran outside a pane — a human at a real
        terminal, which the guard never polices (module docstring)."""
        assert classify(OBSERVED_LOOP, None).allowed is True
        assert classify(OBSERVED_LOOP, "").allowed is True

    def test_one_shot_fanout_over_projects_is_not_a_poll(self) -> None:
        """A loop over `takkub list` with no sleep is a fan-out, not a wait."""
        cmd = "for p in alpha beta gamma; do takkub list --project $p; done"
        assert classify(cmd, "lead").allowed is True

    def test_blessed_non_takkub_verification_poll_survives(self) -> None:
        """docs/lead/patterns.md explicitly endorses curl/healthcheck polling
        for service readiness. Only takkub-status polling is the problem."""
        cmd = "until curl -sf localhost:3000/health; do sleep 2; done"
        assert classify(cmd, "lead").allowed is True

    def test_plain_status_read_without_a_loop_is_allowed(self) -> None:
        assert classify("takkub list", "lead").allowed is True
        assert classify("takkub status && git status --short", "lead").allowed is True

    def test_the_sanctioned_primitive_is_never_blocked(self) -> None:
        """`takkub wait` is the escape hatch the denial text points at — it
        must not itself trip a rule, including when a `sleep` sits nearby."""
        assert classify("takkub wait --role backend --timeout 600", "lead").allowed is True
        assert classify("sleep 5 && takkub wait --role backend", "lead").allowed is True

    def test_naming_the_pattern_in_prose_is_allowed(self) -> None:
        """Writing ABOUT the rule (a task spec, a doc edit) must not be
        denied — same guarantee the browser rule gives for `grep playwright`."""
        cmd = "echo 'do not write: for ... takkub list ... sleep 20 ... done'"
        assert classify(cmd, "lead").allowed is True


class TestHeredocBodies:
    """#287 found this the moment the rule shipped: the bug report documenting
    the loop quotes it verbatim, and creating that issue was denied by the
    rule it described. A heredoc body is data handed to a program, not shell
    the pane runs."""

    ISSUE_BODY = (
        "gh issue create --title x --body \"$(cat <<'EOF'\n"
        "Lead ran this:\n"
        "for i in $(seq 1 40); do s=$(takkub list); "
        'if [ "$s" != working ]; then break; fi; sleep 20; done\n'
        'EOF\n)"'
    )

    def test_quoting_the_loop_in_a_heredoc_is_allowed(self) -> None:
        assert classify(self.ISSUE_BODY, "lead").allowed is True

    def test_heredoc_fed_to_a_shell_is_still_code(self) -> None:
        """`bash <<'EOF' … EOF` genuinely executes its body — exempting it
        would be a one-line bypass of the whole rule."""
        cmd = "bash <<'EOF'\nfor i in $(seq 1 40); do takkub list; sleep 20; done\nEOF"
        assert classify(cmd, "lead").allowed is False

    def test_a_real_loop_outside_a_heredoc_still_denied(self) -> None:
        """Stripping must not swallow code that merely sits near a heredoc."""
        cmd = (
            "cat <<'EOF' > notes.md\nplain notes\nEOF\n"
            "for i in 1 2 3; do takkub status; sleep 9; done"
        )
        assert classify(cmd, "lead").allowed is False
