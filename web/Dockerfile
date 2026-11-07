# Build the static app, then serve it. VITE_* values are baked into the bundle at build
# time; none of them is a secret.
FROM node:24.22.2-slim AS build
RUN npm install --global pnpm@12.18.0
WORKDIR /web
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
ARG VITE_API_URL=http://localhost:8000
RUN pnpm build

FROM nginx:1.31.8-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
