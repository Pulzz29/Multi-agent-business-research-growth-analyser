# InsightIQ web UI: static build served by nginx, which proxies /api and
# /health to the API service (API_UPSTREAM, default http://insightiq-api:8000).
#   docker build -t insightiq-web:local frontend
FROM node:20-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV API_UPSTREAM=http://insightiq-api:8000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
