/** Edge coverage for `lib/progress.ts` reducer (issue #37). */
import { describe, expect, it } from "vitest";
import { INITIAL_PROGRESS, reduceProgress, type GenerationProgress } from "./progress";

const generating = (jobId = "j1", eta: number | null = null): GenerationProgress => ({
  status: "generating", jobId, eta,
});

describe("reduceProgress edge cases", () => {
  it("a job-progress arriving before any started action is ignored", () => {
    const s = reduceProgress(INITIAL_PROGRESS, { type: "progress", job_id: "j1", state: "WAITING_FOR_WEBHOOK", eta: 30 });
    expect(s).toEqual(INITIAL_PROGRESS);
  });

  it("a job-ready arriving before any started action is ignored", () => {
    const s = reduceProgress(INITIAL_PROGRESS, { type: "ready", job_id: "j1", song_id: "s1", title: "T" });
    expect(s).toEqual(INITIAL_PROGRESS);
  });

  it("a progress event with a null eta keeps generating with a null eta", () => {
    const s = reduceProgress(generating("j1", 40), { type: "progress", job_id: "j1", state: "INGEST_PENDING", eta: null });
    expect(s).toEqual({ status: "generating", jobId: "j1", eta: null });
  });

  it("a second started re-tracks the new job id and resets eta", () => {
    const s = reduceProgress(generating("j1", 40), { type: "started", jobId: "j2" });
    expect(s).toEqual({ status: "generating", jobId: "j2", eta: null });
  });

  it("a second started after a finished job resets status to generating", () => {
    const finished: GenerationProgress = { status: "finished", jobId: "j1", eta: null };
    expect(reduceProgress(finished, { type: "started", jobId: "j2" }).status).toBe("generating");
  });

  it("a job-ready for the OLD job after re-tracking is ignored", () => {
    const retracked = reduceProgress(generating("j1"), { type: "started", jobId: "j2" });
    const s = reduceProgress(retracked, { type: "ready", job_id: "j1", song_id: "s1", title: "T" });
    expect(s).toEqual(retracked);
  });

  it("a job-failed for the OLD job after re-tracking is ignored", () => {
    const retracked = reduceProgress(generating("j1"), { type: "started", jobId: "j2" });
    const s = reduceProgress(retracked, { type: "failed", job_id: "j1" });
    expect(s).toEqual(retracked);
  });

  it("a job-progress for the OLD job after re-tracking does not change eta", () => {
    const retracked = reduceProgress(generating("j1"), { type: "started", jobId: "j2" });
    const s = reduceProgress(retracked, { type: "progress", job_id: "j1", state: "X", eta: 99 });
    expect(s.eta).toBeNull();
  });

  it("a late progress event after finished leaves the state finished", () => {
    const finished: GenerationProgress = { status: "finished", jobId: "j1", eta: null };
    const s = reduceProgress(finished, { type: "progress", job_id: "j1", state: "X", eta: 5 });
    expect(s).toEqual(finished);
  });

  it("a job-failed after finished does not flip the status to failed", () => {
    const finished: GenerationProgress = { status: "finished", jobId: "j1", eta: null };
    expect(reduceProgress(finished, { type: "failed", job_id: "j1" }).status).toBe("finished");
  });
});
