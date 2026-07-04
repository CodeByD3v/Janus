# Dockerfile — service image (API + worker share this image)
# API  → default CMD (uvicorn)
# Worker → override entrypoint in docker-compose.yml

FROM python:3.12-slim AS base

LABEL maintainer="janus-team" \
      description="Adversarial code-review service (API + worker)"

# OS-level deps (pg client headers for psycopg2-binary, git for potential VCS ops)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd --gid 1000 janus \
    && useradd --uid 1000 --gid janus --create-home janus

WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache

# Copy project code
COPY --chown=janus:janus . .

USER janus

EXPOSE 8000

# Default: run the API server
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
