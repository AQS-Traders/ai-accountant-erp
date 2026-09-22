import { describe, expect, it } from "vitest";

import { apiErrorMessage } from "./errors";

/**
 * The refusal message is the whole point of the delete rule: a hard delete is
 * refused and the user must be told to deactivate instead. If this helper
 * mangles the body, that instruction never reaches the screen.
 */
describe("apiErrorMessage", () => {
  it("unwraps a FastAPI HTTPException detail", () => {
    const body = JSON.stringify({
      detail:
        "'Desk' is already used by sales invoices, so deleting it would break " +
        "those documents' history. Deactivate it instead.",
    });
    expect(apiErrorMessage(new Error(body))).toBe(
      "'Desk' is already used by sales invoices, so deleting it would break " +
        "those documents' history. Deactivate it instead."
    );
  });

  it("joins FastAPI validation messages", () => {
    const body = JSON.stringify({
      detail: [{ msg: "field required" }, { msg: "not a valid number" }],
    });
    expect(apiErrorMessage(new Error(body))).toBe(
      "field required; not a valid number"
    );
  });

  it("keeps a plain-text error untouched", () => {
    expect(apiErrorMessage(new Error("Failed to fetch"))).toBe("Failed to fetch");
  });

  it("falls back to a friendly line for an empty error", () => {
    expect(apiErrorMessage(new Error("   "))).toBe(
      "Something went wrong. Please try again."
    );
    expect(apiErrorMessage(undefined)).toBe(
      "Something went wrong. Please try again."
    );
  });

  it("never throws on a malformed JSON body", () => {
    expect(apiErrorMessage(new Error("{not json"))).toBe("{not json");
  });
});
