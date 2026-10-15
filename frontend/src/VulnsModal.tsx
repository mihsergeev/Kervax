import { useEffect, useMemo, useState } from 'react'
import { ApiError, getFleetVulns, type FleetVulnServer, type FleetVulns } from './api'
import { useI18n } from './i18n'
import type { Section } from './App'
import { VulnRow, VulnsNote } from './VulnsCard'

// Уязвимости по всему парку (backend api/vulns.py): у каких серверов есть важные уязвимости и
// чем они закрываются. Отдельная страница из меню, только справка - без алертов.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (section: Section, id: number) => void
}

export function VulnsModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [data, setData] = useState<FleetVulns | null>(null)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<Set<string>>(new Set())

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

  const toggle = (k: string) =>
    setOpen((o) => {
      const n = new Set(o)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })

  const needle = q.trim().toLowerCase()
  // по серверу, пакету, бинарнику или CVE; у сервера, найденного по пакету или CVE, видны только совпавшие строки
  const groups = useMemo(() => {
    const vuln: FleetVulnServer[] = []
    const clean: FleetVulnServer[] = []
    const unknown: FleetVulnServer[] = []
    for (const s of data?.servers ?? []) {
      if (!s.release || !s.feed_ts || !s.pkgs) unknown.push(s)
      else if (s.found.length) vuln.push(s)
      else clean.push(s)
    }
    return { vuln, clean, unknown }
  }, [data])

  const rowsOf = (s: FleetVulnServer) => {
    if (!needle || s.name.toLowerCase().includes(needle)) return s.found
    return s.found.filter(
      (f) =>
        f.src.toLowerCase().includes(needle) ||
        f.pkgs.some((p) => p[0].toLowerCase().includes(needle)) ||
        f.cves.some((c) => c.id.toLowerCase().includes(needle)),
    )
  }
  const vuln = groups.vuln.filter((s) => rowsOf(s).length > 0)
  const totals = {
    servers: groups.vuln.length,
    kev: groups.vuln.filter((s) => s.summary.kev > 0).length,
    kernel: groups.vuln.filter((s) => s.summary.kernel).length,
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal dm-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Уязвимости')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="muted small">
          {t(
            'Только critical и уже эксплуатируемые (CISA KEV), которые закрываются обновлением: средние и мелкие не показываются. Пакеты сверяются с базой Canonical, ядро - работающее, а не установленное.',
          )}
        </div>
        <div className="modal-toolbar">
          <input
            className="dm-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t('CVE, пакет или сервер')}
          />
          {data && (
            <span className="small muted">
              {t('с важными уязвимостями: {n}', { n: totals.servers })}
              {totals.kev > 0 && <span className="t-down"> · {t('с эксплуатируемыми: {n}', { n: totals.kev })}</span>}
              {totals.kernel > 0 && ` · ${t('ждут нового ядра: {n}', { n: totals.kernel })}`}
            </span>
          )}
        </div>
        {err && <p className="form-error">{err}</p>}
        {!data && !err && <p className="muted small">{t('загрузка…')}</p>}
        {data && data.feeds.length === 0 && (
          <p className="muted small">
            {t('Базы уязвимостей еще не скачаны: панель скачивает их раз в сутки, первый раз - в течение 10 минут.')}
          </p>
        )}
        {data && vuln.length === 0 && groups.vuln.length > 0 && <p className="muted small">{t('Ничего не найдено.')}</p>}
        <div className="vuln-servers">
          {vuln.map((s) => {
            const rows = rowsOf(s)
            const key = `s${s.id}`
            const isOpen = open.has(key) || !!needle
            return (
              <div key={s.id} className="vuln-srv">
                <div className="vuln-srv-head">
                  <button className="ghost small dm-sec-btn" onClick={() => toggle(key)}>
                    {isOpen ? '▾' : '▸'}
                  </button>
                  <button className="dm-name" onClick={() => onOpen('servers', s.id)}>
                    {s.name}
                  </button>
                  {s.group && <span className="muted small">{s.group}</span>}
                  <span className="muted small">{s.os}</span>
                  {s.summary.kev > 0 && (
                    <span className="vuln-tag vuln-tag-kev">{t('эксплуатируется: {n}', { n: s.summary.kev })}</span>
                  )}
                  {s.summary.critical > 0 && (
                    <span className="vuln-tag">{t('critical: {n}', { n: s.summary.critical })}</span>
                  )}
                  {s.summary.kernel && <span className="vuln-tag vuln-tag-pro">{t('ядро')}</span>}
                  <span className="muted small">
                    {t('пакетов: {n}', { n: s.found.filter((f) => !f.kernel).length })}
                  </span>
                </div>
                {isOpen && (
                  <div className="vuln-list">
                    <VulnsNote v={s} />
                    {rows.map((f) => (
                      <VulnRow key={f.src + (f.kernel ? ':k' : '')} f={f} proOn={s.pro_on} />
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
        {groups.clean.length > 0 && !needle && (
          <section className="rep-sec">
            <button className="ghost small dm-sec-btn" onClick={() => toggle('clean')}>
              {open.has('clean') ? '▾' : '▸'} {t('Без важных уязвимостей: {n}', { n: groups.clean.length })}
            </button>
            {open.has('clean') && (
              <div className="dm-chips">
                {groups.clean.map((s) => (
                  <button key={s.id} className="dm-chip" onClick={() => onOpen('servers', s.id)}>
                    <span className="lk-dot lk-dot-ok" />
                    {s.name}
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
        {groups.unknown.length > 0 && !needle && (
          <section className="rep-sec">
            <button className="ghost small dm-sec-btn" onClick={() => toggle('unknown')}>
              {open.has('unknown') ? '▾' : '▸'} {t('Не проверены: {n}', { n: groups.unknown.length })}
            </button>
            {open.has('unknown') && (
              <div className="dm-chips">
                {groups.unknown.map((s) => (
                  <button
                    key={s.id}
                    className="dm-chip"
                    title={
                      !s.release
                        ? t('не Ubuntu')
                        : !s.pkgs
                          ? t('список пакетов еще не пришел')
                          : t('база уязвимостей еще не скачана')
                    }
                    onClick={() => onOpen('servers', s.id)}
                  >
                    <span className="lk-dot" />
                    {s.name}
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
        {data && data.feeds.length > 0 && (
          <div className="muted small vuln-feeds">
            {t('Базы Canonical:')}{' '}
            {data.feeds
              .map(
                (f) =>
                  `${f.release} ${f.gen || f.ts ? new Date(f.gen || f.ts || '').toLocaleDateString() : '-'}` +
                  (f.err ? ` (${t('ошибка: {e}', { e: f.err })})` : ''),
              )
              .join(', ')}
          </div>
        )}
      </div>
    </div>
  )
}
