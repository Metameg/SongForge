# Next.js frontend — multi-stage build producing a small standalone runtime image.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
# The standalone server binds to $HOSTNAME, which Docker sets to the container ID, so it
# would listen only on the container's network IP and the `localhost` healthcheck would
# be refused. Bind every interface instead (Next.js's own Docker example does this).
ENV HOSTNAME=0.0.0.0
# Next.js "standalone" output bundles a minimal server + only required node_modules.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
