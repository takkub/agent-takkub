"""#763: a long Thai task pasted into codex sat unsubmitted in the composer
while `takkub status` said "working". Two gaps: the composer-presence check
never matched inline Thai text (payload-head ESC[200~ + pyte dropping combining
marks), and the display state trusted the dispatch ledger."""

import types

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.orchestrator_text import _paste_payload
from agent_takkub.pty_session import _input_has_content

TASK = "ใบงานทดสอบ: อ่านไฟล์ README แล้วสรุปเป็นภาษาไทยสั้นๆ " * 20


def test_inline_thai_draft_matches_despite_dropped_marks_and_paste_prefix():
    # what pyte shows for codex's composer: ์ ็ ั dropped
    region = "› ใบงานทดสอบ: อ่านไฟล README แล้วสรุปเปนภาษาไทยสนๆ".lower()
    assert _input_has_content(region, _paste_payload(TASK)[:120])


def test_idle_composer_is_not_pending():
    assert not _input_has_content("› ask codex to do anything", _paste_payload(TASK)[:120])


class _Sess:
    def __init__(self, pending, busy=False, quiet=30.0):
        self._p, self._b, self._q = pending, busy, quiet

    def shows_pending_input(self, fragment=""):
        return self._p

    def shows_busy_marker(self, provider=None):
        return self._b

    def seconds_since_output(self):
        return self._q

    def account_pending_reason(self, p):
        return None

    def auth_failure_reason(self, p):
        return None

    def tool_running_marker(self, p):
        return None

    def shows_boot_phase_marker(self, **kw):
        return False


def _display(sess):
    pane = types.SimpleNamespace(
        state="working", session=sess, model=types.SimpleNamespace(provider_name="codex")
    )
    return Orchestrator._derive_display_state(object(), pane, "working", False)


def test_working_with_stuck_draft_is_not_reported_working():
    assert _display(_Sess(pending=True)) == "stuck:composer"


def test_working_with_busy_marker_or_recent_output_or_empty_composer_unchanged():
    assert _display(_Sess(pending=True, busy=True)) == "working"
    assert _display(_Sess(pending=True, quiet=1.0)) == "working"
    assert _display(_Sess(pending=False)) == "working"
