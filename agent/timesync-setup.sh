#!/usr/bin/env bash
# Kervax: включить ОДНОКЛИК-синхронизацию времени из панели.
#
# Агент под непривилегированным `kervax` + NoNewPrivileges шагать часы не может (это root).
# Поэтому: агент кладёт запрос в спул /var/lib/kervax/tsync-req, root path-unit исполняет
# узким helper (синхронизация NTP + HTTP-фолбэк) и пишет ответ в /var/lib/kervax/tsync-res.
# Агент остаётся изолированным. Действие одно (sync), панель-URL для фолбэка валидируется.
# Запускать root'ом на ноде. Без этого helper'а панель показывает copy-paste команду.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-timesync-helper"
STATE_DIR=/var/lib/kervax
REQ_DIR="$STATE_DIR/tsync-req"
RES_DIR="$STATE_DIR/tsync-res"
AGENT_USER=kervax

KERVAX_SETUP_VERSION=0.2  # МАЖОР.МИНОР; сравнивается покомпонентно
KERVAX_SETUP_ALWAYS=1     # безопасен на любой ноде → ansible ставит и при первом заходе
if ! getent group "$AGENT_USER" >/dev/null 2>&1; then
  echo "На этой ноде нет агента Kervax (нет группы '$AGENT_USER'). Сначала заведите ноду в панель." >&2
  exit 2
fi
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/timesync-setup.ver
chmod 0644 /var/lib/kervax/versions/timesync-setup.ver
# спул: агент (kervax) кладёт запросы (-wx), читает/удаляет ответы (r-x + запись)
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
# агент под ProtectSystem=strict — разрешаем ему писать спул в /var/lib/kervax
if systemctl cat kervax-agent >/dev/null 2>&1; then
  install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
  cat > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf <<'DROPIN'
[Service]
ReadWritePaths=/var/lib/kervax
DROPIN
  systemctl daemon-reload 2>/dev/null || true
  systemctl try-restart kervax-agent 2>/dev/null || true
fi

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Kervax timesync helper (root): поднять/форсировать синхронизацию времени; при закрытом
# исходящем NTP — грубый HTTP-фолбэк по времени панели. Действие одно: sync.
set -euo pipefail
REQ_DIR=/var/lib/kervax/tsync-req
RES_DIR=/var/lib/kervax/tsync-res

# активный демон времени ('' = ни один не жив)
_active_timed() {
  local s
  for s in systemd-timesyncd chronyd chrony ntpd ntpsec openntpd; do
    [ "$(systemctl is-active "$s" 2>/dev/null)" = active ] && { echo "$s"; return; }
  done
}

cmd_sync() {
  local panel="$1" svc method note synced=no i httpdate now
  # panel-URL для фолбэка принимаем только https:// (приходит из конфига агента)
  case "$panel" in https://*) ;; *) panel="" ;; esac
  # 1) есть демон? нет — поднимаем systemd-timesyncd (обычно предустановлен)
  svc="$(_active_timed || true)"
  if [ -z "$svc" ]; then
    timedatectl set-ntp true >/dev/null 2>&1 || true
    systemctl start systemd-timesyncd >/dev/null 2>&1 || true
    svc="$(_active_timed || true)"
  fi
  # 2) форс-синк (шаг часов сразу, а не медленный slew)
  case "$svc" in
    chrony*)          chronyc -a makestep >/dev/null 2>&1 || chronyc makestep >/dev/null 2>&1 || true; method="chrony makestep" ;;
    systemd-timesyncd*) timedatectl set-ntp true >/dev/null 2>&1 || true; systemctl restart systemd-timesyncd >/dev/null 2>&1 || true; method="timesyncd" ;;
    *)                method="" ;;
  esac
  # 3) убеждаемся, что синхронизировалось — СПОСОБОМ ПОД ДЕМОН. timedatectl NTPSynchronized
  #    на chrony-нодах часто показывает «no», хотя chrony уже синхронизирован → спрашиваем
  #    сам chrony (waitsync: до 15 попыток с интервалом 1с, пока коррекция < 0.5с; rc 0 = ок).
  case "$svc" in
    chrony*) chronyc waitsync 15 0.5 0 1 >/dev/null 2>&1 && synced=yes ;;
  esac
  if [ "$synced" != yes ]; then
    for i in $(seq 1 20); do
      [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && { synced=yes; break; }
      sleep 1
    done
  fi
  now="$(date '+%F %T %Z' 2>/dev/null || date)"
  if [ "$synced" = yes ]; then
    echo "OK время синхронизировано (${method:-ntp}) — NTPSynchronized=yes, сейчас $now"
    return 0
  fi
  # 4) NTP не достался (исходящий UDP 123 закрыт?) → HTTP-фолбэк по времени панели
  if [ -n "$panel" ]; then
    httpdate="$(curl -sI --max-time 10 "$panel/" 2>/dev/null | grep -i '^date:' | head -1 | cut -d' ' -f2- | tr -d '\r')"
    if [ -n "$httpdate" ] && date -s "$httpdate" >/dev/null 2>&1; then
      echo "OK NTP недоступен — часы выставлены по времени панели (грубо, до секунды): $(date '+%F %T %Z'). Для точной синхронизации откройте исходящий UDP 123 (напр. ufw allow out 123/udp)"
      return 0
    fi
  fi
  echo "не удалось синхронизировать: демон='${svc:-нет}', NTPSynchronized=no. Похоже, закрыт исходящий NTP (UDP 123) и HTTP-фолбэк не сработал — откройте порт или проверьте доступ к панели" >&2
  return 2
}

cmd_process_spool() {
  local req id panel line k v out ok
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    panel=""
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in panel_url) panel="$v" ;; esac
    done < "$req"
    rm -f "$req"
    out=""; ok=false
    if out="$(cmd_sync "$panel" 2>&1)"; then ok=true; fi
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' ' ')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
}

case "${1:-}" in
  sync)          shift; cmd_sync "${1:-}" ;;
  process-spool) cmd_process_spool ;;
  *) echo "usage: $0 {sync [https://panel]|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# path-unit: как только агент кладёт запрос — root исполняет его сразу
cat > /etc/systemd/system/kervax-tsync-req.service <<UNIT_EOF
[Unit]
Description=Kervax timesync request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-tsync-req.path <<UNIT_EOF
[Unit]
Description=Kervax timesync request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-tsync-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now kervax-tsync-req.path >/dev/null 2>&1 || true
"$HELPER" process-spool >/dev/null 2>&1 || true

echo "timesync-setup: готово → $HELPER; одноклик-синхронизация через спул $REQ_DIR."
