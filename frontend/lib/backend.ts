import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

// Node's fetch sticks to the first DNS answer, so pick a random backend replica per request,
// and steer around a replica that just timed out or refused a connection.

export type Resolver = (hostname: string) => Promise<{ address: string; family: number }[]>;

export type BackendFetchOptions = {
  // Bounds the wait for response HEADERS only, so long-lived SSE bodies are never cut off.
  timeoutMs?: number;
  // Extra attempts on another replica after a network failure. Only safe for idempotent requests.
  retries?: number;
};

const CACHE_MS = 5_000;
const DOWN_MS = 15_000;
const DEFAULT_TIMEOUT_MS = 10_000;

const systemResolver: Resolver = (hostname) => lookup(hostname, { all: true });

export function createBackendClient(
  resolve: Resolver = systemResolver,
  now: () => number = Date.now,
  random: () => number = Math.random,
) {
  const cache = new Map<string, { addresses: string[]; expires: number }>();
  const downUntil = new Map<string, number>();

  async function addressesFor(hostname: string): Promise<string[]> {
    const hit = cache.get(hostname);
    if (hit && hit.expires > now()) return hit.addresses;
    let addresses: string[] = [];
    try {
      const all = await resolve(hostname);
      // The backend binds 0.0.0.0 (IPv4 only), so an IPv6 address would be refused
      // whenever an IPv4 one exists.
      const v4 = all.filter((a) => a.family === 4);
      addresses = [...new Set((v4.length > 0 ? v4 : all).map((a) => a.address))];
    } catch {
      addresses = [];
    }
    cache.set(hostname, { addresses, expires: now() + CACHE_MS });
    return addresses;
  }

  async function pick(path: string): Promise<{ url: string; address: string | null }> {
    const base = process.env.BACKEND_URL || "http://localhost:8000";
    const original = `${base}${path}`;
    const url = new URL(original);
    // https needs the real hostname for TLS; IP literals and localhost have nothing to spread.
    if (url.protocol !== "http:" || isIP(url.hostname.replace(/^\[|\]$/g, "")) || url.hostname === "localhost") {
      return { url: original, address: null };
    }
    const all = await addressesFor(url.hostname);
    if (all.length === 0) return { url: original, address: null };
    const t = now();
    const healthy = all.filter((a) => (downUntil.get(a) ?? 0) <= t);
    // If every replica looks down, try them all rather than refusing outright.
    const pool = healthy.length > 0 ? healthy : all;
    const address = pool[Math.floor(random() * pool.length)];
    url.hostname = isIP(address) === 6 ? `[${address}]` : address;
    return { url: url.toString(), address };
  }

  async function backendFetch(
    path: string,
    init: RequestInit = {},
    { timeoutMs = DEFAULT_TIMEOUT_MS, retries = 0 }: BackendFetchOptions = {},
  ): Promise<Response> {
    const caller = init.signal ?? undefined;
    for (let attempt = 0; ; attempt++) {
      const { url, address } = await pick(path);
      const controller = new AbortController();
      if (caller?.aborted) controller.abort(caller.reason);
      else caller?.addEventListener("abort", () => controller.abort(caller.reason), { once: true });
      const timer = setTimeout(() => controller.abort(new Error("backend timeout")), timeoutMs);
      try {
        return await globalThis.fetch(url, { ...init, signal: controller.signal });
      } catch (err) {
        // The client went away: not the replica's fault, and nobody is waiting for a retry.
        if (caller?.aborted) throw err;
        if (address) downUntil.set(address, now() + DOWN_MS);
        if (attempt >= retries) throw err;
      } finally {
        clearTimeout(timer);
      }
    }
  }

  return { pick, backendFetch };
}

export const { backendFetch } = createBackendClient();
