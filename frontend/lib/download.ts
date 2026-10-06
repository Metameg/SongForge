/**
 * Download history + labels (issue #39): the client-observed size-2 history
 * (now-playing + just-played), the same-origin download href and the dropdown labels.
 * Pure logic apart from `probeDownload`, which only touches the injected fetch.
 */

export interface HistoryEntry {
  song_id: string;
  title: string;
}
export interface DownloadHistory {
  nowPlaying: HistoryEntry | null;
  justPlayed: HistoryEntry | null;
}

export const emptyHistory: DownloadHistory = { nowPlaying: null, justPlayed: null };

export const DOWNLOAD_UNAVAILABLE = "Download unavailable";

/**
 * Record a witnessed now-playing song. A repeat of the current song_id never shifts
 * history; it only refreshes the stored title if that changed.
 */
export function reduceHistory(state: DownloadHistory, song: HistoryEntry): DownloadHistory {
  if (state.nowPlaying?.song_id === song.song_id) {
    if (state.nowPlaying.title === song.title) return state;
    return { ...state, nowPlaying: song };
  }
  return { nowPlaying: song, justPlayed: state.nowPlaying };
}

export function downloadHref(songId: string): string {
  return `/download/${encodeURIComponent(songId)}`;
}

export function nowPlayingLabel(title: string): string {
  return `Now playing — ${title}`;
}

export function justPlayedLabel(title: string): string {
  return `Just played — ${title}`;
}

export interface DownloadEntry {
  key: "now" | "prev";
  label: string;
  href: string;
}

/** Dropdown entries: now-playing first, just-played only when present (at most 2). */
export function downloadEntries(history: DownloadHistory): DownloadEntry[] {
  const out: DownloadEntry[] = [];
  if (history.nowPlaying) {
    const { song_id, title } = history.nowPlaying;
    out.push({ key: "now", label: nowPlayingLabel(title), href: downloadHref(song_id) });
  }
  if (history.justPlayed) {
    const { song_id, title } = history.justPlayed;
    out.push({ key: "prev", label: justPlayedLabel(title), href: downloadHref(song_id) });
  }
  return out;
}

/**
 * Probe the same-origin route without following the redirect. Browsers report an
 * unfollowed redirect as `opaqueredirect`; that (or a 2xx) means the download is available.
 */
export async function probeDownload(
  href: string,
  fetchFn: typeof fetch = fetch,
): Promise<boolean> {
  try {
    const res = await fetchFn(href, { redirect: "manual", cache: "no-store" });
    return res.type === "opaqueredirect" || res.ok;
  } catch {
    return false;
  }
}
