import type { JobEvent } from "./events";

/** Creator-only view of the in-flight generation job (issue #37). */
export interface GenerationProgress {
  status: "idle" | "generating" | "finished" | "failed" | "timedout";
  jobId: string | null;
  eta: number | null;
}

export const INITIAL_PROGRESS: GenerationProgress = { status: "idle", jobId: null, eta: null };

export type ProgressAction =
  | { type: "started"; jobId: string }
  | { type: "timeout"; jobId: string }
  | JobEvent;

/**
 * Client-side safety net for the bar never un-sticking (no hanging "Generating your
 * song"): the per-user event channel is fire-and-forget and bounded (see
 * `backend/.../radio/user_events.py`, `_QUEUE_MAXSIZE` -- its own docstring allows
 * dropping one `job-failed`/`job-ready` push), and a brief SSE disconnect at the
 * wrong moment misses the frame entirely with no replay. Without a timeout the bar
 * would spin forever. `RadioApp` arms a timer for this long after entering
 * `generating` (re-armed whenever a `job-progress` updates the eta); if no terminal
 * frame arrives it dispatches a `timeout` action -> `timedout`. No polling.
 *
 * Deliberately generous and eta-scaled: a real-MusicGPT generation can legitimately
 * take minutes, and firing early would strand a job that is still genuinely working.
 * `timedout` makes NO claim about the outcome (unlike `failed`, which asserts a
 * refund) -- the song still surfaces in the station if it completes, and a reload
 * resets the view.
 */
export const PROGRESS_TIMEOUT_FLOOR_MS = 180_000;

export function progressTimeoutMs(eta: number | null): number {
  if (eta == null || eta <= 0) return PROGRESS_TIMEOUT_FLOOR_MS;
  return Math.max(PROGRESS_TIMEOUT_FLOOR_MS, eta * 2000 + 60_000);
}

/** Events for any job other than the tracked one leave the state untouched. */
export function reduceProgress(s: GenerationProgress, a: ProgressAction): GenerationProgress {
  if (a.type === "started") return { status: "generating", jobId: a.jobId, eta: null };
  if (a.type === "timeout") {
    // Only strand the bar when we're still waiting on the SAME job it was armed for:
    // a terminal frame that landed first (finished/failed) already moved us off
    // `generating`, and a re-tracked newer job must not be timed out by an older timer.
    return s.status === "generating" && a.jobId === s.jobId
      ? { ...s, status: "timedout", eta: null }
      : s;
  }
  if (s.status !== "generating" || a.job_id !== s.jobId) return s;
  switch (a.type) {
    case "progress":
      return { ...s, eta: a.eta };
    case "ready":
      return { ...s, status: "finished", eta: null };
    case "failed":
      return { ...s, status: "failed", eta: null };
  }
}
