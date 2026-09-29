"""#764: a digested done report must clear `_done_unread` — the real digest
bullet carries a stamp with a trailing space, which the role regex missed and
made the #755 recovery re-send the report ~2 min later."""

import time

from agent_takkub.digest_facts import DigestFacts
from agent_takkub.lead_inbox import _format_digest_item, done_roles_in_notice


def test_stamped_digest_bullets_are_recognised():
    t = time.time()
    for facts in (None, DigestFacts(role="devops")):
        line = _format_digest_item("[devops done] x", t, t + 3, facts=facts)
        body = "📬 [Lead Inbox Digest — 1 update]\n" + line
        assert done_roles_in_notice(body) == {"devops"}
