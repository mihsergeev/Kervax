#!/bin/sh
# Установка Kervax-агента. Вызывается: install.sh <url_панели> <токен> [инстанс]
# Ставит статический бинарь в /opt/kervax-agent/bin (владелец kervax — чтобы агент мог
# БЕЗ root безопасно обновлять сам себя), конфиг в /etc, systemd-юнит.
# Агент работает под непривилегированным юзером kervax (без шелла), только исходящие.
#
# [инстанс] — для ВТОРОГО агента на том же сервере (метрики в другую панель):
#   install.sh https://panel2.example.com <токен2> panel2
# → конфиг /etc/kervax-agent-panel2.conf + юнит kervax-agent@panel2 (template).
# Бинарь общий; инстансы полностью независимы (свой url/token/процесс).
#
# Снять агента с ноды:
#   install.sh --uninstall            всё: сервис, хелперы, бинарь, конфиги, юзер
#   install.sh --uninstall panel2     только этот инстанс
#
# У панели с сертификатом не от публичного CA (quickstart --local-ca: внутренний CA caddy
# в локальной сети) команда идет с --ca-file <корень.pem>: корень добавляется в системное
# хранилище, и агент, helper'ы и curl проверяют панель им. Проверка сертификата не
# отключается никогда.
#
# Нода, чей адрес режут на пути к панели (IP в списке блокировок у провайдера панели),
# ставится через прокси: --proxy http://логин:пароль@хост:порт (или https://, socks5://).
# Через него установщик качает агента, а агент получает proxy= в конфиге и тоже шлет
# в панель все через него.
#
# Это русская версия установщика: отличается только языком сообщений.
# Английская — agent/install.sh, её же раздаёт панель по /api/agent/install.sh.
#
# Весь скрипт - один блок { ... }: sh дочитывает составную команду до конца и только потом
# выполняет. Если curl отдал установщик не целиком (канал завис или DPI оборвал передачу
# посреди файла), обрезанный скрипт не выполнится вовсе, а не отработает первой половиной
# и не будет вечно ждать остаток.
{
set -eu

# Разбор аргументов: позиционные <url> <токен> [инстанс] + флаги.
# По умолчанию (авто-режим) install.sh включает ВСЁ применимое на ноде: bounded-доступ
# к Docker (socket-proxy), read-only Kubernetes (узкий SA), статистику сервера бэкапов,
# управление restic-бэкапом. Каждый шаг самодетектируется. Флаг --no-auto — только агент.
# --ca-file <путь>: корень, которым подписан сертификат панели (см. шаг доверия ниже)
# --proxy <url>: прокси до панели для установщика и агента (см. шапку)
URL=""; TOKEN=""; INSTANCE=""; WANT_DOCKER=0; NO_AUTO=0; UNINSTALL=0; CA_FILE=""; WANT_CA=0
# KERVAX_PROXY в окружении - то же, что --proxy: так его передает команда установки из панели
PROXY="${KERVAX_PROXY:-}"; WANT_PROXY=0
for a in "$@"; do
  if [ "$WANT_CA" = 1 ]; then CA_FILE="$a"; WANT_CA=0; continue; fi
  if [ "$WANT_PROXY" = 1 ]; then PROXY="$a"; WANT_PROXY=0; continue; fi
  case "$a" in
    --ca-file) WANT_CA=1 ;;
    --ca-file=*) CA_FILE="${a#--ca-file=}" ;;
    --proxy) WANT_PROXY=1 ;;
    --proxy=*) PROXY="${a#--proxy=}" ;;
    --docker) WANT_DOCKER=1 ;;
    --no-auto) NO_AUTO=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *)
      if [ -z "$URL" ]; then URL="$a"
      elif [ -z "$TOKEN" ]; then TOKEN="$a"
      elif [ -z "$INSTANCE" ]; then INSTANCE="$a"
      fi ;;
  esac
done
# Команда из панели передает токен в KERVAX_TOKEN, а не аргументом: аргументы видит в ps
# любой пользователь все минуты установки. Тогда второй позиционный аргумент - имя
# инстанса, а не токен
if [ -n "${KERVAX_TOKEN:-}" ]; then
  if [ -n "$TOKEN" ] && [ -z "$INSTANCE" ]; then INSTANCE="$TOKEN"; fi
  TOKEN="$KERVAX_TOKEN"
fi
unset KERVAX_TOKEN

# ── Удаление ─────────────────────────────────────────────────────────────────
# Обратной операции не было вовсе: сервер убирают из панели, а на ноде остаётся
# работающий сервис, который вечно стучится с уже отозванным токеном, плюс юзер,
# бинарь, юниты хелперов, sudoers и socket-proxy. Что именно вычищать — знал
# только тот, кто читал этот файл.
#   install.sh --uninstall            снять агента
#   install.sh --uninstall <инстанс>  снять один инстанс (конфиг/юнит с суффиксом)
if [ "$UNINSTALL" = 1 ]; then
  [ "$(id -u)" = "0" ] || { echo "Нужен root (sudo)." >&2; exit 1; }
  # у --uninstall первый позиционный аргумент — это инстанс, а не url
  [ -z "$INSTANCE" ] && [ -n "$URL" ] && INSTANCE="$URL"
  if [ -n "$INSTANCE" ]; then
    UNITS="kervax-agent@$INSTANCE"
    CONFS="/etc/kervax-agent-$INSTANCE.conf"
    echo "→ Снимаю инстанс $INSTANCE"
  else
    UNITS="kervax-agent"
    CONFS="/etc/kervax-agent.conf"
    # инстансы, заведённые через шаблон, снимаем заодно — иначе они останутся
    # без основного юнита и продолжат стучаться в свои панели
    for u in $(systemctl list-units --all --plain --no-legend 'kervax-agent@*' 2>/dev/null | awk '{print $1}'); do
      UNITS="$UNITS ${u%.service}"
      n=${u#kervax-agent@}; n=${n%.service}
      CONFS="$CONFS /etc/kervax-agent-$n.conf"
    done
    echo "→ Снимаю агента полностью"
  fi

  for u in $UNITS; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
  done
  # юниты, таймеры и path-юниты хелперов - по маске kervax-*, а не по списку: список
  # здесь отставал от хелперов, и после --uninstall дюжина таймеров продолжала запускаться
  # впустую (changes, cron, disk-*, fpm, logerr, units, web-rate, workers...).
  # Сначала таймеры и path-юниты, иначе они снова запускают свой сервис посреди снятия.
  if [ -z "$INSTANCE" ]; then
    KUNITS=$({
      systemctl list-unit-files --no-legend --plain 'kervax-*' 2>/dev/null
      systemctl list-units --all --no-legend --plain 'kervax-*' 2>/dev/null
      ls /etc/systemd/system 2>/dev/null
    } | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^kervax-[A-Za-z0-9@_.-]*\.(timer|path|socket|service)$/) { print $i; break } }' | sort -u)
    for kind in timer path socket service; do
      for u in $KUNITS; do
        case "$u" in *."$kind") systemctl disable --now "$u" >/dev/null 2>&1 || true ;; esac
      done
    done
    # файлы юнитов, drop-in'ы (спул, вотчдог) и ссылки включения, если disable не сработал
    rm -rf /etc/systemd/system/kervax-*
    find /etc/systemd/system -name 'kervax-*' \( -type l -o -type f \) -delete 2>/dev/null || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  [ -z "$INSTANCE" ] && { systemctl reset-failed 'kervax-*' >/dev/null 2>&1 || true; }

  rm -f $CONFS
  # CA панели, добавленный через --ca-file (только наш: kervax-panel*.crt), и пересборка хранилища
  if [ -n "$INSTANCE" ]; then CA_GLOB="kervax-panel-$INSTANCE.crt"; else CA_GLOB="kervax-panel*.crt"; fi
  CA_GONE=0
  for d in /usr/local/share/ca-certificates /etc/pki/ca-trust/source/anchors /etc/pki/trust/anchors; do
    for f in $d/$CA_GLOB; do
      [ -e "$f" ] || continue
      rm -f "$f"; CA_GONE=1
    done
  done
  if [ "$CA_GONE" = 1 ]; then
    if command -v update-ca-trust >/dev/null 2>&1 && [ -d /etc/pki/ca-trust/source/anchors ]; then
      update-ca-trust extract >/dev/null 2>&1 || true
    else
      update-ca-certificates --fresh >/dev/null 2>&1 || update-ca-certificates >/dev/null 2>&1 || true
    fi
    echo "✓ CA панели больше не в доверенных на этой ноде."
  fi
  if [ -z "$INSTANCE" ]; then
    # только свой прокси (и его копию от обновления хелпера): другие socket-proxy
    # контейнеры на ноде чужие и остаются
    command -v docker >/dev/null 2>&1 && docker rm -f kervax-docker-proxy kervax-docker-proxy-old >/dev/null 2>&1 || true
    # cron хелперов (статистика клиента и сервера бэкапов). kervax-prune-* снимать не нам:
    # это ночная чистка репозиториев клиентов на сервере бэкапов, перенесенная туда из
    # crontab root, и без нее репозитории растут без предела
    KEEP_PRUNE=""
    for f in /etc/cron.d/kervax-*; do
      [ -e "$f" ] || continue
      case "$f" in
        /etc/cron.d/kervax-prune-*) KEEP_PRUNE="$KEEP_PRUNE $f" ;;
        *) rm -f "$f" ;;
      esac
    done
    # скрипты чистки могут запускать наш restic из /lib65/kervax - он остается с ними
    if [ -n "$KEEP_PRUNE" ] && [ -d /lib65/kervax ]; then
      find /lib65/kervax -mindepth 1 -maxdepth 1 ! -name restic -exec rm -rf {} + 2>/dev/null || true
    else
      rm -rf /lib65/kervax
    fi
    # состояние и спулы запросов панели (*-req/*-res), отчеты, кэши, локи
    rm -rf /opt/kervax-agent /opt/kervax/bin /etc/kervax /var/lib/kervax /run/kervax-*
    rmdir /opt/kervax /lib65 2>/dev/null || true   # только если пусты (каталог панели уцелеет)
    rm -f /etc/sudoers.d/kervax-*
    id kervax >/dev/null 2>&1 && { userdel kervax >/dev/null 2>&1 || deluser kervax >/dev/null 2>&1 || true; }
    echo "✓ Агент снят: сервис, юниты и таймеры хелперов, cron, состояние, бинарь, конфиги, sudoers, юзер kervax."
    if [ -n "$KEEP_PRUNE" ]; then
      echo "  Ночная чистка репозиториев бэкапов остается (это не мониторинг):"
      for f in $KEEP_PRUNE; do echo "    $f"; done
      echo "    снимать только вместе с самим сервером бэкапов: rm -f /etc/cron.d/kervax-prune-* /lib65/kervax/restic"
    fi
    echo "  Учётная запись в Kubernetes (если включалась) остаётся в кластере:"
    echo "    kubectl -n kervax delete serviceaccount kervax    # при необходимости"
    echo "  Сервер из самой панели удалите отдельно — тогда её токен перестанет действовать."
  else
    echo "✓ Инстанс $INSTANCE снят (общий бинарь и остальные инстансы не тронуты)."
  fi
  exit 0
fi

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Использование: install.sh [--ca-file <корень.pem>] [--proxy <url>] <url_панели> <токен> [инстанс] [--no-auto]" >&2
  echo "               install.sh --uninstall [инстанс]" >&2
  exit 1
fi
if [ -n "$INSTANCE" ] && ! printf '%s' "$INSTANCE" | grep -Eq '^[A-Za-z0-9_-]+$'; then
  echo "Имя инстанса — только буквы/цифры/дефис/подчёркивание." >&2
  exit 1
fi

case "$PROXY" in
  *[[:space:]]*) PROXY_OK=0 ;;
  "" | http://?* | https://?* | socks5://?*) PROXY_OK=1 ;;
  *) PROXY_OK=0 ;;
esac
if [ "$PROXY_OK" = 0 ]; then
  echo "--proxy нужен вида http://логин:пароль@хост:порт (подходят и https://, socks5://)." >&2
  exit 1
fi
# Все запросы к панели идут через pcurl: с --proxy он добавляет прокси, и только им.
# Helper'ы, пришедшие с панели, работают без него (свои загрузки, локальный API
# Kubernetes) и его не видят.
pcurl() {
  if [ -n "$PROXY" ]; then curl --proxy "$PROXY" "$@"; else curl "$@"; fi
}

if [ "$(id -u)" != "0" ]; then
  echo "Нужен root (sudo)." >&2
  exit 1
fi

# umask задается здесь, а не берется у того, кто запускает: sudo складывает свой 022 с
# umask вызвавшего, и у пользователя с umask 077 все каталоги ниже создавались 0700.
# В /opt/kervax-agent тогда не мог зайти никто, кроме root, и агент (пользователь kervax)
# падал на старте с "Permission denied" (status=203/EXEC).
umask 022

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "Архитектура $(uname -m) не поддерживается (нужен amd64/arm64)." >&2; exit 1 ;;
esac

# непривилегированный системный юзер
id kervax >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || \
  adduser --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || true

# -- CA панели. Команда из панели передает --ca-file, когда ее сертификат от CA, которого
#    нода не знает (внутренний CA caddy в локальной сети): без него первое же скачивание
#    падает на проверке сертификата, а за ним и агент. Корень кладем в системное хранилище,
#    а не в конфиг агента: это хранилище читает Go-агент, и его же helper'ы со своим curl к
#    панели. В имени файла инстанс, поэтому корень второй панели не заменяет первый, а
#    --uninstall снимает ровно эти файлы. --
CA_NAME="kervax-panel${INSTANCE:+-$INSTANCE}.crt"
if [ -n "$CA_FILE" ]; then
  # Файл приходит из /tmp, где его мог заранее создать или подменить кто угодно: он читается
  # один раз в свой файл и только если это не симлинк, его владелец root или тот, кто вызвал
  # sudo, и писать в него больше никто не может (дописанный CA тоже стал бы доверенным)
  CA_OWN=$(stat -c '%u %a' "$CA_FILE" 2>/dev/null || echo "x 777")
  if [ -L "$CA_FILE" ] || [ ! -f "$CA_FILE" ] || [ $((0${CA_OWN#* } & 022)) != 0 ] \
     || { [ "${CA_OWN% *}" != 0 ] && [ "${CA_OWN% *}" != "${SUDO_UID:-0}" ]; }; then
    echo "✗ --ca-file $CA_FILE - не обычный файл root или пользователя, вызвавшего sudo, или в него может писать кто-то еще." >&2; exit 1
  fi
  CA_TMP=$(mktemp)
  cat "$CA_FILE" > "$CA_TMP"
  grep -q -- '-----BEGIN CERTIFICATE-----' "$CA_TMP" || {
    rm -f "$CA_TMP"; echo "✗ --ca-file $CA_FILE - не сертификат в PEM." >&2; exit 1; }
  if command -v update-ca-certificates >/dev/null 2>&1 && [ -d /usr/local/share/ca-certificates ]; then
    CA_DST=/usr/local/share/ca-certificates/$CA_NAME            # Debian, Ubuntu, Alpine
    CA_UPDATE="update-ca-certificates"
  elif command -v update-ca-trust >/dev/null 2>&1 && [ -d /etc/pki/ca-trust/source/anchors ]; then
    CA_DST=/etc/pki/ca-trust/source/anchors/$CA_NAME             # RHEL, Fedora, Alma, Rocky
    CA_UPDATE="update-ca-trust extract"
  elif command -v update-ca-certificates >/dev/null 2>&1 && [ -d /etc/pki/trust/anchors ]; then
    CA_DST=/etc/pki/trust/anchors/$CA_NAME                       # openSUSE, SLES
    CA_UPDATE="update-ca-certificates"
  else
    echo "✗ Не нашел системного хранилища, куда добавить CA панели (поставьте ca-certificates)." >&2
    exit 1
  fi
  cp "$CA_TMP" "$CA_DST"
  rm -f "$CA_TMP"
  chmod 0644 "$CA_DST"
  $CA_UPDATE >/dev/null 2>&1 || { echo "✗ $CA_UPDATE не отработал." >&2; exit 1; }
  # проверяем БЕЗ --cacert, через системное хранилище: ровно так ходит агент.
  # /api/agent/*, а не /api/health: белый список адресов панели там не действует.
  CA_ERR=$(mktemp)
  if ! pcurl -fsS --connect-timeout 15 -m 60 --retry 3 --retry-delay 5 -o /dev/null "${URL%/}/api/agent/install.sh" 2>"$CA_ERR"; then
    echo "✗ CA панели лежит в $CA_DST, но https к панели все равно не проходит:" >&2
    cat "$CA_ERR" >&2
    echo "  Сертификат должен быть выписан ровно на хост из $URL (IP или имя)." >&2
    rm -f "$CA_ERR"
    exit 1
  fi
  rm -f "$CA_ERR"
  echo "✓ CA панели в доверенных: $CA_DST"
fi

echo "→ Скачиваю агент ($ARCH)..."
# Каталог агента — /opt/kervax-agent, НЕ /opt/kervax. Панель по умолчанию
# ставится именно в /opt/kervax (так делает quickstart.sh), и раньше агент
# селился внутрь неё, а следом делал chown -R kervax по всему каталогу. На
# машине, где мониторят саму панель — совершенно обычный случай, — это забирало
# у панели её же ./data вместе с кластером postgres: работающая база мгновенно
# теряла доступ к своим файлам («could not open file … Permission denied»),
# соединения переставали открываться, панель отдавала 500. Поднималась она
# только после ручного перезапуска контейнеров.
# Заодно уходим из-под документированного удаления панели (`rm -rf /opt/kervax`),
# которое сносило бинарь работающего агента.
BIN_DIR=/opt/kervax-agent/bin
BIN=$BIN_DIR/kervax-agent
LEGACY_BIN_DIR=/opt/kervax/bin
mkdir -p "$BIN_DIR"
# и chmod: mkdir -p существующий каталог не трогает, а переустановка должна починить тот,
# что прошлый запуск оставил с 0700.
chmod 0755 /opt/kervax-agent "$BIN_DIR"
# во временный файл + mv: атомарно и не спотыкается о «Text file busy»,
# когда на сервере уже крутится другой инстанс агента с этим бинарём
# --connect-timeout: если панель закрыта фаерволом — быстрое понятное падение,
# а не многоминутное зависание (проверьте «IP сервера» в панели и её фаервол)
# -C - и --retry: на канале, который рвёт длинную передачу (DPI по пути, нестабильный
# аплинк), скачивание одним заходом не заканчивается никогда — рвётся на одном и том же
# месте и начинается заново. Докачка продолжает с места обрыва, и бинарь доезжает за
# несколько проходов.
# --retry-all-errors появился в curl 7.71: на Ubuntu 20.04 (curl 7.68) ключ был незнаком, curl
# не запускался, а установщик валил все на фаервол панели. Только там, где curl его знает.
RETRY_ALL=""
{ curl --help all; curl --help; } 2>/dev/null | grep -q -- '--retry-all-errors' && RETRY_ALL="--retry-all-errors"
# --speed-limit/--speed-time: зависшая передача (DPI глотает пакеты, а соединение держит)
# через минуту становится ошибкой и повторяется с места обрыва; раньше curl ждал ее вечно.
# Сжатый бинарь в 2.4 раза меньше, на медленном канале это 15 минут вместо 40; gzip
# проверяет его при распаковке (CRC). Нода без gzip получает несжатый.
fetch() {
  pcurl -fsSL --connect-timeout 15 --speed-limit 512 --speed-time 60 --retry 10 --retry-delay 5 \
       $RETRY_ALL -C - "$1" -o "$2"
}
rm -f "$BIN.new" "$BIN.new.gz"
if command -v gzip >/dev/null 2>&1 \
   && pcurl -fsI --connect-timeout 15 -m 60 "${URL%/}/api/agent/download-gz/$ARCH" >/dev/null 2>&1 \
   && fetch "${URL%/}/api/agent/download-gz/$ARCH" "$BIN.new.gz" \
   && gzip -dc "$BIN.new.gz" > "$BIN.new"; then
  rm -f "$BIN.new.gz"
else
  rm -f "$BIN.new" "$BIN.new.gz"
  fetch "${URL%/}/api/agent/download/$ARCH" "$BIN.new"
fi || {
  echo "✗ Не удалось скачать агента с панели: передача все время обрывается." >&2
  echo "  Выполните команду еще раз. Если панель за фаерволом, проверьте IP сервера в ее настройках." >&2
  [ -n "$PROXY" ] && echo "  Скачивали через прокси: проверьте, что он доступен и пускает этот сервер." >&2
  exit 1
}
chmod 0755 "$BIN.new"
mv -f "$BIN.new" "$BIN"
# ТОЛЬКО свой каталог: агенту нужно право заменить самого себя при обновлении,
# и ничего больше. Рекурсивный chown по родительскому каталогу — как раз то, чем
# он однажды забрал у панели её базу.
chown -R kervax "$BIN_DIR"
# уберём легаси-пути прошлых установок, если были
rm -f /usr/local/bin/kervax-agent 2>/dev/null || true
if [ -d "$LEGACY_BIN_DIR" ] && [ "$LEGACY_BIN_DIR" != "$BIN_DIR" ]; then
  rm -rf "$LEGACY_BIN_DIR"
  # каталог панели не трогаем: rmdir сработает, только если он опустел
  rmdir /opt/kervax 2>/dev/null || true
fi

if [ -n "$INSTANCE" ]; then
  CONF=/etc/kervax-agent-$INSTANCE.conf
  UNIT=kervax-agent@$INSTANCE
else
  CONF=/etc/kervax-agent.conf
  UNIT=kervax-agent
fi

umask 077
cat > "$CONF" <<EOF
url=$URL
token=$TOKEN
EOF
[ -n "$PROXY" ] && printf 'proxy=%s\n' "$PROXY" >> "$CONF"
chown kervax "$CONF" 2>/dev/null || true
chmod 0600 "$CONF"
umask 022

# одиночный юнит (по умолчанию)
cat > /etc/systemd/system/kervax-agent.service <<'EOF'
[Unit]
Description=Kervax monitoring agent
After=network-online.target
Wants=network-online.target

[Service]
User=kervax
ExecStart=BIN_PLACEHOLDER/kervax-agent /etc/kervax-agent.conf
Restart=always
RestartSec=10
# systemd-watchdog: агент шлёт WATCHDOG=1 каждый цикл; если замолчал дольше WatchdogSec
# (завис сбор/сеть) — systemd сам гасит и поднимает. Независимо от Go-вотчдога (тот
# однажды не сработал). Type=notify требует READY=1 при старте (агент 1.77+ шлёт).
Type=notify
NotifyAccess=main
WatchdogSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
# агент обновляет сам себя атомарной заменой бинаря — разрешаем запись ТОЛЬКО сюда
ReadWritePaths=BIN_PLACEHOLDER
# читать /dev/kmsg для имени OOM-жертвы (иначе dmesg_restrict=1 не даёт непривил. юзеру)
AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG
[Install]
WantedBy=multi-user.target
EOF

# template-юнит для дополнительных инстансов (kervax-agent@<имя>)
cat > /etc/systemd/system/kervax-agent@.service <<'EOF'
[Unit]
Description=Kervax monitoring agent (%i)
After=network-online.target
Wants=network-online.target

[Service]
User=kervax
ExecStart=BIN_PLACEHOLDER/kervax-agent /etc/kervax-agent-%i.conf
Restart=always
RestartSec=10
Type=notify
NotifyAccess=main
WatchdogSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=BIN_PLACEHOLDER
# читать /dev/kmsg для имени OOM-жертвы (иначе dmesg_restrict=1 не даёт непривил. юзеру)
AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG
[Install]
WantedBy=multi-user.target
EOF

# Путь к бинарю в юнитах подставляем здесь: сами юниты пишутся heredoc'ом с
# закавыченным EOF, чтобы systemd-спецификаторы (%i) не съел шелл.
sed -i "s|BIN_PLACEHOLDER|$BIN_DIR|g"     /etc/systemd/system/kervax-agent.service /etc/systemd/system/kervax-agent@.service

systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || true
# restart (а не just --now): при переустановке поверх работающего агента надо
# перезапустить процесс, иначе он продолжит крутить старый бинарь/юнит.
if ! systemctl restart "$UNIT"; then
  # раньше set -e обрывал скрипт прямо здесь, и оставалось только сообщение systemd
  echo "✗ $UNIT не запустился. Конец его журнала:" >&2
  journalctl -u "$UNIT" -n 5 --no-pager >&2 2>/dev/null || true
  if findmnt -no OPTIONS -T "$BIN_DIR" 2>/dev/null | grep -qw noexec; then
    echo "  $BIN_DIR на разделе с noexec: запускать оттуда ничего нельзя." >&2
  fi
  exit 1
fi
echo "✓ $UNIT установлен и запущен. Логи: journalctl -u $UNIT -f"

# Доступ к Docker для агента БЕЗ выдачи ему root. Крошечный socket-proxy (wollomatic)
# отдает агенту ТОЛЬКО пер-методный allowlist: GET version/list/logs и список образов (даты
# сборки образов внешних прокси) + POST restart/stop/start конкретного контейнера. Все
# остальное (exec, create, pull, build, volumes, host-mount) - 403. Ни RCE, ни host-root. Сети у него
# нет, слушает unix-сокет в /var/lib/kervax/docker, куда заходят только root и группа kervax:
# ни контейнеры, ни другие пользователи ноды до него не достают (на 127.0.0.1:2375 любой из них
# читал переменные окружения всех контейнеров и мог их остановить). Без capabilities, корень
# только для чтения, образ закреплен по digest. Та же команда - в dockerproxy-setup.sh и в
# разделе Docker панели.
setup_docker_proxy() {
  command -v docker >/dev/null 2>&1 || { echo "· Docker не найден — пропускаю proxy." >&2; return; }
  echo "→ Настраиваю bounded-доступ к Docker (socket-proxy)..."
  DGID=$(stat -c %g /var/run/docker.sock 2>/dev/null || getent group docker 2>/dev/null | cut -d: -f3)
  KGID=$(getent group kervax 2>/dev/null | cut -d: -f3)
  install -d -o root -g kervax -m 2770 /var/lib/kervax/docker
  docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
  if [ -n "$KGID" ] && docker run -d --name kervax-docker-proxy --restart unless-stopped \
      --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
      --user "65534:${DGID:-999}" --group-add "$KGID" \
      -v /var/run/docker.sock:/var/run/docker.sock:ro -v /var/lib/kervax/docker:/run/kervax \
      wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848 \
        -loglevel warn -proxysocketendpoint /run/kervax/docker.sock -proxysocketendpointfilemode 0660 -shutdowngracetime 1 \
        -allowGET '^/(v[0-9.]+/)?(version|_ping|containers/json|images/json|containers/[a-zA-Z0-9_.-]+/(json|logs))$' \
        -allowPOST '^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$' >/dev/null; then
    # тот же файл переписывается: он остается агента (kervax, 0600)
    printf '%s\n' "$(grep -v '^docker_host=' "$CONF")" > "$CONF"
    printf 'docker_host=/var/lib/kervax/docker/docker.sock\n' >> "$CONF"
    echo "✓ Docker включён (proxy: view + restart/stop/start, без exec/create/root)."
  else
    echo "✗ Не удалось поднять socket-proxy (docker-демон доступен?)." >&2
  fi
}

# скачать и выполнить серверный setup-скрипт панели (kube-setup/backupserver-setup/
# backup-setup). Они самодетектируются и молча пропускают неприменимое. Нужен bash.
run_remote_setup() {
  if ! command -v bash >/dev/null 2>&1; then echo "· $1: нужен bash — пропускаю." >&2; return; fi
  # общий маршрут: раздаётся ЛЮБОЙ хелпер каталога. Раньше тут был /api/agent/$1.sh —
  # персональные URL есть только у kube/backup/backupserver, поэтому webserver-setup
  # на свежей ноде молча получал 404 и не ставился.
  # сначала в файл, потом bash: helper, отданный прямо в bash, при обрыве передачи посреди
  # файла выполнялся первой половиной. </dev/null - как под плейбуком, вопросов никто не ждет
  f=$(mktemp) || return 0
  if pcurl -fsSL --compressed --connect-timeout 15 --speed-limit 512 --speed-time 60 --retry 3 \
       --retry-delay 5 $RETRY_ALL "${URL%/}/api/agent/setup/$1.sh" -o "$f"; then
    bash "$f" </dev/null || echo "· $1: не удалось выполнить (пропускаю)." >&2
  else
    echo "· $1: не удалось скачать (пропускаю)." >&2
  fi
  rm -f "$f"
}

# Кому нужен хелпер, решают САМИ ХЕЛПЕРЫ, а каталог панели лишь передаёт их ответы:
# KERVAX_SETUP_ALWAYS (безопасен везде) и KERVAX_SETUP_WHEN (shell-условие, которое
# проверяется здесь, на ноде). Про конкретные хелперы в установщике не написано ничего:
# прежняя версия держала список внутри себя, его забывали дополнить при появлении
# нового хелпера, и свежепоставленная нода сразу просила «сходи выполни руками».
#
# Условие выполняется от root и приходит с панели — которой этот скрипт и так доверяет
# полностью: он качает оттуда сами хелперы и отдаёт их в bash. (Ansible-плейбук — другое
# дело: он читает те же условия из репозитория, а не по сети.)
install_applicable_setups() {
  idx=$(pcurl -fsSL --compressed --connect-timeout 15 -m 60 --retry 3 --retry-delay 5 "${URL%/}/api/agent/setup/index" 2>/dev/null) || {
    echo "· каталог хелперов недоступен — пропускаю хелперы." >&2; return; }
  # делим МЕЖДУ записями, а не по каждой '{': в условии бывают свои скобки, и
  # разбиение по ним рвало запись пополам — следующий за ней хелпер пропадал из
  # списка совсем.
  # '%s\n', а не '%s': без завершающего перевода строки `read` не отдаёт последнюю
  # запись, и последний helper каталога молча не ставился.
  printf '%s\n' "$idx" | sed 's/},{/}\n{/g' | while IFS= read -r row; do
    n=$(printf '%s' "$row" | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p')
    [ -n "$n" ] || continue
    if ! printf '%s' "$row" | grep -q '"always": *true'; then
      cond=$(printf '%s' "$row" | sed -n 's/.*"when": *"\([^"]*\)".*/\1/p')
      [ -n "$cond" ] || continue                    # ни условия, ни «безопасен везде»
      ( eval "$cond" ) >/dev/null 2>&1 || continue  # эта нода не подходит
    fi
    echo "→ Хелпер $n..."
    run_remote_setup "$n"
  done
}

# ── авто-интеграции: включаем всё применимое, чтобы после добавления сервера
#    ничего не доделывать вручную. Все привилегии УЗКИЕ (proxy/SA/sudoers). ──
if [ "$NO_AUTO" = 0 ]; then
  # Docker
  command -v docker >/dev/null 2>&1 && setup_docker_proxy
  # всё применимое: и безопасное везде (вотчдог, время, домены, инвентарь СУБД), и то,
  # чьё собственное условие подошло этой ноде (доступ в Kubernetes, статистика
  # бэкап-сервера, …). Добавление ноды — и есть явное действие, поэтому хелперы с
  # пометкой «только вручную» (те, что выдают панели новый доступ) ставятся и здесь;
  # массовый прогон плейбука их намеренно не трогает.
  install_applicable_setups
  # перезапуск, чтобы агент подхватил docker_host/kube.json/хелперы
  systemctl restart "$UNIT" 2>/dev/null || true
elif [ "$WANT_DOCKER" = 1 ]; then
  setup_docker_proxy
  systemctl restart "$UNIT" 2>/dev/null || true
fi
}
