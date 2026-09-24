/**
 * `/quota` fetch + songs-left formatting (issue #15, criterion #5).
 *
 * Mirrors `songforge.web.routes.quota.QuotaResponse` on the backend. `formatSongsLeft`
 * is a pure function (unit-tested directly, PRD testing decision #4); `fetchQuota` reads
 * the same-origin proxy route so the browser never sees the internal backend host.
 */

export interface QuotaResponse {
  /** Songs the current identity can still create today (already floored at 0 server-side). */
  remaining: number;
  /** Whether rate-limit enforcement is on; when false the count is not meaningful. */
  enforced: boolean;
  /** The effective daily cap the `remaining` count is measured against. */
  limit: number;
}

/** Render the songs-left indicator text from a `/quota` response. */
export function formatSongsLeft(quota: QuotaResponse): string {
  if (!quota.enforced) {
    return "Unlimited songs today";
  }
  const remaining = Math.max(quota.remaining, 0);
  const noun = remaining === 1 ? "song" : "songs";
  return `${remaining} ${noun} left today`;
}

/** Fetch the current identity's remaining daily quota via the same-origin proxy. */
export async function fetchQuota(baseUrl: string): Promise<QuotaResponse> {
  const response = await fetch(`${baseUrl}/quota`, { cache: "no-store" });
  return (await response.json()) as QuotaResponse;
}
