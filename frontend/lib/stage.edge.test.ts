/** Edge coverage for `lib/stage.ts` (issue #36 Review Focus #4). */
import { describe, expect, it } from "vitest";
import type { NowPlaying } from "./nowPlaying";
import { gradientFor, stageView } from "./stage";

const np = (o: Partial<NowPlaying> = {}): NowPlaying => ({
  status: "playing", song_id: "s1", title: "T", source: "generated", object_key: "audio/s1.mp3",
  audio_url: "https://x/a.mp3", started_at: "2026-10-04T00:00:00Z", ends_at: "2026-10-04T00:02:00Z",
  duration: 120, playback_id: "p1", version: 1, server_time: "2026-10-04T00:00:01Z",
  album_cover_path: "https://cdn.test/c.svg", ...o,
});

describe("gradientFor edge cases", () => {
  it("is stable per id and keeps both hues within 180-300 (cyan..violet, never red) for many ids", () => {
    for (let i = 0; i < 200; i++) {
      const id = `song-${i}-${"x".repeat(i % 7)}`;
      const g = gradientFor(id);
      expect(gradientFor(id)).toBe(g);
      const hues = [...g.matchAll(/hsl\((\d+)/g)].map((m) => Number(m[1]));
      expect(hues).toHaveLength(2);
      for (const h of hues) {
        expect(h).toBeGreaterThanOrEqual(180);
        expect(h).toBeLessThanOrEqual(300);
      }
    }
  });

  it("is not a constant: distinct ids mostly yield distinct gradients", () => {
    const set = new Set(Array.from({ length: 50 }, (_, i) => gradientFor(`id-${i}`)));
    expect(set.size).toBeGreaterThan(40);
  });
});

describe("stageView source switching", () => {
  it("gradient fallback is keyed on song_id only, not title or cover", () => {
    const a = stageView(np({ album_cover_path: null, title: "A" }), false);
    const b = stageView(np({ album_cover_path: null, title: "B" }), false);
    expect(a).toEqual(b);
    expect(stageView(np({ album_cover_path: null, song_id: "other" }), false)).not.toEqual(a);
  });

  it("switches image -> gradient -> waveform as the playing song changes", () => {
    expect(stageView(np(), false).kind).toBe("image");
    expect(stageView(np({ song_id: "s2", album_cover_path: null }), false).kind).toBe("gradient");
    expect(stageView(np({ song_id: "s3", source: "static", album_cover_path: null }), false).kind).toBe("waveform");
  });

  it("an image failure never changes the idle or static views", () => {
    expect(stageView({ status: "idle" }, true)).toEqual({ kind: "idle" });
    expect(stageView(np({ source: "static" }), true)).toEqual({ kind: "waveform" });
  });
});
