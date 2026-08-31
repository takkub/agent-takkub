# agent-takkub headless (#105 Phase B) — runs the orchestrator + cli_server +
# remote-control server with no display. The PWA (remote-control) is the
# only UI surface; see docs/guides/2026-07-11-headless-docker.md.
#
# PyQt6-WebEngine is a hard dependency of the engine today (orchestrator.py
# imports agent_pane.py for one isinstance() check, which transitively pulls
# in QtWebEngineWidgets — see docs/design/2026-07-11-105-phaseB-headless.md
# "known limitation"), so the image still needs Chromium's runtime shared
# libraries even though no window is ever shown or rendered.
FROM node:20-bookworm-slim AS base

# python3.11 + the native libs PyQt6/PyQt6-WebEngine need to *import*
# successfully with no X server (QT_QPA_PLATFORM is unset here — headless
# mode uses a bare QCoreApplication, not the offscreen QPA plugin the test
# suite uses, so no libEGL/xcb is required — but the WebEngine .so's own
# dlopen()'d Chromium dependencies still are).
# The original list here (pre-#457) was written by inspection, never
# actually built — it was missing 8 libs PyQt6.QtGui/QtWebEngineWidgets
# dlopen() even with no window ever shown. This list is verified by a real
# `docker build` + `python3.11 -c "from PyQt6 import QtWebEngineWidgets"` on
# this image (#457); don't trim it back down from inspection alone.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git ca-certificates \
        libnss3 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libxkbcommon0 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxfixes3 libxi6 libxtst6 libdbus-1-3 \
        libglib2.0-0 libgl1 libegl1 libopengl0 libxcb-cursor0 \
        libxshmfence1 libxcursor1 libfontconfig1 libfreetype6 \
        libx11-xcb1 libsm6 libice6 libatomic1 libxkbfile1 \
        fonts-liberation socat \
    && rm -rf /var/lib/apt/lists/*

# claude + codex CLIs (agy/Antigravity has no scripted Linux install — see
# the guide; a `gemini` role degrades to a claude-substitute pane without
# it, same as the desktop build's provider-unavailable path).
RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /app
COPY pyproject.toml ./
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Deps layer split from src (ponytail): editing src/ (the common case) must
# not re-resolve/re-download every dependency on each rebuild. pyproject.toml
# has no [build-system] deps-only install mode, so extract the dependency
# list with stdlib tomllib and pip-install just that before src/ ever enters
# the build context.
RUN python3.11 -c "import tomllib, pathlib; d = tomllib.load(open('pyproject.toml', 'rb')); pathlib.Path('requirements.txt').write_text(chr(10).join(d['project']['dependencies']) + chr(10))" \
    && pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . \
    && takkub --help > /dev/null  # sanity: console-script entry points resolve

# Sandbox entrypoint (docker/entrypoint-sim.sh + seed_sim.py) — inert for the
# prod ENTRYPOINT below, only invoked when docker-compose.yml's `takkub-sim`
# service overrides `entrypoint:` to it. See docs/guides/2026-08-31-docker-sandbox.md.
COPY docker/entrypoint-sim.sh docker/seed_sim.py ./docker/
RUN chmod +x ./docker/entrypoint-sim.sh

# Runtime state — mount a named volume here (see docker-compose.yml) so
# projects.json / runtime/ / role-providers.json survive container restarts.
# AGENT_TAKKUB_HOME is config.py's documented override
# (_resolve_data_home()) — set explicitly rather than relying on its
# venv-ancestor-name heuristic, which is fragile to the venv dir's name.
ENV AGENT_TAKKUB_HOME=/data
ENV HOME=/root
RUN mkdir -p "$AGENT_TAKKUB_HOME"

# `claude` hard-refuses --dangerously-skip-permissions (which every cockpit
# pane spawn passes — pane_guard.py's hook interception is the safety net
# instead) when the process EUID is 0 — confirmed by a real crash in this
# image (#457): Lead's transcript was just "--dangerously-skip-permissions
# cannot be used with root/sudo privileges". Reuse /root as this user's
# home (instead of creating /home/takkub) so HOME=/root and every path this
# codebase resolves from it (codex's $HOME/.codex, the volume mounts below)
# stays unchanged for both services in docker-compose.yml.
RUN useradd --uid 1001 --home-dir /root --no-create-home --shell /bin/bash takkub \
    && chown -R takkub:takkub /root /app "$AGENT_TAKKUB_HOME"
USER takkub

# remote/config.py's RemoteConfig.bind_port default (8899) — the
# remote-control HTTP server the PWA talks to. cli_server's own TCP port
# (the `takkub` CLI protocol) binds ephemeral and loopback-only; it's used
# only from inside this container (`takkub assign` etc.), never exposed.
EXPOSE 8899

ENTRYPOINT ["agent-takkub-headless"]
