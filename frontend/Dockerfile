FROM node:26-alpine AS build

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Официальный nginx запускает мастер-процесс от root — только ради права занять
# 80-й порт, который панели не нужен: наружу её всё равно публикует реверс-прокси.
# Этот образ — тот же nginx, собранный под работу от uid 101 и порт 8080.
FROM nginxinc/nginx-unprivileged:1.31-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 8080
