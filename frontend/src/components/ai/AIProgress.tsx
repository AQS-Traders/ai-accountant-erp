"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, XCircle, ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { aiProgress } from "@/lib/api/client";
import type { AgentProgress, ProgressStep } from "@/lib/types/api";

const POLL_MS = 1500;

/* Hard ceiling on polling. A stale conversation id (a run whose executor
   was killed mid-flight) never reaches a terminal status, so an unbounded
   poll would hammer the API for as long as the tab stayed open. */
const POLL_DEADLINE_MS = 10 * 60 * 1000;

const PHASE_LABELS: Record<string, string> = {
  RECEIVED: "Request received",
  INTERPRETING: "Understanding intent",
  PLANNING: "Building plan",
  CONTEXT_LOADING: "Loading ERP context",
  AWAITING_CLARIFICATION: "Missing information",
  AWAITING_CONFIRMATION: "Awaiting your confirmation",
  EXECUTING: "Executing trusted tools",
  VALIDATING: "Validating results",
  VERIFYING: "Verifying database state",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  REJECTED: "Cancelled",
};

const PHASE_ORDER = [
  "RECEIVED",
  "INTERPRETING",
  "PLANNING",
  "CONTEXT_LOADING",
  "EXECUTING",
  "VALIDATING",
  "VERIFYING",
  "COMPLETED",
];

// Multi-colour pipeline - each phase has its own identity colour so the
// user can SEE the agent move through the machine.
const PHASE_COLORS: Record<string, { dot: string; text: string }> = {
  RECEIVED: { dot: "bg-info-500", text: "text-info-600" },
  INTERPRETING: { dot: "bg-ai-500", text: "text-ai-600" },
  PLANNING: { dot: "bg-purple-500", text: "text-purple-600" },
  CONTEXT_LOADING: { dot: "bg-warning-500", text: "text-warning-600" },
  EXECUTING: { dot: "bg-success-500", text: "text-success-600" },
  VALIDATING: { dot: "bg-info-500", text: "text-info-600" },
  VERIFYING: { dot: "bg-ai-600", text: "text-ai-700" },
  COMPLETED: { dot: "bg-success-500", text: "text-success-600" },
};

// Rotating, human thoughts per phase - swaps every ~1.2s so the waiting
// feels ALIVE instead of frozen (perceived-latency psychology).
const PHASE_THOUGHTS: Record<string, string[]> = {
  RECEIVED: [
    "Request received…",
    "Waking up the agent…",
  ],
  INTERPRETING: [
    "Thinking…",
    "Understanding what you mean…",
    "Reading between the lines…",
    "Identifying the nature of this transaction…",
  ],
  PLANNING: [
    "Reasoning…",
    "Choosing the right tools…",
    "Drafting an execution plan…",
    "Weighing the accounting impact…",
  ],
  CONTEXT_LOADING: [
    "Fetching your data…",
    "Checking customers, suppliers & accounts…",
    "Gathering ERP context…",
  ],
  AWAITING_CLARIFICATION: [
    "Waiting for your answer…",
  ],
  AWAITING_CONFIRMATION: [
    "Waiting for your approval…",
  ],
  EXECUTING: [
    "Recording the transaction…",
    "Working with trusted tools…",
    "Writing to the ledger…",
  ],
  VALIDATING: [
    "Validating accounting rules…",
    "Checking debits = credits…",
  ],
  VERIFYING: [
    "Verifying the database…",
    "Cross-checking balances…",
    "Almost there…",
  ],
  COMPLETED: ["Done"],
  FAILED: ["Something went wrong"],
};

const TERMINAL_STATUSES = [
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "REJECTED",
  "WAITING_FOR_USER",
];

function stepSummaryLabel(step: ProgressStep): string {
  const s = (step.summary || {}) as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof s.intent === "string") parts.push(s.intent.replace(/_/g, " "));
  const ents = s.extracted_entities as Record<string, unknown> | undefined;
  if (ents && typeof ents === "object" && !Array.isArray(ents)) {
    for (const [key, value] of Object.entries(ents)) {
      if (typeof value === "string" && value.trim()) {
        parts.push(`${key.replace(/_/g, " ")}: ${value}`);
      } else if (typeof value === "number") {
        parts.push(`${key.replace(/_/g, " ")}: ${value.toLocaleString()}`);
      }
    }
  }
  const counts = ["customers", "suppliers", "accounts", "projects", "documents"] as const;
  for (const key of counts) {
    const value = s[key];
    if (typeof value === "number") parts.push(`${value} ${key.replace(/_/g, " ")}`);
  }
  if (typeof s.attachments === "number") {
    parts.push(`${s.attachments} attachment${s.attachments === 1 ? "" : "s"} read`);
  }
  if (typeof s.transaction_nature === "string") parts.push(s.transaction_nature);
  if (typeof s.question === "string" && s.question) {
    parts.push(String(s.question).slice(0, 120));
  }
  return parts.slice(0, 4).join(" · ");
}

/**
 * A step event pushed LIVE over SSE (Work Stream D). Shape mirrors the
 * polled ProgressStep so the render path is identical.
 */
export interface LiveStepEvent {
  step_type: string;
  phase?: string | null;
  status?: string | null;
  created_at?: string | null;
}

export default function AIProgress({
  conversationId,
  liveSteps,
}: {
  conversationId: string | null;
  /** When provided, steps stream in over SSE and polling is skipped. */
  liveSteps?: LiveStepEvent[];
}) {
  const [progress, setProgress] = useState<AgentProgress | null>(null);
  const [expanded, setExpanded] = useState(false);
  // Rotating "thought" index - the visible status changes every ~1.2s so
  // the wait feels alive (perceived-latency psychology).
  const [thoughtIdx, setThoughtIdx] = useState(0);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const t = setInterval(() => {
      setThoughtIdx((i) => i + 1);
      setElapsed((s) => s + 1);
    }, 1150);
    return () => clearInterval(t);
  }, []);

  // Polls the BACKEND-RECORDED state (ai.execution_steps / session phase).
  // The panel reflects what the agent ACTUALLY did - no fake animations.
  // Skipped entirely when steps are streamed in live (SSE mode).
  useEffect(() => {
    if (!conversationId || liveSteps) return;
    let cancelled = false;
    const deadline = Date.now() + POLL_DEADLINE_MS;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      try {
        const next = await aiProgress(conversationId);
        if (cancelled) return;
        setProgress(next);
        const done =
          next.status && TERMINAL_STATUSES.includes(next.status);
        if (done && next.steps.length > 0) {
          setExpanded(true);
          return; // terminal - stop polling
        }
      } catch {
        // Backend briefly unreachable - stay calm and keep polling.
      }
      // Give up once the ceiling is reached: nothing is coming.
      if (!cancelled && Date.now() < deadline) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [conversationId, liveSteps]);

  // SSE mode: derive the rendered steps directly from the live stream
  // (identical shape to the polled ProgressStep rows).
  const streamedSteps = (liveSteps ?? []).map((s, i) => ({
    step_type: s.step_type,
    phase: s.phase || s.step_type,
    status: s.status ?? "COMPLETED",
    created_at: s.created_at ?? new Date().toISOString(),
    summary: {} as Record<string, unknown>,
    _order: i,
  }));
  const steps = liveSteps ? streamedSteps : progress?.steps ?? [];
  const tools = progress?.tools ?? [];
  const currentPhase = liveSteps
    ? streamedSteps[streamedSteps.length - 1]?.phase
    : progress?.current_phase;
  const terminal = Boolean(
    progress?.status && TERMINAL_STATUSES.includes(progress.status)
  );

  // Canonical stages that have REAL evidence (recorded steps or live phase).
  const phasesWithEvidence = new Set<string>();
  for (const step of steps) {
    const phase = step.phase || step.step_type;
    if (phase) phasesWithEvidence.add(phase);
  }
  if (currentPhase) phasesWithEvidence.add(currentPhase);
  const stagesToRender = PHASE_ORDER.filter(
    (p) => phasesWithEvidence.has(p) && p !== "RECEIVED"
  );

  const thoughts = PHASE_THOUGHTS[currentPhase ?? ""] ?? ["Working…"];
  const thought = thoughts[thoughtIdx % thoughts.length];
  const activeColor = PHASE_COLORS[currentPhase ?? ""] ?? {
    dot: "bg-ai-500",
    text: "text-ai-600",
  };
  const stepIndex = currentPhase ? PHASE_ORDER.indexOf(currentPhase) : -1;

  return (
    <div className="glass rounded-2xl p-4">
      {/* Header - collapsible */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 text-sm font-medium text-text-primary text-left"
        aria-expanded={expanded}
      >
        {terminal && progress?.status === "COMPLETED" ? (
          <CheckCircle2 className="w-5 h-5 text-success-500 shrink-0" />
        ) : terminal ? (
          <XCircle
            className={cn(
              "w-5 h-5 shrink-0",
              progress?.status === "FAILED"
                ? "text-error-500"
                : "text-text-muted"
            )}
          />
        ) : (
          /* Pulsing gradient orb - the agent's "brain" while it thinks */
          <span className="relative w-5 h-5 shrink-0 flex items-center justify-center">
            <span
              className={cn(
                "absolute inset-0 rounded-full opacity-40 animate-ping",
                activeColor.dot
              )}
            />
            <span
              className={cn(
                "relative w-3 h-3 rounded-full animate-think",
                activeColor.dot
              )}
            />
          </span>
        )}
        <span className="flex-1 min-w-0">
          {terminal ? (
            <span className="font-medium">
              Agent progress
              {currentPhase && (
                <span className="text-text-secondary font-normal">
                  {" "}- {PHASE_LABELS[currentPhase] || currentPhase.toLowerCase()}
                </span>
              )}
            </span>
          ) : (
            <>
              {/* Rotating thought - key forces the fade-swap animation */}
              <span
                key={thought}
                className={cn(
                  "block font-semibold animate-fade-swap",
                  activeColor.text
                )}
              >
                {thought}
              </span>
              <span className="block text-[11px] text-text-muted font-normal mt-0.5">
                {PHASE_LABELS[currentPhase ?? ""] || "Agent working"}
                {" · "}
                {elapsed}s
              </span>
            </>
          )}
        </span>
        {expanded ? (
          <ChevronDown className="w-4 h-4 text-text-muted" />
        ) : (
          <ChevronRight className="w-4 h-4 text-text-muted" />
        )}
      </button>

      {/* Multi-colour pipeline stepper - visible while the agent works */}
      {!terminal && stepIndex >= 0 && (
        <div className="mt-3 flex items-center gap-0">
          {PHASE_ORDER.slice(0, -1).map((phase, i) => {
            const done = i < stepIndex;
            const active = phase === currentPhase;
            const color = PHASE_COLORS[phase] ?? PHASE_COLORS.RECEIVED;
            return (
              <div key={phase} className="flex items-center flex-1 last:flex-none">
                <div
                  className={cn(
                    "w-2.5 h-2.5 rounded-full shrink-0 transition-all duration-300",
                    done && color.dot,
                    active && cn(color.dot, "ring-4 ring-ai-100 animate-pulse-slow"),
                    !done && !active && "bg-border-default"
                  )}
                  title={PHASE_LABELS[phase]}
                />
                {i < PHASE_ORDER.length - 2 && (
                  <div
                    className={cn(
                      "h-0.5 flex-1 transition-colors duration-300",
                      done ? "bg-success-300" : "bg-border-subtle"
                    )}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}

      {expanded && (
        <div className="mt-3 max-h-64 overflow-y-auto pr-1 space-y-2">
          {steps.length === 0 && tools.length === 0 && !currentPhase && (
            <p className="text-xs text-text-muted">
              Waiting for the agent to accept the request…
            </p>
          )}

          {stagesToRender.map((phase) => {
            const isActive = phase === currentPhase && !terminal;
            const label = PHASE_LABELS[phase] || phase.replace(/_/g, " ");
            const matching = steps.filter(
              (s) => (s.phase || s.step_type) === phase
            );
            const labels = matching
              .map(stepSummaryLabel)
              .filter(Boolean);
            return (
              <div key={phase} className="flex items-start gap-2.5 text-sm">
                {isActive ? (
                  <span className="relative w-4 h-4 mt-0.5 shrink-0 flex items-center justify-center">
                    <span className="absolute inset-0 rounded-full bg-ai-500 opacity-40 animate-ping" />
                    <span className="relative w-2 h-2 rounded-full bg-ai-500 animate-think" />
                  </span>
                ) : (
                  <CheckCircle2 className="w-4 h-4 mt-0.5 text-success-500 shrink-0" />
                )}
                <div className="min-w-0">
                  <span
                    className={cn(
                      isActive
                        ? "text-text-primary font-medium"
                        : "text-text-secondary"
                    )}
                  >
                    {label}
                  </span>
                  {labels.length > 0 && (
                    <p className="text-xs text-text-muted mt-0.5 break-words">
                      {labels[0]}
                    </p>
                  )}
                </div>
              </div>
            );
          })}

          {tools.length > 0 && (
            <div className="text-xs text-text-muted pt-1 border-t border-border-subtle">
              <span className="font-medium text-text-secondary">
                Trusted tools used:{" "}
              </span>
              {tools.map((t) => t.tool.replace(/_/g, " ")).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
