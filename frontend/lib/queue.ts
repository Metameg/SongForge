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

/** A single SSE frame as seen by a listener (only the JSON `data` line matters here). */
export interface QueueSseEvent {
  data?: string;
}

/** Minimal EventSource surface `wireQueueRefetch` needs (testable without a DOM). */
export interface QueueEventSource {
  addEventListener(name: string, listener: (event: QueueSseEvent) => void): void;
  removeEventListener(name: string, listener: (event: QueueSseEvent) => void): void;
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

/**
 * The waiting-queue depth carried on a `song-change` SSE frame (depth-in-SSE). The
 * coordinator stamps `queue_depth` onto the pointer so listeners learn the depth from
 * the push -- and from the connect-time frame -- without a `/queue` -> Postgres count on
 * every advance. Returns null (never throws) when the frame carries no usable depth
 * (absent, null, negative, non-numeric, or unparseable), so the caller keeps its last.
 */
export function parseDepthFromSongChange(data: string | undefined): number | null {
  if (typeof data !== "string") return null;
  try {
    const o = JSON.parse(data) as Record<string, unknown>;
    const d = o.queue_depth;
    return typeof d === "number" && Number.isFinite(d) && d >= 0 ? Math.floor(d) : null;
  } catch {
    return null;
  }
}

/** Handlers for the reactive queue wiring (depth-in-SSE). */
export interface QueueRefetchHandlers {
  /** Apply the waiting-queue depth from a `song-change` frame (null = keep last). */
  onDepth: (depth: number | null) => void;
  /** Refetch `/queue` for the viewer's OWN position(s). */
  refetchPositions: () => void;
  /** Whether the viewer currently holds a queue position -- gates the song-change refetch. */
  hasPosition: () => boolean;
}

/**
 * Wire the queue up to SSE (no polling). Depth rides every `song-change` frame AND every
 * `queue-depth` frame (the latter pushed as songs are enqueued between advances, so a
 * plain listener's "N waiting" stays live), so a plain listener NEVER hits `/queue` ->
 * Postgres. `/queue` is refetched for the viewer's own POSITION only when it can actually
 * have changed for them: on their own `job-progress`/`job-ready`, and on `song-change`
 * ONLY while they currently hold a position (the line advanced past/toward them). A
 * `queue-depth` frame is a global-count update only -- a new song enqueued at the tail
 * doesn't move the viewer's own position -- so it updates depth WITHOUT a refetch.
 * Returns a cleanup that detaches every listener it added.
 */
export function wireQueueRefetch(
  source: QueueEventSource,
  handlers: QueueRefetchHandlers,
): () => void {
  const onSongChange = (event: QueueSseEvent) => {
    handlers.onDepth(parseDepthFromSongChange(event.data));
    if (handlers.hasPosition()) handlers.refetchPositions();
  };
  // `queue-depth` carries the same `{queue_depth}` shape as `song-change`, so the same
  // parser applies; depth-only, so it never refetches positions.
  const onQueueDepth = (event: QueueSseEvent) => {
    handlers.onDepth(parseDepthFromSongChange(event.data));
  };
  const onOwnJob = () => handlers.refetchPositions();
  source.addEventListener("song-change", onSongChange);
  source.addEventListener("queue-depth", onQueueDepth);
  source.addEventListener("job-progress", onOwnJob);
  source.addEventListener("job-ready", onOwnJob);
  return () => {
    source.removeEventListener("song-change", onSongChange);
    source.removeEventListener("queue-depth", onQueueDepth);
    source.removeEventListener("job-progress", onOwnJob);
    source.removeEventListener("job-ready", onOwnJob);
  };
}
