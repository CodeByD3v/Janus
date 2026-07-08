# janus-sandbox

A deterministic, Docker-based secure execution sandbox for AI coding agents.

This package extracts the core isolation engine from the [Janus](https://github.com/CodeByD3v/Janus) Adversarial Code Review system. It allows you to safely run untrusted, LLM-generated code (tests, linters, static analysis) inside a locked-down, resource-capped container.

## Features
- **Zero-Trust Execution**: Runs commands in a `--network none` container.
- **Resource Capped**: Limits Memory, CPUs, and PIDs to prevent fork bombs and OOMs.
- **Read-Only Filesystem**: The entire container is read-only except for a mapped `/workspace` and an in-memory `/tmp`.
- **Fail-Secure**: If Docker is unavailable, the execution fails securely rather than silently falling back to the host machine.

## Installation
```bash
pip install janus-sandbox
```

## Quick Start
```python
from pathlib import Path
from janus_sandbox import SandboxRunner, SandboxConfig

# Configuration for the Sandbox
config = SandboxConfig(
    image="python:3.12-slim",
    memory_limit="512m",
    cpu_limit="1",
    pid_limit=128,
    timeout_seconds=60
)

runner = SandboxRunner(config)

# Run a test inside the sandbox safely
repo_dir = Path("./my_code")
returncode, output = runner.run_containerized(["pytest", "-q"], repo_dir)

if returncode == 0:
    print("Tests passed!")
else:
    print(f"Tests failed:\n{output}")
```
