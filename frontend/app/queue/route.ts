/** Proxy for `GET /queue` (issue #38). STUB for TDD red. */

export const dynamic = "force-dynamic";

export async function GET(_request: Request): Promise<Response> {
  return new Response("not implemented", { status: 501 });
}
