import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  dockerCommand,
  dockerCommandStatus,
  listServers,
  type DockerCommand,
  type DockerContainer,
  type DockerInfo,
  type Server,
} from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'

// Вкладка «Docker»: КОМПАКТНЫЙ список хостов с докером; клик по хосту → модалка
// с его контейнерами (версии, статусы, кнопки, логи). Так масштабируется на 100+
// серверов. Действия/логи — on-demand через bounded proxy (агент забирает команду
// быстрым опросом, исполняет, постит результат). Логи не хранятся — разовый tail.

function dockerTone(state: string): string {
  if (state === 'running') return 't-up'
  if (state === 'exited' || state === 'dead') return 't-down'
  return 't-degraded'
}

const DOCKER_ENABLE_CMD = `DGID=$(getent group docker | cut -d: -f3)
sudo docker rm -f kervax-docker-proxy 2>/dev/null
sudo docker run -d --name kervax-docker-proxy --restart unless-stopped \\
  --user 65534:$DGID -v /var/run/docker.sock:/var/run/docker.sock:ro \\
  -p 127.0.0.1:2375:2375 wollomatic/socket-proxy:1 \\
  -loglevel warn -listenip 0.0.0.0 -allowfrom 0.0.0.0/0 -shutdowngracetime 1 \\
  -allowGET '^/(v[0-9.]+/)?(version|info|_ping|containers/json|containers/[a-zA-Z0-9_.-]+/(json|logs))' \\
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

function EnableBlock() {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <div className="docker-noaccess">
      <div className="muted small">
        {t('Docker установлен (версии видны). Список контейнеров скрыт — включите read-only доступ (docker-socket-proxy: только просмотр + restart, без exec/root). Новым нодам — флаг --docker при установке.')}
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
}: {
  serverId: number
  c: DockerContainer
  problem?: string // 'down' | 'loop' — по контейнеру уже отправлен алерт
  canAct: boolean
  onLogs: () => void
  onChanged: () => void
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
      if (res.status !== 'done') setErr(res.result || t('не удалось'))
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(null)
    }
  }
  const running = c.state === 'running'
  // problem приходит из alert_state бэкенда: подсвечиваем ровно те контейнеры, про
  // которые панель уже написала в телеграм. Крашащийся контейнер обычно в состоянии
  // running (он же перезапускается), поэтому по одному c.state он выглядел зелёным.
  return (
    <div className={`loc-res docker-row ${dockerTone(c.state)}${problem ? ` docker-row-${problem}` : ''}`}>
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
          {!!c.restarts && c.restarts > 0 && (
            <span
              className="type-chip"
              title={t('перезапусков за всё время (RestartCount): {n} (само по себе не проблема; тревога — на устойчивый crash-loop)', { n: c.restarts })}
            >
              ⟳ {c.restarts}
            </span>
          )}
        </div>
        <div className="docker-c-img mono muted small" title={c.image}>
          {c.image}
        </div>
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div className={`docker-c-status mono small ${dockerTone(c.state)}`}>{c.status}</div>
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

// Модалка хоста: контейнеры + действия. Логи — вложенная модалка сверху.
function DockerHostModal({
  server: s,
  docker,
  canAct,
  onClose,
  onChanged,
}: {
  server: Server
  docker: DockerInfo
  canAct: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [logs, setLogs] = useState<string | null>(null)
  const [hideStopped, setHideStopped] = useState(false)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'state' | 'name' | 'restarts'>('state')
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
  const shown = cs
    .filter((c) => !hideStopped || c.state === 'running')
    .filter((c) => !ql || `${c.name} ${c.image} ${c.status}`.toLowerCase().includes(ql))
    .slice()
    .sort((a, b) => byProblem(a, b) || cmp[sort](a, b))
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />{' '}
            <OsIcon os={s.os} /> <CountryFlag code={s.country} /> {s.name}
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="docker-host-head">
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          <span className="type-chip">Docker {docker.version || '—'}</span>
          {docker.compose && <span className="type-chip">compose {docker.compose}</span>}
          {docker.access && cs.length > 0 && (
            <span className="muted small">{t('{r} из {n} запущено', { r: running, n: cs.length })}</span>
          )}
          {docker.access && stopped > 0 && (
            <button className="ghost small docker-hide-stopped" onClick={() => setHideStopped((v) => !v)}>
              {hideStopped ? t('Показать остановленные ({n})', { n: stopped }) : t('Скрыть остановленные ({n})', { n: stopped })}
            </button>
          )}
        </div>
        {docker.access && cs.length > 5 && (
          <div className="modal-toolbar">
            <input
              className="checks-search modal-search"
              placeholder={t('Фильтр: имя, образ…')}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
              <option value="state">{t('сорт: статус')}</option>
              <option value="name">{t('сорт: имя')}</option>
              <option value="restarts">{t('сорт: рестарты')}</option>
            </select>
          </div>
        )}
        {!docker.access ? (
          <EnableBlock />
        ) : cs.length === 0 ? (
          <div className="muted small">{t('Контейнеров нет')}</div>
        ) : shown.length === 0 ? (
          <div className="muted small">{t('Ничего не найдено.')}</div>
        ) : (
          <div className="loc-results docker-clist docker-clist-scroll">
            {shown.map((c) => (
              <ContainerRow
                key={c.name}
                serverId={s.id}
                c={c}
                problem={probs[c.name]}
                canAct={canAct}
                onLogs={() => setLogs(c.name)}
                onChanged={onChanged}
              />
            ))}
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

function HostRow({
  server: s,
  docker,
  showGroup,
  onOpen,
}: {
  server: Server
  docker: DockerInfo
  showGroup?: boolean
  onOpen: () => void
}) {
  const { t } = useI18n()
  const cs = docker.containers ?? []
  const running = cs.filter((c) => c.state === 'running').length
  return (
    <button className="check-row srv-row docker-host-row" onClick={onOpen}>
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div className="check-main">
        <div className="check-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {!docker.access && <span className="type-chip off">{t('нет доступа')}</span>}
        </div>
        <div className="check-target mono muted small">
          Docker {docker.version || '—'}
          {docker.compose ? ` · compose ${docker.compose}` : ''}
        </div>
      </div>
      <div className="docker-host-count mono">
        {docker.access ? (
          <span className={running < cs.length ? 't-degraded' : 't-up'}>
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

export function DockerPage({
  onUnauthorized,
  openHostId = null,
  onConsumed,
}: {
  onUnauthorized: () => void
  openHostId?: number | null
  onConsumed?: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openId, setOpenId] = useState<number | null>(openHostId)
  // диплинк с главной: открыть конкретный хост и «съесть» id (чтобы не переоткрывать)
  useEffect(() => {
    if (openHostId != null) {
      setOpenId(openHostId)
      onConsumed?.()
    }
  }, [openHostId, onConsumed])
  const [query, setQuery] = useState('')
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
  const totalRunning = allHosts.reduce(
    (n, { d }) => n + (d.containers?.filter((c) => c.state === 'running').length ?? 0),
    0,
  )
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
    .sort((a, b) => a.s.name.localeCompare(b.s.name))

  const groups: { key: string; label: string; items: DHost[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, DHost[]>()
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
          {allHosts.length > 0 && (
            <span className="muted small">
              {t('{h} хостов · {c} контейнеров запущено', { h: allHosts.length, c: totalRunning })}
            </span>
          )}
        </div>
      </div>
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
      {err && <p className="form-error">{err}</p>}
      {servers && allHosts.length === 0 && (
        <div className="card muted">
          {t('Docker не найден ни на одном сервере. Агент определяет его сам; если docker есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      {servers && allHosts.length > 0 && hosts.length === 0 && (
        <div className="card muted">{t('Ничего не найдено.')}</div>
      )}
      {groups.map((g) => (
        <div key={g.key} className="mon-group">
          {groupBy !== 'none' && (
            <div className="mon-group-head docker-group-head">
              <span className="mon-group-name">{g.label}</span>
              <span className="mon-group-n">{g.items.length}</span>
            </div>
          )}
          <div className="check-list">
            {g.items.map(({ s, d }) => (
              <HostRow
                key={s.id}
                server={s}
                docker={d}
                showGroup={groupBy !== 'group'}
                onOpen={() => setOpenId(s.id)}
              />
            ))}
          </div>
        </div>
      ))}
      {open && (
        <DockerHostModal
          server={open.s}
          docker={open.d}
          canAct={!isViewer && open.d.access}
          onClose={() => setOpenId(null)}
          onChanged={load}
        />
      )}
    </div>
  )
}
