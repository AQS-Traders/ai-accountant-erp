"""
ERP AI Agent - Migration Contract Tests (WS-E / WS-F)
======================================================
The database is the enforcement layer for the audit trail (049) and the
edit/delete rollout (050). pytest mocks the DB client, so these tests
verify the MIGRATION CONTRACT: the shipped SQL must contain the exact
policies, triggers and refusal semantics per module. Live behavioural
verification is performed against the test org via SQL smoke.

Guards against accidental weakening: any change that removes a policy,
downgrades has_org_role(org, 2), or drops a status-guard trigger fails
this suite.
"""

from __future__ import annotations

import pathlib
import re

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations"

MODULES = ["quotations", "purchase_bills", "invoices", "expenses", "payments", "receipts"]


def _migration(number_prefix: str) -> str:
    files = sorted(MIGRATIONS.glob(f"{number_prefix}_*.sql"))
    assert files, f"migration {number_prefix} is missing from {MIGRATIONS}"
    return files[0].read_text(encoding="utf-8")


class TestAuditTrailContract:
    """049: journal edit/delete/reverse audit trail."""

    def test_audit_trigger_covers_update_and_delete(self):
        sql = _migration("049")
        assert "trg_journal_entries_audit" in sql
        assert "AFTER UPDATE OR DELETE ON public.journal_entries" in sql
        assert "FOR EACH ROW" in sql

    def test_audit_records_all_three_actions(self):
        sql = _migration("049")
        assert "'REVERSE'" in sql, "posted-reversal audit row missing"
        assert "'DELETE'" in sql, "draft-delete audit row missing"
        assert "'UPDATE'" in sql, "edit before/after audit row missing"

    def test_audit_rows_carry_before_after_and_actor(self):
        sql = _migration("049")
        assert "old_values" in sql and "new_values" in sql
        assert "auth.uid()" in sql, "actor identity missing"
        assert "actor_type" in sql

    def test_audit_function_is_security_definer_with_pinned_search_path(self):
        sql = _migration("049")
        fn = sql[sql.index("audit_journal_entry_change"):]
        assert "SECURITY DEFINER" in fn
        assert "SET search_path" in fn

    def test_delete_action_enum_value_added(self):
        sql = _migration("049")
        assert "ADD VALUE IF NOT EXISTS 'DELETE'" in sql


class TestEditDeleteRolloutContract:
    """050: journal permission model applied to all six modules."""

    def test_migration_covers_all_six_modules(self):
        sql = _migration("050")
        for table in MODULES:
            assert f"trg_{table}_lifecycle_guard" in sql, table

    def test_delete_rls_restricted_to_owner_admin(self):
        sql = _migration("050")
        for table in MODULES:
            assert re.search(
                rf"CREATE POLICY {table}_delete ON public.{table}\s*\n\s*FOR DELETE TO authenticated "
                rf"USING \(has_org_role\(organization_id, 2::smallint\)\)",
                sql,
            ), f"{table}: owner/admin-only DELETE policy missing"

    def test_member_rls_select_insert_update(self):
        sql = _migration("050")
        for table in MODULES:
            assert f"{table}_select" in sql
            assert f"{table}_insert" in sql
            assert f"{table}_update" in sql
        assert sql.count("is_org_member(organization_id)") >= 6 * 3

    def test_linked_journal_delete_refusal(self):
        sql = _migration("050")
        assert "cannot be deleted because a linked journal entry exists" in sql

    def test_draft_only_deletion_rule(self):
        sql = _migration("050")
        assert "Only DRAFT %ss can be deleted" in sql

    def test_terminal_states_locked(self):
        sql = _migration("050")
        assert "'CONVERTED'::quotation_status" in sql   # quotations
        assert "'VOIDED'::bill_status" in sql           # purchase bills
        assert "'CREDITED'::invoice_status" in sql      # invoices
        assert "'CANCELLED'::payment_status" in sql     # payments/receipts

    def test_payment_allocation_guards(self):
        sql = _migration("050")
        assert "remove the allocations first" in sql
        assert "payment_allocations" in sql and "receipt_allocations" in sql

    def test_paid_expense_reversal_path(self):
        sql = _migration("050")
        assert "Paid expense cannot be edited - record a reversal instead." in sql


class TestReversePermissionFix:
    """051: the Journal page's Reverse action must be callable by
    authenticated Owner/Admins (live 403 defect)."""

    def test_reverse_function_granted_to_authenticated(self):
        sql = _migration("051")
        assert (
            "GRANT EXECUTE ON FUNCTION public.reverse_journal_entry(uuid, date, text, uuid) TO authenticated"
            in sql
        )

    def test_reverse_function_has_internal_owner_admin_guard(self):
        sql = _migration("051")
        assert "has_org_role(v_entry.organization_id, 2::smallint)" in sql
        assert "Only the Owner or an Administrator can reverse journal entries" in sql

    def test_reverse_function_is_security_definer_with_pinned_search_path(self):
        sql = _migration("051")
        fn = sql[sql.index("reverse_journal_entry"):]
        assert "SECURITY DEFINER" in fn
        assert "SET search_path TO 'public', 'extensions'" in fn


class TestWorkerJobsContract:
    """052: DB-backed claim/lease queue for background AI runs."""

    def test_worker_jobs_table_exists_with_lease_columns(self):
        sql = _migration("052")
        for column in ("claimed_by", "claimed_at", "lease_expires_at", "payload", "result"):
            assert column in sql
        assert "ENABLE ROW LEVEL SECURITY" in sql

    def test_claim_rpc_uses_skip_locked_and_reclaims_expired_leases(self):
        sql = _migration("052")
        assert "FOR UPDATE SKIP LOCKED" in sql
        assert "lease_expires_at < now()" in sql

    def test_claim_rpc_granted_to_service_role_only(self):
        sql = _migration("052")
        assert (
            "GRANT EXECUTE ON FUNCTION ai.claim_worker_job(text, int) TO service_role"
            in sql
        )
