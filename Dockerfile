# Stage 1: build with uv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .

# Stage 2: runtime
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN groupadd -r forge && useradd -r -g forge forge

WORKDIR /app
COPY --from=builder /app /app

# Create data directory for SQLite and set ownership
RUN mkdir -p /app/data /app/.cache/uv \
    && chown -R forge:forge /app/data /app/.cache/uv /app/.venv

ENV UV_CACHE_DIR=/app/.cache/uv \
    UV_LINK_MODE=copy

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8420/health')"]

USER forge

CMD ["uv", "run", "uvicorn", "forge.main:app", "--factory", "--host", "0.0.0.0", "--port", "8420"]
