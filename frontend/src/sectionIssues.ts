import type { Section } from './App'
import type { Check, Server } from './api'
import { bsrvRepoProblems, type RepoProblemKind } from './bsrvRepos'
import { offLines, rotationLines } from './BsrvCleanup'
import { backupAlive, knownActive, srvIssues, srvMutesNow } from './serverUtils'

// Строки беды по разделам: "Что сломано" и плитки главной (HomePage.tsx) и плитки в шапках
// разделов (HomeTiles.tsx) считают по одним правилам, иначе "2 проблемы" на главной и "3" в разделе
type T = (k: string, p?: Record<string, string | number>) => string

// name - имя сервера: по нему "Что сломано" пишет "сервер - что" (text начинается с "имя: ")
export type ProbItem = {
  key: string; id?: number; down: boolean; text: string; srv?: boolean; cc?: string; name?: string
  since?: string | null // с какого момента (ISO), для "Что сломано"
  sec?: string // раздел карточки, куда ведет строка
}

// самое раннее из известных начал: у нескольких упавших контейнеров хоста - старшее
export function earliest(list: (string | null | undefined)[]): string | null {
  const ok = list.filter((x): x is string => !!x).sort()
  return ok[0] ?? null
}

// проблемы Docker: упавшие/циклящиеся контейнеры (по хостам)
export function dockerProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    const d = s.online ? s.last_report?.docker : undefined
    if (!d?.access || !d.containers) continue
    // Что сломано - решает бэкенд, как и в разделе "Докер" (docker_alerts - по чему ушел алерт):
    // упавший - должен работать и не поднялся за выдержку, крутящийся - счетчик перезапусков
    // растет прямо сейчас. Сам счетчик копится за всю жизнь контейнера: семь перезапусков могли
    // быть месяц назад или руками, а главная по нему поднимала ложную тревогу.
    const al = s.docker_alerts ?? {}
    // бэкенд ведет "лежит" и "в цикле" и без отправленного алерта; заглушенное (вид целиком или
    // контейнер кнопкой под алертом) на главную не идет, как и остальные заглушенные строки
    const mn = srvMutesNow(s)
    const off = (kind: string, name: string) => mn.has(kind) || mn.has(`${kind}:${name}`)
    const down = d.containers.filter((c) => al[c.name] === 'down' && !off('docker_down', c.name)).map((c) => c.name)
    const loop = d.containers.filter((c) => al[c.name] === 'loop' && !off('docker_loop', c.name)).map((c) => c.name)
    if (down.length > 0)
      out.push({
        key: `d-down-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'containers',
        since: earliest(down.map((n) => s.docker_since?.[n])),
        text: t(down.length === 1 ? '{name}: не работает контейнер {list}' : '{name}: не работают контейнеры {list}',
          { name: s.name, list: down.join(', ') }),
      })
    if (loop.length > 0)
      out.push({
        key: `d-loop-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'containers',
        text: t(loop.length === 1 ? '{name}: перезапускается в цикле {list}' : '{name}: перезапускаются в цикле {list}',
          { name: s.name, list: loop.join(', ') }),
      })
    // внешний прокси с docker-сокетом: не авария, а дыра - предупреждением, клик ведет в Docker,
    // где написано, как поправить
    const ex = s.docker_exposed ?? []
    if (ex.length > 0 && !(s.alert_mutes ?? []).includes('docker_sock'))
      out.push({
        key: `d-sock-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, sec: 'host',
        text: t('{name}: docker-сокет у {list} - взлом прокси даст root на хосте',
          { name: s.name, list: ex.map((e) => e.name).join(', ') }),
      })
  }
  return out
}

// проблемы бэкапов: клиенты (ошибка/несвежесть) + серверы (стоп/битые репо)
export function backupProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    if (!s.online) continue
    const b = s.last_report?.backup
    // настроен ли бэкап - то же правило, что у раздела "Бэкапы" и формы (backupAlive): метрика,
    // оставшаяся от снесенного бэкапа, не "бэкап упал", а "бэкап не настроен"
    const alive = backupAlive(b)
    const st = s.backup_state
    // заглушенный вид (насовсем или снузом) строку не дает: алерта по нему тоже не будет
    const mnow = srvMutesNow(s)
    const off = (k: string) => mnow.has(k)
    // Старая раскладка (ансибл без prom-метрики): время прогона агент берет из systemd, успех - из
    // Result юнита, как раздел "Бэкапы" (backupStatus) и алерт. Упал и не свежий - отдельно, как
    // два алерта; не свежий в Telegram красный, здесь тоже
    const sysd = b?.ts_source === 'systemd'
    if (alive && b && (b.metric_present || sysd)) {
      const res = (b.service_result || '').trim()
      const failed = sysd ? !!res && res !== 'success' : b.success === 0
      if (failed && !off('backup_failed')) out.push({ key: `b-fail-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'runs', since: s.alert_since?.backup_failed,
        text: st?.reason ? t('{name}: бэкап завершился с ошибкой - {why}', { name: s.name, why: st.reason }) : t('{name}: бэкап завершился с ошибкой', { name: s.name }) })
      if (b.last_backup_ts && Date.now() / 1000 - b.last_backup_ts > 2 * 86400 && !off('backup_stale'))
        out.push({ key: `b-stale-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'runs', since: s.alert_since?.backup_stale, text: t('{name}: бэкап не свежий', { name: s.name }) })
    }
    const bs = s.last_report?.backup_server
    // свои бэкапы ноды (настроены без панели): упал прогон, давно не отрабатывал, выключен
    const own = (s.custom_backups ?? []).filter((j) => ['failed', 'stale', 'disabled'].includes(j.status))
    if (own.length > 0 && !off('backup_custom')) {
      out.push({ key: `b-own-${s.id}`, id: s.id, name: s.name, cc: s.country, sec: 'own', down: own.some((j) => j.status === 'failed'), since: s.alert_since?.backup_custom,
        text: t('{name}: свой бэкап не отрабатывает — {jobs}', {
          name: s.name, jobs: own.map((j) => `${j.name} (${j.problem})`).join(', '),
        }) })
    }
    // прогон добавил в разы больше обычного (helper backup-setup 0.32, считает бэкенд)
    const g = s.backup_growth
    if (g && !off('backup_growth'))
      out.push({ key: `b-growth-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, sec: 'runs',
        since: new Date(g.ts * 1000).toISOString(), text: `${s.name}: ${g.text}` })
    const ownFiles = (s.custom_backups ?? []).some((j) => j.files && !j.ignored)
    // настройка из панели упала на полпути: в "Бэкапах" нода в "Проблемах" с кнопками
    if (off('backup_missing')) {
      // бэкап не настроен - заглушено (настройка, не прошедшая из панели, идет тем же алертом)
    } else if (!bs?.present && !alive && st?.state === 'partial') {
      out.push({ key: `b-part-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, since: st.since,
        text: t('{name}: настройка бэкапа не прошла - {why}', { name: s.name, why: st.reason || t('без причины') }) })
    } else if (!bs?.present && !alive && !s.backup_not_required && !ownFiles) {
      // сервер без настроенного бэкапа (и не помеченный «не требуется», и не сам бэкап-сервер)
      out.push({ key: `b-none-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, since: s.alert_since?.backup_missing,
        text: st?.traces ? t('{name}: бэкап не настроен ({traces})', { name: s.name, traces: st.traces }) : t('{name}: бэкап не настроен', { name: s.name }) })
    }
    if (bs?.present) {
      if (!bs.running) out.push({ key: `bs-stop-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'server', text: t('{name}: rest-server остановлен', { name: s.name }) })
      const smuted = mnow
      const now = Date.now() / 1000
      // репозитории - одной функцией со списком и бейджами карточки (bsrvRepoProblems)
      const probs = bsrvRepoProblems(s).filter((p) => !p.off)
      const names = (k: RepoProblemKind) => probs.filter((p) => p.kind === k).map((p) => p.repo).sort()
      // Висячий лок дольше суток - своей строкой, как и свой алерт backup_lock: в общем
      // счетчике было не видно ни что это лок, ни что из-за него не идет чистка. Лок идущего
      // бэкапа и висячий моложе суток проблемой не считаются (их снимут чистка и клиент).
      const locked = probs.filter((p) => p.kind === 'lock')
      if (locked.length > 0)
        out.push({
          key: `bs-lock-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'repos',
          since: new Date(Math.min(...locked.map((p) => p.since ?? now)) * 1000).toISOString(),
          text: t('{name}: залочен больше суток, чистка не идет - {list}', {
            name: s.name,
            list: locked.map((p) => `${p.repo} (${Math.floor((now - (p.since ?? now)) / 86400)} ${t('дн.')})`).join(', '),
          }),
        })
      // те же репозитории, что в алерте backup_repo: бэкенд считает их с ритмом бэкапов (клиент
      // без агента опаздывает через сутки с небольшим), без приглушенных и архива
      const badNames = names('repo')
      if (badNames.length > 0) out.push({ key: `bs-repo-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'repos', since: s.alert_since?.backup_repo,
        text: t('{name}: репозиториев с проблемой - {n} ({list})', { name: s.name, n: badNames.length,
          list: badNames.slice(0, 3).join(', ') + (badNames.length > 3 ? ', ...' : '') }) })
      // чистка не работает (не открывает репозиторий, падает, не запускается): те же
      // репозитории, что в алерте backup_prune, считает бэкенд
      const pruneNames = names('prune')
      if (pruneNames.length > 0)
        out.push({
          key: `bs-prune-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'cleanup',
          text: t('{name}: чистка не работает - {list}', {
            name: s.name, list: pruneNames.slice(0, 3).join(', ') + (pruneNames.length > 3 ? ', ...' : ''),
          }),
        })
      // ротация встала: считает бэкенд теми же правилами, что и алерт backup_rotation. По строке на
      // то, что делать (перевести на чистку панели, удалить старые группы...), строка ведет на
      // вкладку "Чистка" карточки, где шаги с командами
      if ((s.bsrv_rotation ?? []).length > 0 && !smuted.has('backup_rotation'))
        for (const x of rotationLines(s, t))
          out.push({
            key: `bs-rot-${s.id}-${x.key}`, id: s.id, name: s.name, cc: s.country, down: false, srv: true, sec: 'cleanup',
            text: `${s.name}: ${x.text}`,
          })
      // чистку выключили, а бэкапы идут; чистка идет, хотя ее выключали в crontab (helper 0.34).
      // Не алерт: это решение человека, но его видно, пока не решено
      for (const x of offLines(s, t))
        out.push({
          key: `bs-off-${s.id}-${x.key}`, id: s.id, name: s.name, cc: s.country, down: false, srv: true, sec: 'cleanup',
          text: `${s.name}: ${x.text}`,
        })
    }
  }
  return out
}

// строка беды для плитки: сервер, что с ним и куда вести (как onOpen главной)
export type SecIssue = {
  key: string; id: number; name: string; text: string; down: boolean; section: Section; srv?: boolean; sec?: string
}
// проблемы серверов (srvIssues), раздел строки - по keep: на главной и в Серверах серверы и службы,
// в Сервисах только службы, в Кубере кубер
export function srvIssueRows(servers: Server[], t: T, keep: (section: string) => boolean): SecIssue[] {
  return servers.flatMap((s) =>
    srvIssues(s, t)
      .filter((i) => keep(i.section ?? 'servers'))
      .map((i, n) => ({
        key: `${s.id}-${n}`, id: s.id, name: s.name, text: i.text, down: i.tone === 't-down',
        section: (i.section ?? 'servers') as Section, srv: i.srv ?? true, sec: i.sec,
      })),
  )
}
// ProbItem (Docker, бэкапы) пишут "имя: что" - в плитке имя отдельно
export function probRows(items: ProbItem[], section: Section): SecIssue[] {
  return items.map((p) => ({
    key: p.key, id: p.id ?? 0, name: p.name ?? '', down: p.down, section, srv: p.srv, sec: p.sec,
    text: p.name && p.text.startsWith(p.name + ': ') ? p.text.slice(p.name.length + 2) : p.text,
  }))
}
// проблемы сверху, внутри - по имени, как в "Что сломано"
export function byTone(xs: SecIssue[]): SecIssue[] {
  return [...xs].sort((a, b) => (a.down === b.down ? a.name.localeCompare(b.name) : a.down ? -1 : 1))
}

// беды сайтов: лежащие, деградация и "через раз" (кроме отмеченных известной проблемой - они своим
// блоком) и признаки взлома. Как блок "Сайты" и "Что сломано" на главной
export function siteIssueRows(checks: Check[], t: T): SecIssue[] {
  const st = (c: Check) =>
    c.last_status === 'down' ? t('Недоступно') : c.last_status === 'degraded' ? t('Деградация') : t('Отвечает через раз')
  return [
    ...checks
      .filter((c) => c.enabled && (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since) && !knownActive(c.known_issue))
      .map((c) => ({
        key: `dc-${c.id}`, id: c.id, name: c.name, down: c.last_status === 'down', section: 'sites' as Section, srv: false,
        text: st(c) + (c.last_status !== 'up' && c.last_message ? ` · ${c.last_message}` : ''),
      })),
    ...checks
      .filter((c) => c.enabled && !!c.tamper && c.tamper.level !== 'none')
      .map((c) => ({
        key: `tm-${c.id}`, id: c.id, name: c.name, down: c.tamper!.level === 'hack', section: 'sites' as Section,
        srv: false, sec: 'tamper', text: c.tamper!.summary,
      })),
  ]
}
