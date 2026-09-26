# syntax=docker/dockerfile:1.7
#
# Builds the React bundle and serves it as static files. The reverse proxy in
# front of this container terminates TLS and routes /api to the API service, so
# the browser only ever sees one origin (spec 10.1).

FROM node:22-alpine AS build

WORKDIR /build

COPY package.json package-lock.json* ./
# `npm ci` when a lock file is present, otherwise a plain install.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
# Static assets served at the site root: the logo and its favicon sizes.
COPY public ./public
RUN npm run build


FROM caddy:2-alpine AS runtime

COPY --from=build /build/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile

# Caddy's image already runs the server as a non-root user for :8080.
EXPOSE 8080
