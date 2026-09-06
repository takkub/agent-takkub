"""Router — the future home of quota/cooldown/health/concurrency-aware
provider routing (REUSE_VS_REWRITE_MATRIX.md §2: "ของเดิมเป็น mapping ไม่ใช่
routing (ไม่มี quota/cooldown/health/concurrency)"). Phase 3 wires exactly
one policy (`StaticRoutingPolicy`) so the router itself is real and
testable today, without changing what it resolves to."""

from __future__ import annotations

import logging

from agent_takkub.core.contracts.routing_policy import RoutingPolicy

from .policy import StaticRoutingPolicy

_log = logging.getLogger(__name__)

# Per-(role, provider) de-dupe for the `model_pin_v2_drift` telemetry event
# below — a spawn-heavy session can call `effective_model_for` for the same
# pair many times, and the drift (once real) doesn't change from call to
# call, so log it once per process rather than once per spawn. Module-level
# by design: a fresh `Router()` is constructed per call site (see
# `core.routing.facade`), so instance state would never accumulate.
_DRIFT_LOGGED: set[tuple[str, str]] = set()


class Router:
    def __init__(self, policy: RoutingPolicy | None = None) -> None:
        self._policy: RoutingPolicy = policy or StaticRoutingPolicy()

    def effective_provider_for(self, role: str, project: str | None = None) -> str:
        return self._policy.resolve(role, project)

    def effective_model_for(
        self, role: str, provider: str, project: str | None = None
    ) -> str | None:
        """Model pin lookup — V1-authoritative when ``TAKKUB_V2_AUTHORITY``
        is off, V2-authoritative when it's on (default since 2.0.0, #362).
        Either way both sides are read and compared; the side NOT chosen is
        only ever a shadow, logged as ``model_pin_v2_drift`` when it
        disagrees with the side that was actually returned.

        **Why the V2 side used to go stale (Wave C fix, prod incident
        2026-08-23) — fixed by Wave 1 dual-write:** `migrate apply` copies
        V1's role/provider model pins into `v2/models/{registry,aliases}.json`
        exactly once; before Wave 1 shipped, nothing synced that copy back
        when the user changed a pin afterwards in Settings -> Providers &
        Roles, so the V2 copy would silently go stale the moment the user
        re-pinned a model. `role_models.py`/`provider_models.py` now
        dual-write every `set_model()` into that same target (Wave 1), so
        the two sides stay in sync going forward — the resolver still reads
        both and lets the flag decide which one is authoritative rather than
        assuming Wave 1 covers every write path.

        The V2 read below is kept regardless of which side wins — every call
        where V1 and V2 disagree emits a `model_pin_v2_drift` event (see
        `_log_drift` below) naming which side was actually returned, so a
        stale mirror (a write path Wave 1 missed) still surfaces as telemetry
        instead of silently drifting.

        Deliberately NOT routed through `RoutingPolicy.resolve()` —
        `core.contracts.routing_policy`'s own docstring flags folding model
        (and effort) into that Protocol as a real widening for a later V2
        phase, not something to do half-way here.

        `project` is accepted (not yet consulted — V1's role/provider model
        pins carry no project scoping either) purely to keep this façade's
        shape consistent with `effective_provider_for`.

        Resolves `storage_layout_v2()`'s data_home ONCE and threads it
        explicitly into both legacy-reader calls below, rather than letting
        each re-resolve its own default independently — the two reads must
        agree on which V2 tree they're looking at (matters for tests, which
        patch this method's own `storage_layout_v2` reference via a late
        import; `core.model_catalog.legacy` binds its own copy at module
        import time and would not see that same patch).
        """
        from agent_takkub.provider_models import model_for as _v1_provider_model_for
        from agent_takkub.role_models import model_for as _v1_role_model_for

        v1_value = _v1_role_model_for(role, provider) or _v1_provider_model_for(provider)

        from agent_takkub.core.storage.layout import storage_layout_v2

        layout = storage_layout_v2()
        data_home = layout.root.parent
        if (
            not (layout.models / "registry.json").exists()
            and not (layout.models / "aliases.json").exists()
        ):
            # Never migrated on this machine — no V2 copy to shadow-compare
            # against yet, so there is nothing to drift from. Not a signal.
            return v1_value

        from agent_takkub.core.model_catalog.legacy import (
            read_legacy_provider_model_pin,
            read_legacy_role_model_pin,
        )

        role_pin = read_legacy_role_model_pin(role, data_home)
        if role_pin is not None and role_pin[0] == provider:
            v2_value = role_pin[1]
        else:
            v2_value = read_legacy_provider_model_pin(provider, data_home)

        if v2_value != v1_value:
            from agent_takkub.core.storage.v2_authority import v2_authority_enabled

            authoritative = v2_value if v2_authority_enabled() else v1_value
            self._log_drift(role, provider, v1_value, v2_value, authoritative, data_home)
            return authoritative

        return v1_value

    @staticmethod
    def _log_drift(
        role: str,
        provider: str,
        v1_value: str | None,
        v2_value: str | None,
        authoritative: str | None,
        data_home,
    ) -> None:
        key = (role, provider)
        if key in _DRIFT_LOGGED:
            return
        _DRIFT_LOGGED.add(key)
        _log.warning(
            "model_pin_v2_drift role=%r provider=%r v1=%r v2=%r authoritative=%r "
            "data_home=%r — sides disagree, resolved from %s",
            role,
            provider,
            v1_value,
            v2_value,
            authoritative,
            data_home,
            "V2" if authoritative == v2_value else "V1",
        )
