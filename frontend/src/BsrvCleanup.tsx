import { useState } from 'react'
import type { BsrvCleanup, BsrvCleanupLegacy, BsrvCleanupRot, OsFixStep, Server } from './api'
import { FixSteps } from './FixSteps'
import { useI18n } from './i18n'
import { shortList } from './serverUtils'

// Вкладка "Чистка" сервера бэкапов: у каждого пункта "что сделать" с готовыми командами под этот
// сервер и эти репозитории. Команды собирает бэкенд (app/bsrv_cleanup.py), здесь тексты шагов по id,
// плашки и строки "Что сломано". Раньше пункт был абзацем (18 имен через запятую в строку) без шагов,
// а один репозиторий шел двумя строками с разными причинами.

type TFn = (k: string, p?: Record<string, string | number>) => string

// Шаг словами по id; arg - что подставить (версия helper'а, новое имя, сколько блоков)
export function bsrvFixText(st: OsFixStep, t: TFn): string {
  switch (st.id) {
    case 'adopt_dry': return t('Пробный прогон, ничего не меняет: по каждому репозиторию helper покажет, сколько снапшотов удалит первая чистка (группы по хосту и тегам, как в чистке панели). Проверьте, что уходят только лишние старые снапшоты. Если restic не ответил, пароль в блоке не подходит, и такой репозиторий helper не перенесет.')
    case 'adopt_apply': return t('Перенести: у репозитория появится свой prune-скрипт с той же политикой и паролем из блока, чистка раз в сутки через 12 часов после бэкапа клиента, раз в неделю проверка целостности. Блок в старом скрипте закомментируется, копия файла - /etc/systemd-rest.conf.kervax-bak-<время>. На этом шаге ничего не удаляется.')
    case 'adopt_check': return t('Проверить: в cron есть время чистки, строк # kervax-moved в старом скрипте стало больше. Через минуту панель покажет у репозитория свою чистку.')
    case 'adopt_run': return t('Первая чистка пройдет сама во время из cron. Чтобы не ждать, запустите ее сейчас, но не во время бэкапа клиента: она удалит снапшоты сверх политики, это необратимо. В строке итога - сколько снапшотов было и стало.')
    case 'adopt_undo': return t('Пока чистка не прошла: вернуть старый скрипт из копии и убрать перенос. Снапшоты, которые чистка уже удалила, откат не вернет.')
    case 'dead_dry': return t('Пробный прогон, ничего не меняет: по каждому имени helper скажет, что репозитория нет и его блок будет закомментирован.')
    case 'dead_apply': return t('Закомментировать эти блоки (приставка # kervax-gone). Копия файла - /etc/systemd-rest.conf.kervax-bak-<время>. Если после правки файл не разбирается (sh -n), helper оставит его как был. Бэкапы и репозитории это не трогает.')
    case 'dead_check': return t('Проверить: файл разбирается (OK), закомментированных блоков не меньше {n}.', { n: st.arg ?? '' })
    case 'dead_undo': return t('Вернуть старый скрипт из копии.')
    case 'group_dry': return t('Сначала без --apply: покажет, сколько снапшотов группы и за какие даты удалится. Ничего не меняет. Группу, в которую бэкапились последнюю неделю, команда удалять откажется.')
    case 'group_apply': return t('Удалить снапшоты группы. Это необратимо: восстановиться из них будет нельзя. Место вернет ночная чистка.')
    case 'group_check': return t('Проверить: команда ответит, что снапшотов этой группы нет (no snapshots of host). Пункт уйдет после ночной чистки, она перепишет список групп.')
    case 'group_keep': return t('Если группу надо хранить: кнопка "в архив" выше. На сервере ничего не меняется, пункт уходит.')
    case 'merge_dry': return t('Сначала без --apply: покажет обе группы. Ничего не меняет.')
    case 'merge_apply': return t('Отдать снапшоты прежнего имени серверу {to}: данные не копируются, меняются только записи снапшотов. Дальше их чистит общая политика, лишние уйдут ночной чисткой, и это уже необратимо.', { to: st.arg ?? '' })
    case 'script_log': return t('Что делала чистка в последних прогонах: сколько снапшотов было и стало, ошибки restic.')
    case 'script_dry': return t('Что удалит политика сейчас, без удаления и без блокировки репозитория. Если remove 0, а снапшотов больше политики, они в разных группах (хост, теги), и снапшоты старой группы держатся.')
    case 'script_run': return t('Запустить чистку сейчас: она удалит снапшоты сверх политики, это необратимо.')
    case 'prune_log': return t('Что ответил restic в последнем прогоне чистки.')
    case 'prune_env': return t('Пароль для чистки лежит в env на сервере (строка RESTIC_PASSWORD). Правильный - у клиента в /etc/systemd-resta.conf или в панели, в данных восстановления его бэкапа.')
    case 'prune_policy': return t('Политика хранения в скрипте пустая (все keep-* = 0), и чистка пропускается, чтобы не удалить все. Задайте ее в строке KEEP=(...), например --keep-daily 7 --keep-weekly 4 --keep-monthly 6.')
    case 'prune_cron': return t('Есть ли запись в cron и запускал ли ее cron.')
    case 'prune_run': return t('Запустить чистку и посмотреть итог: в строке done должно быть success=1. Чистка удаляет снапшоты сверх политики, это необратимо.')
    case 'none_setup': return t('Чистки у репозитория на сервере нет: ни своего prune-скрипта, ни блока в старом скрипте. Ее заводит панель, когда бэкап клиента настраивают через нее. Клиента больше нет или бэкап разовый - уберите репозиторий в архив (раздел "Репозитории").')
    case 'helper_update': return t('Нужен helper backupserver-setup {v} или новее: обновите helper\'ы на этом сервере (главная, "Требует действий").', { v: st.arg ?? '' })
    default: return st.id
  }
}

// "что сделать" одной строкой: в плашке, в строке репозитория и в "Что сломано"
const BUCKET: Record<BsrvCleanupRot['cause'], string> = {
  legacy: 'legacy', legacy_off: 'legacy', old_group: 'old_group', none: 'none', script: 'other', unknown: 'other',
}

// Строки "старые снапшоты не вычищаются" для "Что сломано" и замечаний карточки: по одной на то, что
// делать (перевести на чистку панели, удалить старые группы, завести чистку), чтобы из самой строки
// было понятно действие. Без данных вкладки (старый бэкенд) - одна строка, как раньше.
export function rotationLines(s: Server, t: TFn): { key: string; text: string; n: number }[] {
  const cl = s.bsrv_cleanup
  const rot = s.bsrv_rotation ?? []
  if (!cl) return rot.length ? [{ key: 'all', n: rot.length, text: t('старые снапшоты не вычищаются - {list}', { list: rot.join(', ') }) }] : []
  const by = new Map<string, BsrvCleanupRot[]>()
  for (const x of cl.rotation) by.set(BUCKET[x.cause] ?? 'other', [...(by.get(BUCKET[x.cause] ?? 'other') ?? []), x])
  const out: { key: string; text: string; n: number }[] = []
  const names = (xs: BsrvCleanupRot[]) => shortList(xs.map((x) => x.repo), t, 4)
  const leg = by.get('legacy')
  if (leg) out.push({ key: 'legacy', n: leg.length, text: t('старые снапшоты не вычищаются, перевести на чистку панели - {list}', { list: names(leg) }) })
  const og = by.get('old_group')
  if (og)
    out.push({
      key: 'old_group', n: og.length,
      text: t('старые снапшоты не вычищаются, удалить старые группы или в архив - {list}', {
        list: shortList(og.map((x) => (x.hosts.length ? `${x.repo} (${x.hosts.join(', ')})` : x.repo)), t, 4),
      }),
    })
  const none = by.get('none')
  if (none) out.push({ key: 'none', n: none.length, text: t('старые снапшоты не вычищаются, у репозитория нет чистки - {list}', { list: names(none) }) })
  const other = by.get('other')
  if (other)
    out.push({
      key: 'other', n: other.length,
      text: t('старые снапшоты не вычищаются, разобрать в Чистке - {list}', {
        list: shortList(other.map((x) => (x.reasons.length ? `${x.repo} (${x.reasons.join(', ')})` : x.repo)), t, 3),
      }),
    })
  return out
}

// Что сделать с репозиторием, где старье не уходит, - строкой под причиной
export function rotationAction(cause: BsrvCleanupRot['cause'], t: TFn): string {
  switch (cause) {
    case 'legacy': case 'legacy_off': return t('Что сделать: перевести репозиторий на чистку панели.')
    case 'old_group': return t('Что сделать: удалить старую группу или, если ее надо хранить, убрать в архив.')
    case 'script': return t('Что сделать: посмотреть, что делает чистка и что удалит политика.')
    case 'none': return t('Что сделать: завести чистку или убрать репозиторий в архив.')
    default: return t('Что сделать: обновить helper backupserver-setup, по старому не видно, кто чистит репозиторий.')
  }
}

function FixToggle({ open, onClick, label }: { open: boolean; onClick: () => void; label?: string }) {
  const { t } = useI18n()
  return (
    <button className="ghost small row-act" onClick={onClick}>
      {open ? t('скрыть') : label ?? t('как исправить')}
    </button>
  )
}

// Старый общий скрипт с живыми (не в архиве) репозиториями. Не запланирован - их никто не чистит,
// красная плашка; запланирован - просто сведения, перевести на чистку панели можно по желанию.
export function LegacyPlaque({ leg, when, last }: { leg: BsrvCleanupLegacy; when: string; last: string }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const off = !leg.scheduled
  return (
    <div className="bsrv-clean">
      <div className={`osupd-adv ${off ? 'net-prob' : 'bsrv-clean-info'}`}>
        <span className="osupd-adv-text">
          {off
            ? t('Старый скрипт чистки не запускается: эти репозитории никто не чистит')
            : t('Старый скрипт {script} чистит репозиториев: {n}, в {when} ({where})', { script: leg.script, n: leg.repos.length, when, where: leg.where })}
        </span>
        {leg.fix.length > 0 && <FixToggle open={open} onClick={() => setOpen(!open)} />}
      </div>
      <div className="bsrv-clean-body">
        <div className="bsrv-clean-rows">
          {leg.repos.map((r) => (
            <div key={r.name} className="bsrv-clean-row">
              <span className={`lk-dot ${r.why.length ? 'lk-dot-warn' : 'lk-dot-info'}`} />
              <span className="mono bsrv-clean-name">{r.name}</span>
              <span className="muted small">
                {r.why.length ? r.why.join(', ') : t('снапшотов: {n}', { n: r.snaps })}
              </span>
            </div>
          ))}
        </div>
        <div className="muted small">
          {off
            ? [
              t('{script} не запланирован ни в cron, ни в systemd.', { script: leg.script }),
              last && t('Последний прогон {ago}.', { ago: last }),
              leg.archived > 0 && t('Еще на нем репозиториев в архиве: {n}, их чистить не нужно.', { n: leg.archived }),
            ].filter(Boolean).join(' ')
            : [last && t('Последний прогон {ago}.', { ago: last }),
              leg.archived > 0 && t('Еще на нем репозиториев в архиве: {n}, их чистить не нужно.', { n: leg.archived })].filter(Boolean).join(' ')}
        </div>
        <div className={off ? 'bsrv-clean-do' : 'muted small'}>
          {off
            ? t('Что сделать: перевести их на чистку панели. У каждого будет свой prune-скрипт с той же политикой и паролем, чистка раз в сутки и проверка целостности раз в неделю.')
            : t('Можно перевести их на чистку панели: она снимает висячие локи, пишет метрики, раз в неделю проверяет целостность и не держит вечно старые группы снапшотов.')}
        </div>
      </div>
      {open && <FixSteps steps={leg.fix} text={(st) => bsrvFixText(st, t)} />}
    </div>
  )
}

// Блоки старого скрипта на удаленные репозитории: имена списком в колонки, а не абзацем. Скрипт
// запланирован - он шлет по ним ошибку каждый день (желтое), нет - пока не мешают (спокойное).
export function DeadBlocks({ dead }: { dead: NonNullable<BsrvCleanup['dead']> }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return (
    <div className="bsrv-clean">
      <div className={`osupd-adv ${dead.scheduled ? 'net-note' : 'bsrv-clean-info'}`}>
        <span className="osupd-adv-text">
          {t('В {script} блоки на удаленные репозитории: {n}', { script: dead.script, n: dead.repos.length })}
        </span>
        {dead.fix.length > 0 && <FixToggle open={open} onClick={() => setOpen(!open)} label={t('как убрать')} />}
      </div>
      <div className="bsrv-clean-body">
        <div className="bsrv-clean-chips">
          {dead.repos.map((n) => <span key={n} className="mono">{n}</span>)}
        </div>
        <div className="muted small">
          {dead.scheduled
            ? t('Скрипт запускается каждый день, проверяет каждый такой блок и шлет ошибку, в этом шуме тонут настоящие. Блоки стоит убрать.')
            : t('Скрипт сейчас не запускается, и ошибок они не шлют. Если его снова запустят, придет ошибка по каждому, поэтому блоки лучше убрать.')}
        </div>
      </div>
      {open && <FixSteps steps={dead.fix} text={(st) => bsrvFixText(st, t)} />}
    </div>
  )
}

// Шаги одной строки (репозиторий, где старье не уходит, или неработающая чистка): кнопка и FixSteps
export function RowFix({ steps }: { steps: OsFixStep[] }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  if (!steps.length) return null
  return (
    <>
      <div className="bsrv-clean-rowfix"><FixToggle open={open} onClick={() => setOpen(!open)} /></div>
      {open && <FixSteps steps={steps} text={(st) => bsrvFixText(st, t)} />}
    </>
  )
}
