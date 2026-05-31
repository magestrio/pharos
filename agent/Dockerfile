# syntax=docker/dockerfile:1.7

# Single image, two entrypoints (loop + api). uv installs the project
# editably so files written under /app/agent/sandbox/<subdir> map to
# named volumes mounted at those paths in compose.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# libpq for asyncpg (uses libpq for SSL/auth), build-essential for any
# wheel that falls back to source build. tini for clean PID-1 signal
# forwarding (graceful SIGTERM into the asyncio loop).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        tini \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.5.13

WORKDIR /app

# Deps layer — re-runs only when pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Project source.
COPY agent ./agent

# Install the project itself (editable). Site-packages then points at
# /app/agent/, so `Path(__file__).parent / "snapshots"` resolves to
# /app/agent/sandbox/snapshots/ — exactly where compose mounts the
# named volume.
RUN uv sync --frozen --no-dev

ENTRYPOINT ["/usr/bin/tini", "--"]
# Overridden per-service in compose. The placeholder makes raw
# `docker run <image>` print loop usage instead of silently hanging.
CMD ["python", "-m", "agent.sandbox.loop", "--help"]
