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
# Это русская версия установщика: отличается только языком сообщений.
# Английская — agent/install.sh, её же раздаёт панель по /api/agent/install.sh.
set -eu

# Разбор аргументов: позиционные <url> <токен> [инстанс] + флаги.
# По умолчанию (авто-режим) install.sh включает ВСЁ применимое на ноде: bounded-доступ
# к Docker (socket-proxy), read-only Kubernetes (узкий SA), статистику сервера бэкапов,
# управление restic-бэкапом. Каждый шаг самодетектируется. Флаг --no-auto — только агент.
URL=""; TOKEN=""; INSTANCE=""; WANT_DOCKER=0; NO_AUTO=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
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
  # юниты и таймеры хелперов (запросы из панели, инвентарь, дампы, домены)
  if [ -z "$INSTANCE" ]; then
    # .path тоже: запросы из панели хелперы ловят path-юнитом, и без него в
    # /etc/systemd/system оставались висеть kervax-*-req.path
    for u in kervax-backup-req kervax-bsrv-req kervax-tsync-req \
             kervax-db-stats kervax-dumps kervax-web-sites; do
      for kind in timer path service; do
        systemctl disable --now "$u.$kind" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$u.$kind"
      done
    done
    rm -f /etc/systemd/system/kervax-agent.service /etc/systemd/system/kervax-agent@.service
    # drop-in'ы юнита (спул, вотчдог) лежат каталогом рядом — иначе он остаётся
    rm -rf /etc/systemd/system/kervax-agent.service.d /etc/systemd/system/kervax-agent@.service.d
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true

  rm -f $CONFS
  if [ -z "$INSTANCE" ]; then
    command -v docker >/dev/null 2>&1 && docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
    rm -rf /opt/kervax-agent /opt/kervax/bin /lib65/kervax /etc/kervax
    rmdir /opt/kervax /lib65 2>/dev/null || true   # только если пусты (каталог панели уцелеет)
    rm -f /etc/sudoers.d/kervax-backup /etc/sudoers.d/kervax-backupserver
    id kervax >/dev/null 2>&1 && { userdel kervax >/dev/null 2>&1 || deluser kervax >/dev/null 2>&1 || true; }
    echo "✓ Агент снят: сервис, юниты хелперов, бинарь, конфиги, sudoers, юзер kervax."
    echo "  Учётная запись в Kubernetes (если включалась) остаётся в кластере:"
    echo "    kubectl -n kervax delete serviceaccount kervax    # при необходимости"
    echo "  Сервер из самой панели удалите отдельно — тогда её токен перестанет действовать."
  else
    echo "✓ Инстанс $INSTANCE снят (общий бинарь и остальные инстансы не тронуты)."
  fi
  exit 0
fi

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Использование: install.sh <url_панели> <токен> [инстанс] [--no-auto]" >&2
  echo "               install.sh --uninstall [инстанс]" >&2
  exit 1
fi
if [ -n "$INSTANCE" ] && ! printf '%s' "$INSTANCE" | grep -Eq '^[A-Za-z0-9_-]+$'; then
  echo "Имя инстанса — только буквы/цифры/дефис/подчёркивание." >&2
  exit 1
fi

if [ "$(id -u)" != "0" ]; then
  echo "Нужен root (sudo)." >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "Архитектура $(uname -m) не поддерживается (нужен amd64/arm64)." >&2; exit 1 ;;
esac

# непривилегированный системный юзер
id kervax >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || \
  adduser --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || true

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
# во временный файл + mv: атомарно и не спотыкается о «Text file busy»,
# когда на сервере уже крутится другой инстанс агента с этим бинарём
# --connect-timeout: если панель закрыта фаерволом — быстрое понятное падение,
# а не многоминутное зависание (проверьте «IP сервера» в панели и её фаервол)
# -C - и --retry: на канале, который рвёт длинную передачу (DPI по пути, нестабильный
# аплинк), скачивание одним заходом не заканчивается никогда — рвётся на одном и том же
# месте и начинается заново. Докачка продолжает с места обрыва, и бинарь доезжает за
# несколько проходов.
curl -fsSL --connect-timeout 15 --retry 5 --retry-delay 3 --retry-all-errors \
     -C - "${URL%/}/api/agent/download/$ARCH" -o "$BIN.new" || {
  echo "✗ Панель недоступна с этого сервера. Если она за фаерволом — укажите" >&2
  echo "  «IP сервера» в её настройках и подождите ~2 мин (cron-синк), затем повторите." >&2
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
chown kervax "$CONF" 2>/dev/null || true
chmod 0600 "$CONF"

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
systemctl restart "$UNIT"
echo "✓ $UNIT установлен и запущен. Логи: journalctl -u $UNIT -f"

# Доступ к Docker для агента БЕЗ выдачи ему root. Крошечный socket-proxy (wollomatic)
# отдаёт агенту ТОЛЬКО пер-методный allowlist: GET version/list/logs + POST
# restart/stop/start конкретного контейнера. Всё остальное (exec, create, images,
# build, volumes, host-mount) — 403. Ни RCE, ни host-root. Слушает только 127.0.0.1.
setup_docker_proxy() {
  command -v docker >/dev/null 2>&1 || { echo "· Docker не найден — пропускаю proxy." >&2; return; }
  echo "→ Настраиваю bounded-доступ к Docker (socket-proxy)..."
  DGID=$(getent group docker 2>/dev/null | cut -d: -f3)
  docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
  if docker run -d --name kervax-docker-proxy --restart unless-stopped \
      --user "65534:${DGID:-999}" \
      -v /var/run/docker.sock:/var/run/docker.sock:ro \
      -p 127.0.0.1:2375:2375 \
      wollomatic/socket-proxy:1 \
        -loglevel warn -listenip 0.0.0.0 -allowfrom 0.0.0.0/0 -shutdowngracetime 1 \
        -allowGET '^/(v[0-9.]+/)?(version|info|_ping|containers/json|containers/[a-zA-Z0-9_.-]+/(json|logs))' \
        -allowPOST '^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$' >/dev/null; then
    grep -q '^docker_host=' "$CONF" || printf 'docker_host=tcp://127.0.0.1:2375\n' >> "$CONF"
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
  curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/setup/$1.sh" | bash \
    || echo "· $1: не удалось выполнить (пропускаю)." >&2
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
  idx=$(curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/setup/index" 2>/dev/null) || {
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
