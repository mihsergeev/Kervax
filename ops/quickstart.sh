#!/bin/sh
# Kervax: развернуть панель на чистом сервере одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh
#
# Что делает: ставит Docker (если его нет), поднимает caddy-docker-proxy для TLS,
# клонирует репозиторий, придумывает секреты, поднимает панель и печатает адрес
# с паролем администратора.
#
# Домен по умолчанию — <публичный-IP>.sslip.io. sslip.io — публичный DNS, который
# отвечает адресом, записанным в самом имени: 203.0.113.10.sslip.io → 203.0.113.10.
# Свой домен ничего не требует, поэтому Let's Encrypt выдаёт на него сертификат,
# и панель сразу открывается по https. Для постоянной установки лучше свой домен:
#   ... | sudo sh -s -- --domain kervax.example.com
#
# Ключевые опции:
#   --domain <имя>       домен вместо <IP>.sslip.io
#   --allow-ips "A B"    пускать к панели только с этих адресов (см. ниже)
#   --build              собирать образы из исходников вместо готовых из GHCR
#   --dir <путь>         каталог установки (по умолчанию /opt/kervax)
set -eu

DOMAIN=""
ALLOW_IPS=""
BUILD=0
DIR=/opt/kervax
REPO=https://github.com/mihsergeev/Kervax.git

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --allow-ips) ALLOW_IPS="$2"; shift 2 ;;
        --build) BUILD=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
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
if [ -z "$DOMAIN" ]; then
    IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    [ -n "$IP" ] || IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    [ -n "$IP" ] || die "не смог определить внешний IP — задайте --domain"
    DOMAIN="$IP.sslip.io"
    say "Домен не задан, беру $DOMAIN"
    echo "  (sslip.io резолвится в $IP — Let's Encrypt выдаст на это имя сертификат)"
else
    say "Домен: $DOMAIN"
fi

# ── 4. Caddy ─────────────────────────────────────────────────────────────────
docker network inspect caddy >/dev/null 2>&1 || docker network create caddy >/dev/null
if docker ps --format '{{.Image}}' | grep -q caddy-docker-proxy; then
    say "caddy-docker-proxy уже запущен"
else
    say "Поднимаю caddy-docker-proxy (TLS и сертификаты)"
    mkdir -p /srv/caddy
    cat > /srv/caddy/compose.yml <<'YML'
services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:2.10-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      CADDY_INGRESS_NETWORKS: caddy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./data:/data
    networks: [caddy]
networks:
  caddy:
    external: true
YML
    (cd /srv/caddy && docker compose up -d >/dev/null) || die "caddy не поднялся"
fi

# ── 5. Репозиторий ───────────────────────────────────────────────────────────
if [ -d "$DIR/.git" ]; then
    say "Обновляю $DIR"
    git -C "$DIR" pull --ff-only >/dev/null || die "git pull не прошёл"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "каталог $DIR не пуст и не является репозиторием Kervax.
  Уберите его (rm -rf $DIR) или поставьте панель в другой: --dir /путь"
else
    say "Клонирую в $DIR"
    command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq git) >/dev/null 2>&1
    # Клонируем рядом и переносим готовое: прерванный на середине клон иначе
    # оставляет непустой каталог без .git, после которого повторный запуск
    # невозможен — ни склонировать (занято), ни обновить (не репозиторий).
    rm -rf "$DIR.tmp"
    git clone -q "$REPO" "$DIR.tmp" || { rm -rf "$DIR.tmp"; die "не удалось клонировать $REPO"; }
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
FILES=$(printf -- '-f %s ' $(echo "$COMPOSE_LIST" | tr ':' ' '))

if [ -f .env ]; then
    say ".env уже есть — оставляю как есть"
    ADMIN_PW=$(grep '^KERVAX_ADMIN_PASSWORD=' .env | cut -d= -f2-)
else
    say "Готовлю .env со случайными секретами"
    cp .env.example .env
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

# ── 7. Запуск ────────────────────────────────────────────────────────────────
if [ "$BUILD" = "1" ]; then
    say "Собираю образы из исходников (несколько минут, нужно ~2 ГБ памяти)"
    docker compose $FILES up -d --build || die "сборка не удалась"
else
    say "Тяну готовые образы из GHCR"
    # --pull always обязателен: без него повторный запуск поднимает образ,
    # который уже лежит локально, и «обновление» обновляет только файлы
    # репозитория. Панель при этом остаётся прежней версии — молча.
    docker compose $FILES up -d --pull always || die "запуск не удался"
fi

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
code=""
if [ -z "$ALLOW_IPS" ]; then
    i=0
    while [ $i -lt 30 ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/health" --max-time 5 || true)
        [ "$code" = "200" ] && break
        i=$((i + 1))
        sleep 2
    done
fi

# Let's Encrypt считает выпуски по НАБОРУ имён: пять штук на одно и то же имя за
# 168 часов. Переустановка панели с удалением каталога caddy выбрасывает уже
# выданный сертификат и просит новый — на шестой раз выпуск запрещён, и панель
# молчит без единого объяснения. Сказать об этом прямо дешевле, чем гадать.
CADDY_CT=$(docker ps --format '{{.Names}} {{.Image}}' | grep caddy-docker-proxy | head -1 | cut -d' ' -f1)
RATE_LIMITED=0
if [ -n "$CADDY_CT" ] && docker logs "$CADDY_CT" --since 10m 2>&1 | grep -q rateLimited; then
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
if [ -z "$ALLOW_IPS" ]; then
    printf '\n\033[33m  ВНИМАНИЕ: панель открыта всему интернету (список разрешённых\n'
    printf '  адресов пуст) — её защищает только пароль. Ограничьте доступ:\n'
    printf '    KERVAX_ALLOW_IPS="ваш.ip" в %s/.env, затем\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
fi
printf '\033[32m════════════════════════════════════════════════════════\033[0m\n\n'
printf 'Дальше: смените пароль, включите 2FA в меню ⚙, добавьте первый сервер\n'
printf 'кнопкой «Добавить сервер» — панель покажет команду установки агента.\n'
