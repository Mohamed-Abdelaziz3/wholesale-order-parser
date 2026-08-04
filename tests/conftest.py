"""Shared test fixtures for wholesale order parser tests."""

import os
import sys

import pytest

# Ensure the project root is in the path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Authentication is enforced by default, so the suite configures deterministic
# credentials before any application instance is built. These must be set before
# app.security.load_auth_config() runs inside create_app().
from tests.helpers import TEST_PASSWORD  # noqa: E402

os.environ.setdefault("APP_PASSWORD", TEST_PASSWORD)
os.environ.setdefault("APP_SESSION_SECRET", "test-session-secret-not-for-production")

from app.catalog import load_catalog  # noqa: E402
from app.matcher import ProductMatcher  # noqa: E402
from app.models import CatalogProduct  # noqa: E402

CATALOG_PATH = os.path.join(PROJECT_ROOT, "catalog.csv")


@pytest.fixture
def catalog() -> list[CatalogProduct]:
    """Load the test catalog."""
    return load_catalog(CATALOG_PATH)


@pytest.fixture
def matcher(catalog) -> ProductMatcher:
    """Create a ProductMatcher with the test catalog."""
    return ProductMatcher(catalog)
