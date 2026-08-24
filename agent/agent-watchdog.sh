#!/usr/bin/env bash
# Kervax: включить systemd-watchdog для агента (само-восстановление при зависании).
#
# ЗАЧЕМ. Репорт-цикл агента может зависнуть (сеть/сбор), а in-process Go-вотчдог однажды
# не сработал (инцидент: 23 мин «offline»). systemd-watchdog — независимый
# бэкстоп: агент 1.77+ шлёт WATCHDOG=1 каждый цикл; замолчал дольше WatchdogSec — systemd
# сам гасит и поднимает. Здесь мы лишь переводим ЮНИТ на Type=notify (OTA бинаря юнит не
# трогает, поэтому нужен отдельный шаг). Ставит ansible-плейбук ЛОКАЛЬНО, не с панели.
#
# БЕЗОПАСНО НА ЛЮБОЙ НОДЕ (KERVAX_SETUP_ALWAYS): если агент старее 1.77 и не пришлёт
# READY=1 — старт под Type=notify не поднимется, скрипт это увидит и ОТКАТИТ drop-in
# (маркер не пишем → следующий прогон повторит после OTA). Запускать root'ом.
set -euo pipefail

KERVAX_SETUP_VERSION=1.0  # МАЖОР.МИНОР; сравнивается покомпонентно
KERVAX_SETUP_ALWAYS=1     # ставить и при первом заходе (self-gate откатом безопасен везде)

UNIT=kervax-agent
DROPDIR=/etc/systemd/system/$UNIT.service.d
DROP=$DROPDIR/watchdog.conf
VERDIR=/var/lib/kervax/versions
MARK=$VERDIR/agent-watchdog.ver

if [ "$(id -u)" != 0 ]; then echo "Нужен root." >&2; exit 1; fi

# нет агента — тихо выходим (нода без kervax-agent: helper неприменим)
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "· $UNIT не установлен — watchdog неприменим, пропускаю."
  exit 0
fi

# уже на Type=notify и маркер стоит — ничего не делаем (идемпотентность)
if [ -f "$MARK" ] && systemctl show "$UNIT" -p Type --value | grep -qx notify; then
  echo "· systemd-watchdog уже активен ($UNIT)."
  exit 0
fi

install -d -m 0755 "$DROPDIR"
cat > "$DROP" <<'DROPIN'
[Service]
# systemd-watchdog: агент 1.77+ шлёт READY=1 при старте и WATCHDOG=1 каждый цикл; если
# замолчал дольше WatchdogSec — systemd сам гасит и поднимает (независимо от Go-вотчдога).
# TimeoutStartSec держим коротким: агент старее 1.77 не пришлёт READY=1 — старт быстро
# упадёт, и установщик откатит этот drop-in (см. agent-watchdog.sh).
Type=notify
NotifyAccess=main
WatchdogSec=180
TimeoutStartSec=25
DROPIN

systemctl daemon-reload
if systemctl restart "$UNIT" && systemctl is-active --quiet "$UNIT"; then
  install -d -m 0755 /var/lib/kervax "$VERDIR"  # родитель явно 0755: непривил. агент должен зайти
  echo "$KERVAX_SETUP_VERSION" > "$MARK"
  chmod 0644 "$MARK"
  wd=$(systemctl show "$UNIT" -p WatchdogUSec --value)
  echo "✓ systemd-watchdog включён для $UNIT (WatchdogUSec=$wd)."
else
  # агент не подтвердил READY=1 (старее 1.77 или сломан) → откат без маркера
  rm -f "$DROP"
  rmdir "$DROPDIR" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart "$UNIT" || true
  echo "· агент не поддерживает watchdog (нужен 1.77+) — откатил drop-in, пропускаю." >&2
  exit 0
fi
