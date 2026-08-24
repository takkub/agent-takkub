"""credentials.py — bridges the Setup Wizard's API key to `SecretManager`
and to the env var `openviking-server` resolves at load time.

`06_SECURITY_PORT.md` line 13 requires secrets to go through SecretManager
with redaction, not sit in `ov.conf` as plaintext (see
`docs/audit/2026-08-24-openviking-managed-review.md`, HIGH finding).
Upstream's config format supports `${VAR}` environment-variable
substitution for `api_key` fields — confirmed against the real upstream
docs (github.com/volcengine/OpenViking/blob/main/docs/en/guides/
01-configuration.md, fetched 2026-08-24 for this fix): the DashScope
(`"api_key": "${DASHSCOPE_API_KEY}"`) and Volcengine VLM
(`"api_key": "${VOLCENGINE_API_KEY}"`) examples both demonstrate the
syntax on exactly the two fields this module cares about
(`embedding.dense.api_key`, `vlm.api_key`). So `ov.conf` only ever holds
the literal placeholder `${OPENVIKING_API_KEY}` (`openviking_setup_dialog.
build_ov_conf`); the real value lives in `SecretManager` and is injected
as an env var at spawn time for every `openviking-server` subprocess
invocation (start, doctor) via `subprocess_env()`.
"""

from __future__ import annotations

import os

from ..core.secrets.backends import SecretUnavailableError
from ..core.secrets.manager import SecretManager

# The Setup Wizard's form only collects one API Base/API Key pair, reused
# for both `embedding.dense` and `vlm` (`build_ov_conf`'s own docstring) —
# one secret, one env var is enough to mirror that.
SECRET_REF = "secret://openviking/default"
API_KEY_ENV_VAR = "OPENVIKING_API_KEY"
API_KEY_PLACEHOLDER = "${" + API_KEY_ENV_VAR + "}"


def save_api_key(value: str) -> None:
    SecretManager().set_secret(SECRET_REF, value)


def load_api_key() -> str | None:
    """Best-effort — fail-open like every other OpenViking read path
    (`manager.py`'s own convention): no stored key, or no backend
    available at all, reads as None rather than raising into a caller
    that's just trying to spawn a process."""
    try:
        return SecretManager().get_secret(SECRET_REF)
    except SecretUnavailableError:
        return None


def subprocess_env() -> dict[str, str] | None:
    """`os.environ` plus `API_KEY_ENV_VAR` when a key is stored, for any
    subprocess invocation of `openviking-server` that must resolve
    ov.conf's `${OPENVIKING_API_KEY}` placeholder. None (inherit the
    parent env unchanged, `subprocess`'s own default) when no key was
    ever saved — matches `build_ov_conf` omitting the `api_key` field
    entirely in that case (e.g. `ollama`, which needs no key)."""
    key = load_api_key()
    if key is None:
        return None
    env = os.environ.copy()
    env[API_KEY_ENV_VAR] = key
    return env
