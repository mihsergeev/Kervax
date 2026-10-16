import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError, getFleetVulns, type FleetVulnServer, type FleetVulns, type VulnCve, type VulnFinding } from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import type { Section } from './App'
import { VulnCves, VulnFix, VulnRow, VulnsNote } from './VulnsCard'

// Уязвимости по всему парку (backend api/vulns.py). Раскладка как у карточки сервера: на сводке
// что обновить и где, у каждого пакета свой раздел (ядро - один на всех) с серверами и тем, чем
// закрывается, у каждого сервера с уязвимостями свой раздел, отдельно чистые и непроверенные.
// Только справка, без алертов

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  // sub - раздел карточки сервера (vulns)
  onOpen: (section: Section, id: number, sub?: string) => void
}

// сколько серверов перечислять в строке пакета на сводке, остальное - "и еще N"
const WHERE = 4

type Hit = { s: FleetVulnServer; f: VulnFinding }
// исходный пакет по всему парку: на каких серверах и какие CVE (без повторов, эксплуатируемые первыми)
type Pkg = { key: string; src: string; kernel: boolean; hits: Hit[]; cves: VulnCve[]; kev: number; crit: number }

const isKev = (f: VulnFinding) => f.cves.some((c) => c.kev)
const byName = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true })

function packages(servers: FleetVulnServer[]): Pkg[] {
  const by = new Map<string, Pkg>()
  for (const s of servers) {
    for (const f of s.found) {
      const key = f.kernel ? 'kernel' : f.src
      let p = by.get(key)
      if (!p) {
        p = { key, src: f.src, kernel: f.kernel, hits: [], cves: [], kev: 0, crit: 0 }
        by.set(key, p)
      }
      p.hits.push({ s, f })
    }
  }
  for (const p of by.values()) {
    const m = new Map<string, VulnCve>()
    for (const h of p.hits) for (const c of h.f.cves) if (!m.has(c.id)) m.set(c.id, c)
    p.cves = [...m.values()].sort((a, b) => Number(b.kev) - Number(a.kev) || b.cvss - a.cvss || byName(a.id, b.id))
    p.kev = p.cves.filter((c) => c.kev).length
    p.crit = p.cves.filter((c) => c.prio === 'critical').length
    p.hits.sort((a, b) => Number(isKev(b.f)) - Number(isKev(a.f)) || byName(a.s.name, b.s.name))
  }
  return [...by.values()].sort(
    (a, b) => Number(b.kev > 0) - Number(a.kev > 0) || b.hits.length - a.hits.length || byName(a.src, b.src),
  )
}

export function VulnsModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [data, setData] = useState<FleetVulns | null>(null)
  const [err, setErr] = useState('')
  const [sec, setSec] = useState('over')
  const [q, setQ] = useState('')
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    getFleetVulns()
      .then((r) => alive && setData(r))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (alive) setErr(String((e as Error).message || e))
      })
    return () => {
      alive = false
    }
  }, [onUnauthorized])

  const groups = useMemo(() => {
    const vuln: FleetVulnServer[] = []
    const clean: FleetVulnServer[] = []
    const unknown: FleetVulnServer[] = []
    for (const s of data?.servers ?? []) {
      if (!s.release || !s.feed_ts || !s.pkgs) unknown.push(s)
      else if (s.found.length) vuln.push(s)
      else clean.push(s)
    }
    return { vuln, clean, unknown, pkgs: packages(vuln) }
  }, [data])
  const { vuln, clean, unknown, pkgs } = groups

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }

  const pkgLabel = (p: Pkg) => (p.kernel ? t('Ядро') : p.src)
  const why = (s: FleetVulnServer) =>
    !s.release
      ? t('не Ubuntu')
      : !s.pkgs
        ? t('список пакетов еще не пришел')
        : s.feed_err
          ? t('база уязвимостей не скачалась: {err}', { err: s.feed_err })
          : t('база уязвимостей еще не скачана')

  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...pkgs.map((p) => ({ id: `p:${p.key}`, label: p.kernel ? 'Ядро' : p.src, group: 'Что обновить' })),
    ...vuln.map((s) => ({ id: `s${s.id}`, label: s.name, group: 'Серверы' })),
    ...(clean.length ? [{ id: 'clean', label: 'Без важных', group: 'Остальные' }] : []),
    ...(unknown.length ? [{ id: 'unknown', label: 'Не проверены', group: 'Остальные' }] : []),
  ]
  const HEAD: Record<string, string> = { clean: 'Без важных уязвимостей', unknown: 'Не проверены' }
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  const curPkg = cur.id.startsWith('p:') ? pkgs.find((p) => `p:${p.key}` === cur.id) : undefined
  const curSrv = /^s\d+$/.test(cur.id) ? vuln.find((s) => `s${s.id}` === cur.id) : undefined

  // в меню: у пакета - на скольких серверах, у сервера - сколько обновить; красным, если эксплуатируется
  const info: Record<string, SecInfo> = {}
  for (const p of pkgs) info[`p:${p.key}`] = { level: p.kev ? 2 : 1, n: p.hits.length, line: '' }
  for (const s of vuln) info[`s${s.id}`] = { level: s.summary.kev ? 2 : 1, n: s.found.length, line: '' }
  const side: Record<string, string> = { clean: String(clean.length), unknown: String(unknown.length) }

  const toSrv = (s: FleetVulnServer) => (s.found.length && s.release && s.feed_ts ? go(`s${s.id}`) : onOpen('servers', s.id, 'vulns'))
  const srvLink = (s: FleetVulnServer) => (
    <span className="ans-node">
      <span className={`lk-dot ${s.summary.kev ? 'lk-dot-bad' : s.found.length ? 'lk-dot-warn' : s.release && s.feed_ts && s.pkgs ? 'lk-dot-ok' : ''}`} />
      <button className="linklike" onClick={() => toSrv(s)}>
        {s.name}
      </button>
    </span>
  )
  const pkgLink = (p: Pkg) => (
    <button className="linklike" onClick={() => go(`p:${p.key}`)}>
      {pkgLabel(p)}
    </button>
  )
  const tags = (kev: number, crit: number) => (
    <>
      {kev > 0 && <span className="vuln-tag vuln-tag-kev">{t('эксплуатируется: {n}', { n: kev })}</span>}
      {crit > 0 && <span className="vuln-tag">{t('critical: {n}', { n: crit })}</span>}
    </>
  )
  const short = (os: string) => os.replace(/^Ubuntu /, '').replace(/\.\d+ LTS$/, ' LTS')

  // плитка ведет в раздел, когда он есть
  const tile = (id: string | undefined, el: ReactNode) =>
    id && SECS.some((x) => x.id === id) ? (
      <button className="srv-tile-btn" onClick={() => go(id)} title={t('к разделу')}>
        {el}
      </button>
    ) : (
      el
    )

  // "Базы Canonical от 14.10.2026: 20.04, 22.04" - дата одна на все, если совпадает
  const feedLine = (feeds: FleetVulns['feeds'], ver: Record<string, string>) => {
    const day = (f: FleetVulns['feeds'][number]) => {
      const d = f.gen || f.ts
      return d ? new Date(d).toLocaleDateString() : '-'
    }
    const days = new Set(feeds.map(day))
    const rel = (f: FleetVulns['feeds'][number]) => ver[f.release] ?? f.release
    return days.size === 1
      ? t('Базы Canonical от {d}: {list}', { d: day(feeds[0]), list: feeds.map(rel).join(', ') })
      : t('Базы Canonical: {list}', { list: feeds.map((f) => `${rel(f)} ${t('от {d}', { d: day(f) })}`).join(', ') })
  }

  const overview = () => {
    const checked = vuln.length + clean.length
    const kevSrv = vuln.filter((s) => s.summary.kev > 0).length
    const kern = pkgs.find((p) => p.kernel)
    const rest = pkgs.filter((p) => !p.kernel)
    const eol = vuln.concat(clean).filter((s) => s.eol)
    const anyKev = pkgs.some((p) => p.kev > 0)
    // версия релиза для подписи базы: focal -> 20.04
    const ver: Record<string, string> = {}
    for (const s of data?.servers ?? []) {
      const m = /(\d+\.\d+)/.exec(s.os)
      if (s.release && m && !ver[s.release]) ver[s.release] = m[1]
    }
    const feedErr = (data?.feeds ?? []).filter((f) => f.err)
    return (
      <>
        <p className="muted small al-lead">
          {t('Только critical и уже эксплуатируемые (CISA KEV), которые закрываются обновлением: средние и мелкие не показываются. Пакеты сверяются с базой Canonical, ядро - работающее, а не установленное.')}
        </p>
        {data && data.feeds.length === 0 && (
          <p className="muted small">
            {t('Базы уязвимостей еще не скачаны: панель скачивает их раз в сутки, первый раз - в течение 10 минут.')}
          </p>
        )}
        {feedErr.length > 0 && (
          <div className="al-note al-note-warn">
            <span>
              {feedErr.map((f) => t('База {rel} не скачалась: {err}', { rel: ver[f.release] ?? f.release, err: f.err })).join('; ')}
            </span>
          </div>
        )}
        <div className="net-tiles srv-tiles">
          <NetTile
            label={t('С важными уязвимостями')}
            value={String(vuln.length)}
            sub={
              kevSrv
                ? t('из проверенных {n}, с эксплуатируемыми: {k}', { n: checked, k: kevSrv })
                : t('из проверенных {n}', { n: checked })
            }
            tone={kevSrv ? 'bad' : vuln.length ? 'warn' : checked ? 'ok' : undefined}
          />
          {kern &&
            tile(
              'p:kernel',
              <NetTile
                label={t('Ждут нового ядра')}
                value={String(kern.hits.length)}
                sub={t('обновить и перезагрузить')}
                tone={kern.kev ? 'bad' : 'warn'}
              />,
            )}
          {rest.length > 0 &&
            tile(
              rest.length === 1 ? `p:${rest[0].key}` : undefined,
              <NetTile
                label={t('Пакеты обновить')}
                value={String(rest.length)}
                sub={t('на серверах: {n}', { n: new Set(rest.flatMap((p) => p.hits.map((h) => h.s.id))).size })}
                tone={rest.some((p) => p.kev) ? 'bad' : 'warn'}
              />,
            )}
          {eol.length > 0 && (
            <NetTile
              label={t('Без обычной поддержки')}
              value={String(eol.length)}
              sub={t('{os}: исправления только в Ubuntu Pro', { os: [...new Set(eol.map((s) => short(s.os)))].join(', ') })}
            />
          )}
        </div>
        {pkgs.length > 0 && (
          <>
            <div className="srv-nav-grp rt-cap">{t('Что обновить')}</div>
            <table className="sec-tbl">
              <thead>
                <tr>
                  <th>{t('Пакет')}</th>
                  <th className="hide-m">{t('Где')}</th>
                  <th className="num">{t('Серверов')}</th>
                  <th className="num">CVE</th>
                  {anyKev && (
                    <th className="num" title={t('Эксплуатируется')}>
                    <span className="hide-m">{t('Эксплуатируется')}</span>
                    <span className="only-m">{t('эксп.')}</span>
                  </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {pkgs.map((p) => (
                  <tr key={p.key}>
                    <td>{pkgLink(p)}</td>
                    <td className="small hide-m">
                      <span className="muted">
                        {p.hits
                          .slice(0, WHERE)
                          .map((h) => h.s.name)
                          .join(', ')}
                        {p.hits.length > WHERE && ` ${t('и еще {n}', { n: p.hits.length - WHERE })}`}
                      </span>
                    </td>
                    <td className="num">{p.hits.length}</td>
                    <td className="num">{p.cves.length}</td>
                    {anyKev && <td className="num t-down">{p.kev || ''}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {data && data.feeds.length > 0 && <p className="muted small vu-feeds">{feedLine(data.feeds, ver)}</p>}
      </>
    )
  }

  // Поиск на сводке: сервер, пакет или CVE - где это и что там обновить
  const needle = q.trim().toLowerCase()
  const found = () => {
    const hitF = (f: VulnFinding) =>
      f.src.toLowerCase().includes(needle) ||
      f.pkgs.some((p) => p[0].toLowerCase().includes(needle)) ||
      f.cves.some((c) => c.id.toLowerCase().includes(needle))
    const rows = (data?.servers ?? [])
      .map((s) => ({ s, fs: s.name.toLowerCase().includes(needle) ? s.found : s.found.filter(hitF) }))
      .filter((r) => r.fs.length || r.s.name.toLowerCase().includes(needle))
    if (!rows.length) return <p className="muted small">{t('Ничего не нашлось.')}</p>
    return (
      <table className="sec-tbl">
        <tbody>
          {rows.map(({ s, fs }) => (
            <tr key={s.id}>
              <td>{srvLink(s)}</td>
              <td className="small">
                {fs.length ? (
                  fs.map((f, i) => {
                    const p = pkgs.find((x) => x.key === (f.kernel ? 'kernel' : f.src))
                    return (
                      <span key={f.src + i}>
                        {i > 0 && ', '}
                        {p ? pkgLink(p) : f.src}
                        <span className={isKev(f) ? 't-down' : 'muted'}> {f.cves.length} CVE</span>
                      </span>
                    )
                  })
                ) : (
                  <span className="muted">{vuln.includes(s) || clean.includes(s) ? t('важных нет') : why(s)}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }

  // Раздел пакета: на каких серверах, что стоит и чем закрывается, команда и все CVE
  const pkgSec = (p: Pkg) => {
    const bins = [...new Set(p.hits.flatMap((h) => h.f.pkgs.map((x) => x[0])))].sort(byName)
    const pro = p.hits.filter((h) => h.f.pro && !h.s.pro_on)
    return (
      <>
        {(p.kernel || bins.length > 1) && (
          <p className="muted small al-lead">
            {p.kernel
              ? t('Сверяется работающее ядро, а не установленное: новое начинает действовать только после перезагрузки.')
              : t('Из исходного пакета {src} на серверах стоят: {list}.', { src: p.src, list: bins.join(', ') })}
          </p>
        )}
        {pro.length > 0 && (
          <div className="al-note al-note-warn">
            <span>
              {t('Исправление только в Ubuntu Pro, а он не подключен: {list}.', { list: pro.map((h) => h.s.name).join(', ') })}
            </span>
          </div>
        )}
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Сервер')}</th>
              <th>{p.kernel ? t('Работает') : t('Стоит')}</th>
              <th className="hide-m">{t('Исправлено в')}</th>
              <th className="num">CVE</th>
              {p.kev > 0 && (
                <th className="num" title={t('Эксплуатируется')}>
                  <span className="hide-m">{t('Эксплуатируется')}</span>
                  <span className="only-m">{t('эксп.')}</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {p.hits.map(({ s, f }) => {
              const kev = f.cves.filter((c) => c.kev).length
              return (
                <tr key={s.id}>
                  <td>{srvLink(s)}</td>
                  <td className="small">
                    <span className="mono">{f.pkgs[0]?.[1] ?? ''}</span>
                    {p.kernel && f.src !== 'linux' && <span className="muted"> {f.src}</span>}
                  </td>
                  <td className="small hide-m">
                    <span className="mono">{f.fixed}</span>
                    {f.pro && !s.pro_on && <span className="muted"> Pro</span>}
                  </td>
                  <td className="num">{f.cves.length}</td>
                  {p.kev > 0 && <td className="num t-down">{kev || ''}</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
        {/* без подключенного Pro apt такое исправление не поставит: команда - только для остальных */}
        <VulnFix found={p.hits.filter((h) => !pro.includes(h)).map((h) => h.f)} proOn />
        <div className="srv-nav-grp rt-cap">CVE · {p.cves.length}</div>
        <VulnCves cves={p.cves} />
      </>
    )
  }

  // Раздел сервера: то же, что в карточке, плюс система и группа
  const srvSec = (s: FleetVulnServer) => (
    <>
      <div className="ans-kv small">
        <span className="muted">{t('Система')}</span>
        <span>{s.os}</span>
        {s.group && (
          <>
            <span className="muted">{t('Группа')}</span>
            <span>{s.group}</span>
          </>
        )}
      </div>
      <VulnsNote v={s} />
      <div className="vuln-list">
        {s.found.map((f) => (
          <VulnRow key={f.src + (f.kernel ? ':k' : '')} f={f} proOn={s.pro_on} />
        ))}
      </div>
      <VulnFix found={s.found} proOn={s.pro_on} />
    </>
  )

  const listSec = (list: FleetVulnServer[], what: 'clean' | 'unknown') => {
    const anyGroup = list.some((s) => s.group)
    return (
      <>
        <p className="muted small al-lead">
          {what === 'clean'
            ? t('Пакеты и ядро сверены с базой Canonical: critical и эксплуатируемых уязвимостей, которые закрываются обновлением, нет.')
            : t('Эти серверы не с чем сверить: сверка пока только для Ubuntu, и нужен список пакетов от агента.')}
        </p>
        <table className="sec-tbl">
          <tbody>
            {[...list]
              .sort((a, b) => byName(a.name, b.name))
              .map((s) => (
                <tr key={s.id}>
                  <td>{srvLink(s)}</td>
                  {anyGroup && <td className="muted small hide-m">{s.group}</td>}
                  <td className="small">
                    <span className="muted">{what === 'clean' ? s.os : why(s)}</span>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </>
    )
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Уязвимости')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={info} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {curPkg ? pkgLabel(curPkg) : curSrv ? curSrv.name : t(HEAD[cur.id] ?? cur.label)}{' '}
                {curPkg && tags(curPkg.kev, curPkg.crit)}
                {curSrv && tags(curSrv.summary.kev, curSrv.summary.critical)}
              </h4>
              {cur.id === 'over' && data && (
                <div className="modal-toolbar srv-head-tools">
                  <input
                    className="checks-search modal-search"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder={t('CVE, пакет или сервер')}
                  />
                </div>
              )}
              {curSrv && (
                <button className="ghost small srv-head-btn" onClick={() => onOpen('servers', curSrv.id, 'vulns')}>
                  {t('Карточка сервера')} ›
                </button>
              )}
            </div>
            {!data ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                {cur.id === 'over' && (needle ? found() : overview())}
                {curPkg && pkgSec(curPkg)}
                {curSrv && srvSec(curSrv)}
                {cur.id === 'clean' && listSec(clean, 'clean')}
                {cur.id === 'unknown' && listSec(unknown, 'unknown')}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
