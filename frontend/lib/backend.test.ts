import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBackendClient, type Resolver } from "./backend";

const v4 = (address: string) => ({ address, family: 4 });
const v6 = (address: string) => ({ address, family: 6 });
const three: Resolver = async () => [v4("10.0.0.1"), v4("10.0.0.2"), v4("10.0.0.3")];

beforeEach(() => {
  process.env.BACKEND_URL = "http://web.railway.internal:8000";
});
afterEach(() => {
  delete process.env.BACKEND_URL;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("pick", () => {
  it("spreads successive requests across every replica, not just the first", async () => {
    const rolls = [0, 0.4, 0.9];
    const { pick } = createBackendClient(three, () => 0, () => rolls.shift()!);
    const urls = [(await pick("/q")).url, (await pick("/q")).url, (await pick("/q")).url];
    expect(urls).toEqual([
      "http://10.0.0.1:8000/q",
      "http://10.0.0.2:8000/q",
      "http://10.0.0.3:8000/q",
    ]);
  });

  it("prefers IPv4 because the backend only listens on 0.0.0.0", async () => {
    const resolve: Resolver = async () => [v6("fd12::1"), v4("10.0.0.7"), v6("fd12::2")];
    const { pick } = createBackendClient(resolve, () => 0, () => 0.99);
    expect((await pick("/quota")).url).toBe("http://10.0.0.7:8000/quota");
  });

  it("brackets an IPv6 address when no IPv4 one exists", async () => {
    const { pick } = createBackendClient(async () => [v6("fd12::1")], () => 0, () => 0);
    expect((await pick("/quota")).url).toBe("http://[fd12::1]:8000/quota");
  });

  it("falls back to the configured URL when the lookup fails", async () => {
    const resolve: Resolver = async () => {
      throw new Error("ENOTFOUND");
    };
    const { pick } = createBackendClient(resolve, () => 0, () => 0);
    expect(await pick("/create")).toEqual({
      url: "http://web.railway.internal:8000/create",
      address: null,
    });
  });

  it("caches the address list briefly instead of resolving on every request", async () => {
    const resolve = vi.fn<Resolver>(async () => [v4("10.0.0.1")]);
    let clock = 0;
    const { pick } = createBackendClient(resolve, () => clock, () => 0);
    await pick("/a");
    await pick("/b");
    expect(resolve).toHaveBeenCalledTimes(1);
    clock = 5_001;
    await pick("/c");
    expect(resolve).toHaveBeenCalledTimes(2);
  });

  it("leaves localhost, IP literals and https untouched", async () => {
    const resolve = vi.fn<Resolver>(async () => [v4("10.0.0.1")]);
    const { pick } = createBackendClient(resolve, () => 0, () => 0);
    for (const base of ["http://localhost:8000", "http://127.0.0.1:8000", "https://api.example"]) {
      process.env.BACKEND_URL = base;
      expect((await pick("/now-playing")).url).toBe(`${base}/now-playing`);
    }
    expect(resolve).not.toHaveBeenCalled();
  });
});

describe("backendFetch", () => {
  it("steers around a replica that just failed", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValue(new Response("ok"));
    vi.stubGlobal("fetch", fetchMock);
    // Every roll would pick 10.0.0.1 if it were still considered healthy.
    const { backendFetch } = createBackendClient(three, () => 0, () => 0);

    await expect(backendFetch("/quota")).rejects.toThrow("fetch failed");
    await backendFetch("/quota");

    expect(fetchMock.mock.calls[0][0]).toBe("http://10.0.0.1:8000/quota");
    expect(fetchMock.mock.calls[1][0]).toBe("http://10.0.0.2:8000/quota");
  });

  it("brings a replica back once its down window has passed", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValue(new Response("ok"));
    vi.stubGlobal("fetch", fetchMock);
    let clock = 0;
    const { backendFetch } = createBackendClient(three, () => clock, () => 0);

    await expect(backendFetch("/quota")).rejects.toThrow();
    clock = 15_001;
    await backendFetch("/quota");

    expect(fetchMock.mock.calls[1][0]).toBe("http://10.0.0.1:8000/quota");
  });

  it("still tries every replica when all of them look down", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    vi.stubGlobal("fetch", fetchMock);
    const { backendFetch } = createBackendClient(async () => [v4("10.0.0.1")], () => 0, () => 0);

    await expect(backendFetch("/quota")).rejects.toThrow();
    await expect(backendFetch("/quota")).rejects.toThrow();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("http://10.0.0.1:8000/quota");
  });

  it("retries once on another replica when retries are allowed", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValue(new Response("ok"));
    vi.stubGlobal("fetch", fetchMock);
    const { backendFetch } = createBackendClient(three, () => 0, () => 0);

    const res = await backendFetch("/queue", {}, { retries: 1 });

    expect(await res.text()).toBe("ok");
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      "http://10.0.0.1:8000/queue",
      "http://10.0.0.2:8000/queue",
    ]);
  });

  it("does not retry by default, so a POST is never sent twice", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    vi.stubGlobal("fetch", fetchMock);
    const { backendFetch } = createBackendClient(three, () => 0, () => 0);

    await expect(backendFetch("/create", { method: "POST" })).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("aborts a replica that never sends headers, then marks it down", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal!.addEventListener("abort", () => reject(init.signal!.reason));
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { backendFetch } = createBackendClient(three, () => 0, () => 0);

    const pending = backendFetch("/now-playing", {}, { timeoutMs: 1_000 });
    const assertion = expect(pending).rejects.toThrow("backend timeout");
    await vi.advanceTimersByTimeAsync(1_000);
    await assertion;

    fetchMock.mockResolvedValueOnce(new Response("ok"));
    await backendFetch("/now-playing");
    expect(fetchMock.mock.calls[1][0]).toBe("http://10.0.0.2:8000/now-playing");
  });

  it("neither retries nor blames the replica when the client disconnects", async () => {
    const disconnect = new AbortController();
    // Like real fetch: reject at once on an already-aborted signal, else on abort.
    const fetchMock = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          if (init.signal!.aborted) return reject(new Error("aborted"));
          init.signal!.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { backendFetch } = createBackendClient(three, () => 0, () => 0);

    const pending = backendFetch("/events", { signal: disconnect.signal }, { retries: 1 });
    disconnect.abort();
    await expect(pending).rejects.toThrow("aborted");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockResolvedValueOnce(new Response("ok"));
    await backendFetch("/events");
    expect(fetchMock.mock.calls[1][0]).toBe("http://10.0.0.1:8000/events");
  });
});
