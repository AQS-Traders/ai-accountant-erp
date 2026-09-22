/**
 * Turn an API/network failure into the sentence a user should read.
 *
 * The backend answers a refused catalogue action with a FastAPI error body:
 *
 *   HTTP 409  {"detail": "'Desk' is already used by sales invoices, so deleting
 *                        it would break those documents' history. Deactivate it
 *                        instead ..."}
 *
 * `fetchApi` throws `new ApiError(status, <raw body text>)`, so without this
 * the page would show a wall of JSON instead of that instruction. Parsing lives
 * here (not in the page) so it is unit-testable and shared by every caller.
 */
export function apiErrorMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  const trimmed = raw.trim();
  if (!trimmed) return "Something went wrong. Please try again.";

  const looksJson = trimmed.startsWith("{") || trimmed.startsWith("[");
  if (looksJson) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      const detail = (parsed as { detail?: unknown } | null)?.detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
      if (Array.isArray(detail)) {
        // FastAPI validation errors: [{"loc":[...], "msg":"..."}]
        const parts = detail
          .map((entry) =>
            typeof entry === "string"
              ? entry
              : String((entry as { msg?: unknown } | null)?.msg ?? "")
          )
          .filter((text) => text.trim().length > 0);
        if (parts.length) return parts.join("; ");
      }
    } catch {
      // Not JSON after all — fall through and show the original text.
    }
  }
  return trimmed;
}
