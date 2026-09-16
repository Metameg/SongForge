/** @type {import('next').NextConfig} */

// The Next server proxies the radio API to the backend so the browser only ever makes
// SAME-ORIGIN requests — no CORS needed, and the internal backend hostname never leaks to
// the browser. In docker-compose the Next server reaches the backend at http://web:8000;
// for `npm run dev` on the host, set BACKEND_URL=http://localhost:8000 (the published web
// port). This runs server-side only, so it is NOT inlined into the client bundle.
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig = {
  // Emit a self-contained server bundle for a small production Docker image.
  output: "standalone",
  async rewrites() {
    return [
      // Same-origin proxy for the radio pointer read (issue #8). The browser fetches
      // "/now-playing" on its own origin; the Next server forwards it to the backend.
      { source: "/now-playing", destination: `${backendUrl}/now-playing` },
    ];
  },
};

module.exports = nextConfig;
