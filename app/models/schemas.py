"""
ERP AI Agent — Pydantic Schemas
=================================
Type-safe models for every data structure flowing through the agent.

Pydantic v2 is used throughout.  All UUIDs are ``uuid.UUID`` to prevent
invalid identifiers from propagating through the system.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ===================================================================
# Enums
# ===================================================================


class ExecutionStatus(str, Enum):
    """Execution phase (mirrors ai.execution_phase_code).

    Note: this is the *phase* enum written to
    ``ai.execution_sessions.current_phase``; the coarse session lifecycle is
    :class:`SessionStatus` (``ai.session_status_code``).
    """

    RECEIVED = "RECEIVED"
    INTERPRETING = "INTERPRETING"
    PLANNING = "PLANNING"
    CONTEXT_LOADING = "CONTEXT_LOADING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    VALIDATING = "VALIDATING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SessionStatus(str, Enum):
    """Session lifecycle (mirrors ai.session_status_code — 7 values)."""

    PENDING = "PENDING"
    PLANNING = "PLANNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolType(str, Enum):
    QUERY = "QUERY"
    MUTATION = "MUTATION"
    REPORT = "REPORT"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class JournalStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    POSTED = "POSTED"
    REVERSED = "REVERSED"
    VOIDED = "VOIDED"


class DocumentStatus(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    POSTED = "POSTED"
    PAID = "PAID"
    PARTIAL = "PARTIAL"
    VOIDED = "VOIDED"
    OVERDUE = "OVERDUE"


class PaymentDirection(str, Enum):
    """Mirrors the DB enum ``payment_direction`` (migration 034).

    The database contract is authoritative: supplier payments are OUTFLOW,
    customer receipts are INFLOW. Never write INBOUND/OUTBOUND.
    """

    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"


class PaymentMethod(str, Enum):
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    ONLINE = "ONLINE"
    OTHER = "OTHER"


class AccountType(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class NormalBalance(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


# ===================================================================
# Request / Response
# ===================================================================


class UserRequest(BaseModel):
    """Incoming user message from the frontend."""

    message: str = Field(..., min_length=1, max_length=10000)
    conversation_id: Optional[str] = None
    session_id: Optional[uuid.UUID] = None
    attachments: Optional[List[AttachmentRef]] = None


class AttachmentRef(BaseModel):
    """An uploaded attachment carried inline (base64) for vision extraction.

    ``file_url`` remains accepted for backward compatibility (local blob
    preview URLs); actual document content arrives in ``data_base64``.
    """

    file_name: str = Field(..., max_length=255)
    file_url: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    data_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded document content (inline upload)",
    )


# ===================================================================
# Canonical contracts (shared with the frontend types in
# frontend/src/lib/types/api.ts — keep both in sync)
# ===================================================================


class AffectedEntity(BaseModel):
    """Canonical contract for one ERP entity affected by an execution.

    ``type`` is ALWAYS a non-empty, snake_case string (e.g. ``journal_entry``,
    ``supplier``, ``bill``).  The frontend renders this contract directly, so
    the backend guarantees the shape — missing/None/malformed values are
    coerced by the builder, never sent to the client.
    """

    type: str = Field(..., min_length=1, description="Canonical entity type, snake_case")
    action: Optional[str] = Field(
        default=None, description="created | fetched | prepared | validated | posted | reversed | updated"
    )
    id: Optional[str] = None
    name: Optional[str] = None
    number: Optional[str] = None
    total: Optional[float] = None


class AccountingImpactLine(BaseModel):
    """Canonical contract for one journal line shown to the user."""

    account: str = Field(..., min_length=1)
    debit: Optional[float] = None
    credit: Optional[float] = None
    description: Optional[str] = None


class QuestionOption(BaseModel):
    """Work Stream R3.4 — one tap-to-answer option for a clarification
    question.  ``label`` is displayed; ``value`` is what gets sent back."""

    value: str
    label: str


class AgentResponse(BaseModel):
    """Structured response returned to the frontend."""

    status: ExecutionStatus
    execution_id: Optional[uuid.UUID] = None
    action: Optional[str] = None
    summary: Optional[str] = None
    affected_entities: Optional[List[AffectedEntity]] = None
    accounting_impact: Optional[List[AccountingImpactLine]] = None
    verification_status: Optional[str] = None
    requires_user_input: bool = False
    question: Optional[str] = None
    required_information: Optional[List[str]] = None
    # Quick-answer buttons for clarification rounds (e.g. suggested existing
    # accounts + "Create new account" when no matching account exists).
    options: Optional[List[str]] = None
    # Work Stream R3.4 — structured per-question options (data-driven
    # tap-to-answer chips).  Each inner list is aligned with the
    # corresponding numbered sub-question; an inner ``None`` (or empty
    # list) means "free-text question — no chips".
    question_options: Optional[List[Optional[List[QuestionOption]]]] = None
    confirmation_required: bool = False
    risk_level: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options_to_strings(cls, value):
        """Tolerate structured options instead of failing the whole request.

        This field is ``List[str]`` and the frontend renders each entry as a
        tappable chip (it calls ``opt.trim()``), but one producer shipped
        ``{"value": ..., "label": ...}`` dicts.  Pydantic rejected them with

            Input should be a valid string [type=string_type,
            input_value={'value': 'yes', 'label': ...}]

        which propagated out of ``execute()`` and ended the ENTIRE session as
        FAILED — a cosmetic payload mismatch destroying a user's run, and the
        user saw a raw Pydantic error.

        Structured options are therefore flattened to their LABEL, which is
        what the chip displays and what the answer-routing parsers expect
        (e.g. "Create 'X' under 'Y'" -> CREATE).  Anything that cannot be
        rendered as a string is dropped rather than crashing the response.

        The upstream producer was fixed to emit strings, so this is a
        backstop, not the primary contract.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value
        coerced: List[str] = []
        for item in value:
            if isinstance(item, str):
                coerced.append(item)
                continue
            if isinstance(item, dict):
                label = item.get("label") or item.get("value")
                if label is not None:
                    coerced.append(str(label))
                continue
            if item is not None:
                coerced.append(str(item))
        return coerced


class ClarificationResponse(BaseModel):
    """Response when the agent needs more information."""

    status: ExecutionStatus = ExecutionStatus.AWAITING_CLARIFICATION
    execution_id: uuid.UUID
    question: str
    required_information: List[str] = Field(default_factory=list)


class ConfirmationResponse(BaseModel):
    """Response when the agent needs user confirmation."""

    status: ExecutionStatus = ExecutionStatus.AWAITING_CONFIRMATION
    execution_id: uuid.UUID
    action: str
    risk_level: str = "MEDIUM"
    confirmation_required: bool = True
    data: Optional[Dict[str, Any]] = None


class ClarificationAnswer(BaseModel):
    """User's answer to a clarification."""

    session_id: uuid.UUID
    answer: str


class ConfirmationDecision(BaseModel):
    """User's decision on a confirmation request."""

    session_id: uuid.UUID
    approved: bool
    notes: Optional[str] = None


# ===================================================================
# Agent Context
# ===================================================================


class AgentContext(BaseModel):
    """Runtime context passed to Gemini — deliberately minimal."""

    organization: Dict[str, Any]
    user: Dict[str, Any]
    permissions: List[str] = Field(default_factory=list)
    financial_year: Optional[Dict[str, Any]] = None
    accounting_period: Optional[Dict[str, Any]] = None
    relevant_customers: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_suppliers: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_accounts: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_documents: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_projects: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_reports: List[Dict[str, Any]] = Field(default_factory=list)
    relevant_bank_accounts: List[Dict[str, Any]] = Field(default_factory=list)
    # Entities parsed from the user's request (ground truth for Gemini)
    extracted_entities: Dict[str, Any] = Field(default_factory=dict)
    # Prior clarification Q&A (already answered — never re-ask)
    clarification_history: List[Dict[str, str]] = Field(default_factory=list)
    # Work Stream F: learned organization-level defaults.  Rendered into the
    # model prompt as authoritative defaults the user set previously; the
    # planner already treats them as answered-for entities.
    org_preferences: Dict[str, str] = Field(default_factory=dict)
    # Deterministic transaction classification (360° analysis) — see
    # TransactionClassification.  Rendered into the model prompt as
    # authoritative accounting context.
    classification: Optional[Any] = None
    # ECONOMIC EVENT CLASSIFICATION — the underlying real-world business
    # event, its 360° impact map, and prohibited mutations.  Rendered into
    # the model prompt so the LLM plans tools from economic meaning, never
    # from CRUD keyword matching.
    economic_event: Optional[str] = None
    impact_map: Dict[str, Any] = Field(default_factory=dict)
    prohibited_actions: List[Dict[str, str]] = Field(default_factory=list)


# ===================================================================
# Execution Plan
# ===================================================================


class ExecutionPlan(BaseModel):
    """Output of the Planner — describes what the agent intends to do."""

    intent: str
    entity_type: Optional[str] = None
    entity_name: Optional[str] = None
    transaction_type: Optional[str] = None
    required_context: List[str] = Field(default_factory=list)
    potential_tools: List[str] = Field(default_factory=list)
    requires_validation: bool = False
    requires_accounting_engine: bool = False
    requires_confirmation: bool = False
    requires_clarification: bool = False
    clarification_questions: List[str] = Field(default_factory=list)
    # Field names that are genuinely missing (e.g. ["amount"]) — used for
    # audit/UI display; the questions above are the human-readable asks.
    missing_fields: List[str] = Field(default_factory=list)
    # Intelligent transaction classification (360° analysis) — determined
    # AFTER planning by the deterministic classifier using ERP configuration
    # first, then rules, then confident inference (never a guess).
    classification: Optional["TransactionClassification"] = None
    expected_outcome: Optional[str] = None
    # All entities parsed from the request (+ prior clarification answers)
    extracted_entities: Dict[str, Any] = Field(default_factory=dict)
    # ECONOMIC EVENT CLASSIFICATION (first-class stage, determined BEFORE
    # tool selection): what real-world event this request represents, its
    # 360° impact map, and the explicitly PROHIBITED mutations (negative
    # reasoning).  Refined by the classifier once the nature is known.
    economic_event: Optional[str] = None
    impact_map: Dict[str, bool] = Field(default_factory=dict)
    prohibited_actions: List[Dict[str, str]] = Field(default_factory=list)
    # Work Stream B - BATCH TRANSACTIONS: when the request enumerates
    # multiple transactions ("record these 3 expenses: 1) ... 2) ..."),
    # the planner produces one sub-intent per document and merges them
    # into THIS plan. Each entry: {position, segment, intent,
    # missing_fields, requires_confirmation, requires_clarification}.
    # Execution stays SEQUENTIAL (accounting order matters) and each
    # sub-document is independent: one failing document never rolls back
    # its successful siblings.
    batch_items: Optional[List[Dict[str, Any]]] = None
    # Work Stream R - the RESOLVED transaction nature + WHY it was chosen
    # (audit trail / explainability): USER_ANSWER (explicit clarification
    # answer), PREFERENCE (learned org default) or DETERMINISTIC_RULE
    # (intent-inherent, e.g. asset lifecycle).  Never an LLM guess.
    transaction_nature: Optional[str] = None
    transaction_nature_source: Optional[str] = None


# ===================================================================
# Tool Models
# ===================================================================


class ToolCall(BaseModel):
    """A tool call requested by Gemini."""

    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result returned by a tool execution."""

    tool_name: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    # Semantic error normalisation (app/error_normalizer.py) — generic
    # categories like MISSING_REQUIRED_VALUE / DUPLICATE_RECORD /
    # INVALID_REFERENCE / VALIDATION_ERROR / INFRASTRUCTURE_ERROR, plus
    # structured details (entity/field) so the agent can decide between
    # a targeted clarification and a genuine failure.
    error_category: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None


# ===================================================================
# Intelligent Transaction Classification
# ===================================================================


class TransactionClassification(BaseModel):
    """Deterministic classification of a transaction's economic nature.

    Produced by app/classifier.py using the authority hierarchy:
      ERP_CONFIGURATION / ITEM_MAPPING  →  DETERMINISTIC_RULE  →  INFERENCE.
    An explicit user answer (from a classification clarification) is
    USER_ANSWER and always wins.  The LLM receives this as authoritative
    context; the Accounting Engine + Validator remain the execution
    authority.
    """

    transaction_nature: Optional[str] = None  # INVENTORY, FIXED_ASSET, OPERATING_EXPENSE, SERVICE, CONSUMABLE, PREPAYMENT, DEPOSIT_ADVANCE, INTANGIBLE_ASSET, OTHER
    # Suggested existing accounts shown to the user when no dedicated
    # account matches (e.g. 'bonus' → Salaries / Wages / create new).
    candidate_accounts: Optional[List[str]] = None
    confidence: str = "LOW"  # HIGH | MEDIUM | LOW
    source: str = "INFERENCE"  # USER_ANSWER | ERP_CONFIGURATION | ITEM_MAPPING | DETERMINISTIC_RULE | INFERENCE
    entity: Optional[str] = None  # the item/subject being classified (e.g. "laptop")
    account_hint_id: Optional[str] = None  # COA account resolved for the nature (validated by the engine)
    account_hint_code: Optional[str] = None
    account_hint_name: Optional[str] = None
    # Account-creation PROPOSAL (never silent): when the category's ledger
    # is missing, the classifier proposes the canonical account and the
    # user confirms creation (or names an existing account) in the
    # clarification round.  Never auto-created without that confirmation.
    proposed_account_name: Optional[str] = None
    proposed_account_code: Optional[str] = None
    # True when the user ALREADY confirmed creating the proposed account in
    # a prior clarification round — execution must run create_account
    # BEFORE the recording mutation (tool-order guard enforces this).
    create_account_confirmed: bool = False
    requires_clarification: bool = False
    clarification_reason: Optional[str] = None


# ===================================================================
# Validation
# ===================================================================


class ValidationResult(BaseModel):
    """Result of the validator pre-flight check."""

    valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ===================================================================
# Accounting
# ===================================================================


class JournalLineInput(BaseModel):
    """Input for a single journal line."""

    account_id: uuid.UUID
    description: str
    debit: float = 0.0
    credit: float = 0.0
    customer_id: Optional[uuid.UUID] = None
    supplier_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    tax_rate_id: Optional[uuid.UUID] = None


class JournalEntryInput(BaseModel):
    """Input for creating a journal entry."""

    organization_id: uuid.UUID
    transaction_date: date
    description: str
    source_type: Optional[str] = None
    source_id: Optional[uuid.UUID] = None
    currency_code: str = "PKR"
    lines: List[JournalLineInput] = Field(..., min_length=2)


class AccountingEntry(BaseModel):
    """Result of the accounting engine processing."""

    journal_entry_id: Optional[uuid.UUID] = None
    journal_number: Optional[str] = None
    status: JournalStatus = JournalStatus.DRAFT
    total_debit: float = 0.0
    total_credit: float = 0.0
    lines: List[Dict[str, Any]] = Field(default_factory=list)


# ===================================================================
# Report Request
# ===================================================================


class ReportRequest(BaseModel):
    """Request for a financial report."""

    report_type: str
    organization_id: uuid.UUID
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    period_id: Optional[uuid.UUID] = None
    customer_id: Optional[uuid.UUID] = None
    supplier_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    format: str = "json"  # json | pdf | xlsx


# ===================================================================
# Invoice Data (for frontend rendering)
# ===================================================================


class InvoiceLineItem(BaseModel):
    description: str
    quantity: float = 1.0
    unit_price: float
    tax_rate: float = 0.0
    discount: float = 0.0
    amount: float


class InvoiceData(BaseModel):
    """Structured invoice data passed to frontend template."""

    customer_name: str
    invoice_number: Optional[str] = None
    invoice_date: date
    due_date: Optional[date] = None
    currency_code: str = "PKR"
    items: List[InvoiceLineItem] = Field(default_factory=list)
    subtotal: float = 0.0
    tax_total: float = 0.0
    discount_total: float = 0.0
    total: float = 0.0
    notes: Optional[str] = None
    terms: Optional[str] = None
