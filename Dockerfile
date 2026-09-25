# Dockerfile — service image (API + worker + web dashboard)
# Multi-stage build: Stage 1 builds React web frontend, Stage 2 packages Python API/worker

FROM node:20-slim AS web-builder
WORKDIR /app/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS base

LABEL maintainer="janus-team" \
      description="Adversarial code-review service (API + worker + web dashboard)"

# OS-level deps. Debian's docker.io package supplies the Docker CLI used by
# the worker; the daemon is intentionally not started inside this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker.io \
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
# Copy compiled web frontend build
COPY --from=web-builder --chown=janus:janus /app/web/dist ./web/dist

USER janus

EXPOSE 8000

# Default: run the API server
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
