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
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "5px 12px",
          borderRadius: "var(--sf-pill)",
          background: view.live ? "rgba(255,45,45,0.10)" : "var(--sf-surface)",
          border: `1px solid ${view.live ? "rgba(255,45,45,0.35)" : "var(--sf-border)"}`,
        }}
      >
        <span
          aria-hidden="true"
          className="sf-dot"
          style={{
            width: 9,
            height: 9,
            borderRadius: "50%",
            background: view.live ? "var(--sf-live)" : "var(--sf-faint)",
            boxShadow: view.live ? "0 0 10px 0 rgba(255,45,45,0.7)" : "none",
            animation: view.live ? "sf-pulse 1.6s ease-in-out infinite" : "none",
          }}
        />
        <span
          style={{
            letterSpacing: "0.18em",
            fontSize: 11.5,
            fontWeight: 600,
            color: view.live ? "var(--sf-live)" : "var(--sf-muted)",
          }}
        >
          {view.label}
        </span>
      </div>
      {view.message && (
        <p role="status" style={{ color: "var(--sf-muted)", margin: "10px 0 0" }}>
          {view.message}
        </p>
      )}
    </div>
  );
}
