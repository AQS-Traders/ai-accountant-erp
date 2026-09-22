"""
ERP AI Agent - Semantic Tool Shortlister (Work Stream E)
=========================================================
Deterministic intent -> tool-subset mapping.  A mutation intent only
needs ITS mutation tools plus the lookups for the entity types it can
touch - offering all 50 tools to the model wastes context and invites
wrong-tool calls.

Guarantees (never weakened):
* The shortlist ALWAYS includes every tool the planner listed in
  ``potential_tools`` for the intent (planner is authoritative).
* Lookups are scoped to the entity types the intent involves, keeping
  the offered set <= 15 tools.
* Batch plans keep the FULL toolset - every sub-intent needs its own
  tools (handled by the caller, which skips shortlisting for batches).
* Read-only/report intents return no exclusions (the full set is safe).
"""

from __future__ import annotations

from typing import Iterable, Set

# Lookup tools, grouped by entity type.  An intent gets the lookups of
# the entity types it actually involves - never the whole lookup set.
_CUSTOMER_LOOKUPS = {"search_customer", "get_customer", "get_customer_ledger"}
_SUPPLIER_LOOKUPS = {"search_supplier", "get_supplier", "get_supplier_ledger"}
_ACCOUNT_LOOKUPS = {"search_account", "get_chart_of_accounts"}
_PRODUCT_LOOKUPS = {"search_product", "search_service"}
_ASSET_LOOKUPS = {"search_fixed_asset", "get_fixed_asset"}
_BANK_LOOKUPS = {"list_bank_accounts"}

# Per-intent mutation/creation toolsets (the trusted core actions).
_INTENT_TOOLS: dict[str, set[str]] = {
    "record_expense": {
        "classify_expense", "create_expense",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "record_cash_purchase": {
        "create_supplier", "create_purchase_bill",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "record_credit_purchase": {
        "create_supplier", "create_purchase_bill",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "record_purchase": {
        "create_supplier", "create_purchase_bill",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "record_cash_sale": {"create_customer", "record_cash_sale"},
    "record_credit_sale": {
        "create_customer", "create_invoice",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "record_sale": {
        "create_customer", "create_invoice",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "create_invoice": {
        "create_customer", "create_invoice",
        "prepare_journal", "validate_journal", "post_journal",
    },
    "create_quotation": {"create_customer", "create_quotation"},
    "convert_quotation": {
        "create_invoice", "prepare_journal", "validate_journal", "post_journal",
    },
    "create_credit_note": {
        "create_credit_note", "prepare_journal", "post_journal",
    },
    "create_purchase_return": {
        "create_purchase_return", "prepare_journal", "post_journal",
    },
    "record_receipt": {
        "record_customer_receipt", "prepare_journal", "post_journal",
    },
    "record_payment": {
        "record_supplier_payment", "prepare_journal", "post_journal",
    },
    "record_expense_payment": {
        "record_expense_payment", "prepare_journal", "post_journal",
    },
    "record_bank_transfer": {
        "record_bank_transfer", "prepare_journal", "post_journal",
    },
    "create_bank_account": {"create_bank_account"},
    "register_fixed_asset": {
        "register_fixed_asset", "prepare_journal", "post_journal",
    },
    "dispose_fixed_asset": {
        "dispose_fixed_asset", "prepare_journal", "post_journal",
    },
    "record_asset_depreciation": {
        "record_asset_depreciation", "prepare_journal", "post_journal",
    },
    "create_product": {"create_product"},
    "create_service": {"create_service"},
}


def intent_tools(intent: str) -> Set[str]:
    """The TRUSTED core toolset that carries *intent* out (public accessor).

    An intent is not always spelled like its tool: ``record_expense`` is carried
    out by ``create_expense``, and ``record_sale`` / ``record_credit_sale`` by
    ``create_invoice``.  Anything asking "which TOOL performs this intent?" must
    ask here instead of comparing the intent string with a tool slug — that
    comparison silently matched NOTHING for those intents.
    """
    return set(_INTENT_TOOLS.get(intent, set()))


def _lookups_for_intent(intent: str) -> Set[str]:
    """Lookups for the entity types the intent involves."""
    lookups: Set[str] = set()
    if any(w in intent for w in (
        "purchase", "expense", "payment", "supplier", "return",
    )):
        lookups |= _SUPPLIER_LOOKUPS | _ACCOUNT_LOOKUPS | _PRODUCT_LOOKUPS
    if any(w in intent for w in (
        "sale", "invoice", "quotation", "receipt", "customer", "credit_note",
    )):
        lookups |= _CUSTOMER_LOOKUPS | _PRODUCT_LOOKUPS
    if "asset" in intent:
        lookups |= _ASSET_LOOKUPS | _ACCOUNT_LOOKUPS
    if "bank" in intent or "transfer" in intent or "receipt" in intent:
        lookups |= _BANK_LOOKUPS
    if intent in ("record_expense", "create_credit_note",
                  "create_purchase_return"):
        lookups |= _ACCOUNT_LOOKUPS
    return lookups


def tools_for_intent(
    intent: str,
    planner_tools: Iterable[str] = (),
) -> Set[str]:
    """The deterministic shortlist for *intent*.

    Always includes every planner-listed tool (the planner is
    authoritative - the shortlist never narrows below it).
    """
    allowed: Set[str] = set(_INTENT_TOOLS.get(intent, set()))
    allowed |= _lookups_for_intent(intent)
    allowed |= set(planner_tools or ())
    return allowed


def excluded_for_intent(
    intent: str,
    planner_tools: Iterable[str] = (),
    all_tools: Iterable[str] = (),
) -> Set[str]:
    """Tools to EXCLUDE for *intent* (complement of the shortlist).

    Read-only/report intents (and any intent without a shortlist) exclude
    nothing - the full toolset is safe there.
    """
    if intent not in _INTENT_TOOLS:
        return set()
    allowed = tools_for_intent(intent, planner_tools)
    return {t for t in (all_tools or ()) if t not in allowed}
