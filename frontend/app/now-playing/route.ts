import { NextResponse } from "next/server";

/**
 * Server-side runtime proxy for the radio pointer read (issue #8).
 *
 * The player is a client component, so its fetch runs in the browser — it must hit a
 * same-origin path (no CORS, no leaking the internal backend hostname). This handler
 * forwards that request to the backend using the RUNTIME `BACKEND_URL`
 * (`http://web:8000` in docker-compose, `http://localhost:8000` for `npm run dev`).
 *
 * A Route Handler is used rather than a `next.config.js` rewrite because rewrite
 * destinations are evaluated at BUILD time and baked into the routes manifest — they
 * cannot read a runtime env var, so a standalone production image would proxy to
 * whatever host was resolved during `next build` (inside the image build, that's the
 * `localhost` fallback), which is unreachable at runtime.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  try {
    const upstream = await fetch(`${backendUrl}/now-playing`, { cache: "no-store" });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    // Backend momentarily unreachable — report idle so the player shows "quiet" and its
    // own poll loop keeps retrying, rather than throwing in the browser.
    return NextResponse.json({ status: "idle" }, { status: 503 });
  }
}
