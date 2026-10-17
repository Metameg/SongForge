import { NextResponse } from "next/server";

// Liveness probe used by the docker-compose healthcheck.
export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json({ status: "ok", service: "songforge-frontend" });
}
