import { describe, expect, it } from "vitest";
import { INITIAL_PROGRESS, PROGRESS_TIMEOUT_MS, reduceProgress } from "./progress";

const started = (jobId = "j1") => ({ type: "started" as const, jobId });
const progress = (eta: number | null, job_id = "j1") =>
  ({ type: "progress" as const, job_id, state: "WAITING_FOR_WEBHOOK", eta });
const ready = (job_id = "j1") =>
  ({ type: "ready" as const, job_id, song_id: "s1", title: "T" });
const failed = (job_id = "j1") => ({ type: "failed" as const, job_id });

describe("reduceProgress", () => {
  it("starts idle with no eta", () => {
    expect(INITIAL_PROGRESS).toMatchObject({ status: "idle", eta: null });
  });
  it("a started job is generating with no eta yet", () => {
    expect(reduceProgress(INITIAL_PROGRESS, started())).toMatchObject({
      status: "generating", eta: null,
    });
  });
  it("job-progress keeps generating and shows the eta", () => {
    const s = reduceProgress(reduceProgress(INITIAL_PROGRESS, started()), progress(90));
    expect(s).toMatchObject({ status: "generating", eta: 90 });
  });
  it("job-progress with a null eta stays generating without an eta", () => {
    const s = reduceProgress(reduceProgress(INITIAL_PROGRESS, started()), progress(null));
    expect(s).toMatchObject({ status: "generating", eta: null });
  });
  it("job-ready finishes the tracked job", () => {
    const s = reduceProgress(reduceProgress(INITIAL_PROGRESS, started()), ready());
    expect(s.status).toBe("finished");
  });
  it("job-failed fails the tracked job", () => {
    const s = reduceProgress(reduceProgress(INITIAL_PROGRESS, started()), failed());
    expect(s.status).toBe("failed");
  });
  it("ignores a ready for a different job id", () => {
    const g = reduceProgress(INITIAL_PROGRESS, started("j1"));
    expect(reduceProgress(g, ready("other"))).toEqual(g);
  });
  it("ignores a failed for a different job id", () => {
    const g = reduceProgress(INITIAL_PROGRESS, started("j1"));
    expect(reduceProgress(g, failed("other"))).toEqual(g);
  });
  it("ignores a progress for a different job id", () => {
    const g = reduceProgress(INITIAL_PROGRESS, started("j1"));
    expect(reduceProgress(g, progress(5, "other"))).toEqual(g);
  });
  it("starting a new job after a finished one resets to generating", () => {
    let s = reduceProgress(INITIAL_PROGRESS, started("j1"));
    s = reduceProgress(s, ready("j1"));
    s = reduceProgress(s, started("j2"));
    expect(s).toMatchObject({ status: "generating", eta: null });
  });

  const timeout = (jobId = "j1") => ({ type: "timeout" as const, jobId });

  it("a timeout strands the tracked generating job as timedout", () => {
    const s = reduceProgress(reduceProgress(INITIAL_PROGRESS, started()), timeout());
    expect(s).toMatchObject({ status: "timedout", eta: null });
  });
  it("a timeout for a different job id is ignored", () => {
    const g = reduceProgress(INITIAL_PROGRESS, started("j1"));
    expect(reduceProgress(g, timeout("other"))).toEqual(g);
  });
  it("a timeout after the job already finished is ignored", () => {
    let s = reduceProgress(INITIAL_PROGRESS, started());
    s = reduceProgress(s, ready());
    expect(reduceProgress(s, timeout()).status).toBe("finished");
  });
  it("a timeout after the job already failed is ignored", () => {
    let s = reduceProgress(INITIAL_PROGRESS, started());
    s = reduceProgress(s, failed());
    expect(reduceProgress(s, timeout()).status).toBe("failed");
  });
  it("a terminal frame after a timeout does not revive the tracked job", () => {
    let s = reduceProgress(INITIAL_PROGRESS, started());
    s = reduceProgress(s, timeout());
    expect(reduceProgress(s, ready()).status).toBe("timedout");
  });
});

describe("PROGRESS_TIMEOUT_MS", () => {
  it("stays at or beyond the backend hard deadline + one watchdog tick", () => {
    // Backend `job_max_lifetime_seconds` (300s) + `watchdog_poll_interval_seconds`
    // (30s): the bar must give up only AFTER the backend has guaranteed-failed and
    // refunded the job, so the "if charged, refunded" copy is accurate. Keep these in
    // sync if the backend deadline changes.
    expect(PROGRESS_TIMEOUT_MS).toBeGreaterThanOrEqual((300 + 30) * 1000);
  });
});
