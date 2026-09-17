"""
ERP AI Agent — Test Fixtures & Configuration
===============================================
Shared pytest fixtures for the AI agent test suite.
"""

from __future__ import annotations

import os
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Environment setup for tests — use test credentials
# ---------------------------------------------------------------------------
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing-only")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def mock_supabase():
    """Mock the Supabase client for all database operations."""
    with patch("app.database.get_service_client") as mock_client:
        mock_table = MagicMock()
        mock_client.return_value.table.return_value = mock_table
        yield mock_table


@pytest.fixture
def mock_auth_context(org_id, user_id):
    """Return an AuthContext with ACCOUNTANT role."""
    from app.auth import AuthContext
    return AuthContext(
        user_id=user_id,
        organization_id=org_id,
        role_code="ACCOUNTANT",
        role_permissions=["accounting:full", "documents:full", "ai:full", "reports:full"],
    )


@pytest.fixture
def viewer_auth_context(org_id, user_id):
    """Return an AuthContext with VIEWER role (read-only)."""
    from app.auth import AuthContext
    return AuthContext(
        user_id=user_id,
        organization_id=org_id,
        role_code="VIEWER",
        role_permissions=["reports:read"],
    )


@pytest.fixture
def manager_auth_context(org_id, user_id):
    """Return an AuthContext with MANAGER role."""
    from app.auth import AuthContext
    return AuthContext(
        user_id=user_id,
        organization_id=org_id,
        role_code="MANAGER",
        role_permissions=[
            "customers:manage", "suppliers:manage", "sales:manage",
            "purchases:manage", "projects:manage", "reports:read",
        ],
    )


# ---------------------------------------------------------------------------
# Reasoning test data
# ---------------------------------------------------------------------------

@pytest.fixture
def credit_purchase_message():
    return "I bought a laptop from ABC Computers for Rs.150,000 on credit."


@pytest.fixture
def cash_purchase_message():
    return "I bought a Dell laptop for Rs.150,000."


@pytest.fixture
def credit_sale_message():
    return "I sold services to XYZ Corp for Rs.200,000 on credit."


@pytest.fixture
def cash_sale_message():
    return "I sold services to XYZ for Rs.200,000 and received payment."


@pytest.fixture
def missing_amount_message():
    return "I bought a laptop from ABC Computers on credit."


@pytest.fixture
def missing_supplier_message():
    return "I bought something for Rs.50,000 on credit."
