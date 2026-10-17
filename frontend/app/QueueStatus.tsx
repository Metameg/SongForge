"use client";

import { myPosition, positionLabel, type QueueResponse } from "@/lib/queue";

/**
 * Queue status: a prominent global depth stat plus the viewer's own position. Position #1
 * is celebrated with a glowing "next up" badge; later positions get a quieter badge.
 */
export default function QueueStatus({ queue }: { queue: QueueResponse | null }) {
  if (queue === null) return null;
  const mine = myPosition(queue);
  const isNext = mine === 1;
  return (
    <div
      role="status"
      aria-live="polite"
      className="sf-card sf-rise"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexWrap: "wrap",
        gap: 12,
        padding: "12px 16px",
        margin: "12px 0",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "baseline", gap: 8 }}>
        <span className="sf-stat-label">Queue</span>
        <span className="sf-stat-num" style={{ fontSize: 22 }}>
          {queue.depth}
        </span>
        <span style={{ fontSize: 13, color: "var(--sf-muted)" }}>waiting</span>
      </span>
      {mine !== null && (
        <span className={`sf-badge ${isNext ? "sf-badge-next" : "sf-badge-pos"}`}>
          {isNext && (
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M8 1l2.1 4.3 4.7.7-3.4 3.3.8 4.7L8 11.9 3.8 14l.8-4.7L1.2 6l4.7-.7z"
                fill="currentColor"
              />
            </svg>
          )}
          {positionLabel(mine, queue.depth)}
        </span>
      )}
    </div>
  );
}
