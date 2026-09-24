import { NextResponse } from "next/server";

/**
 * Server-side runtime proxy for the songs-left read (issue #15, criterion #5).
 *
 * Mirrors `app/now-playing/route.ts`: the player is a client component whose fetch runs
 * in the browser, so it must hit a same-origin path; this handler forwards to the backend
 * using the RUNTIME `BACKEND_URL`. Unlike `/now-playing`, quota is identity-scoped, so
 * this proxy forwards the request's `Cookie` header upstream and relays any `Set-Cookie`
 * back (a first-time visitor gets their signed identity cookie minted here).
 *
 * A Route Handler (not a `next.config.js` rewrite) is used because rewrite destinations
 * are baked in at build time and cannot read a runtime env var.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  const cookie = request.headers.get("cookie");
  try {
    const upstream = await fetch(`${backendUrl}/quota`, {
      cache: "no-store",
      headers: cookie ? { cookie } : {},
    });
    const body = await upstream.text();
    const headers = new Headers({
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    });
    const setCookie = upstream.headers.get("set-cookie");
    if (setCookie) {
      headers.set("set-cookie", setCookie);
    }
    return new NextResponse(body, { status: upstream.status, headers });
  } catch {
    // Backend momentarily unreachable — report enforcement off so the indicator shows a
    // neutral "unlimited" rather than throwing in the browser; the player keeps working.
    return NextResponse.json(
      { remaining: 0, enforced: false, limit: 0 },
      { status: 503 },
    );
  }
}
