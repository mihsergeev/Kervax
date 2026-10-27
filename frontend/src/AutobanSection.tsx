// Блок "Автобан сканеров" в разделе Веб карточки сервера (helper autoban-setup, backend/app/autoban.py):
// режим (банит, только наблюдает, выключен), где бан на ноде не работает, активные баны (за что, где,
// до когда, разбанить), кого бы забанило в наблюдении, свои адреса, стучавшие в ловушки, и история.
// Белый список и сводка банов - общие для панели, правятся тут же, в окне "Белый список и сводка"
import { useCallback, useEffect, useState } from 'react'
import {
  ApiError, autobanCommand, autobanSettings, backupCommandStatus, saveAutobanSettings, serverAutoban,
  type AutobanBan, type AutobanData, type AutobanSettings, type Server,
} from './api'
import { useI18n } from './i18n'
import { useAuth } from './auth'
import { toastError } from './toast'
import { CountryFlag } from './CountryFlag'

type T = (s: string, vars?: Record<string, string | number>) => string
const when = (ts: number) =>
  new Date(ts * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
const MODES: { m: 'on' | 'watch' | 'off'; l: string }[] = [
  { m: 'on', l: 'банит' }, { m: 'watch', l: 'только наблюдает' }, { m: 'off', l: 'выключен' },
]
const END: Record<string, string> = { expired: 'истек', unban: 'снят вручную', gone: 'снят на ноде' }

// команда в спул helper'а: ждем ответа агента (обычно секунды)
async function runCmd(serverId: number, op: 'on' | 'watch' | 'off' | 'unban', ip = ''): Promise<string> {
  const c = await autobanCommand(serverId, op, ip)
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    const st = await backupCommandStatus(serverId, c.id)
    if (st.status === 'done') return ''
    if (st.status === 'error') return st.result || 'error'
  }
  return ''
}

export function AutobanSection({ server: s, onUnauthorized }: { server: Server; onUnauthorized: () => void }) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  const [d, setD] = useState<AutobanData | null>(null)
  const [busy, setBusy] = useState('')
  const [hist, setHist] = useState(false)
  const [cfgOpen, setCfgOpen] = useState(false)
  const load = useCallback(() => {
    serverAutoban(s.id)
      .then(setD)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        setD(null)
      })
  }, [s.id, onUnauthorized])
  useEffect(() => {
    load()
    const id = window.setInterval(load, 60000)
    return () => window.clearInterval(id)
  }, [load])
  const v = d?.view
  if (!d || (!v && d.history.length === 0)) return null
  const can = isAdmin && !!v?.agent_ok
  const act = async (op: 'on' | 'watch' | 'off' | 'unban', ip = '') => {
    setBusy(op + ip)
    try {
      const err = await runCmd(s.id, op, ip)
      if (err) toastError(new Error(err))
      load()
    } catch (e) {
      toastError(e)
    } finally {
      setBusy('')
    }
  }
  const head = !v ? t('helper не присылает отчет больше 15 минут: баны на ноде держатся, но панель их не видит')
    : v.mode === 'on'
      ? (v.can
        ? t('банит на 80 и 443: сейчас {n}, за сутки {d}, за час {h}', { n: v.bans.length, d: v.day, h: v.hour })
        : t('включен, но не банит: {e}', { e: v.err || t('файрвол не принимает правила') }))
      : v.mode === 'watch'
        ? (v.auto.startsWith('avalanche')
          ? t('сам перешел в наблюдение: больше 100 банов за час, ничего не банит, пока не включат снова')
          : t('только наблюдает: в файрвол ничего не пишет, ниже - кого бы забанил'))
        : t('выключен: логи не читает')
  const hard = v && (v.problems.length > 0 || (v.mode === 'on' && !v.can))
  return (
    <div className="chart-card net-card net-card-wide ab-card">
      <div className="net-head">
        <div className="chart-cap">{t('Автобан сканеров')}</div>
        <span className="muted small">
          {t('3 разных пути-ловушки за 10 минут (/.env, /.git/config, дампы .sql, /actuator...) - бан на 80/443: сутки, повтор - неделя, дальше месяц')}
        </span>
      </div>
      <div className={`small${hard ? ' ab-bad' : v?.mode === 'on' ? '' : ' muted'}`}>{head}</div>
      {v && (
        <div className="ab-modes">
          {MODES.map(({ m, l }) => (
            <button key={m} className={`ghost small${v.mode === m ? ' ab-mode-on' : ''}`} disabled={!can || !!busy || v.mode === m}
              onClick={() => act(m)}>{t(l)}</button>
          ))}
          {isAdmin && <button className="ghost small" onClick={() => setCfgOpen(true)}>{t('белый список и сводка')}</button>}
          {v.mode === 'on' && v.drops > 0 && <span className="muted small">{t('отброшено пакетов: {n}', { n: v.drops })}</span>}
        </div>
      )}
      {v && isAdmin && !v.agent_ok && (
        <div className="muted small">
          {t('Кнопки разбана и режима работают с агентом {v}+. До обновления режим меняется на ноде:', { v: '2.55' })}{' '}
          <code className="mono">echo mode=on | sudo tee /etc/kervax/autoban.conf</code>
        </div>
      )}
      {v?.problems.map((p) => <div key={p.key} className="small ab-bad">{p.text}</div>)}
      {v?.notes.map((n) => <div key={n.k} className={`small ${n.level >= 2 ? 'ab-bad' : 'ab-warn'}`}>{n.text}</div>)}
      {v && v.bans.length > 0 && (
        <BanList t={t} title={t('Забанены сейчас')} rows={v.bans} onUnban={can ? (ip) => act('unban', ip) : undefined} busy={busy} />
      )}
      {v && v.would.length > 0 && <BanList t={t} title={t('Забанил бы (наблюдение)')} rows={v.would} busy={busy} />}
      {v && v.bans.length === 0 && v.would.length === 0 && v.mode !== 'off' && (
        <div className="muted small">{t('Сейчас никто не забанен.')}</div>
      )}
      {v && v.own.length > 0 && (
        <div className="ab-list">
          <div className="small">{t('Не банит, хотя стучали в ловушки (сутки)')}</div>
          {v.own.map((x) => (
            <div key={x.ip} className="web-err-row ab-row ab-row-own">
              <div className="web-err-head">
                <span className="web-err-where mono"><CountryFlag code={x.cc} /> {x.ip}</span>
                <span className="muted small">{x.label === 'cloudflare'
                  ? t('край Cloudflare: бан на ноде не поможет')
                  : t('белый список: {l}', { l: x.label === 'local' || x.label === 'self' ? t('свой адрес') : x.label })}</span>
              </div>
              <div className="web-err-line small">
                <span className="mono">{x.paths.join(', ')}{x.more ? ` +${x.more}` : ''}</span> - {t('{n} ударов, последний {w}', { n: x.hits, w: when(x.last) })}
              </div>
            </div>
          ))}
        </div>
      )}
      {v && v.proxy.length > 0 && (
        <div className="muted small">
          {t('Не банит как прокси перед сервером: {l}', { l: v.proxy.map((x) => t('{ip} - {p}% запросов лога', { ip: x.ip, p: x.share })).join(', ') })}
        </div>
      )}
      {d.history.length > 0 && (
        <div className="ab-list">
          <button className="ghost small" onClick={() => setHist(!hist)}>
            {hist ? '▾' : '▸'} {t('история за 30 дней ({n})', { n: d.history.length })}
          </button>
          {hist && d.history.map((x) => (
            <div key={`${x.ip}-${x.since}`} className="web-err-line small ab-hist">
              <span className="mono"><CountryFlag code={x.cc} /> {x.ip}</span>{' '}
              <span className="muted">{when(x.since)}</span>{' '}
              {x.watch && <span className="type-chip off">{t('наблюдение')}</span>}{' '}
              <span>{x.why}</span>{' '}
              <span className="muted">
                {x.ended ? t('{e} {w}', { e: t(END[x.end] ?? 'снят'), w: when(x.ended) }) : t('до {w}', { w: when(x.until) })}
                {x.n > 1 ? ` · ${t('повтор {n}', { n: x.n })}` : ''}
              </span>
            </div>
          ))}
        </div>
      )}
      {cfgOpen && <AutobanSettingsModal onClose={() => setCfgOpen(false)} />}
    </div>
  )
}

function BanList({ t, title, rows, onUnban, busy }: {
  t: T; title: string; rows: AutobanBan[]; onUnban?: (ip: string) => void; busy: string
}) {
  return (
    <div className="ab-list">
      <div className="small">{title}</div>
      {rows.map((x) => (
        <div key={x.ip} className={`web-err-row ab-row${x.watch ? ' ab-row-watch' : ''}`}>
          <div className="web-err-head">
            <span className="web-err-where mono"><CountryFlag code={x.cc} /> {x.ip}</span>
            <span className="small web-err-mute">
              {t('{s} - до {u}', { s: when(x.since), u: when(x.until) })}
              {x.n > 1 && <span className="type-chip lg-banned">{t('повтор {n}', { n: x.n })}</span>}
              {onUnban && (
                <button className="ghost small" disabled={!!busy} onClick={() => onUnban(x.ip)}>
                  {busy === 'unban' + x.ip ? t('снимаю...') : t('разбанить')}
                </button>
              )}
            </span>
          </div>
          <div className="web-err-line small"><span className="muted">{t('за что')}: </span><span className="mono">{x.why}</span></div>
          {x.seen > 0 && (
            <div className="web-err-line small ab-bad">
              {t('после бана приходил еще {n} раз: трафик идет к ноде не напрямую', { n: x.seen })}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// Белый список автобана и сводка банов: общие для всех серверов панели
function AutobanSettingsModal({ onClose }: { onClose: () => void }) {
  const { t } = useI18n()
  const [cfg, setCfg] = useState<AutobanSettings | null>(null)
  const [text, setText] = useState('')
  const [digest, setDigest] = useState<'day' | 'hour' | 'off'>('day')
  const [saving, setSaving] = useState(false)
  const [showAuto, setShowAuto] = useState(false)
  useEffect(() => {
    autobanSettings()
      .then((c) => {
        setCfg(c)
        setText(c.allow.map((x) => `${x.net}${x.label ? ' ' + x.label : ''}`).join('\n'))
        setDigest(c.digest)
      })
      .catch(toastError)
  }, [])
  const save = async () => {
    setSaving(true)
    try {
      const allow = text.split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#')).map((l) => {
        const [net, ...rest] = l.split(/\s+/)
        return { net, label: rest.join(' ').replace(/^#\s*/, '').slice(0, 40) }
      })
      setCfg(await saveAutobanSettings({ allow, digest }))
      onClose()
    } catch (e) {
      toastError(e)
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal alerts-modal al-small" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Автобан: белый список и сводка')}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        {!cfg ? <div className="muted small">{t('Загрузка...')}</div> : (
          <div className="srv-sec">
            <div className="muted small">
              {t('Эти адреса автобан не банит ни на одном сервере панели: ваши VPN, дом, офис, другие панели Kervax. Строка - адрес или сеть, через пробел подпись. Свои сети ноды, сети подов и Docker, меш 100.64.0.0/10 и Cloudflare не банятся и так.')}
            </div>
            <textarea className="mono ab-allow" rows={8} value={text} placeholder={'203.0.113.5 VPN\n198.51.100.0/24 office'}
              onChange={(e) => setText(e.target.value)} />
            <div className="small">
              <button className="ghost small" onClick={() => setShowAuto(!showAuto)}>
                {showAuto ? '▾' : '▸'} {t('панель добавляет сама адреса серверов и локаций проверок: {n}', { n: cfg.auto.length })}
              </button>
              {showAuto && <div className="muted small mono">{cfg.auto.map((x) => `${x.net} ${x.label}`).join(', ')}</div>}
            </div>
            <label className="small">
              {t('Сводка банов в Telegram')}{' '}
              <select value={digest} onChange={(e) => setDigest(e.target.value as 'day' | 'hour' | 'off')}>
                <option value="day">{t('раз в сутки, в 9 утра')}</option>
                <option value="hour">{t('раз в час, если были баны')}</option>
                <option value="off">{t('не присылать')}</option>
              </select>
            </label>
            <div className="muted small">
              {t('Сразу, отдельным алертом, приходит только то, что требует вмешательства: забанен свой адрес, автобан включен и не может банить, бан не действует, лавина банов.')}
            </div>
            <div>
              <button disabled={saving} onClick={save}>{saving ? t('Сохраняю...') : t('Сохранить')}</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
