#!/usr/bin/env python3
"""Send one explicitly approved, signed payload to a Janus webhook endpoint.

This is an operational harness, not an automatic CI test. It requires a real
endpoint, webhook secret, and payload supplied by the operator. It never
contacts GitHub APIs directly and never prints the secret or full payload.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _signature(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _load_payload(path: Path) -> bytes:
    payload = path.read_bytes()
    json.loads(payload.decode("utf-8"))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one operator-approved signed payload to a Janus GitHub webhook."
    )
    parser.add_argument("endpoint", help="full Janus webhook URL")
    parser.add_argument("payload", type=Path, help="JSON payload captured from a real GitHub event")
    parser.add_argument("--event", default="pull_request", help="X-GitHub-Event value")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="required safety acknowledgement before sending an external request",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15)",
    )
    args = parser.parse_args()

    if not args.confirm_live:
        print("Refusing external request: pass --confirm-live explicitly.", file=sys.stderr)
        return 2
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET must be set in the environment.", file=sys.stderr)
        return 2
    if not args.event.strip():
        print("--event must not be empty.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be greater than zero.", file=sys.stderr)
        return 2

    try:
        payload = _load_payload(args.payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Invalid JSON payload: {exc}", file=sys.stderr)
        return 2

    request = Request(
        args.endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-Hub-Signature-256": _signature(payload, secret),
            "User-Agent": "janus-github-app-smoke-test",
        },
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            body = response.read(2048).decode("utf-8", errors="replace")
            print(f"HTTP {response.status}: {body}")
            return 0 if 200 <= response.status < 300 else 1
    except HTTPError as exc:
        print(f"HTTP {exc.code} from webhook endpoint", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Webhook request failed: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
