"use client";

import { formatQueueLine, myPosition, type QueueResponse } from "@/lib/queue";

/** Queue line (always) plus "You are #N" only when the viewer has a queued song. */
export default function QueueStatus({ queue }: { queue: QueueResponse | null }) {
  if (queue === null) return null;
  const mine = myPosition(queue);
  return (
    <div role="status" aria-live="polite" style={{ margin: "8px 0", fontSize: 14 }}>
      <p style={{ margin: 0, opacity: 0.7 }}>{formatQueueLine(queue)}</p>
      {mine !== null && <p style={{ margin: "4px 0 0", opacity: 0.9 }}>You are #{mine}</p>}
    </div>
  );
}
