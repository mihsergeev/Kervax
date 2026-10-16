import type { PanelRequests } from './api'

// раздел "Запросы к панели" окна Пользователи: кто сейчас шлет в панель левые запросы (это же
// уходит алертом и в "Что сломано" у сервера) и кто за сутки слал отказы или всплески

type T = (s: string, p?: Record<string, string | number>) => string

const when = (iso: string) => {
  const d = new Date(iso)
  const day = d.toDateString() === new Date().toDateString()
  return d.toLocaleString([], day ? { hour: '2-digit', minute: '2-digit' } : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function PanelRequestsSec({ data, t }: { data: PanelRequests; t: T }) {
  return (
    <>
      <p className="muted small al-lead">
        {t('Панель считает запросы к своему API по адресам. Алерт - когда с одного адреса за {w} минут пришло {bad} отказов (неверный токен агента, удаленный push, перебор адресов) или больше {flood} запросов на каждый сервер за этим адресом, обычный агент шлет около {typ}. Неудачные входы считает защита входа.', {
          w: data.window_min,
          bad: data.bad_limit,
          flood: data.flood_per_server,
          typ: data.typical_per_server,
        })}
      </p>

      <div className="srv-nav-grp rt-cap">{t('Сейчас')}</div>
      {data.active.length ? (
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Откуда')}</th>
              <th>{t('Что')}</th>
              <th className="hide-m">{t('С')}</th>
            </tr>
          </thead>
          <tbody>
            {data.active.map((a) => (
              <tr key={a.key}>
                <td>
                  {a.servers.length === 1 ? a.servers[0].name : <span className="mono">{a.key}</span>}
                  <div className="small">
                    <span className="muted">{a.servers.length === 1 ? a.key : a.servers.length ? t('серверы: {s}', { s: a.servers.map((s) => s.name).join(', ') }) : t('не сервер панели')}</span>
                  </div>
                </td>
                <td>
                  <span className="t-down">{a.what}</span>
                  {a.hint && <div className="small">{a.hint}</div>}
                </td>
                <td className="hide-m nowrap">{a.since ? when(a.since) : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">{t('Сейчас левых запросов нет.')}</p>
      )}

      <div className="srv-nav-grp rt-cap">{t('За сутки')}</div>
      {data.sources.length ? (
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Откуда')}</th>
              <th>{t('Отказов')}</th>
              <th className="hide-m">{t('Пик в минуту')}</th>
              <th>{t('Чаще всего')}</th>
              <th className="hide-m">{t('Последний')}</th>
            </tr>
          </thead>
          <tbody>
            {data.sources.map((r) => (
              <tr key={r.key}>
                <td>
                  {r.servers.length === 1 ? r.servers[0].name : <span className="mono">{r.key}</span>}
                  {r.active && <span className="type-chip lg-banned">{t('сейчас')}</span>}
                  <div className="small">
                    <span className="muted">{r.servers.length === 1 ? r.key : r.servers.length ? t('серверы: {s}', { s: r.servers.map((s) => s.name).join(', ') }) : t('не сервер панели')}</span>
                  </div>
                </td>
                <td>
                  {r.bad || <span className="muted">0</span>}
                  {r.bad > 0 && (
                    <div className="small">
                      <span className="muted">{Object.entries(r.codes).map(([c, n]) => `${c}: ${n}`).join(', ')}</span>
                    </div>
                  )}
                </td>
                <td className="hide-m">{r.peak}</td>
                <td className="small">
                  {r.paths.slice(0, 3).map(([p, n]) => (
                    <div key={p} className="mono">
                      {p} <span className="muted">· {n}</span>
                    </div>
                  ))}
                  {r.hint && !r.active && <div>{r.hint}</div>}
                </td>
                <td className="hide-m nowrap">{when(r.last)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">{t('За сутки отказов и всплесков не было.')}</p>
      )}
      {data.more > 0 && <p className="muted small">{t('и еще адресов: {n}', { n: data.more })}</p>}
    </>
  )
}
