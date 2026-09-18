"""Work Stream R4 — settlement ledger correctness.

R4.1: the CASH ledger is separate from the BANK ledger — a CASH
receipt/payment hits the cash GL, a bank one hits the bank GL, and a
missing cash account is a configuration gap (raised, never crossed).
R4.3: the party side follows the BUSINESS OPERATION — an advance credits
a customer-advances liability (receipt) / debits a supplier-advances
asset (payment); a loan hits a loan account; fallbacks WARN.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import payment_service

ORG = uuid.UUID(int=1)
CUSTOMER = uuid.UUID(int=2)
SUPPLIER = uuid.UUID(int=3)
CASH_GL = uuid.UUID(int=10)
BANK_GL = uuid.UUID(int=11)
RECEIVABLE_GL = uuid.UUID(int=12)
PAYABLE_GL = uuid.UUID(int=13)
ADVANCE_LIAB_GL = uuid.UUID(int=14)
LOAN_GL = uuid.UUID(int=15)
ADVANCE_ASSET_GL = uuid.UUID(int=16)


class TestSettlementChannelRouting:
    """R4.1: the money side follows the channel — never crossed."""

    @pytest.mark.asyncio
    async def test_cash_receipt_hits_the_cash_gl(self):
        with patch.object(
            payment_service, "_resolve_cash_gl_account",
            new=AsyncMock(return_value=CASH_GL),
        ) as cash_r, patch.object(
            payment_service, "_resolve_bank_gl_account",
            new=AsyncMock(return_value=BANK_GL),
        ) as bank_r:
            gl = await payment_service._resolve_settlement_gl(
                ORG, payment_method="CASH",
            )
        assert gl == CASH_GL
        cash_r.assert_awaited_once()
        bank_r.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bank_receipt_hits_the_bank_gl(self):
        with patch.object(
            payment_service, "_resolve_cash_gl_account",
            new=AsyncMock(return_value=CASH_GL),
        ) as cash_r, patch.object(
            payment_service, "_resolve_bank_gl_account",
            new=AsyncMock(return_value=BANK_GL),
        ) as bank_r:
            gl = await payment_service._resolve_settlement_gl(
                ORG, payment_method="BANK_TRANSFER",
            )
        assert gl == BANK_GL
        bank_r.assert_awaited_once()
        cash_r.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_cash_account_is_a_configuration_gap(self):
        with patch.object(
            payment_service.bank_repo, "get_default_cash_account",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(ValueError, match="cash account"):
                await payment_service._resolve_cash_gl_account(ORG, None)

    @pytest.mark.asyncio
    async def test_default_cash_drawer_is_used_when_configured(self):
        with patch.object(
            payment_service.bank_repo, "get_default_cash_account",
            new=AsyncMock(
                return_value={"id": "x", "gl_account_id": str(CASH_GL)}
            ),
        ):
            gl = await payment_service._resolve_cash_gl_account(ORG, None)
        assert gl == CASH_GL

    @pytest.mark.asyncio
    async def test_full_cash_receipt_journals_the_cash_ledger(self):
        """End-to-end service call: a CASH receipt debits the CASH GL and
        credits the receivable — the bank GL is never touched."""
        async def _rpc(name, *, params=None):
            assert name == "create_receipt_atomic", name
            return {
                "receipt": {"id": str(uuid.UUID(int=99)), "amount": 5000.0},
                "journal_entry_id": str(uuid.UUID(int=98)),
            }

        rpc = AsyncMock(side_effect=_rpc)
        with patch.object(
            payment_service, "call_rpc", new=rpc,
        ), patch.object(
            payment_service, "_resolve_settlement_gl",
            new=AsyncMock(return_value=CASH_GL),
        ) as settle, patch.object(
            payment_service, "_resolve_receipt_credit_account",
            new=AsyncMock(return_value=(RECEIVABLE_GL, None)),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ):
            await payment_service.record_customer_receipt(
                organization_id=ORG,
                customer_id=CUSTOMER,
                amount=5000.0,
                payment_method="CASH",
                transaction_nature="ALLOCATION",
            )
        settle.assert_awaited_once()
        assert settle.await_args.kwargs["payment_method"] == "CASH"
        lines = rpc.await_args.kwargs["params"]["p_journal"]["lines"]
        assert lines[0]["account_id"] == str(CASH_GL)
        assert lines[0]["debit"] == 5000.0
        assert lines[1]["account_id"] == str(RECEIVABLE_GL)
        assert lines[1]["credit"] == 5000.0


class TestNatureAwarePartySide:
    """R4.3: the credit/debit side follows the BUSINESS OPERATION."""

    @pytest.mark.asyncio
    async def test_advance_receipt_credits_advances_liability(self):
        with patch.object(
            payment_service, "_resolve_account_by_keywords",
            new=AsyncMock(return_value={"id": str(ADVANCE_LIAB_GL)}),
        ):
            gl, warning = await payment_service._resolve_receipt_credit_account(
                ORG, CUSTOMER, "ADVANCE"
            )
        assert gl == ADVANCE_LIAB_GL
        assert warning is None

    @pytest.mark.asyncio
    async def test_advance_receipt_falls_back_with_a_warning(self):
        with patch.object(
            payment_service, "_resolve_account_by_keywords",
            new=AsyncMock(return_value=None),
        ), patch.object(
            payment_service, "_resolve_customer_receivable",
            new=AsyncMock(return_value=RECEIVABLE_GL),
        ):
            gl, warning = await payment_service._resolve_receipt_credit_account(
                ORG, CUSTOMER, "ADVANCE"
            )
        assert gl == RECEIVABLE_GL
        assert warning and "ADVANCE" in warning

    @pytest.mark.asyncio
    async def test_loan_receipt_credits_loan_account(self):
        with patch.object(
            payment_service, "_resolve_account_by_keywords",
            new=AsyncMock(return_value={"id": str(LOAN_GL)}),
        ):
            gl, warning = await payment_service._resolve_receipt_credit_account(
                ORG, CUSTOMER, "LOAN_OR_SETTLEMENT"
            )
        assert gl == LOAN_GL
        assert warning is None

    @pytest.mark.asyncio
    async def test_invoice_settlement_receipt_credits_receivable(self):
        with patch.object(
            payment_service, "_resolve_customer_receivable",
            new=AsyncMock(return_value=RECEIVABLE_GL),
        ) as receivable_r:
            gl, warning = await payment_service._resolve_receipt_credit_account(
                ORG, CUSTOMER, "ALLOCATION"
            )
        assert gl == RECEIVABLE_GL
        assert warning is None
        receivable_r.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_advance_payment_debits_supplier_advances_asset(self):
        with patch.object(
            payment_service, "_resolve_account_by_keywords",
            new=AsyncMock(return_value={"id": str(ADVANCE_ASSET_GL)}),
        ):
            gl, warning = await payment_service._resolve_payment_debit_account(
                ORG, SUPPLIER, "ADVANCE"
            )
        assert gl == ADVANCE_ASSET_GL
        assert warning is None

    @pytest.mark.asyncio
    async def test_advance_payment_falls_back_with_a_warning(self):
        with patch.object(
            payment_service, "_resolve_account_by_keywords",
            new=AsyncMock(return_value=None),
        ), patch.object(
            payment_service, "_resolve_supplier_payable",
            new=AsyncMock(return_value=PAYABLE_GL),
        ):
            gl, warning = await payment_service._resolve_payment_debit_account(
                ORG, SUPPLIER, "ADVANCE"
            )
        assert gl == PAYABLE_GL
        assert warning and "Advance" in warning

    @pytest.mark.asyncio
    async def test_bill_settlement_payment_debits_payable(self):
        with patch.object(
            payment_service, "_resolve_supplier_payable",
            new=AsyncMock(return_value=PAYABLE_GL),
        ):
            gl, warning = await payment_service._resolve_payment_debit_account(
                ORG, SUPPLIER, "ALLOCATION"
            )
        assert gl == PAYABLE_GL
        assert warning is None