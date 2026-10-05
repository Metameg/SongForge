"use client";

import { useState, type CSSProperties, type FormEvent } from "react";
import { canCreate, submitCreate } from "@/lib/create";
import { formatSongsLeft, type QuotaResponse } from "@/lib/quota";

export interface ComposerProps {
  quota: QuotaResponse | null;
  /** Hard-wired false in #36; job progress is a later slice. */
  generationActive: boolean;
  /** RadioApp refreshes quota after a successful create. */
  onCreated: (jobId: string) => void;
}

const field: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: "#16161d",
  color: "#f2f2f2",
  border: "1px solid #2a2a35",
  borderRadius: 12,
  padding: "10px 12px",
  fontSize: 16,
  fontFamily: "inherit",
  resize: "vertical",
};

/** Zone 3: pinned composer. All gating/error logic lives in `lib/create.ts`. */
export default function Composer({ quota, generationActive, onCreated }: ComposerProps) {
  const [prompt, setPrompt] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [showLyrics, setShowLyrics] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remaining = quota && quota.enforced ? quota.remaining : null;
  const enabled = canCreate({ prompt, inFlight, generationActive, remaining });

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!enabled) return;
    setInFlight(true);
    setError(null);
    try {
      const trimmedLyrics = lyrics.trim();
      const result = await submitCreate({
        prompt,
        lyrics: showLyrics && trimmedLyrics ? trimmedLyrics : null,
      });
      if (result.ok) {
        setPrompt("");
        setLyrics("");
        setShowLyrics(false);
        onCreated(result.jobId);
      } else {
        setError(result.message);
      }
    } finally {
      setInFlight(false);
    }
  };

  return (
    <form
      onSubmit={onSubmit}
      aria-label="Create a song"
      style={{
        position: "sticky",
        bottom: 0,
        background: "#0b0b0f",
        padding: "12px 16px",
        borderTop: "1px solid #22222b",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <textarea
        aria-label="Describe your song"
        placeholder="Describe the song you want to hear"
        rows={2}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        style={field}
      />
      <button
        type="button"
        aria-expanded={showLyrics}
        onClick={() => setShowLyrics((v) => !v)}
        style={{
          alignSelf: "flex-start",
          background: "none",
          border: "none",
          color: "#f2f2f2",
          opacity: 0.7,
          cursor: "pointer",
          padding: 0,
          fontSize: 14,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          aria-hidden="true"
          style={{ transform: showLyrics ? "rotate(90deg)" : "none", transition: "transform .15s" }}
        >
          <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        Add lyrics (optional)
      </button>
      {showLyrics && (
        <textarea
          aria-label="Lyrics"
          placeholder="Lyrics (optional)"
          rows={4}
          value={lyrics}
          onChange={(e) => setLyrics(e.target.value)}
          style={field}
        />
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ opacity: 0.5, fontSize: 13 }}>{quota ? formatSongsLeft(quota) : ""}</span>
        <button
          type="submit"
          disabled={!enabled}
          style={{
            padding: "0.65rem 1.5rem",
            fontSize: "1rem",
            borderRadius: 999,
            border: "none",
            cursor: enabled ? "pointer" : "not-allowed",
            background: "#f2f2f2",
            color: "#0b0b0f",
            opacity: enabled ? 1 : 0.5,
          }}
        >
          {inFlight ? "Creating..." : "Create Song"}
        </button>
      </div>
      {error && (
        <p role="alert" style={{ margin: 0, fontSize: 14 }}>
          {error}
        </p>
      )}
    </form>
  );
}
