#!/bin/bash
# Kervax host watchdog (a dead man's switch, outside docker).
#
# A panel cannot report its own death through its own channel, so an observer OUTSIDE its
# process is required. This script is that observer, running on the panel host: it reads the
# heartbeat the scheduler writes every minute into <data>/heartbeat
# (backend/app/heartbeat.py) and sends to Telegram/webhook INDEPENDENTLY when:
#   * the heartbeat is stale (>MAX_AGE) - the container, scheduler or database is dead or hung;
#   * alerts_ok=0 - the panel's own alert-channel self-test failed (expired token, Telegram
#     blocked);
#   * the scheduler loop is stuck: the heartbeat is fresh (it is written by its own task), but
#     the last tick started longer than tick_max ago (an await without a timeout, a lock wait);
#   * a scheduler stage has been failing for STAGE_STRIKES ticks in a row (stage_fail), e.g.
#     the server alerts stage crashing on one node - the details are in "Panel errors".
# Credentials (token, chat, mirror) come from the heartbeat itself, so it gets through even
# when the panel and the database no longer answer. It alerts only on a state change, without
# repeating itself.
#
# INSTALLATION (on the panel host, as root):
#   install -D -m755 ops/panel-watchdog.sh /lib65/scripts/kervax-panel-watchdog.sh
#   install -d -m700 /lib65/kervax
#   # adjust HB below to your data path (default /root/kervax/data/heartbeat)
# The state directory (STATE below) must exist: without it the state is never saved, every run
# starts from zero strikes, so with STRIKES=2 an alarm (and the recovery) is never sent.
#   echo '*/5 * * * * root /lib65/scripts/kervax-panel-watchdog.sh' > /etc/cron.d/kervax-watchdog
#   chmod 644 /etc/cron.d/kervax-watchdog
# (the script lives in /lib65 because /usr is excluded from backups; the cron config is in /etc.)

set -u
HB="${KERVAX_HEARTBEAT:-/root/kervax/data/heartbeat}"   # path to the heartbeat file on the host
STATE="${KERVAX_WD_STATE:-/lib65/kervax/watchdog.state}"
MAX_AGE="${KERVAX_HB_MAX_AGE:-600}"                       # seconds: an older heartbeat raises an alarm
STRIKES="${KERVAX_WD_STRIKES:-2}"                        # consecutive failing checks before alarming
STAGE_STRIKES="${KERVAX_WD_STAGE_STRIKES:-5}"            # ticks in a row a stage may fail before alarming
NOW=$(date +%s)

# The heartbeat is read once: a regular file only (a symlink counts as missing), as a bounded
# snapshot with a timeout. nofollow refuses a symlink swapped in after the check right at open,
# nonblock keeps a FIFO from hanging root, and all values come from the same read (one read of up
# to 64 KB returns the whole regular file)
hb_ok=0; hb=""
if [ -f "$HB" ] && [ ! -L "$HB" ]; then
  hb_ok=1; hb=$(timeout 5 dd if="$HB" iflag=nofollow,nonblock bs=65536 count=1 status=none 2>/dev/null | tr -d '\000')
fi
val() { printf '%s\n' "$hb" | grep -m1 "^$1=" | cut -d= -f2-; }
# A number from the heartbeat or the state file: digits only (anything else is 0, so it never
# reaches arithmetic: "NOW[$(cmd)]" would run cmd), base 10 ("08" is not octal), at most 18 digits
num() { case "$1" in ''|*[!0-9]*) echo 0;; *) [ "${#1}" -gt 18 ] && echo 0 || echo $(( 10#$1 ));; esac; }

send() {
  local msg="$1" token chat api hook
  token=$(val tg_token); chat=$(val tg_chat); api=$(val tg_api); hook=$(val webhook)
  # The heartbeat is written by the container, while this curl runs as root: values go only as
  # the address (--url) and data. A non-http(s) address ("-K file", file://) is dropped; addresses,
  # token and chat must have no spaces or control characters. A chat may be a negative id
  # (-100...): it sits inside chat_id=, so it never becomes an option
  case "$api" in *[[:space:][:cntrl:]]*) api="https://api.telegram.org";; http://*|https://*) ;;
    *) api="https://api.telegram.org";; esac
  case "$hook" in *[[:space:][:cntrl:]]*) hook="";; http://*|https://*) ;; *) hook="";; esac
  case "$token" in -*|*[[:space:][:cntrl:]]*) token="";; esac
  case "$chat" in -|-*[!0-9]*|*[[:space:][:cntrl:]]*) chat="";; esac
  if [ -n "$token" ] && [ -n "$chat" ]; then
    curl -q -g -s --max-time 15 --proto =http,https --url "$api/bot$token/sendMessage" \
      --data-urlencode "chat_id=$chat" --data-urlencode "text=$msg" \
      --data-urlencode "disable_web_page_preview=true" >/dev/null 2>&1
  fi
  [ -n "$hook" ] && curl -q -g -s --max-time 15 --proto =http,https -H 'Content-Type: application/json' \
    --data "{\"text\": \"$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')\"}" --url "$hook" >/dev/null 2>&1
}

problem=""
if [ "$hb_ok" -eq 0 ]; then
  problem="heartbeat missing; check the scheduler"
else
  ts=$(num "$(val ts)"); ok=$(val alerts_ok); age=$(( NOW - 10#$ts ))
  if [ "$ts" -eq 0 ] || [ "$age" -gt "$MAX_AGE" ]; then
    problem="no heartbeat for ${age}s; check the panel"
  elif [ "$ts" -gt $(( NOW + 300 )) ]; then
    # a negative age would pass as fresh forever: a heartbeat from the future is not one
    problem="heartbeat is $(( 10#$ts - NOW ))s in the future; check the panel and host clocks"
  elif [ "$ok" = "0" ]; then
    problem="alert delivery failed; check Telegram and the token"
  else
    # older panels do not write these lines: empty values mean "nothing to check"
    tick=$(num "$(val tick)"); tick_max=$(num "$(val tick_max)"); sf=$(val stage_fail)
    sf_n=$(num "${sf%% *}")
    if [ "$tick" -gt 0 ] && [ "$tick_max" -gt 0 ] && [ $(( NOW - 10#$tick )) -gt "$tick_max" ]; then
      problem="tick stuck for $(( NOW - 10#$tick ))s; see Panel errors"
    elif [ "$sf_n" -ge "$STAGE_STRIKES" ]; then
      problem="stage failed ${sf_n} ticks; see Panel errors"
    fi
  fi
fi

# which panel exactly (from the heartbeat), so the alert says what went down
panel=$(val panel)
[ -z "$panel" ] && panel="$(hostname)"
[ "${#panel}" -gt 64 ] && panel="$(hostname)"
tag="🚑 Kervax watchdog [$panel]"

# Debounce: an alarm is sent only after STRIKES consecutive failing checks, so short-lived
# events (a panel restart during a deploy, a single channel blip) do not wake the watchdog.
# State: line 1 = whether an alarm was already sent (ok|problem), line 2 = the streak.
# alerted is the outstanding alarm; strikes counts consecutive detections before the first one.
alerted=$(sed -n 1p "$STATE" 2>/dev/null); [ -z "$alerted" ] && alerted=ok
strikes=$(num "$(sed -n 2p "$STATE" 2>/dev/null)")

if [ -n "$problem" ]; then
  strikes=$(( 10#$strikes + 1 ))
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
