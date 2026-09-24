/**
 * Pure songs-left formatter unit tests (issue #15, criterion #5).
 *
 * No mocks — `formatSongsLeft` is a plain function of a `/quota` response in, a display
 * string out. `quota.ts` does not exist yet, so this file is RED at import time.
 */

import { describe, expect, it } from "vitest";
import { formatSongsLeft, type QuotaResponse } from "./quota";

function quota(overrides: Partial<QuotaResponse> = {}): QuotaResponse {
  return { remaining: 2, enforced: true, limit: 2, ...overrides };
}

describe("formatSongsLeft", () => {
  it("pluralizes for more than one remaining", () => {
    expect(formatSongsLeft(quota({ remaining: 2 }))).toBe("2 songs left today");
  });

  it("uses the singular for exactly one remaining", () => {
    expect(formatSongsLeft(quota({ remaining: 1 }))).toBe("1 song left today");
  });

  it("says zero (plural) when the quota is used up", () => {
    expect(formatSongsLeft(quota({ remaining: 0 }))).toBe("0 songs left today");
  });

  it("shows unlimited when enforcement is off, ignoring the count", () => {
    expect(formatSongsLeft(quota({ enforced: false, remaining: 0 }))).toBe(
      "Unlimited songs today",
    );
  });

  it("never renders a negative count", () => {
    expect(formatSongsLeft(quota({ remaining: -3 }))).toBe("0 songs left today");
  });
});
