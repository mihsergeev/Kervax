import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  exportBackup,
  getBackupFile,
  getBackupOverview,
  putBackupConfig,
  restoreBackup,
  runBackup,
  type BackupFileInfo,
  type BackupOverview,
} from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { useI18n } from './i18n'
import { useEscape } from './useEscape'
import { NetTile } from './NetLinks'
import { sinceShort } from './serverUtils'
import { fmtBytes } from './units'

// Окно "Бэкап" как карточка сервера: сводка (последний бэкап, расписание, файлы на диске и что
// входит в бэкап), автобэкап сроками кнопками, файлы на сервере со скачиванием и восстановлением,
// ручные скачать и восстановить из файла.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

type Want = { interval: number; keep: number }

// таблицы файла бэкапа - как называть в окне
const PART: Record<string, string> = {
  checks: 'Мониторы',
  check_incidents: 'Инциденты мониторов',
  servers: 'Серверы',
  locations: 'Локации',
  server_links: 'Связь между серверами',
  maintenance_windows: 'Окна обслуживания',
  ignored_domains: 'Скрытые домены',
  backup_vault: 'Сейф бэкапов',
  app_settings: 'Настройки панели',
}
const PART_NOTE: Record<string, string> = {
  backup_vault: 'зашифрован паролем сейфа',
  app_settings: 'алерты, Telegram, группы, сроки',
}
const EVERY: [number, string][] = [
  [0, 'выкл'],
  [6, '6 ч'],
  [12, '12 ч'],
  [24, 'сутки'],
  [48, '2 дня'],
  [168, 'неделя'],
]
const KEEP: [number, string][] = [
  [7, '7'],
  [14, '14'],
  [30, '30'],
  [90, '90'],
]

const SECS: SecDef[] = [
  { id: 'over', label: 'Сводка', group: '' },
  { id: 'auto', label: 'Автобэкап', group: 'На сервере' },
  { id: 'files', label: 'Файлы', group: 'На сервере' },
  { id: 'manual', label: 'Скачать и восстановить', group: 'Вручную' },
]
const LEAD: Record<string, string> = {
  over: 'Бэкап панели - ее настройки одним файлом: мониторы, серверы, локации, алерты. Из него панель восстанавливается целиком или переезжает на другой сервер.',
  auto: 'Панель сама сохраняет бэкап на свой диск по расписанию и хранит последние файлы, старые удаляет.',
  files: 'Бэкапы на диске панели, новые сверху. Любой можно скачать или восстановить из него панель.',
  manual: 'Свежий бэкап файлом на этот компьютер или восстановление из такого файла, например при переезде панели на другой сервер.',
}

function saveJson(data: unknown, name: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

const wantOf = (ov: BackupOverview): Want => ({ interval: ov.interval_hours, keep: ov.keep })

// через сколько: "через 5 ч", а если срок уже прошел - на ближайшем проходе сборщика
function untilShort(iso: string, t: (k: string, p?: Record<string, string | number>) => string): string {
  const sec = (Date.parse(iso) - Date.now()) / 1000
  if (!(sec > 60)) return t('в ближайшие минуты')
  if (sec < 3600) return t('через {n} мин', { n: Math.round(sec / 60) })
  if (sec < 48 * 3600) return t('через {n} ч', { n: Math.round(sec / 3600) })
  return t('через {n} дн', { n: Math.round(sec / 86400) })
}

export function BackupModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [ov, setOv] = useState<BackupOverview | null>(null)
  const [want, setWant] = useState<Want>({ interval: 24, keep: 14 })
  const [sec, setSec] = useState('over')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const mainRef = useRef<HTMLDivElement>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )
  const load = useCallback(
    (resetWant: boolean) =>
      getBackupOverview()
        .then((r) => {
          setOv(r)
          if (resetWant) setWant(wantOf(r))
        })
        .catch(fail),
    [fail],
  )
  useEffect(() => {
    load(true)
  }, [load])

  const saved = ov ? wantOf(ov) : want
  const dirty = !!ov && (want.interval !== saved.interval || want.keep !== saved.keep)

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }
  useEscape(close)
  const setW = (k: keyof Want, v: number) => {
    setMsg(null)
    const lim = k === 'interval' ? [0, 720] : [1, 365]
    setWant((w) => ({ ...w, [k]: Math.max(lim[0], Math.min(lim[1], Math.round(v) || 0)) }))
  }

  async function act(fn: () => Promise<string | void>) {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const m = await fn()
      if (m) setMsg(m)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }
  const save = () =>
    act(async () => {
      await putBackupConfig({ interval_hours: want.interval, keep: want.keep })
      await load(true)
      return t('Сохранено.')
    })
  const runNow = () =>
    act(async () => {
      await runBackup()
      await load(false)
      return t('Бэкап создан на сервере.')
    })
  const download = () =>
    act(async () => {
      const data = await exportBackup()
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      saveJson(data, `kervax-backup-${stamp}.json`)
    })
  const downloadFile = (name: string) =>
    act(async () => {
      saveJson(await getBackupFile(name), name)
    })
  // восстановление заменяет мониторы, серверы и настройки: после него перечитываем всю панель
  const restore = async (data: unknown, name: string) => {
    if (
      !window.confirm(
        t('Восстановить из «{name}»? Текущие мониторы, серверы и настройки будут заменены. Учётные записи в копию не входят и не восстановятся.', { name }),
      )
    )
      return
    await act(async () => {
      const r = await restoreBackup(data)
      const n = Object.values(r.restored).reduce((a, b) => a + b, 0)
      setTimeout(() => window.location.reload(), 1200)
      return t('Восстановлено записей: {n}. Обновляю…', { n })
    })
  }
  const restoreFile = async (f: BackupFileInfo) => {
    setErr(null)
    let data: unknown
    try {
      data = await getBackupFile(f.name)
    } catch (e) {
      return fail(e)
    }
    await restore(data, when(f.created_at))
  }
  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    if (!file) return
    setErr(null)
    setMsg(null)
    let data: unknown
    try {
      data = JSON.parse(await file.text())
    } catch {
      setErr(t('Файл не является корректным JSON.'))
      return
    }
    await restore(data, file.name)
  }

  const fmt = (n: number | null | undefined) => fmtBytes(n ?? 0, '0')
  const when = (iso: string) =>
    new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
  const ago = (iso: string) => {
    const a = sinceShort(iso, t)
    return a ? t('{a} назад', { a }) : t('только что')
  }
  const inShort = (iso: string) => untilShort(iso, t)
  const everyText = (h: number) =>
    h <= 0
      ? t('выключен')
      : h === 24
        ? t('раз в сутки')
        : h === 168
          ? t('раз в неделю')
          : h % 24 === 0
            ? t('раз в {n} дн.', { n: h / 24 })
            : t('раз в {n} ч', { n: h })

  const files = ov?.files ?? []
  const last = files[0]
  const off = !!ov && ov.interval_hours <= 0
  const info: Record<string, SecInfo> = {}
  if (off) info.auto = { level: 1, n: 0, line: '' }
  const side: Record<string, string> = ov
    ? { auto: ov.interval_hours > 0 ? everyText(ov.interval_hours) : t('выкл'), files: String(files.length) }
    : {}
  const offNote = off && (
    <div className="al-note al-note-warn">
      <span>{t('Автобэкап выключен: панель сама бэкап не делает, есть только то, что скачали руками.')}</span>
      {sec !== 'auto' && (
        <button className="linklike" onClick={() => go('auto')}>
          {t('Включить')}
        </button>
      )}
    </div>
  )
  const span = want.interval > 0 ? (want.interval * want.keep) / 24 : 0

  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Бэкап')}</h3>
          {ov && (
            <span className={`small ${off ? 't-degraded' : 'muted'}`}>
              {off ? t('автобэкап выключен') : last ? t('последний {a}', { a: ago(last.created_at) }) : t('бэкапов еще нет')}
            </span>
          )}
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={info} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>
            {!ov ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                <p className="muted small al-lead">{t(LEAD[cur.id])}</p>
                {cur.id === 'over' && (
                  <>
                    {offNote}
                    <div className="net-tiles srv-tiles">
                      <button className="srv-tile-btn" onClick={() => go('files')} title={t('к разделу')}>
                        <NetTile
                          label={t('Последний бэкап')}
                          value={last ? sinceShort(last.created_at, t) || t('сейчас') : t('нет')}
                          sub={last ? `${when(last.created_at)} · ${fmt(last.size)}` : t('еще ни одного файла')}
                          tone={last ? 'ok' : 'warn'}
                        />
                      </button>
                      <button className="srv-tile-btn" onClick={() => go('auto')} title={t('к разделу')}>
                        <NetTile
                          label={t('Автобэкап')}
                          value={
                            off
                              ? t('выкл')
                              : ov.interval_hours >= 48 && ov.interval_hours % 24 === 0
                                ? t('{n} дн', { n: ov.interval_hours / 24 })
                                : t('{n} ч', { n: ov.interval_hours })
                          }
                          sub={
                            ov.next_auto
                              ? t('следующий {when}', { when: inShort(ov.next_auto) })
                              : t('новых бэкапов не будет')
                          }
                          tone={off ? 'warn' : undefined}
                        />
                      </button>
                      <button className="srv-tile-btn" onClick={() => go('files')} title={t('к разделу')}>
                        <NetTile label={t('Файлов на диске')} value={String(files.length)} sub={files.length ? fmt(ov.total_bytes) : ''} />
                      </button>
                    </div>
                    <div className="ans-acts">
                      <button onClick={runNow} disabled={busy}>
                        {t('Создать сейчас')}
                      </button>
                      <button className="ghost" onClick={download} disabled={busy}>
                        {t('Скачать бэкап')}
                      </button>
                    </div>
                    <div className="srv-nav-grp rt-cap">{t('Что входит в бэкап')}</div>
                    <table className="sec-tbl">
                      <tbody>
                        {ov.parts.map((p) => (
                          <tr key={p.key}>
                            <td>
                              {t(PART[p.key] ?? p.key)}
                              {PART_NOTE[p.key] && <span className="muted small"> · {t(PART_NOTE[p.key])}</span>}
                            </td>
                            <td className="num muted">{p.key === 'app_settings' ? '' : p.rows.toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="muted small">
                      {t('Не входит: метрики и история (графики, алерты, хронология), журнал действий и пользователи - их роли и доступы после восстановления заводятся заново.')}
                    </p>
                  </>
                )}
                {cur.id === 'auto' && (
                  <>
                    {offNote}
                    <div className="srv-nav-grp rt-cap">{t('Как часто')}</div>
                    <div className="rt-row">
                      <div className="win-switch">
                        {EVERY.map(([h, l]) => (
                          <button
                            key={h}
                            className={`win-btn${want.interval === h ? ' win-btn-active' : ''}`}
                            onClick={() => setW('interval', h)}
                          >
                            {t(l)}
                          </button>
                        ))}
                      </div>
                      <label className="field rt-days">
                        <span>{t('свой интервал, часов')}</span>
                        <input
                          type="number"
                          min={0}
                          max={720}
                          value={want.interval}
                          onChange={(e) => setW('interval', Number(e.target.value))}
                        />
                      </label>
                    </div>
                    <div className="srv-nav-grp rt-cap">{t('Сколько файлов хранить')}</div>
                    <div className="rt-row">
                      <div className="win-switch">
                        {KEEP.map(([n, l]) => (
                          <button
                            key={n}
                            className={`win-btn${want.keep === n ? ' win-btn-active' : ''}`}
                            onClick={() => setW('keep', n)}
                          >
                            {l}
                          </button>
                        ))}
                      </div>
                      <label className="field rt-days">
                        <span>{t('свое число')}</span>
                        <input type="number" min={1} max={365} value={want.keep} onChange={(e) => setW('keep', Number(e.target.value))} />
                      </label>
                    </div>
                    <p className="small rt-proj muted">
                      {want.interval > 0
                        ? t('Файлов {n} - это бэкапы за {d} дн., на диске около {size}.', {
                            n: want.keep,
                            d: span >= 1 ? Math.round(span) : span.toFixed(1),
                            size: fmt((last?.size ?? 0) * want.keep),
                          })
                        : t('Автобэкап выключен: файлы на диске останутся, новых не будет.')}
                      {!dirty && ov.last_auto && ` ${t('Последний автобэкап - {when}.', { when: when(ov.last_auto) })}`}
                      {!dirty && ov.next_auto && ` ${t('Следующий - {when}.', { when: inShort(ov.next_auto) })}`}
                    </p>
                  </>
                )}
                {cur.id === 'files' &&
                  (files.length === 0 ? (
                    <>
                      <p className="muted small">{t('Пока нет файлов.')}</p>
                      <div className="ans-acts">
                        <button onClick={runNow} disabled={busy}>
                          {t('Создать сейчас')}
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <table className="sec-tbl">
                        <thead>
                          <tr>
                            <th>{t('Когда')}</th>
                            <th className="num">{t('Размер')}</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {files.map((f) => (
                            <tr key={f.name}>
                              <td>
                                {when(f.created_at)} <span className="muted small hide-m">· {ago(f.created_at)}</span>
                              </td>
                              <td className="num">{fmt(f.size)}</td>
                              <td className="num">
                                <span className="bk-acts">
                                  <button className="ghost small" onClick={() => downloadFile(f.name)} disabled={busy}>
                                    {t('Скачать')}
                                  </button>
                                  <button className="ghost small danger-btn" onClick={() => restoreFile(f)} disabled={busy}>
                                    {t('Восстановить')}
                                  </button>
                                </span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <p className="muted small">
                        {t('Всего {n}. Хранятся последние {k}, лишние удаляются при следующем бэкапе.', { n: fmt(ov.total_bytes), k: ov.keep })}
                      </p>
                    </>
                  ))}
                {cur.id === 'manual' && (
                  <>
                    <div className="al-box">
                      <div className="al-box-head">
                        <b>{t('Скачать')}</b>
                      </div>
                      <span className="muted small">{t('Свежий бэкап прямо сейчас - файлом на этот компьютер. На диск панели он не пишется.')}</span>
                      <div className="ans-acts">
                        <button onClick={download} disabled={busy}>
                          {t('Скачать бэкап')}
                        </button>
                      </div>
                    </div>
                    <div className="al-box">
                      <div className="al-box-head">
                        <b>{t('Восстановить из файла')}</b>
                      </div>
                      <span className="muted small">
                        {t('Мониторы, серверы, локации и настройки панели заменятся тем, что в файле. Пользователи остаются как есть. Файл старой версии без каких-то таблиц их не трогает.')}
                      </span>
                      <div className="ans-acts">
                        <button className="ghost danger-btn" onClick={() => fileRef.current?.click()} disabled={busy}>
                          {t('Выбрать файл…')}
                        </button>
                        <input ref={fileRef} type="file" accept="application/json,.json" hidden onChange={onUpload} />
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="al-foot">
          {err && ov ? (
            <span className="form-error small">{err}</span>
          ) : msg ? (
            <span className="form-ok small">{msg}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')} · {everyText(want.interval)}
            </span>
          ) : ov ? (
            <span className="muted small">
              {t('Автобэкап {every}, хранить файлов: {n}', { every: everyText(ov.interval_hours), n: ov.keep })}
            </span>
          ) : null}
          <span className="al-foot-btns">
            {dirty && (
              <button className="ghost" onClick={() => ov && setWant(wantOf(ov))} disabled={busy}>
                {t('Отмена')}
              </button>
            )}
            <button onClick={save} disabled={!dirty || busy}>
              {busy ? t('…') : t('Сохранить')}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}
