#!/usr/bin/env bash
# Kervax: NVIDIA GPUs. A root helper asks nvidia-smi for load, memory, temperature, power, clocks,
# ECC and throttling of every GPU and for the processes on them (with their container, pod or
# systemd unit), reads Xid errors of the driver from the kernel log and lists the GPUs on the PCI
# bus with their bound driver, so the panel sees a GPU the driver lost. It writes
# /var/lib/kervax/report.d/gpu.json every minute. The agent hands report.d to the panel as is.
#
# Read only: nothing is installed and no GPU setting is changed. Without nvidia-smi (driver not
# installed) only the PCI bus and the kernel are reported.
#
# 0.4: a backslash in a name or a line (App\Jobs\Mail, a regex in a query) made the report invalid
# JSON on Debian and Ubuntu, whose awk is mawk: there gsub(/\\/, "\\\\") puts back one backslash,
# not two, and the agent drops a block that is not valid JSON - silently, the whole of it. Backslashes
# are doubled by splitting the string now, the same in every awk.
#
# 0.5: since when nvidia-smi fails (err_since): while the nvidia module is reloaded it fails for a
# minute, the panel waits before it says the driver does not answer. And when the driver took a GPU up
# again without loading the nvidia module (nv_rst, from the "[nvidia-drm] [GPU ID 0x...] Loading driver"
# line): with no processes on the GPU a reload of the modules lets the driver drop the card and start it
# anew, and that clears an Xid 62 without a reboot.
set -euo pipefail

KERVAX_SETUP_VERSION=0.5  # MAJOR.MINOR; compared component-wise
# nvidia-smi is there, or an NVIDIA display card (class 0x03xx) not bound to nouveau: a card on nouveau
# drives a monitor (hosters put them into Ryzen servers without integrated graphics), there is no CUDA on
# it, so the helper has nothing to read. A card with no driver at all still gets the helper. No $ in the
# condition: the assignment is double-quoted and runs under set -u in this very script
KERVAX_SETUP_WHEN="command -v nvidia-smi >/dev/null 2>&1 || grep -lsx 0x10de /sys/bus/pci/devices/*/vendor | sed s/vendor/class/ | xargs -r grep -ls ^0x03 | sed s/class/driver/ | xargs -r readlink -f | grep -qv drivers/nouveau"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-gpu"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/gpu.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# NVIDIA GPUs -> /var/lib/kervax/report.d/gpu.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/gpu.json
STATE=/var/lib/kervax/gpu
TMP="$OUT.tmp.$$"
now=$(date +%s)
install -d -m 0700 "$STATE"

# JSON string body: control characters dropped, backslash and quote escaped
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# -- GPUs on the PCI bus: VGA (0x0300) and 3D controllers (0x0302) of NVIDIA and the bound driver:
# nvidia, vfio-pci (given to a VM), nouveau, or none (the driver could not take the card) --
PCI=""
for d in /sys/bus/pci/devices/*; do
  [ "$(cat "$d/vendor" 2>/dev/null)" = 0x10de ] || continue
  case "$(cat "$d/class" 2>/dev/null)" in 0x0300*|0x0302*) ;; *) continue ;; esac
  drv=""
  [ -L "$d/driver" ] && drv=$(basename "$(readlink "$d/driver")")
  dev=$(cat "$d/device" 2>/dev/null || true)
  PCI="$PCI${PCI:+,}{\"bus\":\"$(esc "${d##*/}")\",\"dev\":\"$(esc "${dev#0x}")\",\"drv\":\"$(esc "$drv")\"}"
done

kernel=$(uname -r)
mod=false
[ -d /sys/module/nvidia ] && mod=true
# the module for the running kernel is on disk: after a kernel update without DKMS it is not
mod_built=false
if find "/lib/modules/$kernel" \( -name 'nvidia.ko' -o -name 'nvidia.ko.*' \) -print -quit 2>/dev/null | grep -q .; then
  mod_built=true
fi

smi=false; ok=false; err=""; driver=""; cuda=""
GPUS=""; PROCS=""
if command -v nvidia-smi >/dev/null 2>&1; then
  smi=true
  F=index,pci.bus_id,name,uuid,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu
  F=$F,power.draw,power.limit,clocks.sm,clocks.max.sm,pstate,fan.speed,persistence_mode
  F=$F,ecc.errors.uncorrected.volatile.total,ecc.errors.corrected.volatile.total,driver_version
  if timeout 20 nvidia-smi --query-gpu="$F" --format=csv,noheader,nounits > "$STATE/q" 2> "$STATE/q.err"; then
    ok=true
  else
    err=$(cat "$STATE/q" "$STATE/q.err" 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | cut -c1-300)
    [ -n "$err" ] || err="nvidia-smi exited with an error"
  fi
fi
# since when nvidia-smi fails, run after run: while the module is reloaded it fails for a minute
ERRSINCE=0
if $smi && ! $ok; then
  ERRSINCE=$(cat "$STATE/fail" 2>/dev/null || true)
  case "$ERRSINCE" in ''|*[!0-9]*) ERRSINCE=$now; echo "$now" > "$STATE/fail" ;; esac
else
  rm -f "$STATE/fail"
fi

if $ok; then
  # throttle reasons: clocks_event_reasons since driver 535, clocks_throttle_reasons before
  : > "$STATE/thr"
  for p in clocks_event_reasons clocks_throttle_reasons; do
    if timeout 20 nvidia-smi --query-gpu="index,$p.hw_thermal_slowdown,$p.sw_thermal_slowdown,$p.hw_power_brake_slowdown,$p.sw_power_cap" \
        --format=csv,noheader > "$STATE/thr" 2>/dev/null; then
      break
    fi
    : > "$STATE/thr"
  done
  # temperature limits, pages and rows of memory waiting for a reset; old drivers do not know
  # ROW_REMAPPER - then without it
  timeout 20 nvidia-smi -q -d TEMPERATURE,PAGE_RETIREMENT,ROW_REMAPPER > "$STATE/x" 2>/dev/null \
    || timeout 20 nvidia-smi -q -d TEMPERATURE,PAGE_RETIREMENT > "$STATE/x" 2>/dev/null \
    || timeout 20 nvidia-smi -q -d TEMPERATURE > "$STATE/x" 2>/dev/null || : > "$STATE/x"
  cuda=$(awk -F': *' '/^CUDA Version/ {print $2; exit}' "$STATE/x")
  # New drivers on data center GPUs (H100, H200) give no absolute limits, only T.Limit: the margin
  # in degrees to the thermal limit and the margins at which the GPU slows down, shuts down and
  # stops being within its operating range. Then the limit is current + margin - its margin
  awk '
    function val(s) { sub(/^[^:]*: */, "", s); gsub(/[^0-9.]/, "", s); return s }
    function sval(s) { sub(/^[^:]*: */, "", s); return (s ~ /^-?[0-9]+(\.[0-9]+)? *C/) ? s + 0 : "" }
    function yes(s) { sub(/^[^:]*: */, "", s); return s }
    function lim(a, d) { return (a == "" && cur != "" && tl != "" && d != "") ? cur + tl - d : a }
    function out() {
      slow = lim(slow, tls); shut = lim(shut, tlx); maxop = lim(maxop, tlm)
      printf "%s|%s|%s|%s|%s|%s|%s\n", tolower(bus), slow, shut, maxop, ret, rmp, rf
    }
    /^GPU [0-9A-Fa-f]+:/ { if (bus != "") out(); bus = $2; slow = shut = maxop = ret = rmp = rf = sec = cur = tl = tls = tlx = tlm = ""; next }
    /^    [A-Za-z][^:]*$/ { sec = $0; sub(/^ +/, "", sec); sub(/ +$/, "", sec); next }
    /GPU Slowdown Temp/ { slow = val($0) }
    /GPU Shutdown Temp/ { shut = val($0) }
    /GPU Max Operating Temp/ { maxop = val($0) }
    /GPU Current Temp/ { cur = sval($0) }
    /GPU T\.Limit Temp/ { tl = sval($0) }
    /GPU Slowdown T\.Limit Temp/ { tls = sval($0) }
    /GPU Shutdown T\.Limit Temp/ { tlx = sval($0) }
    /GPU Max Operating T\.Limit Temp/ { tlm = sval($0) }
    /Pending Page (Blacklist|Retirement)/ { ret = yes($0) }
    sec == "Remapped Rows" && /^ +Pending +:/ { rmp = yes($0) }
    /Remapping Failure Occurred/ { rf = yes($0) }
    END { if (bus != "") out() }' "$STATE/x" > "$STATE/xp"
  # frozen readings: load, power to a hundredth of a watt, temperature and clock the same run after
  # run. A live GPU never holds its power that still, even idle; an H200 NVL in a VM (driver 595)
  # kept every nvidia-smi value for hours while vLLM was serving. Not judged without a power reading
  # or when it comes in whole watts (x.00): an idle card could hold that for long
  touch "$STATE/frz"
  awk -v now="$now" -v old="$STATE/frz" '
    function trim(s) { gsub(/^[ \t]+|[ \t\r]+$/, "", s); return s }
    BEGIN { while ((getline l < old) > 0) { split(l, a, "|"); sig[a[1]] = a[2]; since[a[1]] = a[3] } }
    {
      n = split($0, f, ","); if (n < 19) next
      bus = tolower(trim(f[2])); pw = trim(f[10])
      s = trim(f[5]) "/" trim(f[6]) "/" pw "/" trim(f[9]) "/" trim(f[12])
      print bus "|" s "|" ((bus in sig && sig[bus] == s && pw ~ /^[0-9]+\.[0-9]*[1-9]/) ? since[bus] : now)
    }' "$STATE/q" > "$STATE/frz.new" && mv -f "$STATE/frz.new" "$STATE/frz"
  GPUS=$(awk -v thrf="$STATE/thr" -v xpf="$STATE/xp" -v frf="$STATE/frz" -v now="$now" '
    function trim(s) { gsub(/^[ \t]+|[ \t\r]+$/, "", s); return s }
    function num(s) { s = trim(s); return (s ~ /^-?[0-9]+(\.[0-9]+)?$/) ? s : "null" }
    function str(s,   _jp, _jn, _ji) { s = trim(s); gsub(/[\001-\037]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return "\"" s "\"" }
    function act(s) { return (trim(s) == "Active") ? "true" : "false" }
    function yn(s) { return (trim(s) == "Yes") ? "true" : "false" }
    BEGIN {
      while ((getline l < thrf) > 0) { split(l, a, ","); thr[trim(a[1])] = "{\"hw\":" act(a[2]) ",\"sw\":" act(a[3]) ",\"brake\":" act(a[4]) ",\"pcap\":" act(a[5]) "}" }
      while ((getline l < xpf) > 0) { split(l, a, "|"); x[a[1]] = l }
      while ((getline l < frf) > 0) { split(l, a, "|"); frz[a[1]] = now - a[3] }
    }
    {
      n = split($0, f, ",")
      if (n < 19) next
      i = trim(f[1]); bus = tolower(trim(f[2]))
      split(x[bus], q, "|")
      printf "%s{\"i\":%s,\"bus\":%s,\"name\":%s,\"uuid\":%s,\"util\":%s,\"mutil\":%s,\"mem\":%s,\"memt\":%s,\"temp\":%s,\"pw\":%s,\"pwl\":%s,\"clk\":%s,\"clkm\":%s,\"ps\":%s,\"fan\":%s,\"pm\":%s,\"ecc_u\":%s,\"ecc_c\":%s,\"slow\":%s,\"shut\":%s,\"maxop\":%s,\"thr\":%s,\"retire\":%s,\"remap\":%s,\"remap_fail\":%s,\"frz\":%s}",
        (c++ ? "," : ""), num(i), str(f[2]), str(f[3]), str(f[4]), num(f[5]), num(f[6]), num(f[7]), num(f[8]), num(f[9]),
        num(f[10]), num(f[11]), num(f[12]), num(f[13]), str(f[14]), num(f[15]), str(f[16]), num(f[17]), num(f[18]),
        num(q[2]), num(q[3]), num(q[4]), (i in thr ? thr[i] : "null"), yn(q[5]), yn(q[6]), yn(q[7]),
        (bus in frz ? frz[bus] : 0)
    }' "$STATE/q")
  driver=$(head -1 "$STATE/q" | awk -F',' '{gsub(/^[ \t]+|[ \t\r]+$/, "", $19); print $19}')

  # -- processes on the GPUs and whose they are: container, pod, systemd unit or user --
  declare -A DNAME
  owner_of() {
    kind=""; owner=""
    local cg id uid d
    cg=$(cat "/proc/$1/cgroup" 2>/dev/null) || return 0
    uid=$(printf '%s' "$cg" | grep -oE 'pod[0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12}' | tail -1)
    if [ -n "$uid" ]; then
      uid=$(printf '%s' "${uid#pod}" | tr _ -)
      kind=pod; owner="pod ${uid:0:8}"
      # the kubelet keeps logs in /var/log/pods/<namespace>_<pod>_<uid>: the name without the API
      for d in /var/log/pods/*_"$uid"; do
        [ -d "$d" ] || continue
        d=${d##*/}; d=${d%_"$uid"}
        owner="${d%%_*}/${d#*_}"
      done
      return 0
    fi
    id=$(printf '%s' "$cg" | grep -oE 'docker[-/][0-9a-f]{64}' | tail -1)
    if [ -n "$id" ]; then
      id=${id#docker?}
      kind=container; owner=${id:0:12}
      if [ -z "${DNAME[$id]+x}" ]; then
        DNAME[$id]=$(timeout 5 docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)
      fi
      [ -n "${DNAME[$id]}" ] && owner=${DNAME[$id]}
      return 0
    fi
    owner=$(printf '%s' "$cg" | grep -oE '[^/]+\.service' | tail -1)
    if [ -n "$owner" ]; then kind=unit; return 0; fi
    if printf '%s' "$cg" | grep -q 'user\.slice'; then
      kind=user; owner=$(stat -c %U "/proc/$1" 2>/dev/null || true)
    fi
  }
  timeout 20 nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv,noheader,nounits \
    > "$STATE/p" 2>/dev/null || : > "$STATE/p"
  while IFS=',' read -r uuid pid mem pname; do
    uuid=$(printf '%s' "$uuid" | tr -d ' '); pid=$(printf '%s' "$pid" | tr -d ' '); mem=$(printf '%s' "$mem" | tr -d ' ')
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    case "$mem" in ''|*[!0-9]*) mem=null ;; esac
    comm=$(cat "/proc/$pid/comm" 2>/dev/null || true)
    [ -n "$comm" ] || comm=$(printf '%s' "$pname" | sed 's/^ *//; s#.*/##')
    owner_of "$pid"
    PROCS="$PROCS${PROCS:+,}{\"gpu\":\"$(esc "$uuid")\",\"pid\":$pid,\"mem\":$mem,\"name\":\"$(esc "$comm")\",\"kind\":\"$kind\",\"owner\":\"$(esc "$owner")\"}"
  done < "$STATE/p"
fi

# -- Xid: errors the driver writes to the kernel log. Those of the last day go as they are (20 at
# most); from earlier in this boot the last one of every GPU and code after the driver was loaded:
# an error after which the GPU waits for a reset (Xid 62, 79, 119...) holds until a reboot or a
# reload of the driver, however old it is. They are kept in $STATE/xidb too, should the journal
# rotate. nv_load - when the driver was loaded, errors before it are gone with the old driver; nv_rst -
# when nvidia-drm took a GPU again after that (GPU ID is domain << 16 | bus << 8 | device), the panel
# counts it as a reset of that GPU once its readings move --
XID=""
NVRM=""
NVLOAD=0
NVRST=""
if command -v journalctl >/dev/null 2>&1; then
  boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
  journalctl -k -b -q -o short-unix --no-pager 2>/dev/null \
    | grep -E 'NVRM: (Xid|loading NVIDIA)|\[nvidia-drm\] \[GPU ID 0x[0-9A-Fa-f]+\] Loading driver' > "$STATE/xid" || true
  touch "$STATE/xidb"
  XID=$(awk -v now="$now" -v boot="$boot" -v old="$STATE/xidb" -v nb="$STATE/xidb.new" '
    function str(s,   _jp, _jn, _ji) { gsub(/[\001-\037]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return "\"" s "\"" }
    function put(t, bus, code, text,   k) {
      k = bus "|" code
      if (!(k in last) || t >= last[k]) { last[k] = t; txt[k] = text }
    }
    function js(t, bus, code, text) { return "{\"ts\":" t ",\"bus\":" str(bus) ",\"code\":" code ",\"text\":" str(text) "}" }
    function hex(s,   i, n) { s = tolower(s); n = 0; for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789abcdef", substr(s, i, 1)) - 1; return n }
    function busof(s,   n) { n = hex(s); return sprintf("%04x:%02x:%02x", int(n / 65536) % 65536, int(n / 256) % 256, n % 256) }
    BEGIN {
      load = 0
      if ((getline l < old) > 0 && split(l, h, " ") >= 3 && h[1] == "boot" && h[2] == boot) {
        load = h[3] + 0
        while ((getline l < old) > 0) {
          if (split(l, a, " ") == 3 && a[1] == "rst") { rst[a[2]] = a[3] + 0; continue }
          if (split(l, a, "|") >= 4) put(a[1] + 0, a[2], a[3], substr(l, length(a[1] a[2] a[3]) + 4))
        }
      }
    }
    {
      t = int($1)
      if (match($0, /\[GPU ID 0x[0-9A-Fa-f]+\] Loading driver/)) {
        b = busof(substr($0, RSTART + 10, RLENGTH - 26)); if (t > rst[b]) rst[b] = t; next
      }
      if (index($0, "NVRM: loading NVIDIA")) { if (t > load) load = t; next }
      m = substr($0, index($0, "NVRM: Xid ") + 10)
      bus = ""
      if (match(m, /^\(PCI:[0-9A-Fa-f:.]+\)/)) bus = substr(m, 6, RLENGTH - 6)
      sub(/^\([^)]*\): */, "", m)
      if (!match(m, /^[0-9]+/)) next
      code = substr(m, 1, RLENGTH)
      text = substr(m, RLENGTH + 1); sub(/^,? */, "", text); text = substr(text, 1, 200); gsub(/\|/, "/", text)
      put(t, bus, code, text)
      if (t >= now - 86400) { n++; rt[n % 20] = t; rb[n % 20] = bus; rc[n % 20] = code; rx[n % 20] = text }
    }
    END {
      print "boot " boot " " load > nb
      for (b in rst) if (rst[b] > load) print "rst " b " " rst[b] > nb
      for (k in last) {
        if (last[k] < load) continue
        print last[k] "|" k "|" txt[k] > nb
        if (last[k] < now - 86400 && held++ < 32) { split(k, a, "|"); o = o (o != "" ? "," : "") js(last[k], a[1], a[2], txt[k]) }
      }
      for (i = (n > 20 ? n - 19 : 1); i <= n; i++) { j = i % 20; o = o (o != "" ? "," : "") js(rt[j], rb[j], rc[j], rx[j]) }
      printf "%s", o
    }' "$STATE/xid")
  mv -f "$STATE/xidb.new" "$STATE/xidb" 2>/dev/null || true
  NVLOAD=$(awk 'NR == 1 { print $3 + 0 }' "$STATE/xidb")
  case "$NVLOAD" in ''|*[!0-9]*) NVLOAD=0 ;; esac
  NVRST=$(awk '$1 == "rst" && $2 ~ /^[0-9a-f:]+$/ && $3 ~ /^[0-9]+$/ { printf "%s{\"bus\":\"%s\",\"ts\":%s}", (c++ ? "," : ""), $2, $3 }' "$STATE/xidb")
  # the driver does not answer: its last words in this boot (API mismatch, failed to initialize)
  if [ -n "$err" ]; then
    NVRM=$(journalctl -k -b -q -o cat --no-pager 2>/dev/null | grep -E 'NVRM:|nvidia:|nvidia-' | grep -vF 'NVRM: Xid' | tail -1 | cut -c1-300)
  fi
fi

printf '{"v":1,"ts":%s,"smi":%s,"ok":%s,"err":"%s","err_since":%s,"driver":"%s","cuda":"%s","kernel":"%s","mod":%s,"mod_built":%s,"nvrm":"%s","pci":[%s],"gpus":[%s],"procs":[%s],"xid":[%s],"nv_load":%s,"nv_rst":[%s]}\n' \
  "$now" "$smi" "$ok" "$(esc "$err")" "$ERRSINCE" "$(esc "$driver")" "$(esc "$cuda")" "$(esc "$kernel")" "$mod" "$mod_built" \
  "$(esc "$NVRM")" "$PCI" "$GPUS" "$PROCS" "$XID" "$NVLOAD" "$NVRST" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"

cat > /etc/systemd/system/kervax-gpu.service <<EOF
[Unit]
Description=Kervax: NVIDIA GPUs (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-gpu.timer <<'EOF'
[Unit]
Description=Kervax: check the NVIDIA GPUs every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-gpu.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-gpu.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/gpu-setup.ver"
chmod 0644 "$STATE_DIR/versions/gpu-setup.ver"
echo "✓ gpu-setup: NVIDIA GPUs, processes and Xid errors -> $OUT (every minute)."
