#!/bin/sh
# Kervax: синк агентских IP в хостовый фаервол (вне docker).
#
# Панель ведёт файл <data>/agent_allow_ips — адреса, с которых агенты
# добавленных серверов ходят В панель (поле «IP сервера» в форме). Если хост
# панели закрыт ufw/firewalld, этот скрипт разрешает эти адреса на 80/443.
# Нет ни ufw, ни firewalld — тихо выходит (панели за Caddy-вайтлистом он
# не нужен: /api/agent/* там открыт и защищён токенами агентов).
#
# Установка (пример; скрипты держим в /lib65 — он в бэкапе, /usr — нет):
#   install -D -m755 ops/agent-firewall-sync.sh /lib65/kervax/agent-firewall-sync.sh
#   echo '*/2 * * * * root KERVAX_AGENT_IPS=/app/kervax/data/agent_allow_ips /lib65/kervax/agent-firewall-sync.sh' \
#     > /etc/cron.d/kervax-agent-fw
#   chmod 644 /etc/cron.d/kervax-agent-fw
#
# СИНК В ОБЕ СТОРОНЫ: добавили сервер — доступ открылся, удалили — закрывается.
# Чтобы автоудаление было безопасным, скрипт трогает ТОЛЬКО те адреса, которые
# открыл сам: их список он ведёт в своём файле состояния (STATE). Правила, заведённые
# руками или чем-то другим, он не видит и не удаляет, даже если адрес совпадает.
# Отключить удаление (поведение как раньше, только добавление): KERVAX_FW_PRUNE=0.

set -eu

# cron даёт урезанный PATH (/usr/bin:/bin) — ufw/firewall-cmd живут в sbin,
# без этого скрипт молча решал «фаервола нет» и ничего не открывал
PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH

IPS_FILE="${KERVAX_AGENT_IPS:-/app/kervax/data/agent_allow_ips}"
STATE="${KERVAX_FW_STATE:-/var/lib/kervax/agent-fw-opened}"
PRUNE="${KERVAX_FW_PRUNE:-1}"
[ -r "$IPS_FILE" ] || exit 0

CHANGED=0

have_ufw() { command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; }
have_fwd() { command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; }

add_ufw() {
  ip="$1"
  # INPUT — если панель слушает порт прямо на хосте
  ufw status | grep -q "ALLOW IN[[:space:]]*$ip\b.*kervax-agent" || \
    ufw allow proto tcp from "$ip" to any port 80,443 comment kervax-agent >/dev/null 2>&1 || true
  # FORWARD — если панель за docker-publish (ufw-docker): трафик к контейнеру
  # идёт через FORWARD, и «ufw allow» его НЕ пропускает — нужен route allow
  ufw status | grep -q "ALLOW FWD[[:space:]]*$ip\b.*kervax-agent" || \
    ufw route allow proto tcp from "$ip" to any port 80,443 comment kervax-agent >/dev/null 2>&1 || true
}

del_ufw() {
  ip="$1"
  ufw delete allow proto tcp from "$ip" to any port 80,443 >/dev/null 2>&1 || true
  ufw route delete allow proto tcp from "$ip" to any port 80,443 >/dev/null 2>&1 || true
}

add_firewalld() {
  ip="$1"
  for port in 80 443; do
    rule="rule family=ipv4 source address=$ip port port=$port protocol=tcp accept"
    firewall-cmd --query-rich-rule="$rule" >/dev/null 2>&1 && continue
    firewall-cmd --permanent --add-rich-rule="$rule" >/dev/null 2>&1 || true
    CHANGED=1
  done
}

del_firewalld() {
  ip="$1"
  for port in 80 443; do
    rule="rule family=ipv4 source address=$ip port port=$port protocol=tcp accept"
    firewall-cmd --permanent --remove-rich-rule="$rule" >/dev/null 2>&1 || true
    CHANGED=1
  done
}

# Список адресов, открытых ЭТИМ скриптом. При первом запуске после обновления его нет —
# восстанавливаем из ufw по метке kervax-agent, иначе ранее открытые правила остались бы
# «ничьими» и не снялись бы никогда.
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
if [ ! -f "$STATE" ]; then
  : > "$STATE" 2>/dev/null || true
  if have_ufw; then
    # адрес — ТРЕТЬЕ поле строки «80,443/tcp  ALLOW  <ip>  # kervax-agent».
    # По $(NF-1) брать нельзя: там оказывается «#», и сидинг молча давал пустоту
    ufw status 2>/dev/null | grep "kervax-agent" | awk '{print $3}' \
      | grep -E '^[0-9a-fA-F.:/]+$' | sort -u > "$STATE" 2>/dev/null || : > "$STATE"
  fi
fi

# нормализуем: только валидные адреса, никаких шелл-сюрпризов из файла
DESIRED=$(tr -d '\r' < "$IPS_FILE" | tr -s '[:space:]' '\n' \
          | grep -E '^[0-9a-fA-F.:/]+$' | sort -u || true)
OPENED=$(tr -d '\r' < "$STATE" | tr -s '[:space:]' '\n' \
         | grep -E '^[0-9a-fA-F.:/]+$' | sort -u || true)

# 1) открыть то, чего ещё нет
for ip in $DESIRED; do
  if have_ufw; then add_ufw "$ip"
  elif have_fwd; then add_firewalld "$ip"
  fi
done

# 2) закрыть то, что панель больше не просит (и только если открывали мы сами).
#
# ПРЕДОХРАНИТЕЛИ — проверено на себе: прогон с ЧУЖИМ файлом списка снёс все реальные
# правила разом, и панель осталась закрыта для всего парка. Поэтому:
#  • пустой список = «файл не тот / панель не отдала данные», а не «серверов нет» —
#    по такому поводу не закрываем ничего, только шумим в лог;
#  • за прогон снимаем не больше MAXPRUNE адресов: удаление сервера это 1-2 записи,
#    а «минус десять» почти всегда авария — лучше заметить, чем закрыться от парка.
DESIRED_N=$(printf '%s\n' $DESIRED | grep -c . || true)
OPENED_N=$(printf '%s\n' $OPENED | grep -c . || true)
MAXPRUNE="${KERVAX_FW_MAXPRUNE:-3}"
if [ "$PRUNE" = "1" ] && [ "$DESIRED_N" = "0" ] && [ "$OPENED_N" != "0" ]; then
  logger -t kervax-agent-fw "список агентских IP пуст ($IPS_FILE) — правила НЕ трогаю" 2>/dev/null || true
  PRUNE=0
fi
if [ "$PRUNE" = "1" ]; then
  pruned=0
  for ip in $OPENED; do
    printf '%s\n' $DESIRED | grep -qx "$ip" && continue
    if [ "$pruned" -ge "$MAXPRUNE" ]; then
      logger -t kervax-agent-fw "лишних адресов больше $MAXPRUNE — остальные оставил, проверьте список" 2>/dev/null || true
      break
    fi
    if have_ufw; then del_ufw "$ip"
    elif have_fwd; then del_firewalld "$ip"
    fi
    pruned=$((pruned + 1))
    logger -t kervax-agent-fw "закрыт доступ для $ip (сервера нет в панели)" 2>/dev/null || true
  done
fi

# Состояние = что РЕАЛЬНО открыто (у ufw спрашиваем сам фаервол), а не что мы хотели:
# адреса, которые не сняли из-за предохранителей, иначе стали бы «ничьими» навсегда.
if have_ufw; then
  ufw status 2>/dev/null | grep "kervax-agent" | awk '{print $3}' \
    | grep -E '^[0-9a-fA-F.:/]+$' | sort -u > "$STATE.tmp" 2>/dev/null \
    || printf '%s\n' $DESIRED > "$STATE.tmp"
else
  printf '%s\n' $DESIRED > "$STATE.tmp" 2>/dev/null || true
fi
mv -f "$STATE.tmp" "$STATE" 2>/dev/null || true

# firewalld применяет permanent-правила только после reload
[ "$CHANGED" = "1" ] && have_fwd && firewall-cmd --reload >/dev/null 2>&1 || true
exit 0
