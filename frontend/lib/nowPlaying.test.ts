/**
 * `fetchNowPlaying` contract tests (issue #8, criteria #3, #4).
 *
 * Mocks `fetch` with a payload built to the BACKEND's exact response shape (see
 * `backend/src/songforge/radio/state.py::NowPlayingView.to_response` and
 * `backend/tests/test_now_playing.py`) — not a hand-invented one — so a drift between
 * the two sides (a renamed/missing field, or a timestamp format `Date.parse` can't read
 * as UTC) would show up here rather than only in the browser.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchNowPlaying, type NowPlaying } from "./nowPlaying";

// Exactly what `NowPlayingView.to_response` emits for a seeded pointer (see
// `test_now_playing_returns_full_pointer_shape` / `test_now_playing_timestamps_are_tz_aware_utc`
// in `backend/tests/test_now_playing.py`) — including the UTC-offset timestamp format the
// backend guarantees via `_isoformat_utc`.
const BACKEND_SHAPED_PAYLOAD = {
  status: "playing",
  song_id: "song-1",
  title: "Song One",
  source: "static",
  object_key: "audio/song-1.mp3",
  audio_url: "http://minio:9000/songforge/audio/song-1.mp3",
  started_at: "2026-09-15T12:00:00+00:00",
  ends_at: "2026-09-15T12:03:00+00:00",
  duration: 180,
  playback_id: "pb-1",
  version: 3,
  server_time: "2026-09-15T12:00:30.500000+00:00",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchNowPlaying", () => {
  it("parses every field of the real backend response shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        json: async () => BACKEND_SHAPED_PAYLOAD,
      }),
    );

    const result = await fetchNowPlaying("http://backend");

    for (const [key, value] of Object.entries(BACKEND_SHAPED_PAYLOAD)) {
      expect((result as Record<string, unknown>)[key]).toBe(value);
    }
  });

  // Regression guard for the contract gap found in Phase 4 (see
  // .orchestrator/phase4-contract-report.md): the backend's 200 (playing) body must
  // carry `status: "playing"`, symmetric with the 503 idle body's `{"status": "idle"}`.
  // `NowPlaying.status` is typed as the literal `"playing"`, and `Player.tsx` /
  // `fetchNowPlaying`'s discriminated union branch on `next.status === "playing"`
  // throughout — if the payload ever stops carrying it, the Play button stays disabled
  // and the boundary/`ended` re-anchor never fires. `NowPlayingView.to_response` now
  // emits it (backend/src/songforge/radio/state.py).
  it("discriminates as 'playing' on the real 200 response, which carries the status field", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        json: async () => BACKEND_SHAPED_PAYLOAD,
      }),
    );

    const result = await fetchNowPlaying("http://backend");

    expect(result.status).toBe("playing");
  });

  it("Date.parse reads the backend's started_at/server_time as the exact UTC instant", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        json: async () => BACKEND_SHAPED_PAYLOAD,
      }),
    );

    const result = (await fetchNowPlaying("http://backend")) as NowPlaying;

    // The seam this pins: an offset-less ISO string is parsed by `Date.parse` as
    // *local* time, not UTC, silently corrupting the sync math by the client's
    // timezone offset. The backend's `_isoformat_utc` guarantees an explicit "+00:00",
    // so these must resolve to the literal UTC wall-clock time written above.
    expect(new Date(Date.parse(result.started_at)).toISOString()).toBe(
      "2026-09-15T12:00:00.000Z",
    );
    expect(new Date(Date.parse(result.server_time)).toISOString()).toBe(
      "2026-09-15T12:00:30.500Z",
    );
  });

  it("requests the /now-playing path on the given base URL without caching", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => BACKEND_SHAPED_PAYLOAD });
    vi.stubGlobal("fetch", fetchMock);

    await fetchNowPlaying("http://backend:8000");

    expect(fetchMock).toHaveBeenCalledWith("http://backend:8000/now-playing", {
      cache: "no-store",
    });
  });
});
