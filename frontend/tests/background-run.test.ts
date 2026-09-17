import { describe, it, expect, vi } from "vitest";
import {
  runBackgroundJob,
  isApiErrorWithStatus,
  type AiJobStatus,
} from "../src/lib/api/client";
import type { AgentResponse } from "../src/lib/types/api";

const instantSleep = () => Promise.resolve();

const fakeResult = {
  status: "COMPLETED",
  summary: "done",
} as unknown as AgentResponse;

function makeJob(
  status: AiJobStatus["status"],
  result: AgentResponse | null = null
): AiJobStatus {
  return {
    job_id: "job-1",
    status,
    conversation_id: "conv-1",
    result,
    error: null,
    created_at: "2026-09-06T00:00:00Z",
  };
}

describe("runBackgroundJob (Work Stream C)", () => {
  it("queued -> running -> succeeded renders the result card", async () => {
    const states: AiJobStatus[] = [
      makeJob("QUEUED"),
      makeJob("RUNNING"),
      makeJob("SUCCEEDED", fakeResult),
    ];
    let poll = 0;
    const onResult = vi.fn();
    const onError = vi.fn();
    const onStalled = vi.fn();

    await runBackgroundJob({
      enqueue: async () => ({ queued: true, job_id: "job-1", conversation_id: "conv-1" }),
      getJob: async () => states[Math.min(poll++, states.length - 1)],
      onResult,
      onError,
      onStalled,
      sleep: instantSleep,
    });

    expect(onResult).toHaveBeenCalledWith(fakeResult);
    expect(onError).not.toHaveBeenCalled();
    expect(onStalled).not.toHaveBeenCalled();
  });

  it("failed job surfaces the error, never a result card", async () => {
    const onResult = vi.fn();
    const onError = vi.fn();
    const onStalled = vi.fn();

    await runBackgroundJob({
      enqueue: async () => ({ queued: true, job_id: "job-1", conversation_id: null }),
      getJob: async () => ({ ...makeJob("FAILED"), error: "provider down" }),
      onResult,
      onError,
      onStalled,
      sleep: instantSleep,
    });

    expect(onError).toHaveBeenCalledWith("provider down");
    expect(onResult).not.toHaveBeenCalled();
  });

  it("job stuck QUEUED past the stall window offers the foreground fallback", async () => {
    const onResult = vi.fn();
    const onError = vi.fn();
    const onStalled = vi.fn();

    await runBackgroundJob({
      enqueue: async () => ({ queued: true, job_id: "job-9", conversation_id: null }),
      getJob: async () => makeJob("QUEUED"),
      onResult,
      onError,
      onStalled,
      pollIntervalMs: 1000,
      stallAfterMs: 2500,
      sleep: instantSleep,
    });

    expect(onStalled).toHaveBeenCalledWith("job-9");
    expect(onResult).not.toHaveBeenCalled();
  });
});

describe("isApiErrorWithStatus", () => {
  it("detects a 404 ApiError (older backend fallback)", () => {
    const err = Object.assign(new Error("Not Found"), { status: 404 });
    expect(isApiErrorWithStatus(err, 404)).toBe(true);
    expect(isApiErrorWithStatus(err, 500)).toBe(false);
  });

  it("ignores plain errors", () => {
    expect(isApiErrorWithStatus(new Error("boom"), 404)).toBe(false);
    expect(isApiErrorWithStatus("nope", 404)).toBe(false);
  });
});
