"use client";

import type { GenerationProgress } from "@/lib/progress";

/** Creator-only status card with an animated bar: Generating / Finished / refunded-failure. */
export default function GenerationProgressView({ progress }: { progress: GenerationProgress }) {
  if (progress.status === "idle") return null;

  const generating = progress.status === "generating";
  const failed = progress.status === "failed";
  const timedout = progress.status === "timedout";
  const label = generating
    ? `Generating your song${progress.eta != null ? ` · ~${progress.eta}s` : ""}`
    : progress.status === "finished"
      ? "Finished — your song is ready"
      : timedout
        ? "This is taking longer than expected — try again. If you were charged, your credits have been refunded."
        : "Generation failed — your song credit was refunded";

  return (
    <div
      role="status"
      aria-live="polite"
      className="sf-card sf-rise"
      style={{ margin: "0 16px 10px", padding: "12px 15px", display: "grid", gap: 9 }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          fontSize: 13.5,
          fontWeight: 500,
          color: failed ? "var(--sf-danger)" : "var(--sf-text)",
        }}
      >
        <span className="sf-stat-label" style={{ color: failed ? "var(--sf-danger)" : undefined }}>
          {generating ? "Working" : failed ? "Failed" : timedout ? "Pending" : "Done"}
        </span>
        {label}
      </span>
      <div className="sf-progress-track">
        {generating ? (
          <div className="sf-progress-indeterminate" aria-hidden="true" />
        ) : (
          <div className={`sf-progress-fill${failed ? " is-failed" : ""}`} aria-hidden="true" />
        )}
      </div>
    </div>
  );
}
