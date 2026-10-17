import { NextResponse } from "next/server";
import { backendFetch } from "../../lib/backend";

/**
 * Server-side runtime proxy for the queue read (issue #38). Mirrors `app/quota/route.ts`:
 * forwards the request `Cookie` upstream so an existing identity's positions resolve. The
 * backend `GET /queue` only reads identity (never Set-Cookie), so nothing is relayed back.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const cookie = request.headers.get("cookie");
  try {
    const upstream = await backendFetch(
      "/queue",
      { cache: "no-store", headers: cookie ? { cookie } : {} },
      { retries: 1 },
    );
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    // Backend unreachable: neutral empty queue so the UI never throws.
    return NextResponse.json({ depth: 0, positions: [] }, { status: 503 });
  }
}
