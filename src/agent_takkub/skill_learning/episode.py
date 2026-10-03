"""Turn one `takkub done` into an Episode the reflector can read.

Provider-neutral evidence, best source first:
  1. `core.conversation.ingest` adapter for the pane's provider (claude /
     codex / gemini / opencode / cursor) — normalized user/assistant turns,
     filtered to the ones after the assignment was delivered;
  2. the cockpit's own PTY transcript (`<role>-<HHMMSS>.transcript.log`,
     every provider has one) rendered through the same pyte path `takkub
     tail` uses.
Everything that leaves this module is secret-redacted.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_MSG_CAP = 4_000  # chars per message
_WINDOW_CAP = 60_000  # chars per episode transcript
_PTY_TAIL_BYTES = 600_000
# TUI chrome in a rendered PTY screen — spinner/status/footer rows that carry
# no work content (#804: "Contemplating… · esc to interrupt · ← for agents").
_PTY_CHROME_RE = re.compile(
    r"esc to (interrupt|stop|cancel)|ctrl[+-]c to|\? for shortcuts|← for agents"
    r"|shift\+tab to cycle|bypass permissions|auto-accept edits|^\s*[─━═]{8,}\s*$"
    # spinner glyph + one word + "…" ("✻ Contemplating…"); not ⏺/● — those
    # prefix real assistant messages.
    r"|^\s*[✻✶✳✢·*◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*\w+…",
    re.IGNORECASE,
)
# A user turn that pushes back — the strongest "a lesson was learned here" cue.
_CORRECTION_RE = re.compile(
    r"\b(no|wrong|don't|stop|actually|instead|not what)\b|ไม่ใช่|ผิด|อย่า|ห้าม|แก้ใหม่|ไม่ได้|ทำไม",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DoneEvent:
    project_ns: str
    role: str
    provider: str
    task: str = ""
    note: str = ""
    failed: bool = False
    cwd: str = ""
    session_id: str = ""
    pty_transcript: str = ""
    # The pane's own CLAUDE_CONFIG_DIR / CLAUDE_CODE_PROJECT_DIR_NAME (#804) —
    # where a claude pane's session JSONL actually lives.
    claude_config_dir: str = ""
    claude_project_dir_name: str = ""
    assigned_at: float = 0.0
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class Episode:
    event: DoneEvent
    transcript: str  # redacted, clipped — what the reflector reads
    message_count: int
    source: str  # "ingest:<provider>" | "pty" | "none"
    corrections: int
    worth: int
    reasons: list[str]

    @property
    def episode_id(self) -> str:
        key = f"{self.event.project_ns}|{self.event.role}|{self.event.ts}"
        return hashlib.sha256(key.encode()).hexdigest()[:10]

    def corpus(self) -> str:
        """Everything a quoted piece of evidence may legitimately come from."""
        return "\n".join((self.event.task, self.event.note, self.transcript))


def _redact(text: str) -> str:
    from ..secret_redact import redact_secrets

    return redact_secrets(text)[0]


def _clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[: cap - 20] + "\n…[ตัดทอน]"


def _claude_locations(ev: DoneEvent) -> list[tuple[str | None, str | None]]:
    """(config_dir, project_dir_name) pairs to try for a claude pane: what
    the live session was spawned with first, then what a pane of this
    project/role would be spawned with now (the pane may be gone already)."""
    out: list[tuple[str | None, str | None]] = []
    if ev.claude_config_dir or ev.claude_project_dir_name:
        out.append((ev.claude_config_dir or None, ev.claude_project_dir_name or None))
    try:
        from .. import pane_env

        env: dict[str, str] = {}
        pane_env.inject_user_profile_env(env, ev.project_ns)
        name = pane_env.claude_project_dir_name(ev.project_ns, ev.role.split("#", 1)[0])
        out.append((env.get("CLAUDE_CONFIG_DIR"), name))
    except Exception:
        pass
    out.append((None, None))
    return out


def _resolve_source(adapter, ev: DoneEvent) -> str | None:
    if ev.provider != "claude":
        return adapter.resolve_source(ev.cwd, ev.session_id or None)
    # Two passes: the exact `<session_id>.jsonl` in ANY location beats the
    # adapter's newest-file guess in an earlier one.
    found_any: list[str] = []
    for config_dir, dir_name in _claude_locations(ev):
        found = adapter.resolve_source(
            ev.cwd, ev.session_id or None, config_dir=config_dir, project_dir_name=dir_name
        )
        if found and (not ev.session_id or Path(found).stem == ev.session_id):
            return found
        if found:
            found_any.append(found)
    return found_any[0] if found_any else None


def _from_ingest(ev: DoneEvent) -> tuple[list[tuple[str, str]], str] | None:
    from ..core.conversation.ingest import adapter_for

    adapter = adapter_for(ev.provider)
    if adapter is None or not ev.cwd:
        return None
    try:
        source = _resolve_source(adapter, ev)
        if not source:
            return None
        batch = adapter.read_new(source, None)
    except Exception:
        return None
    turns: list[tuple[str, str]] = []
    # 60s of slack: provider clocks/record stamps vs the cockpit's delivery stamp.
    since = ev.assigned_at - 60 if ev.assigned_at else 0
    for msg in batch.messages:
        if since and msg.created_at is not None and msg.created_at < since:
            continue
        role = getattr(msg.role, "value", str(msg.role))
        if msg.text and msg.text.strip():
            turns.append((role, msg.text))
    return (turns, f"ingest:{ev.provider}") if turns else None


def _from_pty(ev: DoneEvent) -> str:
    if not ev.pty_transcript:
        return ""
    p = Path(ev.pty_transcript)
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - _PTY_TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return ""
    try:
        from ..orchestrator_text import _render_pty_tail

        lines = _render_pty_tail(raw, max_lines=800)
    except Exception:
        return raw.decode("utf-8", errors="replace")
    return "\n".join(ln for ln in lines if not _PTY_CHROME_RE.search(ln))


def render_turns(turns: list[tuple[str, str]]) -> str:
    """Keep the first user turn (the brief) and as much of the tail as fits —
    the lesson is usually at the end, where the work got fixed."""
    blocks = [f"### {role.upper()}\n{_clip(text, _MSG_CAP)}" for role, text in turns]
    if not blocks:
        return ""
    head = blocks[0]
    tail: list[str] = []
    used = len(head)
    for b in reversed(blocks[1:]):
        if used + len(b) > _WINDOW_CAP:
            tail.append("…[ข้าม turn ช่วงกลาง]")
            break
        tail.append(b)
        used += len(b)
    return "\n\n".join([head, *reversed(tail)])


def score(ev: DoneEvent, message_count: int, corrections: int) -> tuple[int, list[str]]:
    """Cheap, deterministic "is there a lesson in here?" gate — reflection
    costs a model call, a one-line typo fix must not pay it."""
    worth, reasons = 0, []
    if ev.failed:
        worth += 3
        reasons.append("failed")
    if corrections:
        worth += min(4, corrections * 2)
        reasons.append(f"corrections={corrections}")
    if message_count >= 12:
        worth += 2
        reasons.append(f"turns={message_count}")
    elif message_count >= 6:
        worth += 1
        reasons.append(f"turns={message_count}")
    if len(ev.note) >= 400:
        worth += 1
        reasons.append("long-note")
    if re.search(
        r"root cause|สาเหตุ|เพราะ|gotcha|trap|กับดัก|workaround|lesson|บทเรียน", ev.note, re.I
    ):
        worth += 2
        reasons.append("lesson-words")
    return worth, reasons


def build(ev: DoneEvent) -> Episode:
    got = _from_ingest(ev)
    if got is not None:
        turns, source = got
        user_turns = [t for r, t in turns[1:] if r == "user"]
        corrections = sum(1 for t in user_turns if _CORRECTION_RE.search(t[:400]))
        transcript = render_turns(turns)
        count = len(turns)
    else:
        pty = _from_pty(ev)
        source = "pty" if pty else "none"
        transcript = _clip(pty, _WINDOW_CAP) if pty else ""
        count = transcript.count("\n") // 8  # rough turn estimate for scoring only
        corrections = 0
    worth, reasons = score(ev, count, corrections)
    return Episode(
        event=ev,
        transcript=_redact(transcript),
        message_count=count,
        source=source,
        corrections=corrections,
        worth=worth,
        reasons=reasons,
    )
