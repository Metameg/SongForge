"use client";

import { useEffect, useRef, useState } from "react";
import {
  DOWNLOAD_UNAVAILABLE,
  downloadEntries,
  probeDownload,
  type DownloadHistory,
} from "@/lib/download";

/**
 * Download dropdown (issue #39): up to HISTORY_LIMIT entries (now-playing + the previous
 * few songs). Logic lives in `lib/download`. The probe + navigation is two cheap 302
 * requests; the probe exists so a failure shows inline instead of navigating to an error page.
 */
export default function DownloadButton({ history }: { history: DownloadHistory }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(false);
  const inFlight = useRef(false);
  const entries = downloadEntries(history);

  useEffect(() => {
    setError(false);
  }, [history]);

  if (entries.length === 0) return null;

  const download = async (href: string) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setError(false);
    try {
      if (await probeDownload(href)) {
        setOpen(false);
        window.location.href = href;
      } else {
        setError(true);
      }
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <div style={{ position: "relative", display: "inline-block", marginTop: 10 }}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 14px",
          borderRadius: "var(--sf-pill)",
          background: "var(--sf-surface)",
          color: "var(--sf-text)",
          border: "1px solid var(--sf-border-strong)",
          fontSize: 13,
          cursor: "pointer",
        }}
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3v12M7 10l5 5 5-5M5 21h14" />
        </svg>
        Download
      </button>
      {open && (
        <ul
          role="menu"
          style={{
            position: "absolute",
            left: "50%",
            transform: "translateX(-50%)",
            top: "calc(100% + 6px)",
            zIndex: 10,
            minWidth: 240,
            maxWidth: "80vw",
            margin: 0,
            padding: 4,
            listStyle: "none",
            background: "var(--sf-surface-2)",
            border: "1px solid var(--sf-border-strong)",
            borderRadius: "var(--sf-radius-sm)",
            boxShadow: "var(--sf-shadow)",
          }}
        >
          {entries.map((e) => (
            <li key={e.key} role="none">
              <button
                type="button"
                role="menuitem"
                onClick={() => void download(e.href)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  padding: "8px 10px",
                  background: "transparent",
                  color: "var(--sf-text)",
                  border: "none",
                  borderRadius: 6,
                  fontSize: 13,
                  cursor: "pointer",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {e.label}
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="status" style={{ margin: "6px 0 0", fontSize: 12, color: "var(--sf-danger)" }}>
          {DOWNLOAD_UNAVAILABLE}
        </p>
      )}
    </div>
  );
}
