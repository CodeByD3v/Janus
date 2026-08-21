"""
evals/eval_api.py — API layer tests.

Tests auth rejection, rate limiting, request validation, debate CRUD,
healthz, and metrics endpoints. Uses FastAPI TestClient with an
in-memory SQLite database. No API key or Docker needed.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Patch DATABASE_URL to use in-memory SQLite BEFORE importing the app
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Every test in this file uses repo_ref="demo_repo" (relative to the repo
# root, matching how `pytest` is invoked from CI/locally). The repo_ref
# allowlist (core/path_safety.py) is fail-closed by design — an empty
# ALLOWED_REPO_ROOTS rejects every repo_ref, including this test suite's
# own fixtures, unless explicitly configured here.
os.environ.setdefault(
    "ALLOWED_REPO_ROOTS", str(Path(__file__).resolve().parent.parent)
)

from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402
from api.auth import KeyStore, key_store, rate_limiter  # noqa: E402
from storage.db import get_session, run_migrations  # noqa: E402
from storage.models import DebateSession  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_KEY = "test-api-key-12345"
TEST_TENANT = "test-tenant"
ADMIN_KEY = "test-admin-key-98765"


@pytest.fixture(autouse=True, scope="module")
def _setup_db():
    """Run migrations once for the module."""
    run_migrations()
    key_store.register_key(TEST_KEY, TEST_TENANT)
    key_store.register_key(ADMIN_KEY, "operator-test", role="admin")


@pytest.fixture
def client():
    """Create a fresh TestClient and rate-limit state for each test."""
    rate_limiter._buckets.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """Headers with a valid API key."""
    return {"X-API-Key": TEST_KEY}


@pytest.fixture
def admin_headers():
    """Headers with a valid admin API key."""
    return {"X-API-Key": ADMIN_KEY}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_api_key_returns_401(self, client: TestClient) -> None:
        resp = client.post("/debates", json={
            "repo_ref": "demo_repo",
            "target_file": "inventory.py",
            "ticket": "Fix the bug",
        })
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"] or "API" in resp.json()["detail"]

    def test_invalid_api_key_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/debates",
            json={
                "repo_ref": "demo_repo",
                "target_file": "inventory.py",
                "ticket": "Fix the bug",
            },
            headers={"X-API-Key": "wrong-key-000"},
        )
        assert resp.status_code == 401

    def test_role_collision_is_rejected(self) -> None:
        store = KeyStore()
        store.register_key("same-secret", "tenant-a", role="tenant")
        store.register_key("same-secret", "operator-a", role="admin")
        assert store.validate_key("same-secret") is None
        assert store.validate_admin_key("same-secret") is None

    def test_rate_limiting(self, client: TestClient) -> None:
        """Exhaust the rate limit and verify 429 is returned."""
        # Register a key with very low limit
        limited_key = "limited-key-xyz"
        limited_tenant = "limited-tenant"
        key_store.register_key(limited_key, limited_tenant)

        # Override the rate limiter for this tenant with a tiny bucket
        from api.auth import TokenBucket
        rate_limiter._buckets[limited_tenant] = TokenBucket(
            max_tokens=2, refill_period=3600.0
        )

        headers = {"X-API-Key": limited_key}
        body = {
            "repo_ref": "demo_repo",
            "target_file": "inventory.py",
            "ticket": "Fix it",
        }

        # First two should succeed
        resp1 = client.post("/debates", json=body, headers=headers)
        assert resp1.status_code == 202
        resp2 = client.post("/debates", json=body, headers=headers)
        assert resp2.status_code == 202

        # Third should be rate limited
        resp3 = client.post("/debates", json=body, headers=headers)
        assert resp3.status_code == 429


# ---------------------------------------------------------------------------
# Admin visibility tests
# ---------------------------------------------------------------------------


class TestAdminVisibility:
    def test_dashboard_shell_is_available_without_data_access(self, client: TestClient) -> None:
        response = client.get("/admin")
        assert response.status_code == 200
        assert "Janus Admin Dashboard" in response.text
        assert "X-API-Key" in response.text

    def test_admin_endpoint_requires_admin_key(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        assert client.get("/admin/debates").status_code == 401
        assert client.get("/admin/debates", headers=auth_headers).status_code == 403

    def test_admin_key_cannot_authenticate_tenant_route(
        self, client: TestClient, admin_headers: dict
    ) -> None:
        response = client.get(
            f"/debates/{uuid.uuid4()}", headers=admin_headers
        )
        assert response.status_code == 401

    def test_admin_can_list_and_filter_tenants(
        self, client: TestClient, admin_headers: dict
    ) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        with get_session() as db:
            db.add_all([
                DebateSession(
                    id=first_id,
                    repo_ref="repo-a",
                    target_file="a.py",
                    ticket="tenant A private ticket",
                    status="queued",
                    tenant_id="tenant-a",
                    webhook_url="https://private.example/webhook",
                    model_api_key_encrypted="encrypted-secret",
                ),
                DebateSession(
                    id=second_id,
                    repo_ref="repo-b",
                    target_file="b.py",
                    ticket="tenant B private ticket",
                    status="error",
                    tenant_id="tenant-b",
                ),
            ])

        response = client.get("/admin/debates?limit=1", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 2
        assert len(data["items"]) == 1
        assert "ticket" not in data["items"][0]
        assert "webhook_url" not in data["items"][0]
        assert "model_api_key_encrypted" not in data["items"][0]

        filtered = client.get(
            "/admin/debates?tenant_id=tenant-a", headers=admin_headers
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["id"] == first_id


# ---------------------------------------------------------------------------
# Debate CRUD tests
# ---------------------------------------------------------------------------


class TestDebateCrud:
    def test_create_debate_valid(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        resp = client.post(
            "/debates",
            json={
                "repo_ref": "demo_repo",
                "target_file": "inventory.py",
                "ticket": "Fix the average_price() function",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "debate_id" in data
        assert data["status"] == "queued"

    def test_get_debate_returns_state(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        # Create a debate first
        create_resp = client.post(
            "/debates",
            json={
                "repo_ref": "demo_repo",
                "target_file": "inventory.py",
                "ticket": "Fix it",
            },
            headers=auth_headers,
        )
        debate_id = create_resp.json()["debate_id"]

        # Retrieve it
        get_resp = client.get(f"/debates/{debate_id}", headers=auth_headers)
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["id"] == debate_id
        assert data["status"] == "queued"
        assert data["repo_ref"] == "demo_repo"
        assert "rounds" in data

    def test_get_debate_not_found(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        resp = client.get(
            f"/debates/{uuid.uuid4()}", headers=auth_headers
        )
        assert resp.status_code == 404

    def test_request_validation_error(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        # Empty body
        resp = client.post("/debates", json={}, headers=auth_headers)
        assert resp.status_code == 422

        # Missing required field
        resp = client.post(
            "/debates",
            json={"repo_ref": "demo_repo"},
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Healthz and metrics
# ---------------------------------------------------------------------------


class TestInfraEndpoints:
    def test_healthz_returns_status(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "db_reachable" in data
        assert "sandbox_image_present" in data

    def test_metrics_endpoint(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "acr_debates" in resp.text
