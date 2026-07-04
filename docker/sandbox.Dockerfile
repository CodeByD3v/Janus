# docker/sandbox.Dockerfile
# Minimal sandbox image for adversarial code-review gate execution.
# Contains ONLY the static-analysis / test tools needed by gate.py.
# Run with: --network none --memory 512m --cpus 1 --pids-limit 128

FROM python:3.12-slim

LABEL maintainer="janus-team" \
      description="Sandboxed gate-execution image (no network, no secrets)"

# Pinned tool versions — bump deliberately, not accidentally.
ARG RUFF_VERSION=0.8.6
ARG MYPY_VERSION=1.14.1
ARG PYTEST_VERSION=8.3.4
ARG BANDIT_VERSION=1.8.3

# Install tools in a single layer; remove pip cache.
RUN pip install --no-cache-dir \
        ruff==${RUFF_VERSION} \
        mypy==${MYPY_VERSION} \
        pytest==${PYTEST_VERSION} \
        bandit==${BANDIT_VERSION} \
    && rm -rf /root/.cache

# Non-root user for defence-in-depth.
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home sandbox

WORKDIR /workspace
RUN chown sandbox:sandbox /workspace

USER sandbox

# No CMD — the orchestrator invokes specific commands via `docker exec`.
