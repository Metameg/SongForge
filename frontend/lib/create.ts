/**
 * Create-flow logic (issue #36): plain-language error mapping, the composer's
 * disabled-state predicate, and the POST helper. Pure / fetch-only so Vitest can cover it
 * without rendering React. Mirrors `songforge.web.routes.create` (CreateRequest /
 * CreateResponse; 403 bot check, 422 validation, 429 quota).
 */

export type CreateResult =
  | { ok: true; jobId: string; state: string }
  | { ok: false; status: number | null; message: string };

/** Map an HTTP status (null = network failure) to a plain-language message. */
export function mapCreateError(status: number | null): string {
  if (status === null) {
    return "Can't reach the station. Check your internet connection and try again.";
  }
  if (status === 422) {
    return "Your prompt or lyrics are empty or too long. Shorten them and try again.";
  }
  if (status === 403) {
    return "We couldn't verify you're human. Please refresh the page and try again.";
  }
  if (status === 429) {
    return "You're out of songs for today. Come back tomorrow for more.";
  }
  if (status >= 500) {
    return "Something went wrong on our end. Please try again in a moment.";
  }
  return "That didn't work. Please try again.";
}

/** Whether the Create button should be enabled. `remaining: null` = quota not loaded yet. */
export function canCreate(opts: {
  prompt: string;
  inFlight: boolean;
  generationActive: boolean;
  remaining: number | null;
}): boolean {
  if (opts.inFlight || opts.generationActive) return false;
  if (opts.prompt.trim() === "") return false;
  if (opts.remaining !== null && opts.remaining <= 0) return false;
  return true;
}

/** POST a create request via the same-origin proxy. Never throws. */
export async function submitCreate(
  req: { prompt: string; lyrics: string | null },
  baseUrl = "",
): Promise<CreateResult> {
  try {
    const res = await fetch(`${baseUrl}/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt: req.prompt, lyrics: req.lyrics }),
    });
    if (!res.ok) {
      return { ok: false, status: res.status, message: mapCreateError(res.status) };
    }
    const body = (await res.json()) as { job_id: string; state: string };
    return { ok: true, jobId: body.job_id, state: body.state };
  } catch {
    return { ok: false, status: null, message: mapCreateError(null) };
  }
}
