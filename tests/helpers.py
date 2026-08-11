"""Shared helpers for API-level tests."""

from __future__ import annotations

from uuid import uuid4

TEST_PASSWORD = "test-password"


def process_payload(message: str, action_id: str | None = None) -> dict[str, str]:
    """Build one explicit idempotent process command for API tests."""
    return {
        "message": message,
        "action_id": action_id or f"test-process-{uuid4()}",
    }


def login(client, operator: str, password: str = TEST_PASSWORD):
    """Sign the test client in as ``operator``.

    Review and approval actions are attributed to the authenticated session
    identity, so a test that acts as two different people signs in twice.
    """
    response = client.post("/api/login", json={"operator": operator, "password": password})
    assert response.status_code == 200, response.text
    return response
