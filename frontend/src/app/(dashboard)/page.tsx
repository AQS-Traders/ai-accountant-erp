"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AlertCircle, ArrowRight, Clock, TrendingUp, Wallet, BarChart3, UserCheck, CreditCard, Landmark, FileText, FilePlus, UserPlus, ShoppingCart, Scale, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { formatCompact } from "@/lib/utils/currency";
import { createClient } from "@/lib/supabase/client";
import { useOrg } from "@/lib/hooks/useOrg";
import AICommandBox from "@/components/ai/AICommandBox";
import StatusBadge from "@/components/shared/StatusBadge";

/* ---- Data shapes ---- */
interface KPIData {
  label: string;
  value: number;
  hint?: string;
}

interface OpenInvoice {
  invoice_number: string;
  due_date: string | null;
  outstanding: number;
}

interface OpenBill {
  bill_number: string;
  due_date: string | null;
  outstanding: number;
}

interface RecentTxn {
  journal_entry_id: string;
  journal_number: string;
  transaction_date: string;
  description: string | null;
  status: string;
  total_debit: number;
}

interface DashboardData {
  revenue: number;
  expenses: number;
  receivables: number;
  payables: number;
  cash: number;
  overdueInvoices: number;
  dueThisWeek: number;
  openBills: number;
  draftInvoices: number;
  recent: RecentTxn[];
}

const isDueSoon = (due: string | null) => {
  if (!due) return false;
  const d = new Date(due);
  const now = new Date();
  const week = new Date(now);
  week.setDate(week.getDate() + 7);
  return d >= now && d <= week;
};

const isOverdue = (due: string | null) => {
  if (!due) return false;
  return new Date(due) < new Date(new Date().toDateString());
};

/* ---- KPI Card ---- */
function KPICard({ label, value, hint, currency, icon: Icon, iconColor, iconBg }: KPIData & {
  currency?: string;
  icon: LucideIcon;
  iconColor: string;
  iconBg: string;
}) {
  return (
    <div className="clay-sm hover-lift bg-bg-surface p-5 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">{label}</p>
        <div className={cn("w-8 h-8 rounded-lg flex items-center justify-center", iconBg)}>
          <Icon className={cn("w-4 h-4", iconColor)} />
        </div>
      </div>
      <p className="text-2xl font-semibold text-text-primary tabular-nums">
        {formatCompact(value, currency)}
      </p>
      {hint && (
        <div className="flex items-center gap-1">
          <Clock className="w-3.5 h-3.5 text-text-muted" />
          <span className="text-[11px] text-text-muted">{hint}</span>
        </div>
      )}
    </div>
  );
}

/* ---- Quick Action ---- */
function QuickAction({ label, href, icon: Icon, iconColor }: {
  label: string;
  href: string;
  icon: LucideIcon;
  iconColor: string;
}) {
  return (
    <Link
      href={href}
      className="group btn-3d-soft hover-lift flex items-center gap-2 px-4 py-2.5 rounded-xl bg-bg-surface border border-border-subtle text-sm font-medium text-text-secondary hover:text-text-primary hover:border-ai-200 transition-all"
    >
      <Icon className={cn("icon-pop w-4 h-4", iconColor)} />
      {label}
    </Link>
  );
}

/* ---- Dashboard Page ---- */
export default function DashboardPage() {
  const { org } = useOrg();
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!org) return;
    const supabase = createClient();
    const orgId = org.organization_id;

    const [income, balance, receivables, payables, recent, drafts] = await Promise.all([
      // Reporting-year scoped (financial_years.is_current) - switch the
      // reporting year in Settings and every KPI follows.
      supabase.rpc("get_income_statement", { target_org: orgId }),
      supabase.from("v_balance_sheet").select("account_code, account_name, net_amount").eq("organization_id", orgId),
      supabase.from("v_open_receivables").select("invoice_number, due_date, outstanding").eq("organization_id", orgId),
      supabase.from("v_open_payables").select("bill_number, due_date, outstanding").eq("organization_id", orgId),
      supabase.from("v_recent_transactions").select("*").eq("organization_id", orgId).limit(6),
      supabase.from("invoices").select("id", { count: "exact", head: true })
        .eq("organization_id", orgId).eq("status", "DRAFT"),
    ]);

    const incomeRows = (income.data ?? []) as { account_type: string; net_amount: number }[];
    const revenue = -incomeRows.filter((r) => r.account_type === "REVENUE").reduce((s, r) => s + r.net_amount, 0);
    const expenses = incomeRows.filter((r) => r.account_type === "EXPENSE").reduce((s, r) => s + r.net_amount, 0);

    const balanceRows = (balance.data ?? []) as { account_code: string; account_name: string; net_amount: number }[];
    const cash = balanceRows
      .filter((r) => /cash|bank/i.test(r.account_name) || /^10(0[1-9]|[1-9]\d)$/.test(r.account_code))
      .reduce((s, r) => s + r.net_amount, 0);

    const recRows = (receivables.data ?? []) as OpenInvoice[];
    const payRows = (payables.data ?? []) as OpenBill[];

    setData({
      revenue,
      expenses,
      receivables: recRows.reduce((s, r) => s + r.outstanding, 0),
      payables: payRows.reduce((s, r) => s + r.outstanding, 0),
      cash,
      overdueInvoices: recRows.filter((r) => isOverdue(r.due_date)).length,
      dueThisWeek: recRows.filter((r) => isDueSoon(r.due_date)).length,
      openBills: payRows.length,
      draftInvoices: drafts.count ?? 0,
      recent: ((recent.data ?? []) as RecentTxn[]),
    });
    setError(
      income.error?.message ??
      balance.error?.message ??
      receivables.error?.message ??
      payables.error?.message ??
      recent.error?.message ??
      drafts.error?.message ?? null
    );
  }, [org]);

  useEffect(() => { load(); }, [load]);

  // The AI command box fires "erp:data-changed" after the agent records a
  // transaction - reload the KPIs + recent transactions IMMEDIATELY so the
  // user never has to refresh the whole site to see the new numbers.
  useEffect(() => {
    const handler = () => load();
    window.addEventListener("erp:data-changed", handler);
    return () => window.removeEventListener("erp:data-changed", handler);
  }, [load]);

  const currency = org?.base_currency_code;

  /* Hydration safety: the time-of-day greeting is derived from the CLIENT
     clock/timezone. Computing it during render made the server HTML
     ("Good afternoon" — server runs UTC) differ from the first client render
     ("Good evening" — visitor timezone), which React reports as error #418
     (hydration failed: text content did not match). The greeting is therefore
     localised AFTER mount, exactly like the rotating placeholder above. */
  const [greeting, setGreeting] = useState("Welcome back");
  useEffect(() => {
    const hour = new Date().getHours();
    setGreeting(hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening");
  }, []);

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      {/* Greeting */}
      <div>
        <h1 className="text-xl lg:text-2xl font-semibold text-text-primary">
          {greeting}{org ? `, ${org.name}` : ""}
        </h1>
        <p className="text-sm text-text-secondary mt-0.5">
          Here&apos;s what&apos;s happening in your business today.
        </p>
      </div>

      {/* AI Command Center */}
      <AICommandBox />

      {/* Quick Actions */}
      <div className="flex flex-wrap gap-2">
        <QuickAction label="New Invoice" href="/sales/invoices" icon={FileText} iconColor="text-success-500" />
        <QuickAction label="New Quotation" href="/sales/quotations" icon={FilePlus} iconColor="text-purple-500" />
        <QuickAction label="Add Customer" href="/customers" icon={UserPlus} iconColor="text-info-500" />
        <QuickAction label="Record Purchase" href="/purchases/bills" icon={ShoppingCart} iconColor="text-warning-500" />
        <QuickAction label="Trial Balance" href="/accounting/trial-balance" icon={Scale} iconColor="text-brand-teal" />
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-xl bg-error-50 border border-error-200 px-4 py-3 text-sm text-error-700">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span className="break-words">{error}</span>
        </div>
      )}

      {/* KPI Grid */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <KPICard label="Revenue" value={data?.revenue ?? 0} hint="Posted, year to date" currency={currency} icon={TrendingUp} iconColor="text-success-500" iconBg="bg-success-50" />
        <KPICard label="Expenses" value={data?.expenses ?? 0} hint="Posted, year to date" currency={currency} icon={Wallet} iconColor="text-purple-500" iconBg="bg-purple-50" />
        <KPICard label="Net Profit" value={(data?.revenue ?? 0) - (data?.expenses ?? 0)} hint="Revenue \u2212 expenses" currency={currency} icon={BarChart3} iconColor="text-info-500" iconBg="bg-info-50" />
        <KPICard label="Receivables" value={data?.receivables ?? 0} hint={`${data?.overdueInvoices ?? 0} overdue`} currency={currency} icon={UserCheck} iconColor="text-warning-500" iconBg="bg-warning-50" />
        <KPICard label="Payables" value={data?.payables ?? 0} hint={`${data?.openBills ?? 0} open bills`} currency={currency} icon={CreditCard} iconColor="text-pink-500" iconBg="bg-pink-50" />
        <KPICard label="Cash & Bank" value={data?.cash ?? 0} hint="Current balances" currency={currency} icon={Landmark} iconColor="text-brand-teal" iconBg="bg-brand-aqua" />
      </div>

      {/* Recent Activity + Upcoming */}
      <div className="grid lg:grid-cols-2 gap-6">
        {/* Recent Transactions */}
        <div className="hover-lift bg-bg-surface rounded-2xl border border-border-subtle shadow-sm p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-text-primary">Recent Transactions</h3>
            <Link href="/accounting/journal" className="text-xs font-medium text-ai-600 hover:text-ai-700 flex items-center gap-1">
              Journal <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          {(data?.recent ?? []).length === 0 ? (
            <p className="text-sm text-text-secondary py-4">
              Nothing posted yet. Tell the AI about a transaction, or record a purchase bill.
            </p>
          ) : (
            <div className="space-y-3">
              {(data?.recent ?? []).map((txn) => (
                <div key={txn.journal_entry_id} className="flex items-center justify-between text-sm gap-3">
                  <div className="min-w-0">
                    <p className="text-text-secondary truncate">{txn.description ?? txn.journal_number}</p>
                    <p className="text-[11px] text-text-muted tabular-nums">
                      {txn.journal_number} · {txn.transaction_date}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <StatusBadge status={txn.status} />
                    <span className="font-medium tabular-nums text-text-primary">
                      {formatCompact(txn.total_debit, currency)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Outstanding Items */}
        <div className="hover-lift bg-bg-surface rounded-2xl border border-border-subtle shadow-sm p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-text-primary">Outstanding</h3>
            <Link href="/reports/aging" className="text-xs font-medium text-ai-600 hover:text-ai-700 flex items-center gap-1">
              Aging report <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-3">
            {[
              { label: "Overdue invoices", count: data?.overdueInvoices ?? 0, severity: "error" },
              { label: "Due this week", count: data?.dueThisWeek ?? 0, severity: "warning" },
              { label: "Open supplier bills", count: data?.openBills ?? 0, severity: "warning" },
              { label: "Draft invoices to issue", count: data?.draftInvoices ?? 0, severity: "info" },
            ].map((item) => (
              <div key={item.label} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <AlertCircle
                    className={cn(
                      "w-4 h-4",
                      item.severity === "error" && "text-error-500",
                      item.severity === "warning" && "text-warning-500",
                      item.severity === "info" && "text-info-500"
                    )}
                  />
                  <span className="text-text-secondary">{item.label}</span>
                </div>
                <span className="font-medium text-text-primary">{item.count}</span>
              </div>
            ))}
          </div>
          <p className="text-[11px] text-text-muted mt-4">
            Figures update as the AI posts entries. Full detail in the Aging report.
          </p>
        </div>
      </div>
    </div>
  );
}
