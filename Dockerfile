# Python with DuckDB for data processing
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install CA certificates for HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY api_agent ./api_agent
COPY start.sh ./

# Install Python dependencies
RUN uv sync --frozen --no-dev

EXPOSE 3000

RUN chmod +x ./start.sh

ENTRYPOINT ["/app/start.sh"]
