// Тексты проблем и советов бэкенд собирает по-русски из данных ("очередь orders растет 47 мин..."),
// и английская панель показывала их как есть. Здесь перевод самых частых по кускам фраз. Правило
// одно: перевод принимается, только если в строке не осталось кириллицы, иначе строка остается
// как пришла - полурусская-полуанглийская строка хуже русской. Новую фразу бэкенда сюда надо
// дописать, иначе в английской панели она так и будет по-русски.

type Rule = [RegExp, string]

// Длинные фразы раньше коротких: "открыто в интернет" входит в "служебное открыто в интернет"
const PHRASES: Rule[] = [
  // настроен ли бэкап (app/backup_state.py) и форма настройки
  [/осталась метрика старого бэкапа от (\S+), restic и таймера бэкапа нет/g,
    'a metric of an old backup from $1 is left, there is no restic and no backup timer'],
  [/осталась метрика старого бэкапа, restic и таймера бэкапа нет/g,
    'a metric of an old backup is left, there is no restic and no backup timer'],
  [/на этом сервере уже есть бэкап - отметьте "переустановить поверх"/g,
    'this server already has a backup - tick "reinstall over it"'],
  [/на сервере работает бэкап - удалять его настройку отсюда нельзя/g,
    'a backup is working on this server - its setup cannot be removed from here'],
  // чистка бэкап-сервера (collector.rotation_stale_repos, rotation_overflow)
  [/сервер переименовали из (\S+): (\d+) (?:снапшот|снапшота|снапшотов) под старым именем, по (\S+)/g,
    'the server was renamed from $1: $2 snapshots under the old name, up to $3'],
  [/группа (\S+) больше не бэкапится: (\d+) (?:снапшот|снапшота|снапшотов), по (\S+)/g,
    'group $1 no longer backs up: $2 snapshots, up to $3'],
  [/, и еще старых групп: (\d+)/g, ', $1 more old groups'],
  [/(?<!\d)1 snapshots/g, '1 snapshot'],
  [/, похоже на старую группу: менялись хост или пути/g, ', looks like an old group: the host or paths changed'],
  [/(\d+) снапшотов при политике (\d+), удалений нет/g, '$1 snapshots with a policy of $2, nothing removed'],
  [/старейшему снапшоту (\d+) дн\. при политике до (\d+)/g, 'the oldest snapshot is $1 d old with a policy of up to $2'],
  // мьют поломки диска снят сам (app/disk_mute.py)
  [/мьют снят: диски? (.+?) заменены?, массив собран/g, 'mute lifted: disk $1 replaced, array rebuilt'],
  [/мьют снят: диск заменен, массив собран/g, 'mute lifted: disk replaced, array rebuilt'],
  // порты и доступы
  [/в интернет открылся новый порт: /g, 'new port open to the internet: '],
  [/служебное открыто в интернет: /g, 'service ports open to the internet: '],
  [/открыто в интернет: /g, 'open to the internet: '],
  [/изменился доступ: /g, 'access changed: '],
  [/добавлен SSH-ключ пользователю /g, 'SSH key added for '],
  [/удален SSH-ключ пользователя /g, 'SSH key removed for '],
  [/ \(ключ уже был на серверах парка\)/g, ' (the key was already on fleet servers)'],
  [/вход по паролю: (\S+) с (\S+), раньше с этого адреса не входил/g,
    'password login: $1 from $2, never logged in from this address before'],
  [/идет перебор паролей SSH: (\d+) неудачных входов за 10 минут/g, 'SSH brute force: $1 failed logins in 10 minutes'],
  [/\(больше всех с (\S+): (\d+)\)/g, '(most from $1: $2)'],
  [/, вход по паролю включен, а fail2ban и подобного нет/g, ', password login is on and there is no fail2ban or similar'],
  [/SSH пускает по паролю, а порт (.+?) открыт в интернет: оставьте вход по ключам \(PasswordAuthentication no\)/g,
    'SSH accepts passwords and port $1 is open to the internet: allow key-only login (PasswordAuthentication no)'],
  [/, от перебора не защищает ни fail2ban, ни sshguard/g, ', nothing like fail2ban or sshguard stops brute force'],
  [/root может войти по SSH паролем, а порт (.+?) открыт в интернет/g,
    'root can log in over SSH with a password and port $1 is open to the internet'],
  // обновления ОС
  [/исправления Ubuntu Pro \(ESM\) для (.+?) кончатся (\S+), через /g, 'Ubuntu Pro (ESM) fixes for $1 end on $2, in '],
  [/обычная поддержка (.+?) кончилась (\S+): исправления безопасности дальше только в Ubuntu Pro \(ESM до (\S+), pro attach\) или после переезда на новую LTS/g,
    'standard support for $1 ended on $2: security fixes now come only with Ubuntu Pro (ESM until $3, pro attach) or after moving to a newer LTS'],
  [/поддержка (.+?) кончилась (\S+), ESM тоже \((\S+)\): обновлений безопасности больше нет/g,
    'support for $1 ended on $2, ESM too ($3): no more security updates'],
  [/поддержка (.+?) кончилась (\S+): обновлений безопасности больше нет/g, 'support for $1 ended on $2: no more security updates'],
  [/поддержка (.+?) кончится (\S+), через /g, 'support for $1 ends on $2, in '],
  [/нужна перезагрузка уже (.+?) \(обновлены (.+?)\)/g, 'reboot pending for $1 (updated $2)'],
  [/нужна перезагрузка уже /g, 'reboot pending for '],
  [/плановая перезагрузка: после обновления ее ждали/g, 'planned reboot: expected after updates'],
  [/плановая перезагрузка: ждало новое ядро (\S+)/g, 'planned reboot: new kernel $1 was waiting'],
  [/в \/boot ядро (\S+), а работает (\S+): нужна перезагрузка/g, 'kernel $1 is in /boot but $2 is running: reboot needed'],
  [/не установлено (\d+) (?:обновление|обновления|обновлений) безопасности: /g, '$1 security updates not installed: '],
  [/автообновления выключены/g, 'automatic updates are off'],
  [/unattended-upgrades не установлен/g, 'unattended-upgrades is not installed'],
  [/автообновления не запускались/g, 'automatic updates have not run'],
  [/список пакетов не обновлялся (.+?): о новых обновлениях нода не знает/g,
    'package lists not refreshed for $1: the node does not know about new updates'],
  // OOM (app/oom.py): пункт "Что сломано" и юнит, убитый OOM. Виновник (из-за ...) - до двоеточия,
  // его "процесс"/"контейнер"/"под" переводят общие куски ниже
  [/нехватка памяти \(OOM\) из-за (.+?): (\d+) раз за сутки, последний (\d+) мин назад, убиты /g,
    'out of memory (OOM) caused by $1: $2 times in a day, the last $3 min ago, killed '],
  [/нехватка памяти \(OOM\) из-за (.+?): (\d+) раз за сутки, последний (\d+) ч назад, убиты /g,
    'out of memory (OOM) caused by $1: $2 times in a day, the last $3 h ago, killed '],
  [/нехватка памяти \(OOM\) из-за (.+?): была (\d+) мин назад, убиты /g, 'out of memory (OOM) caused by $1 $2 min ago, killed '],
  [/нехватка памяти \(OOM\) из-за (.+?): была (\d+) ч назад, убиты /g, 'out of memory (OOM) caused by $1 $2 h ago, killed '],
  [/нехватка памяти \(OOM\): (\d+) раз за сутки, последний (\d+) мин назад, убиты /g, 'out of memory (OOM): $1 times in a day, the last $2 min ago, killed '],
  [/нехватка памяти \(OOM\): (\d+) раз за сутки, последний (\d+) ч назад, убиты /g, 'out of memory (OOM): $1 times in a day, the last $2 h ago, killed '],
  [/нехватка памяти \(OOM\): была (\d+) мин назад, убиты /g, 'out of memory (OOM) $1 min ago, killed '],
  [/нехватка памяти \(OOM\): была (\d+) ч назад, убиты /g, 'out of memory (OOM) $1 h ago, killed '],
  // частые перезапуски (app/restarts.py): причина прошлого завершения пода и пачки
  [/поды перезапускаются пачками: (\d+) раза? за сутки по (\S+) разом/g, 'pods restart in bursts: $1 times a day, $2 at once'],
  [/ - подвисает нода (\S+): /g, ' - node $1 stalls: '],
  [/ - подвисает kube-api или etcd: /g, ' - kube-api or etcd stalls: '],
  [/пробы и запросы к kube-api не дождались ответа/g, 'probes and kube-api requests timed out'],
  [/в эти минуты процессы ждали (.+?) времени/g, 'in those minutes processes waited for $1 of the time'],
  [/, смотрите, что грузит ноду/g, ', check what loads the node'],
  [/память (\d+)%/g, 'memory $1%'],
  [/диск (\d+)%/g, 'disk $1%'],
  [/^Диск (\d+)%/g, 'Disk $1%'],
  [/поды (\S+) \((\d+) шт\.\) перезапускались (\d+) раза? за сутки/g, 'pods $1 ($2) restarted $3 times a day'],
  [/под (\S+) перезапускался (\d+) раза? за сутки/g, 'pod $1 restarted $2 times a day'],
  [/контейнер (\S+) перезапускался (\d+) раза? за сутки/g, 'container $1 restarted $2 times a day'],
  [/, последний (\d+) мин назад/g, ', the last $1 min ago'],
  [/, последний (\d+) ч назад/g, ', the last $1 h ago'],
  [/OOMKilled при лимите /g, 'OOMKilled at the limit '],
  [/OOMKilled, лимита нет: кончилась память ноды/g, 'OOMKilled, no limit: the node ran out of memory'],
  [/убит по (liveness|startup)-пробе/g, 'killed by the $1 probe'],
  [/сам вышел с кодом 0/g, 'exited by itself with code 0'],
  [/: убит OOM killer/g, ': killed by the OOM killer'],
  [/\(сессия ([^)]+)\)/g, '(session $1)'],
  [/\(контейнер ([^)]+)\)/g, '(container $1)'],
  [/\(под ([^)]+)\)/g, '(pod $1)'],
  [/(\d)гб(?![а-яё])/g, '$1 GB'],
  [/(\d)мб(?![а-яё])/g, '$1 MB'],
  [/(\d)кб(?![а-яё])/g, '$1 KB'],
  // давление на ресурсы и зависшие процессы (collector.pressure_problems)
  [/не хватает памяти: все процессы разом стоят ([\d.]+)% времени/g, 'out of memory: all processes stalled $1% of the time'],
  [/не хватает памяти: процессы ждут память ([\d.]+)% времени/g, 'out of memory: processes wait for memory $1% of the time'],
  [/диск не успевает: все процессы разом ждут диск ([\d.]+)% времени/g, 'disk cannot keep up: all processes wait for disk $1% of the time'],
  [/диск не успевает: процессы ждут диск ([\d.]+)% времени/g, 'disk cannot keep up: processes wait for disk $1% of the time'],
  [/на сервере бэкапов так бывает во время prune и check, но не дольше (\d+) ч/g, 'on a backup server this happens during prune and check, but not for longer than $1 h'],
  [/файловые дескрипторы заняты на (\d+)% \((\d+) из (\d+)\)/g, 'file descriptors $1% used ($2 of $3)'],
  [/зависли в ожидании диска или сети (\d+) (?:процесса|процессов): /g, '$1 processes stuck waiting for disk or network: '],
  [/завис (.+?) \(PID (\d+)\) в ожидании диска или сети /g, '$1 (PID $2) stuck waiting for disk or network for '],
  [/ядро видит зависшие задачи: /g, 'the kernel sees hung tasks: '],
  [/hung_task сработал 1 раз за 10 мин/g, 'hung_task fired once in 10 min'],
  [/hung_task сработал (\d+) раза? за 10 мин/g, 'hung_task fired $1 times in 10 min'],
  // диски
  [/за сутки выросли ошибки - /g, 'errors grew in 24 h - '],
  [/переназначенные сектора/g, 'reallocated sectors'],
  [/нечитаемые сектора/g, 'unreadable sectors'],
  [/износ ([\d.]+)%, 90% примерно к /g, 'wear $1%, 90% around '],
  [/износ ([\d.]+)%, пора планировать замену/g, 'wear $1%, time to plan a replacement'],
  [/ заполнится через /g, ' will be full in '],
  [/(^|\s)диск /g, '$1disk '],
  // веб
  [/ошибки 5xx: ([\d.]+)% запросов за 15 минут/g, '5xx errors: $1% of requests in 15 minutes'],
  [/ не успевает принимать соединения: очередь сокета полна/g, ' cannot accept connections fast enough: socket queue is full'],
  [/ не принимает соединения/g, ' refuses connections'],
  [/ не принял соединение вовремя/g, ' did not accept the connection in time'],
  [/ не ответил вовремя/g, ' timed out'],
  [/ без живых серверов/g, ' has no live servers'],
  [/ недоступен по сети/g, ' is unreachable'],
  [/ закрыл соединение, не ответив/g, ' closed the connection without a response'],
  [/ сбросил соединение/g, ' reset the connection'],
  [/сертификат (\S+) истекает через /g, 'certificate $1 expires in '],
  // сервисы
  [/очередь (\S+) (\(.+?\) )?растет (.+?) и нет ни одного потребителя: (.+?) -> (.+?) сообщений/g,
    'queue $1 $2has been growing for $3 with no consumers: $4 -> $5 messages'],
  [/очередь (\S+) (\(.+?\) )?растет (.+?) и не разбирается: (.+?) -> (.+?) сообщений/g,
    'queue $1 $2has been growing for $3 and nobody drains it: $4 -> $5 messages'],
  [/, самая старая задача ждет /g, ', the oldest job has been waiting for '],
  [/воркер (\S+) \((.+?)\) не запускается, (supervisor|pm2) перестал его поднимать/g,
    'worker $1 ($2) does not start, $3 gave up restarting it'],
  [/крон (на хосте|в контейнере \S+|в поде \S+) \((.+?)\) /g, 'cron job $1 ($2) '],
  [/наслаивается: идут (\d+) (?:копия|копии|копий), самая старая /g, 'overlaps: $1 copies running, the oldest for '],
  [/висит /g, 'hangs for '],
  [/обычно меньше минуты/g, 'usually under a minute'],
  [/обычно до /g, 'usually up to '],
  [/ транзакция идет (.+?) \(база (\S+), /g, ' a transaction has been running for $1 (database $2, '],
  [/: занято (\d+)% maxmemory \((.+?) из (.+?)\), при 100% запись начнет отклоняться/g,
    ': $1% of maxmemory used ($2 of $3), at 100% writes will be rejected'],
  // кубер
  [/Flux: доставка встала, /g, 'Flux: delivery stopped, '],
  [/Helm не смог обновить релиз/g, 'Helm failed to upgrade the release'],
  [/Helm не смог поставить релиз/g, 'Helm failed to install the release'],
  [/токен Flux в секрете /g, 'Flux token in secret '],
  [/истекает через /g, 'expires in '],
  // ошибки в логах (log_errors.py) и рост памяти (mem_trend.py)
  [/ошибки в логе: /g, 'log errors: '],
  [/ строк с ошибками за час, обычно около ([\d ]+?) в час/g, ' error lines in an hour, usually about $1 an hour'],
  [/ строк с ошибками за час, обычно их почти нет/g, ' error lines in an hour, usually almost none'],
  [/ строк с ошибками за час, обычного еще не знаем: истории меньше 4 часов/g,
    ' error lines in an hour, the usual rate is not known yet: less than 4 hours of history'],
  [/, держится (\d+) ч (\d+) мин/g, ', lasting $1 h $2 min'],
  [/, держится (\d+) ч/g, ', lasting $1 h'],
  [/, держится (\d+) мин/g, ', lasting $1 min'],
  [/(\d+) (?:пода|подов) /g, '$1 pods '],
  [/память контейнера (.+?) растет (\d+) суток подряд: /g, 'memory of container $1 has grown $2 days in a row: '],
  [/память пода (.+?) растет (\d+) суток подряд: /g, 'memory of pod $1 has grown $2 days in a row: '],
  [/память процесса (.+?) растет (\d+) суток подряд: /g, 'memory of process $1 has grown $2 days in a row: '],
  [/ в сутки, свободной памяти сервера \((.+?)\) при таком росте хватит примерно на (\S+) сут/g,
    ' a day, at this rate the free memory of the server ($1) lasts about $2 days'],
  [/ в сутки/g, ' a day'],
  [/\(пользователь (\S+)\)/g, '(user $1)'],
  // строки шума в истории алертов (alert_what.py)
  [/ выше порога/g, ' above threshold'],
  [/^до ([\d.]+%)/g, 'up to $1'],
]

// Общие куски, которые встречаются внутри фраз
const PARTS: Rule[] = [
  [/процесс (\S+), пользователь ([^\s),]+)/g, 'process $1, user $2'],
  [/(^|[\s(])процесс /g, '$1process '],
  [/(^|[\s(])контейнер /g, '$1container '],
  [/(^|[\s(])под (?=\S)/g, '$1pod '],
  [/ на хосте/g, ' on the host'],
  [/(^|[\s(])на хосте/g, '$1on the host'],
  [/ на (\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]+:[0-9a-f:]+)/g, ' on $1'],
  [/ и еще (\d+)/g, ' and $1 more'],
  [/(\d+) сут (\d+) ч(?![а-яё])/g, '$1 d $2 h'],
  [/(\d+) ч (\d+) мин(?![а-яё])/g, '$1 h $2 min'],
  [/(\d+) мин(?![а-яё])/g, '$1 min'],
  [/(\d+) ч(?![а-яё])/g, '$1 h'],
  [/(\d+) с(?![а-яё])/g, '$1 s'],
  [/(\d+) дн\.?(?![а-яё])/g, '$1 d'],
  [/(\d+) (?:дней|дня)(?![а-яё])/g, '$1 days'],
  // "151 день" по-русски в единственном числе, по-английски нет
  [/(?<!\d)1 день(?![а-яё])/g, '1 day'],
  [/(\d+) день(?![а-яё])/g, '$1 days'],
  [/(\d) ТБ(?![а-яё])/g, '$1 TB'],
  [/(\d) ГБ(?![а-яё])/g, '$1 GB'],
  [/(\d) МБ(?![а-яё])/g, '$1 MB'],
  [/(\d) КБ(?![а-яё])/g, '$1 KB'],
]

const CYR = /[а-яё]/i

export function trBackend(s: string): string {
  if (!CYR.test(s)) return s
  // хвост после "↳" - строка лога приложения, ее не трогаем и по ней не судим
  const cut = s.indexOf('↳')
  const head = cut >= 0 ? s.slice(0, cut) : s
  let out = head
  for (const [re, to] of PHRASES) out = out.replace(re, to)
  for (const [re, to] of PARTS) out = out.replace(re, to)
  if (CYR.test(out)) return s
  return cut >= 0 ? out + s.slice(cut) : out
}

// Поля ответов API, где лежат такие тексты. Ключи и идентификаторы (mute, key, kind) не трогаем:
// по ним панель шлет запросы обратно
const FIELDS = new Set(['text', 'detail', 'what', 'hint', 'note', 'bsrv_rotation', 'reasons', 'why'])

export function trBody<T>(x: T, field = ''): T {
  if (typeof x === 'string') return (FIELDS.has(field) ? trBackend(x) : x) as T
  if (Array.isArray(x)) return x.map((v) => trBody(v, field)) as T
  if (x && typeof x === 'object') {
    const o = x as Record<string, unknown>
    for (const k of Object.keys(o)) o[k] = trBody(o[k], k)
  }
  return x
}
