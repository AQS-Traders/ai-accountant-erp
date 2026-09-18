/**
 * AIClarification / AIConfirmation — render contract tests (SSR).
 *
 * Pins:
 *  - clarification vs confirmation are SEPARATE renderings (never the same
 *    card / never the same endpoint choice);
 *  - the option chips come from the backend payload with a stable contract;
 *  - the disabled prop disables every submit control (duplicate-request
 *    prevention while a resume request is in flight).
 */
import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import AIClarification from "@/components/ai/AIClarification";
import AIConfirmation from "@/components/ai/AIConfirmation";

const REVENUE_QUESTION =
  "Revenue ledger check: nothing is recorded against 'chairs' yet. " +
  "Create a dedicated ledger 'chairs Sales' under 'Operating Revenue' so " +
  "this revenue is reported separately? Reply YES to create it.";
const REVENUE_OPTIONS = [
  "Create 'chairs Sales' under Operating Revenue",
  "Use the existing 'Operating Revenue' account",
];

describe("AIClarification", () => {
  it("renders the question and the backend option chips", () => {
    const html = renderToString(
      <AIClarification
        question={REVENUE_QUESTION}
        options={REVENUE_OPTIONS}
        onAnswer={() => {}}
      />,
    );
    expect(html).toContain("Revenue ledger check");
    // HTML-escaped quotes: assert on the entity-stable parts of the labels.
    expect(html).toContain("chairs Sales&#x27; under Operating Revenue");
    expect(html).toContain("Use the existing &#x27;Operating Revenue&#x27;");
  });

  it("disables every control while a resume request is in flight", () => {
    const html = renderToString(
      <AIClarification
        question={REVENUE_QUESTION}
        options={REVENUE_OPTIONS}
        onAnswer={() => {}}
        disabled
      />,
    );
    // React 19 SSR renders disabled as the bare `disabled=""` attribute:
    // both chips + the free-text input + the Send button.
    expect((html.match(/disabled=""/g) || []).length).toBeGreaterThanOrEqual(4);
  });

  it("keeps the answer chips enabled when not in flight", () => {
    const html = renderToString(
      <AIClarification
        question={REVENUE_QUESTION}
        options={REVENUE_OPTIONS}
        onAnswer={() => {}}
      />,
    );
    // Only the Send button is disabled (empty input) — both option chips
    // and the free-text input must be interactive.
    expect((html.match(/disabled=""/g) || []).length).toBe(1);
    expect(html).toContain("autofocus=\"\"");
  });
});

describe("AIConfirmation", () => {
  it("renders the confirmation state separately from clarifications", () => {
    const html = renderToString(
      <AIConfirmation summary="Execute: create_invoice" onDecision={() => {}} />,
    );
    expect(html).toContain("AWAITING CONFIRMATION");
    expect(html).toContain("Review Required");
    expect(html).toContain("Confirm Action");
    // A confirmation card never renders a clarification answer box.
    expect(html).not.toContain("Type your answer here");
  });

  it("disables both decision buttons while in flight", () => {
    const html = renderToString(
      <AIConfirmation summary="x" onDecision={() => {}} disabled />,
    );
    expect((html.match(/disabled=""/g) || []).length).toBe(2);
  });
});
