import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError, getInventory, type InvServer } from './api'
import { useI18n } from './i18n'
import {
  byGroup,
  countBy,
  cpuName,
  diskGroupText,
  diskGroups,
  fmtRam,
  fmtRamGb,
  fmtSize,
  ghz,
  gpuShort,
  inventoryXlsx,
  invTotals,
  kernelShort,
  KINDS,
  kindLabel,
  osShort,
  raidLabels,
  ramGb,
  saveBlob,
  serverKind,
  uptimeDays,
  type DiskKind,
  type InvTotals,
} from './inventoryData'
import { inventoryHtml } from './inventoryHtml'

// Раздел "Инвентаризация" в окне "Отчет": что стоит на серверах (backend app/inventory.py).
// Сверху плитки итогов, ниже одна плотная таблица: группы - строками-заголовками с подытогами,
// сворачиваются, у группы и у каждого сервера галка - снятые бледнеют и выпадают из итогов и
// файлов. Группы выбираются выпадающим списком в строке с фильтрами. Краткий - строка на
// сервер, полный - модели процессоров и дисков, RAID меткой рядом с дисками, GPU, ядро

type Props = { brand: string; onOpen: (id: number) => void; onUnauthorized: () => void }

const NO_GROUP = '\u0000'
const gkey = (s: InvServer) => s.group || NO_GROUP

function Disk({ kind, children }: { kind: DiskKind; children: ReactNode }) {
  return <span className={`inv-dk inv-k-${kind || 'unk'}`}>{children}</span>
}

// галка группы: все, никого или часть
function GroupBox({ on, n, onChange }: { on: number; n: number; onChange: () => void }) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = on > 0 && on < n
  }, [on, n])
  return <input ref={ref} type="checkbox" checked={on === n} onChange={onChange} onClick={(e) => e.stopPropagation()} />
}

export function Inventory({ brand, onOpen, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [all, setAll] = useState<InvServer[] | null>(null)
  const [err, setErr] = useState('')
  // пусто - все группы
  const [groups, setGroups] = useState<Set<string>>(new Set())
  const [kind, setKind] = useState<'' | 'bare' | 'vm'>('')
  const [withGpu, setWithGpu] = useState(false)
  const [q, setQ] = useState('')
  const [off, setOff] = useState<Set<number>>(new Set())
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const [pop, setPop] = useState(false)
  const [full, setFull] = useState(false)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    getInventory()
      .then((r) => alive && setAll(r.servers))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (alive) setErr(String((e as Error).message || e))
      })
    return () => {
      alive = false
    }
  }, [onUnauthorized])

  // список групп закрывается кликом мимо
  useEffect(() => {
    if (!pop) return
    const h = (e: MouseEvent) => {
      if (!popRef.current?.contains(e.target as Node)) setPop(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [pop])

  const groupList = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of all ?? []) m.set(gkey(s), (m.get(gkey(s)) ?? 0) + 1)
    return [...m.entries()]
  }, [all])

  const base = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (all ?? []).filter(
      (s) =>
        (!groups.size || groups.has(gkey(s))) &&
        (!kind || (kind === 'vm') === !!s.virt) &&
        (!withGpu || s.gpus.length > 0) &&
        (!needle ||
          [s.name, s.group, s.ip, s.local_ip, s.cpu_model, s.os, s.country, ...s.gpus].join(' ').toLowerCase().includes(needle)),
    )
  }, [all, groups, kind, withGpu, q])
  const list = base.filter((s) => !off.has(s.id))
  const tot = invTotals(list)

  if (err) return <p className="form-error">{err}</p>
  if (!all) return <p className="muted small">{t('загрузка…')}</p>
  if (!all.length) return <p className="muted small">{t('Серверов нет.')}</p>

  const gname = (g: string) => (g === NO_GROUP ? t('без группы') : g)
  const toggleGroup = (g: string) => {
    const next = new Set(groups)
    if (next.has(g)) next.delete(g)
    else next.add(g)
    setGroups(next.size === groupList.length ? new Set() : next)
  }
  const toggleOff = (ids: number[]) => {
    const next = new Set(off)
    const allOn = ids.every((id) => !next.has(id))
    for (const id of ids) {
      if (allOn) next.add(id)
      else next.delete(id)
    }
    setOff(next)
  }
  const toggleClosed = (g: string) => {
    const next = new Set(closed)
    if (next.has(g)) next.delete(g)
    else next.add(g)
    setClosed(next)
  }
  // что выбрано - строкой в шапку файла
  const scope = [
    groups.size ? `${t('Группы')}: ${[...groups].map(gname).join(', ')}` : '',
    kind === 'bare' ? t('только Baremetal') : kind === 'vm' ? t('только ВМ') : '',
    withGpu ? t('с GPU') : '',
    q.trim() ? `${t('поиск')}: ${q.trim()}` : '',
  ]
    .filter(Boolean)
    .join(', ')
  const stamp = new Date().toISOString().slice(0, 10)
  const fileName = (ext: string) => `inventory-${stamp}${full ? '-full' : ''}.${ext}`
  const downloadHtml = () =>
    saveBlob(new Blob([inventoryHtml(list, full, scope, t, brand)], { type: 'text/html;charset=utf-8' }), fileName('html'))
  const downloadXlsx = () => saveBlob(inventoryXlsx(list, full, t), fileName('xlsx'))

  const cols = full ? 8 : 7
  const noData = <span className="muted small">{t('агент еще не присылал данные')}</span>
  const unk = (s: InvServer) => (
    <Disk kind="">
      {fmtSize(s.fs_total)} <span className="inv-warn">{t('по разделам')}</span>
    </Disk>
  )
  const totDisks = (x: InvTotals, count: boolean) =>
    KINDS.filter((k) => x.disks[k]).map((k) => (
      <Disk key={k} kind={k}>
        {count ? `${x.disks[k]!.n}x · ` : ''}
        {fmtSize(x.disks[k]!.size)} {kindLabel(k, t)}
      </Disk>
    ))
  const nameCell = (s: InvServer) => (
    <>
      <button className="linklike inv-name" onClick={() => onOpen(s.id)}>
        {s.name}
      </button>
      {s.reported && !s.online && <i className="inv-off" title={t('не на связи')} />}
    </>
  )

  const shortCells = (s: InvServer) => (
    <>
      <td className="nowrap">{nameCell(s)}</td>
      <td className="nowrap inv-ip">{s.ip}</td>
      {s.reported ? (
        <>
          <td className="num">{s.threads || ''}</td>
          <td className="num">{s.mem ? fmtRam(s.mem) : ''}</td>
          <td>
            <div className="inv-dks">
              {s.disks ? diskGroups(s).map((g, i) => <Disk key={i} kind={g.kind}>{diskGroupText(g, t)}</Disk>) : unk(s)}
            </div>
          </td>
          <td className="nowrap">{osShort(s.os)}</td>
        </>
      ) : (
        <td colSpan={4}>{noData}</td>
      )}
    </>
  )

  const fullCells = (s: InvServer) => {
    const gpus = Object.entries(countBy(s.gpus))
    const raid = raidLabels(s.raid).map((r) => (
      <span key={r.text} className={`inv-raid${r.bad ? ' bad' : ''}`}>
        {r.text}
      </span>
    ))
    return (
      <>
        <td className="nowrap">
          {nameCell(s)}
          {s.reported && <span className={`inv-kind${s.virt ? '' : ' bm'}`}>{s.virt ? serverKind(s, t) : 'BM'}</span>}
          <span className="inv-sub inv-ip">
            {s.ip}
            {s.country && ` · ${s.country}`}
            {s.local_ip && `, ${s.local_ip}`}
          </span>
        </td>
        {!s.reported ? (
          <td colSpan={6}>{noData}</td>
        ) : (
          <>
            <td className="num">
              {s.threads || ''}
              <span className="inv-sub">{s.virt ? 'vCPU' : t('пот.')}</span>
            </td>
            <td className="nowrap">
              {cpuName(s.cpu_model)}
              <span className="inv-sub">
                {ghz(s.freq_mhz, t)}
                {gpus.length > 0 && (
                  <span className="inv-gpu">GPU {gpus.map(([g, n]) => `${n > 1 ? `${n}x ` : ''}${gpuShort(g)}`).join(', ')}</span>
                )}
              </span>
            </td>
            <td className="num">{s.mem ? fmtRam(s.mem) : ''}</td>
            <td className="nowrap">
              {s.disks ? (
                diskGroups(s, true).map((g, i) => (
                  <div key={i}>
                    <Disk kind={g.kind}>{diskGroupText(g, t)}</Disk>
                    {i === 0 && raid}
                    {g.model && <span className="inv-dmod">{g.model}</span>}
                  </div>
                ))
              ) : (
                <>
                  {unk(s)}
                  {raid}
                  <span className="inv-dmod">{t('модели не видны: нет helper disk-health')}</span>
                </>
              )}
            </td>
            <td className="nowrap">
              {osShort(s.os)}
              {s.kernel && <span className="inv-sub inv-ip">{kernelShort(s.kernel)}</span>}
            </td>
            <td className="num">{t('{n} дн', { n: uptimeDays(s) })}</td>
          </>
        )}
      </>
    )
  }

  const sums = (x: InvTotals) =>
    full ? (
      <>
        <td className="num">{x.threads}</td>
        <td />
        <td className="num">{fmtRamGb(x.mem)}</td>
        <td colSpan={3}>
          <div className="inv-dks">{totDisks(x, false)}</div>
        </td>
      </>
    ) : (
      <>
        <td className="num">{x.threads}</td>
        <td className="num">{fmtRamGb(x.mem)}</td>
        <td colSpan={2}>
          <div className="inv-dks">{totDisks(x, false)}</div>
        </td>
      </>
    )

  const gpuTot = Object.entries(tot.gpus)
  const diskAll = KINDS.reduce((a, k) => a + (tot.disks[k]?.size ?? 0), 0) || 1
  const rams = list.filter((s) => s.mem).map((s) => ramGb(s.mem))
  const kindN = (vm: boolean) => all.filter((s) => s.reported && !!s.virt === vm).length
  const gsel = groups.size ? [...groups].map(gname).join(', ') : t('все')

  return (
    <div className="inv">
      <div className="inv-top">
        <div className="win-switch">
          <button className={`win-btn${!full ? ' win-btn-active' : ''}`} onClick={() => setFull(false)}>
            {t('Краткий')}
          </button>
          <button className={`win-btn${full ? ' win-btn-active' : ''}`} onClick={() => setFull(true)}>
            {t('Полный')}
          </button>
        </div>
        <span className="inv-grow" />
        <button
          className="ghost"
          disabled={!list.length}
          title={t('Одним HTML-файлом: открывается в любом браузере, можно переслать или распечатать')}
          onClick={downloadHtml}
        >
          {t('Скачать HTML')}
        </button>
        <button disabled={!list.length} onClick={downloadXlsx}>
          {t('Скачать Excel')}
        </button>
      </div>

      <div className="inv-bar">
        <div className="inv-dd" ref={popRef}>
          <button className="ghost inv-dd-btn" onClick={() => setPop(!pop)}>
            {t('Группы')}: <b>{gsel}</b> ▾
          </button>
          {pop && (
            <div className="inv-pop">
              <label>
                <input type="checkbox" checked={!groups.size} onChange={() => setGroups(new Set())} /> {t('Все группы')}
                <i>{all.length}</i>
              </label>
              <div className="inv-pop-sep" />
              {groupList.map(([g, n]) => (
                <label key={g}>
                  <input type="checkbox" checked={groups.has(g)} onChange={() => toggleGroup(g)} /> {gname(g)}
                  <i>{n}</i>
                </label>
              ))}
            </div>
          )}
        </div>
        <div className="win-switch">
          <button className={`win-btn${!kind ? ' win-btn-active' : ''}`} onClick={() => setKind('')}>
            {t('Все')}
          </button>
          <button className={`win-btn${kind === 'bare' ? ' win-btn-active' : ''}`} onClick={() => setKind('bare')}>
            Baremetal <i className="inv-cnt">{kindN(false)}</i>
          </button>
          <button className={`win-btn${kind === 'vm' ? ' win-btn-active' : ''}`} onClick={() => setKind('vm')}>
            {t('ВМ')} <i className="inv-cnt">{kindN(true)}</i>
          </button>
        </div>
        <div className="win-switch">
          <button className={`win-btn${withGpu ? ' win-btn-active' : ''}`} onClick={() => setWithGpu(!withGpu)}>
            {t('с GPU')}
          </button>
        </div>
        <input className="srv-search inv-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('имя, IP, CPU, ОС, страна')} />
        <span className="inv-grow" />
        <span className="muted small">
          {t('{n} из {m}', { n: list.length, m: all.length })}
          {base.length > list.length && `, ${t('снято {n}', { n: base.length - list.length })}`}
        </span>
      </div>

      <div className="inv-tiles">
        <div className="inv-tile">
          <span className="k">{t('Серверы')}</span>
          <b>{tot.n}</b>
          <span className="d">
            Baremetal {tot.bare} · {t('ВМ')} {tot.vm}
          </span>
        </div>
        <div className="inv-tile">
          <span className="k">CPU</span>
          <b>{tot.threads}</b>
          <span className="d">{t('потоков всего')}</span>
        </div>
        <div className="inv-tile">
          <span className="k">RAM</span>
          <b>{fmtRamGb(tot.mem)}</b>
          <span className="d">{rams.length > 1 ? `${fmtRamGb(Math.min(...rams))} - ${fmtRamGb(Math.max(...rams))}` : ''}</span>
        </div>
        {KINDS.filter((k) => tot.disks[k]).map((k) => (
          <div key={k} className={`inv-tile disk inv-k-${k || 'unk'}`}>
            <span className="k">{kindLabel(k, t)}</span>
            <b>{fmtSize(tot.disks[k]!.size)}</b>
            <span className="d">{t('дисков: {n}', { n: tot.disks[k]!.n })}</span>
            <span className="inv-share">
              <i style={{ width: `${(100 * tot.disks[k]!.size) / diskAll}%` }} />
            </span>
          </div>
        ))}
        {gpuTot.length > 0 && (
          <div className="inv-tile disk inv-k-gpu">
            <span className="k">GPU</span>
            <b>{gpuTot.reduce((a, [, n]) => a + n, 0)}</b>
            <span className="d">{gpuTot.map(([g, n]) => `${n}x ${gpuShort(g)}`).join(', ')}</span>
          </div>
        )}
        {tot.unknown > 0 && (
          <div className="inv-tile disk inv-k-warn" title={t('Нет helper disk-health: объем по разделам, тип неизвестен')}>
            <span className="k">{t('Диски неизвестны')}</span>
            <b>{tot.unknown}</b>
            <span className="d">{t('нет helper disk-health')}</span>
          </div>
        )}
      </div>

      {!base.length ? (
        <p className="muted small">{t('Ничего не выбрано')}</p>
      ) : (
        <div className="inv-tw">
          <table className="inv-tbl">
            <thead>
              {full ? (
                <tr>
                  <th className="inv-cb" />
                  <th>{t('Сервер / IP')}</th>
                  <th className="num">CPU</th>
                  <th>{t('Процессор')}</th>
                  <th className="num">RAM</th>
                  <th>{t('Диски и RAID')}</th>
                  <th>{t('ОС / ядро')}</th>
                  <th className="num">{t('Аптайм')}</th>
                </tr>
              ) : (
                <tr>
                  <th className="inv-cb" />
                  <th>{t('Сервер')}</th>
                  <th>IP</th>
                  <th className="num">CPU</th>
                  <th className="num">RAM</th>
                  <th>{t('Диски')}</th>
                  <th>{t('ОС')}</th>
                </tr>
              )}
            </thead>
            <tbody>
              {byGroup(base, gkey).map(([g, items]) => {
                const on = items.filter((s) => !off.has(s.id))
                const shut = closed.has(g)
                return (
                  <Fragment key={g}>
                    <tr className="inv-gh" onClick={() => toggleClosed(g)}>
                      <td className="inv-cb">
                        <GroupBox on={on.length} n={items.length} onChange={() => toggleOff(items.map((s) => s.id))} />
                      </td>
                      <td colSpan={full ? 1 : 2}>
                        <span className="inv-car">{shut ? '▸' : '▾'}</span>
                        {gname(g)} <span className="muted">· {t('{n} серв.', { n: items.length })}</span>
                      </td>
                      {sums(invTotals(on))}
                    </tr>
                    {!shut &&
                      items.map((s) => (
                        <tr key={s.id} className={`inv-r${off.has(s.id) ? ' off' : ''}`}>
                          <td className="inv-cb">
                            <input type="checkbox" checked={!off.has(s.id)} onChange={() => toggleOff([s.id])} />
                          </td>
                          {full ? fullCells(s) : shortCells(s)}
                        </tr>
                      ))}
                  </Fragment>
                )
              })}
            </tbody>
            <tfoot>
              <tr>
                <td className="inv-cb" />
                <td colSpan={full ? 1 : 2}>{t('Итого: {n} серв.', { n: tot.n })}</td>
                {full ? (
                  <>
                    <td className="num">{tot.threads}</td>
                    <td />
                    <td className="num">{fmtRamGb(tot.mem)}</td>
                    <td colSpan={cols - 5}>
                      <div className="inv-dks">{totDisks(tot, true)}</div>
                    </td>
                  </>
                ) : (
                  <>
                    <td className="num">{tot.threads}</td>
                    <td className="num">{fmtRamGb(tot.mem)}</td>
                    <td colSpan={2}>
                      <div className="inv-dks">{totDisks(tot, true)}</div>
                    </td>
                  </>
                )}
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </div>
  )
}
