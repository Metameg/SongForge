/**
 * Queue depth + position logic (issue #38): tolerant parse, the "Queue · N waiting" line,
 * the viewer's own position, the `/queue` fetch and the reactive-refetch wiring.
 * No DOM: the EventSource is a fake exposing spy add/removeEventListener.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchQueue,
  formatQueueLine,
  myPosition,
  parseQueue,
  wireQueueRefetch,
  type QueueResponse,
} from "./queue";

afterEach(() => {
  vi.unstubAllGlobals();
});

const q = (depth: number, positions: number[] = []): QueueResponse => ({
  depth,
  positions: positions.map((position, i) => ({ job_id: `j${i}`, position })),
});

describe("parseQueue", () => {
  it("parses a well-formed response", () => {
    const raw = { depth: 5, positions: [{ job_id: "j1", position: 3 }] };
    expect(parseQueue(raw)).toEqual(raw);
  });
  it("defaults to an empty queue for undefined / null / non-objects", () => {
    for (const raw of [undefined, null, "x", 7, []]) {
      expect(parseQueue(raw)).toEqual({ depth: 0, positions: [] });
    }
  });
  it("defaults a missing or non-numeric depth to 0 and a bad positions to []", () => {
    expect(parseQueue({ positions: "nope" })).toEqual({ depth: 0, positions: [] });
    expect(parseQueue({ depth: "9", positions: null })).toEqual({ depth: 0, positions: [] });
  });
});

describe("formatQueueLine", () => {
  it("renders 'Queue · N waiting'", () => {
    expect(formatQueueLine(q(4))).toBe("Queue · 4 waiting");
  });
  it("renders zero as '0 waiting'", () => {
    expect(formatQueueLine(q(0))).toBe("Queue · 0 waiting");
  });
});

describe("myPosition", () => {
  it("is null when the viewer has no queued song", () => {
    expect(myPosition(q(3))).toBeNull();
  });
  it("is the single position when the viewer has one", () => {
    expect(myPosition(q(5, [4]))).toBe(4);
  });
  it("is the smallest position (next to air) when the viewer has several", () => {
    expect(myPosition(q(9, [7, 2, 5]))).toBe(2);
  });
});

describe("fetchQueue", () => {
  it("reads `${baseUrl}/queue` uncached and returns the parsed body", async () => {
    const body = { depth: 2, positions: [{ job_id: "j1", position: 1 }] };
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => body });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchQueue("")).resolves.toEqual(body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/queue");
    expect(init).toMatchObject({ cache: "no-store" });
  });
  it("honours a non-empty base url", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ json: async () => ({ depth: 0, positions: [] }) });
    vi.stubGlobal("fetch", fetchMock);
    await fetchQueue("http://api.test");
    expect(fetchMock.mock.calls[0][0]).toBe("http://api.test/queue");
  });
});

describe("wireQueueRefetch", () => {
  function fakeSource() {
    return { addEventListener: vi.fn(), removeEventListener: vi.fn() };
  }
  const listenerFor = (spy: ReturnType<typeof vi.fn>, name: string) =>
    spy.mock.calls.find((c) => c[0] === name)?.[1] as (() => void) | undefined;

  it("registers song-change, job-progress and job-ready listeners", () => {
    const source = fakeSource();
    wireQueueRefetch(source, vi.fn());
    const names = source.addEventListener.mock.calls.map((c) => c[0]).sort();
    expect(names).toEqual(["job-progress", "job-ready", "song-change"]);
  });
  it("each registered listener calls refetch", () => {
    const source = fakeSource();
    const refetch = vi.fn();
    wireQueueRefetch(source, refetch);
    for (const name of ["song-change", "job-progress", "job-ready"]) {
      listenerFor(source.addEventListener, name)?.();
    }
    expect(refetch).toHaveBeenCalledTimes(3);
  });
  it("does not listen for job-failed or idle (no refetch on those)", () => {
    const source = fakeSource();
    wireQueueRefetch(source, vi.fn());
    const names = source.addEventListener.mock.calls.map((c) => c[0]);
    expect(names.length).toBeGreaterThan(0);
    expect(names).not.toContain("job-failed");
    expect(names).not.toContain("idle");
  });
  it("does not refetch on its own at wire time (initial load is the caller's job)", () => {
    const source = fakeSource();
    const refetch = vi.fn();
    wireQueueRefetch(source, refetch);
    expect(source.addEventListener).toHaveBeenCalled();
    expect(refetch).not.toHaveBeenCalled();
  });
  it("cleanup removes exactly the listeners it added", () => {
    const source = fakeSource();
    const cleanup = wireQueueRefetch(source, vi.fn());
    expect(source.removeEventListener).not.toHaveBeenCalled();
    cleanup();
    expect(source.addEventListener).toHaveBeenCalledTimes(3);
    expect(source.removeEventListener).toHaveBeenCalledTimes(3);
    for (const [name, fn] of source.addEventListener.mock.calls) {
      expect(source.removeEventListener).toHaveBeenCalledWith(name, fn);
    }
  });
});
