"use client";

import { liveIndicator, type RadioFeed } from "@/lib/radio";

/**
 * LIVE / OFFLINE status row (issue #36). All decisions come from the pure
 * `liveIndicator`; this is the ONLY place red appears in the UI.
 */
export default function LiveIndicator({ feed }: { feed: RadioFeed }) {
  const view = liveIndicator(feed);
  return (
    <div>
      <style>{`@keyframes sf-pulse{0%,100%{opacity:1}50%{opacity:.35}}@media (prefers-reduced-motion: reduce){.sf-dot{animation:none!important}}`}</style>
      <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
        <span
          aria-hidden="true"
          className="sf-dot"
          style={{
            width: 10,
            height: 10,
            borderRadius: "50%",
            background: view.live ? "#ff2d2d" : "#555",
            animation: view.live ? "sf-pulse 1.6s ease-in-out infinite" : "none",
          }}
        />
        <span style={{ letterSpacing: 2, fontSize: 13, fontWeight: 600 }}>{view.label}</span>
      </div>
      {view.message && (
        <p role="status" style={{ opacity: 0.7, margin: "8px 0 0" }}>
          {view.message}
        </p>
      )}
    </div>
  );
}
