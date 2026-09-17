"""
ERP AI Agent — Tool Registry Tests
===================================
Tests that the tool registry and the handler map cannot drift apart, and that
the expected tool surface is present.
"""

from __future__ import annotations

import pytest
from app.tools import list_tools, get_handler


# The complete tool surface the agent advertises, grouped by the module that
# owns it.  Pinning the SET (not just a count) means a tool can never be
# silently renamed or dropped; adding one is a deliberate, reviewed edit here.
#
# The previous form of this test asserted `len(tools) == 50`, which only
# measured how many tools happened to exist and went stale every time a module
# added one (banking, recurring transactions, fixed assets, health, multi
# currency, products/services, ...).  Integrity is the invariant that matters.
EXPECTED_TOOLS = frozenset({
    # -- customers / suppliers -------------------------------------------
    "search_customer", "get_customer", "create_customer", "get_customer_ledger",
    "search_supplier", "get_supplier", "create_supplier", "get_supplier_ledger",
    # -- chart of accounts / journals -------------------------------------
    "search_account", "get_chart_of_accounts", "create_account",
    "prepare_journal", "validate_journal", "post_journal", "reverse_journal",
    # -- sales ------------------------------------------------------------
    "create_invoice", "get_invoice", "create_quotation", "convert_quotation",
    "create_credit_note", "record_cash_sale",
    # -- purchases / expenses ---------------------------------------------
    "create_purchase_bill", "get_purchase_bill", "create_purchase_return",
    "create_expense", "classify_expense", "list_expenses",
    "record_expense_payment",
    # -- receipts / payments / banking ------------------------------------
    "record_customer_receipt", "record_supplier_payment", "record_bank_transfer",
    "create_bank_account", "list_bank_accounts",
    # -- fixed assets -----------------------------------------------------
    "register_fixed_asset", "get_fixed_asset", "search_fixed_asset",
    "dispose_fixed_asset", "record_asset_depreciation",
    # -- projects / products / services -----------------------------------
    "create_project", "get_project", "get_project_profitability",
    "create_product", "search_product", "create_service", "search_service",
    # -- reporting --------------------------------------------------------
    "get_general_ledger", "get_trial_balance", "get_profit_loss",
    "get_balance_sheet", "get_cash_flow", "generate_report",
    # -- financial health -------------------------------------------------
    "calculate_health", "get_health",
    # -- recurring transactions -------------------------------------------
    "create_recurring_template", "get_recurring_template",
    "get_recurring_templates", "update_recurring_template",
    "generate_recurring", "get_recurring_executions",
})


class TestToolRegistry:

    def test_all_tools_registered(self):
        """Registry integrity: every advertised tool must resolve to a real,
        callable handler carrying an explicit boolean read_only flag, and no
        slug may be duplicated.

        This is the invariant that actually matters — a count only measures
        how many tools exist and goes stale whenever a module adds one.
        """
        tools = list_tools()
        assert tools, "The tool registry is empty"

        duplicates = sorted({s for s in tools if tools.count(s) > 1})
        assert not duplicates, f"Duplicate tool slugs in the registry: {duplicates}"

        for slug in tools:
            entry = get_handler(slug)
            assert entry is not None, (
                f"Tool '{slug}' is advertised by list_tools() but has no handler"
            )
            assert callable(entry.get("handler")), (
                f"Tool '{slug}' has no callable handler"
            )
            assert isinstance(entry.get("read_only"), bool), (
                f"Tool '{slug}' must declare a boolean read_only flag"
            )

    def test_expected_tool_surface_is_present(self):
        """The registry must match the expected surface exactly.

        Set equality (not a count) so a tool cannot be silently renamed,
        removed, or leaked in without this list being updated deliberately.
        """
        actual = set(list_tools())
        missing = sorted(EXPECTED_TOOLS - actual)
        unexpected = sorted(actual - EXPECTED_TOOLS)
        assert not missing, f"Expected tools are missing from the registry: {missing}"
        assert not unexpected, (
            f"Unexpected tools appeared in the registry (update EXPECTED_TOOLS "
            f"if intentional): {unexpected}"
        )

    def test_original_32_tools(self):
        expected = [
            "search_customer", "get_customer", "create_customer", "get_customer_ledger",
            "search_supplier", "get_supplier", "create_supplier", "get_supplier_ledger",
            "search_account", "get_chart_of_accounts", "create_account",
            "create_invoice", "get_invoice",
            "create_purchase_bill", "get_purchase_bill",
            "create_expense", "classify_expense",
            "record_customer_receipt", "record_supplier_payment",
            "prepare_journal", "validate_journal", "post_journal", "reverse_journal",
            "get_general_ledger", "get_trial_balance", "get_profit_loss",
            "get_balance_sheet", "get_cash_flow", "generate_report",
            "create_project", "get_project", "get_project_profitability",
        ]
        for slug in expected:
            assert get_handler(slug) is not None, f"Tool '{slug}' not registered"

    def test_new_4_tools(self):
        new_tools = [
            "create_quotation",
            "create_credit_note",
            "create_purchase_return",
            "record_expense_payment",
        ]
        for slug in new_tools:
            handler = get_handler(slug)
            assert handler is not None, f"Tool '{slug}' not registered"
            assert callable(handler["handler"]), f"Tool '{slug}' handler not callable"

    def test_read_only_flags(self):
        """Read-only tools should be marked."""
        read_only_tools = [
            "search_customer", "get_customer", "get_customer_ledger",
            "search_supplier", "get_supplier", "get_supplier_ledger",
            "search_account", "get_chart_of_accounts",
            "get_invoice", "get_purchase_bill",
            "classify_expense",
            "get_general_ledger", "get_trial_balance", "get_profit_loss",
            "get_balance_sheet", "get_cash_flow", "generate_report",
            "get_project", "get_project_profitability",
        ]
        for slug in read_only_tools:
            entry = get_handler(slug)
            assert entry is not None, f"Tool '{slug}' not found"
            assert entry["read_only"] is True, f"Tool '{slug}' should be read-only"

    def test_mutation_tools_not_read_only(self):
        mutation_tools = [
            "create_customer", "create_supplier", "create_account",
            "create_invoice", "create_purchase_bill", "create_expense",
            "record_customer_receipt", "record_supplier_payment",
            "prepare_journal", "post_journal", "reverse_journal",
            "create_project",
            "create_quotation", "create_credit_note",
            "create_purchase_return", "record_expense_payment",
            "record_cash_sale",
        ]
        for slug in mutation_tools:
            entry = get_handler(slug)
            assert entry is not None, f"Tool '{slug}' not found"
            assert entry["read_only"] is False, f"Tool '{slug}' should NOT be read-only"
