"use client";

import { useState, useRef, useCallback, useEffect, useSyncExternalStore } from "react";
import { Send, Paperclip, X, Loader2, Sparkles } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type {
  AgentResponse,
  AttachmentRef,
  UserRequest,
} from "@/lib/types/api";
import {
  ALLOWED_UPLOAD_MIMES,
  MAX_UPLOAD_BYTES,
} from "@/lib/types/api";
import { ExecutionStatus } from "@/lib/types/enums";
import { agentRunStore, USE_BACKGROUND_RUNS, type AgentRunState } from "@/lib/agent/agentRunStore";
import AIProgress from "./AIProgress";
import AIActionCard from "./AIActionCard";
import AIClarification from "./AIClarification";
import AIConfirmation from "./AIConfirmation";

const PLACEHOLDERS = [
  "Tell me what happened...",
  "Create an invoice for ABC Technologies...",
  "Record an expense of Rs. 25,000...",
  "Show me this month's profit...",
  "Upload a receipt and I'll help record it.",
];

export default function AICommandBox() {
  // Hydration safety: the FIRST render (server and client) must be
  // byte-identical.  A module-scope Math.random() placeholder was evaluated
  // separately on the server and the client, producing different
  // placeholder attributes and the "Hydration failed because the server
  // rendered HTML didn't match the client" error.  Use a deterministic
  // initial placeholder and only randomise AFTER hydration (useEffect).
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  useEffect(() => {
    setPlaceholderIdx(Math.floor(Math.random() * PLACEHOLDERS.length));
  }, []);
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  /* ---- Run state lives in the navigation-proof module store --------
     Clicking any sidebar tab unmounts this box — the in-flight run,
     its live steps and its result card survive in agentRunStore and
     re-render the moment you come back. */
  const run: AgentRunState = useSyncExternalStore(
    agentRunStore.subscribe,
    agentRunStore.getSnapshot,
    agentRunStore.getSnapshot,
  );
  const { loading, response, error, notice, liveSteps, activeRequestId, reattached, stalledJobId } = run;

  /* Boot the reattach watcher once per session (module-level, idempotent):
     a page refresh mid-run resumes the live progress view. */
  useEffect(() => {
    agentRunStore.ensureReattach();
  }, []);

  /* Mirror the store's terminal result onto the LOCAL input: clear the
     message + attachments once the agent completed (or failed) a run. */
  const lastTerminalRef = useRef<AgentResponse | null>(null);
  useEffect(() => {
    if (!response) return;
    const terminal =
      response.status === ExecutionStatus.COMPLETED ||
      response.status === ExecutionStatus.FAILED;
    if (terminal && lastTerminalRef.current !== response) {
      lastTerminalRef.current = response;
      setMessage("");
      setAttachments([]);
    }
  }, [response]);

  const autoGrow = useCallback(() => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
  }, []);

  const buildRequest = useCallback(
    (): UserRequest => ({
      message: message.trim(),
      attachments: attachments.length > 0 ? attachments : undefined,
    }),
    [message, attachments],
  );

  const handleSend = async () => {
    if (!message.trim() && attachments.length === 0) return;
    // The promise chain lives in agentRunStore — sidebar navigation
    // during the run is safe and never orphans the work.
    await agentRunStore.startRun(buildRequest());
  };

  // One-click foreground fallback when the queue is not being drained.
  const runStalledJobInForeground = async () => {
    await agentRunStore.runStalledInForeground(buildRequest());
  };

  const handleClarify = async (answer: string) => {
    await agentRunStore.clarify(answer);
  };

  const handleConfirm = async (approved: boolean, notes?: string) => {
    await agentRunStore.confirm(approved, notes);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="w-full max-w-3xl mx-auto space-y-4">
      {/* Input box */}
      <div className="glass rounded-2xl p-1 shadow-lg">
        <div className="flex items-start gap-2 p-2">
          <div className="p-2 text-ai-500">
            <Sparkles className="w-5 h-5" />
          </div>
          <textarea
            ref={textareaRef}
            value={message}
            onChange={(e) => {
              setMessage(e.target.value);
              autoGrow();
            }}
            onKeyDown={handleKeyDown}
            placeholder={PLACEHOLDERS[placeholderIdx]}
            rows={1}
            className="flex-1 bg-transparent text-sm text-text-primary placeholder:text-text-muted resize-none focus:outline-none py-2 min-h-[40px] max-h-[200px]"
          />
        </div>

        {/* Attachment previews */}
        {attachments.length > 0 && (
          <div className="flex gap-2 px-3 pb-2 flex-wrap">
            {attachments.map((att, i) => (
              <div
                key={i}
                className="flex items-center gap-1.5 px-2.5 py-1.5 bg-bg-muted rounded-lg text-xs text-text-secondary"
              >
                <Paperclip className="w-3 h-3" />
                <span className="max-w-24 truncate">{att.file_name}</span>
                <button
                  onClick={() => setAttachments((a) => a.filter((_, j) => j !== i))}
                  className="text-text-muted hover:text-text-primary"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Bottom bar */}
        <div className="flex items-center justify-between px-2 pb-1">
          <div className="flex items-center gap-1">
            <label className="p-2 rounded-lg text-text-muted hover:text-text-secondary hover:bg-bg-muted cursor-pointer transition-colors">
              <Paperclip className="w-4 h-4" />
              <input
                type="file"
                className="hidden"
                multiple
                accept="image/png,image/jpeg,image/webp"
                onChange={(e) => {
                  const files = Array.from(e.target.files || []);
                  e.target.value = ""; // allow re-selecting the same file
                  const errors: string[] = [];
                  const valid = files.filter((f) => {
                    if (!ALLOWED_UPLOAD_MIMES.has(f.type)) {
                      errors.push(`${f.name}: only PNG, JPEG or WEBP images are supported.`);
                      return false;
                    }
                    if (f.size > MAX_UPLOAD_BYTES) {
                      errors.push(`${f.name}: file is larger than 8 MB.`);
                      return false;
                    }
                    return true;
                  });
                  if (errors.length > 0) agentRunStore.setError(errors.join(" "));

                  // Read each file as base64 so the backend receives the
                  // ACTUAL document for vision extraction (blob URLs never
                  // leave the browser).
                  valid.forEach((f) => {
                    const reader = new FileReader();
                    reader.onload = () => {
                      const dataUrl = String(reader.result || "");
                      const base64 = dataUrl.split(",")[1] || "";
                      setAttachments((prev) => [
                        ...prev,
                        {
                          file_name: f.name,
                          file_url: URL.createObjectURL(f),
                          mime_type: f.type,
                          file_size: f.size,
                          data_base64: base64,
                        },
                      ]);
                    };
                    reader.onerror = () =>
                      agentRunStore.setError(`${f.name}: could not read the file.`);
                    reader.readAsDataURL(f);
                  });
                }}
              />
            </label>
          </div>
          <button
            onClick={handleSend}
            disabled={loading || (!message.trim() && attachments.length === 0)}
            className={cn(
              "btn-3d btn-shine flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-semibold transition-all",
              loading
                ? "bg-ai-100 text-ai-500 cursor-wait"
                : "bg-gradient-to-b from-ai-500 to-ai-600 text-white hover:from-ai-400 hover:to-ai-600"
            )}
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            {loading ? "Processing" : "Send"}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="p-3 rounded-xl bg-error-50 border border-error-100 text-sm text-error-600">
          {error}
        </div>
      )}

      {/* Informational notice - e.g. a previous run was closed because it
          stopped reporting progress. Never blocks the input: the user can
          simply send a new request (or dismiss it). */}
      {notice && !loading && (
        <div className="p-3 rounded-xl bg-warning-50 border border-warning-100 text-sm text-text-secondary flex items-start justify-between gap-3">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => agentRunStore.dismissNotice()}
            className="shrink-0 text-text-muted hover:text-text-primary transition-colors"
            aria-label="Dismiss notice"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Loading progress - REAL agent state polled from the backend */}
      {loading && (
        <div className="space-y-2">
          {reattached && (
            <p className="text-xs text-text-muted">
              Reattached to a run that was already in progress. The result will
              be available in AI Activity when it completes.
            </p>
          )}
          <AIProgress
            conversationId={activeRequestId}
            liveSteps={
              !reattached && !USE_BACKGROUND_RUNS ? liveSteps : undefined
            }
          />
        </div>
      )}

      {/* Work Stream C: queued but not picked up by any worker - offer the
          one-click foreground fallback instead of an endless wait. */}
      {stalledJobId && !loading && (
        <div className="p-3 rounded-xl bg-warning-50 border border-warning-100 text-sm text-text-secondary flex items-center justify-between gap-3">
          <span>
            No background worker has picked up this run yet. Start one with
            &quot;python scripts/ai_worker.py&quot; or run it here instead.
          </span>
          <button
            onClick={runStalledJobInForeground}
            className="btn-3d-soft shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium"
          >
            Run in foreground instead
          </button>
        </div>
      )}

      {/* Response rendering */}
      {response && !loading && (
        <>
          {response.status === ExecutionStatus.AWAITING_CLARIFICATION && (
            <AIClarification
              question={response.question || "I need more information."}
              options={response.options}
              questionOptions={response.question_options}
              onAnswer={handleClarify}
            />
          )}
          {response.status === ExecutionStatus.AWAITING_CONFIRMATION && (
            <AIConfirmation
              summary={response.summary || ""}
              riskLevel={response.risk_level}
              accountingImpact={response.accounting_impact}
              transactionDate={
                typeof response.data?.transaction_date === "string"
                  ? response.data.transaction_date
                  : undefined
              }
              dateDefaulted={Boolean(response.data?.date_defaulted)}
              onDecision={handleConfirm}
            />
          )}
          {response.status === ExecutionStatus.COMPLETED && (
            <AIActionCard response={response} />
          )}
          {response.status === ExecutionStatus.REJECTED && (
            <div className="p-4 rounded-xl bg-bg-muted border border-border-subtle">
              <p className="text-sm font-medium text-text-secondary">Action cancelled</p>
              <p className="text-sm text-text-muted mt-1">
                {response.summary || "The action was rejected and nothing was recorded."}
              </p>
            </div>
          )}
          {response.status === ExecutionStatus.FAILED && (
            <div className="p-4 rounded-xl bg-error-50 border border-error-100">
              <p className="text-sm font-medium text-error-600">Something went wrong</p>
              <p className="text-sm text-error-500 mt-1">{response.summary || "The operation could not be completed."}</p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
