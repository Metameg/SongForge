import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

// Node's fetch sticks to the first DNS answer, so pick a random backend replica per request.

export type Resolver = (hostname: string) => Promise<{ address: string; family: number }[]>;

const CACHE_MS = 5_000;

const systemResolver: Resolver = (hostname) => lookup(hostname, { all: true });

export function createBackendUrl(
  resolve: Resolver = systemResolver,
  now: () => number = Date.now,
  random: () => number = Math.random,
) {
  const cache = new Map<string, { addresses: string[]; expires: number }>();

  async function addressesFor(hostname: string): Promise<string[]> {
    const hit = cache.get(hostname);
    if (hit && hit.expires > now()) return hit.addresses;
    let addresses: string[] = [];
    try {
      const all = await resolve(hostname);
      // The backend binds 0.0.0.0 (IPv4 only), so an IPv6 address would be refused
      // whenever an IPv4 one exists.
      const v4 = all.filter((a) => a.family === 4);
      addresses = (v4.length > 0 ? v4 : all).map((a) => a.address);
    } catch {
      addresses = [];
    }
    cache.set(hostname, { addresses, expires: now() + CACHE_MS });
    return addresses;
  }

  return async function backendUrl(path: string): Promise<string> {
    const base = process.env.BACKEND_URL || "http://localhost:8000";
    const original = `${base}${path}`;
    const url = new URL(original);
    // https needs the real hostname for TLS; IP literals and localhost have nothing to spread.
    if (url.protocol !== "http:" || isIP(url.hostname.replace(/^\[|\]$/g, "")) || url.hostname === "localhost") {
      return original;
    }
    const addresses = await addressesFor(url.hostname);
    if (addresses.length === 0) return original;
    const pick = addresses[Math.floor(random() * addresses.length)];
    url.hostname = isIP(pick) === 6 ? `[${pick}]` : pick;
    return url.toString();
  };
}

export const backendUrl = createBackendUrl();
