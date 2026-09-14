"use client";

import { LucideIcon, FileText, Search, Users, ShoppingCart, Landmark, BarChart3, Wallet, ArrowRight, Plus, Bot } from "lucide-react";
import { cn } from "@/lib/utils/cn";

export type EmptyStateIllustration = "default" | "empty" | "error" | "success" | "invoices" | "customers" | "suppliers" | "banking" | "reports" | "journal";

interface QuickAction {
  label: string;
  icon: LucideIcon;
  onClick: () => void;
  variant?: "primary" | "secondary";
}

interface QuickTip {
  title: string;
  description: string;
  action?: string;
  href?: string;
}

interface EmptyStateProps {
  illustration?: EmptyStateIllustration;
  icon?: LucideIcon;
  title: string;
  description: string;
  primaryAction?: { label: string; onClick: () => void; icon?: LucideIcon };
  secondaryAction?: { label: string; onClick: () => void; icon?: LucideIcon };
  aiPrompt?: string;
  onAiClick?: () => void;
  quickActions?: QuickAction[];
  quickTips?: QuickTip[];
  className?: string;
  children?: React.ReactNode;
}

const ILLUSTRATION_ICONS: Record<EmptyStateIllustration, LucideIcon> = {
  default: FileText, empty: Search, error: FileText, success: FileText,
  invoices: FileText, customers: Users, suppliers: ShoppingCart,
  banking: Landmark, reports: BarChart3, journal: Wallet,
};

const ILLUSTRATION_BG: Record<EmptyStateIllustration, string> = {
  default: "from-slate-100 to-slate-50", empty: "from-slate-100 to-slate-50",
  error: "from-red-50 to-red-100", success: "from-emerald-50 to-emerald-100",
  invoices: "from-blue-50 to-indigo-100", customers: "from-teal-50 to-cyan-100",
  suppliers: "from-amber-50 to-orange-100", banking: "from-violet-50 to-purple-100",
  reports: "from-indigo-50 to-blue-100", journal: "from-emerald-50 to-teal-100",
};

export default function EmptyState({
  illustration = "default", icon, title, description,
  primaryAction, secondaryAction, aiPrompt, onAiClick,
  quickActions, quickTips, className, children,
}: EmptyStateProps) {
  const IllustrationIcon = icon || ILLUSTRATION_ICONS[illustration];
  const bgGradient = ILLUSTRATION_BG[illustration] || ILLUSTRATION_BG.default;

  return (
    <div className={cn("flex flex-col items-center justify-center text-center py-12 px-6", className)}>
      <div className={cn("w-20 h-20 rounded-2xl bg-gradient-to-br flex items-center justify-center mb-6", bgGradient)}>
        <IllustrationIcon className="w-10 h-10 text-text-secondary" strokeWidth={1.5} />
      </div>
      <h3 className="text-lg font-semibold text-text-primary mb-2 max-w-md">{title}</h3>
      <p className="text-sm text-text-secondary max-w-lg mb-6">{description}</p>

      {(primaryAction || secondaryAction || onAiClick) && (
        <div className="flex flex-wrap items-center justify-center gap-3 mb-8">
          {primaryAction && (
            <button onClick={primaryAction.onClick}
              className="btn-3d btn-shine inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-gradient-to-r from-teal-500 to-cyan-500 text-white text-sm font-medium shadow-lg shadow-teal-500/25 hover:shadow-cyan-500/40 transition-all">
              {primaryAction.icon && <primaryAction.icon className="w-4 h-4" />}
              {primaryAction.label}
            </button>
          )}
          {secondaryAction && (
            <button onClick={secondaryAction.onClick}
              className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl border border-border-default bg-bg-surface text-text-primary text-sm font-medium hover:border-ai-300 transition-colors">
              {secondaryAction.icon && <secondaryAction.icon className="w-4 h-4" />}
              {secondaryAction.label}
            </button>
          )}
          {onAiClick && (
            <button onClick={onAiClick}
              className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl border border-ai-200 bg-ai-50 text-ai-700 text-sm font-medium hover:bg-ai-100 transition-colors">
      {children}
    </div>
  );
}

export function NoDataEmptyState({ entityName, entityLabel, icon, onCreate, onAiClick, aiContext }: {
  entityName: string; entityLabel: string; icon?: LucideIcon;
  onCreate?: () => void; onAiClick?: () => void; aiContext?: string;
}) {
  return (
    <EmptyState icon={icon} title={`No ${entityName} yet`}
      description={`You haven't created any ${entityLabel} yet. Add your first one manually or ask the AI to create it for you.`}
      primaryAction={onCreate ? { label: `Add ${entityName}`, onClick: onCreate, icon: Plus } : undefined}
      onAiClick={onAiClick} aiPrompt={aiContext ? `Create ${aiContext}` : `Add a ${entityName}`}
      quickTips={[
        { title: "Manual Entry", description: `Click "Add ${entityName}" to create one manually.` },
        { title: "AI Assistant", description: "Tell the AI in plain language and it handles the accounting." },
      ]}
    />
  );
}

export function NoFilterResultsEmptyState({ onClearFilters, filterDescription }: {
  onClearFilters: () => void; filterDescription?: string;
}) {
  return (
    <EmptyState illustration="empty" icon={Search} title="No results found"
      description={filterDescription || "No records match your current filters. Try adjusting your search criteria."}
      primaryAction={{ label: "Clear Filters", onClick: onClearFilters, icon: ArrowRight }}
    />
  );
}

export function ErrorEmptyState({ title = "Something went wrong", description = "We couldn't load this data. This might be a temporary issue.", onRetry, errorCode }: {
  title?: string; description?: string; onRetry?: () => void; errorCode?: string;
}) {
  return (
    <EmptyState illustration="error" icon={FileText} title={title} description={description}
      primaryAction={onRetry ? { label: "Try Again", onClick: onRetry, icon: ArrowRight } : undefined}
      quickTips={errorCode ? [{ title: "Error Code", description: errorCode }] : undefined}
    />
  );
}

              <Bot className="w-4 h-4" />
              {aiPrompt || "Ask AI"}
            </button>
          )}
        </div>
      )}

      {quickActions && quickActions.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 w-full max-w-2xl mb-8">
          {quickActions.map((action, idx) => (
            <button key={idx} onClick={action.onClick}
              className={cn("group flex flex-col items-center gap-3 p-4 rounded-xl border transition-all hover:-translate-y-0.5",
                action.variant === "primary" ? "border-ai-200 bg-ai-50 hover:bg-ai-100" : "border-border-subtle bg-bg-surface hover:border-ai-200 hover:bg-bg-muted"
              )}>
              <action.icon className="w-6 h-6 text-ai-600 group-hover:scale-110 transition-transform" />
              <span className="text-sm font-medium text-text-primary">{action.label}</span>
            </button>
          ))}
        </div>
      )}

      {quickTips && quickTips.length > 0 && (
        <div className="w-full max-w-2xl space-y-3">
          <p className="text-xs font-medium text-text-muted uppercase tracking-wider">Quick Tips</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {quickTips.map((tip, idx) => (
              <div key={idx} className="text-left p-3 rounded-xl bg-bg-muted border border-border-subtle">
                <p className="text-sm font-medium text-text-primary mb-1">{tip.title}</p>
                <p className="text-xs text-text-secondary">{tip.description}</p>
                {tip.action && tip.href && (
                  <a href={tip.href} className="inline-flex items-center gap-1 text-xs text-ai-600 hover:text-ai-700 mt-2">
                    {tip.action} <ArrowRight className="w-3 h-3" />
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {children}
    </div>
  );
}
