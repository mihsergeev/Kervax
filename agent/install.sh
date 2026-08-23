#!/bin/sh
# Установка Kervax-агента. Вызывается: install.sh <url_панели> <токен> [инстанс]
# Ставит статический бинарь в /opt/kervax/bin (владелец kervax — чтобы агент мог
# БЕЗ root безопасно обновлять сам себя), конфиг в /etc, systemd-юнит.
# Агент работает под непривилегированным юзером kervax (без шелла), только исходящие.
#
# [инстанс] — для ВТОРОГО агента на том же сервере (метрики в другую панель):
#   install.sh https://panel2.example.com <токен2> panel2
# → конфиг /etc/kervax-agent-panel2.conf + юнит kervax-agent@panel2 (template).
# Бинарь общий; инстансы полностью независимы (свой url/token/процесс).
set -eu

# Разбор аргументов: позиционные <url> <токен> [инстанс] + флаги.
# По умолчанию (авто-режим) install.sh включает ВСЁ применимое на ноде: bounded-доступ
# к Docker (socket-proxy), read-only Kubernetes (узкий SA), статистику сервера бэкапов,
# управление restic-бэкапом. Каждый шаг самодетектируется. Флаг --no-auto — только агент.
URL=""; TOKEN=""; INSTANCE=""; WANT_DOCKER=0; NO_AUTO=0
for a in "$@"; do
  case "$a" in
    --docker) WANT_DOCKER=1 ;;
    --no-auto) NO_AUTO=1 ;;
    *)
      if [ -z "$URL" ]; then URL="$a"
      elif [ -z "$TOKEN" ]; then TOKEN="$a"
      elif [ -z "$INSTANCE" ]; then INSTANCE="$a"
      fi ;;
  esac
done
if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Использование: install.sh <url_панели> <токен> [инстанс] [--no-auto]" >&2
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
BIN_DIR=/opt/kervax/bin
BIN=$BIN_DIR/kervax-agent
mkdir -p "$BIN_DIR"
# во временный файл + mv: атомарно и не спотыкается о «Text file busy»,
# когда на сервере уже крутится другой инстанс агента с этим бинарём
# --connect-timeout: если панель закрыта фаерволом — быстрое понятное падение,
# а не многоминутное зависание (проверьте «IP сервера» в панели и её фаервол)
curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/download/$ARCH" -o "$BIN.new" || {
  echo "✗ Панель недоступна с этого сервера. Если она за фаерволом — укажите" >&2
  echo "  «IP сервера» в её настройках и подождите ~2 мин (cron-синк), затем повторите." >&2
  exit 1
}
chmod 0755 "$BIN.new"
mv -f "$BIN.new" "$BIN"
# каталог и бинарь принадлежат kervax → агент сможет заменить себя при обновлении
chown -R kervax /opt/kervax
# уберём легаси-путь прошлых установок, если был
rm -f /usr/local/bin/kervax-agent 2>/dev/null || true

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
ExecStart=/opt/kervax/bin/kervax-agent /etc/kervax-agent.conf
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
ReadWritePaths=/opt/kervax/bin
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
ExecStart=/opt/kervax/bin/kervax-agent /etc/kervax-agent-%i.conf
Restart=always
RestartSec=10
Type=notify
NotifyAccess=main
WatchdogSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/kervax/bin
# читать /dev/kmsg для имени OOM-жертвы (иначе dmesg_restrict=1 не даёт непривил. юзеру)
AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG
[Install]
WantedBy=multi-user.target
EOF

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

# Хелперы, безопасные на любой ноде (KERVAX_SETUP_ALWAYS): список СПРАШИВАЕМ У ПАНЕЛИ,
# а не держим захардкоженным здесь. Иначе каждый новый хелпер надо дописывать в
# установщик — о чём забывали, и свежая нода сразу просила «сходи выполни руками».
install_always_setups() {
  idx=$(curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/setup/index" 2>/dev/null) || {
    echo "· каталог хелперов недоступен — ставлю только детектируемые." >&2; return; }
  # без jq: каждая запись каталога — свой объект, берём имена тех, где always=true
  names=$(printf '%s' "$idx" | tr '{' '\n' | grep '"always": *true' | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p')
  for n in $names; do
    echo "→ Хелпер $n..."
    run_remote_setup "$n"
  done
}

# ── авто-интеграции: включаем всё применимое, чтобы после добавления сервера
#    ничего не доделывать вручную. Все привилегии УЗКИЕ (proxy/SA/sudoers). ──
if [ "$NO_AUTO" = 0 ]; then
  # Docker
  command -v docker >/dev/null 2>&1 && setup_docker_proxy
  # всё, что безопасно везде (вотчдог, время, домены, инвентарь СУБД, …) — из каталога
  install_always_setups
  # Kubernetes (k0s/k3s/microk8s/kubeadm) → узкий read-only+ SA (kube-setup.sh)
  if command -v k0s >/dev/null 2>&1 || command -v k3s >/dev/null 2>&1 \
     || command -v microk8s >/dev/null 2>&1 || command -v kubelet >/dev/null 2>&1 \
     || [ -e /etc/rancher/k3s/k3s.yaml ] || [ -e /etc/kubernetes/admin.conf ] || [ -d /var/lib/k0s ]; then
    echo "→ Kubernetes найден — включаю read-only доступ (узкий SA)..."
    run_remote_setup kube-setup
  fi
  # сервер бэкапов (rest-server) → read-only статистика репозиториев
  if [ -d /app/rest-server/data ] \
     || { command -v docker >/dev/null 2>&1 && docker ps --format '{{.Image}}' 2>/dev/null | grep -q 'rest-server'; }; then
    echo "→ Сервер бэкапов найден — включаю статистику репозиториев..."
    run_remote_setup backupserver-setup
  fi
  # клиент restic-бэкапа → управление из панели (узкий sudoers-helper)
  if [ -e /etc/systemd-rest.conf ] || [ -e /etc/systemd/system/systemd-rest.timer ] \
     || [ -x /usr/local/lib/.restic/restic ]; then
    echo "→ restic-бэкап найден — включаю управление из панели..."
    run_remote_setup backup-setup
  fi
  # перезапуск, чтобы агент подхватил docker_host/kube.json/хелперы
  systemctl restart "$UNIT" 2>/dev/null || true
elif [ "$WANT_DOCKER" = 1 ]; then
  setup_docker_proxy
  systemctl restart "$UNIT" 2>/dev/null || true
fi
