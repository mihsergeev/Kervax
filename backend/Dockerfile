# --- сборка Go-агента из исходников (панель собирает агента сама) ---
# AGENT_PUBKEY пуст по умолчанию → у агента нет ключа → самообновление ВЫКЛЮЧЕНО.
# Чтобы включить secure-автообновление, задай KERVAX_AGENT_PUBKEY в .env (см. README).
# Образ Go - ровно тот же, что GO_IMAGE в agent-signing/release.py, с точным патчем:
# панель раздает бинарь этой сборки, а подписан бинарь сборки release.py, и sha256
# совпадают, только пока их собирает один Go. Менять оба места вместе и выпускать новый
# релиз агента (ops/selfcheck.py это проверяет). Digest - multi-arch индекс этого тега.
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS agent
ARG AGENT_PUBKEY=
WORKDIR /agent
COPY agent/go.mod ./
COPY agent/*.go ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
      -ldflags="-s -w -buildid= -X main.updatePubKeyB64=${AGENT_PUBKEY}" \
      -o /out/kervax-agent-amd64 . \
 && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
      -ldflags="-s -w -buildid= -X main.updatePubKeyB64=${AGENT_PUBKEY}" \
      -o /out/kervax-agent-arm64 . \
 && for a in amd64 arm64; do gzip -9 -c /out/kervax-agent-$a > /out/kervax-agent-$a.gz; done
# Сжатые копии - для самообновления агентов 2.18+: в 2.4 раза меньше, на канале за DPI это
# минуты вместо получаса. Подпись им не нужна: агент сверяет с манифестом распакованное.

# --- зависимости бэкенда из lock ---
# Ровно те версии и файлы, что прошли CI и pip-audit: backend/requirements.txt закрепляет
# версии и sha256, pip сверяет каждый файл. Отдельная стадия - кэш по содержимому lock:
# версия панели в pyproject.toml (меняется каждый релиз) и еженедельный слой apt ниже
# пакеты с PyPI заново не ставят (на панели в РФ это минуты). Новые версии приезжают
# только с новым lock - его готовит Dependabot, выпускает релиз.
# --only-binary: только колеса, никакого setup.py и непроверенных зависимостей сборки.
# --prefix: в итоговый образ уходит только установленное. .pyc компилируем с путями
# /usr/local, куда файлы лягут, иначе трейсбеки ссылались бы на /install.
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS pydeps
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --require-hashes --no-deps --only-binary :all: \
      --prefix=/install -r /tmp/requirements.txt \
 && python -m compileall -q -j 0 -s /install -p /usr/local /install

# --- бэкенд ---
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

# Пакеты ОС обновляются без выпуска: ops/deploy.sh передает DEPS_EPOCH - номер недели, и слой
# apt пересобирается раз в неделю (исправления openssl и т.п. приезжают не позже чем через
# неделю). Без DEPS_EPOCH (сборка руками) - по кэшу. Python-пакеты от DEPS_EPOCH не зависят.
# Непривилегированный пользователь. gosu нужен entrypoint'у: он стартует от root,
# выравнивает права на смонтированном томе и сбрасывает привилегии — su/sudo для
# этого не годятся, они плодят промежуточный процесс и ломают доставку сигналов
# первому процессу контейнера.
ARG DEPS_EPOCH=
RUN set -eu; \
    echo "runtime packages, week ${DEPS_EPOCH:-none}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends apt libapt-pkg7.0 gosu zlib1g libssl3t64 openssl; \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libssl3t64)" ge '3.5.7-1~deb13u3'; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -r -g 10001 kervax; \
    useradd -r -u 10001 -g kervax -d /srv -s /usr/sbin/nologin kervax; \
    gosu nobody true      # проверяем на сборке, а не в 3 часа ночи на проде

WORKDIR /srv

# Зависимости - до кода: готовая установка из стадии pydeps, без сети.
COPY --from=pydeps /install /usr/local

# Раздаваемое агентам - до кода панели: бинари меняются только с релизом агента, код
# панели - каждый выпуск. В таком порядке слои с ~22 МБ бинарей берутся из кэша, и
# выкладка их заново не собирает и не экспортирует.
# бинари агента, которые панель раздаёт (/api/agent/download|install.sh)
COPY --from=agent /out/kervax-agent-amd64 /out/kervax-agent-arm64 \
     /out/kervax-agent-amd64.gz /out/kervax-agent-arm64.gz /srv/agent/
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
COPY ops/windows-monitor.ps1 /srv/agent/

COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
# Пакет панели собирает hatchling из того же lock. pip check - lock полон для Python 3.15.
# pip нужен только при сборке. Его vendored-библиотеки не участвуют в работе
# панели, но иначе остаются в образе со своим независимым циклом обновлений.
# .pyc кода и миграций - здесь же: панель запускается из /srv, а писать туда не может.
RUN pip install --no-cache-dir --no-deps --no-build-isolation --no-index . \
 && python -m pip check \
 && python -m pip uninstall --yes pip \
 && python -m compileall -q -j 0 app alembic

# Код, миграции и раздаваемые агентам файлы принадлежат root, панель их только читает:
# ее процесс не может подменить ни свой код, ни бинарь, который скачает весь парк.
# Раньше здесь был chown -R kervax /srv - копия всего /srv отдельным слоем (+31 МБ на
# каждый выпуск) и десятки секунд сборки и экспорта на нагруженной панели. В рантайме
# панель пишет только в KERVAX_DATA_DIR (том /data, владельца выравнивает entrypoint);
# /srv/data - для запуска без него (по умолчанию ./data от /srv). Сборка падает, если
# пользователь панели не может что-то в /srv прочитать: прежний chown это скрывал.
RUN install -d -o kervax -g kervax /srv/data \
 && unreadable="$(gosu kervax find /srv ! -readable -print)" \
 && { [ -z "$unreadable" ] || { echo "kervax cannot read: $unreadable" >&2; exit 1; }; }

COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# без access-лога uvicorn: запросы с временем ответа пишет nginx фронта, здесь они дублировались
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log --no-proxy-headers --timeout-graceful-shutdown 10"]
