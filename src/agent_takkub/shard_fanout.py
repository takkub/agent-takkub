"""Subagent fan-out (#641): ``--shards N`` opens ONE pane that dispatches N
native subagents, instead of N full panes.

Why: every extra pane pays the whole boot bill again — CLI startup, the
role prompt, MCP init, ~0.5 GB RAM, a PTY, cockpit bookkeeping, and a
ShardGroup with its own 45-minute timeout. A native subagent of the pane's
own CLI shares all of that and only costs its own working context.

What stays a pane fan-out (``frontend#1 … #N``), automatically:

* browser-QA shards (``qa``/``critic``/``designer``, i.e. ``reviewer --mode
  e2e|ui``) — the cockpit isolates one browser profile PER SHARD PANE; N
  subagents inside one pane would all drive the parent's single Playwright
  MCP (#92-style collision);
* ``--plan`` (planner pane + orchestrator-driven fan-out on done);
* ``--mode subagent`` (that is the LEAD's own native subagents — a different
  feature, untouched here);
* providers with no native subagent tool (``ProviderSpec.native_subagent_hint``
  empty — kimi/cursor today).

``--fanout pane`` forces the old behaviour; ``--fanout subagent`` forces the
new one and errors instead of silently falling back.

Pure module: no Qt, no orchestrator import — ``cli.py`` (client process) and
the orchestrator both call it.
"""

from __future__ import annotations

FANOUT_MODES: tuple[str, ...] = ("auto", "pane", "subagent")

#: Roles whose ``--shards`` panes each get their own browser profile — a
#: subagent fan-out cannot reproduce that isolation, so they always stay on
#: pane fan-out. Mirrors cli_server's "qa#N/critic#N/designer#N" spawn gap.
BROWSER_SHARD_ROLES: frozenset[str] = frozenset({"qa", "critic", "designer"})

#: Env var stamped on a fan-out pane (``TAKKUB_SUBAGENT_FANOUT=N``) so hooks/
#: guards can tell it apart from an ordinary teammate pane.
ENV_SUBAGENT_FANOUT = "TAKKUB_SUBAGENT_FANOUT"

#: Marker line inside the task text — role prompts point at it as the ONE
#: exception to "never spawn a subagent yourself".
TASK_MARKER = "━━ SUBAGENT FAN-OUT"


def target_base_role(role: str, mode: str | None) -> str:
    """Base role that will actually be dispatched: ``reviewer --mode e2e``
    becomes ``qa``, ``--mode ui`` becomes ``critic`` (#513)."""
    base = (role or "").split("#", 1)[0].strip().lower()
    if base == "reviewer":
        from .routing_planner import _MODE_TO_LEGACY_ROLE

        return _MODE_TO_LEGACY_ROLE.get(mode or "", "reviewer")
    return base


def effective_provider_hint(
    base_role: str, project: str | None, provider_override: str | None
) -> tuple[str, str]:
    """``(effective_provider, native_subagent_hint)`` for *base_role* — the
    hint is ``""`` when that provider has no native subagent tool."""
    from .provider_config import effective_provider_for
    from .provider_spec import PROVIDER_REGISTRY
    from .team_preset import settings_role_for

    provider = (provider_override or "").strip().lower() or None
    if not provider:
        provider = effective_provider_for(settings_role_for(base_role, project), project)
    spec = PROVIDER_REGISTRY.get(provider)
    hint = (getattr(spec, "native_subagent_hint", "") or "").strip() if spec else ""
    return provider, hint


def resolve_shard_fanout(
    role: str,
    shards: int,
    *,
    fanout: str = "auto",
    mode: str | None = None,
    plan: bool = False,
    provider: str | None = None,
    project: str | None = None,
) -> tuple[str, str | None]:
    """Decide how ``--shards N`` runs.

    Returns ``(kind, note)`` with ``kind`` one of:

    * ``"pane"``     — legacy N-pane fan-out (``note`` explains an automatic
      fallback, or is ``None`` when nothing needed saying);
    * ``"subagent"`` — one pane + N native subagents (``note`` names the
      provider and its tool);
    * ``"error"``    — the caller asked ``--fanout subagent`` for a shape that
      cannot do it; ``note`` is the message to show.
    """
    fanout = (fanout or "auto").strip().lower()
    if fanout not in FANOUT_MODES:
        return "error", f"--fanout must be one of {', '.join(FANOUT_MODES)} (got {fanout!r})"
    if shards < 2:
        return "pane", None
    if fanout == "pane":
        return "pane", None
    explicit = fanout == "subagent"
    if mode == "subagent":
        # Lead-side native subagents (`takkub assign --mode subagent --shards N`)
        # — a separate feature; leave its N-capsule registration alone.
        if explicit:
            return "error", (
                "--fanout subagent ใช้กับ --mode subagent ไม่ได้: --mode subagent คือ "
                "subagent ของ Lead เอง (ไม่มี pane) ส่วน --fanout subagent คือ pane ของ role "
                "ยิง subagent ของตัวเอง — เลือกอย่างใดอย่างหนึ่ง"
            )
        return "pane", None
    if plan:
        if explicit:
            return "error", (
                "--plan ใช้กับ --fanout subagent ไม่ได้: --plan คือ planner pane + orchestrator "
                "fan-out เป็น pane ต่อ bucket (browser QA) — ตัด --plan ออก หรือใช้ --fanout pane"
            )
        return "pane", None
    base = target_base_role(role, mode)
    if base in BROWSER_SHARD_ROLES:
        if explicit:
            return "error", (
                f"--fanout subagent ใช้กับ {base} ไม่ได้: shard ของ browser-QA ต้องเป็น pane แยก "
                "(cockpit แยก browser profile ต่อ shard pane; subagent ในpane เดียวจะแย่ง "
                "Playwright MCP ตัวเดียวกัน #92) — ใช้ --fanout pane"
            )
        return "pane", (
            f"{base} เป็น browser-QA shard → fan-out เป็น pane แยก (browser profile ต่อ shard)"
        )
    provider_name, hint = effective_provider_hint(base, project, provider)
    if not hint:
        if explicit:
            return "error", (
                f"--fanout subagent ใช้กับ provider {provider_name!r} ไม่ได้: CLI นี้ไม่มี native "
                "subagent tool ที่ cockpit รู้จัก (มีเฉพาะ claude/codex/gemini-agy/opencode) — "
                "ใช้ --fanout pane หรือ --provider ตัวที่รองรับ"
            )
        return "pane", (
            f"provider {provider_name!r} ไม่มี native subagent → fan-out เป็น {shards} pane แทน"
        )
    return "subagent", f"1 pane + {shards} native subagents ({provider_name}: {hint})"


def direct_instance_assign_error(
    role: str,
    *,
    shard_total: int = 0,
    mode: str | None = None,
    isolation: str = "shared",
    provider: str | None = None,
    project: str | None = None,
) -> str | None:
    """Refuse a hand-typed ``role#N`` pane assign that should have been one
    ``--shards N`` subagent fan-out (#641 round 2).

    Why this exists: shipping the fan-out plus the Lead-prompt rule was NOT
    enough. Measured on prod 2.1.11 — installed 13:44, Lead panes respawned
    13:45 (so they carried the new prompt), and at 14:12 the Lead still
    dispatched ``frontend#2`` and ``frontend#3`` as separate panes,
    ``shard_total=0``, ``assign_subagent_fanout`` count 0. A prompt is a
    suggestion; this is the gate.

    Returns ``None`` (allow) for every shape a subagent fan-out cannot serve:

    * no ``#N`` suffix at all — the ordinary single-pane assign;
    * ``shard_total > 0`` — a REAL fan-out dispatching its own shards
      (``--shards … --fanout pane``, and the ``--plan`` bucket fan-out);
    * ``mode == "subagent"`` — the Lead's own native children, which use
      ``role#N`` as their fan-out key (see ``_register_subagent``);
    * browser-QA roles — they need one browser profile per pane (#92);
    * ``isolation == "worktree"`` — separate branches per piece is a real
      need that subagents inside one pane cannot reproduce;
    * providers with no native subagent tool — nothing to fan out onto.
    """
    base, idx = (role or "").split("#", 1)[0].strip().lower(), None
    if "#" in (role or ""):
        try:
            idx = int(role.split("#", 1)[1])
        except ValueError:
            idx = None
    if idx is None:
        return None
    if shard_total > 0 or mode == "subagent" or isolation == "worktree":
        return None
    if base in BROWSER_SHARD_ROLES:
        return None
    provider_name, hint = effective_provider_hint(base, project, provider)
    if not hint:
        return None
    return (
        f"{role} = เปิด pane ที่ {idx} ของ role เดียวกัน — #641 ปิดทางนี้แล้ว "
        f"(pane ใหม่จ่ายค่า boot + role prompt + tool schema + MCP ซ้ำทั้งก้อน ~36k token/pane "
        f"เทียบกับ subagent ~13.5k)\n"
        f"รวมทุกชิ้นเป็น assign เดียวแทน:\n"
        f'  takkub assign --role {base} --shards <N> "<งานทุกชิ้น แยกหัวข้อ 1./2./3.>"\n'
        f"→ เปิด pane เดียว แล้ว pane ยิง native subagent N ตัวคู่ขนานเอง "
        f"({provider_name}: {hint}) แล้วรายงาน done ครั้งเดียว\n"
        f"ถ้าจำเป็นต้องได้ pane แยกจริงๆ: `--shards <N> --fanout pane` "
        f"(หรือ `--isolation worktree` ถ้าต้องการ branch แยกต่อชิ้น)"
    )


def wrap_subagent_fanout_task(task: str, shards: int, provider_name: str, hint: str) -> str:
    """Append the fan-out contract to *task* — the block the pane acts on.

    The pane starts with the role prompt's "never spawn a subagent" rule; this
    block is the Lead-issued exception the role prompts name. Written so a
    pane that turns out to have NO subagent tool still finishes (sequential
    fallback) instead of stalling — the #641 "อย่าให้ติดขัด" requirement.
    """
    tool = hint or "native subagent tool ของ CLI นี้"
    return (
        f"{task.rstrip()}\n\n"
        f"{TASK_MARKER} {shards} — Lead สั่งให้ pane นี้แตกงานเป็น {shards} subagent ━━\n"
        f"Lead assign งานนี้ด้วย `--shards {shards}` แบบ subagent fan-out (#641): คุณคือ **pane เดียว** "
        f'ของ role นี้ และได้รับอนุญาต (ข้อยกเว้นของกฎ "ห้าม spawn subagent") ให้ใช้ native subagent '
        f"ของ {provider_name} — {tool} — สำหรับงานนี้เท่านั้น\n"
        "\n"
        "ขั้นตอนบังคับ:\n"
        f"1. อ่านงานข้างบนแล้วแบ่งเป็นชิ้นที่ **อิสระต่อกันและไม่แตะไฟล์เดียวกัน** ไม่เกิน {shards} ชิ้น "
        "(แบ่งได้น้อยกว่านั้นก็ใช้เท่าที่แบ่งได้ ห้ามฝืนซอยงานที่ต้องทำต่อกัน)\n"
        "2. เขียนสเปคต่อ subagent ให้ **ครบในตัวเอง**: ไฟล์เป้าหมาย · สิ่งที่ต้องทำ · สิ่งที่ห้ามแตะ · "
        "วิธีเช็คว่าเสร็จ — subagent เริ่มจาก context ว่าง ไม่เห็นข้อความนี้และไม่เห็นไฟล์ที่คุณอ่านไปแล้ว "
        "ใส่ข้อสรุปที่คุณตรวจแล้วลงไปแทนการสั่งให้มันไปอ่านซ้ำ\n"
        "3. ยิง subagent **ทุกตัวพร้อมกันในเทิร์นเดียว** (คู่ขนาน) ห้ามยิงทีละตัวแล้วรอ\n"
        "4. subagent **ห้าม**: ใช้ browser/Playwright MCP · รัน `takkub done`/`takkub progress`/"
        "`takkub send` · git commit/push · spawn subagent ซ้อน — ของพวกนี้เป็นหน้าที่ของคุณ (pane แม่) เท่านั้น\n"
        "5. รวมผลทุกตัว ตรวจว่าเข้ากันได้ (build/lint/test เฉพาะไฟล์ที่แตะ) แล้วทำ screenshot "
        "self-verify ตามกฎ scope ถ้าเป็นงาน UI — ทำหลัง subagent เสร็จหมด ไม่ใช่ระหว่าง\n"
        f"6. `takkub done` **ครั้งเดียว** สรุปครบทุกชิ้น (ชิ้นไหนเสร็จ/ล้ม/ข้าม) — ห้ามให้ subagent รายงาน\n"
        "\n"
        "ถ้า subagent tool ไม่มีให้ใช้จริงหรือใช้แล้ว error ซ้ำ: **ทำทีละชิ้นเองต่อจนจบ** แล้วระบุใน "
        "done ว่า fan-out ไม่ได้เพราะอะไร — ห้ามหยุดรอ ห้ามถาม Lead ว่าจะเอายังไง"
    )
