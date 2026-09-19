/**
 * Server-side runtime streaming proxy for `/events` (issue #10, criteria #1, #4).
 *
 * Same rationale as `app/now-playing/route.ts` (a Route Handler, not a build-time
 * rewrite, so it can read the RUNTIME `BACKEND_URL`) with one substantive difference:
 * this forwards the upstream response's raw `ReadableStream` straight through. It must
 * NOT `await upstream.text()` the way `now-playing/route.ts` does — that would buffer
 * the *entire* SSE stream into memory and never resolve, since an SSE response never
 * ends on its own. No idle-JSON catch fallback either (that's not valid SSE framing);
 * a fetch failure just lets the browser's native `EventSource` `onerror` + auto-retry
 * take over.
 *
 * `signal: request.signal` forwards the browser's disconnect: when the client's
 * `EventSource` closes, Next aborts this handler's request, which aborts the upstream
 * fetch, which closes the socket to the backend so its `/events` generator sees the
 * disconnect and runs its `finally` (unregister + gauge decrement) promptly — rather
 * than the backend connection lingering until the next ~30s heartbeat write fails.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  const upstream = await fetch(`${backendUrl}/events`, {
    cache: "no-store",
    signal: request.signal,
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
    },
  });
}
