import { NextResponse } from "next/server";

/**
 * Runtime proxy for POST /create (issue #36). Mirrors `app/quota/route.ts`: forwards the
 * anonymous identity cookie and raw JSON body upstream, relays `Set-Cookie`, and relays
 * the upstream status UNCHANGED (422/403/429/5xx) so the client can branch on it.
 *
 * Bot check: the backend gate (`HeaderTokenBotCheck`) reads the `X-Bot-Check` header
 * (configurable via `bot_check_header`) and is disabled by default (empty token). The
 * proxy forwards that header when the caller supplies it; it never injects the secret.
 */

export const dynamic = "force-dynamic";

const BOT_CHECK_HEADER = process.env.BOT_CHECK_HEADER || "x-bot-check";

export async function POST(request: Request) {
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  const cookie = request.headers.get("cookie");
  const botCheck = request.headers.get(BOT_CHECK_HEADER);
  const contentType = request.headers.get("content-type") ?? "application/json";
  const body = await request.text();
  try {
    const upstream = await fetch(`${backendUrl}/create`, {
      method: "POST",
      cache: "no-store",
      headers: {
        "content-type": contentType,
        ...(cookie ? { cookie } : {}),
        ...(botCheck ? { [BOT_CHECK_HEADER]: botCheck } : {}),
      },
      body,
    });
    const text = await upstream.text();
    const headers = new Headers({
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    });
    const setCookie = upstream.headers.get("set-cookie");
    if (setCookie) headers.set("set-cookie", setCookie);
    return new NextResponse(text, { status: upstream.status, headers });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}
