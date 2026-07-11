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
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git ca-certificates \
        libnss3 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libxkbcommon0 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxfixes3 libxi6 libxtst6 libdbus-1-3 \
        libglib2.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# claude + codex CLIs (agy/Antigravity has no scripted Linux install — see
# the guide; a `gemini` role degrades to a claude-substitute pane without
# it, same as the desktop build's provider-unavailable path).
RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir . \
    && takkub --help > /dev/null  # sanity: console-script entry points resolve

# Runtime state — mount a named volume here (see docker-compose.yml) so
# projects.json / runtime/ / role-providers.json survive container restarts.
# AGENT_TAKKUB_HOME is config.py's documented override
# (_resolve_data_home()) — set explicitly rather than relying on its
# venv-ancestor-name heuristic, which is fragile to the venv dir's name.
ENV AGENT_TAKKUB_HOME=/data
ENV HOME=/root
RUN mkdir -p "$AGENT_TAKKUB_HOME"

# remote/config.py's RemoteConfig.bind_port default (8899) — the
# remote-control HTTP server the PWA talks to. cli_server's own TCP port
# (the `takkub` CLI protocol) binds ephemeral and loopback-only; it's used
# only from inside this container (`takkub assign` etc.), never exposed.
EXPOSE 8899

ENTRYPOINT ["agent-takkub-headless"]
