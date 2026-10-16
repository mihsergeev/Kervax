import { useEffect, useState } from 'react'
import { ApiError, getServerVulns, type Server, type ServerVulns, type VulnCve, type VulnFinding } from './api'
import { useI18n } from './i18n'

// Важные уязвимости в пакетах сервера (backend app/vulns.py). Только справка, как и пакеты: ни
// алертов, ни пунктов "требует действий". И только важное: critical и уже эксплуатируемые (CISA
// KEV), которые закрываются обновлением.

type Props = { server: Server; onUnauthorized: () => void }

// сколько CVE показывать у пакета сразу, остальное по кнопке
const CVES = 6

function cveUrl(id: string): string {
  return `https://ubuntu.com/security/${encodeURIComponent(id)}`
}

// CVE ссылками на ubuntu.com, уже эксплуатируемые выделены; длинный список - по кнопке
export function VulnCves({ cves }: { cves: VulnCve[] }) {
  const { t } = useI18n()
  const [all, setAll] = useState(false)
  const shown = all ? cves : cves.slice(0, CVES)
  return (
    <div className="vuln-cves">
      {shown.map((c) => (
        <a
          key={c.id}
          className={`vuln-cve${c.kev ? ' vuln-cve-kev' : ''}`}
          href={cveUrl(c.id)}
          target="_blank"
          rel="noreferrer"
          title={`${c.prio}${c.cvss ? `, CVSS ${c.cvss}` : ''}${c.kev ? `, ${t('уже эксплуатируют (CISA KEV)')}` : ''}`}
        >
          {c.id}
        </a>
      ))}
      {cves.length > CVES && (
        <button className="ghost small row-act" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : t('еще {n}', { n: cves.length - CVES })}
        </button>
      )}
    </div>
  )
}

// одна строка: исходный пакет (или ядро), что стоит, чем закрывается, какие CVE
export function VulnRow({ f, proOn }: { f: VulnFinding; proOn: boolean }) {
  const { t } = useI18n()
  const kev = f.cves.filter((c) => c.kev).length
  const crit = f.cves.filter((c) => c.prio === 'critical').length
  const installed = f.pkgs[0]?.[1] ?? ''
  const bins = f.kernel ? [] : f.pkgs.map((p) => p[0])
  return (
    <div className={`vuln-row${kev ? ' vuln-kev' : ''}`}>
      <div className="vuln-head">
        <span className={`lk-dot ${kev ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
        <span className="vuln-name">{f.kernel ? t('ядро ({src})', { src: f.src }) : f.src}</span>
        <span className="muted small vuln-ver">
          {installed} → {f.fixed}
        </span>
        {kev > 0 && <span className="vuln-tag vuln-tag-kev">{t('эксплуатируется: {n}', { n: kev })}</span>}
        {crit > 0 && <span className="vuln-tag">{t('critical: {n}', { n: crit })}</span>}
        {f.pro && !proOn && <span className="vuln-tag vuln-tag-pro">{t('только в Ubuntu Pro')}</span>}
        {f.kernel && <span className="vuln-tag vuln-tag-pro">{t('обновить и перезагрузить')}</span>}
      </div>
      {bins.length > 1 && <div className="muted small vuln-bins">{bins.join(', ')}</div>}
      <VulnCves cves={f.cves} />
    </div>
  )
}

// команда обновления: пакеты без Pro-only (если Pro не подключен) и без ядра
function upgradeCmd(found: VulnFinding[], proOn: boolean): string {
  const bins = [...new Set(found.filter((f) => !f.kernel && (proOn || !f.pro)).flatMap((f) => f.pkgs.map((p) => p[0])))]
  return bins.length ? `sudo apt update && sudo apt install --only-upgrade ${bins.join(' ')}` : ''
}

// как закрыть: команда обновления пакетов с кнопкой "копировать" и что делать с ядром.
// Находки одного сервера или одного пакета на разных серверах: apt с --only-upgrade пропускает
// то, чего на сервере нет, так что команда одна на все
export function VulnFix({ found, proOn }: { found: VulnFinding[]; proOn: boolean }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const cmd = upgradeCmd(found, proOn)
  const kernel = found.some((f) => f.kernel)
  if (!cmd && !kernel) return null
  return (
    <div className="vuln-fix small">
      {cmd && (
        <div className="vuln-cmd">
          <code className="mono">{cmd}</code>
          <button
            className="ghost small row-act"
            onClick={() => {
              navigator.clipboard?.writeText(cmd)
              setCopied(true)
              window.setTimeout(() => setCopied(false), 1500)
            }}
          >
            {copied ? t('скопировано') : t('копировать')}
          </button>
        </div>
      )}
      {kernel && (
        <div className="muted">
          {t('Ядро закрывается только перезагрузкой в новое: sudo apt full-upgrade, затем перезагрузка.')}
        </div>
      )}
    </div>
  )
}

export function VulnsNote({ v }: { v: Pick<ServerVulns, 'release' | 'eol' | 'pro_on' | 'os'> }) {
  const { t } = useI18n()
  if (!v.eol) return null
  return (
    <div className="vuln-eol small">
      {t('{os}: обычная поддержка кончилась ({eol}), исправления выходят только в Ubuntu Pro.', {
        os: v.os.replace(/\.\d+ LTS$/, ' LTS'),
        eol: v.eol.split('-').reverse().join('.'),
      })}{' '}
      {v.pro_on ? t('Pro подключен.') : t('Pro не подключен.')}
    </div>
  )
}

export function VulnsCard({ server: s, onUnauthorized }: Props) {
  const { t } = useI18n()
  const sum = s.last_report?.pkgs
  const [v, setV] = useState<ServerVulns | null>(null)
  const [err, setErr] = useState('')
  const hash = sum?.h ?? ''
  const kernel = s.last_report?.os_upd?.kernel ?? ''

  useEffect(() => {
    if (!sum?.n) return
    let alive = true
    getServerVulns(s.id)
      .then((r) => {
        if (!alive) return
        setV(r)
        setErr('')
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (alive) setErr(String((e as Error).message || e))
      })
    return () => {
      alive = false
    }
  }, [s.id, hash, kernel, sum?.n, onUnauthorized])

  if (!sum?.n) return null
  const n = v?.summary.pkgs ?? 0
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-vulns">
      <div className="net-head">
        <div className="chart-cap">{t('Уязвимости')}</div>
        {v && v.release && v.feed_ts && (
          <span className={`small ${n ? 't-down' : 'muted'}`}>
            {n ? t('важных CVE: {n}', { n: v.summary.cves }) : t('важных нет')}
          </span>
        )}
      </div>
      {err && <p className="form-error">{err}</p>}
      {!v && !err && <div className="muted small">{t('загрузка…')}</div>}
      {v && !v.release && <div className="muted small">{t('Сверка с базой уязвимостей пока только для Ubuntu.')}</div>}
      {v && v.release && !v.feed_ts && (
        <div className="muted small">
          {v.feed_err
            ? t('База уязвимостей не скачалась: {err}', { err: v.feed_err })
            : t('База уязвимостей еще не скачана: панель скачивает ее раз в сутки, первый раз - в течение 10 минут.')}
        </div>
      )}
      {v && v.release && v.feed_ts && (
        <>
          <div className="muted small">
            {t('Только critical и уже эксплуатируемые (CISA KEV), которые закрываются обновлением. База Canonical от {d}.', {
              d: new Date(v.feed_gen || v.feed_ts).toLocaleDateString(),
            })}
          </div>
          <VulnsNote v={v} />
          {v.found.length > 0 && (
            <div className="vuln-list">
              {v.found.map((f) => (
                <VulnRow key={f.src + (f.kernel ? ':k' : '')} f={f} proOn={v.pro_on} />
              ))}
            </div>
          )}
          <VulnFix found={v.found} proOn={v.pro_on} />
        </>
      )}
    </div>
  )
}
