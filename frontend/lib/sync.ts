/**
 * Pure client-side playback sync math (issue #8, criteria #4, #5).
 *
 * No DOM, no fetch, no timers — just the arithmetic that turns a `/now-playing`
 * response plus the client's own clock into "where in the song we should be right
 * now." Kept pure so it is directly unit-testable (vitest) per the PRD testing
 * decision; the player component wires these into the `<audio>` element and the
 * `/now-playing` poll/refetch schedule.
 *
 * All timestamps are epoch milliseconds (`Date.now()` / `Date.parse(...)`), all
 * durations/offsets are in seconds unless a name says `Ms`.
 *
 * Issue #8, phase 3 (green) implementation.
 */

/** Drift magnitude below which the player leaves playback alone (no correction). */
export const DEADBAND_SECONDS = 1;

export type DriftAction = "none" | "seek";

/**
 * Clock skew between server and client, in milliseconds.
 *
 * `skew = clientReceiptMs − serverTimeMs`. Positive means the client's clock reads
 * ahead of the server's.
 *
 * @param serverTimeMs - `server_time` from `/now-playing`, as epoch ms.
 * @param clientReceiptMs - the client's own clock (`Date.now()`) at the moment that
 *   response was received.
 */
export function computeSkewMs(serverTimeMs: number, clientReceiptMs: number): number {
  return clientReceiptMs - serverTimeMs;
}

/**
 * The server's current time, per the client's clock corrected for `skewMs`.
 *
 * `server_now = clientNowMs − skewMs`.
 */
export function correctedServerNowMs(clientNowMs: number, skewMs: number): number {
  return clientNowMs - skewMs;
}

/**
 * Seconds into the current song "now" on the shared server timeline.
 *
 * `offset = (serverNowMs − startedAtMs) / 1000`. May be negative if the song has not
 * started yet on the client's corrected clock (caller should clamp to 0 before seeking).
 */
export function computeOffsetSeconds(serverNowMs: number, startedAtMs: number): number {
  return (serverNowMs - startedAtMs) / 1000;
}

/**
 * Whether accumulated drift (in seconds, either sign) warrants a hard seek.
 *
 * Deadband: `|driftSeconds| < thresholdSeconds` -> "none" (leave playback alone).
 * Otherwise (`|driftSeconds| >= thresholdSeconds`) -> "seek" (correct now).
 * Threshold defaults to {@link DEADBAND_SECONDS} (1s), per the PRD's stated boundary.
 */
export function decideDrift(
  driftSeconds: number,
  thresholdSeconds: number = DEADBAND_SECONDS,
): DriftAction {
  return Math.abs(driftSeconds) >= thresholdSeconds ? "seek" : "none";
}
