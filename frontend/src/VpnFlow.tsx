import { useState } from 'react'
import { muteServerKeys, snoozeServerAlert, type Server, type VpnFlowClient, type VpnFlowInfo } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { TORRENT_MUTE, VPNHEAVY_MUTE } from './mutes'
import { toastError } from './toast'
import { fmtBytes } from './units'

// Клиенты VPN (app/vpn_flow.py): кто, похоже, качает торрент (сотни разных адресов на высоких портах
// по conntrack netns VPN) и кто прокачал много за 3 часа (счетчики из списка клиентов). Строка на
// клиента: имя, внутренний адрес, откуда подключен, потоки и адреса сейчас, трафик за 3 часа.
// 🔕 глушит оба алерта по этому клиенту.

const SHOW = 8
const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-degraded': ' lk-dot-warn', 't-soon': '' }

function fmtUntil(ts: string): string {
  return new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function VpnFlowCard({ server: s, info, onChanged }: { server: Server; info: VpnFlowInfo; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [all, setAll] = useState(false)
  const [picking, setPicking] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const rows = info.clients.filter((x) => (x.flows ?? 0) > 0 || (x.vol ?? 0) > 0 || x.mute_t || x.mute_h)
  if (!rows.length && !info.skipped.length) return null
  const kinds = (key: string) => [TORRENT_MUTE + key, VPNHEAVY_MUTE + key]
  const mute = async (key: string, hours: number) => {
    setBusy(true)
    try {
      if (hours < 0) await muteServerKeys(s.id, kinds(key))
      else if (hours === 0) {
        const perm = kinds(key).filter((k) => (s.alert_mutes ?? []).includes(k))
        if (perm.length) await muteServerKeys(s.id, [], perm)
        for (const k of kinds(key)) if (s.alert_snoozes?.[k]) await snoozeServerAlert(s.id, k, 0)
      } else for (const k of kinds(key)) await snoozeServerAlert(s.id, k, hours)
      setPicking(null)
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  const hours = Math.round(info.heavy_window / 3600)
  const line = (x: VpnFlowClient) => {
    const out: string[] = []
    if ((x.flows ?? 0) > 0) {
      // главное для торрента - первым: на телефоне хвост строки переносится
      if ((x.hp ?? 0) > 0) out.push(t('на высоких портах {n}', { n: x.hp! }))
      if ((x.bt ?? 0) > 0) out.push(t('на портах BitTorrent {n}', { n: x.bt! }))
      out.push(t('адресов {n}', { n: x.peers ?? 0 }))
      out.push(t('потоков {n}', { n: x.flows! }))
      out.push(t('UDP {p}%', { p: Math.round((100 * (x.udp ?? 0)) / x.flows!) }))
    }
    return out.join(', ')
  }
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-vpnflow">
      <div className="net-head">
        <div className="chart-cap">{t('Клиенты VPN: торрент и трафик')}</div>
        <span className="muted small">{t('за {n} ч', { n: hours })}</span>
      </div>
      <div className="loc-results docker-clist">
        {(all ? rows : rows.slice(0, SHOW)).map((x) => {
          const m = x.mute_t || x.mute_h
          const bad = (x.torrent || x.heavy) && !m
          const tone = bad ? 't-degraded' : (x.hot ?? 0) > 0 ? 't-soon' : 't-up'
          return (
            <div key={x.key} className={`loc-res docker-row ${tone}${m ? ' q-muted' : ''}`}>
              <span className={`lk-dot${m ? '' : DOT[tone]}`} />
              <div className="docker-c-main">
                <div className="docker-c-name mono">{x.name || x.ip}</div>
                <div className="docker-c-img muted small">
                  {[x.name ? x.ip : '', x.ep ? t('с {ip}', { ip: x.ep }) : '', x.src].filter(Boolean).join(', ')}
                </div>
                {line(x) && <div className="docker-c-img vpnf-wrap muted small">{line(x)}</div>}
                {(x.vol ?? 0) > 0 && (
                  <div className="docker-c-img muted small">
                    {t('прокачал {v} за {h} ч', { v: fmtBytes(x.vol), h: Math.max(1, Math.round((x.span ?? 0) / 3600)) })}
                  </div>
                )}
                {x.torrent && (
                  <div className={`docker-c-img small${m ? ' muted' : ' q-stuck'}`}>
                    {t('похоже на торрент: {n} мин из 15 к сотням разных адресов на высоких портах', { n: x.hot ?? 0 })}
                  </div>
                )}
                {!x.torrent && (x.hot ?? 0) > 0 && (
                  <div className="docker-c-img vpnf-wrap muted small">
                    {t('много пиров {n} мин из 15, алерт от {m} мин', { n: x.hot!, m: info.torrent_min })}
                  </div>
                )}
                {x.heavy && (
                  <div className={`docker-c-img small${m ? ' muted' : ' q-stuck'}`}>
                    {t('качает очень много: больше {v} за {h} ч', { v: fmtBytes(info.heavy_bytes), h: hours })}
                  </div>
                )}
                {m && (
                  <div className="small">
                    <span className="snooze-active-chip snooze-perm">
                      🔇 {m.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmtUntil(m.until!) }) })}
                      {!isViewer && (
                        <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(x.key, 0)}>
                          ✕
                        </button>
                      )}
                    </span>
                  </div>
                )}
                {!m && picking === x.key && (
                  <div className="small q-mute-pick">
                    <span className="muted">🔕 {t('не алертить об этом клиенте:')}</span>
                    {[
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={busy} onClick={() => mute(x.key, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={busy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </div>
                )}
              </div>
              {!isViewer && !m ? (
                <button className="ghost small q-mute-btn" disabled={busy} title={t('Не алертить о клиенте VPN: на день, неделю или постоянно')}
                  onClick={() => setPicking(picking === x.key ? null : x.key)}>
                  🔕
                </button>
              ) : (
                <span className="q-mute-btn" />
              )}
            </div>
          )
        })}
      </div>
      {rows.length > SHOW && (
        <button className="ghost small ub-more" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : `${t('показать все')} (${rows.length})`}
        </button>
      )}
      <div className="net-foot">
        {info.skipped.map((x) => (
          <div key={x.where} className="q-stuck">
            {x.why === 'toomany'
              ? t('{w}: conntrack не прочитан, в таблице {n} потоков', { w: x.where, n: x.count.toLocaleString() })
              : t('{w}: conntrack не прочитан ({why})', { w: x.where, why: x.why })}
          </div>
        ))}
        {!info.helper && <div>{t('Признаки торрента не видны: на сервере нет helper vpnflow-setup. Видно только трафик клиентов.')}</div>}
        <div>
          {t(
            'Торрент виден по таблице conntrack внутри VPN: у клиента сотни разных адресов на случайных высоких портах, в основном UDP. У браузера и мессенджеров десятки адресов, почти все на 443. Алерт - когда так держится {m} минут из 15. Клиентов Xray так не видно (соединения открывает сам xray), у них только трафик.',
            { m: info.torrent_min },
          )}
        </div>
      </div>
    </div>
  )
}
