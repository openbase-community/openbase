# syntax=docker/dockerfile:1
# Openbase Coder runtime in a container: Django API, LiveKit voice stack,
# sync workers, and routines supervised by docker/entrypoint.sh.
# Usage, environment variables, and limitations: docker/README.md.
#
# The image is a minimal dev-style install (the same shape as the Cloud
# DevSpace AMI): a workspace directory holding this cli checkout plus the
# super-agents sibling that [tool.uv.sources] requires, with the cli venv
# synced at build time so first-run `openbase-coder setup` is fast.
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

WORKDIR /home/openbase
# ~/.openbase/bin leads so the entrypoint's tailscale CLI shim (which targets
# the in-container tailscaled socket) also wins in `docker exec` shells.
ENV PATH="/home/openbase/.openbase/bin:/opt/openbase-coder/workspace/cli/.venv/bin:${PATH}" \
    OPENBASE_CODER_WORKSPACE_DIR=/opt/openbase-coder/workspace

# All state (env file, sqlite DB, downloaded backend binaries, logs) lives in
# ~/.openbase; keep it on a volume so logins and setup survive restarts.
VOLUME ["/home/openbase/.openbase"]

# 7999 Django API, 7880 LiveKit signaling, 7881/7882 LiveKit media.
EXPOSE 7999 7880 7881 7882/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD curl -fsS http://127.0.0.1:7999/api/health/ || exit 1

ENTRYPOINT ["tini", "--", "/usr/local/bin/openbase-coder-entrypoint"]
