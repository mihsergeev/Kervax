# package.json и lock без версии панели: она меняется каждый релиз, и из-за нее npm ci шел с
# нуля на каждой выкладке. Здесь версия заменяется на 0.0.0 - слой с npm ci пересобирается,
# только когда меняются сами зависимости (lock фиксирует их точно, отставать нечему).
FROM node:26.13.1-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS deps
WORKDIR /d
COPY package.json package-lock.json ./
RUN node -e "const fs = require('fs'); for (const f of ['package.json', 'package-lock.json']) { const j = JSON.parse(fs.readFileSync(f, 'utf8')); j.version = '0.0.0'; if (j.packages && j.packages['']) j.packages[''].version = '0.0.0'; fs.writeFileSync(f, JSON.stringify(j)) }"

FROM node:26.13.1-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS build

WORKDIR /build
COPY --from=deps /d/package.json /d/package-lock.json ./
COPY scripts/restrict-build-globs.mjs ./scripts/
RUN npm ci
COPY . .
RUN npm run build

# Официальный nginx запускает мастер-процесс от root — только ради права занять
# 80-й порт, который панели не нужен: наружу её всё равно публикует реверс-прокси.
# Этот образ — тот же nginx, собранный под работу от uid 101 и порт 8080.
FROM ghcr.io/nginx/nginx-unprivileged:1.31.9-alpine@sha256:60214a103e6f38e953ff957f34b1f2e97a9db801ab2df3821da955d0fe6cadd6

# Тег nginx может отставать от исправлений Alpine: обновляем runtime-пакеты,
# затем возвращаем штатного непривилегированного пользователя образа.
USER root
# Как и пакеты ОС в backend, пересобираем слой минимум раз в неделю через ops/deploy.sh.
ARG DEPS_EPOCH=
RUN echo "runtime packages, week ${DEPS_EPOCH:-none}" \
 && apk upgrade --no-cache
USER 101

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 8080
