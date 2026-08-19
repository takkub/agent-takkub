"""redact(text) — strips known credential shapes out of arbitrary text
before it reaches a log line: API keys, OAuth/refresh tokens, Authorization
headers, and the JSON credential fields the file/keychain backends read.
Best-effort: covers the token shapes this codebase's own provider CLIs
actually emit, not a general secret scanner.
"""

from __future__ import annotations

import re

_REDACTED = "***REDACTED***"

# Anthropic keys (sk-ant-...) are tried before the generic sk-... pattern —
# both patterns apply in sequence against the same running `result`, so once
# an sk-ant- match is replaced it can never be re-matched by the looser
# generic pattern.
_SK_ANT = re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}")
_SK_GENERIC = re.compile(r"sk-[A-Za-z0-9]{20,}")
_GITHUB_TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9\-_.=]+")
_JSON_CRED_FIELD = re.compile(
    r'(?i)("(?:access_token|refresh_token|accessToken|refreshToken|'
    r'api_key|apiKey|password|secret|token)"\s*:\s*")[^"]*(")'
)


def redact(text: str) -> str:
    if not text:
        return text
    result = _SK_ANT.sub(_REDACTED, text)
    result = _SK_GENERIC.sub(_REDACTED, result)
    result = _GITHUB_TOKEN.sub(_REDACTED, result)
    result = _BEARER.sub(rf"\1{_REDACTED}", result)
    result = _JSON_CRED_FIELD.sub(rf"\1{_REDACTED}\2", result)
    return result
