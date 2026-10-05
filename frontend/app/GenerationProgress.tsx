"use client";

import type { GenerationProgress } from "@/lib/progress";

/** Creator-only status line: Generating / Finished / refunded-failure. Idle renders nothing. */
export default function GenerationProgressView({ progress }: { progress: GenerationProgress }) {
  if (progress.status === "idle") return null;
  const text =
    progress.status === "generating"
      ? `Generating...${progress.eta != null ? ` ~${progress.eta}s` : ""}`
      : progress.status === "finished"
        ? "Finished"
        : "Failed — your song credit was refunded";
  return (
    <p
      role="status"
      aria-live="polite"
      style={{ margin: 0, padding: "8px 16px", fontSize: 14, textAlign: "center", opacity: 0.8 }}
    >
      {text}
    </p>
  );
}
