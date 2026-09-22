import type { AgentResponse, UserRequest, ClarificationAnswer, ConfirmationDecision, SessionSummary, AgentProgress, OnboardingSchema, OnboardingAnalysis, OnboardingAnswer } from "@/lib/types/api";

// Same-origin by default: on Vercel the FastAPI backend is served under
// /api/* of the same domain (see vercel.json "services"). For local dev set
// NEXT_PUBLIC_API_URL=http://localhost:8000 in frontend/.env.local.
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function fetchApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  // Get JWT from Supabase session (stored in cookie)
  const { createClient } = await import("@/lib/supabase/client");
  const supabase = createClient();

  async function doFetch<T>(): Promise<T> {
    const { data: { session } } = await supabase.auth.getSession();
    const token = session?.access_token;

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string> || {}),
    };

    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
    });

    if (!res.ok) {
      const body = await res.text();
      throw new ApiError(res.status, body || res.statusText);
    }

    return res.json();
  }

  try {
    return await doFetch<T>();
  } catch (err) {
    // A 401 from the API almost always means the browser held a stale
    // access token (e.g. signed by a rotated JWT signing key). supabase-js
    // does NOT proactively refresh a still-unexpired token, so force one
    // refresh and retry exactly once. Security is unchanged: the retry still
    // carries a freshly issued Supabase session token.
    const is401 = err instanceof ApiError && err.status === 401;
    if (!is401) throw err;
    const { data } = await supabase.auth.refreshSession();
    if (!data.session) throw err;
    return doFetch<T>();
  }
}

/* ---- AI Endpoints ---- */

export async function aiExecute(request: UserRequest): Promise<AgentResponse> {
  return fetchApi<AgentResponse>("/api/ai/execute", {
    method: "POST",
    body: JSON.stringify(request),
  });
}
export async function aiClarify(answer: ClarificationAnswer): Promise<AgentResponse> {
  return fetchApi<AgentResponse>("/api/ai/clarify", {
    method: "POST",
    body: JSON.stringify(answer),
  });
}

export async function aiConfirm(decision: ConfirmationDecision): Promise<AgentResponse> {
  return fetchApi<AgentResponse>("/api/ai/confirm", {
    method: "POST",
    body: JSON.stringify(decision),
  });
}

export async function aiGetSessions(limit = 20, offset = 0): Promise<{ sessions: SessionSummary[] }> {
  return fetchApi(`/api/ai/sessions?limit=${limit}&offset=${offset}`);
}

/**
 * Polls the REAL, user-safe reasoning progress for an in-flight execution.
 * The conversation_id is the client-generated request id passed to aiExecute.
 */
export async function aiProgress(conversationId: string): Promise<AgentProgress> {
  return fetchApi(
    `/api/ai/progress?conversation_id=${encodeURIComponent(conversationId)}`
  );
}

export async function aiGetSession(sessionId: string) {
  return fetchApi(`/api/ai/sessions/${sessionId}`);
}

/**
 * Work Stream C: resume-by-conversation. Returns the latest NON-TERMINAL
 * execution session (status PENDING / PLANNING / EXECUTING) for this
 * user+org so a browser refresh mid-run can reattach the progress view.
 */
export interface ActiveSessionInfo {
  found: boolean;
  session: {
    session_id: string;
    conversation_id: string | null;
    status: string;
    current_phase: string | null;
    created_at: string | null;
    /** Last control-plane write (backend ages stranded runs out on this). */
    updated_at?: string | null;
    age_seconds?: number | null;
  } | null;
}

export async function latestActiveSession(): Promise<ActiveSessionInfo> {
  return fetchApi("/api/ai/sessions/latest-active");
}

/* ---- Catalogue (products & services) -------------------------------------
 *
 * The page goes through the API rather than querying Supabase directly (the
 * customers page does that) for three reasons spelled out in app/main.py:
 * the item CODE is generated server-side by an RPC, the delete rule needs a
 * cross-table usage check, and validation must live in one place so the page
 * and the agent can never disagree.  Both kinds share the same four calls.
 */
export type CatalogueKind = "products" | "services";

export interface CatalogueItem {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  /** Products: "PRD-0001". Services: "SRV-0001". */
  product_code?: string;
  service_code?: string;
  /** Products */
  unit?: string | null;
  is_stock_tracked?: boolean;
  unit_price?: number | null;
  cost_price?: number | null;
  /** Services */
  billing_unit?: string;
  standard_rate?: number | null;
  cost_rate?: number | null;
  revenue_account_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CatalogueCounts {
  total: number;
  active: number;
  inactive: number;
}

export interface CatalogueList {
  items: CatalogueItem[];
  counts: CatalogueCounts;
  status: string;
}

export interface CatalogueMutation {
  item: CatalogueItem;
  reused?: boolean;
}

export async function catalogueList(
  kind: CatalogueKind,
  params: { query?: string; status?: string } = {}
): Promise<CatalogueList> {
  const search = new URLSearchParams();
  if (params.query) search.set("query", params.query);
  if (params.status) search.set("status", params.status);
  const qs = search.toString();
  return fetchApi<CatalogueList>(`/api/catalogue/${kind}${qs ? `?${qs}` : ""}`);
}

export async function catalogueCreate(
  kind: CatalogueKind,
  payload: Record<string, unknown>
): Promise<CatalogueMutation> {
  return fetchApi<CatalogueMutation>(`/api/catalogue/${kind}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function catalogueUpdate(
  kind: CatalogueKind,
  id: string,
  payload: Record<string, unknown>
): Promise<CatalogueMutation> {
  return fetchApi<CatalogueMutation>(`/api/catalogue/${kind}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** Soft delete / reactivate — history keeps resolving the item. */
export async function catalogueSetActive(
  kind: CatalogueKind,
  id: string,
  active: boolean
): Promise<CatalogueMutation> {
  return fetchApi<CatalogueMutation>(`/api/catalogue/${kind}/${id}/status`, {
    method: "POST",
    body: JSON.stringify({ active }),
  });
}

/** Hard delete — the API refuses (409) while any document references the item. */
export async function catalogueDelete(
  kind: CatalogueKind,
  id: string
): Promise<{ deleted: boolean }> {
  return fetchApi<{ deleted: boolean }>(`/api/catalogue/${kind}/${id}`, {
    method: "DELETE",
  });
}

/* ---- Work Stream C: background runs ---- */

export interface EnqueueJobResult {
  queued: boolean;
  job_id: string;
  conversation_id: string | null;
}

export async function aiEnqueueJob(request: UserRequest): Promise<EnqueueJobResult> {
  return fetchApi("/api/ai/jobs", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export interface AiJobStatus {
  job_id: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  conversation_id: string | null;
  result: AgentResponse | null;
  error: string | null;
  created_at: string;
}

export async function aiGetJob(jobId: string): Promise<AiJobStatus> {
  return fetchApi(`/api/ai/jobs/${jobId}`);
}

/** True when *err* is an ApiError carrying HTTP *status* (e.g. 404 from
 *  an older backend without the jobs endpoint). */
export function isApiErrorWithStatus(err: unknown, status: number): boolean {
  return (
    err instanceof Error &&
    typeof (err as unknown as { status?: unknown }).status === "number" &&
    (err as unknown as { status: number }).status === status
  );
}

export interface BackgroundJobHandlers {
  enqueue: () => Promise<EnqueueJobResult>;
  getJob: (jobId: string) => Promise<AiJobStatus>;
  onResult: (res: AgentResponse) => void;
  onError: (message: string) => void;
  onStalled: (jobId: string) => void;
  /** Poll cadence and stall window (injectable for tests). */
  pollIntervalMs?: number;
  stallAfterMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Work Stream C: enqueue an AI run and poll it to a terminal state.
 * SUCCEEDED renders the result card, FAILED surfaces the error, and a
 * QUEUED job no worker claims within the stall window triggers the
 * one-click "run in foreground instead" offer. The jobs endpoint is
 * consulted via the injected handlers so older backends can 404.
 */
export async function runBackgroundJob(
  h: BackgroundJobHandlers
): Promise<void> {
  const wait = h.sleep ?? defaultSleep;
  const interval = h.pollIntervalMs ?? 2_000;
  const stallAfter = h.stallAfterMs ?? 90_000;
  const job = await h.enqueue();
  const startedAt = Date.now();
  for (;;) {
    await wait(interval);
    const status = await h.getJob(job.job_id);
    if (status.status === "SUCCEEDED" && status.result) {
      h.onResult(status.result);
      return;
    }
    if (status.status === "FAILED") {
      h.onError(status.error || "The background run failed.");
      return;
    }
    if (status.status === "QUEUED" && Date.now() - startedAt > stallAfter) {
      h.onStalled(job.job_id);
      return;
    }
  }
}

/* ---- Work Stream D: SSE streaming execution ---- */

export interface StreamEvents {
  onStep?: (step: {
    step_type: string;
    phase?: string | null;
    status?: string | null;
    created_at?: string | null;
  }) => void;
}

/**
 * Runs the agent over the SSE endpoint. Resolves with the FINAL
 * AgentResponse once verification completed; every recorded reasoning
 * step is delivered to onStep as it lands. Falls back to the plain
 * POST endpoint when the stream cannot even be opened (older backend).
 */
export async function aiExecuteStream(
  request: UserRequest,
  events?: StreamEvents
): Promise<AgentResponse> {
  const { createClient } = await import("@/lib/supabase/client");
  const supabase = createClient();
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;

  const res = await fetch(`${API_BASE}/api/ai/execute-stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(request),
  });

  if (!res.ok || !res.body) {
    // Older backend without the SSE route - fall back to the plain call.
    return aiExecute(request);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // Wrapped in an object so TypeScript's closure analysis cannot narrow
  // `final` to `null` (it is assigned inside handleEvent).
  const state = { final: null as AgentResponse | null };

  const handleEvent = (raw: string) => {
    const lines = raw.split("\n");
    let event = "message";
    let data = "";
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return;
    if (event === "step") {
      try {
        events?.onStep?.(JSON.parse(data));
      } catch {
        /* malformed step - ignore */
      }
    } else if (event === "final") {
      try {
        state.final = JSON.parse(data) as AgentResponse;
      } catch {
        /* malformed final - handled by null check below */
      }
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    // SSE messages are separated by a blank line.
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      handleEvent(raw);
    }
  }

  if (!state.final) {
    // Stream ended without a final event - falling back to a sync call is
    // unsafe (the run already happened), so surface an honest error.
    throw new Error("The AI stream ended without a result. Try again.");
  }
  return state.final;
}

/* ---- Organization onboarding (AI-assisted first-time setup) ---- */

/**
 * Real backend-compatible onboarding choices: business types with the chart
 * each one produces, supported currencies, required fields and the optional
 * account bundles. Read from the backend contract, never hard-coded, so the
 * UI cannot offer a choice the backend rejects.
 */
export async function onboardingSchema(businessType?: string): Promise<OnboardingSchema> {
  const query = businessType ? `?business_type=${encodeURIComponent(businessType)}` : "";
  return fetchApi<OnboardingSchema>(`/api/onboarding/schema${query}`);
}

/**
 * Ask the assistant to turn a plain-language business description into an
 * onboarding PROPOSAL. Nothing is created: the result is reviewed (and
 * edited) by the user before the organization is created.
 */
export async function aiAnalyzeOrganization(payload: {
  description: string;
  business_type?: string | null;
  answers?: OnboardingAnswer[];
  history?: { role: "user" | "assistant"; content: string }[];
}): Promise<OnboardingAnalysis> {
  return fetchApi<OnboardingAnalysis>("/api/onboarding/analyze", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/* ---- Health ---- */

export async function healthCheck(): Promise<{ status: string; version: string }> {
  return fetchApi("/api/health");
}
