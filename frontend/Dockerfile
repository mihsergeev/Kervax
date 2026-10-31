# package.json и lock без версии панели: она меняется каждый релиз, и из-за нее npm ci шел с
# нуля на каждой выкладке. Здесь версия заменяется на 0.0.0 - слой с npm ci пересобирается,
# только когда меняются сами зависимости (lock фиксирует их точно, отставать нечему).
FROM node:26-alpine AS deps
WORKDIR /d
COPY package.json package-lock.json ./
RUN node -e "const fs = require('fs'); for (const f of ['package.json', 'package-lock.json']) { const j = JSON.parse(fs.readFileSync(f, 'utf8')); j.version = '0.0.0'; if (j.packages && j.packages['']) j.packages[''].version = '0.0.0'; fs.writeFileSync(f, JSON.stringify(j)) }"

FROM node:26-alpine AS build

WORKDIR /build
COPY --from=deps /d/package.json /d/package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Официальный nginx запускает мастер-процесс от root — только ради права занять
# 80-й порт, который панели не нужен: наружу её всё равно публикует реверс-прокси.
# Этот образ — тот же nginx, собранный под работу от uid 101 и порт 8080.
FROM nginxinc/nginx-unprivileged:1.31-alpine

# Тег nginx может отставать от исправлений Alpine: обновляем runtime-пакеты,
# затем возвращаем штатного непривилегированного пользователя образа.
USER root
# Как и backend, пересобираем слой минимум раз в неделю через ops/deploy.sh.
ARG DEPS_EPOCH=
RUN echo "runtime packages, week ${DEPS_EPOCH:-none}" \
 && apk upgrade --no-cache
USER 101

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 8080
