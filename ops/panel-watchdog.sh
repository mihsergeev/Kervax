#!/bin/bash
# Хостовый сторож Kervax (dead-man's-switch, вне docker).
#
# Панель не может сама сообщить о собственной смерти по своему же каналу, поэтому
# нужен наблюдатель ВНЕ её процесса. Этот скрипт — такой наблюдатель на хосте
# панели: он читает «пульс», который планировщик пишет раз в минуту в
# <data>/heartbeat (backend/app/heartbeat.py), и НЕЗАВИСИМО шлёт в Telegram/webhook,
# если:
#   • пульс протух (>MAX_AGE) — контейнер/планировщик/БД мертвы или зависли;
#   • alerts_ok=0 — само-тест канала алертов панели не прошёл (протух токен / TG
#     заблокирован).
# Креды (токен/чат/зеркало) берёт из самого пульса, поэтому достучится даже когда
# панель и БД уже не отвечают. Алертит только на переходе состояния (без спама).
#
# УСТАНОВКА (на хосте панели, от root):
#   install -D -m755 ops/panel-watchdog.sh /lib65/kervax/panel-watchdog.sh
#   # поправьте HB ниже под свой путь data (по умолчанию /root/kervax/data/heartbeat)
#   echo '*/5 * * * * root /lib65/kervax/panel-watchdog.sh' > /etc/cron.d/kervax-watchdog
#   chmod 644 /etc/cron.d/kervax-watchdog
# (скрипт — в /lib65, т.к. /usr исключён из бэкапа; cron-конфиг в /etc.)

set -u
HB="${KERVAX_HEARTBEAT:-/root/kervax/data/heartbeat}"   # путь к файлу пульса на хосте
STATE=/lib65/kervax/watchdog.state
MAX_AGE="${KERVAX_HB_MAX_AGE:-600}"                       # сек: пульс старше — тревога
STRIKES="${KERVAX_WD_STRIKES:-2}"                        # проверок ПОДРЯД с проблемой до тревоги
NOW=$(date +%s)

val() { grep -m1 "^$1=" "$HB" 2>/dev/null | cut -d= -f2-; }

send() {
  local msg="$1" token chat api hook
  token=$(val tg_token); chat=$(val tg_chat); api=$(val tg_api); hook=$(val webhook)
  [ -z "$api" ] && api="https://api.telegram.org"
  if [ -n "$token" ] && [ -n "$chat" ]; then
    curl -s --max-time 15 "$api/bot$token/sendMessage" \
      --data-urlencode "chat_id=$chat" --data-urlencode "text=$msg" \
      --data-urlencode "disable_web_page_preview=true" >/dev/null 2>&1
  fi
  [ -n "$hook" ] && curl -s --max-time 15 -H 'Content-Type: application/json' \
    --data "{\"text\": \"$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')\"}" "$hook" >/dev/null 2>&1
}

problem=""
if [ ! -f "$HB" ]; then
  problem="файл пульса отсутствует — планировщик не пишет heartbeat (панель не запущена?)"
else
  ts=$(val ts); ok=$(val alerts_ok); age=$(( NOW - ${ts:-0} ))
  if [ "${ts:-0}" -eq 0 ] || [ "$age" -gt "$MAX_AGE" ]; then
    problem="панель молчит ${age}с (>${MAX_AGE}с) — контейнер/планировщик/БД мертвы или зависли"
  elif [ "$ok" = "0" ]; then
    problem="панель жива, но канал алертов сломан (Telegram недоступен / токен невалиден)"
  fi
fi

# какая именно панель (из пульса) — чтобы в алерте было видно, что упало
panel=$(val panel)
[ -z "$panel" ] && panel="$(hostname)"
tag="🚑 Kervax watchdog [$panel]"

# Дебаунс: тревогу шлём лишь после STRIKES проверок ПОДРЯД с проблемой — чтобы
# кратковременные события (рестарт панели при деплое, разовый блип канала) не
# будили сторожа. Состояние: строка1 = слали ли уже (ok|problem), строка2 = стрик.
# alerted — outstanding-тревога; strikes — счётчик подряд-обнаружений до первой тревоги.
alerted=$(sed -n 1p "$STATE" 2>/dev/null); [ -z "$alerted" ] && alerted=ok
strikes=$(sed -n 2p "$STATE" 2>/dev/null); case "$strikes" in ''|*[!0-9]*) strikes=0;; esac

if [ -n "$problem" ]; then
  strikes=$((strikes + 1))
  if [ "$alerted" != "problem" ] && [ "$strikes" -ge "$STRIKES" ]; then
    send "$tag: $problem"
    alerted=problem
  fi
else
  strikes=0
  if [ "$alerted" = "problem" ]; then
    send "✅ Kervax watchdog [$panel]: панель снова в норме."
    alerted=ok
  fi
fi
printf '%s\n%s\n' "$alerted" "$strikes" > "$STATE"
