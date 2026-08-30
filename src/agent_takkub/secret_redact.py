"""Secret redaction for text the cockpit *forwards* (#441).

A pane that runs ``cat .env.prod`` gets the real values in its own tool
output — that part is the CLI's, not ours. What IS ours is every hop the
cockpit itself makes with that text afterwards: the ``takkub done`` note
and its digest, ``takkub send`` bodies, the merge proposal, and the remote
(mobile) mirror that re-reads the Lead transcript. Each of those copies the
value into a second place (Lead transcript, inbox file, phone) the project's
``.gitignore`` never covered — so the value is scrubbed here, at the
cockpit layer, for every provider alike (a model-side rule would only ever
bind on the provider that read it).

Pure, stdlib-only, no I/O — callable from any thread and from the remote
scanner. Two families of match:

* **key = value** lines whose key name looks like a credential
  (``*_SECRET``, ``*PASSWORD``, ``*TOKEN``, ``*API_KEY``, ``*PRIVATE_KEY``,
  ``*_KEY``). The key name is kept and the value becomes
  ``<redacted:KEY_NAME>`` so the line stays debuggable ("which variable was
  it") without carrying the value.
* **bare literals** with a recognisable credential shape (JWT, ``sk-…``,
  ``ghp_``/``github_pat_``, Slack ``xox?-``, AWS ``AKIA…``, PEM private-key
  blocks). These become ``<redacted:jwt>`` etc.

Obvious placeholders (``${VAR}``, ``<paste-here>``, ``xxxx``, ``changeme``,
``...``) are left alone so an example snippet in a task spec still reads as
written.
"""

from __future__ import annotations

import re

REDACTED_FMT = "<redacted:{name}>"

# `KEY=value` / `KEY: value` / `export KEY=value` with a credential-looking
# key. Value may be bare or quoted; a quoted value keeps its quotes so the
# line still parses. Anchored per line so a prose sentence never matches.
_KEY_VALUE_RE = re.compile(
    r"(?im)^(?P<lead>[ \t]*(?:export[ \t]+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*?"
    r"(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|PRIVATE_KEY|_KEY|ACCESS_KEY|CLIENT_SECRET))"
    r"(?P<sep>[ \t]*[=:][ \t]*)"
    r"(?P<q>[\"']?)(?P<val>[^\s\"']{4,})(?P=q)"
)

# Values that are clearly not a real secret — leave them so docs/examples
# survive the pass unchanged.
_PLACEHOLDER_RE = re.compile(
    r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|<[^>]*>|\.{3,}|x{4,}|\*{4,}|changeme|change_me|"
    r"your[-_][a-z_-]+|placeholder|redacted|<redacted:[^>]*>|none|null|true|false)$",
    re.I,
)

_LITERAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PEM block first — it spans lines and would otherwise be chewed up
    # piecemeal by the shorter patterns below.
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip()))


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, names)`` — *names* lists what was scrubbed
    (the env key for key=value hits, the literal family otherwise), in order
    of first appearance, deduplicated. Empty list = nothing touched and the
    text is returned as-is (same object), so callers can branch cheaply.
    """
    if not text:
        return text, []
    names: list[str] = []

    def _remember(name: str) -> None:
        if name not in names:
            names.append(name)

    def _kv(m: re.Match[str]) -> str:
        val = m.group("val")
        if _is_placeholder(val):
            return m.group(0)
        key = m.group("key")
        _remember(key)
        q = m.group("q")
        return f"{m.group('lead')}{key}{m.group('sep')}{q}{REDACTED_FMT.format(name=key)}{q}"

    out = _KEY_VALUE_RE.sub(_kv, text)
    for family, pat in _LITERAL_PATTERNS:

        def _lit(m: re.Match[str], family: str = family) -> str:
            _remember(family)
            return REDACTED_FMT.format(name=family)

        out = pat.sub(_lit, out)
    if not names:
        return text, []
    return out, names


def redact_with_notice(text: str, *, prefix: str = "⚠ ") -> str:
    """`redact_secrets` + a one-line trailer naming what was scrubbed, so the
    reader (Lead, phone) knows the message was altered instead of wondering
    why a value reads ``<redacted:…>``. Unchanged text comes back untouched."""
    out, names = redact_secrets(text)
    if not names:
        return text
    shown = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
    return f"{out}\n{prefix}cockpit redacted {len(names)} secret value(s): {shown} (#441)"
