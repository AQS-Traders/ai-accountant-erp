"""Argument contracts — Python rejects an un-bindable plan BEFORE confirmation.

Production incident (session 6a48a432-d9d9-480c-8e47-e6799305bc6f)
=================================================================

The reasoning model authored ``create_invoice`` arguments from the vocabulary it
could see — ``line_items``, ``customer_name``, ``tax_category`` — because the
prompt offers tool NAMES only.  Nothing compared those names with the Python
callable that would receive them, so the plan was snapshotted
(``ai.confirmations.plan``), shown to the user, APPROVED, and then died at
CALL-BINDING time::

    TypeError: create_invoice() got an unexpected keyword argument 'line_items'

No invoice, no journal, no database work at all.

These tests pin the corrected invariant: a proposed call whose arguments cannot
bind is rejected by Python — with the offending name and the valid parameter
list — before any confirmation snapshot exists.

Authority of the contract
-------------------------
The contract is derived from the SERVICE SIGNATURE (``inspect``), never from
``ai.tool_parameters``, which is provably stale:

* it omits ``payment_terms_days`` / ``discount_total`` / ``terms`` /
  ``created_by`` although the service accepts them;
* it marks ``subtotal`` required for create_invoice although the service
  defaults it and recomputes the header from the validated lines — five
  SUCCESSFUL production invoices were recorded without it (Tests C, D);
* it does not list ``payee_name`` / ``payment_mode``, used by three SUCCESSFUL
  production ``create_expense`` calls (Test E).

A rule built on that table would have rejected those working calls.

No live provider, no live database — pure functions and the real registry.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tool_contract import (
    PROTOCOL_ARGUMENTS,
    contract_from_callable,
    validate_arguments,
    validate_calls,
)

# Verbatim from ai.tool_calls.b5d1887d-d150-48bc-a762-eeb24154c279 (FAILED).
PRODUCTION_INVOICE_ARGS = {
    "due_date": "2026-10-20",
    "line_items": [{"quantity": 2, "unit_price": 16666.67, "product_name": "chairs"}],
    "invoice_date": "2026-09-20",
    "tax_category": "Nill",
    "customer_name": "ABC Furnitures",
}

# Verbatim shape of the 19 SUCCESSFUL Create Invoice calls (2026-09-19).
WORKING_INVOICE_ARGS = {
    "items": [
        {
            "quantity": 3,
            "product_id": "4c35aaaf-c35f-4f80-9697-750c2b255abc",
            "unit_price": 16666.67,
            "description": "chairs",
        }
    ],
    "customer_id": "5bd3f7cf-7096-4414-a722-5f6ffed1a04f",
    "invoice_date": "2026-09-19",
}


def _contracts() -> Dict[str, Dict[str, Any]]:
    from app.tools import tool_contracts

    return tool_contracts()


# ---------------------------------------------------------------------------
# Deriving a contract from a Python callable
# ---------------------------------------------------------------------------


class TestContractDerivation:
    def test_strict_signature_is_fully_described(self):
        async def service(*, organization_id, name, quantity=1, note=None):
            ...

        contract = contract_from_callable(service)

        assert set(contract["accepted"]) == {"name", "quantity", "note"}
        assert contract["required"] == ("name",)
        assert contract["accepts_extra"] is False

    def test_router_injected_argument_is_never_required(self):
        async def service(*, organization_id, name="x"):
            ...

        contract = contract_from_callable(service)

        assert "organization_id" not in contract["accepted"]
        assert contract["required"] == ()

    def test_var_keyword_callable_tolerates_unknown_names(self):
        async def service(*, organization_id, name="x", **extra):
            ...

        contract = contract_from_callable(service)

        assert contract["accepts_extra"] is True
        assert validate_arguments("t", {"whatever": 1}, contract) == []


# ---------------------------------------------------------------------------
# Validating a proposed call
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    def test_unknown_parameter_is_named_with_the_valid_list(self):
        contract = {
            "accepted": ("customer_id", "items"),
            "required": ("customer_id",),
            "accepts_extra": False,
        }

        violations = validate_arguments(
            "create_invoice", {"customer_id": "c1", "line_items": []}, contract
        )

        assert len(violations) == 1
        assert "'line_items'" in violations[0]
        assert "customer_id" in violations[0]  # the valid list is shown
        assert "items" in violations[0]

    def test_missing_required_parameter_is_reported(self):
        contract = {
            "accepted": ("customer_id", "items"),
            "required": ("customer_id",),
            "accepts_extra": False,
        }

        violations = validate_arguments("create_invoice", {"items": []}, contract)

        assert len(violations) == 1
        assert "required parameter 'customer_id' is missing" in violations[0]

    def test_unknown_and_missing_are_reported_together(self):
        contract = {
            "accepted": ("customer_id",),
            "required": ("customer_id",),
            "accepts_extra": False,
        }

        violations = validate_arguments("create_invoice", {"line_items": []}, contract)

        assert len(violations) == 2
        assert any("'line_items'" in v for v in violations)
        assert any("'customer_id' is missing" in v for v in violations)

    def test_valid_arguments_pass(self):
        contract = {
            "accepted": ("customer_id", "items"),
            "required": ("customer_id",),
            "accepts_extra": False,
        }

        assert validate_arguments(
            "create_invoice", {"customer_id": "c1", "items": []}, contract
        ) == []

    def test_unknown_tool_contract_is_never_a_rejection(self):
        assert validate_arguments("mystery_tool", {"anything": 1}, None) == []
        assert validate_calls(
            [{"tool_name": "mystery_tool", "arguments": {"anything": 1}}], {}
        ) == []

    def test_router_owned_arguments_are_not_unknown(self):
        contract = {
            "accepted": ("customer_id",),
            "required": (),
            "accepts_extra": False,
        }

        for key in PROTOCOL_ARGUMENTS:
            assert validate_arguments("create_invoice", {key: "x"}, contract) == []

    def test_non_dict_arguments_are_ignored(self):
        contract = {"accepted": ("a",), "required": (), "accepts_extra": False}
        assert validate_arguments("t", ["not", "a", "dict"], contract) == []


# ---------------------------------------------------------------------------
# The real contract of the tool that failed in production
# ---------------------------------------------------------------------------


class TestCreateInvoiceContract:
    def test_python_is_the_authority_not_the_stale_table(self):
        contract = _contracts().get("create_invoice")

        assert contract is not None
        # Parameters ai.tool_parameters does not list at all.
        for name in ("payment_terms_days", "discount_total", "terms", "created_by"):
            assert name in contract["accepted"], name
        # `subtotal` is defaulted and recomputed from the validated lines:
        # five SUCCESSFUL production invoices were recorded without it.
        assert contract["required"] == ("customer_id",)

    def test_production_payload_is_rejected_before_confirmation(self):
        """Test A — the incident's plan can no longer reach the user."""
        violations = validate_calls(
            [{"tool_name": "create_invoice", "arguments": PRODUCTION_INVOICE_ARGS}],
            _contracts(),
        )

        assert violations, "the un-bindable call must be rejected"
        message = " ".join(violations)
        assert "'line_items'" in message
        assert "'customer_name'" in message
        assert "'tax_category'" in message
        assert "items" in message  # the valid parameter list is shown

    def test_the_working_production_shape_passes(self):
        """Test B — no false rejection: the shape that recorded 19 invoices."""
        assert validate_calls(
            [{"tool_name": "create_invoice", "arguments": WORKING_INVOICE_ARGS}],
            _contracts(),
        ) == []

    def test_only_the_offending_call_in_a_multi_step_plan_is_flagged(self):
        calls = [
            {"tool_name": "create_customer", "arguments": {"name": "ABC Furnitures"}},
            {"tool_name": "create_invoice", "arguments": PRODUCTION_INVOICE_ARGS},
        ]

        violations = validate_calls(calls, _contracts())

        assert not any("create_customer" in v for v in violations)
        assert any("create_invoice" in v for v in violations)

    def test_historical_expense_payload_is_accepted(self):
        """Test E — payee_name/payment_mode recorded three real expenses."""
        args = {
            "subtotal": 85000,
            "payee_name": "XYZ Computers",
            "description": "Purchase of laptop",
            "expense_date": "2026-01-15",
            "payment_mode": "CASH",
        }

        assert validate_calls(
            [{"tool_name": "create_expense", "arguments": args}], _contracts()
        ) == []


class TestDeclaredContracts:
    def test_every_declared_contract_is_well_formed(self):
        contracts = _contracts()

        assert contracts, "the registry must declare argument contracts"
        for slug, contract in contracts.items():
            assert contract["accepted"], slug
            assert set(contract["required"]) <= set(contract["accepted"]), slug
            assert "organization_id" not in contract["accepted"], slug

    def test_the_mutations_the_model_plans_are_covered(self):
        contracts = _contracts()

        for slug in (
            "create_invoice", "create_purchase_bill", "create_expense",
            "create_customer", "create_supplier", "create_product",
            "create_service", "record_customer_receipt", "record_supplier_payment",
            "convert_quotation", "prepare_journal", "post_journal",
            "register_fixed_asset", "create_project",
        ):
            assert slug in contracts, slug


# ---------------------------------------------------------------------------
# Integration with the reasoning gate (violations are fed back to the model)
# ---------------------------------------------------------------------------


def _proposal_outcome(arguments: Dict[str, Any]):
    """Build a REAL proposal outcome through the production parser."""
    from app.accounting_reasoning import _outcome_from_parsed

    return _outcome_from_parsed(
        {
            "understanding": {
                "economic_event": "Credit sale of inventory (chairs) to a new customer",
                "what_user_wants": "Invoice 2 chairs at the catalog price, due in 30 days",
                "basis": "Catalog shows 'chairs' at 16,666.67 each",
                "event_type": "new_event",
            },
            "proposal": {
                "interpretation": (
                    "Standard credit sale: the invoice debits Accounts Receivable "
                    "and credits chairs Sales."
                ),
                "affected_records": ["customer:ABC Furnitures", "sales invoice"],
                "accounting_impact": [
                    {"account": "Accounts Receivable", "debit": 33333.34},
                    {"account": "chairs Sales", "credit": 33333.34},
                ],
                "not_affected": ["cash/bank", "inventory quantity"],
                "unresolved_uncertainty": [],
                "confirmation": (
                    "Create customer 'ABC Furnitures' and issue the invoice for "
                    "2 chairs at 16,666.67 each?"
                ),
                "tools": [{"tool_name": "create_invoice", "arguments": arguments}],
            },
        },
        rounds=1,
    )


class TestReasoningGateIntegration:
    def test_unbindable_arguments_are_rejected_by_the_gate(self):
        """The incident's proposal is refused while the model can still fix it."""
        from app.accounting_reasoning import validate_outcome

        violations = validate_outcome(
            _proposal_outcome(PRODUCTION_INVOICE_ARGS),
            offered_tools=("create_invoice",),
            tool_contracts=_contracts(),
        )

        assert any("'line_items'" in v for v in violations)

    def test_bindable_plan_passes_the_gate(self):
        from app.accounting_reasoning import validate_outcome

        assert validate_outcome(
            _proposal_outcome(WORKING_INVOICE_ARGS),
            offered_tools=("create_invoice",),
            tool_contracts=_contracts(),
        ) == []

    def test_gate_is_backwards_compatible_without_contracts(self):
        """No contracts supplied ⇒ no contract violations (existing callers)."""
        from app.accounting_reasoning import validate_outcome

        violations = validate_outcome(
            _proposal_outcome(PRODUCTION_INVOICE_ARGS),
            offered_tools=("create_invoice",),
        )

        assert all("line_items" not in v for v in violations)
