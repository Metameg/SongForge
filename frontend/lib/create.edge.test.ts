/** Edge / error-path coverage for `lib/create.ts` (issue #36 Review Focus #1, #3, #6). */
import { afterEach, describe, expect, it, vi } from "vitest";
import { canCreate, mapCreateError, submitCreate } from "./create";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("canCreate edge cases", () => {
  const base = { inFlight: false, generationActive: false, remaining: 3 };

  it("blocks unicode/NBSP-only prompts but allows a prompt with inner whitespace", () => {
    expect(canCreate({ ...base, prompt: "   " })).toBe(false);
    expect(canCreate({ ...base, prompt: "  rain  on  glass " })).toBe(true);
  });

  it("remaining exactly 0 blocks, 1 allows, null (not loaded / unenforced) does not block", () => {
    const b = { prompt: "p", inFlight: false, generationActive: false };
    expect(canCreate({ ...b, remaining: 0 })).toBe(false);
    expect(canCreate({ ...b, remaining: 1 })).toBe(true);
    expect(canCreate({ ...b, remaining: null })).toBe(true);
  });

  it("treats a negative remaining as exhausted", () => {
    expect(canCreate({ prompt: "p", inFlight: false, generationActive: false, remaining: -1 })).toBe(false);
  });
});

describe("mapCreateError edge cases", () => {
  it.each([400, 401, 404, 409, 418])(
    "maps unexpected 4xx status %i to a generic message distinct from the 5xx and 422 ones",
    (status) => {
      const msg = mapCreateError(status);
      expect(msg.length).toBeGreaterThan(0);
      expect(msg).not.toMatch(/\d{3}/);
      expect(msg).not.toBe(mapCreateError(500));
      expect(msg).not.toBe(mapCreateError(422));
    },
  );

  it.each([500, 503, 599])("maps 5xx status %i to the server-error message", (status) => {
    expect(mapCreateError(status)).toBe(mapCreateError(500));
  });
});

describe("submitCreate edge cases", () => {
  it("keeps the mapped message when an error response has a non-JSON body (proxy 502 page)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: async () => {
          throw new SyntaxError("Unexpected token <");
        },
      }),
    );

    const result = await submitCreate({ prompt: "p", lyrics: null });

    expect(result).toEqual({ ok: false, status: 502, message: mapCreateError(502) });
  });

  it("does not throw when a 200 response body is not valid JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError("Unexpected end of JSON input");
        },
      }),
    );

    const result = await submitCreate({ prompt: "p", lyrics: null });

    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ message: expect.stringMatching(/\S/) });
  });

  it("sends whitespace-padded input through untouched (trimming is the server's concern)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ job_id: "j", state: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await submitCreate({ prompt: "  p  ", lyrics: "" });

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ prompt: "  p  ", lyrics: "" });
  });
});
