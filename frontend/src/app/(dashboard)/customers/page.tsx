"use client";

import { useCallback, useEffect, useState } from "react";
import { Plus, Search, Pencil } from "lucide-react";
import { createClient } from "@/lib/supabase/client";
import { useOrg } from "@/lib/hooks/useOrg";
import { formatCurrency } from "@/lib/utils/currency";
import Modal from "@/components/shared/Modal";
import PageHeader from "@/components/shared/PageHeader";
import StatusBadge from "@/components/shared/StatusBadge";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/shared/States";
import type { Customer } from "@/lib/types/entities";

const inputCls =
  "w-full px-3 py-2 rounded-xl bg-bg-primary border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors";

interface CustomerForm {
  name: string;
  email: string;
  phone: string;
  tax_number: string;
  payment_terms_days: string;
  credit_limit: string;
  notes: string;
}

const EMPTY_FORM: CustomerForm = {
  name: "", email: "", phone: "", tax_number: "",
  payment_terms_days: "30", credit_limit: "", notes: "",
};

export default function CustomersPage() {
  const { org, loading: orgLoading } = useOrg();
  const [rows, setRows] = useState<Customer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<CustomerForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!org) return;
    const supabase = createClient();
    const { data, error: dbError } = await supabase
      .from("customers")
      .select("*")
      .eq("organization_id", org.organization_id)
      .order("created_at", { ascending: false });
    if (dbError) setError(dbError.message);
    else setRows(data ?? []);
  }, [org]);

  useEffect(() => { load(); }, [load]);

  const filtered = (rows ?? []).filter((c) =>
    !search ||
    c.name.toLowerCase().includes(search.toLowerCase()) ||
    (c.customer_code ?? "").toLowerCase().includes(search.toLowerCase()) ||
    (c.email ?? "").toLowerCase().includes(search.toLowerCase())
  );

  const handleSave = async () => {
    if (!org || form.name.trim().length < 2) return;
    setSaving(true);
    setFormError(null);
    const supabase = createClient();
    const { error: insertError } = await supabase.from("customers").insert({
      organization_id: org.organization_id,
      name: form.name.trim(),
      email: form.email.trim() || null,
      phone: form.phone.trim() || null,
      tax_number: form.tax_number.trim() || null,
      payment_terms_days: form.payment_terms_days ? Number(form.payment_terms_days) : null,
      credit_limit: form.credit_limit ? Number(form.credit_limit) : null,
      currency_code: org.base_currency_code,
      notes: form.notes.trim() || null,
    });
    setSaving(false);
    if (insertError) { setFormError(insertError.message); return; }
    setModalOpen(false);
    setForm(EMPTY_FORM);
    load();
  };

  /* ---- Admin edit flow: review changes → confirm → apply ------------- */
  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<Customer | null>(null);
  const [editForm, setEditForm] = useState<CustomerForm>(EMPTY_FORM);
  const [editConfirm, setEditConfirm] = useState(false);
  const [editChanges, setEditChanges] = useState<
    { field: string; label: string; from: string; to: string }[]
  >([]);
  const [editUpdates, setEditUpdates] = useState<Record<string, unknown>>({});
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const FIELD_LABELS: Record<string, string> = {
    name: "Name", email: "Email", phone: "Phone", tax_number: "Tax number",
    payment_terms_days: "Terms (days)", credit_limit: "Credit limit", notes: "Notes",
  };
  const fmtVal = (v: string) => (v === "" ? "(empty)" : v);

  const openEdit = (c: Customer) => {
    setEditing(c);
    setEditForm({
      name: c.name ?? "", email: c.email ?? "", phone: c.phone ?? "",
      tax_number: c.tax_number ?? "",
      payment_terms_days: c.payment_terms_days != null ? String(c.payment_terms_days) : "",
      credit_limit: c.credit_limit != null ? String(c.credit_limit) : "",
      notes: c.notes ?? "",
    });
    setEditConfirm(false); setEditChanges([]); setEditError(null);
    setEditOpen(true);
  };

  const handleEditSave = () => {
    if (!editing) return;
    if (editForm.name.trim().length < 2) { setEditError("Name must be at least 2 characters."); return; }
    const next: Record<string, string> = {
      name: editForm.name.trim(),
      email: editForm.email.trim(),
      phone: editForm.phone.trim(),
      tax_number: editForm.tax_number.trim(),
      payment_terms_days: editForm.payment_terms_days.trim(),
      credit_limit: editForm.credit_limit.trim(),
      notes: editForm.notes.trim(),
    };
    const prev: Record<string, string> = {
      name: editing.name ?? "",
      email: editing.email ?? "",
      phone: editing.phone ?? "",
      tax_number: editing.tax_number ?? "",
      payment_terms_days: editing.payment_terms_days != null ? String(editing.payment_terms_days) : "",
      credit_limit: editing.credit_limit != null ? String(editing.credit_limit) : "",
      notes: editing.notes ?? "",
    };
    const changes: { field: string; label: string; from: string; to: string }[] = [];
    const updates: Record<string, unknown> = {};
    for (const k of Object.keys(next)) {
      if (next[k] !== prev[k]) {
        changes.push({ field: k, label: FIELD_LABELS[k] ?? k, from: prev[k], to: next[k] });
        updates[k] = k === "payment_terms_days" || k === "credit_limit"
          ? (next[k] === "" ? null : Number(next[k]))
          : (next[k] === "" ? null : next[k]);
      }
    }
    if (changes.length === 0) { setEditError("No changes to save."); return; }
    setEditChanges(changes);
    setEditUpdates(updates);
    setEditError(null);
    setEditConfirm(true);
  };

  const handleConfirmEdit = async () => {
    if (!editing) return;
    setEditSaving(true);
    setEditError(null);
    const supabase = createClient();
    const { error: rpcError } = await supabase.rpc("update_party_info", {
      p_kind: "customer",
      p_record_id: editing.id,
      p_updates: editUpdates,
    });
    setEditSaving(false);
    if (rpcError) { setEditError(rpcError.message); return; }
    setEditOpen(false);
    setEditing(null);
    setEditConfirm(false);
    load();
  };

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <PageHeader
        title="Customers"
        subtitle="People and companies you invoice"
        actions={
          <button
            onClick={() => { setForm(EMPTY_FORM); setFormError(null); setModalOpen(true); }}
            className="flex items-center gap-1.5 px-4 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium transition-colors"
          >
            <Plus className="w-4 h-4" /> Add Customer
          </button>
        }
      />

      <div className="relative max-w-sm">
        <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
        <input
          className={`${inputCls} pl-9`}
          placeholder="Search customers…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {orgLoading || (!rows && !error) ? (
        <TableSkeleton cols={5} />
      ) : error ? (
        <ErrorState message={error} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={search ? "No customers match your search" : "No customers yet"}
          hint={search ? undefined : "Add your first customer, or just tell the AI agent: \"Add customer ABC Technologies\"."}
        />
      ) : (
        <div className="bg-bg-surface rounded-2xl border border-border-subtle overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-text-muted border-b border-border-subtle">
                <th className="px-4 py-3 font-medium">Code</th>
                <th className="px-4 py-3 font-medium">Name</th>
                <th className="px-4 py-3 font-medium">Email</th>
                <th className="px-4 py-3 font-medium">Phone</th>
                <th className="px-4 py-3 font-medium text-right">Credit Limit</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((c) => (
                <tr key={c.id} className="border-b border-border-subtle/60 last:border-0 hover:bg-bg-muted/50 transition-colors">
                  <td className="px-4 py-3 text-text-secondary tabular-nums">{c.customer_code ?? "-"}</td>
                  <td className="px-4 py-3 font-medium text-text-primary">{c.name}</td>
                  <td className="px-4 py-3 text-text-secondary">{c.email ?? "-"}</td>
                  <td className="px-4 py-3 text-text-secondary">{c.phone ?? "-"}</td>
                  <td className="px-4 py-3 text-right text-text-secondary tabular-nums">
                    {c.credit_limit != null ? formatCurrency(c.credit_limit, c.currency_code ?? undefined) : "-"}
                  </td>
                  <td className="px-4 py-3"><StatusBadge status={c.is_active ? "ACTIVE" : "VOIDED"} /></td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => openEdit(c)}
                      title="Edit details (owner/admin)"
                      className="inline-flex items-center gap-1.5 rounded-lg border border-border-default bg-bg-surface px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text-primary hover:border-ai-300 transition-colors"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title="Add Customer">
        <div className="space-y-4">
          <div>
            <label className="text-xs font-medium text-text-secondary">Name *</label>
            <input className={`${inputCls} mt-1.5`} value={form.name} autoFocus
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="e.g. ABC Technologies" />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-medium text-text-secondary">Email</label>
              <input className={`${inputCls} mt-1.5`} type="email" value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Phone</label>
              <input className={`${inputCls} mt-1.5`} value={form.phone}
                onChange={(e) => setForm({ ...form, phone: e.target.value })} />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="text-xs font-medium text-text-secondary">Tax number</label>
              <input className={`${inputCls} mt-1.5`} value={form.tax_number}
                onChange={(e) => setForm({ ...form, tax_number: e.target.value })} />
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Terms (days)</label>
              <input className={`${inputCls} mt-1.5`} type="number" min={0} value={form.payment_terms_days}
                onChange={(e) => setForm({ ...form, payment_terms_days: e.target.value })} />
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Credit limit</label>
              <input className={`${inputCls} mt-1.5`} type="number" min={0} value={form.credit_limit}
                onChange={(e) => setForm({ ...form, credit_limit: e.target.value })} />
            </div>
          </div>
          <div>
            <label className="text-xs font-medium text-text-secondary">Notes</label>
            <textarea className={`${inputCls} mt-1.5 min-h-16 resize-y`} value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>
          {formError && <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">{formError}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={() => setModalOpen(false)}
              className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors">
              Cancel
            </button>
            <button onClick={handleSave} disabled={saving || form.name.trim().length < 2}
              className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {saving ? "Saving…" : "Save Customer"}
            </button>
          </div>
        </div>
      </Modal>

      {/* Edit modal — two-step: edit fields → review & confirm */}
      <Modal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        title={editConfirm ? "Confirm changes" : `Edit customer — ${editing?.name ?? ""}`}
      >
        {!editConfirm ? (
          <div className="space-y-4">
            <div>
              <label className="text-xs font-medium text-text-secondary">Name *</label>
              <input className={`${inputCls} mt-1.5`} value={editForm.name}
                onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-text-secondary">Email</label>
                <input className={`${inputCls} mt-1.5`} type="email" value={editForm.email}
                  onChange={(e) => setEditForm({ ...editForm, email: e.target.value })} />
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">Phone</label>
                <input className={`${inputCls} mt-1.5`} value={editForm.phone}
                  onChange={(e) => setEditForm({ ...editForm, phone: e.target.value })} />
              </div>
            </div>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="text-xs font-medium text-text-secondary">Tax number</label>
                <input className={`${inputCls} mt-1.5`} value={editForm.tax_number}
                  onChange={(e) => setEditForm({ ...editForm, tax_number: e.target.value })} />
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">Terms (days)</label>
                <input className={`${inputCls} mt-1.5`} type="number" min={0} value={editForm.payment_terms_days}
                  onChange={(e) => setEditForm({ ...editForm, payment_terms_days: e.target.value })} />
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">Credit limit</label>
                <input className={`${inputCls} mt-1.5`} type="number" min={0} value={editForm.credit_limit}
                  onChange={(e) => setEditForm({ ...editForm, credit_limit: e.target.value })} />
              </div>
            </div>
            <div>
              <label className="text-xs font-medium text-text-secondary">Notes</label>
              <textarea className={`${inputCls} mt-1.5 min-h-16 resize-y`} value={editForm.notes}
                onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })} />
            </div>
            {editError && <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">{editError}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={() => setEditOpen(false)}
                className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors">
                Cancel
              </button>
              <button onClick={handleEditSave}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium transition-colors">
                Review Changes
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <p className="text-sm text-text-secondary">
              You are about to update <span className="font-medium text-text-primary">{editing?.name}</span>.
              These changes apply immediately and are recorded in the audit log.
            </p>
            <div className="rounded-xl border border-border-subtle divide-y divide-border-subtle">
              {editChanges.map((c) => (
                <div key={c.field} className="px-3 py-2 text-sm">
                  <span className="font-medium text-text-primary">{c.label}:</span>{" "}
                  <span className="text-text-muted line-through">{fmtVal(c.from)}</span>{" "}
                  <span className="text-text-muted">→</span>{" "}
                  <span className="text-emerald-600 font-medium">{fmtVal(c.to)}</span>
                </div>
              ))}
            </div>
            {editError && <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">{editError}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={() => setEditConfirm(false)}
                className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors">
                Back
              </button>
              <button onClick={handleConfirmEdit} disabled={editSaving}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-emerald-500 hover:bg-emerald-600 text-white text-sm font-medium disabled:opacity-40 transition-colors">
                {editSaving ? "Saving…" : "Confirm & Save"}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
