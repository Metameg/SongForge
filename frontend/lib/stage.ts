/** Radio stage source switching + deterministic cover fallback (issue #36). Pure. */
import type { NowPlayingState } from "./nowPlaying";

export type StageView =
  | { kind: "idle" }
  | { kind: "image"; src: string; alt: string }
  | { kind: "waveform" }
  | { kind: "gradient"; background: string };

/** FNV-1a 32-bit. */
function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** Hues are confined to 180-300 (cyan..violet): red is reserved for the LIVE dot. */
export function gradientFor(songId: string): string {
  const h = hash(songId);
  const h1 = 180 + (h % 121);
  const h2 = 180 + ((h >>> 8) % 121);
  return `linear-gradient(135deg, hsl(${h1} 55% 38%), hsl(${h2} 60% 18%))`;
}

export function stageView(state: NowPlayingState | null, imageFailed: boolean): StageView {
  if (!state || state.status !== "playing") return { kind: "idle" };
  if (state.source === "static") return { kind: "waveform" };
  const cover = state.album_cover_path?.trim();
  if (cover && !imageFailed) {
    return { kind: "image", src: cover, alt: `Album art for ${state.title}` };
  }
  return { kind: "gradient", background: gradientFor(state.song_id) };
}
