"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Pencil, Plus, Printer, Search, Trash2, Undo2 } from "lucide-react";
import { createClient } from "@/lib/supabase/client";
import { useOrg } from "@/lib/hooks/useOrg";
import { formatCurrency } from "@/lib/utils/currency";
import Modal from "@/components/shared/Modal";
import PageHeader from "@/components/shared/PageHeader";
import StatusBadge from "@/components/shared/StatusBadge";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/shared/States";
import { cn } from "@/lib/utils/cn";
import type { Account, JournalEntry, JournalLine } from "@/lib/types/entities";

const inputCls =
  "w-full px-3 py-2 rounded-xl bg-bg-primary border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors";

type AccountOption = Pick<Account, "id" | "code" | "name" | "account_type">;
type LineRow = JournalLine & {
  account?: { code?: string; name?: string } | { code?: string; name?: string }[] | null;
};

interface LineDraft {
  account_id: string;
  description: string;
  debit: string;
  credit: string;
}

const today = () => new Date().toISOString().slice(0, 10);

const lineAccountLabel = (l: LineRow) => {
  const acc = Array.isArray(l.account) ? l.account[0] : l.account;
  return acc ? `${acc.code} - ${acc.name}` : "-";
};

export default function JournalPage() {
  const { org, loading: orgLoading } = useOrg();
  const [rows, setRows] = useState<JournalEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  // Edit / delete / reverse
  const [editingId, setEditingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [reversingId, setReversingId] = useState<string | null>(null);
  const [myRole, setMyRole] = useState<string | null>(null);
  const isAdmin = myRole === "OWNER" || myRole === "ADMIN";
  // Action errors (post/delete/reverse) are shown as an INLINE banner so a
  // failed action never replaces the whole list (live-defect fix: a reverse
  // permission error used to blank the page with an ErrorState).
  const [actionError, setActionError] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [linesByEntry, setLinesByEntry] = useState<Record<string, LineRow[]>>({});

  const [accounts, setAccounts] = useState<AccountOption[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [postingId, setPostingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [form, setForm] = useState({
    transaction_date: today(),
    description: "",
    reference: "",
    lines: [
      { account_id: "", description: "", debit: "", credit: "" },
      { account_id: "", description: "", debit: "", credit: "" },
    ] as LineDraft[],
  });

  const load = useCallback(async () => {
    if (!org) return;
    const supabase = createClient();
    let query = supabase
      .from("journal_entries")
      .select("*")
      .eq("organization_id", org.organization_id)
      .order("transaction_date", { ascending: false })
      .order("created_at", { ascending: false });
    if (statusFilter !== "ALL") query = query.eq("status", statusFilter);
    const { data, error: dbError } = await query;
    if (dbError) setError(dbError.message);
    else setRows(data ?? []);
  }, [org, statusFilter]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!org) return;
    const supabase = createClient();
    supabase
      .from("accounts")
      .select("id, code, name, account_type")
      .eq("organization_id", org.organization_id)
      .eq("is_active", true)
      .order("code")
      .then(({ data }) => setAccounts((data as AccountOption[]) ?? []));
  }, [org]);

  const totals = useMemo(() => {
    const debit = form.lines.reduce((s, l) => s + (Number(l.debit) || 0), 0);
    const credit = form.lines.reduce((s, l) => s + (Number(l.credit) || 0), 0);
    return { debit, credit, balanced: Math.abs(debit - credit) < 0.005 };
  }, [form.lines]);

  // Client-side search across number, description, reference and source so
  // users can find an entry instantly without leaving the page.
  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rows ?? [];
    return (rows ?? []).filter((e) =>
      [
        e.journal_number,
        e.description,
        e.reference,
        e.source_type,
        e.transaction_date,
      ]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q))
    );
  }, [rows, search]);

  const toggleExpand = async (entryId: string) => {
    if (expanded === entryId) {
      setExpanded(null);
      return;
    }
    setExpanded(entryId);
    if (!linesByEntry[entryId]) {
      const supabase = createClient();
      const { data } = await supabase
        .from("journal_lines")
        .select("*, account:accounts(code, name)")
        .eq("entry_id", entryId)
        .order("line_number");
      setLinesByEntry((prev) => ({ ...prev, [entryId]: (data as LineRow[]) ?? [] }));
    }
  };

  useEffect(() => {
    if (!org) return;
    const supabase = createClient();
    // Role gate: DELETE is Owner/Admin only (enforced again by DB RLS).
    (async () => {
      const { data: { user } } = await supabase.auth.getUser();
      if (!user) return;
      const { data: m } = await supabase
        .from("organization_members")
        .select("role:organization_roles(code)")
        .eq("user_id", user.id)
        .eq("organization_id", org.organization_id)
        .eq("status", "ACTIVE")
        .limit(1);
      const role = (m?.[0] as { role?: { code?: string } | { code?: string }[] } | undefined);
      const r = Array.isArray(role?.role) ? role?.role[0] : role?.role;
      setMyRole(r?.code ?? null);
    })();
  }, [org]);

  const openEdit = async (e: JournalEntry, ev: React.MouseEvent) => {
    ev.stopPropagation();
    setFormError(null);
    setEditingId(e.id);
    setForm({
      transaction_date: e.transaction_date,
      description: e.description ?? "",
      reference: e.reference ?? "",
      lines: [
        { account_id: "", description: "", debit: "", credit: "" },
        { account_id: "", description: "", debit: "", credit: "" },
      ],
    });
    setModalOpen(true);
    const supabase = createClient();
    const { data } = await supabase
      .from("journal_lines")
      .select("*, account:accounts(code, name)")
      .eq("entry_id", e.id)
      .order("line_number");
    const rows = (data as LineRow[]) ?? [];
    setLinesByEntry((prev) => ({ ...prev, [e.id]: rows }));
    if (rows.length > 0) {
      setForm((f) => ({
        ...f,
        lines: rows.map((l) => ({
          account_id: l.account_id ?? "",
          description: l.description ?? "",
          debit: l.debit > 0.005 ? String(l.debit) : "",
          credit: l.credit > 0.005 ? String(l.credit) : "",
        })),
      }));
    }
  };

  const setLine = (i: number, patch: Partial<LineDraft>) =>
    setForm((f) => ({
      ...f,
      lines: f.lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l)),
    }));

  const handleSave = async () => {
    if (!org) return;
    if (!form.description.trim()) { setFormError("Description is required"); return; }
    const validLines = form.lines.filter(
      (l) => l.account_id && (Number(l.debit) > 0 || Number(l.credit) > 0)
    );
    if (validLines.length < 2) {
      setFormError("A journal entry needs at least two lines (one debit, one credit)");
      return;
    }
    if (!totals.balanced) {
      setFormError("Entry is not balanced - total debits must equal total credits");
      return;
    }
    const bothSides = validLines.some((l) => Number(l.debit) > 0) &&
      validLines.some((l) => Number(l.credit) > 0);
    if (!bothSides) { setFormError("Entry needs both a debit and a credit line"); return; }

    setSaving(true);
    setFormError(null);
    const supabase = createClient();
    const { data: { user } } = await supabase.auth.getUser();

    // EDIT mode: update the DRAFT entry header, then replace its lines.
    if (editingId) {
      const { error: updError } = await supabase
        .from("journal_entries")
        .update({
          transaction_date: form.transaction_date,
          description: form.description.trim(),
          reference: form.reference.trim() || null,
        })
        .eq("id", editingId);
      if (updError) {
        setSaving(false);
        setFormError(updError.message);
        return;
      }
      // Replace lines: delete + reinsert (any member may edit).
      const { error: delError } = await supabase
        .from("journal_lines")
        .delete()
        .eq("entry_id", editingId);
      if (delError) {
        setSaving(false);
        setFormError(delError.message);
        return;
      }
      const { error: insError } = await supabase.from("journal_lines").insert(
        validLines.map((l, i) => ({
          organization_id: org.organization_id,
          entry_id: editingId,
          line_number: i + 1,
          account_id: l.account_id,
          description: l.description.trim() || null,
          debit: Number(l.debit) || 0,
          credit: Number(l.credit) || 0,
        }))
      );
      setSaving(false);
      if (insError) { setFormError(insError.message); return; }
      setModalOpen(false);
      setEditingId(null);
      setForm({
        transaction_date: today(),
        description: "",
        reference: "",
        lines: [
          { account_id: "", description: "", debit: "", credit: "" },
          { account_id: "", description: "", debit: "", credit: "" },
        ],
      });
      setLinesByEntry({});
      load();
      return;
    }

    const { data: entry, error: entryError } = await supabase
      .from("journal_entries")
      .insert({
        organization_id: org.organization_id,
        transaction_date: form.transaction_date,
        description: form.description.trim(),
        reference: form.reference.trim() || null,
        currency_code: org.base_currency_code,
        status: "DRAFT",
        created_by: user?.id ?? null,
      })
      .select("id")
      .single();

    if (entryError || !entry) {
      setSaving(false);
      setFormError(entryError?.message ?? "Failed to create entry");
      return;
    }

    const { error: linesError } = await supabase.from("journal_lines").insert(
      validLines.map((l, i) => ({
        organization_id: org.organization_id,
        entry_id: entry.id,
        line_number: i + 1,
        account_id: l.account_id,
        description: l.description.trim() || null,
        debit: Number(l.debit) || 0,
        credit: Number(l.credit) || 0,
      }))
    );

    setSaving(false);
    if (linesError) { setFormError(linesError.message); return; }
    setModalOpen(false);
    setForm({
      transaction_date: today(),
      description: "",
      reference: "",
      lines: [
        { account_id: "", description: "", debit: "", credit: "" },
        { account_id: "", description: "", debit: "", credit: "" },
      ],
    });
    setLinesByEntry({});
    setExpanded(null);
    load();
  };

  const handlePost = async (entryId: string) => {
    setPostingId(entryId);
    setActionError(null);
    const supabase = createClient();
    const { error: postError } = await supabase
      .from("journal_entries")
      .update({ status: "POSTED" })
      .eq("id", entryId);
    setPostingId(null);
    if (postError) setActionError(postError.message);
    else load();
  };

  // DELETE — Owner/Admin only (UI gate; RLS enforces it again server-side,
  // and the database trigger allows DRAFT entries only — POSTED entries
  // must be REVERSED, never deleted).
  const handleDelete = async (entryId: string, ev: React.MouseEvent) => {
    ev.stopPropagation();
    if (!window.confirm("Delete this draft entry? This cannot be undone.")) return;
    setDeletingId(entryId);
    setActionError(null);
    const supabase = createClient();
    const { error: delError } = await supabase
      .from("journal_entries")
      .delete()
      .eq("id", entryId);
    setDeletingId(null);
    if (delError) setActionError(delError.message);
    else {
      setLinesByEntry((prev) => {
        const next = { ...prev };
        delete next[entryId];
        return next;
      });
      load();
    }
  };

  // REVERSE — the correction path for POSTED entries: the database creates
  // a mirrored entry, posts it, and marks this one REVERSED.
  // Owner/Admin only: the RPC itself authorises has_org_role(org, 2), so
  // the button is disabled (with an explanation) for other roles instead
  // of failing with a 403 after the click.
  const handleReverse = async (entryId: string, ev: React.MouseEvent) => {
    ev.stopPropagation();
    if (!window.confirm("Reverse this posted entry? A mirrored reversal entry will be posted and this entry marked REVERSED.")) return;
    setReversingId(entryId);
    setActionError(null);
    const supabase = createClient();
    const { data: { user } } = await supabase.auth.getUser();
    const { error: revError } = await supabase.rpc("reverse_journal_entry", {
      p_entry_id: entryId,
      p_reason: "Reversed from Journal page",
      p_reversed_by: user?.id ?? null,
    });
    setReversingId(null);
    if (revError) setActionError(revError.message);
    else {
      setLinesByEntry({});
      load();
    }
  };

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <PageHeader
        title="Journal Entries"
        subtitle="Double-entry journal - the database rejects unbalanced postings"
        actions={
          <div className="flex flex-wrap items-center gap-2 no-print">
            <button
              onClick={() => window.print()}
              className="btn-3d-soft flex items-center gap-1.5 px-4 py-2 rounded-xl bg-bg-surface border border-border-subtle text-sm font-medium text-text-secondary hover:text-text-primary hover:border-ai-200 transition-colors"
            >
              <Printer className="w-4 h-4" /> Print
            </button>
            <button
              onClick={() => { setFormError(null); setEditingId(null); setModalOpen(true); }}
              className="btn-3d btn-shine flex items-center gap-1.5 px-4 py-2 rounded-xl bg-gradient-to-b from-ai-500 to-ai-600 hover:from-ai-400 hover:to-ai-600 text-white text-sm font-semibold transition-colors"
            >
              <Plus className="w-4 h-4" /> New Entry
            </button>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-3 no-print">
        <select className={`${inputCls} max-w-44`} value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}>
          {["ALL", "DRAFT", "VALIDATED", "POSTED", "REVERSED", "VOIDED"].map((s) => (
            <option key={s} value={s}>{s === "ALL" ? "All statuses" : s}</option>
          ))}
        </select>
        <div className="relative w-full sm:w-auto">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none" />
          <input
            className={`${inputCls} w-full sm:w-64 pl-9`}
            placeholder="Search number, description, source…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <span className="text-xs text-text-muted hidden md:inline">
          Click a row to see its lines. Sales/purchase documents post their journals via the AI agent.
        </span>
      </div>

      {actionError && (
        <div className="no-print flex items-start justify-between gap-3 rounded-xl border border-error-100 bg-error-50 px-4 py-3">
          <p className="text-sm text-error-600">{actionError}</p>
          <button
            onClick={() => setActionError(null)}
            className="text-xs font-medium text-error-600 hover:text-error-700 shrink-0"
          >
            Dismiss
          </button>
        </div>
      )}

      {orgLoading || (!rows && !error) ? (
        <TableSkeleton rows={8} cols={6} />
      ) : error ? (
        <ErrorState message={error} />
      ) : (rows ?? []).length === 0 ? (
        <EmptyState
          title={statusFilter !== "ALL" ? `No ${statusFilter.toLowerCase()} entries` : "No journal entries yet"}
          hint={statusFilter !== "ALL" ? undefined : "Create a manual entry here, or let the AI record a transaction conversationally."}
        />
      ) : (
        <div className="bg-bg-surface rounded-2xl border border-border-subtle overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-text-muted border-b border-border-subtle">
                <th className="px-3 py-3 font-medium w-8" />
                <th className="px-4 py-3 font-medium">Number</th>
                <th className="px-4 py-3 font-medium">Date</th>
                <th className="px-4 py-3 font-medium">Description</th>
                <th className="px-4 py-3 font-medium">Source</th>
                <th className="px-4 py-3 font-medium text-right">Amount</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((e) => (
                <Fragment key={e.id}>
                  <tr
                    className="border-b border-border-subtle/60 hover:bg-bg-muted/50 transition-colors cursor-pointer"
                    onClick={() => toggleExpand(e.id)}
                  >
                    <td className="px-3 py-3 text-text-muted">
                      {expanded === e.id ? (
                        <ChevronDown className="w-4 h-4" />
                      ) : (
                        <ChevronRight className="w-4 h-4" />
                      )}
                    </td>
                    <td className="px-4 py-3 font-medium text-text-primary tabular-nums">{e.journal_number}</td>
                    <td className="px-4 py-3 text-text-secondary tabular-nums">{e.transaction_date}</td>
                    <td className="px-4 py-3 text-text-primary">
                      {e.description || "-"}
                      {e.reference && (
                        <span className="block text-xs text-text-muted mt-0.5">Ref: {e.reference}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-text-secondary text-xs">{e.source_type ?? "MANUAL"}</td>
                    <td className="px-4 py-3 text-right text-text-primary tabular-nums font-medium">
                      {formatCurrency(e.total_debit, e.currency_code)}
                    </td>
                    <td className="px-4 py-3"><StatusBadge status={e.status} /></td>
                    <td className="px-4 py-3 text-right">
                      {e.status === "DRAFT" && (
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={(ev) => openEdit(e, ev)}
                            title="Edit draft entry"
                            className="p-1 rounded text-text-muted hover:text-ai-600 hover:bg-ai-50 transition-colors"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          {isAdmin && (
                            <button
                              onClick={(ev) => handleDelete(e.id, ev)}
                              disabled={deletingId === e.id}
                              title="Delete draft entry (admin only)"
                              className="p-1 rounded text-text-muted hover:text-error-600 hover:bg-error-50 disabled:opacity-50 transition-colors"
                            >
                              <Trash2 className={cn("w-3.5 h-3.5", deletingId === e.id && "animate-pulse")} />
                            </button>
                          )}
                          <button
                            onClick={(ev) => { ev.stopPropagation(); handlePost(e.id); }}
                            disabled={postingId === e.id}
                            className="text-xs font-medium text-ai-600 hover:text-ai-700 disabled:opacity-50"
                          >
                            {postingId === e.id ? "Posting…" : "Post"}
                          </button>
                        </div>
                      )}
                      {e.status === "POSTED" && (
                        <button
                          onClick={(ev) => handleReverse(e.id, ev)}
                          disabled={reversingId === e.id || !isAdmin}
                          title={
                            isAdmin
                              ? "Reverse this posted entry (creates a mirrored reversal)"
                              : "Only the Owner or an Administrator can reverse entries"
                          }
                          className="inline-flex items-center gap-1 text-xs font-medium text-warning-600 hover:text-warning-700 disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          <Undo2 className="w-3.5 h-3.5" />
                          {reversingId === e.id ? "Reversing…" : "Reverse"}
                        </button>
                      )}
                    </td>
                  </tr>
                  {expanded === e.id && (
                    <tr className="bg-bg-muted/30 border-b border-border-subtle/60">
                      <td colSpan={8} className="px-4 py-3">
                        {(linesByEntry[e.id] ?? []).length === 0 ? (
                          <p className="text-xs text-text-muted py-2">Loading lines…</p>
                        ) : (
                          <div className="rounded-xl border border-border-subtle bg-bg-surface overflow-hidden">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="text-left uppercase tracking-wide text-text-muted border-b border-border-subtle">
                                  <th className="px-3 py-2 font-medium">#</th>
                                  <th className="px-3 py-2 font-medium">Account</th>
                                  <th className="px-3 py-2 font-medium">Description</th>
                                  <th className="px-3 py-2 font-medium text-right">Debit</th>
                                  <th className="px-3 py-2 font-medium text-right">Credit</th>
                                </tr>
                              </thead>
                              <tbody>
                                {(linesByEntry[e.id] ?? []).map((l) => (
                                  <tr key={l.id} className="border-b border-border-subtle/40 last:border-0">
                                    <td className="px-3 py-2 text-text-muted tabular-nums">{l.line_number}</td>
                                    <td className="px-3 py-2 text-text-primary">{lineAccountLabel(l)}</td>
                                    <td className="px-3 py-2 text-text-secondary">{l.description ?? "-"}</td>
                                    <td className="px-3 py-2 text-right text-text-primary tabular-nums">
                                      {l.debit > 0.005 ? formatCurrency(l.debit, e.currency_code) : "-"}
                                    </td>
                                    <td className="px-3 py-2 text-right text-text-primary tabular-nums">
                                      {l.credit > 0.005 ? formatCurrency(l.credit, e.currency_code) : "-"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* New / Edit Journal Entry Modal */}
      <Modal open={modalOpen} onClose={() => { setModalOpen(false); setEditingId(null); }} title={editingId ? "Edit Journal Entry" : "New Journal Entry"} wide>
        <div className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className="text-xs font-medium text-text-secondary">Date *</label>
              <input type="date" className={`${inputCls} mt-1.5`} value={form.transaction_date}
                onChange={(e) => setForm({ ...form, transaction_date: e.target.value })} />
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Reference</label>
              <input className={`${inputCls} mt-1.5`} value={form.reference}
                onChange={(e) => setForm({ ...form, reference: e.target.value })}
                placeholder="Optional" />
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Description *</label>
              <input className={`${inputCls} mt-1.5`} value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="What is this entry about?" />
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-medium text-text-secondary">Lines</label>
              <button
                onClick={() => setForm((f) => ({
                  ...f,
                  lines: [...f.lines, { account_id: "", description: "", debit: "", credit: "" }],
                }))}
                className="text-xs font-medium text-ai-600 hover:text-ai-700"
              >
                + Add line
              </button>
            </div>
            <div className="space-y-2">
              {form.lines.map((line, i) => (
                <div key={i} className="grid grid-cols-2 sm:grid-cols-12 gap-2 items-center">
                  <select className={`${inputCls} col-span-2 sm:col-span-4`} value={line.account_id}
                    onChange={(e) => setLine(i, { account_id: e.target.value })}>
                    <option value="">Account…</option>
                    {accounts.map((a) => (
                      <option key={a.id} value={a.id}>{a.code} - {a.name}</option>
                    ))}
                  </select>
                  <input className={`${inputCls} col-span-2 sm:col-span-3`} placeholder="Line note"
                    value={line.description}
                    onChange={(e) => setLine(i, { description: e.target.value })} />
                  <input className={`${inputCls} col-span-1 sm:col-span-2 text-right`} type="number" min="0" step="any"
                    placeholder="Debit" value={line.debit}
                    onChange={(e) => setLine(i, { debit: e.target.value, credit: "" })} />
                  <input className={`${inputCls} col-span-1 sm:col-span-2 text-right`} type="number" min="0" step="any"
                    placeholder="Credit" value={line.credit}
                    onChange={(e) => setLine(i, { credit: e.target.value, debit: "" })} />
                  <button
                    onClick={() => setForm((f) => ({
                      ...f,
                      lines: f.lines.filter((_, idx) => idx !== i),
                    }))}
                    disabled={form.lines.length <= 2}
                    className="col-span-2 sm:col-span-1 p-2 rounded-lg text-text-muted hover:text-error-600 hover:bg-error-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                    aria-label="Remove line"
                  >
                    <Trash2 className="w-4 h-4 mx-auto" />
                  </button>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between rounded-xl bg-bg-muted px-4 py-3 text-sm">
            <span className={cn("text-text-secondary", !totals.balanced && "text-error-600 font-medium")}>
              {totals.balanced
                ? "Balanced"
                : `Out of balance by ${formatCurrency(Math.abs(totals.debit - totals.credit), org?.base_currency_code)}`}
            </span>
            <div className="flex items-center gap-4">
              <span className="text-text-secondary">
                Dr <span className="font-semibold text-text-primary tabular-nums">
                  {formatCurrency(totals.debit, org?.base_currency_code)}
                </span>
              </span>
              <span className="text-text-secondary">
                Cr <span className="font-semibold text-text-primary tabular-nums">
                  {formatCurrency(totals.credit, org?.base_currency_code)}
                </span>
              </span>
            </div>
          </div>

          <p className="text-[11px] text-text-muted">
            Entries are saved as DRAFT. Posting checks balance and the open
            accounting period in the database - nothing unbalanced can ever post.
          </p>
          {formError && <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">{formError}</p>}

          <div className="flex flex-wrap justify-end gap-2">
            <button onClick={() => setModalOpen(false)}
              className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors">
              Cancel
            </button>
            <button onClick={handleSave} disabled={saving}
              className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {saving ? "Saving…" : "Save Draft Entry"}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
