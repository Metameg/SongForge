/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a self-contained server bundle for a small production Docker image.
  output: "standalone",
  // The browser reaches the backend through the same-origin `/now-playing` Route Handler
  // (see `app/now-playing/route.ts`), which proxies to the runtime BACKEND_URL. No
  // build-time rewrite is used, because rewrite destinations are baked into the build
  // and can't read a runtime env var.
};

module.exports = nextConfig;
