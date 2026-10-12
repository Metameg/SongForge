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
 * Client-side backstop so the bar can never hang on "Generating" forever: the per-user
 * event channel is fire-and-forget and bounded (see `backend/.../radio/user_events.py`,
 * `_QUEUE_MAXSIZE` -- its docstring allows dropping one push), and a brief SSE
 * disconnect misses the frame entirely with no replay. `RadioApp` arms a single timer
 * this long after entering `generating` (an ABSOLUTE deadline from submit -- NOT reset
 * by `job-progress`, mirroring the backend's `created_at`-based deadline); if no
 * terminal frame arrives it dispatches `timeout` -> `timedout`. No polling.
 *
 * This is deliberately COUPLED to the backend's absolute hard deadline
 * (`Settings.job_max_lifetime_seconds`, 300s): the watchdog fails any still-running
 * job past that deadline (regardless of stage), refunds it if it was charged, and
 * pushes `job-failed`. This timeout must stay >= that deadline + one
 * `watchdog_poll_interval_seconds` (30s) so the backend has ALREADY failed+refunded
 * the job by the time the bar gives up -- which is what makes the "if you were
 * charged, your credits have been refunded" copy accurate rather than a guess.
 * 300s deadline + 30s worst-case watchdog tick + 30s grace = 360s. If you change
 * `job_max_lifetime_seconds`, change this too.
 */
export const PROGRESS_TIMEOUT_MS = 360_000;

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
