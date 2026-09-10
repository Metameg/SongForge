/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a self-contained server bundle for a small production Docker image.
  output: "standalone",
  // Base URL of the FastAPI backend, injected by docker-compose.
  env: {
    BACKEND_URL: process.env.BACKEND_URL || "http://web:8000",
  },
};

module.exports = nextConfig;
