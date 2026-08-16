# syntax=docker/dockerfile:1
# Openbase Coder runtime in a container: Django API, LiveKit voice stack,
# sync workers, and routines supervised by docker/entrypoint.sh.
# Usage, environment variables, and limitations: docker/README.md.
#
# The image is a minimal dev-style install (the same shape as the Cloud
# DevSpace AMI): a workspace directory holding this cli checkout plus the
# super-agents sibling that [tool.uv.sources] requires, with the cli venv
# synced at build time so first-run `openbase-coder setup` is fast.
# --- Console build stage ------------------------------------------------------
# The web console lives in sibling repos (all public); clone and build them in
# a node stage so the runtime image serves the console UI. Refs are ARGs —
# note they track the remotes, not the local checkout this image builds from.
FROM node:24-slim AS console-build
ARG CONSOLE_REPO=https://github.com/openbase-community/openbase-coder-console
ARG CONSOLE_REF=develop
ARG CODER_REACT_REPO=https://github.com/openbase-community/openbase-coder-react
ARG CODER_REACT_REF=develop
ARG MULTI_REACT_REPO=https://github.com/montaguegabe/multi-react
ARG MULTI_REACT_REF=main
ARG BOILERSYNC_REACT_REPO=https://github.com/montaguegabe/boilersync-react
ARG BOILERSYNC_REACT_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 --branch "$CONSOLE_REF" "$CONSOLE_REPO" console \
    && git clone --depth 1 --branch "$CODER_REACT_REF" "$CODER_REACT_REPO" coder-react \
    && git clone --depth 1 --branch "$MULTI_REACT_REF" "$MULTI_REACT_REPO" multi-react \
    && git clone --depth 1 --branch "$BOILERSYNC_REACT_REF" "$BOILERSYNC_REACT_REPO" boilersync-react

# Minimal pnpm workspace mirroring the multi workspace's frontend packages
# (console depends on @openbase/coder-react via workspace:*, which depends on
# multi-react and boilersync-react).
RUN printf 'packages:\n  - console\n  - coder-react\n  - multi-react\n  - boilersync-react\n' \
        > pnpm-workspace.yaml \
    && printf '{\n  "name": "openbase-coder-docker-console",\n  "private": true,\n  "packageManager": "pnpm@10.18.0",\n  "pnpm": {\n    "overrides": {\n      "@types/react": "^18.3.3",\n      "@types/react-dom": "^18.3.0"\n    }\n  }\n}\n' \
        > package.json \
    && corepack enable \
    && pnpm install \
    && pnpm --dir console build

# --- Runtime image ------------------------------------------------------------
FROM python:3.13-slim-bookworm

# hatch-vcs needs git metadata that the build context does not include; pass
# --build-arg OPENBASE_CODER_VERSION=x.y.z to stamp a real version.
ARG OPENBASE_CODER_VERSION=0.0.0.dev0
ARG SUPER_AGENTS_REPO=https://github.com/montaguegabe/super-agents

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        iproute2 \
        openssh-client \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Tailscale is Openbase Coder's networking layer: phones reach the runtime
# through `tailscale serve` (18080 -> 7999 API, 7880 LiveKit signaling) and
# LiveKit advertises the tailnet IP for media. tailscaled runs unprivileged
# inside the container with userspace networking (see docker/entrypoint.sh).
RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
        -o /etc/apt/sources.list.d/tailscale.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends tailscale \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY docker/entrypoint.sh /usr/local/bin/openbase-coder-entrypoint
# Pre-create the state dir so the named volume inherits openbase ownership.
RUN chmod 0755 /usr/local/bin/openbase-coder-entrypoint \
    && useradd --create-home --uid 1000 openbase \
    && mkdir -p /home/openbase/.openbase \
    && chown openbase:openbase /home/openbase/.openbase \
    && mkdir -p /opt/openbase-coder/workspace \
    && chown -R openbase:openbase /opt/openbase-coder

USER openbase
WORKDIR /opt/openbase-coder/workspace

# A minimal multi workspace: setup only requires multi.json plus a cli/ repo.
RUN printf '{\n  "repos": []\n}\n' > multi.json \
    && git clone --depth 1 "$SUPER_AGENTS_REPO" super-agents

COPY --chown=openbase:openbase pyproject.toml uv.lock README.md LICENSE manage.py cli/
COPY --chown=openbase:openbase openbase_coder_cli cli/openbase_coder_cli

# hatch-vcs needs git metadata, including for the `uv sync` that
# `openbase-coder setup` re-runs on first boot — snapshot the copied source
# as a single tagged commit.
ENV UV_PYTHON_DOWNLOADS=never
RUN cd cli \
    && git init -q \
    && git add -A \
    && git -c user.email=docker@openbase.cloud -c user.name="Docker Build" \
       commit -qm "Docker build snapshot" \
    && git tag "v$OPENBASE_CODER_VERSION" \
    && uv sync

# Voice sessions need the LiveKit VAD/turn-detector model files (e.g.
# languages.json) at call time with downloads disabled. First-run setup
# fetches them into ~/.cache, but that is container-layer state — a
# recreated container reuses the volume, skips setup, and would crash the
# agent mid-call. Bake the models into the image.
RUN cd cli \
    && .venv/bin/python -m openbase_coder_cli.livekit_agent.livekit download-files

# Prebuilt console UI; the env override makes django serve it directly and
# keeps the runtime's own console-build step skipped (no console source here).
COPY --from=console-build --chown=openbase:openbase \
    /build/console/dist /opt/openbase-coder/console-dist

WORKDIR /home/openbase
# ~/.openbase/bin leads so the entrypoint's tailscale CLI shim (which targets
# the in-container tailscaled socket) also wins in `docker exec` shells.
ENV PATH="/home/openbase/.openbase/bin:/opt/openbase-coder/workspace/cli/.venv/bin:${PATH}" \
    OPENBASE_CODER_WORKSPACE_DIR=/opt/openbase-coder/workspace \
    OPENBASE_CODER_CLI_CONSOLE_BUILD_DIR=/opt/openbase-coder/console-dist \
    OPENBASE_CODER_SERVICE_SUPERVISOR=external

# All state (env file, sqlite DB, downloaded backend binaries, logs) lives in
# ~/.openbase; keep it on a volume so logins and setup survive restarts.
VOLUME ["/home/openbase/.openbase"]

# 7999 Django API, 7880 LiveKit signaling, 7881/7882 LiveKit media.
EXPOSE 7999 7880 7881 7882/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD curl -fsS http://127.0.0.1:7999/api/health/ || exit 1

ENTRYPOINT ["tini", "--", "/usr/local/bin/openbase-coder-entrypoint"]
