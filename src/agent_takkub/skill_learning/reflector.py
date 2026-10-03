"""REF — read one episode, compare against the library, PROPOSE changes.

The reflector never writes skills: it gets a read-only working dir holding
one bundle `.md` (the cockpit's handoff policy — a short pointer, details in
a file), answers with JSON intents on stdout, and `promoter` is the only
writer. Any provider that can run one-shot and read a file works; which one
runs is a policy decision (`settings.provider`), not a code path.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .. import config
from .._win_console import SUBPROCESS_NO_WINDOW
from . import store
from .episode import Episode
from .settings import REFLECTOR_PROVIDERS, LearningSettings

CHILD_ENV = "TAKKUB_SKILL_LEARNER"
DESC_INDEX_CUT = 80
DESC_MAX = 300
BODY_MAX_LINES = 30

FORMAT_SPEC = f"""\
## รูปแบบคำตอบ (บังคับ)

ตอบเป็น JSON object เดียวเท่านั้น ห้ามมีข้อความอื่นนอก JSON:

```json
{{"intents": [
  {{"op": "create", "layer": "project", "name": "kebab-case-name", "category": "testing",
    "description": "Use when <trigger> — <what it gives>", "body": "markdown กฎ/ขั้นตอน",
    "reason": "ทำไมควรจำ", "evidence": "ข้อความที่ copy มาจาก episode ตรงตัว"}},
  {{"op": "patch", "name": "existing-skill", "description": "(ไม่บังคับ)", "body": "body ใหม่ทั้งก้อน",
    "reason": "...", "evidence": "..."}},
  {{"op": "merge", "name": "skill-ที่ถูกรวม", "into": "umbrella-skill", "body": "body ใหม่ของ umbrella",
    "description": "(ไม่บังคับ)", "reason": "...", "evidence": "..."}},
  {{"op": "archive", "name": "skill-ที่ผิด/ล้าสมัย", "reason": "...", "evidence": "..."}}
]}}
```

- ไม่มีอะไรควรจำ → `{{"intents": []}}` (คำตอบที่ถูกต้องบ่อยที่สุด)
- `layer`: `project` = เฉพาะ repo นี้ · `global` = เทคนิคที่ใช้ได้ทุกโปรเจกต์
- `name`: a-z 0-9 และ `-` ยาว 2–64 · ห้ามซ้ำกับ skill ที่มีอยู่ (ทั้งของเราและของ user)
- `description` ≤ {DESC_MAX} ตัว และ **trigger ต้องอยู่ใน {DESC_INDEX_CUT} ตัวแรก** (index ตัดตรงนั้น)
- `body` ≤ {BODY_MAX_LINES} บรรทัดที่ไม่ว่าง — เป็น "กฎที่ทำตามได้" ไม่ใช่เล่าเรื่อง session
- `evidence` ต้อง copy มาจาก episode **ตรงตัว** อย่างน้อย 20 ตัวอักษร (ระบบตรวจ ถ้าไม่เจอ = ปฏิเสธ)
- patch / merge / archive ทำได้เฉพาะ skill ในหัวข้อ "Skills ที่ระบบเรียนรู้ไว้"
- ถ้าบทเรียนใหม่ขัดกับ skill เก่า ต้อง patch ตัวเก่าในคำตอบเดียวกัน ห้ามปล่อยให้ขัดกันเอง
- ห้ามใส่ secret/token/password/URL ภายใน
"""

INSTRUCTION = """\
# Skill Learning — reflection pass

คุณคือ reflector ของ agent-takkub cockpit อ่าน episode ด้านล่าง (งานจริงที่ pane เพิ่งจบ)
แล้วตัดสินว่ามี **บทเรียนระดับ class ที่ใช้ซ้ำได้** ไหม — เช่น กับดักของเครื่องมือ, ขั้นตอนที่ต้องทำ
ตามลำดับ, คำสั่งที่ถูกต้องของโปรเจกต์, สิ่งที่ user แก้กลับมา

หลักการ:
1. **เทียบก่อนเสมอ** — ถ้ามี skill ที่ครอบคลุมเรื่องนี้แล้ว ให้ patch/merge แทนการสร้างใหม่
2. จำเฉพาะสิ่งที่ agent คนถัดไป **ไม่มีทางรู้เอง** จากการอ่านโค้ด — ไม่ใช่สรุปว่างานนี้ทำอะไร
3. ห้ามซ้ำกับกฎกลางที่มีอยู่แล้ว (CLAUDE.md / docs/lead) — ระบบนี้เสริม ไม่แทน
4. ไม่แน่ใจ = ไม่สร้าง ห้องสมุดเล็กที่ถูกต้องดีกว่าใหญ่ที่รก
5. ห้ามแก้ไฟล์ใดๆ ห้ามรันคำสั่ง — แค่ตอบ JSON
"""


# ── bundle ───────────────────────────────────────────────────────────────


def _redact(text: str) -> str:
    from ..secret_redact import redact_secrets

    return redact_secrets(text or "")[0]


def library_section(project_ns: str) -> str:
    managed = store.list_skills(project_ns)
    foreign = [r for r in store.list_skills(project_ns, managed_only=False) if not r.sidecar]
    parts = ["## Skills ที่ระบบเรียนรู้ไว้ (แก้ได้)\n"]
    if managed:
        for r in managed:
            parts.append(
                f"### {r.name} [{r.layer} · {r.category}]\n"
                f"description: {r.description}\n\n{r.body}\n"
            )
    else:
        parts.append("(ยังไม่มี)\n")
    parts.append("\n## Skills อื่นที่มีอยู่ (ของ user/ติดตั้ง — ห้ามแตะ ห้ามตั้งชื่อซ้ำ)\n")
    parts.append("\n".join(f"- {r.name}: {r.description[:120]}" for r in foreign) or "(ไม่มี)")
    return "\n".join(parts)


def build_bundle(ep: Episode) -> str:
    ev = ep.event
    header = (
        f"## Episode\n\n- project: `{ev.project_ns}` · role: `{ev.role}` · provider: `{ev.provider}`\n"
        f"- ผลลัพธ์: {'FAILED' if ev.failed else 'done'} · สัญญาณ: {', '.join(ep.reasons) or '-'}\n"
        f"- แหล่ง transcript: {ep.source}\n"
    )
    return "\n\n".join(
        [
            INSTRUCTION,
            header,
            "### งานที่ได้รับมอบหมาย\n\n" + _redact(ev.task)[:6000],
            "### done note\n\n" + _redact(ev.note)[:6000],
            "### Transcript (redacted)\n\n" + (ep.transcript or "(ไม่มี)"),
            library_section(ev.project_ns),
            FORMAT_SPEC,
        ]
    )


def build_curator_bundle(project_ns: str) -> str:
    return "\n\n".join(
        [
            "# Skill Learning — curator pass\n\n"
            "อ่านห้องสมุด skill ที่ระบบเรียนรู้ไว้ทั้งก้อน แล้วรวม skill ที่ซ้ำ/แคบเกินไปเข้าเป็น umbrella "
            "ระดับ class (op=merge / patch) และ archive ตัวที่ขัดกับตัวอื่นหรือล้าสมัย · ถ้าห้องสมุดดีอยู่แล้ว "
            'ตอบ `{"intents": []}` · evidence ให้ copy จาก body ของ skill ที่อ้างถึงตรงตัว · ห้ามแก้ไฟล์ '
            "ห้ามรันคำสั่ง",
            library_section(project_ns),
            FORMAT_SPEC,
        ]
    )


# ── provider choice + exec ───────────────────────────────────────────────


def _binary(provider: str) -> str | None:
    try:
        if provider == "claude":
            exe = config.find_claude_executable()
            return exe if exe and (Path(exe).exists() or shutil.which(exe)) else None
        if provider == "codex":
            from ..codex_helper import find_codex_executable

            exe = find_codex_executable()
            if exe and sys.platform == "win32" and Path(exe).suffix.lower() in {".cmd", ".bat"}:
                return None  # prompts can't be passed through a .cmd shim safely
            return exe
        if provider == "opencode":
            from ..opencode_helper import find_opencode_executable

            return find_opencode_executable()
        if provider == "cursor":
            from ..cursor_helper import find_cursor_executable

            return find_cursor_executable()
    except Exception:
        return None
    return None


def provider_ready(provider: str) -> bool:
    try:
        from .. import provider_state

        if provider_state.is_disabled(provider) or not provider_state.is_quota_ready(provider):
            return False
    except Exception:
        pass
    return _binary(provider) is not None


# provider → monotonic time until which it is skipped after a failed run
# (unreachable proxy, expired login, rate limit). Per process, in memory:
# a cockpit restart retries everything.
_FAIL_COOLDOWN_S = 1800.0
_failed_until: dict[str, float] = {}


def _cooling(provider: str) -> bool:
    return time.monotonic() < _failed_until.get(provider, 0.0)


def _switch_policy(project_ns: str) -> tuple[bool, set[str]]:
    """(park, excluded) from the owner's quota-reroute policy (#791/#798).
    `park` = work must not move to another provider; `excluded` = providers
    the owner never wants work routed to. Unreadable policy = no limits."""
    try:
        from .. import auto_resume

        project = project_ns or None
        park = auto_resume.effective_quota_policy(project) == auto_resume.QUOTA_POLICY_PARK
        return park, set(auto_resume.effective_quota_exclude_providers(project))
    except Exception:
        return False, set()


def candidates(s: LearningSettings, *, prefer: str = "", project_ns: str = "") -> list[str]:
    """Providers to try, in order. Pinned → just that one (the user chose it
    explicitly). Auto → the pane's own provider first (its account is in use
    and warm), then the fixed order — minus what the owner's quota policy
    forbids (#803): an excluded provider is never tried, and under `park`
    nothing beyond the pane's own provider is (claude when there is none).
    Providers that just failed are tried last, not dropped — on a machine
    where only they work, a stale failure must not block learning."""
    if s.provider != "auto":
        return [s.provider] if provider_ready(s.provider) else []
    park, excluded = _switch_policy(project_ns)
    order = list(REFLECTOR_PROVIDERS)
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    order = [p for p in order if p not in excluded]
    if park:
        home = prefer if prefer in REFLECTOR_PROVIDERS else REFLECTOR_PROVIDERS[0]
        order = [p for p in order if p == home]
    ready = [p for p in order if provider_ready(p)]
    return [p for p in ready if not _cooling(p)] + [p for p in ready if _cooling(p)]


def choose_provider(s: LearningSettings, *, prefer: str = "", project_ns: str = "") -> str | None:
    found = candidates(s, prefer=prefer, project_ns=project_ns)
    return found[0] if found else None


_REASONS: tuple[tuple[str, str], ...] = (
    (
        r"unrecognized_model|model[^\n]{0,40}(not found|not exist|invalid|unknown)",
        "unrecognized_model",
    ),
    (r"usage limit|quota|rate.?limit|429|too many requests|credit balance", "quota"),
    (r"timed out", "timeout"),
    (r"could not start", "not started"),
    (r"econnrefused|connection refused|enotfound|getaddrinfo|network", "connection"),
    (r"401|403|unauthori[sz]ed|not logged in|/login|authenticat|api key", "auth"),
    (r"reply had no JSON intents", "no JSON"),
)


def short_reason(error: str) -> str:
    """One word for why a reflector run failed — the notice names every
    provider tried (`claude: unrecognized_model · codex: quota`), not just
    the last one (#803)."""
    for pattern, label in _REASONS:
        if re.search(pattern, error or "", re.IGNORECASE):
            return label
    tail = " ".join((error or "").split())
    return tail[-60:] or "error"


def build_argv(provider: str, binary: str, prompt: str, model: str) -> list[str]:
    if provider == "claude":
        argv = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--disallowedTools",
            "Bash",
            "Edit",
            "Write",
            "NotebookEdit",
            "WebFetch",
        ]
        return argv + (["--model", model] if model else [])
    if provider == "codex":
        # `-` = read the prompt from stdin (the bundle rides in it).
        argv = [binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
        return argv + (["--model", model] if model else []) + ["-"]
    if provider == "opencode":
        return [binary, *(["-m", model] if model else []), "run", prompt]
    if provider == "cursor":
        return [binary, *(["--model", model] if model else []), "--print", prompt]
    raise ValueError(f"provider {provider!r} cannot reflect")


# (argv, cwd, timeout, env, stdin_text | None) → (ok, stdout_or_error)
Runner = Callable[[list[str], Path, float, dict[str, str], str | None], tuple[bool, str]]

# Providers that take the whole bundle on stdin, so no file-read tool is
# needed at all. Live 2026-10-02 (Windows): codex's read-only sandbox can't
# spawn the shell it reads files with (ShellExecuteExW 1223) — a file
# pointer silently got "cannot read episode.md" back. claude `-p` appends
# stdin to the prompt; `codex exec -` reads the prompt from stdin.
# opencode/cursor keep the `.md` pointer (verified: opencode reads it).
STDIN_PROVIDERS = frozenset({"claude", "codex"})


def reflector_env(provider: str, project_ns: str) -> dict[str, str]:
    """The SAME env a real pane of *provider* gets on this machine
    (`pane_env`): the allowlisted base env, the project's chosen account
    (`CLAUDE_CONFIG_DIR` / provider home) — never the cockpit's raw
    `os.environ`. A cockpit launched from a terminal that itself runs inside
    Claude Code inherits that parent's bridge/session vars, and a raw-env
    `claude -p` then talks to the parent's dead socket (ECONNREFUSED, seen
    live 2026-10-02); an env-leaked API key would likewise bill a different
    account than the panes use. Building it like a pane makes the reflector
    authenticate exactly as the panes do, on any machine."""
    from .. import pane_env

    env = pane_env._build_pane_env(project_ns)
    if provider == "claude":
        pane_env.inject_user_profile_env(env, project_ns)
    else:
        pane_env.inject_provider_home_env(env, provider, project_ns)
    try:
        pane_env.inject_provider_no_autoupdate_env(env, provider)
    except Exception:
        pass
    env[CHILD_ENV] = "1"
    return env


def _subprocess_runner(
    argv: list[str], cwd: Path, timeout: float, env: dict[str, str], stdin_text: str | None
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            input=stdin_text if stdin_text is not None else "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(timeout)}s"
    except OSError as exc:
        return False, f"could not start: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[-800:]
    return True, proc.stdout or ""


def parse_intents(text: str) -> list[dict] | None:
    """The JSON object out of a model reply — fenced or bare. None = no
    parseable object (a failed run, distinct from a valid empty list)."""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("intents"), list):
            return [i for i in data["intents"] if isinstance(i, dict)]
    return None


def run(
    bundle: str,
    run_dir: Path,
    s: LearningSettings,
    *,
    prefer: str = "",
    runner: Runner | None = None,
    project_ns: str = "",
) -> tuple[str | None, list[dict] | None, str]:
    """Write the bundle, then try each candidate provider until one returns
    parseable intents — a machine where claude points at a dead proxy (or
    codex's login expired) still learns through whatever else works there.
    Returns (provider, intents_or_None, raw_or_error); on failure *provider*
    is every provider tried with a short reason each
    (`claude: unrecognized_model · codex: quota`)."""
    order = candidates(s, prefer=prefer, project_ns=project_ns)
    if not order:
        return None, None, "no reflector provider is installed, enabled, allowed and within quota"
    run_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = run_dir / "episode.md"
    store.atomic_write_text(bundle_path, bundle)
    pointer_prompt = (
        f'อ่านไฟล์นี้: "{bundle_path}" แล้วทำตามคำสั่งในไฟล์ — '
        "ตอบเป็น JSON object เดียวตามรูปแบบในไฟล์ ห้ามแก้ไฟล์ใดๆ"
    )
    stdin_prompt = (
        "ทำตามคำสั่งในเอกสารที่ส่งมาทาง input นี้ — ตอบเป็น JSON object เดียวตามรูปแบบในเอกสาร "
        "ห้ามแก้ไฟล์ใดๆ ห้ามรันคำสั่ง"
    )
    errors: list[str] = []
    tried: list[str] = []
    for provider in order:
        # A model name is provider-specific: only honoured when pinned.
        model = s.model if s.provider == provider else ""
        via_stdin = provider in STDIN_PROVIDERS
        prompt = stdin_prompt if via_stdin else pointer_prompt
        stdin_text = (
            (prompt + "\n\n" + bundle if provider == "codex" else bundle) if via_stdin else None
        )
        argv = build_argv(provider, _binary(provider) or provider, prompt, model)
        env = reflector_env(provider, project_ns)
        ok, out = (runner or _subprocess_runner)(argv, run_dir, float(s.timeout_s), env, stdin_text)
        store.atomic_write_text(run_dir / f"reply-{provider}.txt", out or "")
        intents = parse_intents(out) if ok else None
        if intents is not None:
            _failed_until.pop(provider, None)
            return provider, intents, out
        _failed_until[provider] = time.monotonic() + _FAIL_COOLDOWN_S
        why = (out or "").strip()[-300:] if not ok else "reply had no JSON intents"
        errors.append(f"{provider}: {why}")
        tried.append(f"{provider}: {short_reason(why)}")
    return " · ".join(tried), None, " | ".join(errors)
