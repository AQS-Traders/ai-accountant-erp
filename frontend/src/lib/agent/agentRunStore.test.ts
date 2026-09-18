/**
 * agentRunStore — duplicate-resume guard tests.
 *
 * A double click, repeated Enter or a replayed HTTP request must fire the
 * clarify/confirm endpoint EXACTLY ONCE: the first request consumes the
 * pending clarification/confirmation; a duplicate would re-run execute()
 * and could create a second mutation.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const aiClarify = vi.fn();
const aiConfirm = vi.fn();
const aiExecuteStream = vi.fn();
const aiEnqueueJob = vi.fn();
const aiGetJob = vi.fn();
const latestActiveSession = vi.fn();
const runBackgroundJob = vi.fn();

vi.mock("@/lib/api/client", () => ({
  aiClarify: (...args: unknown[]) => aiClarify(...args),
  aiConfirm: (...args: unknown[]) => aiConfirm(...args),
  aiExecuteStream: (...args: unknown[]) => aiExecuteStream(...args),
  aiEnqueueJob: (...args: unknown[]) => aiEnqueueJob(...args),
  aiGetJob: (...args: unknown[]) => aiGetJob(...args),
  latestActiveSession: (...args: unknown[]) => latestActiveSession(...args),
  runBackgroundJob: (...args: unknown[]) => runBackgroundJob(...args),
  isApiErrorWithStatus: () => false,
}));

import { agentRunStore } from "@/lib/agent/agentRunStore";
import { ExecutionStatus } from "@/lib/types/enums";

const PENDING: Record<string, unknown> = {
  status: ExecutionStatus.AWAITING_CLARIFICATION,
  execution_id: "11111111-1111-1111-1111-111111111111",
  question: "Revenue ledger check: create a dedicated ledger 'chairs Sales'?",
  options: [
    "Create 'chairs Sales' under Operating Revenue",
    "Use the existing 'Operating Revenue' account",
  ],
  required_information: ["revenue_ledger_decision"],
  requires_user_input: true,
};

describe("agentRunStore clarify/confirm duplicate guard", () => {
  beforeEach(() => {
    aiClarify.mockReset();
    aiConfirm.mockReset();
    aiExecuteStream.mockReset();
    latestActiveSession.mockReset();
    agentRunStore.dismissResult();
  });

  it("fires clarify exactly once for a double click", async () => {
    aiExecuteStream.mockResolvedValue(PENDING);
    aiClarify.mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 25)),
    );

    await agentRunStore.startRun({ message: "sold 5 chairs for 3000 cash today" });
    expect(aiExecuteStream).toHaveBeenCalledTimes(1);

    // Two synchronous invocations (double click) — only ONE may reach the API.
    agentRunStore.clarify("yes");
    agentRunStore.clarify("yes");
    await new Promise((r) => setTimeout(r, 60));

    expect(aiClarify).toHaveBeenCalledTimes(1);
    // The answer is routed to /api/ai/clarify with the PENDING session id —
    // never to /api/ai/confirm.
    expect(aiClarify).toHaveBeenCalledWith({
      session_id: "11111111-1111-1111-1111-111111111111",
      answer: "yes",
    });
    expect(aiConfirm).not.toHaveBeenCalled();
  });

  it("fires confirm exactly once for a double click", async () => {
    aiExecuteStream.mockResolvedValue({
      ...PENDING,
      status: ExecutionStatus.AWAITING_CONFIRMATION,
    });
    aiConfirm.mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 25)),
    );

    await agentRunStore.startRun({ message: "create invoice for abc tech 200" });
    agentRunStore.confirm(true);
    agentRunStore.confirm(true);
    await new Promise((r) => setTimeout(r, 60));

    expect(aiConfirm).toHaveBeenCalledTimes(1);
    expect(aiConfirm).toHaveBeenCalledWith({
      session_id: "11111111-1111-1111-1111-111111111111",
      approved: true,
      notes: undefined,
    });
    expect(aiClarify).not.toHaveBeenCalled();
  });
});
