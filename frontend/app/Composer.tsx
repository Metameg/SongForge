"use client";

import { useState, type FormEvent } from "react";
import { canCreate, submitCreate } from "@/lib/create";
import { formatSongsLeft, type QuotaResponse } from "@/lib/quota";

export interface ComposerProps {
  quota: QuotaResponse | null;
  /** Hard-wired false in #36; job progress is a later slice. */
  generationActive: boolean;
  /** RadioApp refreshes quota after a successful create. */
  onCreated: (jobId: string) => void;
}

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
        background: "linear-gradient(180deg, rgba(11,11,15,0) 0%, var(--sf-bg) 22%)",
        padding: "16px 16px 18px",
        borderTop: "1px solid var(--sf-border)",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <textarea
        aria-label="Describe your song"
        placeholder="Describe the song you want to hear…"
        rows={2}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        className="sf-field"
      />
      <button
        type="button"
        aria-expanded={showLyrics}
        onClick={() => setShowLyrics((v) => !v)}
        className="sf-toggle"
        style={{ alignSelf: "flex-start" }}
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
          className="sf-field sf-rise"
        />
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        {quota ? (
          <span className="sf-chip">
            <span className="sf-chip-dot" aria-hidden="true" />
            {formatSongsLeft(quota)}
          </span>
        ) : (
          <span />
        )}
        <button type="submit" disabled={!enabled} className="sf-btn sf-btn-primary">
          {inFlight ? "Creating…" : "Create Song"}
        </button>
      </div>
      {error && (
        <p role="alert" className="sf-alert sf-rise">
          {error}
        </p>
      )}
    </form>
  );
}
