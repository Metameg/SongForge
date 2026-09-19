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

/**
 * Issue #9 (design D6): continuous drift correction + a periodic skew-refresh
 * heartbeat, closing criterion #1's gap (`decideDrift` above was wired for Play/
 * song-change only). Kept as pure functions here so `Player.tsx` only wires DOM
 * events/timers to them, per the PRD testing decision to keep sync math unit-testable.
 */

/** How often the player re-fetches `/now-playing` to refresh clock skew, in ms, so a
 * missed boundary or drifting skew self-corrects (PRD stories #9, #10). */
export const HEARTBEAT_INTERVAL_MS = 30_000;

/**
 * Whether enough wall-clock time has passed since the last `/now-playing` fetch to
 * trigger a heartbeat refetch.
 */
export function shouldRefetchOnHeartbeat(
  elapsedSinceLastFetchMs: number,
  intervalMs: number = HEARTBEAT_INTERVAL_MS,
): boolean {
  return elapsedSinceLastFetchMs >= intervalMs;
}

/**
 * Seconds into the current song "now" on the shared server timeline, given the
 * client's own clock and its measured skew — composes {@link correctedServerNowMs}
 * with {@link computeOffsetSeconds} so callers (the `timeupdate` handler) don't need
 * to inline both on every tick.
 */
export function computeExpectedOffsetSeconds(
  startedAtMs: number,
  clientNowMs: number,
  skewMs: number,
): number {
  return computeOffsetSeconds(correctedServerNowMs(clientNowMs, skewMs), startedAtMs);
}

/**
 * Issue #10 (SSE push + gapless transitions): two new pure decisions `Player.tsx` needs
 * now that updates arrive by push instead of poll.
 */

/**
 * Whether an incoming `/events` pointer is a genuine song change (re-anchor the
 * `<audio>` element) rather than a heartbeat re-broadcast of the same song.
 *
 * Every `/events` frame — the ~30s heartbeat re-emit included — is `song-change`
 * shaped; only a `playback_id` change means the station actually advanced. The very
 * first pointer (no previous `playback_id` yet, `previous === null`) always counts as a
 * change, since there is nothing to compare against.
 */
export function shouldReanchorOnPointer(
  previousPlaybackId: string | null,
  nextPlaybackId: string,
): boolean {
  return previousPlaybackId === null || previousPlaybackId !== nextPlaybackId;
}

/** Which of the two gapless `<audio>` buffers ("a"/"b") a preload slot names. */
export type PreloadSlot = "a" | "b";

/**
 * The other buffer slot — a plain A/B toggle for the gapless dual-`<audio>` swap
 * (criterion #3). Not a next-song lookahead: the SSE push itself carries the real
 * next-song data at the boundary, this just tracks which DOM element is "current" vs.
 * "preloading".
 */
export function nextPreloadSlot(current: PreloadSlot): PreloadSlot {
  return current === "a" ? "b" : "a";
}
