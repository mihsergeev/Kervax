import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  kubeCommand,
  kubeCommandStatus,
  listServers,
  podFinished,
  type KubeCommand,
  type KubeInfo,
  type KubePod,
  type KubeWorkload,
  type Server,
} from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'

// Вкладка «Кубер»: КОМПАКТНЫЙ список хостов с кластером; клик по хосту → модалка
// с нодами, воркоадами и подами (статусы, рестарты, логи, управление). Агент читает
// kube-api по токену УЗКОГО ServiceAccount (kube-setup.sh) — read + точечный write
// (rollout restart / delete pod), не cluster-admin. Логи не хранятся — разовый tail.

// команда для включения доступа (kube-setup.sh на control-plane ноде): создаёт
// ServiceAccount kervax-agent с узким RBAC (read + rollout restart + delete pod),
// кладёт токен в /etc/kervax/kube.json (root:kervax), перезапускает агент.
const KUBE_ENABLE_CMD = `curl -fsSL ${window.location.origin}/api/agent/kube-setup.sh | sudo bash
sudo systemctl restart kervax-agent`

function podTone(p: KubePod): string {
  // завершённые Job/CronJob-поды — история, не живой воркоад: Succeeded=ок,
  // Failed(job)=приглушённо (нейтрально), не красный «упал».
  if (podFinished(p)) return p.phase === 'Succeeded' ? 't-up' : 't-muted'
  const bad = ['CrashLoopBackOff', 'Error', 'OOMKilled', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerError']
  if (p.phase === 'Failed' || (p.reason && bad.includes(p.reason))) return 't-down'
  if (p.phase === 'Running' && p.ready) return 't-up'
  return 't-degraded'
}
function workloadTone(w: KubeWorkload): string {
  if (w.desired === 0) return 't-degraded'
  if (w.ready >= w.desired) return 't-up'
  if (w.ready === 0) return 't-down'
  return 't-degraded'
}
// Завершённые Job/CronJob-поды (Succeeded/Failed) — история, НЕ считаем «должны бежать».
// active = поды, которые должны работать; running = из них реально Running+ready.
function podCounts(pods: KubePod[]): { running: number; active: number } {
  const active = pods.filter((p) => !podFinished(p))
  const running = active.filter((p) => p.phase === 'Running' && p.ready).length
  return { running, active: active.length }
}

type KAction = 'rollout_restart' | 'delete_pod' | 'logs'

async function runAndWait(
  serverId: number,
  ns: string,
  kind: 'deployment' | 'statefulset' | 'daemonset' | 'pod',
  name: string,
  action: KAction,
  opts: { tail?: number; since?: number } = {},
): Promise<KubeCommand> {
  const c = await kubeCommand(serverId, ns, kind, name, action, opts)
  let last = c
  const maxTries = opts.since ? 200 : 100
  for (let i = 0; i < maxTries && last.status !== 'done' && last.status !== 'error'; i++) {
    await new Promise((r) => setTimeout(r, 300))
    last = await kubeCommandStatus(serverId, c.id)
  }
  return last
}

// Логотип Kubernetes: синий семиугольник + белый штурвал. Двухцветный — чтобы
// штурвал читался на маленьком размере (моно-силуэт из simple-icons сливался в пятно).
function KubeLogo() {
  return (
    <svg className="docker-logo" viewBox="0 0 128 128" aria-hidden>
      <path
        fill="#326CE5"
        d="M64 4 L112.5 26.4 L124.5 78.6 L91.2 120.5 L36.8 120.5 L3.5 78.6 L15.5 26.4 Z"
      />
      <g fill="none" stroke="#fff" strokeWidth="6" strokeLinejoin="round" strokeLinecap="round">
        <polygon points="64,34 87.5,45.3 93.3,70.7 77,91 51,91 34.7,70.7 40.5,45.3" />
        <path d="M64 64 L64 34 M64 64 L87.5 45.3 M64 64 L93.3 70.7 M64 64 L77 91 M64 64 L51 91 M64 64 L34.7 70.7 M64 64 L40.5 45.3" />
      </g>
      <circle cx="64" cy="64" r="12" fill="#326CE5" stroke="#fff" strokeWidth="6" />
    </svg>
  )
}

function EnableBlock() {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <div className="docker-noaccess">
      <div className="muted small">
        {t('Кластер найден, но агент не имеет доступа к kube-api. Включите read-only + точечное управление: скрипт создаёт ServiceAccount с узким RBAC (не cluster-admin) и кладёт токен для агента.')}
      </div>
      <div className="agent-advice-cmd">
        <pre>{KUBE_ENABLE_CMD}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(KUBE_ENABLE_CMD)
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

type LogRange = 'tail' | 'hour' | 'day'

// Логи пода (on-demand tail через агент). Аналог docker-логов.
function PodLogsModal({
  server,
  pod,
  onClose,
}: {
  server: Server
  pod: KubePod
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
        const res = await runAndWait(server.id, pod.ns, 'pod', pod.name, 'logs', opts)
        if (res.status === 'done') setLogs(res.result ?? '')
        else setErr(res.result || t('не удалось получить логи'))
      } catch {
        setErr(t('ошибка'))
      } finally {
        setLoading(false)
      }
    },
    [server.id, pod.ns, pod.name, t],
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
    a.download = `${pod.name}_${stamp}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }
  const ranges: { k: LogRange; l: string }[] = [
    { k: 'tail', l: t('400 строк') },
    { k: 'hour', l: t('за час') },
    { k: 'day', l: t('за день') },
  ]
  const VIEW_MAX = 500_000
  const truncated = !!logs && logs.length > VIEW_MAX
  const view = truncated ? logs!.slice(-VIEW_MAX) : logs
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal logs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3 className="mono">
            {pod.name} <span className="muted small">· {pod.ns} · <CountryFlag code={server.country} /> {server.name}</span>
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

function WorkloadRow({
  serverId,
  w,
  canAct,
  onChanged,
}: {
  serverId: number
  w: KubeWorkload
  canAct: boolean
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const kind = w.kind.toLowerCase() as 'deployment' | 'statefulset' | 'daemonset'
  const restart = async () => {
    if (!window.confirm(t('Rollout restart «{n}» ({k})?', { n: w.name, k: w.kind }))) return
    setBusy(true)
    setErr(null)
    try {
      const res = await runAndWait(serverId, w.ns, kind, w.name, 'rollout_restart')
      if (res.status !== 'done') setErr(res.result || t('не удалось'))
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res docker-row ${workloadTone(w)}`}>
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          <span className="type-chip">{w.kind}</span> {w.name}
          <span className="muted small"> · {w.ns}</span>
        </div>
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div className={`docker-c-status mono small ${workloadTone(w)}`}>{w.ready}/{w.desired}</div>
      <div className="docker-actions">
        {canAct && (
          <button className="ghost icon-btn" disabled={busy} onClick={restart} title={t('Rollout restart')}>
            {busy ? '…' : '⟳'}
          </button>
        )}
      </div>
    </div>
  )
}

function PodRow({
  serverId,
  p,
  canAct,
  onLogs,
  onChanged,
}: {
  serverId: number
  p: KubePod
  canAct: boolean
  onLogs: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const done = podFinished(p)
  const act = async () => {
    const q = done
      ? t('Удалить завершённый под «{n}» (очистить историю)?', { n: p.name })
      : t('Перезапустить под «{n}» (удалить — контроллер пересоздаст)?', { n: p.name })
    if (!window.confirm(q)) return
    setBusy(true)
    setErr(null)
    try {
      const res = await runAndWait(serverId, p.ns, 'pod', p.name, 'delete_pod')
      if (res.status !== 'done') setErr(res.result || t('не удалось'))
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res docker-row ${podTone(p)}`}>
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          {p.name}
          <span className="muted small"> · {p.ns}</span>
          {done ? (
            <span
              className="type-chip"
              title={t('Под Job/CronJob, уже отработал ({phase}). Это историческая запись, а не живой воркоад — чинить нечего; удалите, чтобы убрать из списка.', { phase: p.phase })}
            >
              {p.phase === 'Succeeded' ? t('отработал') : t('Job — упал разово')}
            </span>
          ) : (
            p.reason && <span className="type-chip off">{p.reason}</span>
          )}
          {!!p.restarts && p.restarts > 0 && (
            <span
              className="type-chip"
              title={t('перезапусков за всё время: {n} (само по себе не проблема; тревога — только CrashLoopBackOff)', { n: p.restarts })}
            >
              ⟳ {p.restarts}
            </span>
          )}
        </div>
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div className={`docker-c-status mono small ${podTone(p)}`}>{p.phase}</div>
      <div className="docker-actions">
        <button className="ghost icon-btn" onClick={onLogs} title={t('Логи')}>
          📄
        </button>
        {canAct && (
          <button className="ghost icon-btn" disabled={busy} onClick={act} title={done ? t('Удалить под (очистить)') : t('Перезапустить под')}>
            {busy ? '…' : done ? '🗑' : '⟳'}
          </button>
        )}
      </div>
    </div>
  )
}

// Модалка хоста-кластера: ноды + воркоады + поды. Логи пода — вложенная модалка.
function KubeHostModal({
  server: s,
  kube,
  canAct,
  onClose,
  onChanged,
}: {
  server: Server
  kube: KubeInfo
  canAct: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [logsPod, setLogsPod] = useState<KubePod | null>(null)
  const [tab, setTab] = useState<'pods' | 'finished' | 'workloads' | 'nodes'>('pods')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'state' | 'name' | 'restarts' | 'ns'>('state')
  const nodes = kube.nodes ?? []
  const workloads = kube.workloads ?? []
  const pods = kube.pods ?? []
  const ql = q.trim().toLowerCase()
  const podRank = (p: KubePod) => ({ 't-down': 0, 't-degraded': 1, 't-up': 2 })[podTone(p)] ?? 3
  // при равенстве сохраняем исходный порядок (стабильная сортировка), кроме ns —
  // там логично добить именем внутри namespace.
  const pcmp: Record<string, (a: KubePod, b: KubePod) => number> = {
    state: (a, b) => podRank(a) - podRank(b),
    name: (a, b) => a.name.localeCompare(b.name),
    restarts: (a, b) => (b.restarts ?? 0) - (a.restarts ?? 0),
    ns: (a, b) => (a.ns || '').localeCompare(b.ns || '') || a.name.localeCompare(b.name),
  }
  const doneCount = pods.filter(podFinished).length
  // вкладка «Завершённые» есть только пока они есть; если последний завершённый пропал
  // (удалили/сборщик подчистил) — не залипаем на исчезнувшей вкладке
  const curTab = tab === 'finished' && doneCount === 0 ? 'pods' : tab
  // «Поды» = только живые; завершённые (история Job/CronJob) — своя вкладка, чтобы не
  // подмешивались к тому, что надо чинить
  const fpods = pods
    .filter((p) => (curTab === 'finished' ? podFinished(p) : !podFinished(p)))
    .filter((p) => !ql || `${p.name} ${p.ns} ${p.reason ?? ''}`.toLowerCase().includes(ql))
    .slice()
    .sort(pcmp[sort])
  const fworkloads = workloads.filter((w) => !ql || `${w.name} ${w.ns} ${w.kind}`.toLowerCase().includes(ql))
  const nodesReady = nodes.filter((n) => n.ready).length
  const { running: podsRunning, active: podsActive } = podCounts(pods)
  const tabs: { k: 'pods' | 'finished' | 'workloads' | 'nodes'; l: string; title?: string }[] = [
    { k: 'pods', l: t('Поды ({n})', { n: podsActive }) },
    ...(doneCount > 0
      ? [{
          k: 'finished' as const,
          l: t('Завершённые ({n})', { n: doneCount }),
          title: t('Завершённые Job/CronJob-поды — историческая запись, а не живой воркоад. Их не нужно чинить; удалите под, чтобы убрать из списка.'),
        }]
      : []),
    { k: 'workloads', l: t('Воркоады ({n})', { n: workloads.length }) },
    { k: 'nodes', l: t('Ноды ({n})', { n: nodes.length }) },
  ]
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
          <span className="type-chip">{kube.flavor || 'k8s'} {kube.version || '—'}</span>
          {kube.access && (
            <span className="muted small">
              {t('ноды {nr}/{nn} · поды {pr}/{pn} · ns {ns}', {
                nr: nodesReady, nn: nodes.length, pr: podsRunning, pn: podsActive, ns: kube.namespaces ?? 0,
              })}
            </span>
          )}
        </div>
        {!kube.access ? (
          <EnableBlock />
        ) : (
          <>
            <div className="win-switch kube-tabs">
              {tabs.map(({ k, l, title }) => (
                <button
                  key={k}
                  className={`win-btn${curTab === k ? ' win-btn-active' : ''}`}
                  onClick={() => setTab(k)}
                  title={title}
                >
                  {l}
                </button>
              ))}
            </div>
            {curTab !== 'nodes' && (
              <div className="modal-toolbar">
                <input
                  className="checks-search modal-search"
                  placeholder={t('Фильтр: имя, namespace…')}
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
                {(curTab === 'pods' || curTab === 'finished') && (
                  <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
                    <option value="state">{t('сорт: статус')}</option>
                    <option value="name">{t('сорт: имя')}</option>
                    <option value="restarts">{t('сорт: рестарты')}</option>
                    <option value="ns">{t('сорт: namespace')}</option>
                  </select>
                )}
              </div>
            )}
            <div className="loc-results docker-clist docker-clist-scroll">
              {(curTab === 'pods' || curTab === 'finished') &&
                (pods.length === 0 ? (
                  <div className="muted small">{t('Подов нет')}</div>
                ) : fpods.length === 0 ? (
                  <div className="muted small">{t('Ничего не найдено.')}</div>
                ) : (
                  fpods.map((p) => (
                    <PodRow
                      key={p.ns + '/' + p.name}
                      serverId={s.id}
                      p={p}
                      canAct={canAct}
                      onLogs={() => setLogsPod(p)}
                      onChanged={onChanged}
                    />
                  ))
                ))}
              {curTab === 'workloads' &&
                (workloads.length === 0 ? (
                  <div className="muted small">{t('Воркоадов нет')}</div>
                ) : fworkloads.length === 0 ? (
                  <div className="muted small">{t('Ничего не найдено.')}</div>
                ) : (
                  fworkloads.map((w) => (
                    <WorkloadRow
                      key={w.kind + '/' + w.ns + '/' + w.name}
                      serverId={s.id}
                      w={w}
                      canAct={canAct}
                      onChanged={onChanged}
                    />
                  ))
                ))}
              {curTab === 'nodes' &&
                nodes.map((n) => (
                  <div className={`loc-res docker-row ${n.ready ? 't-up' : 't-down'}`} key={n.name}>
                    <div className="docker-c-main">
                      <div className="docker-c-name mono">
                        {n.name}
                        {n.roles && <span className="type-chip">{n.roles}</span>}
                      </div>
                      <div className="docker-c-img mono muted small">
                        {n.version || ''}{n.ip ? ` · ${n.ip}` : ''}
                      </div>
                    </div>
                    <div className={`docker-c-status mono small ${n.ready ? 't-up' : 't-down'}`}>
                      {n.ready ? 'Ready' : 'NotReady'}
                    </div>
                  </div>
                ))}
            </div>
          </>
        )}
      </div>
      {logsPod && <PodLogsModal server={s} pod={logsPod} onClose={() => setLogsPod(null)} />}
    </div>,
    document.body,
  )
}

function HostRow({
  server: s,
  kube,
  showGroup,
  onOpen,
}: {
  server: Server
  kube: KubeInfo
  showGroup?: boolean
  onOpen: () => void
}) {
  const { t } = useI18n()
  const nodes = kube.nodes ?? []
  const pods = kube.pods ?? []
  const nodesReady = nodes.filter((n) => n.ready).length
  const { running, active } = podCounts(pods)
  return (
    <button className="check-row srv-row docker-host-row" onClick={onOpen}>
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div className="check-main">
        <div className="check-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {!kube.access && <span className="type-chip off">{t('нет доступа')}</span>}
        </div>
        <div className="check-target mono muted small">
          {kube.flavor || 'k8s'} {kube.version || '—'}
          {kube.access ? ` · ${t('ноды {r}/{n}', { r: nodesReady, n: nodes.length })}` : ''}
        </div>
      </div>
      <div className="docker-host-count mono">
        {kube.access ? (
          <span className={running < active ? 't-degraded' : 't-up'}>
            {t('{r}/{n}', { r: running, n: active })}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
    </button>
  )
}

type KHost = { s: Server; d: KubeInfo }

export function KuberPage({
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
  useEffect(() => {
    if (openHostId != null) {
      setOpenId(openHostId)
      onConsumed?.()
    }
  }, [openHostId, onConsumed])
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_kube_groupby') as 'none' | 'group') || 'group',
  )
  const setGrouping = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_kube_groupby', g)
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
    .map((s) => ({ s, d: s.last_report?.kube }))
    .filter((x): x is KHost => !!x.d?.present)
  const q = query.trim().toLowerCase()
  const hosts = allHosts
    .filter(({ s, d }) => {
      if (!q) return true
      const hay = [
        s.name,
        s.group_name,
        `${d.flavor ?? ''} ${d.version ?? ''}`,
        ...(d.nodes ?? []).map((n) => n.name),
        ...(d.pods ?? []).flatMap((p) => [p.name, p.ns]),
        ...(d.workloads ?? []).map((w) => w.name),
      ]
        .join(' ')
        .toLowerCase()
      return hay.includes(q)
    })
    .sort((a, b) => a.s.name.localeCompare(b.s.name))

  const groups: { key: string; label: string; items: KHost[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, KHost[]>()
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
          <KubeLogo /> Kubernetes
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
            <span className="muted small">{t('{n} кластеров', { n: allHosts.length })}</span>
          )}
        </div>
      </div>
      {allHosts.length > 1 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: хост, группа, нода, под, воркоад…')}
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
          {t('Kubernetes не найден ни на одном сервере. Агент определяет кластер сам (k0s/k3s/microk8s/kubeadm); если он есть, но раздел пуст — обновите агент.')}
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
                kube={d}
                showGroup={groupBy !== 'group'}
                onOpen={() => setOpenId(s.id)}
              />
            ))}
          </div>
        </div>
      ))}
      {open && (
        <KubeHostModal
          server={open.s}
          kube={open.d}
          canAct={!isViewer && open.d.access}
          onClose={() => setOpenId(null)}
          onChanged={load}
        />
      )}
    </div>
  )
}
