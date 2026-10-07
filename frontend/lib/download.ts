/**
 * Download history + labels (issue #39): the client-observed history window
 * (now-playing + the previous few songs), the same-origin download href and the
 * dropdown labels. The window is newest-first and capped at {@link HISTORY_LIMIT}
 * (now-playing + 5 prior = 6 downloadable tracks). History accumulates as the
 * listener witnesses the radio rotate — a fresh page load starts with now-playing
 * only and fills up to the cap over time. Pure logic apart from `probeDownload`,
 * which only touches the injected fetch.
 */

export interface HistoryEntry {
  song_id: string;
  title: string;
}
export interface DownloadHistory {
  /** Witnessed songs, newest-first, capped at {@link HISTORY_LIMIT}. */
  entries: HistoryEntry[];
}

/** now-playing + the previous 5 = 6 downloadable tracks. */
export const HISTORY_LIMIT = 6;

export const emptyHistory: DownloadHistory = { entries: [] };

export const DOWNLOAD_UNAVAILABLE = "Download unavailable";

/**
 * Record a witnessed now-playing song. A repeat of the current song_id never shifts
 * history; it only refreshes the stored title if that changed. A distinct song is
 * prepended and the window is trimmed back to {@link HISTORY_LIMIT}.
 */
export function reduceHistory(state: DownloadHistory, song: HistoryEntry): DownloadHistory {
  const [head, ...rest] = state.entries;
  if (head?.song_id === song.song_id) {
    if (head.title === song.title) return state;
    return { entries: [song, ...rest] };
  }
  return { entries: [song, ...state.entries].slice(0, HISTORY_LIMIT) };
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

/** Label for an older track: `n` is how many songs ago it played (>= 2). */
export function olderLabel(n: number, title: string): string {
  return `${n} songs ago — ${title}`;
}

export interface DownloadEntry {
  key: string;
  label: string;
  href: string;
}

/**
 * Dropdown entries, newest-first: now-playing, then just-played, then any older
 * tracks still inside the window ("N songs ago"). At most {@link HISTORY_LIMIT}.
 */
export function downloadEntries(history: DownloadHistory): DownloadEntry[] {
  return history.entries.map((entry, i) => {
    const { song_id, title } = entry;
    const href = downloadHref(song_id);
    if (i === 0) return { key: "now", label: nowPlayingLabel(title), href };
    if (i === 1) return { key: "prev", label: justPlayedLabel(title), href };
    return { key: `older-${i}`, label: olderLabel(i, title), href };
  });
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
