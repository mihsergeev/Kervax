import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  dockerCommand,
  dockerCommandStatus,
  listServers,
  updateServer,
  type DockerCommand,
  type DockerContainer,
  type DockerInfo,
  type Server,
} from './api'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { useI18n } from './i18n'
import { DOCKRISK_MUTE, NOBOOT_MUTE, RESTARTS_DAY_MIN } from './mutes'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { dockRiskText, proxyBuilt, shortList, srvIssues, srvMuted } from './serverUtils'
import { fmtRel } from './ServersPage'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { NetTile, TileFilterChip, useTileFilter } from './NetLinks'
import { fmtBytes } from './units'

// Вкладка «Docker»: КОМПАКТНЫЙ список хостов с докером; клик по хосту → модалка
// с его контейнерами (версии, статусы, кнопки, логи). Так масштабируется на 100+
// серверов. Действия/логи — on-demand через bounded proxy (агент забирает команду
// быстрым опросом, исполняет, постит результат). Логи не хранятся — разовый tail.

// Остановленный контейнер красный, только если это авария: по нему ушел алерт, докер не смог
// его запустить (err) или он dead. Остановленный руками - серый: docker stop не снимает
// restart-policy, и раньше такой выглядел упавшим.
function dockerTone(c: DockerContainer, problem?: string): string {
  if (c.state === 'running') return 't-up'
  if (problem || c.err || c.state === 'dead') return 't-down'
  if (c.state === 'exited' || c.state === 'created') return 't-muted'
  return 't-degraded'
}
const RESTART_POLICIES = new Set(['always', 'unless-stopped', 'on-failure'])
// точка строки контейнера по тону: остановленный штатно - серая
const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-down': ' lk-dot-bad', 't-degraded': ' lk-dot-warn' }
// подпись плитки из частей: пустые выкидываем
const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')

const DOCKER_ENABLE_CMD = `DGID=$(getent group docker | cut -d: -f3)
sudo docker rm -f kervax-docker-proxy 2>/dev/null
sudo docker run -d --name kervax-docker-proxy --restart unless-stopped \\
  --network host --read-only --cap-drop ALL --security-opt no-new-privileges \\
  --user 65534:$DGID -v /var/run/docker.sock:/var/run/docker.sock:ro \\
  wollomatic/socket-proxy:1 \\
  -loglevel warn -listenip 127.0.0.1 -allowfrom 127.0.0.1/32 -shutdowngracetime 1 \\
  -allowGET '^/(v[0-9.]+/)?(version|info|_ping|containers/json|images/json|containers/[a-zA-Z0-9_.-]+/(json|logs))' \\
  -allowPOST '^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$'
grep -q '^docker_host=' /etc/kervax-agent.conf || echo docker_host=tcp://127.0.0.1:2375 | sudo tee -a /etc/kervax-agent.conf
sudo systemctl restart kervax-agent`

async function runAndWait(
  serverId: number,
  container: string,
  action: 'restart' | 'stop' | 'start' | 'logs',
  opts: { tail?: number; since?: number } = {},
): Promise<DockerCommand> {
  const c = await dockerCommand(serverId, container, action, opts)
  let last = c
  // диапазон по времени может тянуть больше — ждём подольше
  const maxTries = opts.since ? 200 : 100
  for (let i = 0; i < maxTries && last.status !== 'done' && last.status !== 'error'; i++) {
    await new Promise((r) => setTimeout(r, 300))
    last = await dockerCommandStatus(serverId, c.id)
  }
  return last
}

// Внешний прокси с docker-сокетом: что добавить в его compose, чтобы он читал докер через
// прокси сокета только на чтение. traefik и nginx-proxy смотрят еще и свойства контейнеров
// (inspect), caddy-docker-proxy - нет. Версия прокси закреплена (образ подписан), шаблоны
// путей прокси сам обрамляет ^ и $, сторож перезапускает его, если сокет отвалился.
const SOCK_PROXY_SVC = (client: string, inspect: boolean) => `  docker-api:
    image: wollomatic/socket-proxy:1.13.1
    restart: unless-stopped
    user: "65534:GID"   # GID: stat -c %g /var/run/docker.sock
    read_only: true
    mem_limit: 64M
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [docker-api]
    command:
      - -listenip=0.0.0.0
      - -allowfrom=${client}   # the service name of ${client}
      - -watchdoginterval=600
      - -stoponwatchdog
      - -allowHEAD=/_ping
      - -allowGET=/(v[0-9]+\\.[0-9]+/)?(_ping|version|info|events|containers/json${inspect ? '|containers/[a-zA-Z0-9][a-zA-Z0-9_.-]*/json' : ''}|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)
networks:
  docker-api:
    internal: true`

const SOCK_FIX: Record<string, string> = {
  caddy: `services:
  caddy:
    environment:
      - DOCKER_HOST=tcp://docker-api:2375
      # required: without it caddy inspects its own container to find
      # the networks, and the proxy does not allow inspect
      - CADDY_INGRESS_NETWORKS=caddy   # your ingress network
    networks: [caddy, docker-api]   # plus the networks it has now
    # and drop /var/run/docker.sock from its volumes; not for swarm mode
${SOCK_PROXY_SVC('caddy', false)}`,
  traefik: `services:
  traefik:
    command:
      - --providers.docker.endpoint=tcp://docker-api:2375
      # keep the other flags; drop /var/run/docker.sock from its volumes
    networks: [docker-api]   # plus the networks it has now
${SOCK_PROXY_SVC('traefik', true)}`,
  'nginx-proxy': `services:
  nginx-proxy:
    environment:
      - DOCKER_HOST=tcp://docker-api:2375
    networks: [docker-api]   # plus the networks it has now
    # and drop /var/run/docker.sock from its volumes
${SOCK_PROXY_SVC('nginx-proxy', true)}`,
}

// Почему helper не подготовил файл сам: код из sockfix-setup -> слова
const SOCK_WHY: Record<string, string> = {
  layout: 'файл размечен необычно (табы или сбитые отступы)',
  v1: 'файл старого формата compose v1, без services:',
  anchors: 'в файле якоря YAML (&, *, <<), такую разметку надежно не поправить',
  inline: 'нужный ключ записан в одну строку ([...] или {...})',
  nosvc: 'сервиса нет в файле, из которого его запустили',
  dupsvc: 'сервис docker-api в файле уже есть',
  extends: 'у сервиса extends',
  networkmode: 'у сервиса network_mode',
  elsewhere: 'сокет монтируется не из этого файла',
  rundir: 'смонтирован весь /run или /var/run, а не один сокет',
  dockerhost: 'DOCKER_HOST уже задан',
  kind: 'сам правлю только caddy-docker-proxy, у traefik и nginx-proxy настройка другая',
  nocompose: 'контейнер запущен не через docker compose',
  multi: 'контейнер запущен из нескольких compose-файлов',
  composev1: 'нужен docker compose v2',
  nosock: 'на хосте нет docker-сокета',
  validate: 'docker compose не принял исправленный файл',
  unexpected: 'не вышло',
}

function CmdBlock({ text, id, copied, onCopy }: { text: string; id: string; copied: string; onCopy: (id: string, text: string) => void }) {
  const { t } = useI18n()
  return (
    <div className="agent-advice-cmd">
      <pre>{text}</pre>
      <button className="ghost" onClick={() => onCopy(id, text)}>
        {copied === id ? t('Скопировано') : t('Копировать')}
      </button>
    </div>
  )
}

// Разница исправленного файла с нынешним: добавленное зеленым, убранное красным
function SockDiff({ diff }: { diff: string }) {
  return (
    <pre className="sock-diff">
      {diff.split('\n').map((l, i) => {
        const c = l.startsWith('+++') || l.startsWith('---') ? 'sd-file' : l.startsWith('@@') ? 'sd-hunk'
          : l.startsWith('+') ? 'sd-add' : l.startsWith('-') ? 'sd-del' : ''
        return <div key={i} className={c}>{l || ' '}</div>
      })}
    </pre>
  )
}

type Exposed = NonNullable<Server['docker_exposed']>[number]

function SockWarning({ items }: { items: Exposed[] }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState('')
  const copy = (id: string, text: string) => {
    navigator.clipboard?.writeText(text)
    setCopied(id)
    window.setTimeout(() => setCopied(''), 1500)
  }
  const ready = items.filter((i) => i.fix?.ok)
  const manual = items.filter((i) => !i.fix?.ok)
  // снипет руками: с настоящим gid сокета и именем сервиса, если helper их прислал
  const snippet = (i: Exposed) => {
    let s = SOCK_FIX[i.kind] ?? ''
    if (i.fix?.gid != null) s = s.replace('"65534:GID"   # GID: stat -c %g /var/run/docker.sock', `"65534:${i.fix.gid}"`)
    const svc = i.fix?.service
    if (svc && i.kind === 'caddy') s = s.replace('\n  caddy:\n', `\n  ${svc}:\n`).replace('-allowfrom=caddy   # the service name of caddy', `-allowfrom=${svc}`)
    if (i.fix?.nets?.length && i.kind === 'caddy') {
      const nets = i.fix.nets.filter((n) => n !== 'docker-api')
      s = s.replace('CADDY_INGRESS_NETWORKS=caddy   # your ingress network', `CADDY_INGRESS_NETWORKS=${nets.join(',')}`)
        .replace('networks: [caddy, docker-api]   # plus the networks it has now', `networks: [${[...nets, 'docker-api'].join(', ')}]`)
    }
    return s
  }
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Прокси держит docker-сокет')}</div>
      </div>
      <div className="net-prob">
        {t('{list}: docker-сокет смонтирован целиком. Прокси смотрит в интернет, а сокет - это root на хосте: одна дыра в прокси, и чужой получает весь сервер. Лечится так: прокси читает докер через прокси сокета только на чтение (список контейнеров, сети, события), а сам сокет из его volumes уходит.',
          { list: items.map((i) => `${i.name} (${i.kind})`).join(', ') })}
      </div>
      {ready.map((i) => {
        const fx = i.fix!
        return (
          <div key={i.name} className="sock-fix">
            <div className="net-sub">
              {t('Исправленный compose для {name} уже готов: helper собрал его на сервере из {file} и проверил через docker compose config. Что меняется:',
                { name: i.name, file: fx.file ?? '' })}
            </div>
            <SockDiff diff={fx.diff ?? ''} />
            <div className="net-sub">{t('1. Применить - от root на сервере, прокси пересоздастся за пару секунд:')}</div>
            <CmdBlock text={fx.apply ?? ''} id={`${i.name}:apply`} copied={copied} onCopy={copy} />
            <div className="net-sub">{t('2. Проверить: оба контейнера работают, blocked в логе docker-api - 0, сайты открываются. Этот пункт уйдет сам со следующим отчетом агента.')}</div>
            <CmdBlock text={fx.check ?? ''} id={`${i.name}:check`} copied={copied} onCopy={copy} />
            <div className="net-sub">{t('Если что-то не так - вернуть как было:')}</div>
            <CmdBlock text={fx.rollback ?? ''} id={`${i.name}:rollback`} copied={copied} onCopy={copy} />
          </div>
        )
      })}
      {manual.map((i) => (
        <div key={i.name} className="sock-fix">
          <div className="net-sub">
            {i.fix
              ? t('Для {name} готового файла нет: {why}{detail}. Поправьте compose руками:', {
                  name: i.name, why: t(SOCK_WHY[i.fix.why ?? ''] ?? 'не вышло'),
                  detail: i.fix.why_detail ? ` (${i.fix.why_detail})` : '',
                })
              : t('Готовый исправленный compose под эту ноду соберет helper sockfix-setup: поставьте его плейбуком хелперов, и здесь появятся разница с нынешним файлом и команды. Пока - руками:')}
          </div>
          <ol className="sock-steps">
            <li>{i.fix?.file
              ? t('в {file} у сервиса {svc} уберите из volumes строку с docker.sock;', { file: i.fix.file, svc: i.fix.service ?? i.kind })
              : t('в compose-файле прокси уберите из volumes строку с docker.sock;')}</li>
            <li>{t('добавьте ему environment и networks из примера ниже, остальное в сервисе не трогайте;')}</li>
            <li>{t('добавьте сервис docker-api и сеть docker-api из примера;')}</li>
            <li>{t('docker compose up -d в каталоге проекта.')}</li>
          </ol>
          {SOCK_FIX[i.kind] && <CmdBlock text={snippet(i)} id={`${i.name}:snippet`} copied={copied} onCopy={copy} />}
        </div>
      ))}
      {manual.length > 0 && (
        <div className="net-foot">
          {t('Проверка после замены: в логе docker-api нет строк blocked request, сайты отвечают как раньше. Ноды с этой проблемой плагин инвентаря ansible собирает в группу kervax_docker_sock.')}
        </div>
      )}
    </div>
  )
}

// Внешний прокси на образе старше года: на что обновлять. Подсказки - из обновлений 06.10.2026
// (traefik 2.5.5 -> 2.11.57, caddy 2.4.5 -> caddy-docker-proxy 2.13.1): что меняется по умолчанию
// и чего не поймает сравнение ответов сайтов.
type ProxyOld = NonNullable<Server['proxy_outdated']>[number]

const PROXY_HINT: Record<string, string> = {
  cdp: 'caddy-docker-proxy: закрепленная версия, как в роли caddy-proxy - lucaslorentz/caddy-docker-proxy:2.13.1. С caddy 2.5 бэкенд получает заголовки Via и X-Forwarded-Host, а X-Forwarded-For от клиента отбрасывается: остается только реальный IP.',
  caddy: 'caddy: последний caddy:2.',
  traefik: 'traefik: последний патч своей ветки, для v2 это traefik:v2.11 (не latest: это уже v3, метки пришлось бы переписать). В 2.11 на чтение запроса вместе с телом по умолчанию 60 с: если на сайтах есть загрузки дольше минуты, верните --entryPoints.<имя>.transport.respondingTimeouts.readTimeout=0.',
  'nginx-proxy': 'nginx-proxy: последний тег nginxproxy/nginx-proxy (и docker-gen, если он запущен отдельно).',
}

const proxyHintKey = (p: ProxyOld) => (p.image.includes('caddy-docker-proxy') ? 'cdp' : p.kind)

function ProxyOldWarning({ items }: { items: ProxyOld[] }) {
  const { t } = useI18n()
  const keys = [...new Set(items.map(proxyHintKey))].filter((k) => PROXY_HINT[k])
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Старый образ прокси')}</div>
      </div>
      <div className="net-note">
        {t('{list}: образ собран больше года назад. Прокси смотрит в интернет, а за год в нем закрывают не одну дыру.',
          { list: items.map((p) => `${p.name} (${proxyBuilt(p)})`).join(', ') })}
      </div>
      {keys.map((k) => (
        <div key={k} className="small">{t(PROXY_HINT[k])}</div>
      ))}
      <div className="net-foot">
        {t('Обновлять лучше через стенд рядом: новый образ на других портах, сравнить ответы всех доменов, потом переключить, а старый тег держать для отката. Ноды с этой проблемой плагин инвентаря ansible собирает в группу kervax_proxy_old, caddy по роли обновляет caddy_harden.yml.')}
      </div>
    </div>
  )
}

// Контейнеры с ключами от хоста сверх положенного по назначению: что это значит, как поправить и
// кнопка "по назначению" (точечный мут dockrisk:<имя>), если так задумано.
const RISK_FIX: Record<string, string> = {
  priv: 'privileged: уберите privileged: true и дайте только нужные capabilities (cap_add) и устройства (devices).',
  caps: 'capabilities: уберите из cap_add SYS_ADMIN, SYS_MODULE, SYS_PTRACE и подобные, если приложение работает без них.',
  sock: 'docker-сокет: если приложению нужен список контейнеров или события, пусть ходит через прокси сокета только на чтение (wollomatic/socket-proxy во внутренней сети), а сам сокет уберите из volumes.',
  host: 'корень хоста: смонтируйте только нужный каталог и, если писать туда не нужно, с :ro.',
}

function RiskWarning({ server: s, canAct, onChanged }: { server: Server; canAct: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState('')
  const items = s.docker_risk ?? []
  const live = items.filter((x) => !x.muted)
  const muted = items.filter((x) => x.muted)
  const flags = [...new Set(live.flatMap((x) => x.flags))].filter((f) => RISK_FIX[f])
  const toggle = (name: string, on: boolean) => {
    const key = DOCKRISK_MUTE + name
    const cur = s.alert_mutes ?? []
    setBusy(name)
    updateServer(s.id, { alert_mutes: on ? [...new Set([...cur, key])] : cur.filter((m) => m !== key) })
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(''))
  }
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Ключи от хоста')}</div>
      </div>
      {live.length > 0 && (
        <>
          <div className="net-prob">
            {t('У контейнеров ключи от хоста: взлом приложения в них даст root на сервере.')}
          </div>
          <div className="loc-results">
            {live.map((x) => (
              <div key={x.name} className="loc-res hs-warn-row">
                <span className="lk-dot lk-dot-bad" />
                <div className="hs-warn-main">
                  <span className="mono">{x.name}</span> <span className="muted small">({x.image})</span>
                  <div className="small">{dockRiskText(x, t)}</div>
                </div>
                {canAct && (
                  <button className="ghost small" disabled={busy === x.name} onClick={() => toggle(x.name, true)}
                    title={t('Так задумано: убрать из "Требует действий" и не алертить по этому контейнеру')}>
                    {t('по назначению')}
                  </button>
                )}
              </div>
            ))}
          </div>
          {flags.length > 0 && (
            <div className="net-foot">
              {flags.map((f) => (
                <div key={f}>{t(RISK_FIX[f])}</div>
              ))}
            </div>
          )}
        </>
      )}
      {muted.length > 0 && (
        <div className="net-foot hs-muted-list">
          <div>
            {t('По назначению: {list}', { list: muted.map((x) => `${x.name} (${dockRiskText(x, t)})`).join(', ') })}
            {canAct && muted.map((x) => (
              <button key={x.name} className="ghost small" disabled={busy === x.name} onClick={() => toggle(x.name, false)}>
                {t('вернуть {name}', { name: x.name })}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Контейнеры, которые после перезагрузки не поднимутся: restart policy no или on-failure. Одна
// команда на все и кнопка "не нужен после перезагрузки" (мут noboot:<имя>), если так задумано.
function NoBootWarning({ server: s, canAct, onChanged }: { server: Server; canAct: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState('')
  const [copied, setCopied] = useState(false)
  const items = s.noboot?.docker ?? []
  const live = items.filter((x) => !x.muted)
  const muted = items.filter((x) => x.muted)
  const cmd = `docker update --restart unless-stopped ${live.map((x) => x.name).join(' ')}`
  const toggle = (name: string, on: boolean) => {
    const key = NOBOOT_MUTE + name
    const cur = s.alert_mutes ?? []
    setBusy(name)
    updateServer(s.id, { alert_mutes: on ? [...new Set([...cur, key])] : cur.filter((m) => m !== key) })
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(''))
  }
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Не поднимутся после перезагрузки')}</div>
      </div>
      {live.length > 0 && (
        <>
          <div className="net-note">
            {t('После перезагрузки не поднимутся: у контейнеров нет restart policy (или on-failure), докер запускает после ребута только always и unless-stopped.')}
          </div>
          <div className="loc-results">
            {live.map((x) => (
              <div key={x.name} className="loc-res hs-warn-row">
                <span className="lk-dot lk-dot-warn" />
                <div className="hs-warn-main">
                  <span className="mono">{x.name}</span> <span className="muted small">({x.image}, restart {x.policy})</span>
                </div>
                {canAct && (
                  <button className="ghost small" disabled={busy === x.name} onClick={() => toggle(x.name, true)}
                    title={t('Так задумано: не напоминать про этот контейнер')}>
                    {t('не нужен после перезагрузки')}
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="agent-advice-cmd">
            <pre>{cmd}</pre>
            <button
              className="ghost"
              onClick={() => {
                navigator.clipboard?.writeText(cmd)
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1500)
              }}
            >
              {copied ? t('Скопировано') : t('Копировать')}
            </button>
          </div>
          <div className="net-foot">
            {t('Если контейнер из compose, поправьте и сам файл (restart: unless-stopped), иначе следующий up вернет как было.')}
          </div>
        </>
      )}
      {muted.length > 0 && (
        <div className="net-foot hs-muted-list">
          <div>
            {t('Не нужны после перезагрузки: {list}', { list: muted.map((x) => x.name).join(', ') })}
            {canAct && muted.map((x) => (
              <button key={x.name} className="ghost small" disabled={busy === x.name} onClick={() => toggle(x.name, false)}>
                {t('вернуть {name}', { name: x.name })}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// readonly: доступ есть, но действие получило 403 - на ноде прокси только для чтения
function EnableBlock({ readonly = false }: { readonly?: boolean }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{readonly ? t('Доступ только на чтение') : t('Нет доступа к контейнерам')}</div>
      </div>
      <div className={readonly ? 'net-prob' : 'net-note'}>
        {readonly
          ? t('Прокси docker-сокета на этой ноде пускает только чтение, поэтому перезапуск, стоп и старт дают 403. Замените его этой командой: просмотр плюс перезапуск, стоп и старт, без exec, создания контейнеров и образов, слушает только 127.0.0.1 хоста.')
          : t('Docker установлен (версии видны). Список контейнеров скрыт — включите read-only доступ (docker-socket-proxy: только просмотр + restart, без exec/root). Новым нодам — флаг --docker при установке.')}
      </div>
      <div className="agent-advice-cmd">
        <pre>{DOCKER_ENABLE_CMD}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(DOCKER_ENABLE_CMD)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? t('Скопировано') : t('Копировать')}
        </button>
      </div>
    </div>
  )
}

function ContainerRow({
  serverId,
  c,
  problem,
  canAct,
  onLogs,
  onChanged,
  onDenied,
  sock = false,
  day = 0,
}: {
  serverId: number
  c: DockerContainer
  day?: number // перезапусков за сутки (restarts_day)
  problem?: string // 'down' | 'loop' — по контейнеру уже отправлен алерт
  canAct: boolean
  onLogs: () => void
  onChanged: () => void
  onDenied?: () => void // прокси ответил 403: на ноде доступ только на чтение
  sock?: boolean // внешний прокси с docker-сокетом
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const act = async (action: 'restart' | 'stop' | 'start') => {
    const label = { restart: t('перезапустить'), stop: t('остановить'), start: t('запустить') }[action]
    if (!window.confirm(t('{a} контейнер «{c}»?', { a: label, c: c.name }))) return
    setBusy(action)
    setErr(null)
    try {
      const res = await runAndWait(serverId, c.name, action)
      if (res.status !== 'done') {
        setErr(res.result || t('не удалось'))
        if ((res.result || '').includes('(403)')) onDenied?.()
      }
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(null)
    }
  }
  const running = c.state === 'running'
  const tone = dockerTone(c, problem)
  // крутящийся по кругу и unhealthy обычно running: точку по алерту и healthcheck, не по state
  const dot = problem === 'down' ? ' lk-dot-bad' : problem || c.health === 'unhealthy' ? ' lk-dot-warn' : DOT[tone] ?? ''
  // агент 2.22+ сказал, что ошибки запуска нет: значит, остановили руками
  const byHand = tone === 't-muted' && c.exit !== undefined && RESTART_POLICIES.has(c.policy ?? '')
  // problem приходит из alert_state бэкенда: подсвечиваем ровно те контейнеры, про
  // которые панель уже написала в телеграм. Крашащийся контейнер обычно в состоянии
  // running (он же перезапускается), поэтому по одному c.state он выглядел зелёным.
  return (
    <div
      className={`loc-res docker-row ${tone === 't-muted' ? 'docker-row-stopped' : tone}${problem ? ` docker-row-${problem}` : ''}`}
    >
      <span className={`lk-dot${dot}`} />
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          {problem && (
            <span className="docker-prob-ico" title={problem === 'down'
              ? t('контейнер упал и не поднялся — по нему отправлен алерт')
              : t('контейнер постоянно перезапускается — по нему отправлен алерт')}>
              🔥
            </span>
          )}
          {c.name}
          {c.health === 'unhealthy' && (
            <span className="type-chip off" title={t('healthcheck: unhealthy')}>unhealthy</span>
          )}
          {sock && (
            <span className="type-chip off" title={t('прокси смотрит в интернет и держит docker-сокет: взлом прокси даст root на хосте')}>
              🔓 docker.sock
            </span>
          )}
          {!!c.restarts && c.restarts > 0 && (
            <span
              className={`type-chip${day >= RESTARTS_DAY_MIN ? ' t-degraded' : ''}`}
              title={day >= RESTARTS_DAY_MIN
                ? t('перезапусков за сутки: {d}, за все время: {n}. Падает раз в час-другой: между падениями работает, но теряет соединения и очереди', { d: day, n: c.restarts })
                : t('перезапусков за все время (RestartCount): {n} (само по себе не проблема, тревога - на устойчивый crash-loop)', { n: c.restarts })}
            >
              ⟳ {c.restarts}{day > 0 && ` · ${t('{d} за сутки', { d: day })}`}
            </span>
          )}
        </div>
        <div className="docker-c-img mono muted small" title={c.image}>
          {c.image}
        </div>
        {running && (c.cpu != null || c.mem != null) && (
          <div className="hs-c-use mono muted small">
            {[c.cpu != null ? `CPU ${Math.round(c.cpu)}%` : '', c.mem != null ? fmtBytes(c.mem) : ''].filter(Boolean).join(' · ')}
          </div>
        )}
        {c.err && !running && (
          <div className="form-error small docker-c-err" title={c.err}>
            {t('докер не смог запустить')}: {c.err}
          </div>
        )}
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div
        className={`docker-c-status mono small ${tone}`}
        title={byHand ? t('остановлен вручную: docker такой контейнер сам не поднимает, это не авария и тревоги нет') : undefined}
      >
        {c.status}
      </div>
      <div className="docker-actions">
        <button className="ghost icon-btn" onClick={onLogs} title={t('Логи')}>
          📄
        </button>
        {canAct &&
          (running ? (
            <>
              <button className="ghost icon-btn" disabled={!!busy} onClick={() => act('restart')} title={t('Перезапустить')}>
                {busy === 'restart' ? '…' : '⟳'}
              </button>
              <button className="ghost icon-btn" disabled={!!busy} onClick={() => act('stop')} title={t('Остановить')}>
                {busy === 'stop' ? '…' : '⏹'}
              </button>
            </>
          ) : (
            <button className="ghost icon-btn" disabled={!!busy} onClick={() => act('start')} title={t('Запустить')}>
              {busy === 'start' ? '…' : '▶'}
            </button>
          ))}
      </div>
    </div>
  )
}

type LogRange = 'tail' | 'hour' | 'day'
function LogsModal({
  server,
  container,
  onClose,
}: {
  server: Server
  container: string
  onClose: () => void
}) {
  const { t } = useI18n()
  const [logs, setLogs] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [range, setRange] = useState<LogRange>('tail')
  const load = useCallback(
    async (r: LogRange) => {
      setLoading(true)
      setErr(null)
      setRange(r)
      const opts = r === 'tail' ? { tail: 400 } : { since: r === 'hour' ? 3600 : 86400 }
      try {
        const res = await runAndWait(server.id, container, 'logs', opts)
        if (res.status === 'done') setLogs(res.result ?? '')
        else setErr(res.result || t('не удалось получить логи'))
      } catch {
        setErr(t('ошибка'))
      } finally {
        setLoading(false)
      }
    },
    [server.id, container, t],
  )
  useEffect(() => {
    load('tail')
  }, [load])
  const download = () => {
    const blob = new Blob([logs ?? ''], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
    a.href = url
    a.download = `${container}_${stamp}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }
  const ranges: { k: LogRange; l: string }[] = [
    { k: 'tail', l: t('400 строк') },
    { k: 'hour', l: t('за час') },
    { k: 'day', l: t('за день') },
  ]
  // рендерим только хвост (браузер не тянет 20МБ текста в <pre>), скачивание — полное
  const VIEW_MAX = 500_000
  const truncated = !!logs && logs.length > VIEW_MAX
  const view = truncated ? logs!.slice(-VIEW_MAX) : logs
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal logs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3 className="mono">
            {container} <span className="muted small">· <CountryFlag code={server.country} /> {server.name}</span>
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="logs-toolbar">
          <div className="win-switch">
            {ranges.map(({ k, l }) => (
              <button
                key={k}
                className={`win-btn${range === k ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => load(k)}
              >
                {l}
              </button>
            ))}
          </div>
          <div className="spacer" />
          <button className="ghost" disabled={loading} onClick={() => load(range)}>
            {loading ? t('…') : t('Обновить')}
          </button>
          <button className="ghost" disabled={!logs} onClick={download}>
            {t('Скачать .txt')}
          </button>
        </div>
        {err && <p className="form-error">{err}</p>}
        {loading && logs == null ? (
          <div className="muted small">{t('Запрашиваю логи у агента…')}</div>
        ) : logs === '' ? (
          <div className="logs-empty muted">
            {range === 'tail'
              ? t('Пусто — контейнер ничего не писал в лог.')
              : t('Нет записей за выбранный период. Контейнер давно не писал в лог — попробуйте «400 строк».')}
          </div>
        ) : (
          <pre className="logs-pre">{view}</pre>
        )}
        <div className="muted small">
          {truncated
            ? t('Показаны последние 500 КБ из {mb} МБ — скачайте .txt для полного.', {
                mb: ((logs?.length ?? 0) / 1_000_000).toFixed(1),
              })
            : t('Логи не хранятся — читаются с ноды по запросу (кап ~20 МБ).')}
        </div>
      </div>
    </div>,
    document.body,
  )
}

// Карточка Docker-ноды: меню разделов слева, на экране один раздел, как у сервера (cardLayout).
// Раньше сверху шли плитки, под ними список, а под списком карточками то, что поправить на хосте,
// и до них надо было докручивать. На сводке проблемы ноды из "Что сломано", плитки и карта.
// Логи - модалка сверху.
const DOCKER_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'containers', label: 'Контейнеры', group: 'Контейнеры' },
  { id: 'host', label: 'Доступ к хосту', group: 'Замечания' },
  { id: 'proxy', label: 'Прокси', group: 'Замечания' },
  { id: 'boot', label: 'Перезагрузка', group: 'Замечания' },
]
const DOCKER_SEC_IDS = new Set(DOCKER_SECTIONS.map((x) => x.id))
// внешний прокси в контейнере: по образу, как его узнает бэкенд для proxy_outdated
const PROXY_IMAGE = /traefik|caddy|nginx-proxy|docker-gen/

function DockerHostModal({
  server: s,
  docker,
  canAct,
  initialSec,
  onClose,
  onChanged,
}: {
  server: Server
  docker: DockerInfo
  canAct: boolean
  initialSec?: string | null // из ссылки алерта или с главной: ?docker=id&sec=host
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [logs, setLogs] = useState<string | null>(null)
  const [hideStopped, setHideStopped] = useState(false)
  const [denied, setDenied] = useState(false)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'state' | 'name' | 'restarts'>('state')
  // открытый раздел: сводка или тот, куда вела ссылка; в адресе он виден (?sec=)
  const [active, setActive] = useState<string>(() => (DOCKER_SEC_IDS.has(initialSec ?? '') ? initialSec! : 'overview'))
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    setQ('')
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  const cs = docker.containers ?? []
  const running = cs.filter((c) => c.state === 'running').length
  const stopped = cs.length - running
  const ql = q.trim().toLowerCase()
  // при равенстве НЕ сортируем по имени — сохраняем родной docker-порядок (стабильная
  // сортировка). Иначе при всех running/0-рестартов любой сорт схлопывался в алфавит.
  const probs = s.docker_alerts ?? {}
  const cmp: Record<string, (a: DockerContainer, b: DockerContainer) => number> = {
    state: (a, b) => (a.state === 'running' ? 0 : 1) - (b.state === 'running' ? 0 : 1),
    name: (a, b) => a.name.localeCompare(b.name),
    restarts: (a, b) => (b.restarts ?? 0) - (a.restarts ?? 0),
  }
  // Проблемный контейнер всегда сверху, каким бы ни был выбранный порядок: в списке
  // из 26 штук крашащийся уезжал в середину и терялся среди зелёных.
  const byProblem = (a: DockerContainer, b: DockerContainer) =>
    (probs[b.name] ? 1 : 0) - (probs[a.name] ? 1 : 0)
  const list = cs
    .filter((c) => !hideStopped || c.state === 'running')
    .filter((c) => !ql || `${c.name} ${c.image} ${c.status}`.toLowerCase().includes(ql))
    .slice()
    .sort((a, b) => byProblem(a, b) || cmp[sort](a, b))
  // плитки: аварии - по ним ушел алерт (упал или крутится по кругу); перезапуски - за сутки
  const pv = Object.values(probs)
  const pDown = pv.filter((p) => p === 'down').length
  const unhealthy = cs.filter((c) => c.health === 'unhealthy')
  const days = cs.map((c) => ({ name: c.name, n: s.restarts_day?.['docker:' + c.name] ?? 0 })).filter((x) => x.n > 0)
  const dayTop = days.reduce<{ name: string; n: number } | null>((a, b) => (!a || b.n > a.n ? b : a), null)
  const used = cs.filter((c) => c.state === 'running' && (c.mem != null || c.cpu != null))
  const memSum = used.reduce((n, c) => n + (c.mem ?? 0), 0)
  const cpuSum = used.reduce((n, c) => n + (c.cpu ?? 0), 0)
  const exposed = s.docker_exposed ?? []
  const outdated = s.proxy_outdated ?? []
  const proxies = cs.filter((c) => PROXY_IMAGE.test(c.image))
  // Пустой noboot с бэкенда значит "все поднимутся", а не "нет данных": бэкенд отдает его, только
  // когда есть что показать. Нет данных - когда агент не прислал restart policy ни у одного
  // контейнера (старый агент)
  const policyKnown = cs.length === 0 || cs.some((c) => c.policy != null)

  // Разделы: контейнеры и замечания есть, когда есть доступ к докеру; прокси - когда он на ноде
  // есть (или его образ устарел)
  const shown: Record<string, boolean> = {
    overview: true,
    containers: docker.access,
    host: docker.access,
    proxy: docker.access && (proxies.length > 0 || outdated.length > 0),
    boot: docker.access,
  }
  const secs = DOCKER_SECTIONS.filter((x) => shown[x.id])
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)

  // Замечания по разделам. Проблемы те же, что в "Что сломано" на главной: упавшие и крутящиеся
  // по кругу (docker_alerts), docker-сокет у прокси, утечки и частые перезапуски с бэкенда.
  // Ключи от хоста, старый прокси и restart policy - советы из "Требует действий"
  const live = s.online && docker.access
  const mutes = new Set(s.alert_mutes ?? [])
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, x: SecItem) => (items[sec] ??= []).push(x)
  if (live) {
    const down = cs.filter((c) => probs[c.name] === 'down').map((c) => c.name)
    const loop = cs.filter((c) => probs[c.name] === 'loop').map((c) => c.name)
    if (down.length)
      add('containers', {
        level: 2,
        count: down.length,
        since: down.map((n) => s.docker_since?.[n]).filter((x): x is string => !!x).sort()[0] ?? null,
        text: t(down.length === 1 ? 'не работает контейнер {list}' : 'не работают контейнеры {list}', { list: shortList(down, t, 3) }),
      })
    if (loop.length)
      add('containers', {
        level: 2,
        count: loop.length,
        text: t(loop.length === 1 ? 'перезапускается в цикле {list}' : 'перезапускаются в цикле {list}', { list: shortList(loop, t, 3) }),
      })
    // утечки памяти и частые перезапуски контейнеров - с бэкенда, без заглушенных и известных
    for (const i of srvIssues(s, t).filter((x) => x.section === 'docker'))
      add('containers', { level: i.tone === 't-down' ? 2 : 1, text: i.text, since: i.since })
    if (exposed.length && !mutes.has('docker_sock'))
      add('host', {
        level: 1,
        count: exposed.length,
        text: t('docker-сокет у {list} - взлом прокси даст root на хосте', { list: exposed.map((e) => e.name).join(', ') }),
      })
    const risk = (s.docker_risk ?? []).filter((x) => !x.muted)
    if (risk.length && !mutes.has('docker_risk'))
      add('host', {
        level: 1,
        count: risk.length,
        text: t('ключи от хоста у {list}', { list: shortList(risk.map((x) => `${x.name} (${dockRiskText(x, t)})`), t, 3) }),
      })
    if (outdated.length)
      add('proxy', {
        level: 1,
        count: outdated.length,
        text: t('образ собран больше года назад: {list}', { list: outdated.map((p) => `${p.name} (${proxyBuilt(p)})`).join(', ') }),
      })
    const nb = s.noboot_advice?.docker ?? []
    if (nb.length)
      add('boot', {
        level: 1,
        count: nb.length,
        text: t('не поднимутся после перезагрузки: {list}', { list: shortList(nb, t, 3) }),
      })
  }
  const ok: Record<string, string> = {
    containers: cs.length
      ? [t('запущено {a} из {b}', { a: running, b: cs.length }), stopped ? t('остановлено: {n}', { n: stopped }) : '']
          .filter(Boolean).join(', ')
      : t('Контейнеров нет'),
    host: s.docker_risk ? t('ключей от хоста ни у кого нет') : t('нет данных'),
    proxy: proxies.length
      ? t('{list}, образ моложе года', { list: shortList(proxies.map((c) => c.name), t, 3) })
      : '',
    boot: policyKnown ? t('все контейнеры поднимутся после перезагрузки') : t('нет данных'),
  }
  const info: Record<string, SecInfo> = {}
  for (const { id } of DOCKER_SECTIONS) info[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // unhealthy - не авария, но и не "все в порядке": желтым на карте, без числа в меню
  if (live && unhealthy.length && !info.containers.level)
    info.containers = { ...info.containers, level: 1, line: t('unhealthy: {list}', { list: shortList(unhealthy.map((c) => c.name), t, 3) }) }
  if (!s.docker_risk && !info.host.level) info.host.idle = true
  if (!policyKnown && !info.boot.level) info.boot.idle = true
  if (live) {
    const riskAll = (s.docker_risk ?? []).filter((x) => !x.muted)
    markMuted(info, [
      ...srvMuted(s, t).filter((i) => i.section === 'docker').map((i) => ({ sec: 'containers', text: i.text })),
      ...(exposed.length && mutes.has('docker_sock')
        ? [{ sec: 'host', text: t('docker-сокет у {list}', { list: shortList(exposed.map((e) => e.name), t, 3) }) }] : []),
      ...(riskAll.length && mutes.has('docker_risk')
        ? [{ sec: 'host', text: t('ключи от хоста у {list}', { list: shortList(riskAll.map((x) => x.name), t, 3) }) }] : []),
    ], t)
  }

  const rows: OvRow[] = DOCKER_SECTIONS.flatMap(({ id, label }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: shown[id] ? { label: t(label), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const tileLabel = (label: string) =>
    info.containers.fresh ? <>{label} <FreshChip since={info.containers.fresh} /></> : label

  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        {!s.online && (
          <div className="srv-offline-note">
            <span className="srv-offline-ic">🔴</span>
            <span>
              <b>{t('Сервер недоступен')}</b>{' '}
              {t('— агент молчит {ago}. Всё, что ниже, — последние данные, снятые тогда же.', { ago: fmtRel(s.last_seen) })}
            </span>
          </div>
        )}
        <div className="srv-meta srv-meta-line">
          <span className="srv-meta-item"><span className="srv-meta-k">Docker</span> {docker.version || '—'}</span>
          {docker.compose && <span className="srv-meta-item"><span className="srv-meta-k">compose</span> {docker.compose}</span>}
          {docker.access && (
            <span className="srv-meta-item"><span className="srv-meta-k">{t('контейнеров')}</span> {cs.length}</span>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        {!docker.access ? (
          <EnableBlock />
        ) : (
          <div className="srv-layout" ref={layoutRef}>
            <CardNav secs={secs} active={cur.id} info={info} onGo={go} />
            <div className="srv-main">
              <div className="srv-main-head">
                <h4>{t(cur.label)}</h4>
                {cur.id === 'containers' && cs.length > 0 && (
                  <div className="modal-toolbar srv-head-tools">
                    {cs.length > 5 && (
                      <input
                        className="checks-search modal-search"
                        placeholder={t('Фильтр: имя, образ…')}
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                      />
                    )}
                    <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
                      <option value="state">{t('сорт: статус')}</option>
                      <option value="name">{t('сорт: имя')}</option>
                      <option value="restarts">{t('сорт: рестарты')}</option>
                    </select>
                    {stopped > 0 && (
                      <button className="ghost small docker-hide-stopped" onClick={() => setHideStopped((v) => !v)}>
                        {hideStopped ? t('Показать остановленные ({n})', { n: stopped }) : t('Скрыть остановленные ({n})', { n: stopped })}
                      </button>
                    )}
                  </div>
                )}
              </div>

              {cur.id === 'overview' && (
                <div className="srv-sec">
                  <OvRows rows={rows} />
                  <div className="net-tiles srv-tiles">
                    <button className="srv-tile-btn" onClick={() => go('containers')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel(t('Запущено'))}
                        value={String(running)}
                        unit={t('из {n}', { n: cs.length })}
                        sub={stopped ? t('остановлено: {n}', { n: stopped }) : t('все запущены')}
                        tone={stopped ? undefined : 'ok'}
                      />
                    </button>
                    <button className="srv-tile-btn" onClick={() => go('containers')} title={t('к разделу')}>
                      <NetTile
                        label={t('Аварии')}
                        value={String(pv.length)}
                        sub={subOf(
                          pDown > 0 && t('упали: {n}', { n: pDown }),
                          pv.length - pDown > 0 && t('по кругу: {n}', { n: pv.length - pDown }),
                          unhealthy.length > 0 && t('unhealthy: {n}', { n: unhealthy.length }),
                        ) || t('нет')}
                        tone={pv.length ? 'bad' : unhealthy.length ? 'warn' : 'ok'}
                      />
                    </button>
                    {used.length > 0 && (
                      <button className="srv-tile-btn" onClick={() => go('containers')} title={t('к разделу')}>
                        <NetTile label={t('Память контейнеров')} value={fmtBytes(memSum)} sub={`CPU ${Math.round(cpuSum)}%`} />
                      </button>
                    )}
                    <button className="srv-tile-btn" onClick={() => { go('containers'); setSort('restarts') }} title={t('к разделу')}>
                      <NetTile
                        label={t('Перезапусков за сутки')}
                        value={String(days.reduce((n, x) => n + x.n, 0))}
                        sub={dayTop ? `${dayTop.name}: ${dayTop.n}` : t('нет')}
                        tone={dayTop && dayTop.n >= RESTARTS_DAY_MIN ? 'warn' : undefined}
                      />
                    </button>
                  </div>
                  <OvAreas
                    cap={t('Нода по областям')}
                    items={DOCKER_SECTIONS.filter((x) => x.id !== 'overview' && shown[x.id]).map((x) => ({ id: x.id, label: x.label, info: info[x.id] }))}
                    onGo={go}
                  />
                </div>
              )}

              {cur.id === 'containers' && (
                <div className="srv-sec">
                  {denied && <EnableBlock readonly />}
                  <div className="loc-results docker-clist">
                    {cs.length === 0 ? (
                      <div className="srv-empty">{t('Контейнеров нет')}</div>
                    ) : list.length === 0 ? (
                      <div className="srv-empty">{t('Ничего не найдено.')}</div>
                    ) : (
                      list.map((c) => (
                        <ContainerRow
                          key={c.name}
                          serverId={s.id}
                          c={c}
                          problem={probs[c.name]}
                          onDenied={() => setDenied(true)}
                          sock={exposed.some((e) => e.name === c.name)}
                          day={s.restarts_day?.['docker:' + c.name] ?? 0}
                          canAct={canAct}
                          onLogs={() => setLogs(c.name)}
                          onChanged={onChanged}
                        />
                      ))
                    )}
                  </div>
                </div>
              )}

              {cur.id === 'host' && (
                <div className="srv-sec">
                  {exposed.length > 0 && <SockWarning items={exposed} />}
                  {(s.docker_risk?.length ?? 0) > 0 && <RiskWarning server={s} canAct={canAct} onChanged={onChanged} />}
                  {!exposed.length && !(s.docker_risk?.length ?? 0) && (
                    <div className="srv-empty">
                      {s.docker_risk
                        ? t('Ни у одного контейнера нет ключей от хоста: ни docker-сокета, ни privileged, ни опасных capabilities, ни корня хоста на запись.')
                        : t('Агент этой ноды еще не отдает ключи контейнеров от хоста: обновите агент.')}
                    </div>
                  )}
                </div>
              )}

              {cur.id === 'proxy' && (
                <div className="srv-sec">
                  {outdated.length > 0 ? (
                    <ProxyOldWarning items={outdated} />
                  ) : (
                    <div className="srv-empty">
                      {t('Внешний прокси {list}: образ собран меньше года назад.', { list: proxies.map((c) => `${c.name} (${c.image})`).join(', ') })}
                    </div>
                  )}
                </div>
              )}

              {cur.id === 'boot' && (
                <div className="srv-sec">
                  {(s.noboot?.docker.length ?? 0) > 0 ? (
                    <NoBootWarning server={s} canAct={canAct} onChanged={onChanged} />
                  ) : (
                    <div className="srv-empty">
                      {policyKnown
                        ? t('У всех контейнеров restart policy always или unless-stopped: после перезагрузки докер поднимет их сам.')
                        : t('Агент {v} не прислал restart policy контейнеров. Обновите его: кнопка "Обновить все" в разделе "Серверы" или стрелка у версии агента в карточке сервера.', { v: s.agent_version || '' })}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      {logs && <LogsModal server={s} container={logs} onClose={() => setLogs(null)} />}
    </div>,
    document.body,
  )
}

// Логотип Docker (moby) — вместо эмодзи. Заливка фирменным синим.
function DockerLogo() {
  return (
    <svg className="docker-logo" viewBox="0 0 24 24" aria-hidden fill="#2496ED">
      <path d="M13.983 11.078h2.119a.186.186 0 00.186-.185V9.006a.186.186 0 00-.186-.186h-2.119a.185.185 0 00-.185.185v1.888c0 .102.083.185.185.185m-2.954-5.43h2.118a.186.186 0 00.186-.186V3.574a.186.186 0 00-.186-.185h-2.118a.185.185 0 00-.185.185v1.888c0 .102.082.185.185.185m0 2.716h2.118a.187.187 0 00.186-.186V6.29a.186.186 0 00-.186-.185h-2.118a.185.185 0 00-.185.185v1.887c0 .102.082.185.185.186m-2.93 0h2.12a.186.186 0 00.184-.186V6.29a.185.185 0 00-.185-.185H8.1a.185.185 0 00-.185.185v1.887c0 .102.083.185.185.186m-2.964 0h2.119a.186.186 0 00.185-.186V6.29a.185.185 0 00-.185-.185H5.136a.186.186 0 00-.186.185v1.887c0 .102.084.185.186.186m5.893 2.715h2.118a.186.186 0 00.186-.185V9.006a.186.186 0 00-.186-.186h-2.118a.185.185 0 00-.185.185v1.888c0 .102.082.185.185.185m-2.93 0h2.12a.185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 00-.184.185v1.888c0 .102.083.185.185.185m-2.964 0h2.119a.185.185 0 00.185-.185V9.006a.185.185 0 00-.184-.186h-2.12a.186.186 0 00-.186.186v1.887c0 .102.084.185.186.185m-2.92 0h2.12a.185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 00-.184.185v1.888c0 .102.082.185.185.185M23.763 9.89c-.065-.051-.672-.51-1.954-.51-.338.001-.676.03-1.01.087-.248-1.7-1.653-2.53-1.716-2.566l-.344-.199-.226.327c-.284.438-.49.922-.612 1.43-.23.97-.09 1.882.403 2.661-.595.332-1.55.413-1.744.42H.751a.751.751 0 00-.75.748 11.376 11.376 0 00.692 4.062c.545 1.428 1.355 2.48 2.41 3.124 1.18.723 3.1 1.137 5.275 1.137.983.003 1.963-.086 2.93-.266a12.248 12.248 0 003.823-1.389c.98-.567 1.86-1.288 2.61-2.136 1.252-1.418 1.998-2.997 2.553-4.4h.222c1.372 0 2.215-.549 2.68-1.009.309-.293.55-.65.707-1.046l.098-.288z" />
    </svg>
  )
}

// Строка хоста: точка - на связи ли нода, справа запущено из всех. Красное - по контейнеру
// ушел алерт, желтое - есть остановленные
function HostRow({
  server: s,
  docker,
  showGroup,
  hint,
  onOpen,
}: {
  server: Server
  docker: DockerInfo
  showGroup?: boolean
  hint?: string | null // что из нажатой плитки нашлось на хосте
  onOpen: () => void
}) {
  const { t } = useI18n()
  const cs = docker.containers ?? []
  const running = cs.filter((c) => c.state === 'running').length
  const probs = Object.keys(s.docker_alerts ?? {}).length
  return (
    <button className="loc-res loc-res-btn hs-host" onClick={onOpen}>
      <span className={`lk-dot ${s.online ? 'lk-dot-ok' : 'lk-dot-bad'}`} title={s.online ? t('на связи') : t('не на связи')} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {!docker.access && <span className="type-chip off">{t('нет доступа')}</span>}
          {probs > 0 && (
            <span className="type-chip off" title={t('по контейнерам ушел алерт: упали или крутятся по кругу')}>
              🔥 {probs}
            </span>
          )}
          {(s.docker_exposed?.length ?? 0) > 0 && (
            <span className="type-chip off" title={t('внешний прокси держит docker-сокет: взлом прокси даст root на хосте')}>
              🔓 docker.sock
            </span>
          )}
          {(s.noboot?.docker ?? []).some((x) => !x.muted) && (
            <span className="type-chip off" title={t('после перезагрузки не поднимутся: нет restart policy')}>
              🔁 {(s.noboot?.docker ?? []).filter((x) => !x.muted).length}
            </span>
          )}
          {(s.docker_risk ?? []).some((x) => !x.muted) && (
            <span className="type-chip off" title={t('у контейнеров ключи от хоста: взлом приложения в них даст root на сервере')}>
              🔑 {(s.docker_risk ?? []).filter((x) => !x.muted).map((x) => x.name).join(', ')}
            </span>
          )}
          {(s.proxy_outdated?.length ?? 0) > 0 && (
            <span className="type-chip off" title={t('образ внешнего прокси собран больше года назад')}>
              🧓 {(s.proxy_outdated ?? []).map((p) => proxyBuilt(p)).join(', ')}
            </span>
          )}
        </div>
        <div className="mono muted small">
          Docker {docker.version || '—'}
          {docker.compose ? ` · compose ${docker.compose}` : ''}
        </div>
        {hint && <div className="hs-host-hit">{hint}</div>}
      </div>
      <div className="hs-host-count mono">
        {docker.access ? (
          <span className={probs ? 't-down' : running < cs.length ? 't-degraded' : 't-up'}>
            {t('{r}/{n}', { r: running, n: cs.length })}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
    </button>
  )
}

type DHost = { s: Server; d: DockerInfo }
type DFilter = 'stopped' | 'alerts' | 'attn'

// Что из нажатой плитки нашлось на хосте - строка под его именем; null - хост под плитку не
// подходит. Условия те же, что у чисел в плитках (только хосты на связи), поэтому имена по
// строкам сходятся с числами плитки
function dockerHit(f: DFilter, { s, d }: DHost, t: (k: string, p?: Record<string, string | number>) => string): string | null {
  if (!s.online) return null
  const cs = d.containers ?? []
  const list = (xs: string[]) => ({ list: shortList(xs, t, 8) })
  if (f === 'stopped') {
    const xs = cs.filter((c) => c.state !== 'running').map((c) => c.name)
    return xs.length ? t('остановлены: {list}', list(xs)) : null
  }
  if (f === 'alerts') {
    const al = Object.entries(s.docker_alerts ?? {})
    const down = al.filter(([, v]) => v === 'down').map(([k]) => k)
    const loop = al.filter(([, v]) => v !== 'down').map(([k]) => k)
    const unh = cs.filter((c) => c.health === 'unhealthy').map((c) => c.name)
    const flap = Object.entries(s.restarts_day ?? {})
      .filter(([k, v]) => k.startsWith('docker:') && v >= RESTARTS_DAY_MIN)
      .map(([k]) => k.slice('docker:'.length))
    return subOf(
      down.length > 0 && t('упали: {list}', list(down)),
      loop.length > 0 && t('по кругу: {list}', list(loop)),
      unh.length > 0 && t('unhealthy: {list}', list(unh)),
      flap.length > 0 && t('часто перезапускаются: {list}', list(flap)),
    ) || null
  }
  const risk = (s.docker_risk ?? []).filter((x) => !x.muted).map((x) => x.name)
  const noboot = (s.noboot?.docker ?? []).filter((x) => !x.muted).map((x) => x.name)
  const sock = (s.docker_exposed ?? []).map((x) => x.name)
  const old = (s.proxy_outdated ?? []).map((x) => x.name)
  return subOf(
    sock.length > 0 && t('docker-сокет у прокси: {list}', list(sock)),
    risk.length > 0 && t('ключи от хоста: {list}', list(risk)),
    noboot.length > 0 && t('без restart policy: {list}', list(noboot)),
    old.length > 0 && t('старый прокси: {list}', list(old)),
  ) || null
}

export function DockerPage({
  onUnauthorized,
  openHostId = null,
  openSec = null,
  onConsumed,
}: {
  onUnauthorized: () => void
  openHostId?: number | null
  openSec?: string | null // раздел карточки из ссылки (?docker=id&sec=host)
  onConsumed?: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openId, setOpenId] = useState<number | null>(openHostId)
  // раздел из ссылки держим здесь до первого открытия: список нод приезжает позже, чем App
  // забывает номер ноды
  const [linkSec, setLinkSec] = useState<{ id: number | null; sec: string | null }>({ id: openHostId, sec: openSec })
  // диплинк с главной: открыть конкретный хост и «съесть» id (чтобы не переоткрывать)
  useEffect(() => {
    if (openHostId != null) {
      setOpenId(openHostId)
      setLinkSec({ id: openHostId, sec: openSec })
      onConsumed?.()
    }
  }, [openHostId, openSec, onConsumed])
  useUrlCard('docker', openId)
  const [query, setQuery] = useState('')
  const { tile, setTile, tileProps } = useTileFilter<DFilter>()
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_docker_groupby') as 'none' | 'group') || 'group',
  )
  const setGrouping = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_docker_groupby', g)
  }

  const load = useCallback(() => {
    listServers()
      .then((s) => {
        setServers(s)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 12000)
    return () => window.clearInterval(id)
  }, [load])

  const allHosts = (servers ?? [])
    .map((s) => ({ s, d: s.last_report?.docker }))
    .filter((x): x is DHost => !!x.d?.present)
  const q = query.trim().toLowerCase()
  const hosts = allHosts
    .filter(({ s, d }) => {
      if (!q) return true
      const hay = [
        s.name,
        s.group_name,
        `docker ${d.version ?? ''}`,
        `compose ${d.compose ?? ''}`,
        ...(d.containers ?? []).flatMap((c) => [c.name, c.image]),
      ]
        .join(' ')
        .toLowerCase()
      return hay.includes(q)
    })
    .map((h) => ({ ...h, hint: tile ? dockerHit(tile, h, t) : null }))
    .filter((h) => !tile || h.hint)
    .sort((a, b) => a.s.name.localeCompare(b.s.name))

  const groups: { key: string; label: string; items: typeof hosts }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, typeof hosts>()
          for (const h of hosts) {
            const k = h.s.group_name?.trim() || ''
            if (!map.has(k)) map.set(k, [])
            map.get(k)!.push(h)
          }
          return [...map.keys()]
            .sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
            .map((k) => ({ key: k, label: k || t('Без группы'), items: map.get(k)! }))
        })()

  const open = allHosts.find(({ s }) => s.id === openId)
  // Сводка: что запущено, что упало и где есть что поправить. Считаем по хостам на связи: у
  // молчащих данные старые, и "все запущены" по ним было бы неправдой. Молчащие названы в
  // первой плитке по именам, а когда не осталось ни одного хоста на связи с доступом, остальных
  // плиток нет: "0 из 0, все запущены" ничего бы не сказало
  const live = allHosts.filter(({ s }) => s.online)
  const names = (xs: DHost[]) => shortList(xs.map(({ s }) => s.name), t, 2)
  const offHosts = allHosts.filter(({ s }) => !s.online)
  const noAccHosts = live.filter(({ d }) => !d.access)
  const cAll = live.flatMap(({ d }) => d.containers ?? [])
  const totalRunning = cAll.filter((c) => c.state === 'running').length
  const alerts = live.flatMap(({ s }) => Object.values(s.docker_alerts ?? {}))
  const aDown = alerts.filter((a) => a === 'down').length
  const unhealthy = cAll.filter((c) => c.health === 'unhealthy').length
  const flappy = live.reduce(
    (n, { s }) => n + Object.entries(s.restarts_day ?? {}).filter(([k, v]) => k.startsWith('docker:') && v >= RESTARTS_DAY_MIN).length,
    0,
  )
  const sockHosts = live.filter(({ s }) => (s.docker_exposed?.length ?? 0) > 0)
  const riskHosts = live.filter(({ s }) => (s.docker_risk ?? []).some((x) => !x.muted))
  const nobootHosts = live.filter(({ s }) => (s.noboot?.docker ?? []).some((x) => !x.muted))
  const oldHosts = live.filter(({ s }) => (s.proxy_outdated?.length ?? 0) > 0)
  const attn = live.filter(
    ({ s }) =>
      (s.docker_exposed?.length ?? 0) > 0 ||
      (s.docker_risk ?? []).some((x) => !x.muted) ||
      (s.noboot?.docker ?? []).some((x) => !x.muted) ||
      (s.proxy_outdated?.length ?? 0) > 0,
  ).length
  // нажатая плитка: подпись чипа над списком и что сказать, если под нее ничего не попало
  const filterLabel: Record<DFilter, string> = {
    stopped: t('остановленные контейнеры'), alerts: t('аварии'), attn: t('хосты с замечаниями'),
  }
  const filterEmpty: Record<DFilter, string> = {
    stopped: t('Все контейнеры запущены.'), alerts: t('Аварий нет.'), attn: t('Замечаний нет.'),
  }

  return (
    <div>
      <div className="checks-head">
        <h2 className="docker-title">
          <DockerLogo /> Docker
        </h2>
        <div className="checks-head-actions">
          {allHosts.length > 1 && (
            <div className="win-switch">
              <button
                className={`win-btn${groupBy === 'group' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('group')}
              >
                {t('По группе')}
              </button>
              <button
                className={`win-btn${groupBy === 'none' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('none')}
              >
                {t('Без групп')}
              </button>
            </div>
          )}
        </div>
      </div>
      {allHosts.length > 0 && (
        <div className="net-tiles hs-tiles">
          <NetTile
            label={t('Хосты с Docker')}
            value={String(allHosts.length)}
            sub={subOf(
              noAccHosts.length > 0 && t('без доступа: {list}', { list: names(noAccHosts) }),
              offHosts.length > 0 && t('не на связи: {list}', { list: names(offHosts) }),
            ) || t('все на связи')}
            tone={noAccHosts.length || offHosts.length ? 'warn' : 'ok'}
            {...tileProps(null)}
          />
          {live.some(({ d }) => d.access) && <>
          <NetTile
            label={t('Контейнеры запущены')}
            value={String(totalRunning)}
            unit={t('из {n}', { n: cAll.length })}
            sub={cAll.length > totalRunning ? t('остановлено: {n}', { n: cAll.length - totalRunning }) : t('все запущены')}
            tone={cAll.length > totalRunning ? undefined : 'ok'}
            {...tileProps('stopped')}
          />
          <NetTile
            label={t('Аварии')}
            value={String(alerts.length)}
            sub={subOf(
              aDown > 0 && t('упали: {n}', { n: aDown }),
              alerts.length - aDown > 0 && t('по кругу: {n}', { n: alerts.length - aDown }),
              unhealthy > 0 && t('unhealthy: {n}', { n: unhealthy }),
              flappy > 0 && t('часто перезапускаются: {n}', { n: flappy }),
            ) || t('нет')}
            tone={alerts.length ? 'bad' : unhealthy || flappy ? 'warn' : 'ok'}
            {...tileProps('alerts')}
          />
          <NetTile
            label={t('Хосты с замечаниями')}
            value={String(attn)}
            sub={subOf(
              sockHosts.length > 0 && t('docker-сокет у прокси: {list}', { list: names(sockHosts) }),
              riskHosts.length > 0 && t('ключи от хоста: {list}', { list: names(riskHosts) }),
              nobootHosts.length > 0 && t('без restart policy: {list}', { list: names(nobootHosts) }),
              oldHosts.length > 0 && t('старый прокси: {list}', { list: names(oldHosts) }),
            ) || t('все в порядке')}
            tone={attn ? 'warn' : 'ok'}
            {...tileProps('attn')}
          />
          </>}
        </div>
      )}
      {allHosts.length > 1 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: хост, группа, контейнер, образ…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}
      {tile && <TileFilterChip label={filterLabel[tile]} onClear={() => setTile(null)} />}
      {err && <p className="form-error">{err}</p>}
      {servers && allHosts.length === 0 && (
        <div className="card muted">
          {t('Docker не найден ни на одном сервере. Агент определяет его сам; если docker есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      {servers && allHosts.length > 0 && hosts.length === 0 && (
        <div className="card muted">{tile && !q ? filterEmpty[tile] : t('Ничего не найдено.')}</div>
      )}
      {hosts.length > 0 && (
        <div className="net-cards hs-groups">
          {groups.map((g) => (
            <div key={g.key} className="chart-card net-card net-card-wide">
              <div className="net-head">
                <div className="chart-cap net-cap">
                  {groupBy === 'none' ? t('Хосты') : g.label}
                  <span className="net-n">{g.items.length}</span>
                </div>
              </div>
              <div className="loc-results">
                {g.items.map(({ s, d, hint }) => (
                  <HostRow
                    key={s.id}
                    server={s}
                    docker={d}
                    showGroup={groupBy !== 'group'}
                    hint={hint}
                    onOpen={() => setOpenId(s.id)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {open && (
        <DockerHostModal
          key={open.s.id}
          server={open.s}
          docker={open.d}
          canAct={!isViewer && open.d.access}
          initialSec={linkSec.id === open.s.id ? linkSec.sec : null}
          onClose={() => {
            setOpenId(null)
            setLinkSec({ id: null, sec: null })
          }}
          onChanged={load}
        />
      )}
    </div>
  )
}
