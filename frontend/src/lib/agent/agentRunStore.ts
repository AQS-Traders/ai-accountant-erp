/* ==================================================================
   AgentRunStore — navigation-proof agent run controller.
   ==================================================================
   The agent's run state (progress, live steps, result card, awaiting
   clarification/confirmation) lives HERE, at module level — never in
   a component's useState. Consequences:

   * Clicking any sidebar tab unmounts AICommandBox, but the run's
     fetch/SSE promise chain keeps streaming into this store — the work
     being executed is NEVER killed or lost.
   * Returning to the dashboard re-renders the exact state (progress,
     awaiting-question card, result) instantly from the snapshot.
   * A compact AgentRunDock mounted in the dashboard layout shows live
     status on EVERY tab while the agent works.
   * Full page reloads reattach via /api/ai/sessions/latest-active
     (the run continues server-side through the disconnect).

   Components subscribe with useSyncExternalStore; updates are plain
   immutable snapshots. */
import {
  aiClarify,
  aiConfirm,
  aiExecuteStream,
  aiEnqueueJob,
  aiGetJob,
  isApiErrorWithStatus,
  latestActiveSession,
  runBackgroundJob,
} from "@/lib/api/client";
import type { AgentResponse, UserRequest } from "@/lib/types/api";
import { ExecutionStatus } from "@/lib/types/enums";

/* Structurally identical to AIProgress's LiveStepEvent (kept here to
   avoid a lib → components import; TS structural typing bridges them). */
export interface AgentLiveStep {
  step_type: string;
  phase: string | null;
  status: string | null;
  created_at: string | null;
}

export interface AgentRunState {
  loading: boolean;
  activeRequestId: string | null;
  liveSteps: AgentLiveStep[];
  response: AgentResponse | null;
  error: string;
  /* Informational, non-blocking message (stranded run, run awaiting an
     answer...). Unlike `error` it never implies the box is unusable. */
  notice: string;
  reattached: boolean;
  stalledJobId: string | null;
}

let state: AgentRunState = {
  loading: false,
  activeRequestId: null,
  liveSteps: [],
  response: null,
  error: "",
  notice: "",
  reattached: false,
  stalledJobId: null,
};

const listeners = new Set<() => void>();

function set(partial: Partial<AgentRunState>) {
  state = { ...state, ...partial };
  listeners.forEach((l) => l());
}

/* Background runs are the DEFAULT path when a supervised worker drains
   ai.worker_jobs (local dev). A serverless host (Vercel) has no worker,
   so background mode is enabled only when the backend is local or
   explicitly forced — mirroring the backend run contracts. */
export const USE_BACKGROUND_RUNS =
  process.env.NEXT_PUBLIC_BACKGROUND_RUNS === "1" ||
  /^(https?:)?\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(
    process.env.NEXT_PUBLIC_API_URL ?? ""
  );

/* Fired whenever the agent FINISHES a mutation (or fails one) so every
   listening page reloads its data instantly — no manual browser refresh. */
function notifyDataChanged() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("erp:data-changed"));
  }
}

const requestId = () =>
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `req-${Date.now()}-${Math.random().toString(36).slice(2)}`;

function isTerminal(res: AgentResponse): boolean {
  return (
    res.status === ExecutionStatus.COMPLETED ||
    res.status === ExecutionStatus.FAILED ||
    res.status === ExecutionStatus.REJECTED
  );
}

function applyResponse(res: AgentResponse) {
  const terminal = isTerminal(res);
  set({
    response: res,
    loading: false,
    liveSteps: [],
    ...(terminal ? { activeRequestId: null, reattached: false, stalledJobId: null } : {}),
  });
  if (terminal) notifyDataChanged();
}

async function runForeground(request: UserRequest) {
  const res = await aiExecuteStream(request, {
    onStep: (step) =>
      set({
        liveSteps: [
          ...state.liveSteps,
          {
            step_type: step.step_type,
            phase: step.phase ?? null,
            status: step.status ?? null,
            created_at: step.created_at ?? null,
          },
        ],
      }),
  });
  applyResponse(res);
}

function runBackground(request: UserRequest) {
  return runBackgroundJob({
    enqueue: () => aiEnqueueJob(request),
    getJob: aiGetJob,
    onResult: applyResponse,
    onError: (msg) => set({ error: msg, loading: false }),
    onStalled: (jobId) => set({ stalledJobId: jobId }),
  });
}

/* ---- Reattach (page refresh mid-run) — module-level, idempotent ----
   The poller watches the latest in-flight session until it reaches a
   terminal state. It is NOT tied to any component: navigating away
   and back never interrupts it, and unmounts cannot stop it.

   It is BOUNDED, though. A run whose serverless invocation was killed
   mid-flight stays NON-TERMINAL in the database for ever; the watcher
   used to re-attach to that corpse on every dashboard load, showing
   "Processing" indefinitely and polling every 4s with no user input at
   all (an unbounded API-call leak). It now backs off, gives up after
   REATTACH_DEADLINE_MS, and reports a stranded run as a one-line notice
   instead of a spinner that never ends. */
let reattachStarted = false;

/* Only these can still move: anything else is settled (or dead) and must
   never be presented as a run in progress. */
const LIVE_RUN_STATUSES = new Set(["PENDING", "PLANNING", "EXECUTING"]);
const REATTACH_POLL_MS = 4000;
const REATTACH_SLOW_POLL_MS = 10000;
const REATTACH_SLOW_AFTER_MS = 60_000;
const REATTACH_DEADLINE_MS = 180_000;
const REATTACH_MAX_ERRORS = 4;

const STRANDED_NOTICE =
  "The previous run stopped reporting progress, so it was closed. It may have " +
  "timed out - please send your request again.";
const AWAITING_NOTICE =
  "Your last request is still waiting for your answer. Send it again to continue.";

function ensureReattach() {
  if (reattachStarted || state.loading) return;
  reattachStarted = true;

  (async () => {
    let info;
    try {
      info = await latestActiveSession();
    } catch {
      reattachStarted = false; /* backend offline / not signed in */
      return;
    }
    if (!info.found || !info.session) {
      reattachStarted = false;
      return;
    }

    const status = (info.session.status || "").toUpperCase();
    const convId = info.session.conversation_id;

    /* Parked on a question: NOTHING is executing, so a spinner here would
       spin for ever (only the user's answer resumes the run). Say what is
       really going on and leave the input box free. */
    if (status === "WAITING_FOR_USER") {
      reattachStarted = false;
      set({
        notice: AWAITING_NOTICE,
        loading: false,
        activeRequestId: null,
        reattached: false,
      });
      return;
    }

    if (!LIVE_RUN_STATUSES.has(status) || !convId) {
      reattachStarted = false;
      return;
    }

    const startedAt = Date.now();
    let errors = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    set({ activeRequestId: convId, loading: true, reattached: true, notice: "" });

    const stop = (notice?: string) => {
      if (timer) clearTimeout(timer);
      timer = null;
      reattachStarted = false;
      set({
        loading: false,
        activeRequestId: null,
        liveSteps: [],
        reattached: false,
        ...(notice ? { notice } : {}),
      });
    };

    const step = async () => {
      const elapsed = Date.now() - startedAt;
      if (elapsed > REATTACH_DEADLINE_MS) {
        stop(STRANDED_NOTICE);
        notifyDataChanged();
        return;
      }
      try {
        const again = await latestActiveSession();
        errors = 0;
        const live =
          again.found &&
          LIVE_RUN_STATUSES.has((again.session?.status || "").toUpperCase());
        if (!live) {
          stop();
          notifyDataChanged(); /* finished while we were away */
          return;
        }
      } catch {
        errors += 1;
        if (errors >= REATTACH_MAX_ERRORS) {
          stop("Lost contact with the server while resuming your last run.");
          return;
        }
      }
      timer = setTimeout(
        step,
        elapsed > REATTACH_SLOW_AFTER_MS ? REATTACH_SLOW_POLL_MS : REATTACH_POLL_MS
      );
    };

    timer = setTimeout(step, REATTACH_POLL_MS);
  })();
}

export const agentRunStore = {
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  getSnapshot: (): AgentRunState => state,

  /* Boot the reattach watcher (called on any dashboard mount — idempotent) */
  ensureReattach,

  setError: (msg: string) => set({ error: msg }),
  clearError: () => set({ error: "" }),

  /* Start a run. The promise chain lives in THIS module — sidebar
     navigation cannot abort, unmount or orphan it. */
  async startRun(request: UserRequest) {
    if (state.loading) return; // one run at a time
    set({
      loading: true,
      response: null,
      error: "",
      notice: "",
      stalledJobId: null,
      reattached: false,
      liveSteps: [],
    });
    const rid = requestId();
    set({ activeRequestId: rid });
    try {
      if (USE_BACKGROUND_RUNS) {
        try {
          await runBackground({ ...request, conversation_id: rid });
        } catch (jobErr) {
          if (!isApiErrorWithStatus(jobErr, 404)) throw jobErr;
          await runForeground({ ...request, conversation_id: rid }); // older backend
        }
      } else {
        await runForeground({ ...request, conversation_id: rid });
      }
    } catch (err) {
      set({
        error:
          err instanceof Error
            ? /failed to fetch|networkerror|net::err/i.test(err.message)
              ? "The AI service is offline. Start the backend server (port 8000) and try again."
              : err.message
            : "Something went wrong",
      });
    } finally {
      set({ loading: false, activeRequestId: null, liveSteps: [] });
    }
  },

  async clarify(answer: string) {
    if (!state.response?.execution_id) return;
    // In-flight guard: a double click / repeated Enter / duplicate HTTP
    // request must never fire a SECOND clarify — the first one already
    // consumed the pending clarification, and a duplicate re-runs execute().
    if (state.loading) return;
    set({ loading: true, error: "" });
    try {
      const res = await aiClarify({
        session_id: state.response.execution_id,
        answer,
      });
      applyResponse(res);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "Failed to send answer" });
    } finally {
      set({ loading: false });
    }
  },

  async confirm(approved: boolean, notes?: string) {
    if (!state.response?.execution_id) return;
    // In-flight guard: see clarify() — a duplicate confirm must never
    // re-enter execution.
    if (state.loading) return;
    set({ loading: true, error: "" });
    try {
      const res = await aiConfirm({
        session_id: state.response.execution_id,
        approved,
        notes,
      });
      applyResponse(res);
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "Failed to send decision" });
    } finally {
      set({ loading: false });
    }
  },

  /* One-click foreground fallback when the queue is not being drained. */
  async runStalledInForeground(request: UserRequest) {
    set({ stalledJobId: null, loading: true, error: "" });
    const rid = requestId();
    set({ activeRequestId: rid });
    try {
      await runForeground({ ...request, conversation_id: rid });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "Something went wrong" });
    } finally {
      set({ loading: false, activeRequestId: null, liveSteps: [] });
    }
  },

  /* Dismiss the result card / dock chip after reading it. */
  dismissResult() {
    set({ response: null, error: "", notice: "", reattached: false, stalledJobId: null });
  },

  /* Dismiss the informational notice (stranded run / awaiting answer). */
  dismissNotice() {
    set({ notice: "" });
  },
};