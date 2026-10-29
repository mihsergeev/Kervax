import type { GreView, Server } from './api'
import { NetTile, type Tone } from './NetLinks'
import { useI18n } from './i18n'
import { sinceShort } from './serverUtils'

// GRE до панели (backend app/gre.py) в карточке сервера: пометка у адреса в шапке и в списке и
// карточка в разделе "Сеть". Пометка не алерт: только видно, ходит ли агент туннелем сейчас, а если
// GRE включен, но последний отчет пришел напрямую, - почему. Алерт - отдельно (gre_down, backend
// gre.down): напрямую 10 минут подряд, он же в "Что сломано" и в замечаниях раздела. Настройка и
// подробный статус helper'а - в настройках сервера (ServerEdit.tsx, GreStatus)

type TFn = (k: string, p?: Record<string, string | number>) => string

// Состояния, где GRE еще настраивается: агент и не должен пока ходить туннелем
const SETUP = new Set<GreView['state']>(['no_helper', 'waiting'])

function ago(iso: string | null, t: TFn): string {
  if (!iso) return ''
  const sec = (Date.now() - Date.parse(iso)) / 1000
  if (!Number.isFinite(sec)) return ''
  if (sec < 60) return t('только что')
  if (sec < 3600) return t('{n} мин назад', { n: Math.floor(sec / 60) })
  if (sec < 48 * 3600) return t('{n} ч назад', { n: Math.floor(sec / 3600) })
  return t('{n} дн назад', { n: Math.floor(sec / 86400) })
}

// "14:20" сегодня, иначе "28.10 14:20"
function at(iso: string): string {
  const d = new Date(iso)
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === new Date().toDateString()) return hm
  return `${d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${hm}`
}

// Почему агент сейчас не в туннеле, по состоянию helper'а, хоста панели и агента
function whyDirect(g: GreView, t: TFn): string {
  if (g.connect_down)
    return t('агент не дождался ответа панели через туннель и ходит напрямую, повторит через 2 минуты')
  switch (g.state) {
    case 'no_helper':
      return t('helper gre-setup еще не поставлен')
    case 'waiting':
      return t('helper поставлен, ждем его первого обращения к панели')
    case 'host':
      return t('хост панели свою сторону туннеля не поднял (ops/gre-sync.sh)')
    case 'nat':
      return t('у сервера нет публичного адреса на интерфейсе, GRE через NAT не проходит')
    case 'stale':
      return t('helper gre-setup давно не обращался к панели')
    case 'down':
      return t('туннель не поднят или панель через него не отвечает')
    case 'error':
      return t('ошибка GRE: {err}', { err: g.error || '?' })
    case 'up':
      return g.connect
        ? t('туннель поднят, агент вот-вот перейдет в него')
        : t('туннель поднят, но агент о нем не знает: нужен агент 2.56 и connect= в его конфиге')
    default:
      return t('последний отчет пришел напрямую')
  }
}

type GreMark = {
  tone: 'ok' | 'warn' | 'idle'
  label: string // в шапке карточки
  short: string // в списке
  lines: string[] // подсказка
}

function greMark(g: GreView, online: boolean, t: TFn): GreMark {
  const ends = { node: g.node_iface, naddr: g.node_addr || '?', iface: g.iface, paddr: g.panel_addr || '?', host: g.panel_ip }
  const tunnel = g.panel_ip
    ? t('Туннель: {node} {naddr} на сервере, {iface} {paddr} на хосте панели {host}', ends)
    : t('Туннель: {node} {naddr} на сервере, {iface} {paddr} на хосте панели', ends)
  const net = t('Сеть {net}, MTU {mtu}', { net: g.net || '?', mtu: g.mtu })
  const real = g.peer ? t('Внешний адрес сервера {ip}: его панель и показывает вместо туннельного', { ip: g.peer }) : ''
  const helper =
    g.up && g.seen_at
      ? t('Туннель жив: helper {h} проверил панель через него {ago}', {
          h: g.helper ? `gre-setup ${g.helper}` : 'gre-setup',
          ago: ago(g.seen_at, t),
        })
      : ''
  const lastVia = g.agent_at ? t('Последний отчет через туннель: {ago}', { ago: ago(g.agent_at, t) }) : ''
  const offline = online ? '' : t('Сервер сейчас не на связи: это про последний отчет')
  if (g.via === 'gre') {
    const since = g.via_since
      ? t('Агент ходит в панель через GRE-туннель с {at} ({dur})', { at: at(g.via_since), dur: sinceShort(g.via_since, t) || t('только что') })
      : t('Агент ходит в панель через GRE-туннель')
    return {
      tone: 'ok',
      label: t('через GRE'),
      short: 'GRE',
      lines: [since, tunnel, net, real, helper, offline].filter(Boolean),
    }
  }
  const setup = SETUP.has(g.state) && !g.connect_down
  const head = setup
    ? t('GRE до панели включен и настраивается: {why}', { why: whyDirect(g, t) })
    : t('GRE до панели включен, но последний отчет пришел напрямую: {why}', { why: whyDirect(g, t) })
  const since = g.via === 'direct' && g.via_since && !setup ? t('Напрямую с {at}', { at: at(g.via_since) }) : ''
  return {
    tone: setup ? 'idle' : 'warn',
    label: setup ? t('GRE настраивается') : t('GRE: напрямую'),
    short: 'GRE',
    lines: [head, since, lastVia, tunnel, net, offline].filter(Boolean),
  }
}

// Плашка у адреса: в шапке карточки "через GRE" / "GRE: напрямую", в списке коротко "GRE"
export function GreChip({ s, compact, onClick }: { s: Server; compact?: boolean; onClick?: () => void }) {
  const { t } = useI18n()
  if (!s.gre_enabled || !s.gre) return null
  const m = greMark(s.gre, s.online, t)
  const tip = [...m.lines, onClick ? t('Нажмите: подробнее в разделе Сеть') : ''].filter(Boolean).join('\n')
  return (
    <span
      className={`role-chip gre-chip gre-${m.tone}${onClick ? ' clickable' : ''}`}
      title={tip}
      onClick={onClick && ((e) => { e.stopPropagation(); onClick() })}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        {/* вход в туннель */}
        <path d="M3 20v-7a9 9 0 0 1 18 0v7" />
        <path d="M8 20v-6a4 4 0 0 1 8 0v6" />
        <path d="M2 20h20" />
      </svg>
      <span className="role-chip-t">{compact ? m.short : m.label}</span>
    </span>
  )
}

// Карточка в разделе "Сеть": состояние туннеля, путь агента, адреса, MTU и замеры канала
export function GreCard({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const g = s.gre
  if (!s.gre_enabled || !g) return null
  const m = greMark(g, s.online, t)
  const tone: Tone = m.tone === 'ok' ? 'ok' : m.tone === 'warn' ? 'warn' : 'muted'
  const alive = g.up && g.state !== 'stale' && g.state !== 'nat' && g.state !== 'error'
  const p = g.probe
  const rows: [string, string][] = [
    [t('На сервере'), `${g.node_iface} ${g.node_addr || '?'}`],
    [t('На хосте панели'), `${g.iface} ${g.panel_addr || '?'}`],
    [t('Хост панели'), [g.panel_ip || t('туннель еще не поднял'), g.host_at && t('отвечал {ago}', { ago: ago(g.host_at, t) })].filter(Boolean).join(', ')],
    [t('Внешний адрес сервера'), g.peer || '—'],
    [t('Сеть и MTU'), `${g.net || '?'}, MTU ${g.mtu}`],
    [t('Агент знает туннель'), g.connect ? `connect=${g.connect}${g.connect_down ? `, ${t('сейчас не отвечает')}` : ''}` : t('нет (connect= не задан)')],
    [t('Фаервол'), [g.firewall && t('на сервере {fw}', { fw: g.firewall }), g.host_fw && t('на хосте {fw}', { fw: g.host_fw })].filter(Boolean).join(', ') || '—'],
  ]
  return (
    <div className="chart-card net-card" id="mcard-gre">
      <div className="net-head">
        <div className="chart-cap">{t('GRE до панели')}</div>
      </div>
      <div className={`gre-card-head${tone === 'ok' ? '' : ` net-tone-${tone}`}`}>{m.lines[0]}</div>
      <div className="net-tiles">
        <NetTile
          label={t('Путь последнего отчета')}
          value={g.via === 'gre' ? t('через GRE') : g.via === 'direct' ? t('напрямую') : '—'}
          sub={g.via_since ? t('с {at}', { at: at(g.via_since) }) : undefined}
          tone={tone}
        />
        <NetTile
          label={t('Туннель')}
          value={alive ? t('жив') : SETUP.has(g.state) ? t('настраивается') : t('не работает')}
          sub={g.seen_at ? t('helper {h}, проверял {ago}', { h: g.helper || 'gre-setup', ago: ago(g.seen_at, t) }) : t('helper еще не обращался')}
          tone={alive ? 'ok' : SETUP.has(g.state) ? 'muted' : 'warn'}
          title={alive ? undefined : whyDirect(g, t)}
        />
        <NetTile
          label={t('Отчет через туннель')}
          value={g.agent_at ? ago(g.agent_at, t) : t('не было')}
          tone={g.via === 'gre' ? 'ok' : 'muted'}
        />
        {p && p.big_n > 0 && (
          <NetTile
            label={t('Куски по {kb} КБ от панели', { kb: p.big_kb || 100 })}
            value={`${p.big_ok}/${p.big_n}`}
            sub={t('дошли, мелкие {ok}/{n}', { ok: p.small_ok, n: p.small_n })}
            tone={p.big_ok * 4 <= p.big_n ? 'warn' : 'ok'}
            title={t('Замеры агента раз в 30 минут, своими соединениями: по ним панель и советует GRE')}
          />
        )}
      </div>
      <div className="loc-results gre-rows">
        {rows.map(([k, v]) => (
          <div className="loc-res" key={k}>
            <div className="loc-res-name">{k}</div>
            <div className="loc-res-msg mono">{v}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
