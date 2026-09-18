"use client";

import { AlertTriangle, BookOpen } from "lucide-react";
import type { AccountingEntry } from "@/lib/types/api";
import type { RiskLevel } from "@/lib/types/enums";
import { formatCurrency } from "@/lib/utils/currency";
import { cn } from "@/lib/utils/cn";

interface Props {
  summary: string;
  riskLevel?: RiskLevel;
  accountingImpact?: AccountingEntry[];
  /** Work Stream A: the resolved transaction date shown for review. */
  transactionDate?: string;
  dateDefaulted?: boolean;
  onDecision: (approved: boolean, notes?: string) => void;
  /** While the confirm request is in flight both buttons are disabled —
   *  a double click must never fire a duplicate confirmation. */
  disabled?: boolean;
}

const riskConfig: Record<string, { bg: string; text: string; label: string }> = {
  LOW: { bg: "bg-success-50", text: "text-success-700", label: "Low Risk" },
  MEDIUM: { bg: "bg-warning-50", text: "text-warning-600", label: "Medium Risk" },
  HIGH: { bg: "bg-error-50", text: "text-error-600", label: "High Risk" },
  CRITICAL: { bg: "bg-error-50", text: "text-error-600", label: "Critical Risk" },
};

export default function AIConfirmation({
  summary,
  riskLevel,
  accountingImpact,
  transactionDate,
  dateDefaulted,
  onDecision,
  disabled = false,
}: Props) {
  const risk = riskConfig[riskLevel || "LOW"] || riskConfig.LOW;

  return (
    <div className="bg-bg-surface rounded-2xl border border-warning-100 shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-5 py-4 border-b border-border-subtle bg-warning-50/50">
        <AlertTriangle className="w-5 h-5 text-warning-600 shrink-0" />
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-text-primary">Review Required</h3>
            <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-warning-50 text-warning-600 border border-warning-100">
              AWAITING CONFIRMATION
            </span>
          </div>
          <span className={cn("text-xs font-medium px-2 py-0.5 rounded-full mt-1 inline-block", risk.bg, risk.text)}>
            {risk.label}
          </span>
        </div>
      </div>

      <div className="p-5 space-y-4">
        <p className="text-sm text-text-secondary">{summary}</p>

        {/* Work Stream A: the transaction date under review is prominent -
            a defaulted today-assumption is labelled honestly. */}
        {transactionDate && (
          <div className="flex items-center gap-2 text-sm">
            <span className="text-xs font-medium text-text-muted uppercase tracking-wide">
              Dated
            </span>
            <span className="text-sm font-semibold text-text-primary tabular-nums">
              {transactionDate}
              {dateDefaulted ? " (assumed today)" : ""}
            </span>
          </div>
        )}

        {/* Accounting Impact Preview */}
        {accountingImpact && accountingImpact.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-xs font-medium text-text-muted uppercase tracking-wide flex items-center gap-1.5">
              <BookOpen className="w-3.5 h-3.5" /> Accounting Entry Preview
            </h4>
            <div className="bg-bg-muted rounded-xl p-3 space-y-1">
              {accountingImpact.map((entry, i) => (
                <div key={i} className="flex items-center justify-between text-sm">
                  <span className="text-text-secondary">{entry.account}</span>
                  <span className="text-text-primary font-medium tabular-nums">
                    {entry.debit != null && <span>Dr {formatCurrency(entry.debit)}</span>}
                    {entry.credit != null && <span>Cr {formatCurrency(entry.credit)}</span>}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Decision buttons */}
        <div className="flex gap-3 pt-2">
          <button
            onClick={() => onDecision(false)}
            disabled={disabled}
            className="px-4 py-2.5 rounded-xl border border-border-default text-sm font-medium text-text-secondary hover:text-text-primary hover:bg-bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
          <button
            onClick={() => onDecision(true)}
            disabled={disabled}
            className="px-5 py-2.5 rounded-xl bg-ai-600 text-white text-sm font-medium hover:bg-ai-700 focus:outline-none focus:ring-2 focus:ring-ai-500/30 transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Confirm Action
          </button>
        </div>
      </div>
    </div>
  );
}
