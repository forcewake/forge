# forge release image (multi-arch friendly, no dev tooling inside)
FROM docker.io/library/python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_CACHE_DIR=/app/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
RUN mkdir -p /app/data /app/.cache/uv

# Dependencies first: this layer only invalidates when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Application source
COPY src /app/src
RUN uv sync --frozen --no-dev \
    && useradd -u 1000 -m forge \
    && chown -R forge:forge /app/data /app/.cache

USER forge
# Runtime does not invoke uv: the synced venv is used directly, so a
# read-only /app keeps working and no cache directory is touched.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8420
# Health endpoint: /health (checks DB + LiteLLM reachability).
# Deployment-specific healthchecks (and the worker process, which does not
# serve HTTP) are configured by the deployment, not baked into the image.

CMD ["uvicorn", "forge.main:app", "--host", "0.0.0.0", "--port", "8420"]
