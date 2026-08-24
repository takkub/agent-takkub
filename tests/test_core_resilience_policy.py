"""core.resilience.policy — `10_FAIL_OPEN_MATRIX.md`'s central service->label
table. Verifies every matrix item from the plan is represented with a valid
label, and that entries claiming breaker wiring line up with what actually
got wired (design_clients + brain_read)."""

from __future__ import annotations

from agent_takkub.core.resilience.policy import POLICY, Label, policy_for

_VALID_LABELS = {Label.FATAL, Label.DEGRADED, Label.OPTIONAL, Label.RETRYABLE, Label.WARNING}

# Every dependency named in `10_FAIL_OPEN_MATRIX.md` (OpenViking excluded —
# withdrawn from the product entirely, see the audit doc) must have an entry.
_EXPECTED_SERVICES = {
    "figma",
    "21st.dev",
    "penpot",
    "storybook",
    "graft",
    "brain_read",
    "git",
    "trace_write",
    "preview",
}


class TestPolicyTable:
    def test_every_expected_service_has_a_policy(self) -> None:
        assert _EXPECTED_SERVICES <= POLICY.keys()

    def test_every_label_is_one_of_the_four_matrix_labels(self) -> None:
        for spec in POLICY.values():
            assert spec.label in _VALID_LABELS

    def test_every_entry_has_a_nonempty_fallback_description(self) -> None:
        for spec in POLICY.values():
            assert spec.fallback

    def test_service_field_matches_its_own_dict_key(self) -> None:
        for key, spec in POLICY.items():
            assert spec.service == key

    def test_policy_for_unknown_service_returns_none(self) -> None:
        assert policy_for("does-not-exist") is None

    def test_policy_for_known_service_matches_table(self) -> None:
        assert policy_for("figma") is POLICY["figma"]

    def test_no_service_is_labeled_fatal(self) -> None:
        # Every entry here is, by construction, an OPTIONAL dependency whose
        # failure must never fail an assignment (plan §0 rules 3+4) — FATAL
        # exists as a label for a future genuinely-required dependency, not
        # for anything in this table today.
        assert all(spec.label != Label.FATAL for spec in POLICY.values())

    def test_breaker_wired_services_match_design_clients_and_brain_read(self) -> None:
        wired = {name for name, spec in POLICY.items() if spec.breaker}
        assert wired == {"figma", "21st.dev", "penpot", "brain_read"}

    def test_non_breaker_entries_document_a_reason(self) -> None:
        for spec in POLICY.values():
            if not spec.breaker:
                assert spec.reason
