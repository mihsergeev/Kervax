import { useEffect, useMemo, useState } from 'react'
import { ApiError, getInventory, type InvServer } from './api'
import { useI18n } from './i18n'
import {
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
  raidText,
  saveBlob,
  serverKind,
  uptimeDays,
  type DiskKind,
} from './inventoryData'
import { inventoryHtml } from './inventoryHtml'
import { NetTile } from './NetLinks'

// Раздел "Инвентаризация" в окне "Отчет": что стоит на серверах (backend app/inventory.py).
// Выбор - группами, фильтрами и поиском, лишние снимаются по одному. Краткий отчет - строка на
// сервер, полный - модели процессоров и дисков, RAID, GPU, ядро. Скачать - HTML или Excel

type Props = { brand: string; onOpen: (id: number) => void; onUnauthorized: () => void }

const NO_GROUP = '\u0000'

function Disk({ kind, text, model }: { kind: DiskKind; text: string; model?: string }) {
  return (
    <span className="inv-disk">
      <i className={`inv-dt inv-dt-${kind || 'unk'}`} />
      {text}
      {model && <span className="muted mono"> {model}</span>}
    </span>
  )
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
  const [showList, setShowList] = useState(false)
  const [full, setFull] = useState(false)

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

  const groupList = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of all ?? []) m.set(s.group || NO_GROUP, (m.get(s.group || NO_GROUP) ?? 0) + 1)
    return [...m.entries()]
  }, [all])

  const base = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (all ?? []).filter(
      (s) =>
        (!groups.size || groups.has(s.group || NO_GROUP)) &&
        (!kind || (kind === 'vm') === !!s.virt) &&
        (!withGpu || s.gpus.length > 0) &&
        (!needle ||
          [s.name, s.group, s.ip, s.local_ip, s.cpu_model, s.os, s.country, ...s.gpus].join(' ').toLowerCase().includes(needle)),
    )
  }, [all, groups, kind, withGpu, q])
  const list = base.filter((s) => !off.has(s.id))
  const tot = invTotals(list)
  const removed = base.length - list.length

  if (err) return <p className="form-error">{err}</p>
  if (!all) return <p className="muted small">{t('загрузка…')}</p>
  if (!all.length) return <p className="muted small">{t('Серверов нет.')}</p>

  const toggleGroup = (g: string) => {
    const next = new Set(groups)
    if (next.has(g)) next.delete(g)
    else next.add(g)
    setGroups(next.size === groupList.length ? new Set() : next)
  }
  const toggleOff = (id: number) => {
    const next = new Set(off)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setOff(next)
  }
  // что выбрано - строкой в шапку файла
  const scope = [
    groups.size ? `${t('Группы')}: ${[...groups].map((g) => (g === NO_GROUP ? t('без группы') : g)).join(', ')}` : '',
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

  const name = (s: InvServer) => (
    <button className="linklike inv-name" onClick={() => onOpen(s.id)}>
      {s.name}
    </button>
  )
  const disksShort = (s: InvServer) =>
    !s.reported ? null : s.disks ? (
      diskGroups(s).map((g, i) => <Disk key={i} kind={g.kind} text={diskGroupText(g, t)} />)
    ) : (
      <span className="inv-disk" title={t('Нет helper disk-health: объем по разделам, тип неизвестен')}>
        <i className="inv-dt inv-dt-unk" />
        {fmtSize(s.fs_total)} <span className="muted">{t('по разделам')}</span>
      </span>
    )
  const noData = <span className="muted small">{t('агент еще не присылал данные')}</span>
  const anyRaid = list.some((s) => s.raid.length > 0)

  const shortTable = (
    <table className="sec-tbl inv-tbl">
      <thead>
        <tr>
          <th>{t('Сервер')}</th>
          <th>IP</th>
          <th className="num">CPU</th>
          <th className="num">RAM</th>
          <th>{t('Диски')}</th>
          <th>{t('ОС')}</th>
        </tr>
      </thead>
      <tbody>
        {list.map((s) => (
          <tr key={s.id}>
            <td className="nowrap">
              {name(s)} {s.group && <span className="muted small">{s.group}</span>}
            </td>
            <td className="nowrap mono">
              <span className="muted">{s.ip}</span>
            </td>
            {s.reported ? (
              <>
                <td className="num">{s.threads || ''}</td>
                <td className="num">{s.mem ? fmtRam(s.mem) : ''}</td>
                <td>
                  <div className="inv-disks">{disksShort(s)}</div>
                </td>
                <td className="nowrap">{osShort(s.os)}</td>
              </>
            ) : (
              <td colSpan={4}>{noData}</td>
            )}
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={2}>{t('Итого: {n}', { n: tot.n })}</td>
          <td className="num">{tot.threads}</td>
          <td className="num">{fmtRamGb(tot.mem)}</td>
          <td colSpan={2}>
            <div className="inv-disks">
              {KINDS.filter((k) => tot.disks[k]).map((k) => (
                <Disk key={k} kind={k} text={`${tot.disks[k]!.n}x, ${fmtSize(tot.disks[k]!.size)} ${kindLabel(k, t)}`} />
              ))}
            </div>
          </td>
        </tr>
      </tfoot>
    </table>
  )

  const fullTable = (
    <table className="sec-tbl inv-tbl inv-full">
      <thead>
        <tr>
          <th>{t('Сервер / IP')}</th>
          <th>{t('Процессор')}</th>
          <th className="num">RAM</th>
          <th>{t('Диски')}</th>
          {anyRaid && <th>RAID</th>}
          <th>{t('ОС / ядро')}</th>
          <th className="num">{t('Аптайм')}</th>
        </tr>
      </thead>
      <tbody>
        {list.map((s) => {
          const gpus = Object.entries(countBy(s.gpus))
          return (
            <tr key={s.id}>
              <td className="nowrap">
                {name(s)} {s.group && <span className="muted small">{s.group}</span>}{' '}
                {s.reported && <span className={`type-chip${s.virt ? '' : ' inv-bare'}`}>{serverKind(s, t)}</span>}
                {!s.online && s.reported && <i className="inv-off" title={t('не на связи')} />}
                <div className="mono muted small">
                  {s.ip}
                  {s.country && ` ${s.country}`}
                  {s.local_ip && `, ${s.local_ip}`}
                </div>
              </td>
              {!s.reported ? (
                <td colSpan={anyRaid ? 6 : 5}>{noData}</td>
              ) : (
                <>
                  <td className="nowrap">
                    {cpuName(s.cpu_model)}{' '}
                    <span className="muted">
                      {[s.virt ? `${s.threads} vCPU` : t('{n} пот.', { n: s.threads }), ghz(s.freq_mhz, t)].filter(Boolean).join(', ')}
                    </span>
                    {gpus.length > 0 && (
                      <div>
                        <span className="muted">GPU</span> {gpus.map(([g, n]) => `${n > 1 ? `${n}x ` : ''}${gpuShort(g)}`).join(', ')}
                      </div>
                    )}
                  </td>
                  <td className="num">{s.mem ? fmtRam(s.mem) : ''}</td>
                  <td className="nowrap">
                    <div className="inv-disks inv-col">
                      {s.disks
                        ? diskGroups(s, true).map((g, i) => <Disk key={i} kind={g.kind} text={diskGroupText(g, t)} model={g.model} />)
                        : disksShort(s)}
                    </div>
                  </td>
                  {anyRaid && (
                    <td className="nowrap">
                      {s.raid.map((r) => (
                        <div key={r.dev} className={r.degraded ? 'inv-bad' : ''} title={r.dev}>
                          {raidText(r)}
                        </div>
                      ))}
                    </td>
                  )}
                  <td className="nowrap">
                    {osShort(s.os)} {s.kernel && <span className="muted mono small">{kernelShort(s.kernel)}</span>}
                  </td>
                  <td className="num">{t('{n} дн', { n: uptimeDays(s) })}</td>
                </>
              )}
            </tr>
          )
        })}
      </tbody>
    </table>
  )

  const gpuTot = Object.entries(tot.gpus)
  return (
    <div className="inv">
      <div className="inv-pick">
        <div className="inv-row">
          <span className="inv-lab">{t('Группы')}</span>
          <button className={`inv-chip${!groups.size ? ' on' : ''}`} onClick={() => setGroups(new Set())}>
            {t('Все')} <i>{all.length}</i>
          </button>
          {groupList.map(([g, n]) => (
            <button key={g} className={`inv-chip${groups.has(g) ? ' on' : ''}`} onClick={() => toggleGroup(g)}>
              {g === NO_GROUP ? t('без группы') : g} <i>{n}</i>
            </button>
          ))}
        </div>
        <div className="inv-row">
          <span className="inv-lab">{t('Фильтр')}</span>
          <button className={`inv-chip${kind === 'bare' ? ' on' : ''}`} onClick={() => setKind(kind === 'bare' ? '' : 'bare')}>
            {t('только Baremetal')}
          </button>
          <button className={`inv-chip${kind === 'vm' ? ' on' : ''}`} onClick={() => setKind(kind === 'vm' ? '' : 'vm')}>
            {t('только ВМ')}
          </button>
          <button className={`inv-chip${withGpu ? ' on' : ''}`} onClick={() => setWithGpu(!withGpu)}>
            {t('с GPU')}
          </button>
          <input
            className="srv-search inv-q"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t('имя, IP, CPU, ОС, страна')}
          />
        </div>
        <div className="inv-row small">
          <span>
            {t('В отчете {n} из {m}', { n: list.length, m: all.length })}
            {removed > 0 && `, ${t('вручную убрано {n}', { n: removed })}`}
          </span>
          <button className="linklike" onClick={() => setShowList(!showList)}>
            {showList ? t('скрыть список') : t('убрать отдельные серверы')}
          </button>
        </div>
        {showList && (
          <div className="inv-list">
            {base.map((s) => (
              <label key={s.id} className={off.has(s.id) ? 'off' : ''}>
                <input type="checkbox" checked={!off.has(s.id)} onChange={() => toggleOff(s.id)} /> {s.name}
              </label>
            ))}
          </div>
        )}
      </div>

      <div className="inv-row">
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

      {!list.length ? (
        <p className="muted small">{t('Ничего не выбрано')}</p>
      ) : (
        <>
          <div className="inv-tw">{full ? fullTable : shortTable}</div>
          <div className="net-tiles inv-tiles">
            <NetTile label={t('Серверов')} value={String(tot.n)} sub={`Baremetal ${tot.bare}, ${t('ВМ')} ${tot.vm}`} />
            <NetTile label={t('CPU (потоков)')} value={String(tot.threads)} />
            <NetTile label="RAM" value={fmtRamGb(tot.mem)} />
            {KINDS.filter((k) => tot.disks[k]).map((k) => (
              <NetTile
                key={k}
                label={kindLabel(k, t)}
                value={fmtSize(tot.disks[k]!.size)}
                sub={t('дисков: {n}', { n: tot.disks[k]!.n })}
              />
            ))}
            {gpuTot.length > 0 && (
              <NetTile
                label="GPU"
                value={String(gpuTot.reduce((a, [, n]) => a + n, 0))}
                sub={gpuTot.map(([g, n]) => `${n}x ${gpuShort(g)}`).join(', ')}
              />
            )}
            {tot.unknown > 0 && (
              <NetTile label={t('Диски неизвестны')} value={String(tot.unknown)} sub={t('нет helper disk-health')} tone="warn" />
            )}
          </div>
        </>
      )}
    </div>
  )
}
