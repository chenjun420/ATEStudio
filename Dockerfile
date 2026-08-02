# =============================================================================
# ATE Studio - Python Multi-Stage Dockerfile
# Serves both ate-cloud (FastAPI) and ate-platform (edge worker) services.
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder — install uv, resolve dependencies, create virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Install uv via pip (avoids ghcr.io dependency for restricted networks)
RUN pip install --no-cache-dir uv

# Use copy mode to avoid hardlink issues across Docker layers
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock ./

# Create virtualenv with production dependencies only (no dev, no project install)
# Project packages (ate_cloud, ate_platform, shared) are imported via PYTHONPATH at runtime
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2: Runtime — slim image with virtualenv and source code
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Install curl for container healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source and configuration
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY scripts/ ./scripts/

# Environment: venv on PATH, src on PYTHONPATH for package imports
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Default: run ate-cloud FastAPI service
# Override with `command:` in docker-compose for ate-platform worker
CMD ["uvicorn", "ate_cloud.main:app", "--host", "0.0.0.0", "--port", "8000"]
