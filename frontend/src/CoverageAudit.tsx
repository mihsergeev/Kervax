import { useEffect, useRef, useState } from 'react'
import { backupCommand, backupCommandStatus, updateServer, type BackupCommand, type Server, backupAuditMute } from './api'
import { CommandProgress, nextPhase, type CmdProgressState } from './CommandProgress'
import { helperText } from './helperText'
import { useI18n } from './i18n'
import { EngineIcon } from './engineIcon'
import { byteUnits } from './units'

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
function instLabel(inst: string): string {
  if (!isKube(inst)) return inst
  const [, ns, , ...name] = inst.split('.')
  return `☸ ${ns}/${name.join('.')}`
}

// Аудит покрытия: панель сверяет, что на ноде есть, с тем, что реально попадает в бэкап.
// Две категории: ДЫРА (данных в бэкапе нет) и РИСК (данные есть, но восстановимость под
// вопросом — живая СУБД). Алертов по этому намеренно нет: сначала смотрим, не шумит ли.
export function CoverageAudit({ server: s, canManage, onChanged }: {
  server: Server; canManage: boolean; onChanged: () => void
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
  const [showMuted, setShowMuted] = useState(false)
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
        phase('error', { ok: false, text: helperText(res.result, t) || t('не удалось') })
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
      phase('error', { ok: false, text: e instanceof Error ? e.message : t('ошибка') })
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
  const dumpLine = (engine?: string, inst?: string) => {
    const d = findDump(engine, inst)
    if (!d) return null
    const dir = d.dir || '/backup'
    return (
      <div className="svc-dump-on small">
        ✓ {dumpsAreLocalOnly
          ? t('дамп включён — снимается раз в сутки по своему таймеру в {dir}, хранится {k} последних',
            { dir, k: d.keep })
          : t('дамп включён — снимается перед каждым бэкапом в {dir}, хранится {k} последних',
            { dir, k: d.keep })}
        <span className="muted">
          {(d.min_free_pct ?? 0) > 0
            ? ` · ${t('не запускать при <{p}% свободного', { p: d.min_free_pct! })}`
            : ` · ${t('без защиты от переполнения')}`}
          {' · '}{t('файлов: {n}', { n: d.files })}
          {d.size_bytes > 0 && ` · ${fmtSize(d.size_bytes)}`}
          {' · '}{fmtWhen(d.last_ts, t)}
        </span>
        {engine === 'PostgreSQL' && (
          <div className="dump-cfg-hint">
            {t('PostgreSQL: каждая база — отдельный файл + globals (роли/права).')}
          </div>
        )}
        {/* без файлового бэкапа helper запускает дамп своим таймером (03:00 + до 15 мин) */}
        <div className="dump-cfg-hint">
          {dumpsAreLocalOnly
            ? t('Файлового бэкапа на ноде нет: дамп снимается ночью, около 03:00 по времени ноды, и хранится только на ней. Восстановиться с самой ноды можно, копии за её пределами нет.')
            : t('По расписанию сначала снимается дамп, затем restic бэкапит файлы (включая свежий дамп) — одним запуском, последовательно. Долгий дамп задержит начало restic, но не сорвёт его.')}
          {!dumpsAreLocalOnly && hasSchedule && (
            <>
              {' '}
              <button className="linklike" onClick={() => {
                document.getElementById('backup-manage-schedule')
                  ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
              }}>
                {t('Поменять время бэкапа →')}
              </button>
            </>
          )}
        </div>
        {d.skipped && (
          <div className="form-error small">
            ⚠️ {t('последний прогон пропущен: на разделе было свободно {p}% (< порога) — расчистите место',
              { p: d.skip_free_pct ?? 0 })}
          </div>
        )}
        {canManage && (
          <>
            <button className="svc-act" disabled={busyAny}
              onClick={() => setCfgFor(cfgFor === (inst || engine || '') ? null : (inst || engine || ''))}>
              {t('настроить')}
            </button>
            <button className="svc-act svc-act-off" disabled={busyAny}
              onClick={() => dumpAction('dump_remove', d.engine, d.container || '')}>
              {t('выключить')}
            </button>
          </>
        )}
        {canManage && cfgFor === (inst || engine || '') && (
          <DumpConfig
            init={{ dir, keep: d.keep, minfree: d.min_free_pct ?? 10 }}
            suffix={`/${d.engine}${d.container ? '/' + d.container : ''}`}
            busy={busyAny}
            onApply={(o) => dumpAction('dump_setup', d.engine, d.container || '', o)}
          />
        )}
      </div>
    )
  }
  // ранний выход — после всех хуков: иначе при опустевшем списке менялся бы их порядок
  if (items.length === 0) return null
  return (
    <div className="coverage-audit">
      <div className="backup-manage-head">{t('Покрытие')}</div>
      {gaps.length > 0 && (
        <div className="coverage-group">
          <div className="form-error small">
            ⚠️ {t('Не попадает в бэкап ({n}):', { n: gaps.length })}
          </div>
          {gaps.map((x, i) => (
            <div key={i} className="coverage-item">
              <span className="mono">{x.subject}</span>
              <span className="muted small"> — {x.detail}</span>
              {muteBtn(x)}
            </div>
          ))}
        </div>
      )}
      {(risks.length > 0 || (dbSelfManaged && dbRisks.length > 0)) && (
        <div className="coverage-group">
          {risks.length > 0 && (
            <div className="t-degraded small">
              🛢 {t('Восстановимость под вопросом ({n}):', { n: risks.length })}
            </div>
          )}
          {/* при «бэкаплю все базы сам» находки-СУБД свёрнуты — показываем счётчик и кнопку */}
          {dbSelfManaged && dbRisks.length > 0 && (
            <button className="ghost repo-cleanup-toggle" onClick={() => setShowDbRisks(!showDbRisks)}>
              {showDbRisks
                ? t('свернуть базы (бэкаплю сам)')
                : t('🛢 базы, которые бэкаплю сам: {n} — показать', { n: dbRisks.length })}
            </button>
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
          {canDumpHere && dumpsAreLocalOnly && risks.some((x) => x.dump_engine && x.can_dump) && (
            <div className="muted small coverage-dump-hint">
              {t('Файлового бэкапа на ноде нет: дампы лягут локально (ежедневно, свой таймер) — восстановиться с самой ноды можно, копии за её пределами не будет.')}
            </div>
          )}
          {risks.map((x, i) => (
            <div key={i} className="svc-card">
              <div className="svc-card-head">
                <span className="svc-ico"><EngineIcon name={x.subject} /></span>
                <span className="svc-name">{x.subject}</span>
                {x.instance && <span className="type-chip mono">{instLabel(x.instance)}</span>}
                {/* действия — единой группой справа. Раньше margin-left:auto висел
                    и на кнопке, и на колокольчике: свободное место делилось между
                    ними, и кнопка вставала тем левее, чем длиннее имя движка —
                    в списке они шли лесенкой. */}
                <span className="svc-head-act">
                  {canDumpHere && x.dump_engine && x.can_dump && !findDump(x.dump_engine, x.instance) &&
                    !(opActive && opFor(x.dump_engine, x.container || '')) && (
                    <button className="svc-act svc-act-primary" disabled={busyAny}
                      onClick={() => setCfgFor(cfgFor === (x.instance || x.dump_engine!) ? null : (x.instance || x.dump_engine!))}>
                      {t('включить дампы')}
                    </button>
                  )}
                  {muteBtn(x)}
                </span>
              </div>
              <div className="svc-card-detail muted small">{x.detail}</div>
              {dumpLine(x.dump_engine, x.instance)}
              {/* форма настроек ДО первого включения: у ещё-не-включённого дампа dumpLine
                  ничего не рисует, поэтому форму показываем здесь */}
              {canDumpHere && x.dump_engine && x.can_dump && !findDump(x.dump_engine, x.instance) &&
                cfgFor === (x.instance || x.dump_engine) && x.downtime && (
                /* цена включения должна быть видна ДО нажатия «применить», а не
                   обнаружиться потом по графику доступности */
                <div className="coverage-downtime small">⏸ {x.downtime}</div>
              )}
              {canDumpHere && x.dump_engine && x.can_dump && !findDump(x.dump_engine, x.instance) &&
                cfgFor === (x.instance || x.dump_engine) && (
                <DumpConfig
                  init={{ dir: '/backup', keep: 2, minfree: 10 }}
                  suffix={`/${x.dump_engine}${x.container ? '/' + x.container : ''}`}
                  busy={busyAny}
                  onApply={(o) => dumpAction('dump_setup', x.dump_engine!, x.container || '', o)}
                />
              )}
              {opBlock(x.dump_engine, x.container || '')}
            </div>
          ))}
          {/* итог, если карточка, у которой шла команда, пропала из списков (например,
              контейнер удалили, пока ждали отчёт) — иначе ответ сервера просто исчез бы */}
          {op?.msg && (op.progress.phase === 'done' || op.progress.phase === 'error') &&
            !live.some((x) => `${x.dump_engine ?? ''}|${x.container || ''}` === op.key) && (
            <div className={`small ${op.msg.ok ? 't-up' : 'form-error'}`}>{op.msg.text}</div>
          )}
          {dumpMsg && !dumpBusy && (
            <div className={`small ${dumpMsg.ok ? 't-up' : 'form-error'}`}>{dumpMsg.text}</div>
          )}
        </div>
      )}
      {okItems.length > 0 && (
        <div className="coverage-group">
          {okItems.map((x, i) => (
            <div key={i} className="svc-card svc-card-ok">
              <div className="svc-card-head">
                <span className="svc-ico"><EngineIcon name={x.subject} /></span>
                <span className="svc-name">{x.subject}</span>
                {x.instance && <span className="type-chip mono">{instLabel(x.instance)}</span>}
                <span className="t-up small svc-ok-tag">✓</span>
              </div>
              <div className="svc-card-detail muted small">{x.detail}</div>
              {dumpLine(x.dump_engine, x.instance)}
              {opBlock(x.dump_engine, x.container || '')}
            </div>
          ))}
        </div>
      )}
      {hasDb && canManage && (
        <>
          <label className="nobackup-check">
            <input type="checkbox" checked={dumpsOk} disabled={busy} onChange={toggleDumps} />
            {t('Все базы этой ноды бэкаплю сам — не напоминать')}
          </label>
          {/* Объясняем прямо здесь: галка НЕ настраивает дамп, а только гасит напоминание.
              «Эти базы» путало — теперь явно «все базы ноды» + отличие от точечного 🔕. */}
          <div className="muted small coverage-hint">
            {t('Скопом гасит напоминание про ВСЕ найденные базы этой ноды (список выше). Панель видит только дампы, которые настроила сама или нашла CronJob’ом в кластере. Если базы вы бэкапите своим способом (свой cron, ansible, managed-база у провайдера) — отметьте. Одну базу — точечно кнопкой 🔕 на её карточке.')}
            <br />
            {t('Только гасит напоминание: на сервере ничего не меняется, автопроверка выше работает. Не ставьте, если дампов на самом деле нет, — иначе спрячете реальную дыру в бэкапе.')}
          </div>
        </>
      )}
      {mutedItems.length > 0 && (
        <div className="coverage-group coverage-muted">
          <button className="ghost repo-cleanup-toggle" onClick={() => setShowMuted(!showMuted)}>
            {showMuted ? t('скрыть') : t('🔕 приглушено вручную: {n}', { n: mutedItems.length })}
          </button>
          {showMuted && mutedItems.map((x, i) => (
            <div key={i} className="coverage-item">
              <span className="mono muted">{x.subject}</span>
              <span className="muted small"> — {x.detail}</span>
              {canManage && x.key && (
                <button className="ghost icon-btn coverage-mute" disabled={muteBusy !== null}
                  title={t('Вернуть в проблемы')}
                  onClick={() => muteFinding(x.key!, false)}>
                  {muteBusy === x.key ? '…' : '🔔'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="muted small">
        {t('Сверяется автоматически: точки монтирования, bind-mount ы контейнеров, живые СУБД и тома kubernetes.')}
        {/* proc_full=false → на ноде hidepid, агент не видит чужие процессы: нативно
            установленные СУБД других юзеров могут не попасть в детект (контейнерные и
            kube видны по образам). Честно предупреждаем, что список может быть неполным. */}
        {s.last_report?.caps && s.last_report.caps.proc_full === false && (
          <div className="dump-cfg-hint warn" style={{ marginTop: 4 }}>
            ⚠️ {t('На ноде включён hidepid — нативно установленные СУБД других пользователей могут быть не видны (в контейнерах и kubernetes — видны). Проверьте такие базы вручную.')}
          </div>
        )}
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
