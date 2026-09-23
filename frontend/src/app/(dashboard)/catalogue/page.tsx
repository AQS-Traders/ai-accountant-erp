"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Plus, Search, Pencil, Trash2, Power, RefreshCw, Package, Wrench } from "lucide-react";
import PageHeader from "@/components/shared/PageHeader";
import Modal from "@/components/shared/Modal";
import StatusBadge from "@/components/shared/StatusBadge";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/shared/States";
import { useOrg } from "@/lib/hooks/useOrg";
import { formatCurrency } from "@/lib/utils/currency";
import { apiErrorMessage } from "@/lib/api/errors";
import {
  catalogueCreate,
  catalogueDelete,
  catalogueList,
  catalogueSetActive,
  catalogueUpdate,
  type CatalogueItem,
  type CatalogueKind,
} from "@/lib/api/client";

const inputCls =
  "w-full px-3 py-2 rounded-xl bg-bg-primary border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors";

/** The service_unit_code enum (authoritative DB enum). */
const BILLING_UNITS = ["HOUR", "DAY", "MONTH", "FIXED", "ITEM"] as const;

const STATUS_FILTERS = ["ALL", "ACTIVE", "INACTIVE"] as const;

/** Refresh cadence: the page is a catalogue, not a live feed — 20s is enough
 *  to see another user's edit without hammering the API. A window focus and
 *  every mutation also refresh immediately. */
const REFRESH_MS = 20_000;

interface ItemForm {
  name: string;
  description: string;
  // product
  unit: string;
  is_stock_tracked: boolean;
  unit_price: string;
  cost_price: string;
  // service
  billing_unit: string;
  standard_rate: string;
  cost_rate: string;
}

const EMPTY_FORM: ItemForm = {
  name: "", description: "",
  unit: "", is_stock_tracked: false, unit_price: "", cost_price: "",
  billing_unit: "HOUR", standard_rate: "", cost_rate: "",
};

function codeOf(item: CatalogueItem): string {
  return item.product_code || item.service_code || "—";
}

/** A short, human status for the row: active items are live in pickers, an
 *  inactive item is archived but every past document still resolves it. */
function statusOf(item: CatalogueItem): string {
  return item.is_active ? "ACTIVE" : "INACTIVE";
}


export default function CataloguePage() {
    const { org } = useOrg();
  const [kind, setKind] = useState<CatalogueKind>("products");
  const noun = kind === "products" ? "Product" : "Service";
  const [items, setItems] = useState<CatalogueItem[] | null>(null);
  const [counts, setCounts] = useState({ total: 0, active: 0, inactive: 0 });
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("ALL");
  const [loading, setLoading] = useState(false);
  const [lastSync, setLastSync] = useState<Date | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<CatalogueItem | null>(null);
  const [form, setForm] = useState<ItemForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [confirmDelete, setConfirmDelete] = useState<CatalogueItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [rowBusy, setRowBusy] = useState<string | null>(null);

  // The polling interval must not restart on every keystroke, so `load` reads
  // the latest term from a ref.  The ref is synced in an EFFECT: writing it
  // during render is forbidden (react-hooks/refs) and can drop updates.
  const searchRef = useRef(search);
  useEffect(() => {
    searchRef.current = search;
  }, [search]);

  const load = useCallback(
    async (opts: { silent?: boolean } = {}) => {
      if (!org) return;
      if (!opts.silent) setLoading(true);
      try {
        const data = await catalogueList(kind, {
          query: searchRef.current.trim(),
          status: statusFilter,
        });
        setItems(data.items ?? []);
        setCounts(data.counts ?? { total: 0, active: 0, inactive: 0 });
        setLastSync(new Date());
        setError(null);
      } catch (err) {
        setError(apiErrorMessage(err));
      } finally {
        if (!opts.silent) setLoading(false);
      }
    },
    [org, kind, statusFilter]
  );

  useEffect(() => {
    const timer = setTimeout(() => load(), search ? 250 : 0);
    return () => clearTimeout(timer);
  }, [load, search]);

  // Real-time-ish tracking: refresh on an interval and whenever the window
  // regains focus, so an item added elsewhere appears without a manual reload.
  useEffect(() => {
    const timer = setInterval(() => load({ silent: true }), REFRESH_MS);
    const onFocus = () => load({ silent: true });
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setModalOpen(true);
  };

  const openEdit = (item: CatalogueItem) => {
    setEditing(item);
    setForm({
      name: item.name ?? "",
      description: item.description ?? "",
      unit: item.unit ?? "",
      is_stock_tracked: Boolean(item.is_stock_tracked),
      unit_price: item.unit_price != null ? String(item.unit_price) : "",
      cost_price: item.cost_price != null ? String(item.cost_price) : "",
      billing_unit: item.billing_unit ?? "HOUR",
      standard_rate: item.standard_rate != null ? String(item.standard_rate) : "",
      cost_rate: item.cost_rate != null ? String(item.cost_rate) : "",
    });
    setFormError(null);
    setModalOpen(true);
  };

  const handleSave = async () => {
    if (!org || form.name.trim().length < 2) return;
    setSaving(true);
    setFormError(null);
    const payload: Record<string, unknown> =
      kind === "products"
        ? {
            name: form.name.trim(),
            description: form.description.trim() || null,
            unit: form.unit.trim() || null,
            is_stock_tracked: form.is_stock_tracked,
            unit_price: form.unit_price === "" ? 0 : Number(form.unit_price),
            cost_price: form.cost_price === "" ? null : Number(form.cost_price),
          }
        : {
            name: form.name.trim(),
            description: form.description.trim() || null,
            billing_unit: form.billing_unit,
            standard_rate:
              form.standard_rate === "" ? 0 : Number(form.standard_rate),
            cost_rate: form.cost_rate === "" ? null : Number(form.cost_rate),
          };

    try {
      if (editing) {
        await catalogueUpdate(kind, editing.id, payload);
      } else {
        await catalogueCreate(kind, payload);
      }
      setModalOpen(false);
      setEditing(null);
      setForm(EMPTY_FORM);
      await load();
    } catch (err) {
      setFormError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const handleToggleActive = async (item: CatalogueItem) => {
    setRowBusy(item.id);
    try {
      await catalogueSetActive(kind, item.id, !item.is_active);
      await load();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setRowBusy(null);
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) return;
    setRowBusy(confirmDelete.id);
    setDeleteError(null);
    try {
      await catalogueDelete(kind, confirmDelete.id);
      setConfirmDelete(null);
      await load();
    } catch (err) {
      // 409: the API refuses a used item and says to deactivate instead.
      setDeleteError(apiErrorMessage(err));
    } finally {
      setRowBusy(null);
    }
  };

  const filtered = useMemo(() => {
    const rows = items ?? [];
    const term = search.trim().toLowerCase();
    if (!term) return rows;
    // The server already searched; this keeps typing instant between fetches.
    return rows.filter(
      (row) =>
        row.name.toLowerCase().includes(term) ||
        codeOf(row).toLowerCase().includes(term)
    );
  }, [items, search]);


  return (
    <div className="space-y-5">
      <PageHeader
        title="Products & Services Catalogue"
        subtitle="Every item you sell or bill, with live status. Edit anytime; an item that documents already use is archived (deactivated) instead of deleted, so history keeps resolving it."
        actions={
          <>
            <button
              onClick={() => load()}
              className="inline-flex items-center gap-1.5 rounded-xl border border-border-default bg-bg-surface px-3 py-2 text-xs font-medium text-text-secondary hover:text-text-primary hover:border-ai-300 transition-colors"
              title="Refresh now"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Refresh
            </button>
            <button
              onClick={openCreate}
              className="inline-flex items-center gap-1.5 rounded-xl bg-ai-500 hover:bg-ai-600 px-4 py-2 text-sm font-medium text-white btn-3d btn-shine transition-colors"
            >
              <Plus className="w-4 h-4" />
              Add {noun}
            </button>
          </>
        }
      />

      {/* ---- tabs: two catalogues, one screen ---------------------------- */}
      <div className="flex flex-wrap items-center gap-2">
        {(["products", "services"] as CatalogueKind[]).map((k) => {
          const active = k === kind;
          const Icon = k === "products" ? Package : Wrench;
          return (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={`inline-flex items-center gap-2 rounded-xl px-3.5 py-2 text-sm font-medium transition-colors ${
                active
                  ? "bg-ai-500 text-white shadow-sm"
                  : "bg-bg-surface border border-border-default text-text-secondary hover:text-text-primary"
              }`}
            >
              <Icon className="w-4 h-4" />
              {k === "products" ? "Products" : "Services"}
            </button>
          );
        })}

        <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-text-muted">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              error ? "bg-error-500" : "bg-success-500"
            }`}
          />
          {error
            ? "not syncing"
            : lastSync
              ? `live · updated ${lastSync.toLocaleTimeString()}`
              : "connecting…"}
        </span>
      </div>

      {/* ---- status strip ------------------------------------------------ */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: "Total", value: counts.total },
          { label: "Active", value: counts.active },
          { label: "Inactive", value: counts.inactive },
        ].map((card) => (
          <div
            key={card.label}
            className="bg-bg-surface rounded-2xl border border-border-subtle px-4 py-3"
          >
            <p className="text-[11px] uppercase tracking-wide text-text-muted">
              {card.label}
            </p>
            <p className="text-lg font-semibold text-text-primary">
              {card.value}
            </p>
          </div>
        ))}
      </div>

      {/* ---- toolbar ----------------------------------------------------- */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search className="w-4 h-4 text-text-muted absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            className={`${inputCls} pl-9`}
            placeholder={`Search ${kind} by name or code…`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="flex gap-1.5">
          {STATUS_FILTERS.map((value) => (
            <button
              key={value}
              onClick={() => setStatusFilter(value)}
              className={`rounded-xl px-3 py-2 text-xs font-medium transition-colors ${
                statusFilter === value
                  ? "bg-brand-navy text-white"
                  : "bg-bg-surface border border-border-default text-text-secondary hover:text-text-primary"
              }`}
            >
              {value === "ALL" ? "All" : value === "ACTIVE" ? "Active" : "Inactive"}
            </button>
          ))}
        </div>
      </div>

      {/* ---- table ------------------------------------------------------- */}
      {error && <ErrorState message={error} />}

      {!error && loading && !items && <TableSkeleton rows={6} cols={5} />}

      {!error && items && filtered.length === 0 && (
        <EmptyState
          title={search ? `No ${kind} match “${search}”` : `No ${kind} yet`}
          hint={
            search
              ? "Try a different name or code."
              : `Add your first ${noun} — the AI agent then uses it on invoices and bills instead of inventing a line item.`
          }
        />
      )}

      {!error && items && filtered.length > 0 && (
        <div className="bg-bg-surface rounded-2xl border border-border-subtle overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-text-muted border-b border-border-subtle">
                  <th className="px-4 py-3 font-medium">Code</th>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">
                    {kind === "products" ? "Unit price" : "Standard rate"}
                  </th>
                  <th className="px-4 py-3 font-medium">
                    {kind === "products" ? "Unit" : "Billed per"}
                  </th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr
                    key={item.id}
                    className="border-b border-border-subtle last:border-0 hover:bg-bg-muted/40 transition-colors"
                  >
                    <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                      {codeOf(item)}
                    </td>
                    <td className="px-4 py-3">
                      <p className="font-medium text-text-primary">{item.name}</p>
                      {item.description && (
                        <p className="text-xs text-text-muted line-clamp-1">
                          {item.description}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3 text-text-primary">
                      {formatCurrency(
                        Number(
                          kind === "products"
                            ? item.unit_price ?? 0
                            : item.standard_rate ?? 0
                        ),
                        org?.base_currency_code
                      )}
                    </td>
                    <td className="px-4 py-3 text-text-secondary">
                      {kind === "products"
                        ? item.unit || "—"
                        : item.billing_unit || "—"}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={statusOf(item)} />
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => openEdit(item)}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-border-default bg-bg-surface px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text-primary hover:border-ai-300 transition-colors"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                          Edit
                        </button>
                        <button
                          onClick={() => handleToggleActive(item)}
                          disabled={rowBusy === item.id}
                          title={
                            item.is_active
                              ? "Deactivate — keeps history, hides it from pickers"
                              : "Reactivate"
                          }
                          className="inline-flex items-center gap-1.5 rounded-lg border border-border-default bg-bg-surface px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text-primary hover:border-warning-300 transition-colors disabled:opacity-40"
                        >
                          <Power className="w-3.5 h-3.5" />
                          {item.is_active ? "Deactivate" : "Activate"}
                        </button>
                        <button
                          onClick={() => {
                            setConfirmDelete(item);
                            setDeleteError(null);
                          }}
                          title="Delete — only possible while no document uses it"
                          className="inline-flex items-center gap-1.5 rounded-lg border border-border-default bg-bg-surface px-2.5 py-1.5 text-xs font-medium text-error-600 hover:border-error-300 transition-colors"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}


      {/* ---- add / edit -------------------------------------------------- */}
      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={`${editing ? "Edit" : "Add"} ${noun}`}
      >
        <div className="space-y-4">
          <div>
            <label className="text-xs font-medium text-text-secondary">
              Name *
            </label>
            <input
              className={`${inputCls} mt-1.5`}
              value={form.name}
              autoFocus
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={
                kind === "products" ? "e.g. Study Table" : "e.g. Monthly Retainer"
              }
            />
          </div>

          {kind === "products" ? (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Unit price *
                </label>
                <input
                  className={`${inputCls} mt-1.5`}
                  type="number"
                  min={0}
                  value={form.unit_price}
                  onChange={(e) =>
                    setForm({ ...form, unit_price: e.target.value })
                  }
                />
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Cost price
                </label>
                <input
                  className={`${inputCls} mt-1.5`}
                  type="number"
                  min={0}
                  value={form.cost_price}
                  onChange={(e) =>
                    setForm({ ...form, cost_price: e.target.value })
                  }
                />
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Billed per
                </label>
                <select
                  className={`${inputCls} mt-1.5`}
                  value={form.billing_unit}
                  onChange={(e) =>
                    setForm({ ...form, billing_unit: e.target.value })
                  }
                >
                  {BILLING_UNITS.map((unit) => (
                    <option key={unit} value={unit}>
                      {unit}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Standard rate *
                </label>
                <input
                  className={`${inputCls} mt-1.5`}
                  type="number"
                  min={0}
                  value={form.standard_rate}
                  onChange={(e) =>
                    setForm({ ...form, standard_rate: e.target.value })
                  }
                />
              </div>
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Cost rate
                </label>
                <input
                  className={`${inputCls} mt-1.5`}
                  type="number"
                  min={0}
                  value={form.cost_rate}
                  onChange={(e) =>
                    setForm({ ...form, cost_rate: e.target.value })
                  }
                />
              </div>
            </div>
          )}

          {kind === "products" && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-text-secondary">
                  Unit
                </label>
                <input
                  className={`${inputCls} mt-1.5`}
                  value={form.unit}
                  onChange={(e) => setForm({ ...form, unit: e.target.value })}
                  placeholder="pcs, kg, box…"
                />
              </div>
              <label className="flex items-end gap-2 pb-2 text-xs text-text-secondary">
                <input
                  type="checkbox"
                  checked={form.is_stock_tracked}
                  onChange={(e) =>
                    setForm({ ...form, is_stock_tracked: e.target.checked })
                  }
                />
                Stock-tracked item
              </label>
            </div>
          )}

          <div>
            <label className="text-xs font-medium text-text-secondary">
              Description
            </label>
            <textarea
              className={`${inputCls} mt-1.5 min-h-16 resize-y`}
              value={form.description}
              onChange={(e) =>
                setForm({ ...form, description: e.target.value })
              }
            />
          </div>

          {formError && (
            <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2">
              {formError}
            </p>
          )}

                              <div className="flex justify-end gap-2 pt-1">
            <button
              onClick={() => setModalOpen(false)}
              className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving || form.name.trim().length < 2}
              className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {saving ? "Saving…" : editing ? "Save changes" : `Save ${noun}`}
            </button>
          </div>
        </div>
      </Modal>

      {/* ---- delete (refused while used) --------------------------------- */}
      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title={`Delete ${noun}`}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-primary">
            Delete <span className="font-medium">{confirmDelete?.name}</span>?
          </p>
          <p className="text-xs text-text-secondary">
            This is only possible while no document uses the {noun}. If it is in
            use, the app refuses and offers to deactivate it instead — which
            keeps every past invoice, quote and bill resolving correctly.
          </p>

          {deleteError && (
            <div className="text-xs text-warning-700 bg-warning-50 rounded-xl px-3 py-2.5 space-y-2">
              <p>{deleteError}</p>
              {confirmDelete && (
                <button
                  onClick={async () => {
                    await handleToggleActive(confirmDelete);
                    setConfirmDelete(null);
                    setDeleteError(null);
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-warning-300 bg-bg-surface px-2.5 py-1.5 text-xs font-medium text-warning-700 transition-colors"
                >
                  <Power className="w-3.5 h-3.5" />
                  Deactivate it instead
                </button>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button
              onClick={() => setConfirmDelete(null)}
              className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors"
            >
              Cancel
            </button>
            {!deleteError && (
              <button
                onClick={handleDelete}
                disabled={rowBusy !== null}
                className="px-5 py-2 rounded-xl bg-error-600 hover:bg-error-700 text-white text-sm font-medium disabled:opacity-40 transition-colors"
              >
                {rowBusy ? "Deleting…" : "Delete"}
              </button>
            )}
          </div>
        </div>
      </Modal>
    </div>
  );
}
