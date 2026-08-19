"""Detects each provider's installed CLI version using the SAME binary-
resolution knowledge `doctor.check_providers()` / `check_provider_auth()`
rely on — `provider_spec.PROVIDER_REGISTRY` + `provider_probe.resolve_provider_bin`
(moved out of doctor.py in this phase into a pure-leaf module, see
`provider_probe.py`'s docstring), so this detector and doctor.py share one
resolver instead of two independently-drifting copies.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_takkub import provider_probe
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@dataclass(frozen=True, slots=True)
class DetectedVersion:
    provider: str
    version_text: str | None
    path: str | None


class ProviderVersionDetector:
    def detect(self, provider: str) -> DetectedVersion:
        spec = PROVIDER_REGISTRY.get(provider)
        if spec is None:
            return DetectedVersion(provider, None, None)
        path = provider_probe.resolve_provider_bin(spec)
        if not path:
            return DetectedVersion(provider, None, None)
        rc, out = provider_probe.run_probe([path, "--version"])
        if rc != 0 or not out:
            return DetectedVersion(provider, None, path)
        version_text = out.splitlines()[0]
        return DetectedVersion(provider, version_text, path)

    def detect_all(self) -> dict[str, DetectedVersion]:
        return {name: self.detect(name) for name in PROVIDER_REGISTRY}
