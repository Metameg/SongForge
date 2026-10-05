import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

const req = (headers: Record<string, string> = {}) =>
  new Request("http://x/events", { method: "GET", headers });

beforeEach(() => {
  process.env.BACKEND_URL = "http://backend.test";
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GET /events proxy", () => {
  it("forwards the incoming cookie upstream so the stream is identity-scoped", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("event: idle\n\n"));
    vi.stubGlobal("fetch", fetchMock);
    await GET(req({ cookie: "sf=1" }));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend.test/events");
    expect(init.headers).toMatchObject({ cookie: "sf=1" });
  });

  it("still works without a cookie and sends no cookie header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("event: idle\n\n"));
    vi.stubGlobal("fetch", fetchMock);
    const res = await GET(req());
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("cookie");
  });

  it("streams the upstream body through without buffering and forwards the abort signal", async () => {
    const stream = new ReadableStream({ start() {} }); // never closes, like real SSE
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ body: stream, status: 200 } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    const request = req({ cookie: "sf=1" });
    const res = await GET(request);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("cache-control")).toBe("no-cache");
    expect(res.body).not.toBeNull();
    expect(fetchMock.mock.calls[0][1].signal).toBe(request.signal);
  });
});
