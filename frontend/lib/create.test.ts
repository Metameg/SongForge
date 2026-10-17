/**
 * Create-flow logic tests (issue #36): plain-language error mapping, the composer's
 * disabled-state predicate, and the POST helper. Pure logic / fetch-mock only (no React
 * rendering -- the repo has no jsdom/RTL).
 *
 * RED phase: `./create` does not exist yet, so it is imported dynamically inside each test
 * so a missing module fails only that test, not collection.
 *
 * Contract under test (`lib/create.ts`):
 *   mapCreateError(status: number | null): string        // null = network failure
 *   canCreate(opts: { prompt: string; inFlight: boolean; generationActive: boolean;
 *                     remaining: number | null }): boolean
 *   submitCreate(req: { prompt: string; lyrics: string | null }, baseUrl?: string)
 *     : Promise<{ ok: true; jobId: string; state: string }
 *             | { ok: false; status: number | null; message: string }>   // never throws
 */

import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mapCreateError", () => {
  it("maps 422 to a message about the prompt/lyrics being empty or too long", async () => {
    const { mapCreateError } = await import("./create");
    expect(mapCreateError(422)).toMatch(/prompt|lyrics/i);
  });

  it("maps 403 to a message about the verification / bot check", async () => {
    const { mapCreateError } = await import("./create");
    expect(mapCreateError(403)).toMatch(/verif|human|bot/i);
  });

  it("maps 429 to a message about running out of songs / the daily limit", async () => {
    const { mapCreateError } = await import("./create");
    expect(mapCreateError(429)).toMatch(/song|limit|quota|today|tomorrow/i);
  });

  it("maps every 5xx to the same try-again-later message", async () => {
    const { mapCreateError } = await import("./create");
    const msg = mapCreateError(500);
    expect(msg).toMatch(/try again|wrong|unavailable/i);
    expect(mapCreateError(502)).toBe(msg);
    expect(mapCreateError(503)).toBe(msg);
  });

  it("maps a network failure (null) to a connectivity message", async () => {
    const { mapCreateError } = await import("./create");
    expect(mapCreateError(null)).toMatch(/connect|network|internet|offline/i);
  });

  it("gives each failure class its own message and never leaks a raw status code", async () => {
    const { mapCreateError } = await import("./create");
    const messages = [422, 403, 429, 500, null].map((s) => mapCreateError(s));
    expect(new Set(messages).size).toBe(messages.length);
    for (const m of messages) {
      expect(m.length).toBeGreaterThan(0);
      expect(m).not.toMatch(/\b(422|403|429|500)\b/);
    }
  });

  it("falls back to a non-empty generic message for an unmapped status", async () => {
    const { mapCreateError } = await import("./create");
    expect(mapCreateError(418).length).toBeGreaterThan(0);
  });
});

describe("canCreate", () => {
  const base = { prompt: "a song about rain", inFlight: false, generationActive: false, remaining: 3 };

  it("is true when idle, prompted, and songs remain", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate(base)).toBe(true);
  });

  it("is false while a create request is in flight", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate({ ...base, inFlight: true })).toBe(false);
  });

  it("is false while a generation is already active", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate({ ...base, generationActive: true })).toBe(false);
  });

  it("is false when no songs remain", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate({ ...base, remaining: 0 })).toBe(false);
  });

  it("is false for an empty or whitespace-only prompt", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate({ ...base, prompt: "" })).toBe(false);
    expect(canCreate({ ...base, prompt: "   \n\t " })).toBe(false);
  });

  it("is true when the quota has not loaded yet (remaining null) and no other gate applies", async () => {
    const { canCreate } = await import("./create");
    expect(canCreate({ ...base, remaining: null })).toBe(true);
  });
});

describe("submitCreate", () => {
  it("POSTs JSON {prompt, lyrics} to /create and resolves ok with the job id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ job_id: "job-1", state: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { submitCreate } = await import("./create");

    const result = await submitCreate({ prompt: "a song about rain", lyrics: null });

    expect(result).toEqual({ ok: true, jobId: "job-1", state: "QUEUED" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/create");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "content-type": "application/json" });
    expect(JSON.parse(init.body)).toEqual({ prompt: "a song about rain", lyrics: null });
  });

  it("honours an explicit base URL and sends provided lyrics", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ job_id: "job-2", state: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { submitCreate } = await import("./create");

    await submitCreate({ prompt: "p", lyrics: "la la" }, "http://backend");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend/create");
    expect(JSON.parse(init.body)).toEqual({ prompt: "p", lyrics: "la la" });
  });

  it.each([422, 403, 429, 500, 503])(
    "surfaces the mapped plain-language error for HTTP %i",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({ detail: "raw" }) }),
      );
      const { submitCreate, mapCreateError } = await import("./create");

      const result = await submitCreate({ prompt: "p", lyrics: null });

      expect(result).toEqual({ ok: false, status, message: mapCreateError(status) });
    },
  );

  it("surfaces the network message (status null) when fetch throws, without rethrowing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const { submitCreate, mapCreateError } = await import("./create");

    const result = await submitCreate({ prompt: "p", lyrics: null });

    expect(result).toEqual({ ok: false, status: null, message: mapCreateError(null) });
  });
});
