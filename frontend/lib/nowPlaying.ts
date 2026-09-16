/**
 * `/now-playing` fetch + response shape (issue #8, criteria #3, #4, #5).
 *
 * Mirrors `songforge.radio.state.NowPlayingView.to_response` on the backend. Idle is a
 * distinct HTTP status (503) rather than a body field, so `fetchNowPlaying` returns a
 * discriminated union the player can branch on directly.
 */

export interface NowPlaying {
  status: "playing";
  song_id: string;
  title: string;
  source: "static" | "generated";
  object_key: string;
  audio_url: string;
  started_at: string;
  ends_at: string;
  duration: number | null;
  playback_id: string;
  version: number;
  server_time: string;
}

export interface RadioIdle {
  status: "idle";
}

export type NowPlayingState = NowPlaying | RadioIdle;

/** Fetch the current radio pointer from the backend. Never throws on a 503 idle body. */
export async function fetchNowPlaying(baseUrl: string): Promise<NowPlayingState> {
  const response = await fetch(`${baseUrl}/now-playing`, { cache: "no-store" });
  const body = (await response.json()) as NowPlayingState;
  return body;
}
