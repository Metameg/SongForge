import { NextResponse } from "next/server";
import { backendFetch } from "../../../../lib/backend";

/**
 * Public entry point for the generation API's completion webhook. `web` has no public
 * domain (it also serves /metrics), so MusicGPT reaches it through this proxy. The query
 * string is forwarded untouched because it carries the `token` the backend checks against
 * WEBHOOK_SECRET; this route does no auth of its own and must never log the URL.
 */

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const { search } = new URL(request.url);
  const body = await request.text();
  try {
    // No retry: the backend is idempotent per task_id, but MusicGPT retries on its own.
    const upstream = await backendFetch(
      `/api/generation/webhook${search}`,
      {
        method: "POST",
        cache: "no-store",
        headers: { "content-type": request.headers.get("content-type") ?? "application/json" },
        body,
      },
      { timeoutMs: 15_000 },
    );
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}
