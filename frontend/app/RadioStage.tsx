"use client";

import { useEffect, useState } from "react";
import type { NowPlayingState } from "@/lib/nowPlaying";
import { stageView } from "@/lib/stage";

const BARS = 24;

/** Zone 1: album art / animated waveform / gradient fallback (never a broken image). */
export default function RadioStage({
  state,
  isMine = false,
}: {
  state: NowPlayingState | null;
  isMine?: boolean;
}) {
  const [imageFailed, setImageFailed] = useState(false);
  const songId = state?.status === "playing" ? state.song_id : null;
  useEffect(() => {
    setImageFailed(false);
  }, [songId]);
  const view = stageView(state, imageFailed);
  const frame = {
    width: "100%",
    aspectRatio: "1 / 1",
    maxWidth: 420,
    margin: "0 auto",
    borderRadius: 16,
    overflow: "hidden",
    background: "#16161d",
    position: "relative",
  } as const;
  return (
    <section aria-label="Now playing" style={frame}>
      <style>{`@keyframes sf-bar{0%,100%{transform:scaleY(.2)}50%{transform:scaleY(1)}}
@media (prefers-reduced-motion: reduce){.sf-bar{animation:none!important;transform:scaleY(.6)}}`}</style>
      {view.kind === "image" && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={view.src}
          alt={view.alt}
          onError={() => setImageFailed(true)}
          style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
        />
      )}
      {view.kind === "gradient" && (
        <div
          role="img"
          aria-label="Generated song artwork"
          style={{ width: "100%", height: "100%", background: view.background }}
        />
      )}
      {view.kind === "waveform" && (
        <div
          role="img"
          aria-label="Playing a station track"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 4,
            height: "100%",
            padding: 16,
            boxSizing: "border-box",
          }}
        >
          {Array.from({ length: BARS }, (_, i) => (
            <span
              key={i}
              className="sf-bar"
              style={{
                width: 6,
                height: "60%",
                borderRadius: 3,
                background: "#f2f2f2",
                transformOrigin: "center",
                animation: `sf-bar ${0.9 + (i % 5) * 0.15}s ease-in-out ${i * 0.04}s infinite`,
              }}
            />
          ))}
        </div>
      )}
      {isMine && state?.status === "playing" && (
        <span
          style={{
            position: "absolute",
            top: 12,
            left: 12,
            padding: "4px 10px",
            borderRadius: 999,
            fontSize: 12,
            fontWeight: 600,
            background: "#0b0b0f",
            color: "#8ab4ff",
            border: "1px solid #8ab4ff",
          }}
        >
          Your song
        </span>
      )}
      {view.kind === "idle" && <div style={{ width: "100%", height: "100%" }} />}
    </section>
  );
}
