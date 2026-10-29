import { useState, type CSSProperties } from 'react'
import type { Server, VpnClient, VpnSvc } from './api'
import { OvRows, type OvRow } from './cardLayout'
import { useI18n } from './i18n'
import { fmtBytes } from './units'

// VPN и прокси на сервере (app/vpn.py) в карточке: что раздает, где стоит, клиенты от helper'а
// vpn-setup - имя, адрес в туннеле, когда был на связи, трафик и откуда пришел. Внизу то, где сервер
// сам в меше или туннеле. Плашка в списке и в шапке карточки - RoleChips.tsx

type TFn = (k: string, p?: Record<string, string | number>) => string

const SHOW = 30 // клиентов сразу, остальные по кнопке

function ago(ts: number | null, t: TFn): string {
  if (!ts) return '—'
  const sec = Date.now() / 1000 - ts
  if (sec < 120) return t('только что')
  if (sec < 3600) return t('{n} мин назад', { n: Math.floor(sec / 60) })
  if (sec < 48 * 3600) return t('{n} ч назад', { n: Math.floor(sec / 3600) })
  return new Date(ts * 1000).toLocaleDateString()
}

const portText = (p: string) => p.toUpperCase().split('/').reverse().join(' ')

// секретов helper не присылает, а адрес клиента в подсказке целиком: в таблице он может не влезть
function ClientRow({ c, cols, many, t }: { c: VpnClient; cols: Cols; many: boolean; t: TFn }) {
  return (
    <div className="gpu-tr vpn-tr">
      <span className={`lk-dot ${c.on ? 'lk-dot-ok' : 'lk-dot-idle'}`} title={c.on ? t('на связи') : t('не на связи')} />
      <span className="vpn-name" title={many ? c.src : undefined}>
        {c.name || <span className="muted">{t('без имени')}</span>}
        {many && <span className="muted small"> {c.src}</span>}
      </span>
      {cols.ip && <span className="mono">{c.ip || '—'}</span>}
      {cols.hs && <span className="vpn-m" title={c.hs ? new Date(c.hs * 1000).toLocaleString() : undefined}>{c.on ? t('на связи') : ago(c.hs, t)}</span>}
      {cols.tr && <span className="num">{fmtBytes(c.tx, '—')}</span>}
      {cols.tr && <span className="num vpn-m">{fmtBytes(c.rx, '—')}</span>}
      {cols.ep && <span className="mono vpn-m2 vpn-ep" title={c.ep}>{c.ep || '—'}</span>}
    </div>
  )
}

type Cols = { ip: boolean; hs: boolean; tr: boolean; ep: boolean }

function SvcCard({ m, helper, stale, t }: { m: VpnSvc; helper: boolean; stale: boolean; t: TFn }) {
  const [all, setAll] = useState(false)
  const cl = m.clients
  // пустые у всех колонки не рисуем: у Xray нет адреса в туннеле и handshake, у Headscale трафика
  const cols: Cols = {
    ip: cl.some((c) => c.ip),
    hs: cl.some((c) => c.hs || c.on),
    tr: cl.some((c) => c.rx || c.tx),
    ep: cl.some((c) => c.ep),
  }
  const many = m.src.length > 1
  const pills: { text: string; lv?: 'warn' | 'idle' }[] = []
  pills.push({ text: m.src.join(', ') })
  if (m.ports.length) pills.push({ text: m.ports.map(portText).join(', ') })
  if (m.short !== m.label) pills.push({ text: m.short })
  if (m.n != null) pills.push({ text: t('клиентов: {n}', { n: m.n }) })
  if (m.helper) pills.push({ text: t('на связи: {n}', { n: m.on }), lv: stale ? 'idle' : undefined })
  if (m.exit) pills.push({ text: t('выход в интернет для меша (exit node)') })
  if (m.routes?.length) pills.push({ text: t('отдает в меш: {list}', { list: m.routes.join(', ') }) })
  const errs = m.parts.filter((p) => p.err)
  const shown = all ? cl : cl.slice(0, SHOW)
  return (
    <div className="chart-card net-card net-card-wide" id={`mcard-vpn-${m.k}`}>
      <div className="net-head">
        <div className="chart-cap">{m.label}</div>
      </div>
      <div className="dh-sum">
        {pills.map((p, i) => (
          <span key={i} className="dh-pill">
            <span className={`lk-dot lk-dot-${p.lv ?? 'ok'}`} />
            {p.text}
          </span>
        ))}
      </div>
      {errs.map((p) => (
        <div key={p.src} className="muted small">{t('{src}: клиентов не прочитать ({err})', { src: p.src, err: p.err })}</div>
      ))}
      {!helper && m.role === 'server' && (
        <div className="muted small">{t('Клиентов показывает helper vpn-setup, на сервере его нет: он есть в "Требует действий".')}</div>
      )}
      {helper && !m.helper && !errs.length && m.role === 'server' && (
        <div className="muted small">{t('Клиентов этого сервиса helper не читает: видно только, что он работает.')}</div>
      )}
      {m.helper && cl.length === 0 && <div className="muted small">{t('Клиентов нет.')}</div>}
      {cl.length > 0 && (
        <div className="gpu-ptbl vpn-tbl" style={vpnGrid(cols)}>
          <div className="gpu-tr gpu-th vpn-tr">
            <span />
            <span>{t('клиент')}</span>
            {cols.ip && <span>{t('адрес в туннеле')}</span>}
            {cols.hs && <span className="vpn-m">{t('был на связи')}</span>}
            {cols.tr && <span className="num">{t('скачал')}</span>}
            {cols.tr && <span className="num vpn-m">{t('отдал')}</span>}
            {cols.ep && <span className="vpn-m2">{t('откуда')}</span>}
          </div>
          {shown.map((c, i) => <ClientRow key={`${c.src}-${c.name}-${c.ip}-${i}`} c={c} cols={cols} many={many} t={t} />)}
        </div>
      )}
      {cl.length > SHOW && (
        <button className="ghost small" onClick={() => setAll((v) => !v)}>
          {all ? t('свернуть') : t('показать всех: {n}', { n: cl.length })}
        </button>
      )}
    </div>
  )
}

// колонки таблицы по тому, что в ней есть: на среднем экране App.css прячет "откуда" (.vpn-m2), на
// телефоне еще "был на связи" и "отдал" (.vpn-m) и берет шаблон без них
function vpnGrid(c: Cols): CSSProperties {
  // точка - max-content, а не 10px: отступ строки-subgrid ложится на крайние колонки и наезжал на имя
  const cols = (m: number) => ['max-content', 'minmax(90px, 1fr)', c.ip && 'max-content', m < 2 && c.hs && 'max-content',
    c.tr && 'max-content', m < 2 && c.tr && 'max-content', m < 1 && c.ep && 'minmax(0, max-content)'].filter(Boolean).join(' ')
  return { '--vpn-cols': cols(0), '--vpn-cols-1': cols(1), '--vpn-cols-2': cols(2) } as CSSProperties
}

export function VpnCards({ server: s, rows }: { server: Server; rows: OvRow[] }) {
  const { t } = useI18n()
  const v = s.vpn
  if (!v) return null
  const serve = v.svc.filter((m) => m.role === 'server')
  const mesh = v.svc.filter((m) => m.role === 'mesh')
  return (
    <>
      <div className="chart-card net-card net-card-wide" id="mcard-vpn">
        <div className="net-head">
          <div className="chart-cap">VPN</div>
          <span className="muted small">
            {!v.helper
              ? t('по контейнерам и интерфейсам')
              : v.stale && v.ts
                ? t('клиенты устарели: снимок {when}', { when: ago(v.ts, t) })
                : t('helper vpn-setup, раз в 2 минуты')}
          </span>
        </div>
        <OvRows rows={rows} />
        <div className="muted small">
          {serve.length
            ? `${t('Раздает: {list}', { list: serve.map((m) => (m.short !== m.label ? `${m.label} (${m.short})` : m.label)).join(', ') })}.`
            : t('VPN клиентам не раздает.')}
          {mesh.length > 0 && ` ${t('Сам в меше или туннеле: {list}', { list: mesh.map((m) => `${m.label} (${m.src.join(', ')})`).join(', ') })}.`}
        </div>
        {mesh.some((m) => m.guess) && (
          <div className="muted small">{t('Раздает ли VPN туннель WireGuard, панель узнает от helper vpn-setup: пир без Endpoint в конфиге подключается к серверу.')}</div>
        )}
      </div>
      {serve.map((m) => <SvcCard key={`${m.k}-${m.role}`} m={m} helper={v.helper} stale={v.stale} t={t} />)}
      {mesh.filter((m) => m.clients.length).map((m) => <SvcCard key={`${m.k}-${m.role}`} m={m} helper={v.helper} stale={v.stale} t={t} />)}
    </>
  )
}

// строка раздела на карте сервера и в меню
// eslint-disable-next-line react-refresh/only-export-components
export function vpnSecLine(s: Server, t: TFn): string {
  const serve = (s.vpn?.svc ?? []).filter((m) => m.role === 'server')
  if (!serve.length) return s.vpn?.svc.length ? t('сам в меше, VPN не раздает') : ''
  const on = serve.reduce((a, m) => a + m.on, 0)
  const n = serve.reduce((a, m) => a + (m.n ?? 0), 0)
  const names = [...new Set(serve.map((m) => m.short))].join(', ')
  return s.vpn?.helper && !s.vpn.stale
    ? t('{names}: клиентов {n}, на связи {on}', { names, n, on })
    : names
}

// слова для поиска в списке серверов: "vpn" у раздающих, подписи, протоколы и контейнеры
// eslint-disable-next-line react-refresh/only-export-components
export function vpnSearch(s: Server): string {
  const w: string[] = []
  for (const m of s.vpn?.svc ?? []) {
    w.push(m.label, m.short, ...m.src)
    if (m.role === 'server') w.push('vpn')
  }
  return w.join(' ').toLowerCase()
}
