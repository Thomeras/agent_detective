# Web UI (M7): multi-stage build. Vite/React/TS bundle, then served by nginx.
# Build context is the repo root (see docker-compose.yml `web` service).

FROM node:20-alpine AS build
WORKDIR /app

# Install deps against the lockfile first for better layer caching.
COPY web/package.json web/package-lock.json* ./
RUN npm install

COPY web/ ./

# The browser reaches the API on the host-published port (default :8000).
# Override at build time: --build-arg VITE_API_BASE_URL=...
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
