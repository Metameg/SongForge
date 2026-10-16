/**
 * Server-side runtime streaming proxy for `/events` (issue #10, criteria #1, #4).
 *
 * Same rationale as `app/now-playing/route.ts` (a Route Handler, not a build-time
 * rewrite, so it can read the RUNTIME `BACKEND_URL`) with one substantive difference:
 * this forwards the upstream response's raw `ReadableStream` straight through. It must
 * NOT `await upstream.text()` the way `now-playing/route.ts` does — that would buffer
 * the *entire* SSE stream into memory and never resolve, since an SSE response never
 * ends on its own. On failure it returns an empty SSE stream with a `retry:` hint rather
 * than an error status: `EventSource` permanently gives up on a non-200 response, but
 * reconnects after a stream that simply ends.
 *
 * `signal: request.signal` forwards the browser's disconnect: when the client's
 * `EventSource` closes, Next aborts this handler's request, which aborts the upstream
 * fetch, which closes the socket to the backend so its `/events` generator sees the
 * disconnect and runs its `finally` (unregister + gauge decrement) promptly — rather
 * than the backend connection lingering until the next ~30s heartbeat write fails.
 */

import { backendUrl } from "../../lib/backend";

export const dynamic = "force-dynamic";

const SSE_HEADERS = {
  "content-type": "text/event-stream",
  "cache-control": "no-cache",
};

// Jittered so an overloaded backend isn't hit by every client reconnecting at once.
function retryLaterResponse(): Response {
  const retryMs = 2000 + Math.floor(Math.random() * 3000);
  return new Response(`retry: ${retryMs}\n\n`, { status: 200, headers: SSE_HEADERS });
}

export async function GET(request: Request) {
  // The backend only READS identity on /events (never mints), so the cookie must be
  // forwarded or the stream is anonymous and the creator never gets their per-user events.
  const cookie = request.headers.get("cookie");
  let upstream: Response;
  try {
    upstream = await fetch(await backendUrl("/events"), {
      cache: "no-store",
      headers: cookie ? { cookie } : {},
      signal: request.signal,
    });
  } catch {
    return retryLaterResponse();
  }
  if (upstream.status !== 200) {
    await upstream.body?.cancel().catch(() => {});
    return retryLaterResponse();
  }
  return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
}
