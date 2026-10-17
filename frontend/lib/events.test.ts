import { describe, expect, it } from "vitest";
import { parseJobEvent } from "./events";

describe("parseJobEvent", () => {
  it("parses a job-progress frame with an eta", () => {
    const data = JSON.stringify({ job_id: "j1", state: "WAITING_FOR_WEBHOOK", eta: 90 });
    expect(parseJobEvent("job-progress", data)).toEqual({
      type: "progress", job_id: "j1", state: "WAITING_FOR_WEBHOOK", eta: 90,
    });
  });
  it("parses a job-progress frame with a null eta", () => {
    const data = JSON.stringify({ job_id: "j1", state: "INGEST_PENDING", eta: null });
    expect(parseJobEvent("job-progress", data)).toEqual({
      type: "progress", job_id: "j1", state: "INGEST_PENDING", eta: null,
    });
  });
  it("parses a job-ready frame", () => {
    const data = JSON.stringify({ job_id: "j1", song_id: "s9", title: "My Song" });
    expect(parseJobEvent("job-ready", data)).toEqual({
      type: "ready", job_id: "j1", song_id: "s9", title: "My Song",
    });
  });
  it("parses a job-failed frame", () => {
    expect(parseJobEvent("job-failed", JSON.stringify({ job_id: "j1" }))).toEqual({
      type: "failed", job_id: "j1",
    });
  });
  it("returns null for malformed JSON", () => {
    expect(parseJobEvent("job-ready", "{not json")).toBeNull();
  });
  it("returns null for an unrecognised event name", () => {
    expect(parseJobEvent("song-change", JSON.stringify({ job_id: "j1" }))).toBeNull();
  });
});
