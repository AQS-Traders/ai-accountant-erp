"""
ERP AI Agent — Permission-Based Routing Tests
===============================================
Tests that tools are properly authorized/denied based on user role.
"""

from __future__ import annotations

import uuid
import pytest

from app.auth import AuthContext
from app.permissions import (
    check_permission,
    _build_tool_capability_map,
    _build_tool_deny_set,
    _role_capability_map,
)


# Sample permissions matching the Supabase ai.permissions data
SAMPLE_PERMISSIONS = [
    {"capability": "sales", "allowed_tools": ["search_customer", "get_customer", "create_quotation", "create_invoice", "get_invoice", "create_credit_note"], "denied_tools": []},
    {"capability": "purchases", "allowed_tools": ["search_supplier", "get_supplier", "create_purchase_bill", "get_purchase_bill", "create_purchase_return"], "denied_tools": []},
    {"capability": "expenses", "allowed_tools": ["create_expense", "classify_expense", "search_account", "create_account"], "denied_tools": []},
    {"capability": "payments", "allowed_tools": ["record_customer_receipt", "record_supplier_payment", "record_expense_payment", "search_customer", "search_supplier"], "denied_tools": []},
    {"capability": "reporting", "allowed_tools": ["get_general_ledger", "get_trial_balance", "get_profit_loss", "get_balance_sheet", "get_cash_flow", "generate_report"], "denied_tools": []},
    {"capability": "master_data", "allowed_tools": ["search_customer", "create_customer", "get_customer", "search_supplier", "create_supplier", "get_supplier", "create_project", "get_project"], "denied_tools": []},
    {"capability": "journal_management", "allowed_tools": ["prepare_journal", "validate_journal", "post_journal", "reverse_journal", "search_account", "get_chart_of_accounts"], "denied_tools": []},
]


@pytest.fixture
def tool_cap_map():
    return _build_tool_capability_map(SAMPLE_PERMISSIONS)


@pytest.fixture
def denied_tools():
    return _build_tool_deny_set(SAMPLE_PERMISSIONS)


@pytest.fixture
def accountant_auth():
    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role_code="ACCOUNTANT",
        role_permissions=["accounting:full", "documents:full", "ai:full", "reports:full"],
    )


@pytest.fixture
def viewer_auth():
    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role_code="VIEWER",
        role_permissions=["reports:read"],
    )


@pytest.fixture
def manager_auth():
    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role_code="MANAGER",
        role_permissions=["customers:manage", "suppliers:manage", "sales:manage", "purchases:manage", "projects:manage", "reports:read"],
    )


class TestPermissionChecks:

    def test_always_allowed_tools(self, viewer_auth, tool_cap_map, denied_tools):
        """Read-only tools like search_customer should be allowed for any role."""
        assert check_permission("search_customer", auth=viewer_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is True
        assert check_permission("get_chart_of_accounts", auth=viewer_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is True

    def test_accountant_can_post_journal(self, accountant_auth, tool_cap_map, denied_tools):
        assert check_permission("post_journal", auth=accountant_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is True

    def test_viewer_cannot_post_journal(self, viewer_auth, tool_cap_map, denied_tools):
        assert check_permission("post_journal", auth=viewer_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is False

    def test_viewer_cannot_create_customer(self, viewer_auth, tool_cap_map, denied_tools):
        assert check_permission("create_customer", auth=viewer_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is False

    def test_manager_can_create_customer(self, manager_auth, tool_cap_map, denied_tools):
        assert check_permission("create_customer", auth=manager_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is True

    def test_manager_can_create_invoice(self, manager_auth, tool_cap_map, denied_tools):
        assert check_permission("create_invoice", auth=manager_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is True

    def test_manager_cannot_post_journal(self, manager_auth, tool_cap_map, denied_tools):
        assert check_permission("post_journal", auth=manager_auth, tool_capability_map=tool_cap_map, denied_tools=denied_tools) is False


class TestRoleCapabilityMapping:

    def test_ai_full_gives_all_capabilities(self):
        caps = _role_capability_map(["ai:full"])
        assert "sales" in caps
        assert "purchases" in caps
        assert "journal_management" in caps
        assert "reporting" in caps

    def test_accounting_maps_to_journal(self):
        caps = _role_capability_map(["accounting:full"])
        assert "journal_management" in caps
        assert "master_data" in caps

    def test_reports_read_maps_to_reporting(self):
        caps = _role_capability_map(["reports:read"])
        assert "reporting" in caps

    def test_empty_permissions_gives_nothing(self):
        caps = _role_capability_map([])
        assert len(caps) == 0
