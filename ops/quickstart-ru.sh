#!/bin/sh
# Kervax: развернуть панель на чистом сервере одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart-ru.sh | sudo sh
#
# Что делает: ставит Docker (если его нет), поднимает caddy-docker-proxy для TLS,
# клонирует репозиторий, придумывает секреты, поднимает панель и печатает адрес
# с паролем администратора.
#
# Из main берется только этот скрипт. Репозиторий ставится на тег последнего выпуска,
# и образы закрепляются на нем же (KERVAX_VERSION в .env): файлы и образы - одна
# выпущенная версия, и коммит в main попадает в установку только через выпуск.
# Повторный запуск обновляет до самого нового выпуска.
#
# Домен по умолчанию — <публичный-IP>.sslip.io. sslip.io — публичный DNS, который
# отвечает адресом, записанным в самом имени: 203.0.113.10.sslip.io → 203.0.113.10.
# Свой домен ничего не требует, поэтому Let's Encrypt выдаёт на него сертификат,
# и панель сразу открывается по https. Для постоянной установки лучше свой домен:
#   ... | sudo sh -s -- --domain kervax.example.com
#
# Сервер в локальной сети (ВМ для стенда, домашняя сеть) Let's Encrypt не видит, а
# домашний роутер часто вообще не резолвит <приватный-IP>.sslip.io (защита от DNS
# rebinding). Для этого случая:
#   ... | sudo sh -s -- --local-ca
# Панель открывается по https://<IP-в-локалке>, сертификат от внутреннего CA caddy,
# а команда установки агента в панели везет с собой корень этого CA.
#
# Ключевые опции:
#   --domain <имя>       домен вместо <IP>.sslip.io
#   --local-ca           локальная сеть: внутренний CA caddy вместо Let's Encrypt,
#                        адрес в локалке вместо sslip.io (docs/install.ru.md)
#   --allow-ips "A B"    пускать к панели только с этих адресов (см. ниже)
#   --build              собирать образы из исходников вместо готовых из GHCR
#   --dir <путь>         каталог установки (по умолчанию /opt/kervax)
#   --version <vX.Y.Z>   этот выпуск вместо последнего
#   --allow-downgrade    пустить повторный запуск на более старый выпуск (см. шаг 5)
#
# Это русская версия скрипта: она отличается только языком сообщений.
# Английская — ops/quickstart.sh.
set -eu

DOMAIN=""
ALLOW_IPS=""
BUILD=0
LOCAL_CA=0
DIR=/opt/kervax
VERSION=""
ALLOW_DOWNGRADE=0
REPO=https://github.com/mihsergeev/Kervax.git

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --allow-ips) ALLOW_IPS="$2"; shift 2 ;;
        --build) BUILD=1; shift ;;
        --local-ca) LOCAL_CA=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
        -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "Запускайте от root (sudo)." >&2; exit 1; }

say() { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Скрипт часто запускают сразу после сноса панели — из каталога, которого уже
# нет. Тогда git и docker падают невнятным «Unable to read current working
# directory», а скрипт объявляет, что не смог склонировать репозиторий: причина
# названа неверно, и искать её будут не там. Уходим в корень, предварительно
# развернув относительный --dir, пока текущий каталог ещё можно прочитать.
case "$DIR" in
    /*) ;;
    *) DIR="$(pwd 2>/dev/null || echo /root)/$DIR" ;;
esac
cd / || die "не удалось перейти в /"

# ── 1. Порты ─────────────────────────────────────────────────────────────────
# Порты 80/443 нужны Caddy для выпуска сертификата. Занятый порт — самая частая
# причина «сертификат не выпустился». Проверяем ДО установки Docker: иначе на машине,
# где ставить нечего, всё равно оставался бы установленный Docker.
for port in 80 443; do
    if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
        # свой же caddy на этом порту помехой не является, но docker может быть
        # ещё не установлен — тогда порт занят точно не нами
        { command -v docker >/dev/null 2>&1 &&
          docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":$port->"; } ||
            die "порт $port занят другим процессом — освободите его и повторите"
    fi
done

# ── 2. Docker ────────────────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
    say "Docker уже есть: $(docker --version)"
else
    say "Ставлю Docker"
    curl -fsSL https://get.docker.com | sh >/dev/null || die "не удалось поставить Docker"
    echo "  $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || die "нужен Docker Compose v2 (плагин compose)"

# ── 3. Домен ─────────────────────────────────────────────────────────────────
# Повторный запуск (обновление: quickstart без аргументов) оставляет то, что выбрал первый.
# Иначе панель, поставленная с --local-ca, переключалась обратно на Let's Encrypt и на
# <публичный-IP>.sslip.io, и адрес, по которому ее все открывают, переставал отвечать.
if [ -f "$DIR/.env" ]; then
    grep -q '^COMPOSE_FILE=.*compose\.localca\.yml' "$DIR/.env" && LOCAL_CA=1
    [ -n "$DOMAIN" ] || DOMAIN=$(grep '^KERVAX_DOMAIN=' "$DIR/.env" | cut -d= -f2-)
fi
if [ -z "$DOMAIN" ] && [ "$LOCAL_CA" = "1" ]; then
    # Адрес панели - сам IP в локалке: нет имени, значит нечего прописывать в /etc/hosts
    # на каждой машине и нечего блокировать роутеру. Внутренний CA caddy выдает
    # сертификат и на IP-адрес.
    IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
    [ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$IP" ] || die "не смог определить адрес в локальной сети, задайте --domain"
    DOMAIN="$IP"
    say "Локальная сеть, адрес панели - IP: $DOMAIN"
    echo "  (сертификат от внутреннего CA caddy, подробности в конце вывода)"
elif [ -z "$DOMAIN" ]; then
    IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    [ -n "$IP" ] || IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    [ -n "$IP" ] || die "не смог определить внешний IP — задайте --domain"
    DOMAIN="$IP.sslip.io"
    say "Домен не задан, беру $DOMAIN"
    echo "  (sslip.io резолвится в $IP — Let's Encrypt выдаст на это имя сертификат)"
else
    say "Домен: $DOMAIN"
    if [ "$LOCAL_CA" = "1" ]; then
        echo "  (внутренний CA caddy. Имя должно резолвиться в панель на каждой машине"
        echo "  с агентом и на вашем компьютере: локальный DNS или /etc/hosts, см. docs/install.ru.md)"
    fi
fi

# ── 4. Caddy ─────────────────────────────────────────────────────────────────
# --ipv6 здесь не для красоты. В сети без IPv6 docker проносит IPv6-соединения
# через свой userland-прокси, и caddy видит не клиента, а адрес шлюза моста: белый
# список по IP перестаёт различать кого-либо и пускает всех, кто пришёл по IPv6.
# С IPv6 в сети порт пробрасывается через ip6tables и адрес доходит настоящим.
# Старый docker, который так не умеет, откатывается на IPv4 - тогда порты ниже
# публикуются только на IPv4, и по IPv6 до caddy никто не доходит.
if ! docker network inspect caddy >/dev/null 2>&1; then
    docker network create --ipv6 caddy >/dev/null 2>&1 || docker network create caddy >/dev/null
fi
NET_IPV6=$(docker network inspect caddy -f '{{.EnableIPv6}}' 2>/dev/null)
if [ "$NET_IPV6" = "true" ]; then
    CADDY_PORTS='["80:80", "443:443"]'
else
    CADDY_PORTS='["0.0.0.0:80:80", "0.0.0.0:443:443"]'
fi
if docker ps --format '{{.Image}}' | grep -q caddy-docker-proxy; then
    say "caddy-docker-proxy уже запущен"
    if [ "$NET_IPV6" != "true" ] && ip -6 addr show scope global 2>/dev/null | grep -q inet6; then
        say "ВНИМАНИЕ: сеть caddy без IPv6, а у хоста есть публичный IPv6"
        echo "  Клиент по IPv6 приходит к caddy с адреса шлюза моста, и белый список"
        echo "  по IP его не остановит. Либо публикуйте порты caddy только на 0.0.0.0,"
        echo "  либо пересоздайте сеть с IPv6: остановить все стеки в ней, затем"
        echo "  docker network rm caddy && docker network create --ipv6 caddy."
    fi
else
    say "Поднимаю caddy-docker-proxy (TLS и сертификаты)"
    mkdir -p /srv/caddy
    # caddy смотрит в интернет, поэтому docker-сокет ему не даем (дыра в caddy была бы root
    # на хосте): список контейнеров, сети и события он читает через прокси только на
    # чтение, и подключаться к нему может только caddy
    DGID=$(stat -c %g /var/run/docker.sock)
    cat > /srv/caddy/compose.yml <<'YML'
services:
  caddy:
    image: local/kervax-caddy-docker-proxy:2.11.9
    pull_policy: build
    build:
      context: .
      dockerfile_inline: |
        FROM caddy:2.11.9-builder-alpine@sha256:646fee578e680989ca5c7fd1ce6f1de574d19fd71ef2221810cc0510a9f40c86 AS xcaddy
        FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
        COPY --from=xcaddy /usr/bin/xcaddy /usr/bin/xcaddy
        RUN GOTOOLCHAIN=local xcaddy build v2.11.9 --output /out/caddy \
              --with github.com/lucaslorentz/caddy-docker-proxy/v2@v2.13.1 \
              --replace golang.org/x/net=golang.org/x/net@v0.62.0
        FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
        RUN apk upgrade --no-cache
        COPY --from=build /out/caddy /usr/bin/caddy
        CMD ["caddy", "docker-proxy"]
    restart: unless-stopped
    ports: __PORTS__
    environment:
      CADDY_INGRESS_NETWORKS: caddy
      DOCKER_HOST: tcp://docker-api:2375
    volumes:
      - ./data:/data
    networks: [caddy, docker-api]
    depends_on: [docker-api]
  docker-api:
    image: wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848
    restart: unless-stopped
    user: "65534:__DGID__"
    read_only: true
    mem_limit: 64M
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [docker-api]
    command:
      - -loglevel=warn
      - -listenip=0.0.0.0
      - -allowfrom=caddy
      - -shutdowngracetime=5
      - -watchdoginterval=600
      - -stoponwatchdog
      - -allowHEAD=/_ping
      - -allowGET=/(v[0-9]+\.[0-9]+/)?(_ping|version|info|events|containers/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)
networks:
  caddy:
    external: true
  docker-api:
    internal: true
YML
    sed -i "s|__PORTS__|$CADDY_PORTS|; s|__DGID__|$DGID|" /srv/caddy/compose.yml
    (cd /srv/caddy && docker compose up -d >/dev/null) || die "caddy не поднялся"
fi

# ── 5. Репозиторий ───────────────────────────────────────────────────────────
# Выпуск, а не main: скрипты ниже работают от root, и образы должны им соответствовать.
# Последний выпуск - старший стабильный тег vX.Y.Z, у которого образы уже в реестре: в этом
# порядке ops/release-latest.sh двигает образы latest. "Последний релиз" GitHub не используется:
# бэкпорт, опубликованный после более новой версии, становится им и увел бы панель назад. Тег
# появляется за минуты до своих образов (Release workflow), а упавший workflow оставляет тег без
# них насовсем: такой тег пропускается, пока в $DIR и .env ничего не изменено.
# --build собирает образы сам и берет старший тег.
command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq git) >/dev/null 2>&1
IMAGES=ghcr.io/mihsergeev      # владелец в нижнем регистре, как публикует release.yml
stable_tags() {                # стабильные теги репозитория, старший первым
    git ls-remote --tags --refs "$REPO" 'refs/tags/v*' 2>/dev/null | cut -f2 | sed -n 's#^refs/tags/##p' \
        | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
        | sort -t . -k 1.2,1nr -k 2,2nr -k 3,3nr
}
published() {                  # оба образа выпуска $1 есть в реестре
    docker manifest inspect "$IMAGES/kervax-backend:${1#v}" >/dev/null 2>&1 &&
        docker manifest inspect "$IMAGES/kervax-frontend:${1#v}" >/dev/null 2>&1
}
# ver_lt A B: версия A (без v) ниже B, по числам; суффиксы пре-релизов не учитываются
ver_lt() {
    [ "${1%%-*}" != "${2%%-*}" ] &&
        [ "$(printf '%s\n%s\n' "${1%%-*}" "${2%%-*}" | sort -t . -k 1,1n -k 2,2n -k 3,3n | head -n 1)" = "${1%%-*}" ]
}
if [ -n "$VERSION" ]; then
    TAG="v${VERSION#v}"
    # значение целиком, а не построчно: перевод строки протащил бы мимо grep что угодно
    case "$TAG" in *[!0-9A-Za-z.-]*) TAG="" ;; esac
    printf '%s\n' "$TAG" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' \
        || die "--version должен выглядеть как vX.Y.Z"
    [ "$BUILD" = 1 ] || published "$TAG" || die "образов $TAG нет в $IMAGES (еще не опубликованы или ghcr.io недоступен?).
  Выберите другой выпуск, подождите несколько минут после свежего тега или соберите из исходников: --build"
else
    TAG=""
    n=0
    for t in $(stable_tags); do
        if [ "$BUILD" = 1 ] || published "$t"; then TAG=$t; break; fi
        echo "  $t: образов в $IMAGES (еще) нет, смотрю предыдущий выпуск"
        n=$((n + 1))
        [ "$n" -lt 5 ] || break
    done
    [ -n "$TAG" ] || [ "$n" = 0 ] || die "ни у одного из последних $n выпусков нет образов в $IMAGES: доступен ли ghcr.io?
  Или соберите из исходников: --build"
    [ -n "$TAG" ] || die "не удалось получить выпуски с github.com: доступен ли он? Или укажите --version vX.Y.Z"
fi
if [ -d "$DIR/.git" ]; then
    say "Обновляю $DIR до $TAG"
    # только этот тег: изменившийся на сервере тег отвергается, а не перенимается
    git -C "$DIR" fetch -q origin "refs/tags/$TAG:refs/tags/$TAG" >/dev/null || die "не удалось получить $TAG в $DIR"
    # Назад сам не идет: базу уже мигрировал более новый код, и старый бэкенд падает на старте
    # на незнакомой ревизии (alembic) и перезапускается по кругу.
    CUR=$(grep '^KERVAX_VERSION=' "$DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2-)
    printf '%s\n' "$CUR" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' || CUR=""
    BACK=""
    if [ -n "$CUR" ]; then
        # установленный выпуск известен: важен только его номер (обновление с бэкпорт-выпуска -
        # не потомок более нового тега, и это нормально)
        ver_lt "${TAG#v}" "$CUR" && BACK="$TAG старше установленной $CUR.
  В базе может быть уже более новая схема, и старая панель на ней не запустится.
  --allow-downgrade - только вместе с бэкапом базы $TAG для восстановления"
    elif grep '^COMPOSE_FILE=' "$DIR/.env" 2>/dev/null | grep -q 'compose\.ghcr\.yml'; then
        # Поставлена из main до закрепления на выпусках, на образах latest: база идет за образами
        # (старший выпуск), а не за файлами в $DIR. Последний выпуск с образами - он и есть;
        # явно указанный старый может оказаться ниже.
        TOP=$(stable_tags | head -n 1)
        if [ -n "$VERSION" ] && [ -n "$TOP" ] && ver_lt "${TAG#v}" "${TOP#v}"; then
            BACK="$TAG старше $TOP - последних образов, на которых могла работать панель.
  --allow-downgrade - только вместе с бэкапом базы $TAG для восстановления"
        else
            echo "  Панель работала на образах latest: с этого запуска она закреплена на $TAG."
        fi
    elif ! git -C "$DIR" merge-base --is-ancestor HEAD "refs/tags/$TAG" 2>/dev/null; then
        # собрана из исходников коммита, которого нет в $TAG: его миграции могут быть в базе
        BACK="$DIR собран из коммита $(git -C "$DIR" rev-parse --short HEAD 2>/dev/null), которого нет в $TAG,
  и в базе могут быть миграции новее $TAG. Дождитесь выпуска, который содержит этот коммит, и
  запустите еще раз. Если его не будет (коммит ушел из main), восстановите бэкап, сделанный на
  $TAG, и укажите --allow-downgrade"
    fi
    if [ -n "$BACK" ]; then
        [ "$ALLOW_DOWNGRADE" = 1 ] || die "назад не иду: $BACK."
        say "ВНИМАНИЕ: откат по --allow-downgrade: $BACK"
        echo "  Схема базы новее $TAG останавливает бэкенд на старте: восстановите подходящий бэкап."
    fi
    git -C "$DIR" checkout -q --detach "refs/tags/$TAG" >/dev/null || die "не удалось переключить $DIR на $TAG"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "каталог $DIR не пуст и не является репозиторием Kervax.
  Уберите его (rm -rf $DIR) или поставьте панель в другой: --dir /путь"
else
    say "Клонирую $TAG в $DIR"
    # Клонируем рядом и переносим готовое: прерванный на середине клон иначе
    # оставляет непустой каталог без .git, после которого повторный запуск
    # невозможен — ни склонировать (занято), ни обновить (не репозиторий).
    rm -rf "$DIR.tmp"
    git clone -q --branch "$TAG" "$REPO" "$DIR.tmp" || { rm -rf "$DIR.tmp"; die "не удалось клонировать $TAG из $REPO"; }
    rm -rf "$DIR"
    mv "$DIR.tmp" "$DIR"
fi
cd "$DIR"

# ── 6. Настройки ─────────────────────────────────────────────────────────────
# Набор оверлеев решаем ЗДЕСЬ, потому что его нужно записать в .env (см. ниже).
if [ "$BUILD" = "1" ]; then
    COMPOSE_LIST="compose.yml:compose.caddy.yml"
else
    COMPOSE_LIST="compose.yml:compose.ghcr.yml:compose.caddy.yml"
fi
[ "$LOCAL_CA" = "1" ] && COMPOSE_LIST="$COMPOSE_LIST:compose.localca.yml"
FILES=$(printf -- '-f %s ' $(echo "$COMPOSE_LIST" | tr ':' ' '))

if [ -f .env ]; then
    say ".env уже есть — оставляю как есть"
    ADMIN_PW=$(grep '^KERVAX_ADMIN_PASSWORD=' .env | cut -d= -f2-)
else
    say "Готовлю .env со случайными секретами"
    # 0600 сразу: с обычным umask копия была читаема всем до chmod ниже, а секреты
    # пишутся в нее как раз между ними
    (umask 077 && cp .env.example .env)
    ADMIN_PW=$(openssl rand -base64 18)
    set_env() { sed -i "s|^$1=.*|$1=$2|" .env; }
    set_env KERVAX_ADMIN_PASSWORD "$ADMIN_PW"
    set_env KERVAX_JWT_SECRET "$(openssl rand -hex 32)"
    set_env KERVAX_DB_PASSWORD "$(openssl rand -hex 24)"
    set_env KERVAX_DOMAIN "$DOMAIN"
    set_env KERVAX_PANEL_URL "https://$DOMAIN"
    set_env KERVAX_ALLOW_IPS "$ALLOW_IPS"
    chmod 600 .env
fi

# COMPOSE_FILE прописываем ВСЕГДА, в том числе в уже существующий .env. Без него
# «docker compose up -d» в этом каталоге видит только базовый compose.yml: тянет
# не готовые образы, а сборку из исходников, и пересоздаёт контейнеры без сети
# caddy и без его лейблов — панель молча пропадает со своего домена. Ровно это и
# делает документированная команда обновления, у которой нет флагов -f.
if grep -q '^COMPOSE_FILE=' .env; then
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$COMPOSE_LIST|" .env
else
    printf 'COMPOSE_FILE=%s\n' "$COMPOSE_LIST" >> .env
fi

# KERVAX_VERSION прописываем тоже ВСЕГДА: он закрепляет образы (compose.ghcr.yml) на
# выпуске, на который поставлен репозиторий выше. Без него образы идут за latest, и
# файлы панели расходятся с ее образами.
if grep -q '^KERVAX_VERSION=' .env; then
    sed -i "s|^KERVAX_VERSION=.*|KERVAX_VERSION=${TAG#v}|" .env
else
    printf 'KERVAX_VERSION=%s\n' "${TAG#v}" >> .env
fi

# KERVAX_PROXY_GATEWAY прописываем тоже ВСЕГДА. Этот один адрес пускается в
# caddy-оверлее, чтобы работала локальная проверка: агент проверяет сайт за белым
# списком изнутри своего же сервера, docker публикует порт через свой прокси, и
# caddy видит шлюз своей сети, а не loopback. Один адрес, а не подсеть
# 172.16.0.0/12: подсеть пустила бы и соседние контейнеры, а в сети без IPv6 - всех,
# кто пришёл по IPv6.
PROXY_GW=$(docker network inspect caddy \
    -f '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' 2>/dev/null \
    | sed 's/ *$//')
if grep -q '^KERVAX_PROXY_GATEWAY=' .env; then
    sed -i "s|^KERVAX_PROXY_GATEWAY=.*|KERVAX_PROXY_GATEWAY=$PROXY_GW|" .env
else
    printf 'KERVAX_PROXY_GATEWAY=%s\n' "$PROXY_GW" >> .env
fi

# ── 7. Запуск ────────────────────────────────────────────────────────────────
sh "$DIR/ops/agent-proxy-peers-setup.sh" "$DIR" $FILES || die "не удалось подготовить адреса прокси"
if [ "$BUILD" = "1" ]; then
    say "Собираю образы из исходников (несколько минут, нужно ~2 ГБ памяти)"
    docker compose $FILES up -d --build || die "сборка не удалась"
else
    say "Тяну готовые образы из GHCR"
    # --pull always обязателен: без него повторный запуск поднимает образ,
    # который уже лежит локально, и «обновление» обновляет только файлы
    # репозитория. Панель при этом остаётся прежней версии — молча.
    docker compose $FILES up -d --pull always || die "запуск не удался (образы выпуска появляются через несколько минут после его тега)"
fi

sh "$DIR/ops/agent-proxy-peers-setup.sh" "$DIR" $FILES || die "не удалось обновить адреса прокси"
say "Жду, пока панель ответит"
# Спрашиваем панель ИЗНУТРИ, а не по публичному адресу. Снаружи мы обращались бы
# с адреса самого сервера, а список разрешённых (--allow-ips) его не содержит —
# caddy обрывает такое соединение (abort), и проверка «не ответила» всегда, хотя
# панель работает. Внутренний health говорит ровно то, что нам тут нужно:
# приложение поднялось и база отвечает.
ready=0
i=0
while [ $i -lt 90 ]; do
    # </dev/null здесь ОБЯЗАТЕЛЕН. Скрипт запускают как «curl … | sh»: сам текст
    # скрипта приходит на stdin и дочитывается по мере выполнения. docker compose
    # exec стандартный ввод читает (и -T этого не отменяет), поэтому съедает
    # непрочитанный остаток — sh обрывается на середине строки с «Unterminated
    # quoted string». Из файла всё работает, ломается ровно главный сценарий.
    if docker compose $FILES exec -T backend \
        python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" \
        </dev/null >/dev/null 2>&1; then
        ready=1
        break
    fi
    i=$((i + 1))
    sleep 2
done

# Публичный адрес проверяем, только когда панель открыта всем: иначе запрос с
# сервера обречён (см. выше). Это заодно проверка сертификата.
CADDY_CT=$(docker ps --format '{{.Names}} {{.Image}}' | grep caddy-docker-proxy | head -1 | cut -d' ' -f1)

# Локальная сеть: корень внутреннего CA caddy кладем в data/panel-ca.crt. Панель вставляет
# его в команду установки агента (install.sh --ca-file доверяет ему на ноде), а проверка https
# ниже проверяет панель этим корнем, а не пропускает проверку. caddy создает CA, когда грузит
# сайт с `tls internal`, через несколько секунд после появления контейнера панели. Читаем
# через docker: caddy может быть чужим, и его данные не в /srv/caddy.
CA_FILE="$DIR/data/panel-ca.crt"
CURL_CA=""
if [ "$LOCAL_CA" = "1" ]; then
    say "Беру корень внутреннего CA caddy"
    i=0
    root=""
    while [ $i -lt 30 ] && [ -n "$CADDY_CT" ]; do
        # </dev/null: см. заметку про docker compose exec выше
        root=$(docker exec "$CADDY_CT" cat /data/caddy/pki/authorities/local/root.crt </dev/null 2>/dev/null || true)
        case "$root" in *"BEGIN CERTIFICATE"*) break ;; esac
        root=""
        i=$((i + 1))
        sleep 2
    done
    if [ -n "$root" ]; then
        mkdir -p "$DIR/data"
        # в data/ пишет контейнер панели, он уже запущен: случайное имя создается с O_EXCL
        # (noclobber) и правами 0644 от umask, без фиксированного .tmp, который подменят ссылкой
        ca_tmp=$(mktemp -u "$CA_FILE.XXXXXXXXXX") && (umask 022 && set -C && printf '%s\n' "$root" > "$ca_tmp") \
            && mv -fT "$ca_tmp" "$CA_FILE"
        CURL_CA="--cacert $CA_FILE"
        # имя проверяем на этом же сервере: записи в DNS у него может и не быть
        case "$DOMAIN" in *[!0-9.]*) CURL_CA="$CURL_CA --resolve $DOMAIN:443:127.0.0.1" ;; esac
        echo "  $CA_FILE"
    else
        echo "  caddy его еще не создал: docker logs ${CADDY_CT:-<контейнер caddy>} --tail=50"
    fi
fi

code=""
if [ -z "$ALLOW_IPS" ]; then
    i=0
    while [ $i -lt 30 ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' $CURL_CA "https://$DOMAIN/api/health" --max-time 5 || true)
        [ "$code" = "200" ] && break
        i=$((i + 1))
        sleep 2
    done
fi

# Let's Encrypt считает выпуски по НАБОРУ имён: пять штук на одно и то же имя за
# 168 часов. Переустановка панели с удалением каталога caddy выбрасывает уже
# выданный сертификат и просит новый — на шестой раз выпуск запрещён, и панель
# молчит без единого объяснения. Сказать об этом прямо дешевле, чем гадать.
RATE_LIMITED=0
if [ "$LOCAL_CA" != "1" ] && [ -n "$CADDY_CT" ] && docker logs "$CADDY_CT" --since 10m 2>&1 | grep -q rateLimited; then
    RATE_LIMITED=1
fi

printf '\n\033[32m════════════════════════════════════════════════════════\033[0m\n'
if [ "$RATE_LIMITED" = "1" ]; then
    printf '  Панель работает, но сертификат на %s\n' "$DOMAIN"
    printf '  \033[33mне выпущен: Let'"'"'s Encrypt отказал по лимиту\033[0m (5 сертификатов на одно\n'
    printf '  имя за 168 часов — обычно так выходит после переустановок).\n'
    printf '  Что делать: взять другое имя (--domain other.%s)\n' "$DOMAIN"
    printf '  или подождать снятия лимита. Точное время — в логах:\n'
    printf '    docker logs %s | grep rateLimited\n' "$CADDY_CT"
    printf '  На будущее: не удаляйте /srv/caddy/data — там лежат сертификаты.\n'
elif [ "$ready" = "1" ] && { [ -z "$ALLOW_IPS" ] && [ "$code" = "200" ] || [ -n "$ALLOW_IPS" ]; }; then
    printf '  Панель готова:  \033[1mhttps://%s\033[0m\n' "$DOMAIN"
    if [ -n "$ALLOW_IPS" ]; then
        printf '  Открывать с разрешённых адресов: %s\n' "$ALLOW_IPS"
        printf '  С самого сервера она намеренно не отвечает — его в списке нет.\n'
    fi
elif [ "$ready" = "1" ]; then
    printf '  Панель работает, но https ещё не ответил (первый сертификат\n'
    printf '  выпускается до минуты). Адрес: https://%s\n' "$DOMAIN"
    printf '  Если через пару минут пусто: docker compose %s logs caddy --tail=50\n' "$FILES"
else
    printf '  Контейнеры подняты, но панель пока не отвечает изнутри.\n'
    printf '  Адрес: https://%s\n' "$DOMAIN"
    printf '  Смотрите: docker compose %s logs --tail=50\n' "$FILES"
fi
printf '  Логин:   \033[1madmin\033[0m\n'
printf '  Пароль:  \033[1m%s\033[0m\n' "$ADMIN_PW"
printf '  Он же лежит в %s/.env\n' "$DIR"
if [ "$LOCAL_CA" = "1" ]; then
    printf '\n  Сертификат выдал внутренний CA caddy, ему пока никто не доверяет.\n'
    if [ -f "$CA_FILE" ]; then
        FP=$(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
        printf '  Его корень: %s\n' "$CA_FILE"
        [ -n "$FP" ] && printf '  SHA-256:    %s\n' "$FP"
        printf '  Агенты: руками ничего не нужно. Команда установки в панели везет этот\n'
        printf '  корень, и install.sh добавляет его в системное хранилище ноды.\n'
        printf '  Браузер: импортируйте корень как доверенный центр, панель отдает его по\n'
        printf '    https://%s/api/agent/ca.crt (или скопируйте файл выше).\n' "$DOMAIN"
        printf '  Любая другая машина с Debian/Ubuntu (curl, скрипты):\n'
        printf '    cp panel-ca.crt /usr/local/share/ca-certificates/kervax-panel.crt && update-ca-certificates\n'
    fi
    case "$DOMAIN" in
        *[!0-9.]*)
            printf '  Имя %s должно резолвиться на каждой машине с агентом и на вашем\n' "$DOMAIN"
            printf '  компьютере: локальный DNS или строка в /etc/hosts (см. docs/install.ru.md).\n' ;;
    esac
fi
if [ -z "$ALLOW_IPS" ] && [ "$LOCAL_CA" = "1" ]; then
    printf '\n\033[33m  ВНИМАНИЕ: к панели пускают всю локальную сеть, ее защищает только\n'
    printf '  пароль. Ограничьте доступ:\n'
    printf '    KERVAX_ALLOW_IPS="ваш.ip" в %s/.env, затем\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
elif [ -z "$ALLOW_IPS" ]; then
    printf '\n\033[33m  ВНИМАНИЕ: панель открыта всему интернету (список разрешённых\n'
    printf '  адресов пуст) — её защищает только пароль. Ограничьте доступ:\n'
    printf '    KERVAX_ALLOW_IPS="ваш.ip" в %s/.env, затем\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
fi
printf '\033[32m════════════════════════════════════════════════════════\033[0m\n\n'
printf 'Дальше: смените пароль, включите 2FA в меню ⚙, добавьте первый сервер\n'
printf 'кнопкой «Добавить сервер» — панель покажет команду установки агента.\n'
