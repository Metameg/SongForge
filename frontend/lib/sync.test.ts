/**
 * Pure sync-math unit tests (issue #8, criteria #4, #5; PRD testing decision #4).
 *
 * No mocks — these are plain functions of numbers in, numbers out.
 */

import { describe, expect, it } from "vitest";
import {
  DEADBAND_SECONDS,
  computeSkewMs,
  correctedServerNowMs,
  computeOffsetSeconds,
  decideDrift,
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
