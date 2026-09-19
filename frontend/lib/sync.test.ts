/**
 * Pure sync-math unit tests (issue #8, criteria #4, #5; PRD testing decision #4).
 *
 * No mocks — these are plain functions of numbers in, numbers out.
 */

import { describe, expect, it } from "vitest";
import {
  DEADBAND_SECONDS,
  HEARTBEAT_INTERVAL_MS,
  computeSkewMs,
  correctedServerNowMs,
  computeOffsetSeconds,
  computeExpectedOffsetSeconds,
  decideDrift,
  shouldRefetchOnHeartbeat,
} from "./sync";

describe("computeSkewMs", () => {
  it("is positive when the client clock reads ahead of the server", () => {
    // server_time = 1_000_000ms, client's own clock read 1_000_300ms on receipt.
    expect(computeSkewMs(1_000_000, 1_000_300)).toBe(300);
  });

  it("is negative when the client clock reads behind the server", () => {
    expect(computeSkewMs(1_000_500, 1_000_000)).toBe(-500);
  });

  it("is zero for perfectly synced clocks", () => {
    expect(computeSkewMs(1_000_000, 1_000_000)).toBe(0);
  });
});

describe("correctedServerNowMs", () => {
  it("subtracts a positive skew (client ahead) back down to server time", () => {
    expect(correctedServerNowMs(2_000_300, 300)).toBe(2_000_000);
  });

  it("adds back a negative skew (client behind) up to server time", () => {
    expect(correctedServerNowMs(2_000_000, -500)).toBe(2_000_500);
  });
});

describe("computeOffsetSeconds", () => {
  it("is the elapsed seconds since the song started on the server timeline", () => {
    const startedAtMs = 1_700_000_000_000;
    const serverNowMs = startedAtMs + 45_000;
    expect(computeOffsetSeconds(serverNowMs, startedAtMs)).toBe(45);
  });

  it("is negative before the song's start (caller clamps to 0 before seeking)", () => {
    const startedAtMs = 1_700_000_000_000;
    const serverNowMs = startedAtMs - 2_000;
    expect(computeOffsetSeconds(serverNowMs, startedAtMs)).toBe(-2);
  });
});

describe("decideDrift", () => {
  it("defaults the threshold to the 1s deadband", () => {
    expect(DEADBAND_SECONDS).toBe(1);
  });

  it("is 'none' just under the 1s deadband, either direction", () => {
    expect(decideDrift(0.999)).toBe("none");
    expect(decideDrift(-0.999)).toBe("none");
  });

  it("is 'seek' exactly at the 1s threshold (>= 1s is a hard seek)", () => {
    expect(decideDrift(1)).toBe("seek");
    expect(decideDrift(-1)).toBe("seek");
  });

  it("is 'seek' well past the threshold", () => {
    expect(decideDrift(3.5)).toBe("seek");
  });

  it("honors a custom threshold", () => {
    expect(decideDrift(1.4, 2)).toBe("none");
    expect(decideDrift(2, 2)).toBe("seek");
  });
});

/**
 * Issue #9 (design D6): the continuous-drift + heartbeat gap. `decideDrift` above is
 * already wired for Play/song-change; these cover the two NEW pure decisions Player.tsx
 * needs to close criterion #1's gap -- a ~30s heartbeat re-fetch trigger, and a single
 * call point combining clock-skew correction with the started_at offset for the
 * `timeupdate` handler (composes the existing `correctedServerNowMs` +
 * `computeOffsetSeconds`, so `Player.tsx` needn't inline both on every tick).
 *
 * Neither symbol exists yet in `./sync` -- this is the RED phase for #9's client gap.
 */
describe("HEARTBEAT_INTERVAL_MS", () => {
  it("defaults to 30 seconds (PRD stories #9, #10)", () => {
    expect(HEARTBEAT_INTERVAL_MS).toBe(30_000);
  });
});

describe("shouldRefetchOnHeartbeat", () => {
  it("is false before the heartbeat interval has elapsed", () => {
    expect(shouldRefetchOnHeartbeat(29_999)).toBe(false);
  });

  it("is true once the heartbeat interval has elapsed", () => {
    expect(shouldRefetchOnHeartbeat(30_000)).toBe(true);
  });

  it("is true well past the interval (e.g. after a backgrounded tab)", () => {
    expect(shouldRefetchOnHeartbeat(120_000)).toBe(true);
  });

  it("honors a custom interval", () => {
    expect(shouldRefetchOnHeartbeat(4_999, 5_000)).toBe(false);
    expect(shouldRefetchOnHeartbeat(5_000, 5_000)).toBe(true);
  });
});

describe("computeExpectedOffsetSeconds", () => {
  it("composes clock-skew correction with the started_at offset", () => {
    const startedAtMs = 1_700_000_000_000;
    const clientNowMs = startedAtMs + 45_300; // 45.3s of client wall-clock elapsed
    const skewMs = 300; // client clock reads 300ms ahead of the server
    // correctedServerNowMs = clientNowMs - skewMs = startedAtMs + 45_000
    // offset = 45_000ms / 1000 = 45s
    expect(computeExpectedOffsetSeconds(startedAtMs, clientNowMs, skewMs)).toBe(45);
  });

  it("matches manual correctedServerNowMs + computeOffsetSeconds composition", () => {
    const startedAtMs = 1_700_000_000_000;
    const clientNowMs = startedAtMs + 10_000;
    const skewMs = -200;
    const expected = computeOffsetSeconds(
      correctedServerNowMs(clientNowMs, skewMs),
      startedAtMs,
    );
    expect(computeExpectedOffsetSeconds(startedAtMs, clientNowMs, skewMs)).toBe(expected);
  });
});

/**
 * Issue #10 (SSE push + gapless transitions): two NEW pure decisions `Player.tsx`
 * needs now that updates arrive by push instead of poll.
 *
 * `shouldReanchorOnPointer` — every `/events` frame (including the ~30s heartbeat
 * re-broadcast of the SAME pointer, and the pub/sub relay of a genuinely new one) is
 * `song-change`-shaped. The player must only re-anchor/reseek `<audio>` when the
 * incoming pointer is actually a new song (`playback_id` changed); re-anchoring on
 * every frame would audibly restart playback on each heartbeat.
 *
 * `nextPreloadSlot` — gapless playback bookkeeping (criterion #3): which of the two
 * `<audio>` buffers ("a"/"b") becomes the active one after a swap. A plain A/B toggle,
 * not a next-song lookahead field (out of scope per `.orchestrator/CONTEXT.md`) — the
 * SSE push itself carries the real next-song data at the boundary, this just tracks
 * which DOM element is "current" vs. "preloading".
 *
 * Neither symbol exists yet in `./sync` — this is the RED phase for issue #10's
 * frontend gap. Imported dynamically INSIDE each test (rather than a static
 * top-of-file import) so a missing export fails only these tests, not the whole
 * file/collection of the pre-existing tests above — mirrors the backend's
 * local-import convention for RED-phase symbols (see the NOTE block in
 * `backend/tests/test_now_playing.py`).
 */
describe("shouldReanchorOnPointer", () => {
  it("is true when the incoming pointer is a different playback_id (a real song change)", async () => {
    const { shouldReanchorOnPointer } = await import("./sync");
    expect(shouldReanchorOnPointer("pb-1", "pb-2")).toBe(true);
  });

  it("is false when the incoming pointer repeats the same playback_id (a heartbeat, not a change)", async () => {
    const { shouldReanchorOnPointer } = await import("./sync");
    expect(shouldReanchorOnPointer("pb-1", "pb-1")).toBe(false);
  });

  it("is true on the very first pointer (no previous playback_id yet)", async () => {
    const { shouldReanchorOnPointer } = await import("./sync");
    expect(shouldReanchorOnPointer(null, "pb-1")).toBe(true);
  });
});

describe("nextPreloadSlot", () => {
  it("toggles from the 'a' buffer to the 'b' buffer", async () => {
    const { nextPreloadSlot } = await import("./sync");
    expect(nextPreloadSlot("a")).toBe("b");
  });

  it("toggles from the 'b' buffer back to the 'a' buffer", async () => {
    const { nextPreloadSlot } = await import("./sync");
    expect(nextPreloadSlot("b")).toBe("a");
  });
});
