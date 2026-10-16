import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBackendUrl, type Resolver } from "./backend";

const v4 = (address: string) => ({ address, family: 4 });
const v6 = (address: string) => ({ address, family: 6 });

beforeEach(() => {
  process.env.BACKEND_URL = "http://web.railway.internal:8000";
});
afterEach(() => {
  delete process.env.BACKEND_URL;
});

describe("backendUrl", () => {
  it("pins each request to one resolved replica address", async () => {
    const resolve: Resolver = async () => [v4("10.0.0.1"), v4("10.0.0.2"), v4("10.0.0.3")];
    const backendUrl = createBackendUrl(resolve, () => 0, () => 0.5);
    expect(await backendUrl("/events")).toBe("http://10.0.0.2:8000/events");
  });

  it("spreads successive requests across every replica, not just the first", async () => {
    const resolve: Resolver = async () => [v4("10.0.0.1"), v4("10.0.0.2"), v4("10.0.0.3")];
    const rolls = [0, 0.4, 0.9];
    const backendUrl = createBackendUrl(resolve, () => 0, () => rolls.shift()!);
    const urls = [await backendUrl("/q"), await backendUrl("/q"), await backendUrl("/q")];
    expect(urls).toEqual([
      "http://10.0.0.1:8000/q",
      "http://10.0.0.2:8000/q",
      "http://10.0.0.3:8000/q",
    ]);
  });

  it("prefers IPv4 because the backend only listens on 0.0.0.0", async () => {
    const resolve: Resolver = async () => [v6("fd12::1"), v4("10.0.0.7"), v6("fd12::2")];
    const backendUrl = createBackendUrl(resolve, () => 0, () => 0.99);
    expect(await backendUrl("/quota")).toBe("http://10.0.0.7:8000/quota");
  });

  it("brackets an IPv6 address when no IPv4 one exists", async () => {
    const resolve: Resolver = async () => [v6("fd12::1")];
    const backendUrl = createBackendUrl(resolve, () => 0, () => 0);
    expect(await backendUrl("/quota")).toBe("http://[fd12::1]:8000/quota");
  });

  it("falls back to the configured URL when the lookup fails", async () => {
    const resolve: Resolver = async () => {
      throw new Error("ENOTFOUND");
    };
    const backendUrl = createBackendUrl(resolve, () => 0, () => 0);
    expect(await backendUrl("/create")).toBe("http://web.railway.internal:8000/create");
  });

  it("caches the address list briefly instead of resolving on every request", async () => {
    const resolve = vi.fn<Resolver>(async () => [v4("10.0.0.1")]);
    let clock = 0;
    const backendUrl = createBackendUrl(resolve, () => clock, () => 0);
    await backendUrl("/a");
    await backendUrl("/b");
    expect(resolve).toHaveBeenCalledTimes(1);
    clock = 5_001;
    await backendUrl("/c");
    expect(resolve).toHaveBeenCalledTimes(2);
  });

  it("leaves localhost, IP literals and https untouched", async () => {
    const resolve = vi.fn<Resolver>(async () => [v4("10.0.0.1")]);
    const backendUrl = createBackendUrl(resolve, () => 0, () => 0);
    for (const base of ["http://localhost:8000", "http://127.0.0.1:8000", "https://api.example"]) {
      process.env.BACKEND_URL = base;
      expect(await backendUrl("/now-playing")).toBe(`${base}/now-playing`);
    }
    expect(resolve).not.toHaveBeenCalled();
  });
});
