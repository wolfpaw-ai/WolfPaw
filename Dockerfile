# Wolfpaw backend image — FastAPI + asyncpg + the agent stack.
#
# Two-stage build: the `builder` stage installs everything into a venv so
# the final stage stays slim. We skip optional extras (`[pdf]`, `[s3]`,
# `[e2b]`, `[docker]`, `[langsmith]`) — operators that need them rebuild
# with `--build-arg EXTRAS=pdf,s3,...` or layer their own image on top.
#
# The sandbox default in this image is `subprocess`, which is NOT a
# security boundary — it runs Python in a subprocess inside the container.
# Production deployments should switch to `e2b` (set WOLFPAW_SANDBOX_BACKEND=e2b
# + WOLFPAW_E2B_API_KEY) or `docker` (requires socket / DinD; see
# docs/self-host.md).

# ---------- builder ----------
FROM python:3.12-slim AS builder

ARG EXTRAS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Project metadata + source. Copy these separately from the heavy deps so
# small source changes don't bust the dep-install layer cache.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
 && if [ -n "$EXTRAS" ]; then \
        pip install ".[${EXTRAS}]"; \
    else \
        pip install .; \
    fi

# ---------- runtime ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WOLFPAW_MIGRATIONS_DIR=/app/migrations

# Non-root user. Docker volume mounts (workspace, soul) inherit ownership
# from the host; the install.sh creates matching dirs ahead of compose-up.
RUN groupadd --system --gid 1000 wolfpaw \
 && useradd --system --uid 1000 --gid 1000 --no-create-home wolfpaw

# Pull the installed packages from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

WORKDIR /app

# Migrations + the runtime scripts + the soul file. The package itself is
# already installed; these are repo-root files that the package reads at
# runtime.
COPY migrations ./migrations
COPY scripts ./scripts
COPY soul.md ./soul.md

# Workspace mount point (LocalStorage). docker-compose binds a host
# volume here for persistence across container restarts.
RUN mkdir -p /workspace && chown -R wolfpaw:wolfpaw /workspace /app
ENV WOLFPAW_LOCAL_STORAGE_ROOT=/workspace \
    WOLFPAW_SOUL_PATH=/app/soul.md

USER wolfpaw
EXPOSE 8000

# Default: serve the API. The compose `migrate` service overrides this
# with `python -m scripts.migrate`.
CMD ["uvicorn", "wolfpaw.api:app", "--host", "0.0.0.0", "--port", "8000"]
