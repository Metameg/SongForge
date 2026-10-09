/** `/queue` fetch, parsing, formatting and SSE refetch wiring (issue #38). */

export interface QueuePosition {
  job_id: string;
  /** 1-based GLOBAL position in the waiting FIFO. */
  position: number;
}
export interface QueueResponse {
  depth: number;
  positions: QueuePosition[];
}

/** Minimal EventSource surface `wireQueueRefetch` needs (testable without a DOM). */
export interface QueueEventSource {
  addEventListener(name: string, listener: () => void): void;
  removeEventListener(name: string, listener: () => void): void;
}

/** Tolerant parse: anything malformed degrades to empty/zero rather than throwing. */
export function parseQueue(raw: unknown): QueueResponse {
  if (typeof raw !== "object" || raw === null) return { depth: 0, positions: [] };
  const o = raw as Record<string, unknown>;
  const depth =
    typeof o.depth === "number" && Number.isFinite(o.depth) && o.depth > 0
      ? Math.floor(o.depth)
      : 0;
  const positions: QueuePosition[] = [];
  if (Array.isArray(o.positions)) {
    for (const p of o.positions) {
      if (typeof p !== "object" || p === null) continue;
      const r = p as Record<string, unknown>;
      if (
        typeof r.job_id === "string" &&
        typeof r.position === "number" &&
        Number.isInteger(r.position) &&
        r.position >= 1
      ) {
        positions.push({ job_id: r.job_id, position: r.position });
      }
    }
  }
  return { depth, positions };
}

/** The always-visible queue line. */
export function formatQueueLine(q: QueueResponse): string {
  return `Queue · ${q.depth} waiting`;
}

/** The viewer's soonest position (lowest number), or null when they have none queued. */
export function myPosition(q: QueueResponse): number | null {
  if (q.positions.length === 0) return null;
  return Math.min(...q.positions.map((p) => p.position));
}

/**
 * Human-facing label for the viewer's own queue position. Position 1 gets a celebratory
 * "next up" message; later positions get a plain in-line label. When ``total`` (the queue
 * depth) is supplied, later positions are shown OUT OF the total (e.g. "You're #42 of 2000
 * in line"); omitting it preserves the plain "#42 in line" form.
 */
export function positionLabel(position: number, total?: number): string {
  if (position === 1) return "Your song is next up!";
  if (total !== undefined && total >= position) {
    return `You're #${position} of ${total} in line`;
  }
  return `You're #${position} in line`;
}

/** Fetch via the same-origin proxy. */
export async function fetchQueue(baseUrl: string): Promise<QueueResponse> {
  const response = await fetch(`${baseUrl}/queue`, { cache: "no-store" });
  // A non-OK response (e.g. the proxy's 503 fallback) must NOT be parsed as an empty
  // queue: throw so the caller keeps its last known queue line.
  if (!response.ok) throw new Error(`queue fetch failed: ${response.status}`);
  return parseQueue(await response.json());
}

/**
 * Monotonic guard so only the newest in-flight refresh applies its result. Rapid SSE
 * events fire concurrent `fetchQueue` calls; without this an older-but-slower response
 * could resolve last and overwrite a newer one, leaving a stale depth/position until the
 * next event. Each refresh calls `begin()` for a token and only commits when
 * `isCurrent(token)` still holds at resolve time.
 */
export function makeLatestGuard(): {
  begin: () => number;
  isCurrent: (token: number) => boolean;
} {
  let current = 0;
  return {
    begin: () => ++current,
    isCurrent: (token: number) => token === current,
  };
}

/** Events after which the queue may have changed (advance, or the viewer's own job moved). */
const REFETCH_EVENTS = ["song-change", "job-progress", "job-ready"] as const;

/** Refetch on queue-relevant SSE events (no polling). Returns a cleanup that detaches. */
export function wireQueueRefetch(
  source: QueueEventSource,
  refetch: () => void,
): () => void {
  const handler = () => refetch();
  for (const name of REFETCH_EVENTS) source.addEventListener(name, handler);
  return () => {
    for (const name of REFETCH_EVENTS) source.removeEventListener(name, handler);
  };
}
