# ---------- builder ----------
FROM python:3.11.15-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /usr/local/bin/uv

# git required for toon_format optional dependency (--extra toon)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (cache-friendly — only busts on lockfile change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra toon

# Then copy source and install project
COPY api_agent ./api_agent
RUN uv sync --frozen --no-dev --extra toon

# ---------- runtime ----------
FROM python:3.11.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    PORT=3000

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --no-create-home appuser

WORKDIR /app

# Copy uv binary and installed venv from builder
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=builder /app /app

COPY start.sh healthcheck.sh ./
RUN chmod +x ./start.sh ./healthcheck.sh && chown -R appuser:appuser /app

EXPOSE ${PORT}
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["./healthcheck.sh"]

USER appuser
ENTRYPOINT ["/app/start.sh"]
