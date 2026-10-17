import { describe, expect, it } from "vitest";
import type { NowPlaying } from "./nowPlaying";
import { gradientFor, stageView } from "./stage";

const np = (o: Partial<NowPlaying> = {}): NowPlaying => ({
  status: "playing", song_id: "s1", title: "T", source: "generated", object_key: "audio/s1.mp3",
  audio_url: "https://x/a.mp3", started_at: "2026-10-04T00:00:00Z", ends_at: "2026-10-04T00:02:00Z",
  duration: 120, playback_id: "p1", version: 1, server_time: "2026-10-04T00:00:01Z",
  album_cover_path: "https://cdn.test/c.svg", ...o,
});

describe("gradientFor", () => {
  it("is deterministic per song id and differs across ids", () => {
    expect(gradientFor("abc")).toBe(gradientFor("abc"));
    expect(gradientFor("abc")).not.toBe(gradientFor("abd"));
  });
  it("is a valid linear-gradient and never uses a red hue", () => {
    for (const id of ["a", "b", "c", "song-123", "", "ffffffffffffffff"]) {
      const g = gradientFor(id);
      expect(g.startsWith("linear-gradient(")).toBe(true);
      const hues = [...g.matchAll(/hsl\((\d+)/g)].map((m) => Number(m[1]));
      expect(hues.length).toBe(2);
      for (const h of hues) { expect(h).toBeGreaterThanOrEqual(180); expect(h).toBeLessThanOrEqual(300); }
    }
  });
});

describe("stageView", () => {
  it("is idle for null and idle states", () => {
    expect(stageView(null, false)).toEqual({ kind: "idle" });
    expect(stageView({ status: "idle" }, false)).toEqual({ kind: "idle" });
  });
  it("shows album art for generated songs with a cover", () => {
    expect(stageView(np(), false)).toEqual({ kind: "image", src: "https://cdn.test/c.svg", alt: "Album art for T" });
  });
  it("falls back to the gradient when cover is null, blank, or the image failed", () => {
    const g = gradientFor("s1");
    expect(stageView(np({ album_cover_path: null }), false)).toEqual({ kind: "gradient", background: g });
    expect(stageView(np({ album_cover_path: "  " }), false)).toEqual({ kind: "gradient", background: g });
    expect(stageView(np(), true)).toEqual({ kind: "gradient", background: g });
  });
  it("shows the waveform for static songs regardless of cover", () => {
    expect(stageView(np({ source: "static", album_cover_path: null }), false)).toEqual({ kind: "waveform" });
    expect(stageView(np({ source: "static" }), true)).toEqual({ kind: "waveform" });
  });
});
