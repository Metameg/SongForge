import { NextResponse } from "next/server";

/**
 * Same-origin proxy for the download redirect (issue #39). Forwards to backend
 * `GET /download/{id}` WITHOUT following the redirect and relays the 302 + `Location`
 * (a short-lived presigned object-store URL) so audio bytes flow browser <-> storage
 * directly, never through this server. Non-redirect upstream statuses are relayed so the
 * client can show "Download unavailable".
 */

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ songId: string }> },
) {
  const { songId } = await params;
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  const cookie = request.headers.get("cookie");
  try {
    const upstream = await fetch(`${backendUrl}/download/${encodeURIComponent(songId)}`, {
      cache: "no-store",
      redirect: "manual",
      headers: cookie ? { cookie } : {},
    });
    const location = upstream.headers.get("location");
    if (upstream.status >= 300 && upstream.status < 400 && location) {
      return new NextResponse(null, { status: 302, headers: { location, "cache-control": "no-store" },
      });
    }
    return new NextResponse(null, { status: upstream.ok ? 502 : upstream.status });
  } catch {
    return new NextResponse(null, { status: 503 });
  }
}
