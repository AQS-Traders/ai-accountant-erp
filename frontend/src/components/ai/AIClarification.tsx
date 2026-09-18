"use client";

import { useState } from "react";
import { MessageCircleQuestion, Send } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type { QuestionOption } from "@/lib/types/api";

interface Props {
  question: string;
  options?: string[];
  /** Work Stream R3.4 — backend-emitted options per numbered
   *  sub-question (aligned by index; the hardcoded quickOptions
   *  switch below remains the offline fallback). */
  questionOptions?: QuestionOption[][];
  onAnswer: (answer: string) => void;
  /** While a resume request is in flight every control is disabled —
   *  a double click must never fire a duplicate answer. */
  disabled?: boolean;
}

/** Split a consolidated questionnaire ("...:\n1. Q1\n2. Q2") into its
 *  individual questions so EACH gets its own answer box. */
function splitQuestions(question: string): string[] {
  const lines = question.split(/\n+/).map((l) => l.trim()).filter(Boolean);
  const numbered = lines
    .map((l) => l.match(/^(\d+)[.)]\s*(.+)$/))
    .filter((m): m is RegExpMatchArray => m !== null);
  const numberedCount = lines.filter((l) => /^\d+[.)]\s/.test(l)).length;
  if (numbered.length >= 2 && numbered.length === numberedCount) {
    return numbered.map((m) => m[2].trim());
  }
  return [];
}

/** Work Stream R2: the intro is the questionnaire text MINUS the numbered
 *  questions (those render as their own interactive fields - repeating
 *  them verbatim made the card duplicated and cluttered). */
function introText(question: string): string {
  return question
    .split(/\n+/)
    .map((l) => l.trim())
    .filter((l) => l && !/^\d+[.)]\s/.test(l))
    .join("\n");
}

/** Work Stream R3.4a — strip model markdown (**bold**, backticks,
 *  headings, bullets) before rendering; the backend sanitizes too,
 *  this is the belt-and-braces fallback. */
function sanitizeMarkdown(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/(^|[^*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)/g, "$1$2")
    .replace(/`/g, "")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}[-*]\s+/gm, "");
}

/** Work Stream R2: tap-to-select answers for known question types
 *  (no typing needed). */
function quickOptions(q: string): string[] | null {
  if (/fixed asset/i.test(q) && /inventory/i.test(q)) {
    return ["a", "b", "c", "d"];
  }
  if (/cash or on credit/i.test(q)) {
    return ["CASH", "BANK", "CREDIT"];
  }
  return null;
}

/** Work Stream R3.4 — the option list for ONE sub-question: the
 *  backend payload wins (data-driven), the hardcoded switch is the
 *  fallback.  Dates offer Today/Yesterday chips when the backend did
 *  not send a payload. */
function optionsForSubQuestion(
  q: string,
  index: number,
  questionOptions?: QuestionOption[][]
): QuestionOption[] | null {
  const payload = questionOptions?.[index];
  if (payload && payload.length > 0) {
    return payload;
  }
  const hardcoded = quickOptions(q);
  if (hardcoded) {
    return hardcoded.map((opt) => ({ value: opt, label: opt }));
  }
  if (/transaction date|expense date|receive the goods/i.test(q)) {
    return [
      { value: "TODAY", label: "Today" },
      { value: "YESTERDAY", label: "Yesterday" },
    ];
  }
  return null;
}

const isPaymentQ = (q: string) => /cash or on credit/i.test(q);
/** R3.3 — the settlement question's Outstanding answer ("c") also
 *  requires the party box (a payable needs a party). */
const isSettlementQ = (q: string) => /paid, or is it outstanding/i.test(q);

export default function AIClarification({
  question,
  options,
  questionOptions,
  onAnswer,
  disabled = false,
}: Props) {
  const subQuestions = splitQuestions(question);
  const multi = subQuestions.length >= 2;
  const [custom, setCustom] = useState("");
  // One answer slot per question - answered separately, sent together.
  const [answers, setAnswers] = useState<string[]>(
    multi ? subQuestions.map(() => "") : []
  );
  // Work Stream R2: the CONDITIONAL party box appears only when CREDIT is
  // selected on the payment question.  Ticking the Local Vendor toggle
  // disables the text box; the typed text is then NOT sent - the standing
  // "Local Vendor" account is used instead.
  const [creditParty, setCreditParty] = useState("");
  const [localVendor, setLocalVendor] = useState(false);
  const paymentIdx = multi
    ? subQuestions.findIndex((q) => isPaymentQ(q))
    : -1;
  const settlementIdx = multi
    ? subQuestions.findIndex((q) => isSettlementQ(q))
    : -1;
  // CREDIT on the payment question — or "Outstanding" (c) on the R3.3
  // settlement question — means a payable, which needs a party.
  const creditSelected =
    (paymentIdx >= 0 &&
      (answers[paymentIdx] ?? "").trim().toUpperCase() === "CREDIT") ||
    (settlementIdx >= 0 &&
      /^(c|3|outstanding|credit)\b/i.test((answers[settlementIdx] ?? "").trim()));
  const allAnswered =
    multi &&
    answers.every((a) => a.trim().length > 0) &&
    (!creditSelected || localVendor || creditParty.trim().length > 0);

  // Quick-response buttons are ONLY for genuine answer options (e.g. "CASH",
  // "CREDIT"). A question (text ending in "?") must never be sent back as an
  // answer - that echoed the question to the backend and caused an infinite
  // clarification loop.
  const answerOptions = (options ?? []).filter(
    (opt) => opt.trim().length > 0 && !opt.trim().endsWith("?")
  );

  // R3.4: structured chips for a SINGLE question (backend payload or
  // fallback).  Typing remains available below the chips.
  const singleOpts = multi
    ? null
    : optionsForSubQuestion(question, 0, questionOptions);

  const sendMulti = () => {
    if (!allAnswered || disabled) return;
    // Numbered so the backend routes every part to its own question.
    // Work Stream R2: when CREDIT is selected, the conditional party box
    // is appended as a trailing "N) supplier: X" part (the backend pairs
    // it with the supplier field); the Local Vendor toggle sends the
    // standing "Local Vendor" answer instead of any typed text.
    const lines = subQuestions.map((q, i) => `${i + 1}) ${answers[i].trim()}`);
    if (creditSelected) {
      lines.push(
        `${subQuestions.length + 1}) supplier: ${
          localVendor ? "Local Vendor" : creditParty.trim()
        }`
      );
    }
    onAnswer(lines.join("\n"));
  };

  // Deduplicated header: only the intro prose, never the numbered
  // questions (they render below as their own interactive fields).
  // R3.4a: markdown is stripped before rendering.
  const headerText = sanitizeMarkdown(multi ? introText(question) : question);

  return (
    <div className="bg-bg-surface rounded-2xl border border-warning-100 shadow-sm p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div className="p-2 rounded-xl bg-warning-50 text-warning-600 shrink-0">
          <MessageCircleQuestion className="w-5 h-5" />
        </div>
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-text-primary">I need a bit more info</h3>
            <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-warning-50 text-warning-600 border border-warning-100">
              AWAITING YOUR ANSWER
            </span>
          </div>
          <p className="text-sm text-text-secondary mt-1 whitespace-pre-line">{headerText}</p>
        </div>
      </div>

      {/* R3.4: structured single-question chips (label shown, value sent) */}
      {singleOpts && singleOpts.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {singleOpts.map((opt) => (
            <button
              key={opt.value}
              type="button"
              disabled={disabled}
              onClick={() => onAnswer(opt.value)}
              className="px-4 py-2 rounded-xl bg-bg-muted text-sm font-medium text-text-primary hover:bg-ai-50 hover:text-ai-700 border border-border-subtle hover:border-ai-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}

      {/* Quick response buttons (genuine answer options only) */}
      {answerOptions.length > 0 && !multi && (
        <div className="flex flex-wrap gap-2">
          {answerOptions.map((opt) => (
            <button
              key={opt}
              disabled={disabled}
              onClick={() => onAnswer(opt)}
              className="px-4 py-2 rounded-xl bg-bg-muted text-sm font-medium text-text-primary hover:bg-ai-50 hover:text-ai-700 border border-border-subtle hover:border-ai-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {opt}
            </button>
          ))}
        </div>
      )}

      {/* ONE ANSWER BOX PER QUESTION - answered separately, sent together */}
      {multi ? (
        <div className="space-y-3">
          {subQuestions.map((q, i) => (
            <div key={i} className="space-y-1">
              <label className="text-xs font-medium text-text-muted flex items-start gap-1.5">
                <span className="inline-flex items-center justify-center w-5 h-5 mt-px rounded-full bg-ai-50 text-ai-700 text-[10px] font-semibold shrink-0">
                  {i + 1}
                </span>
                <span>{sanitizeMarkdown(q)}</span>
              </label>
              {/* R3.4: data-driven chips — backend payload first, the
                  hardcoded quickOptions switch as fallback. */}
              {optionsForSubQuestion(q, i, questionOptions) && (
                <div className="flex flex-wrap gap-1.5">
                  {optionsForSubQuestion(q, i, questionOptions)!.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() =>
                        setAnswers((prev) =>
                          prev.map((a, j) => (j === i ? opt.value : a))
                        )
                      }
                      className={cn(
                        "px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors",
                        (answers[i] ?? "").toUpperCase() === opt.value.toUpperCase()
                          ? "bg-ai-50 text-ai-700 border-ai-300"
                          : "bg-bg-muted text-text-secondary border-border-subtle hover:border-ai-200"
                      )}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              )}
              <input
                type="text"
                value={answers[i] ?? ""}
                autoFocus={i === 0 && !optionsForSubQuestion(q, i, questionOptions)}
                disabled={disabled}
                onChange={(e) =>
                  setAnswers((prev) =>
                    prev.map((a, j) => (j === i ? e.target.value : a))
                  )
                }
                onKeyDown={(e) => {
                  if (e.key === "Enter" && allAnswered) sendMulti();
                }}
                placeholder="Type your answer here..."
                className="w-full px-3 py-2 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-500/30 focus:border-ai-500 transition disabled:bg-bg-muted disabled:text-text-muted disabled:cursor-not-allowed"
              />
              {/* Work Stream R2: the CONDITIONAL party box - only visible
                  once CREDIT is selected on the payment question (or
                  "Outstanding" on the R3.3 settlement question). */}
              {(i === paymentIdx || i === settlementIdx) && creditSelected && (
                <div className="space-y-2 rounded-xl border border-ai-100 bg-ai-50/40 p-3">
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={localVendor}
                      onChange={(e) => setLocalVendor(e.target.checked)}
                      className="w-4 h-4 rounded border-border-default text-ai-600 focus:ring-ai-500/30"
                    />
                    <span className="text-xs font-medium text-text-secondary">
                      Purchased from a local vendor (one-off - no party name
                      needed; a &quot;Local Vendor&quot; account will be used)
                    </span>
                  </label>
                  <input
                    type="text"
                    value={creditParty}
                    disabled={localVendor}
                    onChange={(e) => setCreditParty(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && allAnswered) sendMulti();
                    }}
                    placeholder="Party / vendor name for the credit ledger..."
                    className="w-full px-3 py-2 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-500/30 focus:border-ai-500 transition disabled:bg-bg-muted disabled:text-text-muted disabled:cursor-not-allowed"
                  />
                </div>
              )}
            </div>
          ))}
          <div className="flex justify-end">
            <button
              onClick={sendMulti}
              disabled={!allAnswered || disabled}
              title={allAnswered ? undefined : "Answer every question to send"}
              className="btn-3d btn-shine px-3 py-2 rounded-xl bg-gradient-to-b from-ai-500 to-ai-600 text-white hover:from-ai-400 hover:to-ai-600 transition flex items-center gap-1.5 text-sm font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send className="w-4 h-4" />
              Send all answers
            </button>
          </div>
        </div>
      ) : (
        /* Single question - one free-text answer */
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
            Your answer
          </label>
          <div className="flex gap-2">
            <input
              type="text"
              value={custom}
              autoFocus
              disabled={disabled}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && custom.trim() && !disabled) {
                  onAnswer(custom.trim());
                  setCustom("");
                }
              }}
              placeholder="Type your answer here..."
              className="flex-1 px-3 py-2 rounded-xl border border-border-default bg-bg-surface text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-ai-500/30 focus:border-ai-500 transition disabled:bg-bg-muted disabled:text-text-muted disabled:cursor-not-allowed"
            />
            <button
              onClick={() => {
                if (custom.trim() && !disabled) {
                  onAnswer(custom.trim());
                  setCustom("");
                }
              }}
              disabled={!custom.trim() || disabled}
              className="btn-3d px-3 py-2 rounded-xl bg-gradient-to-b from-ai-500 to-ai-600 text-white hover:from-ai-400 hover:to-ai-600 transition flex items-center gap-1.5 text-sm font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send className="w-4 h-4" />
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

