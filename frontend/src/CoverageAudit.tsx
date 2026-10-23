import { useEffect, useRef, useState } from 'react'
import { backupCommand, backupCommandStatus, updateServer, type BackupAudit, type BackupCommand, type DumpStat, type Server, backupAuditMute } from './api'
import { CommandProgress, nextPhase, type CmdProgressState } from './CommandProgress'
import { dumpFailText, helperText } from './helperText'
import { useI18n } from './i18n'
import { EngineIcon } from './engineIcon'
import { NetTile } from './NetLinks'
import { byteUnits, fmtBytes } from './units'

const fmtSize = (n: number) => {
  const [, kb, mb, gb] = byteUnits()
  return n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)} ${gb}`
    : n >= 1 << 20 ? `${Math.round(n / (1 << 20))} ${mb}`
    : `${Math.max(1, Math.round(n / 1024))} ${kb}`
}
const fmtWhen = (ts: number, t: (s: string, p?: Record<string, string | number>) => string) => {
  if (!ts) return t('ещё не снимался')
  const h = (Date.now() / 1000 - ts) / 3600
  return h < 1 ? t('{n} мин назад', { n: Math.max(1, Math.round(h * 60)) })
    : h < 48 ? t('{n} ч назад', { n: Math.round(h) })
    : t('{n} д назад', { n: Math.round(h / 24) })
}

// Блок «Покрытие» вынесен из BackupsPage в отдельный файл: он показывается И в модалке
// бэкапа, И на детали сервера. Раньше жил только в модалке клиента — у ноды без бэкапа
// (или у бэкап-сервера) находки показать было негде, и пункт с главной вёл в никуда.

async function runAndWait(
  serverId: number,
  body: Parameters<typeof backupCommand>[1],
  onStatus?: (status: string) => void,
): Promise<BackupCommand> {
  const c = await backupCommand(serverId, body)
  let last = c
  onStatus?.(last.status)
  // Пробный дамп на крупной базе идёт десятки секунд. Ждём до таймаута самой панели (90 с,
  // после него команда помечается «агент не ответил»), а не обрываем раньше: иначе
  // медленная, но успешная команда выглядела бы незавершённой.
  for (let i = 0; i < 240 && last.status !== 'done' && last.status !== 'error'; i++) {
    await new Promise((r) => setTimeout(r, 400))
    last = await backupCommandStatus(serverId, c.id)
    onStatus?.(last.status)
  }
  return last
}

// что делает helper на этапе «выполняет» — у каждого движка своя проверка
const PROBE_TEXT: Record<string, string> = {
  pg: 'Сервер проверяет доступ к базе (pg_dumpall --schema-only)',
  mysql: 'Сервер проверяет доступ к базе (mysqldump --no-data)',
  ch: 'Сервер проверяет доступ к ClickHouse',
  redis: 'Сервер проверяет доступ к Redis',
  rabbitmq: 'Сервер проверяет доступ к RabbitMQ',
  k8s: 'Сервер настраивает снимок кластера',
  grafana: 'Сервер снимает пробную копию grafana.db',
  neo4j: 'Сервер проверяет neo4j-admin в образе базы',
}
const PROBE_SEC: Record<string, number> = { k8s: 4, redis: 6, ch: 6, rabbitmq: 8, neo4j: 12 }

// База в поде kubernetes: экземпляр — не контейнер, а контроллер пода
// «k8s.<namespace>.<sts|deploy|ds>.<имя>». Дамп снимает helper на ноде через kubectl exec,
// сам под он находит заново при каждом запуске. Человеку показываем «namespace/имя».
const isKube = (inst?: string) => !!inst && inst.startsWith('k8s.')
const MUTED_HIDDEN_KEY = 'kervax_cov_muted_hidden'
// значок заглушенной находки: у базы - значок движка, у тома кластера - kubernetes,
// у каталога или диска - папка (цилиндр базы на пути вводил бы в заблуждение)
function MutedIcon({ x }: { x: BackupAudit }) {
  if (x.kind.startsWith('kube_vol')) return <EngineIcon name="kubernetes" />
  if (x.kind === 'mount' || x.kind === 'bind') {
    return (
      <svg className="engine-ico" viewBox="0 0 24 24" width={15} height={15} aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M3 5.5A1.5 1.5 0 0 1 4.5 4h4.6l2 2.2h8.4A1.5 1.5 0 0 1 21 7.7v10.8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z" />
      </svg>
    )
  }
  return <EngineIcon name={x.subject} />
}
function instLabel(inst: string): string {
  if (!isKube(inst)) return inst
  const [, ns, , ...name] = inst.split('.')
  return `☸ ${ns}/${name.join('.')}`
}

// Аудит покрытия: панель сверяет, что на ноде есть, с тем, что реально попадает в бэкап.
// Две категории: ДЫРА (данных в бэкапе нет) и РИСК (данные есть, но восстановимость под
// вопросом — живая СУБД). Алертов по этому намеренно нет: сначала смотрим, не шумит ли.
export function CoverageAudit({ server: s, canManage, onChanged, onSchedule, bare = false }: {
  server: Server; canManage: boolean; onChanged: () => void
  onSchedule?: () => void // открыть раздел "Расписание и пути" окна бэкапа
  bare?: boolean // без своего заголовка: он уже в шапке раздела окна
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [dumpBusy, setDumpBusy] = useState<string | null>(null)
  // Ход команды дампа — в карточке той базы, которую включают или выключают. Ключ
  // «движок|контейнер»: на ноде бывает несколько postgres, и полоса должна стоять у своей.
  const [op, setOp] = useState<{
    key: string; engine: string; container: string; action: 'dump_setup' | 'dump_remove'
    progress: CmdProgressState; msg: { ok: boolean; text: string } | null
  } | null>(null)
  const [cfgFor, setCfgFor] = useState<string | null>(null)
  const [muteBusy, setMuteBusy] = useState<string | null>(null)
  // заглушенное видно по умолчанию: человек должен помнить, что именно выключил.
  // "скрыть" запоминаем в браузере, доступ к localStorage бывает закрыт
  const [mutedHidden, setMutedHidden] = useState(() => {
    try { return localStorage.getItem(MUTED_HIDDEN_KEY) === '1' } catch { return false }
  })
  const toggleMutedHidden = () => {
    const next = !mutedHidden
    setMutedHidden(next)
    try { localStorage.setItem(MUTED_HIDDEN_KEY, next ? '1' : '0') } catch { /* не запомним - не беда */ }
  }
  const [showDbRisks, setShowDbRisks] = useState(false)
  const [dumpMsg, setDumpMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // Дамп нужен и БЕЗ файлового бэкапа: локальная копия на самой ноде, из которой можно
  // восстановиться (helper 0.19 поднимает под это свой таймер). Единственное реальное
  // требование — установленный helper: через его спул панель и отдаёт команду.
  const bk = s.last_report?.backup
  const canDumpHere = canManage && !!bk?.manageable
  const dumpsAreLocalOnly = !bk?.configured  // копии вне ноды не будет — честно предупреждаем
  // ссылку «поменять время бэкапа» показываем, только если раздел «Управление» есть на
  // странице (модалка бэкапа), — в детали сервера его нет, кнопка была бы мёртвой
  const [hasSchedule, setHasSchedule] = useState(false)
  useEffect(() => {
    setHasSchedule(!!document.getElementById('backup-manage-schedule'))
  }, [])
  const items = s.backup_audit ?? []
  const live = items.filter((x) => !x.muted)
  const mutedItems = items.filter((x) => !!x.muted)
  const gaps = live.filter((x) => x.gap)
  const allRisks = live.filter((x) => !x.gap && x.kind !== 'db_ok')
  const okItems = live.filter((x) => x.kind === 'db_ok') // дамп уже настроен — это не проблема
  const hasDb = live.some((x) => x.kind === 'db')
  // галка «бэкаплю все базы сам» (db_dumps_ok) прячет находки-СУБД под спойлер — иначе они
  // висят простынёй (на ноде бывает 100+ баз), хотя оператор уже сказал «не напоминать».
  // Не-СУБД риски (если есть) остаются видны; свернутое доступно по «показать».
  const dbSelfManaged = !!s.db_dumps_ok
  const dbRisks = allRisks.filter((x) => x.kind === 'db')
  const risks = dbSelfManaged && !showDbRisks ? allRisks.filter((x) => x.kind !== 'db') : allRisks
  const muteFinding = async (key: string, muted: boolean) => {
    setMuteBusy(key)
    try {
      await backupAuditMute(s.id, key, muted)
      onChanged()
    } finally {
      setMuteBusy(null)
    }
  }
  // кнопка «не нужно» на карточке: точечная замена общей галочке — глушит ОДНУ находку
  const muteBtn = (x: { key?: string }) => (canManage && x.key ? (
    <button className="ghost icon-btn coverage-mute" disabled={muteBusy !== null}
      title={t('Не бэкапить это — убрать из проблем')}
      onClick={() => muteFinding(x.key!, true)}>
      {muteBusy === x.key ? '…' : '🔕'}
    </button>
  ) : null)
  const [dumpsWish, setDumpsWish] = useState<boolean | null>(null)
  useEffect(() => { setDumpsWish(null) }, [s.db_dumps_ok])

  // Пока обновлённый сервер едет с бэкенда (запрос + перезагрузка списка), показываем
  // то, что человек только что выбрал. Иначе галочка полсекунды стоит в прежнем
  // положении, и клик выглядит непринятым — жмут второй раз, отменяя первый.
  const dumpsOk = dumpsWish ?? !!s.db_dumps_ok
  const toggleDumps = async () => {
    const next = !dumpsOk
    setDumpsWish(next)
    setBusy(true)
    try {
      await updateServer(s.id, { db_dumps_ok: next })
      onChanged()
    } catch {
      setDumpsWish(null)  // не сохранилось — возвращаем как было
    } finally {
      setBusy(false)
    }
  }
  // включить локальные дампы: helper положит скрипт в /lib65/kervax/dumps.d и повесит его
  // на сервис бэкапа, дампы лягут в /backup/<движок> — restic заберёт их как обычные файлы
  const dumpAction = async (
    action: 'dump_setup' | 'dump_remove', engine: string, container: string,
    opts?: { dump_dir?: string; dump_keep?: number; dump_minfree?: number },
  ) => {
    if (action === 'dump_remove' &&
      !window.confirm(t('Выключить дампы {eng}? Локальные файлы дампов будут удалены; история останется в restic.', { eng: engine })))
      return
    const key = `${engine}|${container}`
    const start = Date.now()
    setDumpBusy(engine); setDumpMsg(null); setCfgFor(null)
    setOp({ key, engine, container, action, msg: null, progress: { phase: 'sent', since: start, phaseSince: start } })
    const phase = (ph: CmdProgressState['phase'], msg: { ok: boolean; text: string } | null = null) =>
      setOp((o) => (o && o.key === key ? { ...o, msg, progress: nextPhase(o.progress, ph) } : o))
    try {
      const body = action === 'dump_setup'
        ? { action, engine, container, ...opts } as const
        : { action, engine, container } as const
      const res = await runAndWait(s.id, body, (st) => { if (st === 'running') phase('running') })
      if (res.status === 'error') {
        phase('error', { ok: false, text: dumpFailText(res.result, t) })
      } else if (res.status === 'done') {
        // Команда отработала, но карточка перерисуется, только когда новое состояние
        // приедет отчётом сервера (до ~15 с). До того кнопка «включить» висела бы как ни
        // в чём не бывало, поэтому держим этап «подтверждение» — см. эффект ниже.
        phase('confirm', { ok: true, text: helperText(res.result, t) || t('готово') })
        onChanged()
      } else {
        phase('confirm', { ok: true, text: t('Команда ещё выполняется на сервере — статус обновится в течение минуты.') })
        onChanged()
      }
    } catch (e) {
      phase('error', { ok: false, text: (e instanceof Error && e.message) || t('ошибка') })
    } finally {
      setDumpBusy(null)
    }
  }
  // статус включённого дампа: показываем и у «под вопросом», и у закрытых находок —
  // иначе выключить дамп можно было бы только пока он числится проблемой
  // дамп ищем по паре «движок + контейнер»: на ноде бывает две postgres, и дамп одной
  // не должен подсвечиваться на карточке другой
  const findDump = (engine?: string, inst?: string) => {
    const all = s.last_report?.backup?.dumps ?? []
    return all.find((y) => y.engine === engine && (y.container || '') === (inst || ''))
      // helper < v8 контейнер не присылал — засчитываем такой дамп первому экземпляру
      ?? all.find((y) => y.engine === engine && !y.container)
  }
  // Этап «подтверждение»: ждём, пока отчёт сервера покажет включённый (или исчезнувший)
  // дамп, и понемногу перечитываем сервер. Больше минуты не ждём — отчёт догонит сам.
  const confirmed = op?.progress.phase === 'confirm' && (() => {
    const has = !!findDump(op.engine, op.container)
    return op.action === 'dump_setup' ? has : !has
  })()
  useEffect(() => {
    if (confirmed) setOp((o) => (o ? { ...o, progress: nextPhase(o.progress, 'done') } : o))
  }, [confirmed])
  // Опрос — своим таймером, а не «следующей отрисовкой»: если отчёт вернёт то же самое,
  // перерисовки не будет, и ожидание зависло бы навсегда.
  const onChangedRef = useRef(onChanged)
  onChangedRef.current = onChanged
  const confirmSince = op?.progress.phase === 'confirm' ? op.progress.phaseSince : 0
  useEffect(() => {
    if (!confirmSince) return
    const id = window.setInterval(() => {
      if (Date.now() - confirmSince > 60_000) {
        setOp((o) => (o && o.progress.phase === 'confirm' ? { ...o, progress: nextPhase(o.progress, 'done') } : o))
        return
      }
      onChangedRef.current()
    }, 3000)
    return () => window.clearInterval(id)
  }, [confirmSince])
  // пока команда идёт (включая подтверждение), остальные действия с дампами недоступны
  const opActive = !!op && op.progress.phase !== 'done' && op.progress.phase !== 'error'
  const busyAny = dumpBusy !== null || opActive
  // полоса и итог — в карточке своей базы
  const opFor = (engine?: string, inst?: string) =>
    op && op.key === `${engine ?? ''}|${inst ?? ''}` ? op : null
  const opBlock = (engine?: string, inst?: string) => {
    const o = opFor(engine, inst)
    if (!o) return null
    const running = o.action === 'dump_setup'
      ? isKube(o.container)
        ? t('Сервер находит под и проверяет доступ к базе')
        : t(PROBE_TEXT[o.engine] ?? 'Сервер настраивает дамп')
      : t('Сервер выключает дамп и удаляет локальные файлы')
    return (
      <div className="coverage-op">
        {o.progress.phase !== 'done' && o.progress.phase !== 'error' && (
          <CommandProgress state={o.progress} runningText={running}
            expectSec={isKube(o.container) ? 12 : PROBE_SEC[o.engine] ?? 20} />
        )}
        {o.msg && (o.progress.phase === 'done' || o.progress.phase === 'error') && (
          <div className={`small coverage-op-msg ${o.msg.ok ? 't-up' : 'form-error'}`}>
            {o.msg.ok ? '✓ ' : '✗ '}{o.msg.text}
            <button className="ghost icon-btn coverage-op-x" title={t('Закрыть')} onClick={() => setOp(null)}>✕</button>
          </div>
        )}
      </div>
    )
  }
  // Строка под именем базы: у дампа - когда снимался или когда снимется первый, каталог и сколько
  // хранить; у остального - короткая note с бэкенда. Полный текст находки - в подсказке.
  const rowLine = (x: BackupAudit, d: DumpStat | undefined) => {
    if (!d) return <>{x.note || x.detail}{sizeTail(x)}</>
    const head = x.kind === 'db'
      ? x.note || x.detail // дамп включен, но не снимается
      : d.files > 0
        ? `${t('дамп {when}', { when: fmtWhen(d.last_ts, t) })} · ${t('файлов: {n}', { n: d.files })}${d.size_bytes > 0 ? `, ${fmtSize(d.size_bytes)}` : ''}`
        : dumpsAreLocalOnly
          ? t('первый снимется ночью, около 03:00')
          : bk?.schedule
            ? t('первый снимется в бэкап в {time}', { time: bk.schedule })
            : t('первый снимется в ближайший бэкап')
    return (
      <>
        {head} · <span className="mono">{d.dir || '/backup'}</span> · {t('хранить {k}', { k: d.keep })}
        {(d.min_free_pct ?? 0) === 0 && <> · {t('без защиты от переполнения')}</>}
      </>
    )
  }
  // Объем: рядом с именем - сколько примерно бэкапить, в строке - из чего он (база целиком, если дамп
  // без индексов заметно меньше, и крупнейшая база). Не посчитано - молчим
  const sizeChip = (x: BackupAudit) =>
    x.size_small
      ? <span className="cov-size" title={t('бэкапятся только настройки (definitions): килобайты при любом объеме очередей')}>{t('мало')}</span>
      : x.size_bytes
        ? <span className="cov-size" title={x.size_db
            ? t('примерный объем дампа: данные без индексов, индексы в дамп не идут')
            : t('примерно столько придется бэкапить, замер раз в сутки')}>~{fmtBytes(x.size_bytes)}</span>
        : null
  const sizeTail = (x: BackupAudit) => {
    const parts = [
      x.size_db ? t('дамп без индексов, база {v}', { v: fmtBytes(x.size_db) }) : '',
      x.size_top && x.size_top_bytes ? t('крупнейшая {name} {v}', { name: x.size_top, v: fmtBytes(x.size_top_bytes) }) : '',
    ].filter(Boolean)
    return parts.length ? <> · {parts.join(', ')}</> : null
  }
  const row = (x: BackupAudit, side: 'q' | 'ok', i: number) => {
    const d = findDump(x.dump_engine, x.instance)
    const cfgKey = x.instance || x.dump_engine || ''
    const canEnable = side === 'q' && canDumpHere && !!x.dump_engine && !!x.can_dump && !d &&
      !(opActive && opFor(x.dump_engine, x.container || ''))
    return (
      <div key={x.key || i} className="cov-li">
        <span className="cov-ico"><EngineIcon name={x.subject} /></span>
        <div className="cov-nm">
          <b>{x.subject}</b>
          {x.instance && <span className="type-chip mono">{instLabel(x.instance)}</span>}
          {sizeChip(x)}
          {side === 'q' && d && <span className="cov-tag cov-tag-bad">{t('дамп не снимается')}</span>}
          {side === 'ok' && d && !d.files && <span className="cov-tag cov-tag-wait">{t('ждет первого бэкапа')}</span>}
          {d?.skipped && <span className="cov-tag cov-tag-bad">{t('пропущен: мало места')}</span>}
        </div>
        <span className="cov-act">
          {canEnable && (
            <button className="svc-act svc-act-primary" disabled={busyAny}
              onClick={() => setCfgFor(cfgFor === cfgKey ? null : cfgKey)}>
              {t('включить дамп')}
            </button>
          )}
          {d && canManage && (
            <>
              <button className="ghost icon-btn cov-ib" disabled={busyAny}
                title={t('Настройки дампа: каталог, сколько хранить, порог свободного места')}
                onClick={() => setCfgFor(cfgFor === cfgKey ? null : cfgKey)}>
                ⚙
              </button>
              <button className="ghost icon-btn cov-ib" disabled={busyAny} title={t('Выключить дамп')}
                onClick={() => dumpAction('dump_remove', d.engine, d.container || '')}>
                ✕
              </button>
            </>
          )}
          {side === 'q' && muteBtn(x)}
        </span>
        <div className="cov-ln" title={x.detail}>{rowLine(x, d)}</div>
        {d?.skipped && (
          <div className="cov-full form-error small">
            ⚠️ {t('последний прогон пропущен: на разделе было свободно {p}% (< порога) — расчистите место',
              { p: d.skip_free_pct ?? 0 })}
          </div>
        )}
        {/* форма до первого включения: цена включения (простой) видна до применения */}
        {canEnable && cfgFor === cfgKey && (
          <div className="cov-full">
            {x.downtime && <div className="coverage-downtime small">⏸ {x.downtime}</div>}
            <DumpConfig
              init={{ dir: '/backup', keep: 2, minfree: 10 }}
              suffix={`/${x.dump_engine}${x.container ? '/' + x.container : ''}`}
              busy={busyAny}
              onApply={(o) => dumpAction('dump_setup', x.dump_engine!, x.container || '', o)}
            />
          </div>
        )}
        {d && canManage && cfgFor === cfgKey && (
          <div className="cov-full">
            <DumpConfig
              init={{ dir: d.dir || '/backup', keep: d.keep, minfree: d.min_free_pct ?? 10 }}
              suffix={`/${d.engine}${d.container ? '/' + d.container : ''}`}
              busy={busyAny}
              onApply={(o) => dumpAction('dump_setup', d.engine, d.container || '', o)}
            />
          </div>
        )}
        {opFor(x.dump_engine, x.container || '') && <div className="cov-full">{opBlock(x.dump_engine, x.container || '')}</div>}
      </div>
    )
  }
  // ранний выход — после всех хуков: иначе при опустевшем списке менялся бы их порядок
  if (items.length === 0) return null
  // слева сломанные дампы первыми, затем базы без дампа
  const broken = (x: BackupAudit) => x.kind === 'db' && !!findDump(x.dump_engine, x.instance)
  const left = [...risks].sort((a, b) => Number(broken(b)) - Number(broken(a)))
  const brokenN = allRisks.filter(broken).length
  // сколько всего придется бэкапить из "Под вопросом": по строкам, где объем посчитан; есть
  // непосчитанные - "от", чтобы сумма не выдавала себя за полную
  const leftSized = left.filter((x) => (x.size_bytes ?? 0) > 0)
  const leftSum = leftSized.reduce((n, x) => n + (x.size_bytes ?? 0), 0)
  const leftPartial = left.some((x) => !x.size_bytes && !x.size_small)
  const waitN = okItems.filter((x) => { const d = findDump(x.dump_engine, x.instance); return !!d && !d.files }).length
  const isDb = (x: BackupAudit) => x.kind === 'db' || x.kind === 'db_ok'
  const mutedDb = mutedItems.filter(isDb).length
  const hiddenDb = dbSelfManaged && dbRisks.length > 0 && !showDbRisks
  const anyDump = okItems.some((x) => !!findDump(x.dump_engine, x.instance))
  const showCols = risks.length > 0 || okItems.length > 0 || (dbSelfManaged && dbRisks.length > 0)
  // Заглушенное - карточками как "Бэкапится", но серым: видно, что выключено и почему, и
  // не спорит с проблемами. Встает под более короткую колонку, чтобы рядом не зияла дыра;
  // если там не помещается (строки заглушенного ниже карточек базы, отсюда 1.5) - на всю
  // ширину под колонками, и тогда список сам раскладывается в две колонки
  const isPath = (x: BackupAudit) => x.kind === 'mount' || x.kind === 'bind' || x.kind === 'kube_vol'
  const leftRows = Math.max(1, left.length) + (dbSelfManaged && dbRisks.length > 0 ? 1 : 0)
  const rightRows = Math.max(1, okItems.length)
  const mutedPlace = !showCols || mutedItems.length > Math.abs(leftRows - rightRows) * 1.5 + 3
    ? 'wide' : leftRows <= rightRows ? 'left' : 'right'
  const mutedBlock = mutedItems.length > 0 && (
    <div className="cov-col cov-col-mute">
      <div className="cov-col-h">
        {t('Заглушено')} <span className="cov-n">{mutedItems.length}</span>
        <span className="cov-col-sub">{t('не бэкапится намеренно, в проблемы не идет')}</span>
        <button className="linklike cov-col-toggle" onClick={toggleMutedHidden}>
          {mutedHidden ? t('показать') : t('скрыть')}
        </button>
      </div>
      {!mutedHidden && <div className="cov-mute-list">{mutedItems.map((x, i) => (
        <div key={x.key || i} className="cov-li cov-li-mute">
          <span className="cov-ico"><MutedIcon x={x} /></span>
          <div className="cov-nm">
            <b className={isPath(x) ? 'mono' : undefined}>{x.subject}</b>
            {x.instance && <span className="type-chip mono">{instLabel(x.instance)}</span>}
          </div>
          <span className="cov-act">
            {canManage && x.key && (
              <button className="ghost cov-unmute" disabled={muteBusy !== null}
                title={t('Вернуть в проблемы: панель снова будет напоминать, что это не в бэкапе')}
                onClick={() => muteFinding(x.key!, false)}>
                {muteBusy === x.key ? '...' : <><span className="cov-unmute-ico">🔔</span> {t('вернуть')}</>}
              </button>
            )}
          </span>
          <div className="cov-ln" title={x.detail}>{x.note || x.detail}</div>
        </div>
      ))}</div>}
    </div>
  )
  return (
    <div className="chart-card net-card net-card-wide coverage-audit cov-card">
      {!bare && (
        <div className="net-head">
          <div className="chart-cap net-cap">{t('Покрытие')}</div>
        </div>
      )}
      {showCols && (
        <div className="net-tiles">
          <NetTile label={t('Баз на ноде')} value={String(live.filter(isDb).length)}
            sub={mutedDb ? t('и приглушено: {n}', { n: mutedDb }) : undefined} />
          <NetTile label={t('Бэкапится')} value={String(okItems.length)} tone={okItems.length ? 'ok' : undefined}
            sub={waitN ? t('из них ждут первого бэкапа: {n}', { n: waitN }) : t('дамп или свой бэкап')} />
          <NetTile label={t('Под вопросом')} value={String(allRisks.length - brokenN)}
            tone={allRisks.length - brokenN ? 'warn' : 'ok'} sub={t('только файловый снапшот')} />
          <NetTile label={t('Сломано')} value={String(brokenN + gaps.length)} tone={brokenN + gaps.length ? 'bad' : 'ok'}
            sub={t('дамп не снимается, не в бэкапе')} />
        </div>
      )}
      {gaps.length > 0 && (
        <div className="cov-gaps">
          <div className="cov-gaps-h">⚠️ {t('Не попадает в бэкап ({n}):', { n: gaps.length })}</div>
          {gaps.map((x, i) => (
            <div key={x.key || i} className="cov-gap">
              <span className="mono">{x.subject}</span>
              <span className="muted">{x.note || x.detail}</span>
              {muteBtn(x)}
            </div>
          ))}
        </div>
      )}
      {/* Нет helper'а — кнопка не сработает (некуда класть команду), поэтому вместо
          молчаливой ошибки «спул недоступен» показываем, чего не хватает. */}
      {canManage && !canDumpHere && risks.some((x) => x.dump_engine && x.can_dump) && (
        <div className="muted small coverage-dump-hint">
          {t('Управление дампами идёт через helper backup-setup — на ноде его нет:')}
          <div className="mono small">
            {`curl -fsSL ${window.location.origin}/api/agent/setup/backup-setup.sh | sudo bash`}
          </div>
        </div>
      )}
      {/* дамп без файлового бэкапа полезен, но копия остаётся на той же ноде */}
      {canDumpHere && dumpsAreLocalOnly &&
        risks.some((x) => x.dump_engine && x.can_dump && !findDump(x.dump_engine, x.instance)) && (
        <div className="muted small coverage-dump-hint">
          {t('Файлового бэкапа на ноде нет: дампы лягут локально (ежедневно, свой таймер) — восстановиться с самой ноды можно, копии за её пределами не будет.')}
        </div>
      )}
      {showCols && (
        <div className="cov-cols">
          <div className="cov-stack">
            <div className={`cov-col cov-col-q${left.length ? '' : ' cov-col-calm'}`}>
              <div className="cov-col-h">
                {t('Под вопросом')} <span className="cov-n">{left.length}</span>
                {leftSized.length > 1 && (
                  <span className="cov-size cov-size-sum" title={leftPartial
                    ? t('сумма по базам, где объем посчитан; у остальных его пока нет')
                    : t('примерно столько придется бэкапить по всем базам под вопросом')}>
                    {leftPartial ? t('от ~{v}', { v: fmtBytes(leftSum) }) : `~${fmtBytes(leftSum)}`}
                  </span>
                )}
                {left.length > 0 && <span className="cov-col-sub">{t('нужен дамп: файловый снапшот живой базы может не восстановиться')}</span>}
              </div>
              {/* при «бэкаплю все базы сам» находки-СУБД свёрнуты — счётчик и кнопка */}
              {dbSelfManaged && dbRisks.length > 0 && (
                <button className="ghost repo-cleanup-toggle" onClick={() => setShowDbRisks(!showDbRisks)}>
                  {showDbRisks
                    ? t('свернуть базы (бэкаплю сам)')
                    : t('🛢 базы, которые бэкаплю сам: {n} — показать', { n: dbRisks.length })}
                </button>
              )}
              {left.map((x, i) => row(x, 'q', i))}
              {!left.length && !hiddenDb && <div className="cov-empty">✓ {t('все базы бэкапятся')}</div>}
            </div>
            {mutedPlace === 'left' && mutedBlock}
          </div>
          <div className="cov-stack">
            <div className="cov-col cov-col-ok">
              <div className="cov-col-h">
                {t('Бэкапится')} <span className="cov-n">{okItems.length}</span>
                {anyDump && (
                  <span className="cov-col-sub">
                    {dumpsAreLocalOnly
                      ? t('дампы снимаются ночью и лежат только на ноде')
                      : bk?.schedule
                        ? t('дамп снимается перед бэкапом в {time}', { time: bk.schedule })
                        : t('дамп снимается перед каждым бэкапом')}
                    {!dumpsAreLocalOnly && (hasSchedule || onSchedule) && (
                      <>
                        {' · '}
                        <button className="linklike" onClick={() => {
                          if (onSchedule) return onSchedule()
                          document.getElementById('backup-manage-schedule')
                            ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                        }}>
                          {t('поменять время')}
                        </button>
                      </>
                    )}
                  </span>
                )}
              </div>
              {okItems.map((x, i) => row(x, 'ok', i))}
              {!okItems.length && <div className="cov-empty muted">{t('пока ни одной базы')}</div>}
            </div>
            {mutedPlace === 'right' && mutedBlock}
          </div>
        </div>
      )}
      {mutedPlace === 'wide' && mutedBlock}
      {/* итог, если карточка, у которой шла команда, пропала из списков (например,
          контейнер удалили, пока ждали отчёт) — иначе ответ сервера просто исчез бы */}
      {op?.msg && (op.progress.phase === 'done' || op.progress.phase === 'error') &&
        !live.some((x) => `${x.dump_engine ?? ''}|${x.container || ''}` === op.key) && (
        <div className={`small ${op.msg.ok ? 't-up' : 'form-error'}`}>{op.msg.text}</div>
      )}
      {dumpMsg && !dumpBusy && (
        <div className={`small ${dumpMsg.ok ? 't-up' : 'form-error'}`}>{dumpMsg.text}</div>
      )}
      <div className="cov-foot">
        {hasDb && canManage && (
          <label className="nobackup-check">
            <input type="checkbox" checked={dumpsOk} disabled={busy} onChange={toggleDumps} />
            {t('Все базы этой ноды бэкаплю сам — не напоминать')}
            <span className="cov-q"
              title={`${t('Скопом гасит напоминание про ВСЕ найденные базы этой ноды (список выше). Панель видит только дампы, которые настроила сама или нашла CronJob’ом в кластере. Если базы вы бэкапите своим способом (свой cron, ansible, managed-база у провайдера) — отметьте. Одну базу — точечно кнопкой 🔕 на её карточке.')} ${t('Только гасит напоминание: на сервере ничего не меняется, автопроверка выше работает. Не ставьте, если дампов на самом деле нет, — иначе спрячете реальную дыру в бэкапе.')}`}>
              ?
            </span>
          </label>
        )}
        {/* proc_full=false → на ноде hidepid, агент не видит чужие процессы: нативно
            установленные СУБД других юзеров могут не попасть в детект (контейнерные и
            kube видны по образам). Честно предупреждаем, что список может быть неполным. */}
        {s.last_report?.caps && s.last_report.caps.proc_full === false && (
          <div className="dump-cfg-hint warn">
            ⚠️ {t('На ноде включён hidepid — нативно установленные СУБД других пользователей могут быть не видны (в контейнерах и kubernetes — видны). Проверьте такие базы вручную.')}
          </div>
        )}
        {/* длинные объяснения - один раз здесь, а не в каждой карточке базы */}
        <details className="cov-how">
          <summary>{t('как это работает')}</summary>
          <div>
            <p>{t('Файловый снапшот живой базы может не восстановиться: пока restic копирует файлы, база их меняет. Надежнее дамп.')}</p>
            <p>
              {dumpsAreLocalOnly
                ? t('Файлового бэкапа на ноде нет: дамп снимается ночью, около 03:00 по времени ноды, и хранится только на ней. Восстановиться с самой ноды можно, копии за её пределами нет.')
                : t('По расписанию сначала снимается дамп, затем restic бэкапит файлы (включая свежий дамп) — одним запуском, последовательно. Долгий дамп задержит начало restic, но не сорвёт его.')}
            </p>
            <p>{t('PostgreSQL: каждая база — отдельный файл + globals (роли/права).')}</p>
            <p>{t('Сверяется автоматически: точки монтирования, bind-mount ы контейнеров, живые СУБД и тома kubernetes.')}</p>
          </div>
        </details>
      </div>
    </div>
  )
}

// Форма настроек дампа: каталог, сколько хранить, порог свободного места. Значения те же,
// что валидирует helper (keep 1..30, minfree 0..50) — но окончательная проверка на ноде.
function DumpConfig({ init, suffix, busy, onApply }: {
  init: { dir: string; keep: number; minfree: number }
  // «/<движок>[/<контейнер>]» — хвост, который helper добавляет к присланной базе.
  // В поле показываем ПОЛНЫЙ путь (его и правят), а перед отправкой хвост срезаем:
  // пришли мы полный — helper приписал бы движок ещё раз (/backup/k8s → /backup/k8s/k8s)
  suffix: string
  busy: boolean
  onApply: (o: { dump_dir: string; dump_keep: number; dump_minfree: number }) => void
}) {
  const { t } = useI18n()
  // в поле — ПОЛНЫЙ путь: у включённого дампа helper его и отдаёт, у нового собираем сами
  const [dir, setDir] = useState(
    init.dir.endsWith(suffix) ? init.dir : init.dir.replace(/\/+$/, '') + suffix,
  )
  const [keep, setKeep] = useState(init.keep)
  const [minfree, setMinfree] = useState(init.minfree)
  const dirOk = /^\/[A-Za-z0-9._/-]*$/.test(dir) && !dir.includes('..') && dir !== '/'
  return (
    <div className="dump-config">
      <div className="dump-cfg-title">{t('Настройки дампа')}</div>
      <label className="dump-cfg-row">
        <span>{t('Каталог дампов')}</span>
        <input className="field-inp dump-cfg-dir mono" value={dir} disabled={busy}
          onChange={(e) => setDir(e.target.value)} />
      </label>
      {!dirOk
        ? <div className="dump-cfg-hint warn">{t('Абсолютный путь, без «..» и не корень.')}</div>
        : !dir.startsWith('/backup') && (
          <div className="dump-cfg-hint">
            {t('Не /backup? Панель добавит этот путь в список файлового бэкапа, иначе restic дампы не заберёт.')}
          </div>
        )}
      <label className="dump-cfg-row">
        <span>{t('Хранить последних')}</span>
        <input className="field-inp dump-cfg-num" type="number" min={1} max={30} value={keep}
          disabled={busy} onChange={(e) => setKeep(Math.min(30, Math.max(1, Number(e.target.value) || 2)))} />
      </label>
      <label className="dump-cfg-row">
        <span>{t('Не делать, если свободного меньше, %')}</span>
        <input className="field-inp dump-cfg-num" type="number" min={0} max={50} value={minfree}
          disabled={busy} onChange={(e) => setMinfree(Math.min(50, Math.max(0, Number(e.target.value) || 0)))} />
      </label>
      <div className="dump-cfg-hint">
        {minfree > 0
          ? t('Дамп не запустится, если после него на разделе останется меньше {p}%. Вместо этого — алерт.', { p: minfree })
          : t('Защита от переполнения выключена: дамп снимается всегда.')}
      </div>
      <button className="svc-act dump-cfg-apply" disabled={busy || !dirOk}
        onClick={() => {
          // хвост движка дописываем, если его стёрли: путь в поле = то, что реально будет
          const full = dir.endsWith(suffix) ? dir : dir.replace(/\/+$/, '') + suffix
          setDir(full)
          onApply({ dump_dir: full.slice(0, -suffix.length) || '/', dump_keep: keep, dump_minfree: minfree })
        }}>
        {busy ? '…' : t('Применить')}
      </button>
    </div>
  )
}
