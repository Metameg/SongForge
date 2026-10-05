import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "./route";

const BODY = '{"prompt":"p"}';
const req = (extra: Record<string, string> = {}) =>
  new Request("http://x/create", {
    method: "POST",
    headers: { cookie: "sf=1", "content-type": "application/json", ...extra },
    body: BODY,
  });

beforeEach(() => {
  process.env.BACKEND_URL = "http://backend.test";
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("POST /create proxy", () => {
  it("forwards body, cookie and content-type upstream and relays set-cookie", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('{"job_id":"j","state":"QUEUED"}', {
        status: 201,
        headers: { "content-type": "application/json", "set-cookie": "sf=2; Path=/" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const res = await POST(req());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend.test/create");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(BODY);
    expect(init.headers).toMatchObject({ cookie: "sf=1", "content-type": "application/json" });
    expect(res.status).toBe(201);
    expect(res.headers.get("set-cookie")).toContain("sf=2");
    expect(await res.text()).toBe('{"job_id":"j","state":"QUEUED"}');
  });

  it("forwards the bot-check header when supplied", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    await POST(req({ "x-bot-check": "tok" }));
    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({ "x-bot-check": "tok" });
  });

  it.each([422, 403, 429, 500])("relays upstream status %i unchanged", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"x"}', { status })));
    const res = await POST(req());
    expect(res.status).toBe(status);
  });

  it("returns 502 JSON when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    const res = await POST(req());
    expect(res.status).toBe(502);
    expect(await res.json()).toHaveProperty("detail");
  });
});
