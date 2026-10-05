/** Queue depth + the viewer's own position (issue #38). STUB for TDD red. */

export interface QueuePosition {
  job_id: string;
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

export function parseQueue(_raw: unknown): QueueResponse {
  return { depth: 0, positions: [] };
}

export function formatQueueLine(_q: QueueResponse): string {
  return "";
}

export function myPosition(_q: QueueResponse): number | null {
  return null;
}

export async function fetchQueue(_baseUrl: string): Promise<QueueResponse> {
  throw new Error("fetchQueue not implemented (issue #38)");
}

export function wireQueueRefetch(
  _source: QueueEventSource,
  _refetch: () => void,
): () => void {
  return () => {};
}
