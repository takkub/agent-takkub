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


def _seed_remote() -> None:
    from agent_takkub.remote.auth import hash_password
    from agent_takkub.remote.config import RemoteConfig

    cfg = RemoteConfig.load()
    if cfg.enabled and cfg.secret_path and cfg.token:
        print("seed_sim: remote.json already configured — skipping", file=sys.stderr)
        print(f"seed_sim: pairing URL: {cfg.pairing_url()}", file=sys.stderr)
        return

    password = os.environ.get("TAKKUB_SIM_PASSWORD", "sandbox")
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

    print(
        "seed_sim: remote-control enabled — password from TAKKUB_SIM_PASSWORD (default 'sandbox')",
        file=sys.stderr,
    )
    print(f"seed_sim: pairing URL: {cfg.pairing_url()}", file=sys.stderr)


def main() -> int:
    _seed_project()
    _seed_remote()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
