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
        """V1-authoritative model pin lookup, with the V2 model catalog read
        alongside it purely as a shadow — never as the answer.

        **Why V1 still wins outright (Wave C fix, prod incident 2026-08-23):**
        `migrate apply` copies V1's role/provider model pins into
        `v2/models/{registry,aliases}.json` exactly once. Nothing syncs that
        copy back when the user changes a pin afterwards in Settings ->
        Providers & Roles — those writers (`role_models.py`/
        `provider_models.py`) only ever touch the V1 files (Phase 10's job,
        not this resolver's). So on any machine that has already migrated,
        the V2 copy silently goes stale the moment the user re-pins a model,
        and answering from V2 would make that re-pin take effect nowhere —
        the exact class of silent behavior change this V2 rollout exists to
        avoid. Returning V1 unconditionally makes that impossible by
        construction.

        The V2 read below is kept — not deleted — so this method still
        proves V2-catalog parity against live production data before Phase
        10 flips which side is authoritative: every call where V1 and V2
        disagree emits a `model_pin_v2_drift` event (see `_log_event`
        below), which is the evidence Phase 10 needs that V2 isn't safe to
        promote yet. Read this as "intentionally shadow, not forgotten to
        switch."

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
            self._log_drift(role, provider, v1_value, v2_value, data_home)

        return v1_value

    @staticmethod
    def _log_drift(
        role: str, provider: str, v1_value: str | None, v2_value: str | None, data_home
    ) -> None:
        key = (role, provider)
        if key in _DRIFT_LOGGED:
            return
        _DRIFT_LOGGED.add(key)
        _log.warning(
            "model_pin_v2_drift role=%r provider=%r v1=%r v2=%r data_home=%r — V2 catalog "
            "copy is stale (Settings writes only reach V1); resolved from V1",
            role,
            provider,
            v1_value,
            v2_value,
            str(data_home),
        )
