/** Edge coverage for `lib/events.ts` `parseJobEvent` (issue #37). */
import { describe, expect, it } from "vitest";
import { parseJobEvent } from "./events";

describe("parseJobEvent edge cases", () => {
  it("an unknown event name returns null even with a valid payload", () => {
    expect(parseJobEvent("job-exploded", JSON.stringify({ job_id: "j1" }))).toBeNull();
  });

  it("a valid name with malformed JSON returns null", () => {
    expect(parseJobEvent("job-progress", '{"job_id": ')).toBeNull();
  });

  it("an empty data string returns null", () => {
    expect(parseJobEvent("job-failed", "")).toBeNull();
  });

  it.each(["null", "42", '"j1"'])("a non-object JSON payload (%s) returns null", (data) => {
    expect(parseJobEvent("job-failed", data)).toBeNull();
  });

  it("a frame missing job_id returns null", () => {
    expect(parseJobEvent("job-progress", JSON.stringify({ state: "X", eta: 1 }))).toBeNull();
  });

  it("a non-string job_id returns null", () => {
    expect(parseJobEvent("job-failed", JSON.stringify({ job_id: 7 }))).toBeNull();
  });

  it("a job-progress frame missing state returns null", () => {
    expect(parseJobEvent("job-progress", JSON.stringify({ job_id: "j1", eta: 3 }))).toBeNull();
  });

  it("a job-progress frame without eta parses with a null eta", () => {
    const e = parseJobEvent("job-progress", JSON.stringify({ job_id: "j1", state: "X" }));
    expect(e).toEqual({ type: "progress", job_id: "j1", state: "X", eta: null });
  });

  it("a job-progress frame with a non-numeric eta parses with a null eta", () => {
    const e = parseJobEvent("job-progress", JSON.stringify({ job_id: "j1", state: "X", eta: "soon" }));
    expect(e).toMatchObject({ type: "progress", eta: null });
  });

  it("a job-ready frame missing song_id returns null", () => {
    expect(parseJobEvent("job-ready", JSON.stringify({ job_id: "j1", title: "T" }))).toBeNull();
  });

  it("a job-ready frame missing title returns null", () => {
    expect(parseJobEvent("job-ready", JSON.stringify({ job_id: "j1", song_id: "s1" }))).toBeNull();
  });

  it("a job-ready frame with null song_id returns null", () => {
    expect(parseJobEvent("job-ready", JSON.stringify({ job_id: "j1", song_id: null, title: "T" }))).toBeNull();
  });

  it("extra unknown fields on a job-failed frame are dropped", () => {
    const e = parseJobEvent("job-failed", JSON.stringify({ job_id: "j1", user_id: "u" }));
    expect(e).toEqual({ type: "failed", job_id: "j1" });
  });
});
