import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

const req = (headers: Record<string, string> = {}) =>
  new Request("http://x/queue", { method: "GET", headers });

beforeEach(() => {
  process.env.BACKEND_URL = "http://backend.test";
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const okBody = JSON.stringify({ depth: 3, positions: [{ job_id: "j1", position: 2 }] });
const okResponse = () =>
  new Response(okBody, { status: 200, headers: { "content-type": "application/json" } });

describe("GET /queue proxy", () => {
  it("forwards the incoming cookie upstream to ${BACKEND_URL}/queue", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchMock);
    await GET(req({ cookie: "sf_uid=abc" }));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend.test/queue");
    expect(init.headers).toMatchObject({ cookie: "sf_uid=abc" });
  });

  it("works without a cookie and sends no cookie header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchMock);
    const res = await GET(req());
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("cookie");
  });

  it("relays the upstream status and body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse()));
    const res = await GET(req());
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(JSON.parse(okBody));
  });

  it("relays a non-200 upstream status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 502 })));
    const res = await GET(req());
    expect(res.status).toBe(502);
  });

  it("returns a neutral empty queue with 503 when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    const res = await GET(req());
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ depth: 0, positions: [] });
  });
});
