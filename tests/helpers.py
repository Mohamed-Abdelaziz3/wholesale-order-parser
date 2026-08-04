"""Shared helpers for API-level tests."""

from __future__ import annotations

TEST_PASSWORD = "test-password"


def login(client, operator: str, password: str = TEST_PASSWORD):
    """Sign the test client in as ``operator``.

    Review and approval actions are attributed to the authenticated session
    identity, so a test that acts as two different people signs in twice.
    """
    response = client.post("/api/login", json={"operator": operator, "password": password})
    assert response.status_code == 200, response.text
    return response
