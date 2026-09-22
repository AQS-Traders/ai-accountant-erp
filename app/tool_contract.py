"""Tool argument contracts — Python is the authority.

Why this exists
---------------
The accounting-reasoning model authors ``proposal.tools[].arguments`` itself,
and nothing checked those names against the Python callable that would receive
them.  A plan could therefore be shown to the user, approved, executed — and
die at CALL-BINDING time:

    production incident 2026-09-20 (session 6a48a432, create_invoice)
    TypeError: create_invoice() got an unexpected keyword argument 'line_items'

The model had invented ``line_items`` / ``customer_name`` / ``tax_category``
because the reasoning prompt offers tool *slugs only* — no parameter contract is
ever shown.  Python must therefore reject the un-bindable call BEFORE the user is
asked to approve it, and say precisely which name is wrong.

Source of truth
---------------
The contract is DERIVED FROM THE SERVICE SIGNATURE (``inspect``) at import time:
never hand-maintained, never stale.

``ai.tool_parameters`` is deliberately NOT the source of truth — it is provably
out of date:

* it omits ``payment_terms_days``, ``discount_total``, ``terms`` and
  ``created_by`` although ``invoice_service.create_invoice`` accepts them;
* it marks ``subtotal`` as required for create_invoice although the service
  defaults it and recomputes the header from the validated lines — five
  SUCCESSFUL production invoices were recorded without it;
* three SUCCESSFUL production ``create_expense`` calls carry ``payee_name`` /
  ``payment_mode``, which that table does not list (the service accepts them).

A rule built on that table would have rejected working calls.  A rule built on
the signature is exactly the binding Python performs, so it can only ever reject
what would have raised.

Fail-open on *validation*, fail-closed on *execution*: a tool with no declared
contract is not validated here (it keeps today's behaviour), and a contract
that cannot be derived is simply absent.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Arguments the ROUTER itself owns and consumes before dispatch, so they are
# never part of a service signature and must never be reported as unknown:
#   * idempotency_key — popped by tool_router for the exactly-once claim;
#   * transaction_date — the transaction-date protocol's input alias, mapped
#     onto the tool's native date parameter by tool_router.
PROTOCOL_ARGUMENTS = frozenset({"idempotency_key", "transaction_date"})

# Injected by the router on every call; never expected from the model.
ROUTER_ARGUMENTS = frozenset({"organization_id"})


def contract_from_callable(fn: Callable[..., Any]) -> Dict[str, Any]:
    """Derive the argument contract of *fn* from its signature.

    ``accepted``      — every keyword the callable binds (excluding the
                        router-injected ``organization_id``).
    ``required``      — the subset that has NO default, i.e. a call omitting it
                        cannot bind.
    ``accepts_extra`` — the callable has ``**kwargs``, so unknown names are
                        silently tolerated and must NOT be rejected.
    """
    signature = inspect.signature(fn)
    accepted: List[str] = []
    required: List[str] = []
    accepts_extra = False
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_extra = True
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if name in ROUTER_ARGUMENTS:
            continue
        accepted.append(name)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "accepted": tuple(accepted),
        "required": tuple(required),
        "accepts_extra": accepts_extra,
    }


def validate_arguments(
    tool_name: str,
    arguments: Any,
    contract: Optional[Dict[str, Any]] = None,
    reference_inputs: Optional[Dict[str, Sequence[str]]] = None,
) -> List[str]:
    """Return violations for one proposed call, or ``[]`` when it is bindable.

    ``[]`` also means "cannot judge": an absent contract (a tool that declares
    none) is never a rejection — this layer must not invent strictness.

    ``reference_inputs`` maps a canonical parameter to the DECLARED alias keys a
    plan may use instead (``{"customer_id": ("customer_name", …)}``).  Those
    aliases are read-only INPUTS to resolution: plan materialization turns them
    into the canonical id before the tool ever runs
    (see app/plan_materialization.py).  An alias therefore satisfies BOTH the
    unknown-key check and the required check for its parameter.

    Without this, the gate forbids the only shape a model can express when the
    referenced record does not exist yet — it has no id to pass, and inventing
    one is forbidden — so the plan could never be built (production
    2026-09-22, session 0076d9a0: three proposals rejected, reasoning exhausted,
    the request degraded to a preparatory plan and failed).
    """
    if not contract or not isinstance(arguments, dict):
        return []

    accepted = tuple(contract.get("accepted") or ())
    ref_inputs = reference_inputs or {}
    alias_keys = {
        alias for aliases in ref_inputs.values() for alias in (aliases or ())
    }
    violations: List[str] = []

    if not contract.get("accepts_extra"):
        unknown = sorted(
            key for key in arguments
            if key not in accepted
            and key not in PROTOCOL_ARGUMENTS
            and key not in alias_keys
        )
        if unknown:
            valid = ", ".join(sorted(accepted))
            aliases_shown = ", ".join(sorted(alias_keys))
            hint = f" Declared reference inputs may also be used: {aliases_shown}." if aliases_shown else ""
            violations.append(
                f"{tool_name}: {', '.join(repr(u) for u in unknown)} is not a "
                f"parameter of this tool — the call would fail before anything "
                f"ran. Valid parameters: {valid}.{hint}"
            )

    missing = sorted(
        name for name in (contract.get("required") or ())
        if name not in arguments
        and not any(alias in arguments for alias in (ref_inputs.get(name) or ()))
    )
    if missing:
        violations.append(
            f"{tool_name}: required parameter "
            f"{', '.join(repr(m) for m in missing)} is missing — the tool "
            "cannot run without it. Resolve it from the live books (a record "
            "that already exists supplies its id) or ask the user; never "
            "invent an id."
        )

    return violations


def validate_calls(
    calls: Sequence[Dict[str, Any]],
    contracts: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_inputs: Optional[Dict[str, Dict[str, Sequence[str]]]] = None,
) -> List[str]:
    """Validate a list of ``{"tool_name": ..., "arguments": {...}}`` entries.

    ``reference_inputs`` is keyed by tool slug (see ``validate_arguments``).
    """
    if not contracts:
        return []
    per_tool_inputs = reference_inputs or {}
    violations: List[str] = []
    for call in calls or ():
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or "").strip()
        if not tool_name:
            continue
        violations.extend(
            validate_arguments(
                tool_name,
                call.get("arguments") or {},
                contracts.get(tool_name),
                per_tool_inputs.get(tool_name),
            )
        )
    return violations


def contract_summary(contract: Dict[str, Any]) -> str:
    """Human-readable one-liner (used by tests and diagnostics)."""
    accepted = ", ".join(contract.get("accepted") or ())
    required = ", ".join(contract.get("required") or ()) or "none"
    return (
        f"accepted: {accepted} | required: {required} | "
        f"accepts extra: {bool(contract.get('accepts_extra'))}"
    )

