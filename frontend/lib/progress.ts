import type { JobEvent } from "./events";

/** Creator-only view of the in-flight generation job (issue #37). */
export interface GenerationProgress {
  status: "idle" | "generating" | "finished" | "failed";
  jobId: string | null;
  eta: number | null;
}

export const INITIAL_PROGRESS: GenerationProgress = { status: "idle", jobId: null, eta: null };

export type ProgressAction = { type: "started"; jobId: string } | JobEvent;

/** Events for any job other than the tracked one leave the state untouched. */
export function reduceProgress(s: GenerationProgress, a: ProgressAction): GenerationProgress {
  if (a.type === "started") return { status: "generating", jobId: a.jobId, eta: null };
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
