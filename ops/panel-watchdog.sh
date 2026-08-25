#!/bin/bash
# Kervax host watchdog (a dead man's switch, outside docker).
#
# A panel cannot report its own death through its own channel, so an observer OUTSIDE its
# process is required. This script is that observer, running on the panel host: it reads the
# heartbeat the scheduler writes every minute into <data>/heartbeat
# (backend/app/heartbeat.py) and sends to Telegram/webhook INDEPENDENTLY when:
#   * the heartbeat is stale (>MAX_AGE) - the container, scheduler or database is dead or hung;
#   * alerts_ok=0 - the panel's own alert-channel self-test failed (expired token, Telegram
#     blocked).
# Credentials (token, chat, mirror) come from the heartbeat itself, so it gets through even
# when the panel and the database no longer answer. It alerts only on a state change, without
# repeating itself.
#
# INSTALLATION (on the panel host, as root):
#   install -D -m755 ops/panel-watchdog.sh /lib65/kervax/panel-watchdog.sh
#   # adjust HB below to your data path (default /root/kervax/data/heartbeat)
#   echo '*/5 * * * * root /lib65/kervax/panel-watchdog.sh' > /etc/cron.d/kervax-watchdog
#   chmod 644 /etc/cron.d/kervax-watchdog
# (the script lives in /lib65 because /usr is excluded from backups; the cron config is in /etc.)

set -u
HB="${KERVAX_HEARTBEAT:-/root/kervax/data/heartbeat}"   # path to the heartbeat file on the host
STATE=/lib65/kervax/watchdog.state
MAX_AGE="${KERVAX_HB_MAX_AGE:-600}"                       # seconds: an older heartbeat raises an alarm
STRIKES="${KERVAX_WD_STRIKES:-2}"                        # consecutive failing checks before alarming
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
  problem="the heartbeat file is missing - the scheduler is not writing it (is the panel running?)"
else
  ts=$(val ts); ok=$(val alerts_ok); age=$(( NOW - ${ts:-0} ))
  if [ "${ts:-0}" -eq 0 ] || [ "$age" -gt "$MAX_AGE" ]; then
    problem="the panel has been silent for ${age}s (>${MAX_AGE}s) - container, scheduler or database is dead or hung"
  elif [ "$ok" = "0" ]; then
    problem="the panel is alive but its alert channel is broken (Telegram unreachable or the token is invalid)"
  fi
fi

# which panel exactly (from the heartbeat), so the alert says what went down
panel=$(val panel)
[ -z "$panel" ] && panel="$(hostname)"
tag="🚑 Kervax watchdog [$panel]"

# Debounce: an alarm is sent only after STRIKES consecutive failing checks, so short-lived
# events (a panel restart during a deploy, a single channel blip) do not wake the watchdog.
# State: line 1 = whether an alarm was already sent (ok|problem), line 2 = the streak.
# alerted is the outstanding alarm; strikes counts consecutive detections before the first one.
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
    send "✅ Kervax watchdog [$panel]: the panel is back to normal."
    alerted=ok
  fi
fi
printf '%s\n%s\n' "$alerted" "$strikes" > "$STATE"
