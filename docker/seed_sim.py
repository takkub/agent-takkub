"""First-boot seeding for the `takkub-sim` sandbox container — registers the
demo project and enables remote-control, both idempotently (never overwrites
state a later boot or a human already changed). Run once by
docker/entrypoint-sim.sh before `agent-takkub-headless` starts.

Deliberately NOT part of the installed package (`src/agent_takkub/`): this is
sandbox bootstrap, not cockpit behavior, so it stays a plain script invoked
from the image's docker/ dir — see
docs/guides/2026-08-31-docker-sandbox.md.
"""

from __future__ import annotations

import os
import secrets
import sys

DEMO_PROJECT_NAME = "sandbox-demo"
DEMO_PROJECT_PATH = "/projects/demo"

# remote/http_server.py's start_server() binds 127.0.0.1 ONLY, by design (it
# assumes a co-located tunnel process on the desktop build, not a published
# Docker port reachable from outside the container's network namespace — see
# entrypoint-sim.sh, which relays the container's externally-published 8899
# to this loopback port via socat). Never point this at the same 8899 the
# compose file publishes — the two ports must stay distinct or socat and the
# HTTP server fight over the bind.
INTERNAL_BIND_PORT = 18899


def _seed_project() -> None:
    from agent_takkub import config

    data = config.load_projects()
    projects = data.setdefault("projects", {})
    if DEMO_PROJECT_NAME in projects:
        print(
            f"seed_sim: project '{DEMO_PROJECT_NAME}' already registered — skipping",
            file=sys.stderr,
        )
        return
    if not os.path.isdir(DEMO_PROJECT_PATH):
        print(
            f"seed_sim: {DEMO_PROJECT_PATH} not found — is ./volumes/projects mounted? skipping",
            file=sys.stderr,
        )
        return
    projects[DEMO_PROJECT_NAME] = {
        "description": "agent-takkub Docker sandbox demo project",
        "paths": {"main": DEMO_PROJECT_PATH},
        "presets": [],
    }
    if not data.get("active"):
        data["active"] = DEMO_PROJECT_NAME
    config.save_projects_json(data)
    print(
        f"seed_sim: registered project '{DEMO_PROJECT_NAME}' at {DEMO_PROJECT_PATH}",
        file=sys.stderr,
    )


def _seed_claude_onboarding() -> None:
    """Pre-answer claude's two interactive first-launch gates: the
    theme/onboarding wizard, and the per-directory "do you trust this
    folder?" prompt for DEMO_PROJECT_PATH. With no TTY input, a headless
    Lead pane spawned before either has been answered once just sits there
    forever — confirmed by a real hang in this image (#457): the pty
    transcript stayed empty until a `takkub send` happened to trigger a
    repaint, which revealed the trust dialog underneath (claude itself had
    already written a partial .claude.json with a real oauthAccount by
    then, proving the seeded OAuth token IS valid — this isn't an auth
    problem). Only ever adds the two missing flags; never touches any
    other key a real login/session has written.
    """
    import json

    from agent_takkub import config

    claude_config_dir = config.default_claude_config_dir()
    claude_json = claude_config_dir / ".claude.json"
    if claude_json.exists():
        data = json.loads(claude_json.read_text(encoding="utf-8") or "{}")
    else:
        claude_config_dir.mkdir(parents=True, exist_ok=True)
        data = {}

    changed = False
    if not data.get("hasCompletedOnboarding"):
        data["hasCompletedOnboarding"] = True
        changed = True

    projects = data.setdefault("projects", {})
    demo_entry = projects.setdefault(DEMO_PROJECT_PATH, {})
    if not demo_entry.get("hasTrustDialogAccepted"):
        demo_entry["hasTrustDialogAccepted"] = True
        changed = True

    if changed:
        claude_json.write_text(json.dumps(data), encoding="utf-8")
        print(
            f"seed_sim: pre-answered claude onboarding + trust dialog at {claude_json}",
            file=sys.stderr,
        )


# Passwords this sandbox shipped or documented at some point — public
# knowledge, so never acceptable.
_KNOWN_PASSWORDS = frozenset(
    {"sandbox", "change-me", "changeme", "password", "replace-with-your-own-password"}
)
_MIN_PASSWORD_LEN = 8


def _required_password() -> str:
    """The operator's own TAKKUB_SIM_PASSWORD, or exit: the sandbox used to
    fall back to the well-known ``sandbox`` on a remote-control endpoint."""
    password = os.environ.get("TAKKUB_SIM_PASSWORD", "").strip()
    problem = ""
    if not password:
        problem = "ยังไม่ได้ตั้ง"
    elif password.lower() in _KNOWN_PASSWORDS:
        problem = "เป็นค่าตัวอย่างที่ใครก็รู้"
    elif len(password) < _MIN_PASSWORD_LEN:
        problem = f"สั้นกว่า {_MIN_PASSWORD_LEN} ตัวอักษร"
    if problem:
        print(
            f"seed_sim: TAKKUB_SIM_PASSWORD {problem} — ตั้ง TAKKUB_SIM_PASSWORD ใน .env ก่อน "
            "(ดู .env.example) แล้ว `docker compose --profile sim up -d` ใหม่",
            file=sys.stderr,
        )
        raise SystemExit(78)  # EX_CONFIG
    return password


def _seed_remote() -> None:
    from agent_takkub.remote.auth import hash_password, verify_password
    from agent_takkub.remote.config import RemoteConfig

    cfg = RemoteConfig.load()
    if cfg.enabled and cfg.secret_path and cfg.token:
        print("seed_sim: remote.json already configured — skipping", file=sys.stderr)
        try:
            weak = any(verify_password(p, cfg.password_hash) for p in _KNOWN_PASSWORDS)
        except Exception:
            weak = False
        if weak:
            # A sandbox seeded before the default was removed. Not rewritten
            # here (never overwrite state a human may have changed) — said
            # loudly instead.
            print(
                "seed_sim: WARNING — this sandbox still uses a well-known password. "
                "Change it in the PWA settings, or reset volumes/data and set "
                "TAKKUB_SIM_PASSWORD in .env.",
                file=sys.stderr,
            )
        print(f"seed_sim: pairing URL: {cfg.pairing_url()}", file=sys.stderr)
        return

    password = _required_password()
    public_url = os.environ.get("TAKKUB_SIM_PUBLIC_URL", "http://localhost:8899")

    cfg.enabled = True
    cfg.mode = "control"  # sending messages to Lead from the PWA needs control mode
    cfg.bind_port = INTERNAL_BIND_PORT
    cfg.public_url = public_url
    cfg.secret_path = cfg.secret_path or secrets.token_urlsafe(16)
    cfg.token = cfg.token or secrets.token_urlsafe(32)
    cfg.password_hash = hash_password(password)
    cfg.url_only_auth = True  # plain link + password, no #token= fragment
    cfg.auto_start_tunnel = (
        False  # no cloudflared/ngrok in the sandbox — loopback + published port only
    )
    cfg.save()

    print("seed_sim: remote-control enabled — password from TAKKUB_SIM_PASSWORD", file=sys.stderr)
    print(f"seed_sim: pairing URL: {cfg.pairing_url()}", file=sys.stderr)


def main() -> int:
    _seed_project()
    _seed_claude_onboarding()
    _seed_remote()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
