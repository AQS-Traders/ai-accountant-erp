"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  Building2,
  Check,
  ChevronDown,
  Loader2,
  MessageCircleQuestion,
  Sparkles,
  Wand2,
} from "lucide-react";
import Modal from "@/components/shared/Modal";
import { cn } from "@/lib/utils/cn";
import { aiAnalyzeOrganization } from "@/lib/api/client";
import type { OnboardingAnalysis, OnboardingAnswer, OnboardingBundle } from "@/lib/types/api";

/* Presentation-only labels.  The SET of fields is whatever the backend
   accepts — this map only decides how they read on screen. */
const FIELD_LABELS: Record<string, string> = {
  name: "Organization name",
  business_type: "Business type",
  legal_name: "Registered legal name",
  tax_number: "Tax / VAT number",
  registration_number: "Company registration number",
  core_services: "Main products or services",
  industry_details: "Industry",
  base_currency_code: "Base currency",
  country_code: "Country",
  timezone: "Timezone",
  fiscal_year_end_month: "Fiscal year ends",
  fiscal_year_start_year: "First financial year",
};

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const SOURCE_LABELS: Record<string, string> = {
  inferred: "from your description",
  backend_default: "backend default",
  inferred_from_stated_country: "from the country you mentioned",
};

const EXAMPLES = [
  "We are a small software house providing web and mobile development services to international clients. We don't maintain physical inventory and most of our revenue comes from project-based work.",
  "We buy electrical goods from importers and resell them to retailers from our warehouse. We hold stock and sell on credit.",
  "We manufacture furniture and sell to wholesalers. We run a small factory with machinery and employ production staff.",
];

function displayValue(field: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (field === "fiscal_year_end_month") {
    const month = Number(value);
    return MONTHS[month - 1] ?? String(value);
  }
  if (field === "business_type") return String(value).replace(/_/g, " ").toLowerCase();
  return String(value);
}

export interface OnboardingApplyPayload {
  /** Validated, backend-compatible field values. */
  fields: Record<string, string | number | null>;
  /** Bundles the user approved (codes understood by apply_organization_onboarding). */
  groups: string[];
  /** The assistant's analysis + answers, stored on the onboarding record. */
  responses: Record<string, unknown>;
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** Business type currently selected in the form (a hint, not a verdict). */
  businessType?: string;
  onApply: (payload: OnboardingApplyPayload) => void;
}

export default function AIOrganizationWizard({ open, onClose, businessType, onApply }: Props) {
  const [description, setDescription] = useState("");
  const [analysis, setAnalysis] = useState<OnboardingAnalysis | null>(null);
  const [answers, setAnswers] = useState<OnboardingAnswer[]>([]);
  const [history, setHistory] = useState<{ role: "user" | "assistant"; content: string }[]>([]);
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const [draftAnswers, setDraftAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /* Reset whenever the dialog is opened, so a second run never shows the
     previous organization's proposal. */
  useEffect(() => {
    if (!open) return;
    setAnalysis(null);
    setAnswers([]);
    setHistory([]);
    setSelectedGroups([]);
    setDraftAnswers({});
    setExpandedGroup(null);
    setError(null);
    setBusy(false);
  }, [open]);

  const runAnalysis = useCallback(
    async (nextAnswers: OnboardingAnswer[], nextHistory: { role: "user" | "assistant"; content: string }[]) => {
      setBusy(true);
      setError(null);
      try {
        const result = await aiAnalyzeOrganization({
          description,
          business_type: businessType || null,
          answers: nextAnswers,
          history: nextHistory,
        });
        setAnalysis(result);
        setSelectedGroups(
          (result.account_groups ?? []).filter((g) => g.selected).map((g) => g.code),
        );
      } catch (e) {
        setError(
          e instanceof Error
            ? `The assistant could not be reached (${e.message.slice(0, 120)}). You can set everything up manually below.`
            : "The assistant could not be reached. You can set everything up manually below.",
        );
      } finally {
        setBusy(false);
      }
    },
    [description, businessType],
  );

  const start = () => {
    const text = description.trim();
    if (text.length < 10) {
      setError("Describe the business in a sentence or two so the assistant has something to work with.");
      return;
    }
    runAnalysis([], []);
  };

  const sendAnswers = () => {
    const filled: OnboardingAnswer[] = Object.entries(draftAnswers)
      .filter(([, value]) => value.trim())
      .map(([field, answer]) => ({ field, answer: answer.trim() }));
    if (filled.length === 0) {
      setError("Answer at least one question so the assistant can continue.");
      return;
    }
    const nextAnswers = [...answers, ...filled];
    const nextHistory = [
      ...history,
      { role: "assistant" as const, content: (analysis?.questions ?? []).map((q) => q.question).join(" ") },
      { role: "user" as const, content: filled.map((a) => `${a.field}: ${a.answer}`).join("; ") },
    ];
    setAnswers(nextAnswers);
    setHistory(nextHistory);
    setDraftAnswers({});
    runAnalysis(nextAnswers, nextHistory);
  };

  const toggleGroup = (code: string) =>
    setSelectedGroups((current) =>
      current.includes(code) ? current.filter((c) => c !== code) : [...current, code],
    );

  const apply = () => {
    if (!analysis) return;
    // Only validated, backend-compatible values are handed over, and only
    // bundles the catalog published for the resolved business type.
    onApply({
      fields: analysis.fields ?? {},
      groups: selectedGroups,
      responses: {
        source: "ai_onboarding_assistant",
        description,
        answers,
        summary: analysis.summary,
        field_sources: analysis.field_sources,
        field_notes: analysis.field_notes,
        unresolved: analysis.unresolved,
        account_groups: (analysis.account_groups ?? []).map((g) => ({
          code: g.code,
          label: g.label,
          selected: selectedGroups.includes(g.code),
          reason: g.reason,
        })),
        business_type_chart: analysis.analysis?.business_type_chart ?? null,
      },
    });
    onClose();
  };

  const populated = useMemo(
    () => Object.entries(analysis?.fields ?? {}).filter(([, value]) => value !== null && value !== ""),
    [analysis],
  );
  const groups: OnboardingBundle[] = analysis?.account_groups ?? [];

  return (
    <Modal open={open} onClose={onClose} title="Create Organization with AI" wide>
      <div className="space-y-4">
        {/* Intro — plain language, no accounting terms. */}
        {!analysis && (
          <>
            <div className="rounded-xl bg-ai-50 border border-ai-100 p-3 flex gap-2.5">
              <Sparkles className="w-4 h-4 text-ai-600 shrink-0 mt-0.5" />
              <p className="text-xs text-ai-700 leading-relaxed">
                Describe your business the way you would explain it to a friend.
                The assistant reads your words, fills in the organization setup and
                suggests the accounts your business actually needs — then you review
                and change anything before anything is created.
              </p>
            </div>

            <div>
              <label className="text-xs font-medium text-text-secondary">
                What does your business do?
              </label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={6}
                autoFocus
                placeholder={EXAMPLES[0]}
                className="mt-1.5 w-full px-3 py-2 rounded-xl bg-bg-primary border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors resize-y"
              />
              <p className="text-[11px] text-text-muted mt-1">
                Mention what you sell, how you sell it and anything unusual (stock,
                machinery, premises, staff, where you are based). You do not need to
                know any accounting terms.
              </p>
            </div>

            <div className="flex flex-wrap gap-1.5">
              {EXAMPLES.map((example, index) => (
                <button
                  key={index}
                  type="button"
                  onClick={() => setDescription(example)}
                  className="text-[11px] px-2.5 py-1.5 rounded-full border border-border-default text-text-secondary hover:border-ai-300 hover:text-ai-700 transition-colors"
                >
                  Example {index + 1}
                </button>
              ))}
            </div>

            {error && (
              <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2 flex gap-2">
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                {error}
              </p>
            )}

            <div className="flex items-center justify-between pt-1">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors"
              >
                I&apos;ll set it up manually
              </button>
              <button
                type="button"
                onClick={start}
                disabled={busy || description.trim().length < 10}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wand2 className="w-4 h-4" />}
                {busy ? "Reading your description…" : "Analyze with AI"}
              </button>
            </div>
          </>
        )}

        {/* Busy (after the first result, while re-analyzing with answers). */}
        {analysis && busy && (
          <div className="flex items-center gap-2 text-sm text-text-secondary py-6 justify-center">
            <Loader2 className="w-4 h-4 animate-spin" />
            Updating the configuration from your answers…
          </div>
        )}

        {/* Clarification questions — only what the backend requires, or what
            genuinely changes the accounts. */}
        {analysis && !busy && analysis.status === "needs_information" && (
          <div className="space-y-3">
            <div className="flex items-start gap-2.5">
              <MessageCircleQuestion className="w-4 h-4 text-ai-600 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-text-primary">
                  A couple of things I need from you
                </p>
                <p className="text-xs text-text-secondary mt-0.5">{analysis.summary}</p>
              </div>
            </div>

            {analysis.questions.map((question) => (
              <div
                key={question.id}
                className="rounded-xl border border-border-default bg-bg-primary p-3 space-y-2"
              >
                <p className="text-sm text-text-primary">{question.question}</p>
                {question.why && (
                  <p className="text-[11px] text-text-muted">Why: {question.why}</p>
                )}
                {question.options && question.options.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {question.options.map((option) => (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() =>
                          setDraftAnswers((current) => ({
                            ...current,
                            [question.field]: option.value,
                          }))
                        }
                        className={cn(
                          "px-2.5 py-1.5 rounded-full text-[11px] border transition-colors",
                          draftAnswers[question.field] === option.value
                            ? "bg-ai-500 border-ai-500 text-white"
                            : "border-border-default text-text-secondary hover:border-ai-300 hover:text-ai-700",
                        )}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                )}
                <input
                  value={
                    question.options?.some((o) => o.value === draftAnswers[question.field])
                      ? ""
                      : draftAnswers[question.field] ?? ""
                  }
                  onChange={(e) =>
                    setDraftAnswers((current) => ({ ...current, [question.field]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter") sendAnswers();
                  }}
                  placeholder="Or answer in your own words…"
                  className="w-full px-3 py-2 rounded-xl bg-bg-surface border border-border-default text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-100 focus:border-ai-300 transition-colors"
                />
              </div>
            ))}

            {error && (
              <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2 flex gap-2">
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                {error}
              </p>
            )}

            <div className="flex items-center justify-between pt-1">
              <button
                type="button"
                onClick={() => setDescription(EXAMPLES[0])}
                className="text-[11px] text-text-muted hover:text-text-secondary transition-colors"
              >
                Rewrite the description instead
              </button>
              <button
                type="button"
                onClick={sendAnswers}
                disabled={busy}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium disabled:opacity-40 transition-colors flex items-center gap-2"
              >
                <ArrowRight className="w-4 h-4" />
                Send answers
              </button>
            </div>
          </div>
        )}
        {/* Review — every populated field, what was NOT stated, and the
            account bundles (each with its reason). */}
        {analysis && !busy && analysis.status !== "needs_information" && (
          <div className="space-y-4">
            <div className="flex items-start gap-2.5">
              <Sparkles className="w-4 h-4 text-ai-600 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-text-primary">
                  {analysis.status === "ok" ? "Here is what I understood" : "I could not analyze that"}
                </p>
                <p className="text-xs text-text-secondary mt-0.5">{analysis.summary}</p>
              </div>
            </div>

            {populated.length > 0 && (
              <div className="rounded-xl border border-border-subtle overflow-hidden">
                {populated.map(([field, value]) => (
                  <div
                    key={field}
                    className="flex items-start justify-between gap-3 px-3 py-2 border-b border-border-subtle last:border-b-0"
                  >
                    <span className="text-[11px] uppercase tracking-wide text-text-muted shrink-0 pt-0.5">
                      {FIELD_LABELS[field] ?? field}
                    </span>
                    <span className="text-right min-w-0">
                      <span className="block text-sm text-text-primary break-words">
                        {displayValue(field, value)}
                      </span>
                      {analysis.field_notes[field] && (
                        <span className="block text-[11px] text-text-muted">
                          {analysis.field_notes[field]}
                        </span>
                      )}
                      <span className="block text-[10px] text-ai-600">
                        {SOURCE_LABELS[analysis.field_sources[field]] ?? "set by the assistant"}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            )}

            {analysis.unresolved.length > 0 && (
              <p className="text-[11px] text-text-muted">
                You did not tell me about:{" "}
                {analysis.unresolved.map((f) => FIELD_LABELS[f] ?? f).join(", ")}. I have left those
                for you to fill in — I will not guess them.
              </p>
            )}

            {groups.length > 0 && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Building2 className="w-3.5 h-3.5 text-text-muted" />
                  <p className="text-xs font-medium text-text-secondary">
                    Accounts this will create
                    {analysis.analysis?.business_type_chart ? (
                      <span className="text-text-muted font-normal">
                        {" "}
                        — {String(analysis.analysis.business_type_chart)} chart
                        {analysis.analysis.base_account_count
                          ? ` (${String(analysis.analysis.base_account_count)} base accounts)`
                          : ""}
                      </span>
                    ) : null}
                  </p>
                </div>
                <p className="text-[11px] text-text-muted">
                  Untick anything your business does not need. The chart is built only from the
                  bundles you leave ticked.
                </p>
                {groups.map((group) => {
                  const checked = selectedGroups.includes(group.code);
                  const expanded = expandedGroup === group.code;
                  const count = group.account_count ?? group.accounts?.length ?? 0;
                  return (
                    <div
                      key={group.code}
                      className={cn(
                        "rounded-xl border p-2.5 transition-colors",
                        checked ? "border-ai-200 bg-ai-50/60" : "border-border-default bg-bg-primary",
                      )}
                    >
                      <div className="flex items-start gap-2.5">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleGroup(group.code)}
                          className="mt-0.5 w-4 h-4 rounded border-border-default text-ai-600 focus:ring-ai-500/30"
                        />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-text-primary flex items-center gap-2">
                            {group.label}
                            {group.recommended && (
                              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-ai-100 text-ai-700">
                                recommended
                              </span>
                            )}
                          </p>
                          {group.reason && (
                            <p className="text-[11px] text-text-secondary mt-0.5">{group.reason}</p>
                          )}
                          {count > 0 && (
                            <button
                              type="button"
                              onClick={() => setExpandedGroup(expanded ? null : group.code)}
                              className="mt-1 inline-flex items-center gap-1 text-[11px] text-ai-700 hover:text-ai-600"
                            >
                              <ChevronDown
                                className={cn("w-3 h-3 transition-transform", expanded && "rotate-180")}
                              />
                              {expanded ? "Hide" : "Show"} {count} account{count === 1 ? "" : "s"}
                            </button>
                          )}
                          {expanded && (
                            <ul className="mt-1.5 space-y-0.5">
                              {(group.accounts ?? []).map((account) => (
                                <li key={account.code} className="text-[11px] text-text-secondary">
                                  <span className="tabular-nums text-text-muted">{account.code}</span>{" "}
                                  {account.name}
                                  {account.parent_code && (
                                    <span className="text-text-muted">
                                      {" "}
                                      · under {account.parent_code}
                                    </span>
                                  )}
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {analysis.status === "unavailable" && (
              <p className="text-xs text-text-secondary bg-bg-muted rounded-xl px-3 py-2">
                Nothing was filled in for you. You can set the fields yourself — the chart of
                accounts is still generated from the business type.
              </p>
            )}

            {error && (
              <p className="text-xs text-error-600 bg-error-50 rounded-xl px-3 py-2 flex gap-2">
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                {error}
              </p>
            )}

            <div className="flex items-center justify-between pt-2 border-t border-border-subtle">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 rounded-xl text-sm font-medium text-text-secondary hover:text-text-primary transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={apply}
                className="px-5 py-2 btn-3d btn-shine rounded-xl bg-ai-500 hover:bg-ai-600 text-white text-sm font-medium transition-colors flex items-center gap-2"
              >
                <Check className="w-4 h-4" />
                Apply to the setup form
              </button>
            </div>
            <p className="text-[11px] text-text-muted">
              This only fills the form — you still review every field and press
              &ldquo;Create organization&rdquo; yourself. Nothing has been created yet.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}