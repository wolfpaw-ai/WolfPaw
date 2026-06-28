# Wolfpaw web image — Vite build, served by nginx with SPA fallback.
#
# In dev the React app is served by Vite on :5173 (with a proxy to the
# backend on :8000); in compose both land behind nginx in this container,
# which serves the static build and proxies API paths to the `app`
# service.

# ---------- builder ----------
FROM node:20-alpine AS builder

WORKDIR /build

# package.json + lockfile first so the install layer is cached when only
# source changes. `npm ci` installs strictly from package-lock.json
# (reproducible, and fails if the lock and package.json have drifted).
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

RUN npm run build

# ---------- runtime ----------
FROM nginx:1.27-alpine

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
