/** Songs the viewer created, persisted client-side only (issue #37). Never throws. */

export const MY_SONGS_KEY = "songforge:mySongs";
const MAX_IDS = 200;

interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

function storage(): StorageLike | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function loadMySongs(): Set<string> {
  try {
    const raw = storage()?.getItem(MY_SONGS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return new Set();
  }
}

/** Returns the set including `id`; persistence is best-effort. */
export function addMySong(id: string): Set<string> {
  const next = loadMySongs();
  next.delete(id); // re-insert so the id counts as most recent
  next.add(id);
  try {
    storage()?.setItem(MY_SONGS_KEY, JSON.stringify([...next].slice(-MAX_IDS)));
  } catch {
    /* in-memory only */
  }
  return next;
}

export function hasMySong(set: Set<string>, id: string | null | undefined): boolean {
  return id != null && set.has(id);
}
