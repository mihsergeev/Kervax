# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-08-23

Установка одной командой — и то, что нашлось, когда её прогнали на чистых
машинах несколько раз подряд.

### Added
- **`ops/quickstart.sh` — установка одной командой.** Ставит Docker, если его
  нет, поднимает caddy-docker-proxy, придумывает секреты, поднимает панель и
  печатает адрес с паролем. Домен не обязателен: без него берётся
  `<IP-сервера>.sslip.io`, и Let's Encrypt выдаёт на это имя обычный сертификат —
  посмотреть панель можно, ничего не регистрируя.
- **`compose.ghcr.yml`** — запуск из готовых образов вместо сборки из исходников.
  Сборка требует ~2 ГБ памяти и нескольких минут (Go-агент под две архитектуры и
  фронтенд); на маленьком VPS проще взять образы, собранные тем же тегом в CI.
- **`docs/install.md` / `docs/install.ru.md`** — подробная инструкция: быстрый
  путь, установка по шагам, свой домен, вайтлист по IP, обновление, удаление и
  таблица «симптом → причина → что делать». Всё проверено на чистой Ubuntu 26.04
  с 2 ГБ: и сборка из исходников, и готовые образы, и агент на второй машине.
- README получил раздел «Из коробки» — сравнение с типовым стеком на Prometheus
  по задачам, а не по галочкам, с честной оговоркой, где Prometheus сильнее.

### Fixed
- **Обновление на готовых образах не обновляло панель.** `docker compose up -d`
  поднимает образ, который уже лежит на диске, поэтому `git pull` менял файлы
  репозитория, а панель оставалась прежней версии — и ничего об этом не говорила.
  `quickstart.sh` теперь запускает с `--pull always`, а в инструкции для этого
  пути явным шагом стоит `docker compose pull`. Поймано на четвёртом прогоне
  установки: свежий клон работал со старым образом.
- **У монитора типа «сертификат» срок никуда не доезжал.** Дни до истечения
  считаются самой проверкой и лежат в `last_value`, а всё остальное — чип срока
  в списке, блок «истекает» на главной, группировка по второму уровню домена —
  построено на `ssl_days`. Заполнял его отдельный проход по срокам, который
  ходит только к http-мониторам, так что у cert-мониторов поле оставалось
  пустым: данные посчитаны, показать их нечем. Теперь результат cert-проверки
  пишется и в `ssl_days`. Найдено при сквозной проверке живой панели.
- **`compose.ghcr.yml` поднимал пустой контейнер планировщика.** Оверлей упоминал
  `scheduler` «на случай scale-режима», но объявление сервиса в оверлее его
  СОЗДАЁТ, а не дополняет: в обычном режиме рядом с рабочей панелью появлялся
  контейнер без единой переменной окружения и без томов, который падал на первой
  же попытке открыть базу и оставался в `Exited (1)`. Установка при этом
  выглядела успешной. Планировщик убран в отдельный `compose.ghcr-scale.yml`, а
  `ops/selfcheck.py` теперь ругается на любой оверлей, вводящий сервис, которого
  нет в базовом compose. Найдено повторным прогоном установки с нуля.
- **`KERVAX_PANEL_URL` не было в `.env.example`.** Переменная нужна для команды
  установки агента и ссылок в алертах: без неё панель показывала
  `curl … https://ПАНЕЛЬ/api/agent/install.sh`, и команду приходилось править
  руками. Обнаружено при установке с нуля по собственной инструкции.

## [1.1.0] - 2026-08-23

Everything below ships in one release: the panel now runs unprivileged, its
English is actually English, and the screenshots come from the panel itself.

### Security
- **The containers no longer run as root.** The backend and the scheduler run as
  `kervax` (uid 10001); the container still starts as root, but only to fix
  ownership of the mounted `./data` — a volume created by the previous,
  root-running image — and then drops privileges via `gosu`. Nothing to do by
  hand when upgrading. The frontend moved to the unprivileged nginx image (uid
  101), which listens on **8080** instead of 80.
  **If you proxy to the frontend container directly** (rather than through the
  published `KERVAX_BIND` port or the bundled caddy overlay, both of which are
  updated), point your reverse proxy at port 8080.
- **Minimum password length is now 12 characters**, up from 8, and the rule lives
  in one place (`MIN_PASSWORD_LEN`) instead of being written out as a number in
  three schemas and four spots in the UI. The initial `KERVAX_ADMIN_PASSWORD` is
  checked too — but only when the admin account is actually being created, so an
  existing installation with a shorter value left over in `.env` still starts
  (that value has long stopped being used). `ops/selfcheck.py` now fails if the
  frontend and backend minimums drift apart.
- **Admin-only endpoints now require an admin on the backend, not just in the menu.**
  Alert settings, alert rules, retention, the audit log and location editing were
  protected only by the fact that the menu item is visible to admins — with a token
  in hand any account, including read-only, could call them directly. `GET
  /api/alerts` returns `telegram_token` and the webhook; an editor could silently
  disable alert rules and leave the fleet unmonitored. Verified against a live
  panel before the fix: a viewer got 200 on all of them.
- **Secrets no longer travel to accounts that cannot edit them.** `GET /api/checks`
  returned `auth_pass` (the password to a protected part of a site) and
  `http_headers` (often `Authorization: Bearer …`) to every role; `GET
  /api/locations` returned proxy URLs with `user:pass` in them. Both are now hidden
  from read-only accounts, and the backup repository password — the key that
  decrypts every backup — requires an admin (its own docstring already said so,
  the signature did not).
- **The Telegram bot token no longer leaks into the logs.** httpx logs the URL of
  every request at INFO level, and the bot token lives right in the path
  (`/bot<token>/sendMessage`), so a valid secret was sitting in the container logs
  — readable by anyone who could reach `docker logs`, and carried along into any
  log excerpt shared elsewhere. The httpx logger is now capped at WARNING in both
  the web and scheduler processes. **Rotate the bot token** if the logs were ever
  exported or shared.

### Fixed
- **The English panel was partly Russian.** Units (`41 ГБ`, `2ч 30м`, `Б/с`), the
  backup-window notices, the generic HTTP error and — worst of the lot — the bash
  and YAML snippets the panel hands you to paste into a terminal all stayed in
  Russian under the English locale. They had one thing in common: the text was
  assembled in template literals and unit tables, never passing through `t()`.
  There is now a `tr()` for code outside the React tree, a single `units.ts`
  instead of five copies of the byte table (exactly one of which was localised),
  and about seventy new dictionary entries. `ops/selfcheck.py` grew a check that
  looks for Russian text bypassing the translator, so the gap cannot silently
  reopen — the previous i18n check only saw strings already wrapped in `t()`.
- **"helper is outdated (vv0.19 → vv0.23)"** — the English template added a `v`
  that `fmtSetupVersion` had already added, and an unknown version rendered as
  `v?`. Russian was unaffected, which is why it survived this long.
- **Site alerts lost the address, the link and the snooze check.** Adding an eighth
  element to the pending tuple (the expiry icon) shifted a positional unpack that
  read fields from the end, so `check_id` picked up the flag instead — alerts arrived
  as a bare "🔴 — down" with no address and no link, and per-monitor snoozes and mutes
  were not applied at all. The queue element is a `NamedTuple` now, with the new field
  defaulted, so adding one no longer breaks every unpack site.
- **Six backend tests had been red before anyone noticed** — the CI workflow only
  runs on GitHub and this repository has no remote, so nothing ever executed it.
  Five failed on `_PANEL_STARTED`, which is captured at import time: in tests the
  panel has always "just started", and offline alerts are deliberately suppressed
  for the first minutes, so those tests were quietly asserting on an empty list. A
  conftest fixture now moves the start time into the past. The sixth predated the
  one-day grace before the first "backup not configured" alert.
- **"Expired" shown for a domain/certificate that had not expired yet.** Zero days
  left was treated as expired everywhere — in the badge, in the detail pill and in
  the alert text — even though the actual deadline could still be almost a day away
  (a live site showed a red "domain expired" on its renewal date). `_days_until` made
  it worse by truncating toward zero, so "in 20 hours" and "two hours ago" both came
  out as `0`; it now floors, so anything past its date goes negative. Zero now reads
  "today". Related gap closed: the last reminder used to be "1 day left" and the
  moment of actual expiry passed silently, because no tighter threshold followed —
  a virtual threshold now fires exactly one after-the-fact notice.

### Added
- **README screenshots come from a running panel now.** `docs/demo-seed.py` fills a
  throwaway SQLite database with invented nodes and monitors (RFC 2606 domains,
  RFC 5737 addresses), `docs/make-shots.py` starts the panel on it and
  `frontend/scripts/shots.mjs` walks the sections and captures the frames — English
  and Russian separately, since monitor names and error texts are data and the panel
  does not translate them. The pictures therefore cannot drift away from the actual
  interface, and none of them show a real host.
- **`docs/why.md` / `docs/why.ru.md`** — what the panel takes over, and an explicit
  list of what it is *not* (no APM, no log storage, not a Prometheus replacement,
  not a long-term data warehouse). `SECURITY.md` in the repository root now carries
  the reporting policy and the deployment notes that matter most.
- **Per-account Telegram alerts.** The panel had a single channel: everything landed
  in one shared chat, and "whose problem is this" was worked out by eye. Each account
  can now link its own chat and receives only what it can see in the panel — the
  filter comes from the RBAC already configured (sections + groups), so a person's
  area of responsibility is set once rather than twice. The shared bot is used by
  default (staff only press Start; no BotFather needed); an optional per-account
  token covers a separate bot or a personal proxy to api.telegram.org. Linking is
  done with a one-time code sent to the bot rather than by typing a chat id — you
  cannot learn your own id without third-party bots, and mistyping someone else's
  would quietly forward your infrastructure alerts to a stranger. Delivery is
  best-effort by design: one revoked bot must not make the panel re-send an alert to
  everybody else forever. New `/api/auth/telegram*`, migration 0061.
- **"Not worth monitoring" for discovered domains.** The "N domains found off
  monitoring" banner used to sit there permanently — agents see dev stands, internal
  dashboards and parked domains that nobody intended to monitor, and a counter that
  never clears stops meaning anything. Such a domain can now be dismissed (one by
  one or in bulk) and brought back later from the "not needed" list. The list is
  panel-wide rather than personal: "this dev stand needs no monitoring" is a fact
  about the infrastructure. Table `ignored_domains` (migration 0060), `POST
  /api/checks/discovered/ignore`.
- **"Add sites to monitoring" wizard — put discovered domains under monitoring.** The
  agent already collects hostnames from nginx/Apache/Caddy and from Kubernetes Ingress
  and Gateway API routes, but turning them into monitors meant retyping them by hand.
  The wizard lists them with checkboxes grouped by domain zone (tick a whole zone at
  once), a search over domains and nodes, a "new only" toggle, a group picker and a
  "monitors to create" counter. Two entry points, one screen: from Services for a
  single web service, and from Sites — a banner "domains found on servers but not
  monitored: N" answers a question the section could not ask itself, since it only
  knows what someone once added manually. Already-monitored domains show a tick that
  opens the monitor; wildcards (`*.example.com`) and `server_name` regexps show a dash
  — an HTTP monitor needs a concrete address. Duplicates are refused even when the
  existing monitor sits in a group the account cannot see. New `GET
  /api/checks/known-hosts`, `GET /api/checks/discovered` and `POST /api/checks/adopt`;
  all three live under the Sites section, so an account without it never sees the
  feature, and discovery is filtered by the account's server groups.
- **"Test" button for location proxies — save only a reachable one.** Adding/editing
  a proxy location now has a Test button that routes a request to a neutral endpoint
  through the proxy; Save is blocked until a non-empty proxy passes (empty = direct
  is always allowed, and an unchanged existing proxy on edit isn't forced to re-test).
  New `POST /api/locations/test`. Prevents dead proxies from silently marking every
  monitor "down" from that location.
- **Per-IP breakdown in the monitor detail** (for monitors with "check all IP
  addresses" on). A new "Domain IP addresses" section lists every resolved IP with
  a status dot, latency and the HTTP code/message it returned — so you can see at a
  glance which backend is green and which is failing. Stored as the last run's
  snapshot (`checks.last_ip_results`, migration 0035); IPs rotate, so it's the
  current picture rather than per-IP history.
- **Optional "check all of a domain's IP addresses"** (per-monitor, default off,
  HTTP only). Normally the OS resolver picks one A-record; a dead backend behind a
  round-robin/LB can go unnoticed. When enabled, the monitor probes every IPv4
  address of the domain — each pinned to its IP but with `Host` header and TLS
  SNI/verification against the hostname (httpx `sni_hostname` extension). Any dead
  address → the monitor goes down and names the failing IP. IPv4-only on purpose
  (resolving AAAA on a host without IPv6 routing would fake "down"). Column
  `checks.check_all_ips` (migration 0034).

### Changed
- **Disk alerts: no severity word, and the recovery says what changed.** The leading
  icon already tells the level apart (⚠️ / 🔴 / 🚨), so the trailing "(предупреждение)"
  was only making the line longer — the default template is now `диск {value}% ≥
  {threshold}%` (`{severity}` still works in custom templates). In return, the
  all-clear for threshold alerts carries the numbers: `диск снова в норме: 82%
  (было 90%)` — "back to normal" alone never answered the first question, how much
  actually freed up. The announced value is remembered in `alert_state.<kind>_val`
  and re-written on escalation, so "было" is the last figure the operator saw. Same
  for CPU/RAM/conntrack (%) and CPU/disk temperature (°C).
- **Proxy-location checks are softened (fewer false "unavailable" from distance).**
  A far site through a proxy (e.g. a Brazilian endpoint via a Kazakhstan proxy) is
  slower and flakier, which tripped false location-down alerts. Proxy probes now get
  a bigger timeout (`location_timeout_extra_ms`, +10 s), the raised slow threshold
  (as before), and internal retries (`location_retries`, 2) before reporting down —
  a live-but-slow site no longer flaps red. Direct checks are unchanged.

### Fixed
- **Alert messages: two links, no leaked tokens, cleaner layout.** A site alert now
  has two hyperlinks — the **address** opens the actual site (to eyeball it), and a
  separate **"монитор"** opens the monitor in the panel. Both the shown address and
  the site link drop the query string (targets like `…/get-active?access-token=…`
  were exposing secrets in Telegram) and any `user:pass@`. When the monitor's name
  is just its domain the address isn't printed twice anymore (`✅ vault.example.com —
  … · монитор`); custom names (e.g. with @mentions) print as plain text so Telegram
  tagging keeps working.
- **Location probes stored nothing after adding the first proxy location.** A newly
  created `LocationResult` has `consecutive_fails = None` (the `default=0` only lands
  on flush); the first *down* probe of a new (monitor, location) pair did `None + 1`
  and threw, aborting the whole location-probe batch before commit — so *every*
  location (even working ones) stayed empty ("—"). Now guarded with `(… or 0)`.

### Added
- **Quick snooze of alerts (1 h / 1 d / 1 w).** A monitor's or server's detail now
  has one-click "Snooze alerts: 1h / 1d / 1w" buttons (and "Unsnooze"), right where
  the alert links to — no digging through settings. While snoozed, that entity sends
  no alerts (state isn't marked, so a still-open problem re-alerts after it expires);
  a badge shows "snoozed until …". New `checks.snooze_until` / `servers.snooze_until`
  columns (migration 0040) and `POST /api/{checks,servers}/{id}/snooze`.
- **OOM-kill detection + dashboard + alert.** Agent reads the kernel's
  `/proc/vmstat oom_kill` counter (no root needed) and reports OOM kills per interval.
  The panel stores it (`server_metrics.oom_kill`, migration 0039), shows an "OOM
  kills" chart in a server's Memory tab, and fires a one-shot, actionable alert
  ("kernel killed N processes out of memory") — no recovery spam (it's an event).
  Mutable per-server. OOM is accumulated at ingest (`servers.oom_total`, migration
  0041) and alerted on a high-water mark, so no kill is missed (the per-interval
  delta only lived in last_report for one tick and the poll could skip it). Agent
  1.20 best-effort reads `/dev/kmsg` for the victim (`mysqld (pid 1234)`) and the
  alert names it; that read needs `CAP_SYSLOG`, now granted by the systemd unit
  (`AmbientCapabilities`) — existing nodes get it after re-running install (OTA swaps
  only the binary, not the unit).
- **Custom HTTP headers per monitor.** An HTTP monitor can now send extra request
  headers (JSON, e.g. `{"x-application-token": "…"}`) — for sites that return 401
  without a token/header. New `checks.http_headers` column (migration 0037); the
  add/edit form has a JSON textarea with a validity hint; parsing is tolerant
  (invalid JSON is ignored, never breaks the check). Merges over the default
  User-Agent.
- **"Ignore TLS/SSL errors" per monitor.** For sites with a self-signed, expired,
  or wrong-host certificate that must still be monitored — the main HTTP check runs
  with `verify=False`. New `checks.ignore_tls` column (migration 0038); checkbox in
  the add/edit form.
- **Apply settings to selected monitors (not just all).** In select mode a
  "Configure (N)" button opens the bulk-settings modal scoped to the checked
  monitors — set interval/timeout/retries/thresholds/expected-status, toggle
  locations, check-all-IPs, SSL/domain watch on just that batch. Bulk-delete of the
  selection already existed. Backend `/checks/bulk` now takes an optional `ids`
  list (absent = all, as before); added `check_all_ips`, `method`, `expected_status`
  to the bulk-updatable fields.
- **Bulk toggle for "check from locations" (and SSL/domain watch).** "Apply to all
  monitors" now has on/off switches, so you can turn location (proxy) checking on or
  off for every monitor at once instead of per-monitor. New monitors now default to
  **locations off** (`check_locations=false` in CheckCreate and the add form) — proxy
  checking is opt-in, so adding a proxy location no longer floods RU-only internal
  sites with "partial availability" alerts.
- **Per-IP response-time graph.** In the "Domain IP addresses" section each IP row
  is now clickable and drives the response-time chart for that specific backend
  (new `check_ip_samples` time-series, migration 0036; `/history?ip=` param). Same
  click-to-view already worked for locations. Selecting an IP or a location swaps
  the chart between them.

### Fixed
- **False watchdog "panel is silent" alerts.** The heartbeat was written only at
  the end of a scheduler tick; once the concurrency cap (below) stretched ticks on
  panels with hundreds of monitors, and across container restarts, the heartbeat
  could look stale enough (>600 s) to trip the host watchdog even though the panel
  was fine — just busy. The heartbeat now runs as an independent 60 s task that
  writes immediately on startup, decoupled from tick duration; real failures
  (dead process, hung loop, DB down) still stop it. The watchdog message now names
  the specific panel (`panel=` from the heartbeat), so "[kervax.example.com]" vs
  "[kervax.example.org]" is obvious.

### Changed
- **Outbound checks are now spread out instead of firing all at once.** The
  scheduler used to `asyncio.gather` every due monitor simultaneously — with
  hundreds of monitors that's a traffic/CPU spike each tick. Now runs through a
  concurrency cap (`check_max_concurrency`, default 50) plus a small per-check
  start jitter (`check_jitter_ms`, default 400 ms), so at most N requests are in
  flight at any instant and they don't all launch on the same millisecond. Applies
  to direct checks, expiry probes, and location/proxy probes. Set concurrency to 0
  to restore the old unbounded behavior.
- **CPU-throttle alerts no longer fire on momentary blips (false positives).**
  The Intel core throttle counter ticks up for sub-second micro-spikes even at
  49–77 °C and 4–10 % CPU (observed on an i9-13900: 6 isolated 1–2-tick events in
  24 h, each self-recovering next interval) — not a cooling problem, so alerting
  on any `throttle > 0` was pure noise. Now an interval only counts as real
  throttling when the CPU is genuinely hot (≥ 80 °C), and the alert fires only
  after ≥ 3 such intervals in a row (sustained under-cooling that an engineer
  should act on). Streak is tracked in `alert_state.throttle_streak`; default
  message reworded to "устойчивый тепловой троттлинг CPU (N интервала подряд)".
  Guiding principle: alerts are for things a human must act on, not info noise.
- **Site alerts got a clean default format with a clickable monitor name.**
  Instead of `🔴 Kervax: «name» — недоступен: msg` + a separate `🔗` link line,
  the default is now `address 🔴 — <name-as-link> — error text` — the monitor's
  host/path up front, a colored icon, the panel name hyperlinked straight to the
  monitor (Telegram `parse_mode=HTML`), and the error text. No branding, no clutter.
  Recovery now carries the up-message (`HTTP 200 · N ms`). Multi-location partial
  outages keep the per-location list under the same header. Sent via `parse_mode`
  HTML with every dynamic field escaped; the digest wrapper lost its "Kervax:"
  prefix too. Custom per-rule templates still render plain (with the `🔗` line).
  Installs that had saved the old default rules are recognized (LEGACY defaults)
  and upgraded to the rich format automatically.
- **Rebranded to Kervax.** New falcon logo from the brand kit (wordmark baked into
  the SVGs): horizontal lockup (mark + KERVAX to the right) in the app header,
  vertical lockup (wordmark below the mark) on the login screen; dark/light logo
  variants switch with the theme. New favicons/PWA icons (white falcon on brand
  navy `#09172e`), page title, manifest, alert message prefixes, TOTP issuer and
  monitor User-Agent (`KervaxMonitor/1.0`). Technical identifiers are unchanged
  (env prefix `KERVAX_`, agent binary `kervax-agent`, localStorage keys) so
  existing deployments and agents keep working.

### Added
- **Manual drag & drop for monitors and groups.** Each monitor row and each group
  header has a drag handle. Drag a monitor to reorder it, move it into another
  group (updates its `group_name`), or drag a whole group header to reorder groups.
  Group order is manual (first appearance in `sort_order`), "no group" stays last.
  Order and group membership persist (`checks.sort_order`, migration 0030); new
  monitors append to the end. Health-based auto-sort is replaced by this manual
  order (status tiles still filter to problems). Endpoint: `POST /api/checks/reorder`
  (`{order: [{id, group_name?}]}` — order + optional regroup in one call).
- **Monitor count badge** next to each group name.
- **"Disabled" tile on the Sites page** (shown when there are any) — one click
  filters to disabled monitors, so they're findable among hundreds. Status tiles
  (Up/Degraded/Down) now count only ENABLED monitors; disabled ones sit in their
  own counter and no longer pollute "Down".
- **The Alerts dialog got wider** (680px) — bot tokens and API URLs fit without
  clipping.
- **Search box on the Sites page** (shown at 6+ monitors) — filters by name,
  address and group as you type. Drag-reorder is disabled while a search is
  active (dropping a filtered subset would corrupt the global order).
- **Bulk monitor deletion.** A "Select" button switches the list into selection
  mode: checkboxes on rows (clicking a row toggles it), "All visible" respects
  the active filter/search — so deleting every disabled monitor is two clicks —
  then "Delete (N)" removes them with history/incidents.
  Endpoint: `POST /api/checks/bulk-delete`.
- **The monitor Name field is full-width** in the add/edit form (long names with
  @mentions fit), with a hint that the name goes into alert text — @mentions in
  it tag people in Telegram.
- **Multiple agents per server (different panels).** `install.sh` takes an
  optional third argument — an instance name: the agent installs as a systemd
  template unit `kervax-agent@<name>` with its own `/etc/kervax-agent-<name>.conf`
  (url+token), sharing the binary with the default agent. So one server can push
  metrics to several panels side by side. The binary download now goes through a
  tmp file + atomic rename (no more "Text file busy" when another instance is
  running). The "Server created" dialog hints at this.
- **Agent 1.18 — rebrand leftovers purged from the binary.** The shipped 1.17
  binary predated the rebrand and still printed `uptrion-agent` in logs; 1.18 is
  rebuilt from the renamed sources (`kervax-agent` everywhere). Rolled out to all
  agents via signed OTA. `agent-signing/release.py` fixes: built binaries are now
  copied into `agent-dist/` next to the manifest (they used to die with the temp
  dir, leaving the manifest pointing at a non-existent build), and remote builds
  fall back to `sudo -n docker` for non-root build hosts.
- **Live connect check in the "Server created" dialog.** After running the
  install command, the dialog polls and flips to a green "Agent connected:
  hostname · vX — metrics are flowing" on the first report (plus a Done button),
  so you don't have to close the window blind and hunt for the server card.
- **Firewalled panels: agents can now reach a locked-down panel.**
  - The Caddy allow-list override (`compose.caddy.yml`) serves `/api/agent/*`
    to ANY IP via a dedicated handle block (agent endpoints authenticate with
    per-server tokens) while everything else stays IP-restricted. Rewritten from
    suffixed `not_N` matchers to `handle` blocks — works on caddy-docker-proxy
    2.8 and 2.12 alike.
  - New optional **"Server IP"** field when adding/editing a server: the panel
    maintains `data/agent_allow_ips`, and the host-side
    `ops/agent-firewall-sync.sh` (cron) allows those addresses on 80/443 in
    ufw (both INPUT and FORWARD — docker-published ports need `route allow`)
    or firewalld. New `servers.agent_ip` column (migration 0032).

### Fixed
- **agent-firewall-sync.sh actually works from cron now.** Cron's trimmed PATH
  (`/usr/bin:/bin`) doesn't include `/usr/sbin`, so `command -v ufw` silently
  concluded "no firewall here" and never opened anything — new servers' agents
  timed out even with the Server IP filled in. The script now prepends sbin dirs
  to PATH. Also: `install.sh` and the install command use
  `curl --connect-timeout 15` with a clear error, instead of hanging for minutes
  against a firewalled panel.
- **Domain-expiry probes no longer hammer registries (RDAP HTTP 429).** Results
  are cached per registrable domain (6 h TTL; errors retried after 15 min), and
  real RDAP/WHOIS queries go through a single serial queue with a 1.1 s gap —
  hundreds of monitors on subdomains of one domain now cost ONE registry query
  instead of a parallel burst that got rate-limited.
- **"Issues" filter tile on the Servers page** — one click to show only servers
  that need attention (offline OR a breached threshold: CPU / RAM / disk / CPU temp
  / throttling / conntrack / disk temp). Server issue detection extended to cover
  temperature, throttling and conntrack (was CPU/RAM/disk only).
- **Nav sections "Докер" and "Кубер"** (placeholders) next to "Сервера" — future
  Docker / Kubernetes monitoring, stubbed for now.

### Fixed
- **HTTPS-only failures no longer hide behind the HTTP fallback.** When a monitor
  is added without a scheme, we still try `https://` first, but if HTTPS fails and
  the site answers only over HTTP, the monitor is now **degraded** (message
  "HTTPS недоступен, отвечает только HTTP") instead of a misleading green "up".
  The fallback now triggers on any HTTPS transport failure (connect / SSL / dropped
  connection / timeout), not just connection refused. To intentionally monitor an
  HTTP site, type `http://` explicitly — it stays "up".

### Changed
- **VMs no longer show temperature alerts.** On virtual machines (no thermal
  sensors) the CPU-temp and disk-temp alert thresholds and the temperature mute
  chips are hidden in the server edit form.
- **"Delete server" moved to the top** of the server edit form (away from "Save")
  to prevent accidental clicks.

## [1.0.0] - 2026-07-14

First public release.

### Added
- **Self-monitoring watchdog (dead-man's-switch).** The scheduler writes a
  heartbeat file (`<data>/heartbeat`) every ~minute with a timestamp, an
  alert-channel self-test flag (Telegram `getMe`), and the channel credentials.
  A host-side cron script (`ops/panel-watchdog.sh`, installed outside Docker)
  reads it and alerts **independently** — via the creds from the heartbeat — when
  the pulse goes stale (panel/scheduler/DB dead or hung) or the alert channel is
  broken (expired token / Telegram blocked). This catches exactly the cases the
  panel can't report about itself.

### Fixed
- **Location alerts no longer flap.** A flaky proxy location (intermittent
  timeout) used to fire a "partial availability" alert on every single status
  flip. Location status is now debounced: a location counts as down only after
  the monitor's "alert after N consecutive failures" threshold — same as the
  main alert — for both proxy and direct locations. New
  `location_results.consecutive_fails` (migration 0028).

### Changed
- **Simpler two-level alert config.** The global alert-rules editor lost the
  per-rule scope picker that conflated "which alert types are on" with "which
  servers they apply to". Now each rule is just a toggle + message text and
  **applies to everything**. Per-entity customisation moved to where the entity
  lives: the server/monitor edit form has its thresholds (already there) plus
  **mute chips** to silence specific alert types for that one server/monitor
  (new `servers.alert_mutes` / `checks.alert_mutes`, migration 0029).
- **Compact monitor list with per-monitor status bar.** Monitor rows are denser
  (more fit on screen), the whole row is clickable, and the run/edit/delete
  buttons moved into the monitor detail (run + delete added to its header) to
  declutter. Each row now shows a mini heartbeat bar of the last ~30 checks
  (green/amber/red) for at-a-glance recent status — served by a new `beats` field
  on the overview endpoint (a windowed last-N query, cheap for any monitor count).
- **Enterprise chart restyle (Datadog-like).** Desaturated the chart palette and
  cut area-fill opacity so dense overlays (per-core, per-interface, per-device)
  read as clean thin lines instead of muddy overlapping blocks: overlay fills
  ~14%, stacked/mirror ~30%. Gradient ids are now keyed by (mode + colour) to
  avoid a fill bleeding between an overlay and a stacked chart that share a hue.
  Server-detail charts are now full-width (one per row, taller) and the panel
  widens to `min(1360px, 94vw)` — bigger, more legible panels on wide screens.
  Added a sticky left-rail section nav (CPU / memory / network / connections /
  disk / processes) with scroll-spy highlighting; clicking a section expands
  and scrolls to it — quick jumping around the now-taller detail view, plus a
  floating "back to top" button that appears once you scroll down. The
  full-screen chart (⤢) is now genuinely large — `min(2200px, 96vw)` wide and
  ~64% of the viewport tall (was a fixed 1100×420, smaller than the inline charts).

### Added
- **Alert flood control.** When many alerts fire in one evaluation cycle (a rack
  or upstream going down at once), the panel now coalesces them into a single
  digest message instead of flooding the channel. The threshold is configurable
  (`flood_threshold`, default 6, 0 = off) and applies to both server and monitor
  alerts; below the threshold, alerts are sent individually as before.
- **Alerts for conntrack fill and disk temperature.** Two new server alert
  conditions: **conntrack near-full** (fires when the conntrack table is ≥ a
  per-server threshold, default 90%, only where conntrack exists) and **disk
  temperature** (fires when the hottest disk with a sensor is ≥ a per-server
  threshold, default off). Both thresholds are editable in the server edit form
  (`conntrack_alert_percent`, `disk_temp_alert_c`, migration 0027), and both
  appear as toggleable/editable rules in the server alert-rules editor. Panel-only
  change — consumes data agent v1.17 already reports.
- **Connection tracking, sockets & disk temperature.** The agent now reports
  the conntrack table usage (`nf_conntrack_count`/`nf_conntrack_max`), socket
  counts from `/proc/net/sockstat(+6)` (total, TCP in-use, TCP time-wait, UDP
  in-use) and per-disk temperature from sysfs `hwmon` (drivetemp for SATA, nvme
  for NVMe). A new **Connections** section charts conntrack fill and sockets
  (TCP/time-wait/UDP); the **Disk** section gains a per-device **temperature**
  chart that auto-hides where there's no sensor (e.g. VMs). New
  `server_metrics.conntrack_count/conntrack_max/sock_*` columns (migration 0026);
  disk temp rides in the existing `disk_devs` JSON. Full SMART attributes are out
  of reach for the unprivileged agent (need smartctl/root) — temperature is the
  sysfs-exposed part. Agent v1.17.
- **Top processes by CPU / memory.** The agent reads every `/proc/<pid>/stat`
  and reports the top processes by CPU% (delta of utime+stime over the interval,
  top-style per-core percent) and by resident memory (RSS), with pid and comm.
  A new **Processes** section on the server detail shows two ranked cards — top
  by CPU and top by memory — each listing the other metric and the PID, with
  heat-map row highlighting by load (red = heavy, amber, green, white = idle;
  CPU by %, memory by share of total RAM).
- The CPU **frequency** chart is now hidden on VMs (where the guest only sees a
  fixed passthrough host clock) and shown on baremetal, where it's a real signal
  (stuck-low governor, throttling, turbo). It's a
  point-in-time snapshot carried in the report (no time-series column, no
  migration). Agent v1.16.
- **Per-interface network errors & drops.** The agent now also reports RX+TX
  error and drop rates per interface (`/proc/net/dev` errs/drop counters, delta
  per second). A new **network errors/drops per interface** overlay chart (one
  line = errs+drops per NIC — flat at zero means healthy) with a per-interface
  readout splitting errors and drops. Carried in the existing `net_ifaces` JSON
  (no migration). Agent v1.15.
- **Per-interface network + per-device disk (util / latency).** The agent now
  reports throughput per network interface (`/proc/net/dev`, `lo` and docker
  `veth*` excluded) and per whole physical disk it reports `%util` (busy time
  from `/proc/diskstats` field 13) and average `await` latency in ms
  (queue/service time ÷ completed ops) — the iostat metrics. The server detail
  gains overlay charts **network in/out per interface** (auto-hidden when there's
  only aggregate data) and **disk utilization / latency per device**, each with a
  colour-matched per-entity readout. To stay readable with many interfaces/disks,
  charts and readouts show the top 8 entities by peak and fold the rest into a
  "+N more" row (bounded, scrollable); colours are hashed from the entity name so
  a line and its readout row always match. New `server_metrics.net_ifaces/disk_devs`
  JSON columns (migration 0025), averaged by name when binned. Agent v1.14.
- **Swap activity + detailed memory breakdown.** The agent now reports swap
  in/out throughput (`/proc/vmstat` `pswpin`/`pswpout` × page size, bytes/sec)
  and the kernel memory breakdown from `/proc/meminfo` — Slab, Dirty and
  Writeback. The Memory section of the server detail gains a **dirty / writeback**
  overlay chart (with a `slab` current-value readout) and a **swap** in/out mirror
  chart that auto-hides when the host has no swap configured. New
  `server_metrics.swap_in/swap_out/mem_slab/mem_dirty/mem_writeback` columns
  (migration 0024). Agent v1.13.
- **Host identity: VM/baremetal, hypervisor and CPU model.** The agent detects
  whether the server is virtualized (hypervisor flag + DMI) and which hypervisor
  (Hyper-V / VMware / KVM / QEMU / VirtualBox / Xen / EC2 / GCE / …), and reads the
  CPU model name. The server detail now shows a `VM · Hyper-V` / `Baremetal` chip
  and the CPU model. No schema change (carried in the agent's report snapshot).
  Agent v1.12.
- **Extended CPU metrics + thermal alerts.** The agent now reports per-core
  utilization (`/proc/stat` per-core lines), average clock frequency
  (`/proc/cpuinfo`), CPU temperature (hwmon / thermal_zone) and thermal
  throttling counts (`thermal_throttle`) — the last two degrade gracefully to
  "no data" where there are no sensors (e.g. VMs). New server-detail charts in
  the CPU section: per-core (overlay), frequency, temperature and throttling
  (the sensor-less ones auto-hide). New `server_metrics.cpu_cores_pct/cpu_freq/
  cpu_temp/cpu_throttle` columns (migration 0023). Two new server alerts —
  **CPU temperature** (per-server threshold `temp_alert_c`, editable in the edit
  form) and **CPU throttling** (fires when throttling is detected) — wired into
  the existing server alert-rules (editable text/scope). Agent v1.11.
- **Disk I/O metrics (read / write throughput + IOPS).** The agent now reads
  `/proc/diskstats` and reports aggregate read & write bytes/sec and total
  IOPS (ops/sec) over whole physical disks (partitions, loop/dm/md/ram devices
  excluded, no double-counting), computed by delta like network. New
  `server_metrics.disk_read/disk_write/disk_iops` columns (migration 0021), a
  mirror "Disk I/O" chart (↓ read / ↑ write) in the server detail plus a current
  read/write/IOPS readout. Agent v1.9. *(Widening each metric row by a few float
  columns has negligible effect on ingest scale — row count and request rate
  dominate, not row width.)*
- **Horizontal ingest scaling (`compose.scale.yml`).** The default single
  container runs the API, ingest and scheduler in one process; past a few hundred
  servers that one process is the bottleneck (not Postgres). The optional overlay
  runs the API as `KERVAX_WEB_WORKERS` uvicorn workers and moves the background
  scheduler into its **own single process** (`app.scheduler_run`), gated by
  `KERVAX_RUN_SCHEDULER` so it never starts once-per-worker. Bootstrap (admin /
  default-location seed) is now race-safe across workers (catches the
  `IntegrityError`). Same image, same DB; the web tier scales while background
  work stays single-instance.
- **Secure over-the-air agent updates (signed releases) — optional, off by
  default.** A fresh clone builds the agent from source and runs with self-updates
  **disabled** (empty signing key); nothing to configure. To opt in, generate a
  keypair and set `KERVAX_AGENT_PUBKEY` in `.env` — the public key is baked into
  the agent at build time (`-ldflags -X`), so agents only trust *your* offline
  key. The panel is **not** the root of trust — releases are signed offline with
  an Ed25519 key that never touches the server. The agent installs an update
  **only if**: the
  release manifest's signature verifies, the version is strictly newer than the
  running one (anti-rollback), and the downloaded binary's sha256 matches the
  signed manifest. A fully compromised panel therefore cannot push code agents
  will run — proven live: a tampered binary is rejected on hash mismatch. Rollout
  is admin-driven from the Servers page: **canary** one node, then **update all**,
  with per-node cancel; audit-logged. The agent replaces itself atomically and
  re-execs **without root** (binary lives in `/opt/kervax/bin`, owned by the
  `kervax` user; systemd `ReadWritePaths` scopes write access to just that dir).
  Release tooling under `agent-signing/` (`keygen.py`, `release.py`); the private
  key is git-ignored. New `servers.target_agent_version` (migration 0019),
  `GET /api/agent/manifest[.sig]`, `GET /api/servers/agent-release`,
  `POST /api/servers/agent-update[/cancel]`. Agent v1.7.

### Changed
- **Server detail regrouped into collapsible sections + a chart grid** (scales to
  many metrics). Charts are grouped by category (CPU / Memory / Network / Disk),
  each a collapsible section (state persisted per section), and laid out in a
  responsive `auto-fit` grid — one chart fills the row, two-plus flow into two
  columns — instead of one full-width chart per row. The server-detail modal is
  wider (1080px) to fit the grid; per-chart fullscreen zoom is preserved. Adding
  new metrics is now just dropping a chart into the right section.
- **PostgreSQL tuned for time-series scale.** The metric/sample tables grow into
  the tens of millions of rows at ~100 monitored servers; the DB ran on stock
  defaults. `compose.yml` now passes tuned settings (shared_buffers 256MB,
  work_mem 16MB, maintenance_work_mem 256MB, `random_page_cost=1.1` for local
  disk, `wal_compression=on`, `max_wal_size=2GB`, aggressive autovacuum at 5%
  dead tuples) plus `shm_size=256mb`; `shared_buffers`/`effective_cache_size` are
  env-overridable (`KERVAX_PG_*`) to scale up on bigger boxes. Retention pruning
  now runs hourly instead of every scheduler tick (`prune_interval_seconds`).
  Dropped three redundant single-column indexes already covered by the composite
  `(server_id, ts)` / `(check_id, ts)` / `(check_id, location_id, ts)` indexes
  (migration 0020) — less write amplification and disk at scale.

### Fixed
- **Byte/rate/uptime/"time ago" units now localize.** `fmtBytes`, `fmtRate`,
  `fmtUptime` and `fmtRel` were hard-coded Russian (ГБ, КБ/с, «мин назад») and
  ignored the language toggle; they now switch to English (GB, KB/s, "min ago")
  via a module-level `currentLang()`. Affects the server detail's tiles/charts.
- **Expiry dates aren't clobbered by transient probe failures.** A single failed
  TLS/RDAP probe (e.g. a `ConnectTimeout` while the site is down via DNS) used to
  overwrite a previously-known good date with a scary "timeout" message, making a
  perfectly valid domain look expired. Now a `None` result only fills the message
  when there is no prior date; a real number always wins. TLS/RDAP error messages
  are also friendlier (`сайт недоступен (таймаут)`, `сертификат невалиден: …`)
  instead of raw exception text.
- **Adding a monitor no longer hangs.** The first check ran synchronously inside
  the create request, so adding a slow/unreachable site (up to ~4 retries ×
  timeout ≈ 40 s) froze the "Add" button. It now runs in the background — create
  returns instantly and the status fills in shortly after.
- **Scheme is auto-detected (https first).** You can enter a bare domain
  (`example.com`); the checker tries `https://` and falls back to `http://` if
  the HTTPS connection can't be established. No need to type `http(s)://`.
- **Server alert-rules editor redesigned to scale.** The five rule types are now
  a compact accordion (one collapsed line each with a scope summary); expanding
  one reveals the text and scope editor. Scoping to specific groups/servers uses
  a **search-as-you-type picker** (selected shown as removable chips) instead of
  rendering every server as a checkbox — so it works with hundreds/thousands of
  servers.
- **No accidental modal closes.** Clicking the dimmed area outside a detail /
  chart modal no longer closes it — only the ✕ / Close buttons do.
- **Chart axis labels no longer stretch.** They're now HTML overlays instead of
  SVG text, so they stay crisp in the stretched (fullscreen) charts instead of
  looking "smeared".
- **Fullscreen chart opened off-screen.** Opening a chart fullscreen while the
  server detail was scrolled down made the modal appear above the fold (the
  detail backdrop's `backdrop-filter` turned the nested modal's `position:
  fixed` into a local one). The fullscreen modal now renders as a top-level
  overlay, so it's always centred in the viewport.
- **Server sort by CPU/RAM/Disk now uses raw values** (so near-equal servers
  still reorder) and the active metric shows a ↓ marker.
- **Delete server moved into the edit form** (opened via ✎) instead of a
  standalone button in the detail.
- **Home cards redesigned** — fixed-height header with a divider (so the two
  "Открыть →" links line up), mini-stats as chips, clearer spacing. Fixes the
  "everything blends / buttons at different levels" look.

### Added
- **Separate alert threshold for "degraded" (slow).** Slow responses are noisier
  than a full outage, so degradation now has its own consecutive-count threshold
  (`degraded_after_failures`, default **10**) distinct from the down threshold
  (`alert_after_failures`, default 3) — a monitor must be slow N times in a row
  before it alerts, while a genuine outage still alerts sooner. Editable per
  monitor in the form and **in bulk** via "Применить ко всем мониторам". New
  `checks.degraded_after_failures` column (migration 0018).
- **Editable site (monitor) alert rules.** Same as the server rules, now for
  site alerts too — down/degraded, recovery, SSL-expiry, domain-expiry and
  partial-location alerts each get an on/off toggle, an editable text template
  ({name} / {group} / {message} / {status}) and a scope (all / groups / chosen
  monitors). Reuses the compact accordion + search-picker UI. New
  `GET/PUT /api/alerts/site-rules`; the send path checks the rule before firing
  and only overrides the default text when customised. 1 new test (64 total).
- **Editable server alert rules.** The Alerts dialog gained a "Server alerts"
  section where each alert type (offline / CPU / RAM / disk / reboot) can be
  turned on/off, its message text edited (with `{server}` / `{value}` /
  `{threshold}` / `{severity}` placeholders), and scoped to **all servers, chosen
  groups, or chosen servers**. Stored in `settings_store`
  (`server_alert_rules`, no migration); the scheduler honours enabled/scope and
  formats the custom text. New `GET/PUT /api/alerts/server-rules`. 1 new test
  (63 total).
- **Fullscreen, zoomable server charts.** Click a metric card (or its ⤢) to open
  it fullscreen — with the server name and address in the header, period presets
  (6h … 90d) and **Grafana-style drag-to-zoom**: select a range on the chart with
  the mouse and it re-fetches finer-grained data for that exact window (metrics
  endpoint now takes `from_ts`/`to_ts`). Each metric in the detail is now its own
  bordered card so the sections no longer blend together.
- **Escalating disk alerts (warn / problem / critical).** Each server now has
  three disk thresholds — 85 % warning ⚠️, 90 % problem 🔴, 95 % critical 🚨
  (editable, 0 disables a level). Alerts escalate as usage climbs and recover
  when it drops. Migration 0017.
- **Reboot alert.** When a server's uptime drops (it was rebooted), the panel
  sends a one-shot "🔄 server rebooted" alert. Detected at ingest, deduped by
  reboot timestamp.
- **Server grouping, sorting and search on the home summary.** The servers page
  gained group-by-group (collapsible, with per-group summary) and sort-by
  CPU / RAM / Disk / Name, matching the sites page. The home cards now list
  *what* is wrong — which monitors are down and which servers are offline or
  over their CPU/RAM/disk thresholds.

### Changed
- **Site (monitor) charts brought up to the server level.** The response-time
  chart is now a filled area in a bordered card that opens fullscreen on click,
  with period presets and Grafana-style drag-to-zoom (the history endpoint takes
  `from_ts`/`to_ts`). The availability strip above it is taller, rounded and has
  a hover tooltip.
- **Server delete moved into the detail.** The ✕ is gone from each row; delete
  now lives at the bottom of the server detail modal (a rare, deliberate action).
- **Alerts: "mute" toggle instead of quiet hours.** Replaced the UTC quiet-hours
  window with a single **"Mute alerts (pause)"** checkbox that temporarily
  silences all notifications (incidents are still recorded). Simpler and covers
  the common "shut them up for now" case. `quiet_hours` → `muted` in the alert
  config and `settings_store`.
- **Prettier server meta line.** The run-on "OS · host · IP · uptime · agent ·
  seen" line in the server detail is now a row of small labelled chips.

### Added
- **Clean "server unavailable" state.** A server that has never reported now
  shows a compact "Server unavailable — agent hasn't reported yet" placeholder
  in its detail modal (with edit/delete still available) instead of a wall of
  empty charts and dashes. Servers that were online keep showing their last
  snapshot and history when they go offline. (Offline/recovery alerts were
  verified end-to-end: stopping an agent fires "🖥 server X unavailable", and
  it recovers on return — servers that never connected are intentionally not
  alerted.)
- **Backup / restore (config, without metrics).** New ⚙ → **"Backup"** dialog:
  download a JSON backup of the whole config — monitors, servers (agents),
  locations, incidents and settings, **excluding the heavy time-series
  metrics** — and restore it from a file (replaces current config). The panel
  also keeps **auto-backups on disk** (`data/backups/`) on a configurable
  schedule (every N hours, keep M files); it prunes old ones and lets you
  download any of them. New `backup.py` module + `/api/backup/*` endpoints
  (`config`, `export`, `list`, `file/{name}`, `run`, `restore`); the scheduler
  writes an auto-backup when due. Postgres id-sequences are re-synced after a
  restore. 3 new tests (61 total).
- **Home + servers page polish.** The home "Servers" card now shows a real
  summary (online / offline) instead of a "coming soon" placeholder. The
  servers page gained clickable Total / Online / Offline filter tiles and a
  search box (by name, group, IP, hostname, OS).
- **Absolute values under the CPU and Memory charts.** Like the disk list, the
  CPU chart now has a small stats block (cores count + load 1/5/15) and the
  Memory chart shows the real amounts — used / total, cache/buffer and free in
  GB (colours matching the chart series), plus swap when present. The agent
  (v1.5) reports `cpu_cores`; memory bytes come from the existing snapshot. No
  migration.
- **Time-range selector on server charts + configurable retention.** The server
  detail modal now has a 6h / 24h / 7d / 30d / 90d window switcher above the
  charts (axis labels switch to dates for windows longer than a day). A new
  ⚙ → **"Data retention"** dialog lets you set how long time-series are kept —
  separately for **server metrics** and **site-check history** — with quick
  presets (30d / 90d / half-year / year / 2 years). Values are stored in the DB
  (`settings_store`, no migration) and the pruning job reads them each tick.
  New `GET/PUT /api/settings/retention`. 2 new tests (58 total).
- **Server rows show IP addresses instead of OS + scan time.** Each server row's
  second line now shows the node's **external · local** IP (`внеш … · лок …`,
  collapsed to one when they're equal) instead of the OS name and the
  "N sec ago" scan time (redundant with the online dot). The agent (v1.4)
  reports its outbound `local_ip`; the panel records the `external_ip` from the
  report's source address (`X-Forwarded-For`/`X-Real-IP`). The OS name moved
  into the detail header line. Migration 0016 adds `local_ip` / `external_ip`
  to `servers`.
- **Edit servers from the panel.** The server detail modal got a ✎ button that
  opens an edit form (name, group, the CPU/RAM/Disk alert thresholds and the
  "mark offline after N sec" timeout) with Save/Cancel — mirroring the monitor
  edit flow. The always-on "Пороги алертов" block at the bottom is gone; the
  thresholds now live in that form. Name and group are editable after enrolment
  (were fixed at enrol time before). Backend already supported it (`PATCH
  /servers/{id}`, all fields optional) — frontend-only change.
- **Agent skips tiny system partitions (v1.3).** The agent no longer reports
  `vfat`/`exfat` mounts (EFI `/boot/efi`, plugged-in USB sticks) or any
  partition smaller than 2 GiB (e.g. `/boot`) — they never fill and only
  cluttered the disk chart, the disk list and the disk-usage alert. Only real
  data volumes (`/`, `/home`, `/var`, `/data`, …) are monitored.
- **Grafana-grade server charts, in Kervax's own style.** The agent (v1.2) now
  reports the CPU breakdown (user / system / iowait / irq, from `/proc/stat`
  deltas) and memory split (cache/buffers vs free). The detail view renders
  four custom `StackedAreaChart`s — hand-rolled SVG, no chart libs: **CPU by
  state** stacked to 100 % (система/юзер/iowait/irq, purple→blue→amber→pink
  palette), **Memory** stacked (занято + кэш/буфер with gradient fills),
  **Network** as a mirror chart (приём up / отдача down from the centre line),
  and **Disk** as an *overlay* chart (one filled area per mount, independent —
  not stacked — since mount percentages don't sum). Each has a hover tooltip
  with per-series values and a live legend. Migration 0014 adds `cpu_user` /
  `cpu_system` / `cpu_iowait` / `cpu_irq` / `mem_cache` / `mem_free`; migration
  0015 adds a `disks` JSON column (`[{mount, pct}]`) — all carried through the
  time-series binning (disk snapshot = last sample per bucket).
- **Stage 5 — server monitoring via a push agent.** A small static Go agent
  (no cgo → zero dependencies, one file, amd64/arm64) runs on each node, reads
  `/proc` (CPU/RAM/swap/load/uptime) and disk usage, and pushes metrics *out*
  to the panel over HTTPS. The panel holds **no access to the servers** — only a
  hashed per-agent token — so a panel compromise can't reach the nodes, and
  hosts behind NAT (outbound-only) work fine. Enroll a server to get a
  one-line install command (`curl … | sh`) that drops the binary in
  `/usr/local/bin` and runs it via systemd as an unprivileged user. The panel serves the binary
  and script, ingests reports (`POST /api/agent/report`), stores a light
  time-series, and detects offline + CPU/RAM/disk threshold breaches → alerts
  (with the same channels, quiet hours and deep-links). New "Servers" section:
  per-server CPU/RAM/disk meters, online/offline, detail with charts, disks and
  editable thresholds. Models `Server` / `ServerMetric` (migration 0012). HTTP
  checks also send a browser User-Agent. 2 new backend tests (56 total).
- **Server network metrics + nicer charts.** The agent (v1.1) now also reports
  network throughput (rx/tx bytes/s, computed from `/proc/net/dev` deltas). The
  detail view gained a network tile and chart, and the CPU/RAM/**disk** usage is
  shown on one multi-series chart (fixed 0–100 % axis, legend) plus a network
  chart with byte-rate units — via a new `MultiLineChart`. Migration 0013 adds
  `disk_percent` / `net_rx` / `net_tx` to `server_metrics`; the y-axis label
  collapse on narrow ranges is fixed.
- **Filter monitors by status.** The overview tiles (Total / Up / Degraded /
  Down) are now clickable filters — click "Down" to show only the failing
  monitors, click again (or "Total") to clear. Works together with grouping.
- **Check log in the monitor detail.** A "Check log" section lists raw check
  results newest-first — timestamp, latency and the exact message
  (e.g. `HTTP 429`, `ReadTimeout`) — so you can see what error occurred at each
  moment of downtime. Toggles between "failures only" (default) and "all".
  New `GET /checks/{id}/log?failed=&limit=` endpoint.
- **Deep-links in alerts.** When `KERVAX_PANEL_URL` is set, every monitor alert
  includes a `🔗 …/?check=<id>` link that opens the panel straight on that
  monitor's detail view (the frontend reads the `check` query param on load).
- **Bulk-add sites.** A "Bulk add" modal takes a pasted list (one site per line,
  optional `Name | address`), a shared type and group, and creates a monitor for
  each in one request (`POST /checks/import`, up to 500). The name is derived
  from the address when omitted; the live button shows how many will be added.
- **Monitor grouping.** Each monitor has a free-text **Group** field (with
  autocomplete from existing groups); the Sites page groups the list by group
  (or by type, or off) with collapsible section headers showing a per-group
  health summary. The group selection is remembered. Uses the existing
  `group_name` column — no backend change.
- **Partial-availability alerts across locations.** When a monitor is reachable
  from some locations but down from others, the panel sends a per-location
  breakdown — one line per location with a 🟢/🔴 dot (e.g. "🟢 Germany —
  available / 🔴 Russia — down") — the classic regional-block signal. It fires
  on the direct+proxy status mix, deduplicates on the set of down locations, and
  sends a recovery when reachable everywhere again. A full outage (down from all)
  stays with the main "down" incident alert. New `checks.loc_alerted`
  (migration 0011).
- **Escalating SSL & domain expiry reminders.** Warn thresholds are now lists,
  not a single value — default SSL `[14, 7, 1]` and domain `[7, 1]` days — and
  each threshold fires its own reminder as expiry approaches (tracked via
  `ssl_alerted_days` / `domain_alerted_days`, reset on renewal). Migration 0010
  turns `cert_warn_days`/`domain_warn_days` into JSON lists.
- **Bulk-apply settings to all monitors.** A "Apply to all" button opens a modal
  where you tick which settings (interval, slow threshold, retries, alert
  threshold, SSL/domain reminders) to push to every monitor at once
  (`PATCH /checks/bulk`).
- **Choose which locations a monitor is checked from.** The edit form lists the
  locations as checkboxes under "check from locations" — untick the direct one
  and keep only the proxies you want, or any subset. Stored as
  `checks.location_ids` (JSON: `null` = all enabled, `[]` = none, `[id,…]` =
  subset; migration 0009). Probing, the locations list and per-location charts
  all honour the selection.
- **Multi-location checks via proxies.** A new **Locations** list (managed from
  the gear menu) holds HTTP/HTTPS/SOCKS5 proxies. Turn on "check from locations"
  on an HTTP monitor and the panel additionally probes the site through each
  enabled proxy on a slower cadence (`location_probe_interval`, default 5 min),
  storing the latest per-location result. The monitor detail view gains a
  **Locations** section comparing "Direct (panel)" vs each proxy — so you can
  see a site is reachable directly but blocked/failing from a given network or
  region. New `locations` / `location_results` tables + `checks.check_locations`
  (migration 0007); `Location` CRUD API and a `/checks/{id}/locations` results
  endpoint; `httpx[socks]` for SOCKS proxies. A **default "direct" location**
  (empty proxy URL = no proxy) is auto-seeded once on first start, named after
  the panel's own geolocation (via `ip-api.com`) — so the panel's own vantage
  point is a first-class location out of the box. 4 new backend tests
  (48 total).
- **Per-location response-time charts.** The monitor detail's Locations list
  moved above the chart and became a selector: click a location and the
  response-time graph + status strip rebuild from *that* vantage point. The
  direct location shows the main high-resolution series; each proxy keeps its
  own time-series (`location_samples`, migration 0008) sampled on the location
  cadence. History endpoint gained a `location_id` query param.
- **Per-monitor alert threshold, editable in the panel.** The
  "alert after N consecutive failures" threshold moved from a global env knob
  to a per-monitor field (`checks.alert_after_failures`, migration 0006),
  editable in the add/edit form.
- **Edit a monitor from its detail view.** The detail modal now has an edit
  (✎) button that swaps in the full edit form inline — change settings and
  save without leaving the modal. The form card was extracted to a shared
  `CheckFormCard` and a `checkToForm` helper.
- **Per-monitor retries on failure.** A failed check is now re-attempted up to
  `retries` more times (default 2, `1.5 s` apart) before its result counts —
  the status only goes bad, and an incident/alert is only raised, if *all*
  attempts fail; any success within the budget reports up. Configurable per
  monitor in the add/edit form and via `KERVAX_DEFAULT_CHECK_RETRIES` /
  `KERVAX_CHECK_RETRY_DELAY_MS`. Migration 0005 adds `checks.retries`. 4 new
  backend tests (44 total).

### Fixed
- **Monitor detail auto-refreshes.** The detail modal used to fetch uptime,
  incidents, per-location statuses and the chart once on open and then freeze,
  so it could show a stale "down from location X" long after recovery. It now
  refreshes every 12 s while open (paused during inline editing).
- **Alert "Test" now saves the form first.** Previously Test checked the
  server-stored config, so filling in channels and clicking Test (before Save)
  reported "Channels not configured". Test now persists the on-screen values,
  then sends — and shows a clear hint if no channel is filled in.

### Added
- **Configurable Telegram Bot API base URL.** Alert settings now expose an
  "API URL" field (default `https://api.telegram.org`) so a proxy/mirror can be
  used where Telegram is regionally blocked; empty falls back to the default,
  trailing slashes are trimmed. Stored alongside the other alert channels.
- **Home dashboard.** Clicking the logo/wordmark returns to a new home section
  with an at-a-glance summary of Sites (up/degraded/down, open incidents,
  soon-expiring SSL/domain warnings) and a Servers placeholder.
- **SSL & domain expiry monitoring folded into HTTP monitors.** Adding a site
  now offers two checkboxes — *watch TLS certificate (validity & expiry)* and
  *watch domain registration expiry* — both **on by default**, each with a
  "warn N days before" threshold (14 / 30). The scheduler refreshes these slow
  signals infrequently (every `expiry_refresh_hours`, default 6): a TLS probe
  for cert days-left and a domain-registration lookup: RDAP (rdap.org bootstrap)
  for gTLDs, with a WHOIS fallback (`whois.tcinet.ru`, `paid-till` field, IDN →
  punycode) for the Russian zones `.ru` / `.рф` / `.su` that have no RDAP.
  Deduplicated Telegram/webhook alerts fire when either
  drops below its threshold. Row badges and detail-modal pills show the days
  left. The standalone `cert` monitor type is retired from the UI (backend
  still supports existing ones). Migration 0004 adds the new columns. 3 new
  backend tests (35 total).
- **Stage 4 — SVG dashboards (monitor detail view).** Click a monitor to open a
  detail modal with uptime tiles (24h / 7d / 30d), a per-bin status strip, a
  hand-rolled SVG response-time line chart (certificate monitors chart days
  left instead of latency), a 24h/7d/30d window switch, and that monitor's
  incident timeline. New endpoints: `GET /checks/{id}/uptime` (24h/7d/30d %)
  and a `check_id` filter on `/checks/incidents`; `/checks/{id}/history` now
  bins samples server-side (≤300 buckets: average latency, worst status per
  bucket) so 7d/30d payloads stay small. No new tables/migration. Frontend:
  `charts/LineChart`, `charts/StatusBar`, `CheckDetail`. 1 new backend test
  (32 total).
- **Stage 3 — incidents, uptime & alerts.** `CheckIncident` model + a
  `consecutive_fails` counter (migration 0003); the scheduler now reconciles
  incidents (open on down/degraded, close on recovery) and fires threshold
  alerts to Telegram / webhook — down after N consecutive failures, recovery,
  cert-expiring — with quiet hours. 24h uptime % per monitor; alerts config API
  (get/put/test) and an incidents API. Frontend: `AlertsModal` (channels +
  quiet hours + test), uptime badges and an open-incidents indicator. 5 new
  backend tests (31 total).
- **Stage 2 — website & service monitors.** `Check` / `CheckSample` models
  (migration 0002); panel-side executors — `http` (status code, keyword
  present/absent, response-time, degraded threshold), `tcp_port` (reachability +
  latency), `cert` (TLS-certificate expiry in days); background scheduler that
  runs due monitors on their interval and stores time-series; REST API
  (CRUD + run-now + history + overview). Frontend `ChecksPage`: overview tiles
  (up/degraded/down), add/edit/delete monitors, run-now, status list. 8 new
  backend tests (26 total).

### Changed
- **Location-checking on by default; browser User-Agent on HTTP checks.** New
  monitors now have "check from locations" enabled by default. HTTP checks send
  a browser-like `User-Agent` so sites that reject bare requests (403/429) no
  longer read as false "down".
- **New reliability defaults + proxy-aware slow threshold.** New monitors default
  to 3 retries and alert-after-3-failures (were 2/2). Proxy-location probes add a
  configurable overhead margin (`location_degraded_extra_ms`, default 2000 ms) to
  the "slow" threshold so the proxy's own latency doesn't flag them degraded.
- **Bumped PostgreSQL 17 → 18 (`18-alpine`).** PG18's image stores data in a
  major-versioned subdirectory, so the compose volume now mounts the cluster
  root (`./data/postgres:/var/lib/postgresql`) instead of the data dir. Major
  upgrades need a dump/restore (documented; not automated).

### Added
- **Stage 1 — authentication.** JWT (HS256, pinned algorithm) with a
  `token_version` claim so a password change invalidates every old session;
  TOTP 2FA on the stdlib with replay protection (last-counter check);
  in-memory login rate-limit + lockout; timing-equalised login (dummy hash for
  unknown users); `ensure_admin` seed-only + break-glass reset; audit log of
  login / password / 2FA events; security alerts (Telegram / webhook) on
  brute-force lockout and password change. Frontend: login page (with 2FA
  step), change-password and 2FA modals, header account menu. First Alembic
  migration (`users`, `audit_log`, `app_settings`). 18 backend tests.
- **Stage 0 — repository skeleton.** FastAPI backend with `/api/health`,
  React 19 + Vite frontend shell (theme + i18n), SQLAlchemy/Alembic wiring,
  Docker Compose (`compose.yml` + `compose.caddy.yml`), nginx SPA with strict
  CSP, GitHub Actions CI and Dependabot. Env prefix `KERVAX_`.
