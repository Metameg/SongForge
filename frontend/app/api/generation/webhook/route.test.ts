/** The public webhook proxy forwards body and query (the auth token) and relays status. */
import { afterEach, describe, expect, it, vi } from "vitest";

const backendFetch = vi.fn();
vi.mock("../../../../lib/backend", () => ({ backendFetch: (...args: unknown[]) => backendFetch(...args) }));

const { POST } = await import("./route");

afterEach(() => backendFetch.mockReset());

function webhook(url: string, body = '{"task_id":"t"}') {
  return new Request(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
}

describe("POST /api/generation/webhook proxy", () => {
  it("forwards the token query and the raw body to the backend webhook", async () => {
    backendFetch.mockResolvedValue(new Response('{"status":"ok"}', { status: 200 }));

    const resp = await POST(webhook("https://jukeboxradioai.com/api/generation/webhook?token=abc"));

    expect(resp.status).toBe(200);
    const [path, init, opts] = backendFetch.mock.calls[0];
    expect(path).toBe("/api/generation/webhook?token=abc");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"task_id":"t"}');
    expect(opts).toEqual({ timeoutMs: 15_000 });
  });

  it("relays a backend rejection unchanged", async () => {
    backendFetch.mockResolvedValue(
      new Response('{"detail":"invalid webhook token"}', { status: 401 }),
    );

    const resp = await POST(webhook("https://jukeboxradioai.com/api/generation/webhook"));

    expect(resp.status).toBe(401);
    expect(backendFetch.mock.calls[0][0]).toBe("/api/generation/webhook");
  });

  it("returns 502 when the backend is unreachable", async () => {
    backendFetch.mockRejectedValue(new Error("ECONNREFUSED"));

    const resp = await POST(webhook("https://jukeboxradioai.com/api/generation/webhook?token=abc"));

    expect(resp.status).toBe(502);
  });
});
