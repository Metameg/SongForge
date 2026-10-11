/**
 * Queue depth + position logic (issue #38): tolerant parse, the "Queue · N waiting" line,
 * the viewer's own position, the `/queue` fetch and the reactive-refetch wiring.
 * No DOM: the EventSource is a fake exposing spy add/removeEventListener.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchQueue,
  formatQueueLine,
  makeLatestGuard,
  myPosition,
  parseDepthFromSongChange,
  parseQueue,
  positionLabel,
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

describe("makeLatestGuard", () => {
  it("treats only the most recently begun token as current", () => {
    const g = makeLatestGuard();
    const first = g.begin();
    const second = g.begin();
    // Out-of-order completion: the stale (first) response must be rejected, the newest kept.
    expect(g.isCurrent(first)).toBe(false);
    expect(g.isCurrent(second)).toBe(true);
  });

  it("issues strictly increasing tokens and keeps the latest current after more begins", () => {
    const g = makeLatestGuard();
    const a = g.begin();
    const b = g.begin();
    const c = g.begin();
    expect(b).toBeGreaterThan(a);
    expect(c).toBeGreaterThan(b);
    expect(g.isCurrent(a)).toBe(false);
    expect(g.isCurrent(b)).toBe(false);
    expect(g.isCurrent(c)).toBe(true);
  });
});

describe("positionLabel", () => {
  it("celebrates the next-up spot for position 1", () => {
    expect(positionLabel(1)).toBe("Your song is next up!");
  });

  it("renders a plain in-line label for later positions", () => {
    expect(positionLabel(2)).toBe("You're #2 in line");
    expect(positionLabel(7)).toBe("You're #7 in line");
  });

  it("shows the position OUT OF the total when a depth is supplied", () => {
    expect(positionLabel(42, 2000)).toBe("You're #42 of 2000 in line");
    // Position 1 stays celebratory even with a total.
    expect(positionLabel(1, 2000)).toBe("Your song is next up!");
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
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchQueue("")).resolves.toEqual(body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/queue");
    expect(init).toMatchObject({ cache: "no-store" });
  });
  it("rejects on a non-OK response so callers keep the last known queue", async () => {
    const json = vi.fn().mockResolvedValue({ depth: 0, positions: [] });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, json }));
    await expect(fetchQueue("")).rejects.toThrow();
  });
  it("honours a non-empty base url", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ depth: 0, positions: [] }) });
    vi.stubGlobal("fetch", fetchMock);
    await fetchQueue("http://api.test");
    expect(fetchMock.mock.calls[0][0]).toBe("http://api.test/queue");
  });
});

describe("parseDepthFromSongChange", () => {
  it("extracts a non-negative integer queue_depth from the frame data", () => {
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: 42 }))).toBe(42);
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: 0 }))).toBe(0);
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: 7.9 }))).toBe(7);
  });
  it("is null when queue_depth is absent, null, negative, NaN or not a number", () => {
    expect(parseDepthFromSongChange(JSON.stringify({ song_id: "x" }))).toBeNull();
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: null }))).toBeNull();
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: -1 }))).toBeNull();
    expect(parseDepthFromSongChange(JSON.stringify({ queue_depth: "9" }))).toBeNull();
  });
  it("is null for undefined or unparseable data rather than throwing", () => {
    expect(parseDepthFromSongChange(undefined)).toBeNull();
    expect(parseDepthFromSongChange("not json")).toBeNull();
  });
});

describe("wireQueueRefetch (depth-in-SSE)", () => {
  function fakeSource() {
    return { addEventListener: vi.fn(), removeEventListener: vi.fn() };
  }
  type Ev = { data?: string };
  const listenerFor = (spy: ReturnType<typeof vi.fn>, name: string) =>
    spy.mock.calls.find((c) => c[0] === name)?.[1] as ((e: Ev) => void) | undefined;
  const handlers = (over: Partial<Parameters<typeof wireQueueRefetch>[1]> = {}) => ({
    onDepth: vi.fn(),
    refetchPositions: vi.fn(),
    hasPosition: () => false,
    ...over,
  });

  it("registers song-change, queue-depth, job-progress and job-ready listeners", () => {
    const source = fakeSource();
    wireQueueRefetch(source, handlers());
    const names = source.addEventListener.mock.calls.map((c) => c[0]).sort();
    expect(names).toEqual(["job-progress", "job-ready", "queue-depth", "song-change"]);
  });
  it("does not listen for job-failed or idle", () => {
    const source = fakeSource();
    wireQueueRefetch(source, handlers());
    const names = source.addEventListener.mock.calls.map((c) => c[0]);
    expect(names).not.toContain("job-failed");
    expect(names).not.toContain("idle");
  });
  it("reports the waiting-queue depth from each song-change frame", () => {
    const source = fakeSource();
    const h = handlers();
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "song-change")?.({
      data: JSON.stringify({ queue_depth: 5 }),
    });
    expect(h.onDepth).toHaveBeenCalledWith(5);
  });
  it("does NOT refetch /queue on song-change for a pure listener (no position)", () => {
    const source = fakeSource();
    const h = handlers({ hasPosition: () => false });
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "song-change")?.({
      data: JSON.stringify({ queue_depth: 5 }),
    });
    expect(h.refetchPositions).not.toHaveBeenCalled();
  });
  it("DOES refetch /queue on song-change when the viewer holds a position", () => {
    const source = fakeSource();
    const h = handlers({ hasPosition: () => true });
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "song-change")?.({
      data: JSON.stringify({ queue_depth: 5 }),
    });
    expect(h.refetchPositions).toHaveBeenCalledTimes(1);
  });
  it("reports the waiting-queue depth from a queue-depth frame", () => {
    const source = fakeSource();
    const h = handlers();
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "queue-depth")?.({
      data: JSON.stringify({ queue_depth: 8 }),
    });
    expect(h.onDepth).toHaveBeenCalledWith(8);
  });
  it("does NOT refetch /queue on queue-depth, even when the viewer holds a position", () => {
    const source = fakeSource();
    const h = handlers({ hasPosition: () => true });
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "queue-depth")?.({
      data: JSON.stringify({ queue_depth: 8 }),
    });
    expect(h.refetchPositions).not.toHaveBeenCalled();
  });
  it("always refetches /queue on the viewer's own job-progress and job-ready", () => {
    const source = fakeSource();
    const h = handlers({ hasPosition: () => false });
    wireQueueRefetch(source, h);
    listenerFor(source.addEventListener, "job-progress")?.({});
    listenerFor(source.addEventListener, "job-ready")?.({});
    expect(h.refetchPositions).toHaveBeenCalledTimes(2);
  });
  it("does not act on its own at wire time (initial load is the caller's job)", () => {
    const source = fakeSource();
    const h = handlers();
    wireQueueRefetch(source, h);
    expect(source.addEventListener).toHaveBeenCalled();
    expect(h.onDepth).not.toHaveBeenCalled();
    expect(h.refetchPositions).not.toHaveBeenCalled();
  });
  it("cleanup removes exactly the listeners it added", () => {
    const source = fakeSource();
    const cleanup = wireQueueRefetch(source, handlers());
    expect(source.removeEventListener).not.toHaveBeenCalled();
    cleanup();
    expect(source.addEventListener).toHaveBeenCalledTimes(4);
    expect(source.removeEventListener).toHaveBeenCalledTimes(4);
    for (const [name, fn] of source.addEventListener.mock.calls) {
      expect(source.removeEventListener).toHaveBeenCalledWith(name, fn);
    }
  });
  it("calling cleanup twice does not throw", () => {
    const source = fakeSource();
    const cleanup = wireQueueRefetch(source, handlers());
    expect(() => {
      cleanup();
      cleanup();
    }).not.toThrow();
  });
});

describe("parseQueue edge inputs", () => {
  it("clamps a negative, NaN or Infinity depth to 0", () => {
    for (const depth of [-3, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(parseQueue({ depth, positions: [] }).depth).toBe(0);
    }
  });
  it("floors a fractional depth", () => {
    expect(parseQueue({ depth: 2.9, positions: [] }).depth).toBe(2);
  });
  it("drops malformed position entries, keeping the valid ones", () => {
    const parsed = parseQueue({
      depth: 4,
      positions: [
        null,
        "x",
        7,
        { job_id: "no-pos" },
        { position: 2 },
        { job_id: 5, position: 2 },
        { job_id: "str-pos", position: "2" },
        { job_id: "zero", position: 0 },
        { job_id: "neg", position: -1 },
        { job_id: "frac", position: 1.5 },
        { job_id: "ok", position: 3 },
      ],
    });
    expect(parsed).toEqual({ depth: 4, positions: [{ job_id: "ok", position: 3 }] });
  });
  it("strips extra fields from the response and from position entries", () => {
    const parsed = parseQueue({
      depth: 1,
      extra: true,
      positions: [{ job_id: "j", position: 1, secret: "x" }],
    });
    expect(parsed).toEqual({ depth: 1, positions: [{ job_id: "j", position: 1 }] });
  });
  it("treats a boolean as an empty queue", () => {
    expect(parseQueue(true)).toEqual({ depth: 0, positions: [] });
  });
});

describe("formatQueueLine / myPosition / fetchQueue edges", () => {
  it("renders 1 and a large N", () => {
    expect(formatQueueLine(q(1))).toBe("Queue · 1 waiting");
    expect(formatQueueLine(q(1234))).toBe("Queue · 1234 waiting");
  });
  it("myPosition does not mutate or reorder the positions", () => {
    const queue = q(9, [5, 1, 3]);
    expect(myPosition(queue)).toBe(1);
    expect(queue.positions.map((p) => p.position)).toEqual([5, 1, 3]);
  });
  it("fetchQueue degrades a malformed body to an empty queue", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => "oops" }));
    await expect(fetchQueue("")).resolves.toEqual({ depth: 0, positions: [] });
  });
});
