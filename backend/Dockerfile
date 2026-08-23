# --- сборка Go-агента из исходников (панель собирает агента сама) ---
# AGENT_PUBKEY пуст по умолчанию → у агента нет ключа → самообновление ВЫКЛЮЧЕНО.
# Чтобы включить secure-автообновление, задай KERVAX_AGENT_PUBKEY в .env (см. README).
FROM golang:1.22-alpine AS agent
ARG AGENT_PUBKEY=
WORKDIR /agent
COPY agent/go.mod ./
COPY agent/main.go ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
      -ldflags="-s -w -buildid= -X main.updatePubKeyB64=${AGENT_PUBKEY}" \
      -o /out/kervax-agent-amd64 . \
 && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
      -ldflags="-s -w -buildid= -X main.updatePubKeyB64=${AGENT_PUBKEY}" \
      -o /out/kervax-agent-arm64 .

# --- бэкенд ---
FROM python:3.14-slim

# Непривилегированный пользователь. gosu нужен entrypoint'у: он стартует от root,
# выравнивает права на смонтированном томе и сбрасывает привилегии — su/sudo для
# этого не годятся, они плодят промежуточный процесс и ломают доставку сигналов
# первому процессу контейнера.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -r -g 10001 kervax; \
    useradd -r -u 10001 -g kervax -d /srv -s /usr/sbin/nologin kervax; \
    gosu nobody true      # проверяем на сборке, а не в 3 часа ночи на проде

WORKDIR /srv

COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir .

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic

# бинари агента, которые панель раздаёт (/api/agent/download|install.sh)
COPY --from=agent /out/kervax-agent-amd64 /out/kervax-agent-arm64 /srv/agent/
# ОПЦИОНАЛЬНО: подписанный релиз-манифест (manifest.json/.sig) для secure-автообновления.
# agent-dist/ всегда существует (.gitkeep); manifest появляется после agent-signing/release.py.
# Нет манифеста → эндпоинт отдаёт 404, фича просто выключена.
#
# Берём ОТТУДА ТОЛЬКО манифест и подпись. Раньше копировали каталог целиком, и
# завалявшийся там бинарь прошлого релиза затирал только что собранный (COPY идёт
# после сборки) — панель раздавала бинарь, чей sha256 не совпадал с подписанным, и
# обновление молча отваливалось у ВСЕГО парка. Живьём поймано на 1.90.
COPY agent-dist/ /tmp/agent-dist/
RUN set -eu; \
    for f in manifest.json manifest.sig; do \
      if [ -f "/tmp/agent-dist/$f" ]; then cp -f "/tmp/agent-dist/$f" /srv/agent/; fi; \
    done; \
    rm -rf /tmp/agent-dist
# Скрипты из agent/ КОПИРУЕМ ПОСЛЕ agent-dist/ — иначе устаревший agent-dist/install.sh
# (если завалялся в репо/на сервере) перетрёт свежий. Здесь agent/ — источник истины.
# Маской, а не поимённо: перечисление уже подводило — dbstat-setup.sh забыли добавить,
# и панель отдавала 404 на хелпер, который сама же флагует как невыкаченный.
COPY agent/*.sh /srv/agent/

# Всё, что панель читает на ходу, отдаём пользователю, под которым она работает.
RUN chown -R kervax:kervax /srv

COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
