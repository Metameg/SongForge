/** Typed per-user SSE events (issue #37): `job-progress`, `job-ready`, `job-failed`. */

export interface JobProgressEvent {
  job_id: string;
  state: string;
  eta: number | null;
}
export interface JobReadyEvent {
  job_id: string;
  song_id: string;
  title: string;
}
export interface JobFailedEvent {
  job_id: string;
}

export type JobEvent =
  | ({ type: "progress" } & JobProgressEvent)
  | ({ type: "ready" } & JobReadyEvent)
  | ({ type: "failed" } & JobFailedEvent);

export type JobEventName = "job-progress" | "job-ready" | "job-failed";

/** Parse one SSE frame; `null` for malformed JSON, missing fields or an unknown name. */
export function parseJobEvent(name: string, data: string): JobEvent | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  if (typeof o.job_id !== "string") return null;
  const job_id = o.job_id;
  switch (name) {
    case "job-progress":
      if (typeof o.state !== "string") return null;
      return {
        type: "progress",
        job_id,
        state: o.state,
        eta: typeof o.eta === "number" ? o.eta : null,
      };
    case "job-ready":
      if (typeof o.song_id !== "string" || typeof o.title !== "string") return null;
      return { type: "ready", job_id, song_id: o.song_id, title: o.title };
    case "job-failed":
      return { type: "failed", job_id };
    default:
      return null;
  }
}
