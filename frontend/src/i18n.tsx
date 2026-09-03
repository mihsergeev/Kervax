import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react'

export type Lang = 'ru' | 'en'

// Ключ = русская строка (дефолт). Значение = английский перевод.
// Отсутствующий ключ → возвращается русский (мягкий фолбэк).
const EN: Record<string, string> = {
  // --- шапка / общее ---
  'Мониторинг инфраструктуры': 'Infrastructure monitoring',
  'Скоро здесь появятся мониторы, дашборды и алерты.':
    'Monitors, dashboards and alerts are coming here soon.',
  'Вы вошли как {user}': 'Signed in as {user}',
  'Тема': 'Theme',
  'Язык': 'Language',
  'Меню': 'Menu',
  'Выйти': 'Log out',
  'загрузка…': 'loading…',
  'Закрыть': 'Close',
  'Отмена': 'Cancel',
  'Готово': 'Done',
  'Ошибка': 'Error',
  'Ошибка HTTP {code}': 'HTTP error {code}',
  '…': '…',

  // --- вход ---
  'Вход в панель': 'Sign in',
  'Войти': 'Log in',
  'Проверка…': 'Checking…',
  'Логин': 'Login',
  'Пароль': 'Password',
  // --- пользователи / роли ---
  'Пользователи': 'Users',
  'Только просмотр': 'View only',
  'Админ': 'Admin',
  'Роль': 'Role',
  'Сбросить пароль': 'Reset password',
  'Новая учётка': 'New account',
  'Пароль (мин. {n})': 'Password (min. {n})',
  'Пароль обновлён': 'Password updated',
  'Пароль минимум {n} символов': 'Password must be at least {n} characters',
  'Нет учёток.': 'No accounts.',
  'Учётка «Только просмотр» видит все данные, но ничего не может менять.':
    'A "View only" account can see everything but change nothing.',
  'Удалить учётку «{name}»?': 'Delete account "{name}"?',
  'Новый пароль для «{name}» (мин. {n} симв.):':
    'New password for "{name}" (min. {n} chars):',
  'Не удалось войти': 'Sign in failed',
  'Неверный логин или пароль': 'Wrong login or password',
  'Код из приложения (2FA)': 'Code from the app (2FA)',
  'Неверный код 2FA': 'Invalid 2FA code',

  // --- смена пароля ---
  'Сменить пароль': 'Change password',
  'Смена пароля завершит все другие активные сессии.':
    'Changing the password ends all other active sessions.',
  'Текущий пароль': 'Current password',
  'Новый пароль (мин. {n} символов)': 'New password (min. {n} characters)',
  'Повторите новый пароль': 'Repeat new password',
  'Пароль изменён. Все прежние сессии завершены.':
    'Password changed. All previous sessions have been ended.',
  'Пароль должен быть не короче {n} символов':
    'Password must be at least {n} characters',
  'Пароли не совпадают': 'Passwords do not match',

  // --- 2FA ---
  'Двухфакторная аутентификация': 'Two-factor authentication',
  '2FA включена. Вход требует код из приложения.':
    '2FA is on. Login requires a code from the app.',
  'Чтобы отключить, введите текущий код из приложения-аутентификатора.':
    'To turn it off, enter the current code from your authenticator app.',
  'Отключить 2FA': 'Disable 2FA',
  'Отсканируйте QR в приложении-аутентификаторе (Google Authenticator, Aegis, 1Password) или введите ключ вручную, затем подтвердите кодом.':
    'Scan the QR in an authenticator app (Google Authenticator, Aegis, 1Password) or enter the key manually, then confirm with a code.',
  'генерация QR…': 'generating QR…',
  'Ключ:': 'Key:',
  'Включить': 'Enable',
  'Выключить': 'Disable',
  'Настроить ({n})': 'Configure ({n})',
  'Применить к выбранным ({n})': 'Apply to selected ({n})',
  'Отметьте поля, которые применить к {n} выбранным мониторам. Остальные настройки не тронутся.':
    'Check the fields to apply to the {n} selected monitors. Other settings stay untouched.',
  'Проверять все IP-адреса домена': 'Check all of the domain’s IP addresses',
  'Следить за SSL-сертификатом': 'Watch the SSL certificate',
  'Следить за сроком домена': 'Watch the domain expiry',
  'Добавьте второй фактор к входу в панель — одноразовый код из приложения-аутентификатора (TOTP).':
    'Add a second factor to panel login — a one-time code from an authenticator app (TOTP).',
  'Включить 2FA': 'Enable 2FA',
  'Неверный код': 'Invalid code',

  // --- мониторы ---
  'Всего': 'Total',
  'Работает': 'Up',
  'Деградация': 'Degraded',
  'Недоступно': 'Down',
  'Выключены': 'Disabled',
  'Мониторы': 'Monitors',
  '+ Добавить монитор': '+ Add monitor',
  'Пока нет мониторов. Нажмите «Добавить монитор».':
    'No monitors yet. Click “Add monitor”.',
  'Нет мониторов с этим статусом.': 'No monitors with this status.',
  'Удалить монитор «{name}»?': 'Delete monitor “{name}”?',
  '{n} дн.': '{n} d',
  'мс': 'ms',
  'Проверить сейчас': 'Check now',
  'Изменить': 'Edit',
  'Удалить': 'Delete',
  'выкл': 'off',
  'чистка': 'rotation',
  'снято {n}': '{n} removed',
  'старейший': 'oldest',
  'политика допускает снапшоты возрастом до {n} дн.': 'policy allows snapshots up to {n} days old',
  'политика хранения не задана — судить не о чем': 'no retention policy — nothing to judge by',
  'последний прогон завершился ошибкой': 'the last run finished with an error',
  'Название': 'Name',
  'Тип': 'Type',
  'HTTP(S) — сайт/эндпоинт': 'HTTP(S) — site/endpoint',
  'TCP-порт': 'TCP port',
  'TLS-сертификат': 'TLS certificate',
  'URL': 'URL',
  'Хост (домен/IP)': 'Host (domain/IP)',
  'Порт': 'Port',
  'Интервал, сек': 'Interval, s',
  'Порог «медленно», мс': 'Slow threshold, ms',
  'Повторы при сбое': 'Retries on failure',
  'Считать «упавшим» и слать алерт, только если провалятся все попытки подряд.':
    'Mark down and alert only if all consecutive attempts fail.',
  'Алерт после N сбоев подряд': 'Alert after N failures in a row',
  'Сколько «плохих» проверок подряд до отправки алерта (гасит редкие флапы).':
    'How many bad checks in a row before alerting (damps rare flaps).',
  'Алерт «медленно» после N подряд': 'Slow alert after N in a row',
  'Отдельный, обычно больший порог для деградации (медленных ответов) — она шумнее падения.':
    'A separate, usually higher threshold for degraded (slow) responses — noisier than an outage.',

  // --- хранение данных (retention) ---
  'Хранение данных': 'Data retention',
  'Данные старше указанного срока удаляются автоматически. Дольше хранить — больше места в БД.':
    'Data older than the given period is deleted automatically. Keeping longer uses more DB space.',
  'Метрики серверов': 'Server metrics',
  'CPU / память / сеть / диск с агентов.': 'CPU / memory / network / disk from agents.',
  'История проверок сайтов': 'Site check history',
  'Время ответа и статусы мониторов, инциденты.':
    'Monitor response times and statuses, incidents.',
  'Хранить, дней': 'Keep, days',
  'полгода': 'half-year',
  'год': 'year',
  '2 года': '2 years',

  // --- локации (прокси) ---
  'Локации': 'Locations',
  'Проверять из локаций (прокси)': 'Check from locations (proxies)',
  'Прокси, через которые панель проверяет сайты (HTTP/HTTPS/SOCKS5) — чтобы видеть доступность из разных сетей/регионов.':
    'Proxies the panel checks sites through (HTTP/HTTPS/SOCKS5) — to see availability from different networks/regions.',
  'Адрес прокси': 'Proxy URL',
  'напр. Германия': 'e.g. Germany',
  'Локаций пока нет.': 'No locations yet.',
  'Удалить локацию «{name}»?': 'Delete location “{name}”?',
  'Включена': 'Enabled',
  'Ещё нет данных из локаций — проверка идёт в фоне.':
    'No location data yet — checks run in the background.',
  'проверено: {t}': 'checked: {t}',
  'socks5://… (пусто = напрямую)': 'socks5://… (empty = direct)',
  'напрямую, без прокси': 'direct, no proxy',
  'напрямую': 'direct',
  'Время ответа · {loc}': 'Response time · {loc}',
  'Из каких локаций проверять:': 'Which locations to check from:',
  'Локаций пока нет — добавьте в ⚙ → Локации.':
    'No locations yet — add them in ⚙ → Locations.',

  // --- пороги напоминаний / массовое применение ---
  'напоминать за, дн.': 'remind, days before',
  'Применить ко всем': 'Apply to all',
  'Применить ко всем мониторам': 'Apply to all monitors',
  'Отметьте поля, которые применить ко всем мониторам разом. Остальные настройки не тронутся.':
    'Tick the fields to apply to every monitor at once. Other settings stay untouched.',
  'Применить': 'Apply',
  'Применено к {n} мониторам.': 'Applied to {n} monitors.',
  'SSL: напоминать за, дн.': 'SSL: remind, days before',
  'Домен: напоминать за, дн.': 'Domain: remind, days before',

  // --- группировка ---
  'Группа': 'Group',
  'напр. Организация / Прод / VPN': 'e.g. Organization / Prod / VPN',
  'По группе': 'By group',
  'По типу': 'By type',
  'Без групп': 'No grouping',
  'Без группы': 'Ungrouped',
  '{n} шт.': '{n}',

  // --- массовое добавление ---
  'Массово': 'Bulk add',
  'Массово добавить сайты': 'Bulk-add sites',
  'По одному на строку. Можно «Имя | адрес» — иначе имя возьмётся из адреса. Протокол можно не писать: проверим по https, а сайт только на http отметим как проблему (для http-сайта впишите http:// явно).':
    'One per line. Optional "Name | address" — otherwise the name comes from the address. No need to type the protocol: we check https, and an http-only site is flagged as a problem (for an http site type http:// explicitly).',
  'Список': 'List',
  'Добавить {n}': 'Add {n}',
  'Предупреждать за N дней': 'Warn N days before',
  'Ожидаемый статус': 'Expected status',
  'Слово в ответе (должно быть)': 'Keyword (must be present)',
  'Слово-стоп (не должно быть)': 'Stop-word (must be absent)',
  'Включён': 'Enabled',
  'Сохранить': 'Save',
  'Добавить': 'Add',
  'Считать оффлайн после, сек': 'Mark offline after, sec',
  'Алертить, только если держится дольше, мин': 'Alert only if sustained longer than, min',
  'Гасит кратковременные спайки CPU/RAM/температуры/conntrack. 0 = слать сразу.':
    'Suppresses brief CPU/RAM/temp/conntrack spikes. 0 = alert immediately.',
  'внеш': 'ext',
  'лок': 'loc',
  'внешний (публичный) IP': 'external (public) IP',
  'локальный IP (внутренняя сеть)': 'local IP (internal network)',

  // --- разделы ---
  'Сайты': 'Sites',
  'Серверы': 'Servers',
  'Перетащите, чтобы изменить порядок': 'Drag to reorder',
  'Перетащите в другую группу': 'Drag to another group',
  '{n} пот.': '{n} thr',
  '+{v} общей': '+{v} shared',
  'непрер. сон (I/O)': 'uninterruptible sleep (I/O)',
  'зомби': 'zombie',
  'остановлен': 'stopped',
  'под трассировкой': 'traced',
  'мёртв': 'dead',
  'Удалить выбранные серверы ({n} шт.) вместе с историей метрик?':
    'Delete the selected servers ({n}) along with their metric history?',
  'Перетащите группу': 'Drag the group',
  'Мониторов в группе': 'Monitors in group',
  'Докер': 'Docker',
  'Кубер': 'Kuber',
  'Мониторинг Docker': 'Docker monitoring',
  'Мониторинг Kubernetes': 'Kubernetes monitoring',
  'Контейнеры, образы, health-статусы и рестарты — появятся здесь. Раздел в разработке.':
    'Containers, images, health statuses and restarts — coming here. Section under development.',
  'Ноды, поды, деплойменты и события кластера — появятся здесь. Раздел в разработке.':
    'Nodes, pods, deployments and cluster events — coming here. Section under development.',
  // --- серверы (агент) ---
  'Мониторинг серверов (агент по HTTPS, без входящих)':
    'Server monitoring (agent over HTTPS, no inbound)',
  'Онлайн': 'Online',
  'Оффлайн': 'Offline',
  'Проблемы': 'Issues',
  'По умолчанию алерт выглядит так: «адрес 🔴 — имя-ссылкой на монитор — текст ошибки». Свой текст в поле выше отключает адрес и ссылку.':
    'By default an alert looks like: "address 🔴 — name-as-link-to-monitor — error text". A custom text above turns off the address and the link.',
  'Аутентификация': 'Authentication',
  'Нет': 'None',
  'HTTP-заголовки (JSON, опционально)': 'HTTP headers (JSON, optional)',
  'Игнорировать ошибки TLS/SSL (самоподписанный / истёкший / чужой хост)':
    'Ignore TLS/SSL errors (self-signed / expired / wrong host)',
  '⚠ Невалидный JSON — заголовки не применятся.': '⚠ Invalid JSON — headers won’t be applied.',
  'Доп. заголовки к запросу (напр. токен), если сайт без них отдаёт 401.':
    'Extra request headers (e.g. a token) for sites that return 401 without them.',
  'Проверять все IP-адреса домена (ловит мёртвый бэкенд за балансировщиком)':
    'Check all of the domain’s IP addresses (catches a dead backend behind a load balancer)',
  'Проверить': 'Test',
  'Проверяю…': 'Testing…',
  'Проверьте прокси — сохранить можно только доступный.':
    'Test the proxy — only a reachable one can be saved.',
  'Бэкапы': 'Backups',
  'Состояние резервных копий, расписание и восстановление — появятся здесь. Раздел в разработке.':
    'Backup status, schedule and restore — coming here. Section under development.',
  'Сбросить': 'Clear',
  'Заглушить алерты:': 'Snooze alerts:',
  'Заглушить:': 'Snooze:',
  'Копировать': 'Copy',
  'Скопировано': 'Copied',
  'OTA обновляет только бинарь, не юнит — поэтому вручную. На новых установках уже включено.':
    'OTA updates only the binary, not the unit — hence manual. Already included on fresh installs.',
  'Весь сервер': 'Whole server',
  '1 час': '1 h',
  '1 день': '1 d',
  '1 неделя': '1 w',
  'Снять': 'Unsnooze',
  'алерты заглушены до {t}': 'alerts snoozed until {t}',
  '✓ Все агенты на подписанной версии {v}': '✓ All agents on signed version {v}',
  '{r} из {n} запущено': '{r} of {n} running',
  'Скрыть остановленные ({n})': 'Hide stopped ({n})',
  'Показать остановленные ({n})': 'Show stopped ({n})',
  'Фильтр: имя, образ…': 'Filter: name, image…',
  'Фильтр: имя, namespace…': 'Filter: name, namespace…',
  'сорт: статус': 'sort: status',
  'сорт: имя': 'sort: name',
  'сорт: рестарты': 'sort: restarts',
  'сорт: namespace': 'sort: namespace',
  'сорт: проблемные': 'sort: problems',
  'сорт: размер': 'sort: size',
  'сорт: старые': 'sort: oldest',
  'сорт: снапшоты': 'sort: snapshots',
  '{h} хостов · {c} контейнеров запущено': '{h} hosts · {c} containers running',
  'Docker не найден ни на одном сервере. Агент определяет его сам; если docker есть, но раздел пуст — обновите агент.':
    'Docker not found on any server. The agent detects it automatically; if docker is present but this is empty, update the agent.',
  'Контейнеров нет': 'No containers',
  'нет доступа': 'no access',
  '{r}/{n}': '{r}/{n}',
  'Доступно обновление агента {v} — отстаёт нод: {n}. Открыть «Серверы» →':
    'Agent update {v} available — {n} node(s) behind. Open “Servers” →',
  'Требует внимания': 'Needs attention',
  'Требует действий': 'Action needed',
  '…и ещё {n}': '…and {n} more',
  '{name}: бэкап-сервер — включите статистику (backupserver-setup)':
    '{name}: backup server — enable stats (backupserver-setup)',
  '{name}: перезапусков — {n}': '{name}: restarts — {n}',
  'Все хосты ок': 'All hosts OK',
  'Все кластеры ок': 'All clusters OK',
  'Все свежие': 'All fresh',
  'Хостов': 'Hosts',
  'Контейнеров': 'Containers',
  'Кластеров': 'Clusters',
  'Подов': 'Pods',
  'Клиентов': 'Clients',
  'Серверов': 'Servers',
  'Доступно обновление агента {v}: отстаёт нод — {n}': 'Agent update {v} available: {n} node(s) behind',
  '{what}: нод без настройки — {n}': '{what}: {n} node(s) not configured',
  '{name}: Docker без доступа — включите read-only proxy': '{name}: Docker without access — enable the read-only proxy',
  '{name}: контейнеров упало — {n}': '{name}: containers down — {n}',
  '{name}: Kubernetes без доступа — запустите kube-setup': '{name}: Kubernetes without access — run kube-setup',
  '{name}: нод NotReady — {n}': '{name}: nodes NotReady — {n}',
  '{name}: проблемных подов — {n}': '{name}: problem pods — {n}',
  '{name}: бэкап завершился с ошибкой': '{name}: backup failed',
  '{name}: бэкап не свежий': '{name}: backup is stale',
  '{name}: rest-server остановлен': '{name}: rest-server is stopped',
  '{name}: репозиториев с проблемой — {n}': '{name}: repositories with problems — {n}',
  // --- Бэкап-серверы ---
  'Бэкап-серверы': 'Backup servers',
  'бэкап-сервер': 'backup server',
  'Сервер бэкапов (rest-server)': 'Backup server (rest-server)',
  'Клиенты (что бэкапится)': 'Clients (what is backed up)',
  'Без бэкапа': 'No backup',
  'Бэкап не требуется': 'Backup not required',
  'Настроить бэкап': 'Set up backup',
  'Бэкап-сервер': 'Backup server',
  'Бэкап (клиент)': 'Backup (client)',
  'Сделать бэкап-сервером': 'Make it a backup server',
  'Завершённые ({n})': 'Finished ({n})',
  '(по systemd)': '(from systemd)',
  '🧹 как удалить': '🧹 how to delete',
  '🔓 как снять лок': '🔓 how to unlock',
  'Сначала убедитесь, что по этому репозиторию НЕ идёт бэкап или prune прямо сейчас — снимать живой лок нельзя. Обычно лок остаётся после аварийно прерванной операции.':
    'First make sure no backup or prune is running against this repository right now — never remove a live lock. A lock usually lingers after an operation was aborted.',
  'Выполнять на бэкап-сервере от root. Пароль читается из prune-env внутри команды и в списке процессов не виден.':
    'Run on the backup server as root. The password is read from prune-env inside the command and never appears in the process list.',
  'Покрытие': 'Coverage',
  'На серверах': 'On servers',
  'По бэкапам': 'Backups',
  'выключить': 'turn off',
  'Выключить дампы {eng}? Локальные файлы дампов будут удалены; история останется в restic.':
    'Turn off {eng} dumps? Local dump files will be deleted; history stays in restic.',
  'дамп включён — снимается перед каждым бэкапом в /backup/{eng}, хранится {k} последних':
    'dump enabled — taken before every backup into /backup/{eng}, {k} latest kept',
  'файлов: {n}': 'files: {n}',
  'ещё не снимался': 'not taken yet',
  '{n} мин назад': '{n} min ago',
  '{n} ч назад': '{n} h ago',
  '{n} д назад': '{n} d ago',
  'Что на ноде': 'What is on the node',
  'Бэкап на этой ноде помечен как не требующийся, но данные на ней есть:':
    'Backup is marked as not required for this node, but it does hold data:',
  'Файловый бэкап на ноде не настроен. Что на ней обнаружено:':
    'No file-level backup is configured on this node. What was detected:',
  'Сервисы': 'Services',
  'обнаружено типов: {n}': 'kinds detected: {n}',
  '{s} нод · {k} типов сервисов': '{s} nodes · {k} service kinds',
  'сервисов: {n}': 'services: {n}',
  'серв.': 'svc',
  'сайтов: {n}': 'sites: {n}',
  'показать все': 'show all',
  'свернуть': 'collapse',
  'фильтр доменов…': 'filter domains…',
  'ничего не найдено': 'nothing found',
  'очереди': 'queues',
  'есть переполненные (≥{n})': 'some overfilled (≥{n})',
  'Метрики без секретов: очереди RabbitMQ — через prometheus-плагин, статус дампов — из аудита покрытия. Пароли/exec не используются.':
    'Secret-free metrics: RabbitMQ queues via the prometheus plugin, dump status from the coverage audit. No passwords/exec used.',
  'Сервисы не обнаружены. Панель находит их по контейнерам, подам и процессам — обновите агенты.':
    'No services detected. The panel finds them via containers, pods and processes — update the agents.',
  'очередей: {n}': 'queues: {n}',
  'сообщений: {m}': 'messages: {m}',
  'очередей: {n} · с сообщениями: {m}': 'queues: {n} · with messages: {m}',
  'Фильтр: имя очереди…': 'Filter: queue name…',
  'в очереди, ждут доставки': 'queued, awaiting delivery',
  'взяты консьюмером, ещё не подтверждены': 'taken by a consumer, not yet acked',
  'Показаны {n} самых глубоких из {m} — остальные пустые.':
    'Showing the {n} deepest of {m} — the rest are empty.',
  'дампы делаются вне панели — не напоминать': 'dumps are handled outside the panel — stop reminding',
  'Отметка «разобрался сам»: убирает пункт с главной. На ноду не влияет — список выше остаётся, панель ничего не настраивает и не выключает.':
    'An "I handled it" mark: removes the item from the home page. It does not touch the node — the list above stays and the panel configures nothing.',
  'включить дампы': 'enable dumps',
  'показать манифест': 'show manifest',
  'Панель кластер не трогает и прав exec не просит — примените этот CronJob сами. Дамп ляжет в /backup на ноде, откуда его заберёт restic:':
    'The panel never touches the cluster and asks for no exec rights — apply this CronJob yourself. The dump lands in /backup on the node, where restic picks it up:',
  '{name}: {db} — нужен отдельный дамп': '{name}: {db} — needs its own dump',
  'Не попадает в бэкап ({n}):': 'Not included in the backup ({n}):',
  'Дампы БД': 'DB dumps',
  'В репозитории': 'In repository',
  'время': 'time',
  'с': 's',
  'мин': 'min',
  'сдвиг': 'offset',
  'синхронизировано': 'synchronized',
  'не синхронизировано': 'not synchronized',
  'Часы разошлись с панелью на {n} — синхронизируйте время:':
    'Clock differs from the panel by {n} — synchronize the time:',
  'Время не синхронизировано — включите NTP:': 'Time is not synchronized — enable NTP:',
  'Синхронизировать время': 'Sync time',
  'синхронизирую…': 'syncing…',
  'Синхронизировать время сейчас? Часы шагнут к точному времени; при большом расхождении это резкий скачок (для БД/приложений заметно).':
    'Sync time now? The clock will step to the correct time; with a large drift this is a sudden jump (noticeable for DBs/apps).',
  'или вручную': 'or manually',
  'команды для ручной синхронизации': 'manual sync commands',
  'Кнопка «в один клик» появится после установки timesync-хелпера (ansible kervax_helpers.yml).':
    'The one-click button appears once the timesync helper is installed (ansible kervax_helpers.yml).',
  'Демон синхронизации не найден — команда выше включит systemd-timesyncd.':
    'No time-sync daemon found — the command above enables systemd-timesyncd.',
  'Если исходящий закрыт и до NTP не достучаться (UDP 123): открой порт (напр. sudo ufw allow out 123/udp) — либо разово выставь часы по времени панели (она достижима по HTTPS):':
    'If outbound is blocked and NTP is unreachable (UDP 123): open the port (e.g. sudo ufw allow out 123/udp) — or set the clock once from the panel time (it is reachable over HTTPS):',
  'Восстановимость под вопросом ({n}):': 'Restorability in question ({n}):',
  '🛢 базы, которые бэкаплю сам: {n} — показать': '🛢 databases I back up myself: {n} — show',
  'свернуть базы (бэкаплю сам)': 'collapse (I back these up myself)',
  'Сверяется автоматически: точки монтирования, bind-mount ы контейнеров, живые СУБД и тома kubernetes.':
    'Checked automatically: mount points, container bind mounts and live databases. Kubernetes volumes are not checked yet.',
  'Репозиторий пустой и не инициализирован — удалять нечего, кроме мусора. Панель бэкапы не удаляет (это защита от взлома), выполните от root на бэкап-сервере:':
    'The repository is empty and never initialized — nothing to lose but junk. The panel never deletes backups (that is the anti-compromise guarantee); run as root on the backup server:',
  'Последняя строка — чтобы панель сразу забыла репу (иначе до следующей минуты). Если репу заводил Ansible, у неё может быть ещё строка в root-crontab — проверьте `sudo crontab -l`.':
    'The last line makes the panel drop the repo immediately (otherwise it lingers until the next minute). If the repo was created by Ansible, it may also have a line in root crontab — check `sudo crontab -l`.',
  'Нет config, но снапшоты есть ({n} шт., {sz}) — данные могут быть восстановимы. Вслепую не удаляйте, разберитесь на сервере.':
    'No config, but snapshots exist ({n}, {sz}) — data may be recoverable. Do not delete blindly; investigate on the server.',
  'метрики restic нет (старый runner) — время и длительность взяты из systemd':
    'no restic metric (legacy runner) — time and duration taken from systemd',
  'скрыть': 'hide',
  'показать': 'show',
  'Порт rest-server': 'rest-server port',
  '▶ Развернуть': '▶ Deploy',
  'бэкап-сервер развёрнут': 'backup server deployed',
  'rest-server (docker, restic, htpasswd)': 'rest-server (docker, restic, htpasswd)',
  'Сначала поставьте на ноду helper бэкап-сервера (root). Он же ставит docker, restic и htpasswd, если их нет:':
    'First install the backup-server helper on the node (root). It also installs docker, restic and htpasswd if missing:',
  'После установки вернитесь сюда — появится кнопка развёртывания.':
    'Once installed, come back here — the deploy button will appear.',
  'Панель поставит docker/restic/htpasswd из штатных реп дистрибутива и поднимет rest-server в режиме append-only + private-repos. Репозитории клиентов создаются потом, отдельно. До ~2 мин.':
    'The panel will install docker/restic/htpasswd from the distro repos and start rest-server in append-only + private-repos mode. Client repositories are created later, separately. Up to ~2 min.',
  'репозиторий уже был — подключились к нему, история сохранена':
    'repository already existed — reconnected to it, history preserved',
  '{name}: helper «{helper}» устарел ({a} → {b}) — переустановите':
    '{name}: helper "{helper}" is outdated ({a} → {b}) — reinstall it',
  '{name}: helper «{helper}» не установлен ({b}) — поставьте':
    '{name}: helper "{helper}" is not installed ({b}) — install it',
  'Требуется ручное действие на сервере. Выполните от root:':
    'Manual action required on the server. Run as root:',
  'Агенту не хватает прав из systemd-юнита:':
    'The agent lacks permissions from its systemd unit:',
  'Setup-скрипты на ноде устарели:': 'Setup scripts on the node are outdated:',
  '{label} (helper «{helper}»): {a} → {b}':
    '{label} (helper "{helper}"): {a} → {b}',
  'Переустановка идемпотентна — обновит helper, ничего не сломает.':
    'Reinstall is idempotent — it updates the helper without breaking anything.',
  'Куда бэкапится': 'Backs up to',
  'куда бэкапится': 'backs up to',
  'адрес ноды': 'node address',
  'helper на ноде устарел — переустановите, чтобы подхватить новые фичи/фиксы:':
    'the helper on the node is outdated — reinstall it to pick up new features/fixes:',
  'Данные для восстановления': 'Restore credentials',
  'Это ключ дешифровки бэкапа. Сохраните в vault и никому не передавайте.':
    'This is the backup decryption key. Store it in a vault and never share it.',
  'Пароль репозитория': 'Repository password',
  'Восстановление проще всего на самом бэкап-сервере (локально):':
    'Restore is easiest on the backup server itself (locally):',
  'Клиент недоступен — пароль взят с бэкап-сервера':
    'Client unreachable — password taken from the backup server',
  'Время (ежедневно)': 'Time (daily)',
  'Сколько копий хранить': 'How many copies to keep',
  'последних': 'last',
  'дневных': 'daily',
  'недельных': 'weekly',
  'месячных': 'monthly',
  'HTTPS/TLS (self-signed на порту 64101)': 'HTTPS/TLS (self-signed on port 64101)',
  'включить HTTPS': 'enable HTTPS',
  'HTTPS включён — бейдж обновится через минуту': 'HTTPS enabled — badge updates within a minute',
  'клиенты ходят по HTTPS (self-signed TLS-фронт на порту {p})': 'clients use HTTPS (self-signed TLS front on port {p})',
  'трафик по HTTP без TLS. Данные шифрует restic на клиенте, но логин/пароль и метаданные идут открыто. Можно включить HTTPS (self-signed) кнопкой ниже.': 'plain HTTP, no TLS. restic encrypts data client-side, but the login/password and metadata go in the clear. You can enable HTTPS (self-signed) with the button below.',
  'Поднять self-signed HTTPS-фронт на :64101? HTTP :64100 продолжит работать — старые клиенты не сломаются. Новых/мигрируемых клиентов подключайте с TLS (галка при энролле).': 'Bring up a self-signed HTTPS front on :64101? HTTP :64100 keeps working — existing clients won’t break. Connect new/migrated clients with TLS (checkbox at enroll).',
  '▶ Настроить и запустить': '▶ Set up and run',
  'Панель создаст репозиторий на бэкап-сервере (htpasswd/init/prune) и настроит restic-бэкап на клиенте. Данные шифруются на клиенте. До ~2 мин.':
    'The panel will create a repository on the backup server (htpasswd/init/prune) and set up a restic backup on the client. Data is encrypted on the client. Takes up to ~2 min.',
  'выберите бэкап-сервер': 'select a backup server',
  'На этой панели нет бэкап-серверов. Чтобы настраивать бэкапы отсюда, добавьте под мониторинг ноду с rest-server — она появится как бэкап-сервер, и её можно будет выбрать целью.':
    'This panel has no backup servers. To configure backups from here, add a node running rest-server to monitoring — it will appear as a backup server and can be selected as the target.',
  'для режима «только» укажите пути': 'for "include" mode, specify paths',
  'TLS-фронт на бэкап-сервере': 'TLS front on backup server',
  'сертификат': 'certificate',
  'репозиторий на бэкап-сервере': 'repository on backup server',
  'бэкап на клиенте': 'backup on client',
  'первый бэкап': 'first backup',
  'выполняется…': 'running…',
  'бэкап настроен': 'backup configured',
  'не требуется': 'not required',
  'бэкапить не требуется': 'backup not required',
  '{name}: бэкап не настроен': '{name}: no backup configured',
  'таймер выкл': 'timer off',
  'ежедневно в': 'daily at',
  'последний бэкап': 'last backup',
  'длительность': 'duration',
  'systemd-таймер бэкапа': 'systemd backup timer',
  'отработал': 'finished',
  'Job — упал разово': 'Job — one-off failure',
  'Удалить под (очистить)': 'Delete pod (clean up)',
  'Завершённые Job/CronJob-поды — историческая запись, а не живой воркоад. Их не нужно чинить; удалите под, чтобы убрать из списка.':
    'Finished Job/CronJob pods are a historical record, not a live workload. Nothing to fix; delete the pod to remove it from the list.',
  'Удалить завершённый под «{n}» (очистить историю)?': 'Delete finished pod "{n}" (clean up history)?',
  'Под Job/CronJob, уже отработал ({phase}). Это историческая запись, а не живой воркоад — чинить нечего; удалите, чтобы убрать из списка.':
    'Job/CronJob pod, already finished ({phase}). This is a historical record, not a live workload — nothing to fix; delete to remove from the list.',
  'запущен': 'running',
  'невалиден': 'invalid',
  'залочен': 'locked',
  'дамп включён — снимается перед каждым бэкапом в {dir}, хранится {k} последних':
    'dump on — taken before every backup into {dir}, keeps {k} latest',
  'PostgreSQL: каждая база — отдельный файл + globals (роли/права).':
    'PostgreSQL: each database is a separate file + globals (roles/grants).',
  'не запускать при <{p}% свободного': 'skip if <{p}% free',
  'без защиты от переполнения': 'no overflow guard',
  'последний прогон пропущен: на разделе было свободно {p}% (< порога) — расчистите место':
    'last run skipped: only {p}% free on the partition (below threshold) — free up space',
  'настроить': 'configure',
  'Настройки дампа': 'Dump settings',
  'обновить до {v}': 'update to {v}',
  'обновлено до {v} — версия в шапке обновится через минуту': 'updated to {v} — the version above refreshes within a minute',
  'Обновить restic до {v}? Скачает с github, сверит sha256, заменит бинарь. Конфиг бэкапа не трогается.':
    'Update restic to {v}? Downloads from github, verifies sha256, replaces the binary. The backup config is not touched.',
  'Обновить образ rest-server до {v}? Перекачает образ и перезапустит контейнер (~30с недоступности). Репозитории лежат вне образа — не пострадают.':
    'Update the rest-server image to {v}? Pulls the image and restarts the container (~30s of downtime). Repositories live outside the image and are not affected.',
  'доступно обновление до {v}': 'update to {v} available',

  'Предупреждения: {n}': 'Warnings: {n}',
  'Должен закончиться до, ч': 'Must finish by, h',
  'Бэкап в любое время — не уведомлять о выходе за окно (дневные сервисы)':
    'Backup at any time — don’t notify about missing the window (daytime services)',
  'Окно бэкапа сохранено': 'Backup window saved',
  'Не уложился в окно': 'Missed the window',
  'Бэкап вышел за окно ({n})': 'Backup ran past its window ({n})',

  '(дампы + restic)': '(dumps + restic)',
  'Окно бэкапа': 'Backup window',
  'На ноде включён hidepid — нативно установленные СУБД других пользователей могут быть не видны (в контейнерах и kubernetes — видны). Проверьте такие базы вручную.':
    'hidepid is on for this node — natively installed databases of other users may be invisible (those in containers and kubernetes are still seen). Check such databases by hand.',
  'Все базы этой ноды бэкаплю сам — не напоминать': 'I back up all databases on this node myself — don’t remind me',
  'Скопом гасит напоминание про ВСЕ найденные базы этой ноды (список выше). Панель видит только дампы, которые настроила сама или нашла CronJob’ом в кластере. Если базы вы бэкапите своим способом (свой cron, ansible, managed-база у провайдера) — отметьте. Одну базу — точечно кнопкой 🔕 на её карточке.':
    'Silences the reminder for ALL databases found on this node at once (the list above). The panel only sees dumps it set up itself or found as a CronJob in the cluster. If you back the databases up your own way (your cron, ansible, a managed DB) — tick this. For a single database use the 🔕 button on its card.',
  'Только гасит напоминание: на сервере ничего не меняется, автопроверка выше работает. Не ставьте, если дампов на самом деле нет, — иначе спрячете реальную дыру в бэкапе.':
    'It only silences the reminder: nothing changes on the server, the auto-check above keeps working. Don’t tick it if there are no dumps in reality — you’d hide a real gap in the backup.',

  'Применяется — статус обновится в течение минуты.': 'Applying — status updates within a minute.',
  'Поменять время бэкапа →': 'Change backup time →',
  'По расписанию сначала снимается дамп, затем restic бэкапит файлы (включая свежий дамп) — одним запуском, последовательно. Долгий дамп задержит начало restic, но не сорвёт его.':
    'On schedule the dump runs first, then restic backs up files (including the fresh dump) — one run, sequentially. A long dump delays restic’s start but does not break it.',

  'Каталог дампов': 'Dump directory',
  'Абсолютный путь, без «..» и не корень.': 'Absolute path, no “..”, not root.',
  'Не /backup? Панель добавит этот путь в список файлового бэкапа, иначе restic дампы не заберёт.':
    'Not /backup? The panel will add this path to the file backup list, otherwise restic will not pick the dumps up.',
  'Хранить последних': 'Keep latest',
  'Не делать, если свободного меньше, %': 'Skip if free space below, %',
  'Дамп не запустится, если после него на разделе останется меньше {p}%. Вместо этого — алерт.':
    'The dump will not run if it would leave less than {p}% free on the partition. An alert is sent instead.',
  'Защита от переполнения выключена: дамп снимается всегда.':
    'Overflow guard off: the dump always runs.',

  '🧩 обновить сразу на {n} нодах': '🧩 update on {n} nodes at once',
  'Запускать из своего репозитория ansible. Сначала можно вхолостую: добавьте --check --diff. Панель ничего не выполняет — только называет хосты.':
    'Run it from your own ansible repo. Try a dry run first: add --check --diff. The panel executes nothing — it only names the hosts.',

  'постоянно': 'permanently',
  'Файлового бэкапа на ноде нет: дампы лягут локально (ежедневно, свой таймер) — восстановиться с самой ноды можно, копии за её пределами не будет.':
    'No file backup on this node: dumps stay local (daily, own timer) — you can restore from the node itself, but there is no off-node copy.',
  'Управление дампами идёт через helper backup-setup — на ноде его нет:':
    'Dump management goes through the backup-setup helper, which is missing on this node:',
  'подпапку добавляет сам helper': 'the helper adds this subfolder itself',
  'баз: {n}': 'databases: {n}',
  'инстансов: {n}': 'instances: {n}',
  'логинов: {n}': 'logins: {n}',
  'ключей: {n}': 'keys: {n}',
  '{n} ключей': '{n} keys',
  'домены не собраны: на ноде нет хелпера webserver-setup': 'domains not collected: webserver-setup helper missing on the node',
  'Правка': 'Editor',
  'Группы серверов': 'Server groups',
  'Группы сайтов': 'Site groups',
  'все серверы': 'all servers',
  'все сайты': 'all sites',
  'серверы: {g}': 'servers: {g}',
  'сайты: {g}': 'sites: {g}',
  'Свою учётку удалить нельзя': 'You cannot delete your own account',
  'Это последний администратор — панель останется без управления': 'This is the last administrator — the panel would be left unmanaged',
  'Доступ': 'Access',
  'Свернуть': 'Collapse',
  'Сохранить доступ': 'Save access',
  'не отмечено — доступны все': 'nothing checked — all allowed',
  'не отмечено — видны все': 'nothing checked — all visible',
  'Групп пока нет': 'No groups yet',
  'все разделы': 'all sections',
  'все группы': 'all groups',
  'разделов: {n}': 'sections: {n}',
  'групп: {n}': 'groups: {n}',
  'Роль задаёт, что можно менять; разделы — какие вкладки видны; группы — какие серверы и мониторы вообще доступны. Пусто в разделах или группах означает «без ограничений».': 'The role defines what can be changed; sections — which tabs are visible; groups — which servers and monitors are accessible at all. Empty sections or groups mean no restrictions.',
  'Алерт, когда в очереди накопилось': 'Alert when the queue holds',
  'Порог этой очереди: с этого числа сообщений алертим. Пусто или 0 — не алертить.': 'Threshold for this queue: alert from this message count. Empty or 0 — no alerts.',
  'сообщений и больше — сразу во всех очередях ниже. У любой очереди значение можно поправить или стереть.': 'messages or more — applied to every queue below. Any queue can be adjusted or cleared.',
  'алерт при {n}+ сообщениях — нажмите, чтобы выключить для этой очереди': 'alerts at {n}+ messages — click to disable for this queue',
  'алерты по этой очереди выключены — нажмите, чтобы включить': 'alerts for this queue are off — click to enable',
  'контейнер: {c}': 'container: {c}',
  'под kubernetes: {p}': 'kubernetes pod: {p}',
  'процесс на хосте': 'process on the host',
  'базы и размеры не собраны: на ноде нет хелпера dbstat-setup': 'databases and sizes not collected: dbstat-setup helper missing on the node',
  'инвентарь снимается с хостовых и docker-баз — до баз в кластере хелпер не дотягивается': 'inventory covers host and docker databases — the helper cannot reach databases inside the cluster',
  'база не ответила на опрос инвентаря': 'the database did not answer the inventory query',
  'маршруты приходят по xDS — панель их не читает': 'routes come via xDS — the panel does not read them',
  'в кластере нет Ingress/HTTPRoute с доменами': 'no Ingress/HTTPRoute with hostnames in the cluster',
  'домены не заданы — обычно это апстрим за прокси': 'no hostnames configured — usually an upstream behind a proxy',
  // --- сейф доступов к бэкапам ---
  'Доступы к бэкапам': 'Backup credentials',
  'Сейф хранит доступы в зашифрованном виде. Пароль задаёте вы, панель его не знает и не хранит — расшифровка идёт в браузере. Забытый пароль восстановить нельзя: сейф придётся собрать заново с нод.':
    'The vault keeps credentials encrypted. You choose the password; the panel never learns or stores it — decryption happens in your browser. A forgotten password cannot be recovered: the vault would have to be collected from the nodes again.',
  // выгрузка доступов и команды восстановления (текстовый файл на копипаст)
  'Kervax — доступы к бэкапам': 'Kervax — backup credentials',
  'выгружено: {when} UTC · репозиториев: {n}': 'exported: {when} UTC · repositories: {n}',
  'ВНИМАНИЕ: здесь пароли от бэкапов ОТКРЫТЫМ ТЕКСТОМ. Это ключи к данным:':
    'WARNING: backup passwords below are in PLAIN TEXT. They are the keys to your data:',
  'храните файл в менеджере паролей или на шифрованном томе, не оставляйте на диске.':
    'keep this file in a password manager or on an encrypted volume, not lying on disk.',
  'Порядок восстановления: поставьте restic, вставьте блок нужного репозитория,':
    'To restore: install restic, paste the block of the repository you need,',
  'дальше `restic snapshots` покажет копии, `restic mount` откроет их как папку.':
    'then `restic snapshots` lists the copies and `restic mount` opens them as a folder.',
  'нода': 'node',
  'репозиторий': 'repository',
  'пароль': 'password',
  'вложен в команды ниже': 'embedded in the commands below',
  'отсутствует — https без сертификата не подключится':
    'missing — https will not connect without it',
  'не нужен (http)': 'not needed (http)',
  'снято': 'captured',
  'восстановление': 'restore',
  'сертификат бэкап-сервера (самоподписанный) — создаём файл прямо здесь:':
    'backup server certificate (self-signed) — writing the file right here:',
  'сертификат бэкап-сервера получить не удалось — возьмите /app/rest-server-tls/cert.pem':
    'could not fetch the backup server certificate — take /app/rest-server-tls/cert.pem',
  'с бэкап-сервера и добавьте: restic --cacert <файл> …':
    'from the backup server and add: restic --cacert <file> …',
  'смотреть бэкап как файловую систему': 'browse the backup as a filesystem',
  'сертификата под рукой нет? подойдёт любой из вариантов:':
    'no certificate at hand? any of these works:',
  '1) снять его с сервера — он отдаёт сертификат всем, кто подключается:':
    '1) take it from the server — it presents the certificate to everyone who connects:',
  '2) без проверки подлинности сервера (канал шифруется, подмену сервера не поймать):':
    '2) without verifying the server (traffic is still encrypted, a swapped server is not caught):',
  '3) по http, если порт открыт:': '3) over http, if the port is open:',
  '4) прямо на бэкап-сервере, вообще без сети и TLS (от root, тем же паролем):':
    '4) on the backup server itself, with no network and no TLS (as root, same password):',
  'пароль сейфа': 'vault password',
  'ещё раз': 'repeat',
  'создать сейф': 'create vault',
  'открыть': 'unlock',
  'закрыть сейф': 'lock vault',
  'собрать доступы с нод': 'collect from nodes',
  'Поиск: нода, группа, сервис…': 'Search: node, group, service…',
  'По запросу «{q}» ничего нет.': 'Nothing matches “{q}”.',
  'По запросу «{q}» ничего нет — это фильтр, а не отсутствие бэкапов.':
    'Nothing matches “{q}” — that is the filter, not a lack of backups.',
  'забыли пароль?': 'forgot the password?',
  'стереть сейф и задать пароль заново': 'wipe the vault and set a new password',
  'Стереть сейф целиком? Записи будут удалены безвозвратно, доступы придётся собрать с нод заново.':
    'Wipe the whole vault? Entries are deleted for good; credentials will have to be collected from the nodes again.',
  'сейф стёрт, записей удалено: {n}': 'vault wiped, entries removed: {n}',
  'Содержимое сейфа не расшифровать без пароля — ни панели, ни кому-либо ещё: ключ выводился только из него. Бэкапы при этом целы: пароли репозиториев лежат на самих нодах (у клиента и на бэкап-сервере), панель их оттуда и брала. Поэтому выход простой — стереть сейф, задать новый пароль и снова нажать «собрать доступы с нод». Потеряете только записи тех репозиториев, чей клиент И бэкап-сервер уже недоступны.':
    'Vault contents cannot be decrypted without the password — not by the panel, not by anyone: the key came only from it. Backups themselves are fine: repository passwords live on the nodes (on the client and on the backup server), which is where the panel read them. So the way out is simple — wipe the vault, set a new password and press “collect from nodes” again. You only lose entries for repositories whose client AND backup server are both gone.',
  'скачать доступы': 'download credentials',
  'копия сейфа': 'vault backup copy',
  'выгрузка открытым текстом': 'plaintext export',
  '«Скачать доступы» — обычный текстовый файл: адрес, пароль, сертификат и команды по каждому репозиторию, восстанавливаться можно прямо из него. Пароли в нём открытым текстом, так что храните в менеджере паролей. «Копия сейфа» — то же самое, но зашифрованным: читается только этой панелью и только с паролем сейфа.':
    '“Download credentials” gives a plain text file: address, password, certificate and commands for every repository — you can restore straight from it. Passwords are in the clear, so keep it in a password manager. “Vault backup copy” is the same data encrypted: readable only by this panel and only with the vault password.',
  'замок через 15 мин бездействия': 'auto-locks after 15 min idle',
  'показать доступ': 'show credentials',
  'скопировать': 'copy',
  'пароль сейфа — минимум 12 символов': 'vault password must be at least 12 characters',
  'пароли не совпадают': 'passwords do not match',
  'неверный пароль сейфа': 'wrong vault password',
  'не расшифровалось (запись от другого пароля?)': 'could not decrypt (entry from another password?)',
  'Сейф пуст — нажмите «собрать доступы с нод».': 'The vault is empty — press “collect from nodes”.',
  'в сейфе: {n}': 'in vault: {n}',
  'не отдали доступы: {n}': 'no credentials from: {n}',
  'источник: {s}': 'source: {s}',
  // --- сводка заглушённого на странице серверов ---
  'Заглушено ({n})': 'Muted ({n})',
  'бессрочно': 'no expiry',
  'ещё {d}': '{d} left',
  '{n} мин': '{n} min',
  '{n} ч': '{n} h',
  '{n} дн': '{n} d',
  'репо {n}': 'repo {n}',
  'покрытие {n}': 'coverage {n}',
  'репо ×{n}': 'repos ×{n}',
  'покрытие ×{n}': 'coverage ×{n}',
  'Docker: контейнер упал': 'Docker: container down',
  'Docker: перезапуски': 'Docker: restart loop',
  'Бэкап: не настроен': 'Backup: not configured',
  'Бэкап: ошибка': 'Backup: failed',
  'Бэкап: не свежий': 'Backup: stale',
  'Бэкап-сервер: репозитории': 'Backup server: repositories',
  'Бэкап: дамп СУБД': 'Backup: DB dump',
  'Бэкап: место под дампы': 'Backup: dump disk space',
  'Бэкап: дамп-CronJob': 'Backup: dump CronJob',
  'Время сервера': 'Server clock',
  'открыть график': 'open chart',
  'Диск — только предупреждения': 'Disk — warnings only',
  'Диск — предупр. и проблемы': 'Disk — warnings and problems',
  'Насовсем уровень выключается порогом: поставьте 0 в настройках сервера.':
    'To disable a level for good, set its threshold to 0 in the server settings.',

  'Имя репозитория выглядит небезопасно — команда не показана, разберитесь на сервере вручную.':
    'The repository name looks unsafe — no command shown, sort it out on the server by hand.',
  '🧹 массовая зачистка': '🧹 bulk cleanup',
  'не обновлялись дольше': 'not updated for more than',
  'приглушённые вручную': 'muted manually',
  'дней': 'days',
  'Под условие никто не попал.': 'Nothing matches.',
  'Попадает под удаление: {n} шт., {sz}. Восстановить будет нечем.':
    'Matched for deletion: {n} repos, {sz}. There will be nothing to restore from.',
  'Пропущено с подозрительными именами: {n} — удалите вручную, разобравшись.':
    'Skipped due to suspicious names: {n} — delete those by hand after checking.',
  'Скрипт спросит подтверждение числом. Панель ничего не удаляет — это защита на случай её взлома.':
    'The script asks for numeric confirmation. The panel deletes nothing — this protects you if the panel itself is compromised.',

  'Будет стёрто безвозвратно: {n} снапшотов, {sz}, последний {ago}. Восстановить будет нечем — других копий этих данных у панели нет.':
    'Will be erased permanently: {n} snapshots, {sz}, last one {ago}. There will be nothing to restore from — the panel holds no other copy.',
  'Удаляйте, только если сервер выведен из эксплуатации. Если бэкап ещё может понадобиться — приглушите репозиторий (🔕): он перестанет считаться проблемой, а данные останутся.':
    'Delete only if the server is decommissioned. If the backup may still be needed, mute the repository (🔕) instead: it stops counting as an issue and the data stays.',
  'Репозиторий пустой и не инициализирован — удалять нечего, кроме мусора.':
    'The repository is empty and uninitialised — there is nothing to delete but leftovers.',
  'Панель бэкапы не удаляет (это защита от взлома), выполните от root на бэкап-сервере:':
    'The panel never deletes backups (this is protection against compromise) — run as root on the backup server:',

  'Не бэкапить это — убрать из проблем': 'Do not back this up — clear from issues',
  'Вернуть в проблемы': 'Move back to issues',
  '🔕 приглушено вручную: {n}': '🔕 muted manually: {n}',

  'Настраиваю и снимаю пробный дамп — подождите, на большой базе это может занять до минуты…':
    'Setting up and taking a test dump — please wait, on a large database this can take up to a minute…',
  'Снял — проверить': 'Removed it — re-check',
  'Статус обновится и сам, в течение минуты: сервер пересчитывает статистику по расписанию.':
    'The status also clears on its own within a minute: the server recomputes stats on a schedule.',
  'идёт бэкап': 'backup running',
  'битый': 'broken',
  'активность': 'activity',
  'Репозиториев нет': 'No repositories',
  'приглушён': 'muted',
  'устарел': 'stale',
  'обновлён': 'updated',
  'хранит': 'keeps',
  'политика хранения: последних / дневных / недельных / месячных': 'retention: last / daily / weekly / monthly',
  'Снять приглушение': 'Unmute',
  'Приглушить (разовый/неактуальный)': 'Mute (one-off / not needed)',
  'репозиториев: {n} · все ок': 'repositories: {n} · all OK',
  'Фильтр: имя репозитория…': 'Filter: repository name…',
  'Читается на сервере без паролей: config (валидность), снапшоты, размер, свежесть, лок, политика хранения. Устаревшие (давно нет бэкапа) — красным и алертят; приглушите разовые/неактуальные (🔕).':
    'Read on the server without passwords: config (validity), snapshots, size, freshness, lock, retention. Stale (no backup for a long time) — red + alerts; mute one-off / unneeded ones (🔕).',
  'снапшотов: {n}': 'snapshots: {n}',
  'репозиториев: {n}': 'repositories: {n}',
  'проблемных: {b}': 'problem: {b}',
  'репозиториев: {n} · проблемных: {b}': 'repositories: {n} · problem: {b}',
  'репозиториев: {n} · все валидны': 'repositories: {n} · all valid',
  'Статистики репозиториев пока нет. Включите её на бэкап-сервере (read-only helper, root-таймер пишет статистику в файл, без паролей):':
    'No repository stats yet. Enable them on the backup server (read-only helper, a root timer writes stats to a file, no passwords):',
  'Читается на сервере без паролей: наличие config (валидность), число снапшотов, свежесть, лок.':
    'Read on the server without passwords: presence of config (validity), snapshot count, freshness, lock.',
  // --- CronJob-манифест дампа СУБД в кластере (копипаст в kubectl) ---
  'Дамп {engine} ({ns}/{host}) → {dir} на ноде {node}.':
    'Dump of {engine} ({ns}/{host}) → {dir} on node {node}.',
  'Оттуда его заберёт обычный restic-бэкап этой ноды (путь /backup уже в бэкапе).':
    'From there the node\'s regular restic backup picks it up (/backup is already included).',
  'Секрет с доступом к базе подставлен АВТОМАТИЧЕСКИ из спеки пода (те же ссылки, что у самой СУБД — панель значений не читает). Сверьте и примените.':
    'The database secret was filled in AUTOMATICALLY from the pod spec (the same references the database itself uses — the panel never reads the values). Check it and apply.',
  'ВАЖНО: подставьте имя Secret\'а с доступом к базе — панель секреты не читает и не хранит.':
    'IMPORTANT: fill in the name of the Secret holding database credentials — the panel neither reads nor stores secrets.',
  'Это ОДИН из нескольких манифестов: баз этого типа найдено больше одной, у каждой свой CronJob и свой каталог — иначе они затирали бы дампы друг друга.':
    'This is ONE of several manifests: more than one database of this type was found, and each gets its own CronJob and directory — otherwise they would overwrite each other\'s dumps.',
  'Обращаемся к Service «{host}» (имя выведено из имени пода — СВЕРЬТЕ его с kubectl get svc), а не к конкретному поду: у реплик дамп надо снимать один раз. Если у вас primary/replica — укажите здесь Service именно primary.':
    'This targets the Service “{host}” (the name is derived from the pod name — VERIFY it against kubectl get svc), not a specific pod: replicas should be dumped once. With a primary/replica setup, point this at the primary\'s Service.',
  'секрет не требуется (открытый доступ)': 'no secret needed (open access)',
  'secret-с-доступом-к-базе': 'secret-with-database-credentials',
  'ПОДСТАВЬТЕ': 'FILL IN',
  'нода с агентом': 'node running the agent',
  'нода-с-агентом': 'node-running-the-agent',
  'за час до бэкапа ноды': 'an hour before the node backup',
  'дамп должен лечь на ноду, где идёт restic': 'the dump must land on the node running restic',

  // --- Бэкапы: скрипт массовой зачистки репозиториев (копипаст в терминал) ---
  'Kervax: зачистка репозиториев на {server}.': 'Kervax: repository cleanup on {server}.',
  'БУДЕТ УДАЛЕНО БЕЗВОЗВРАТНО: {n} шт., {size}.':
    'WILL BE DELETED PERMANENTLY: {n} repositories, {size}.',
  'Других копий этих данных у панели нет. Сверьте список ВЫШЕ перед запуском.':
    'The panel holds no other copy of this data. Check the list ABOVE before running.',
  'Выполнять от root на бэкап-сервере.': 'Run as root on the backup server.',
  'Подтверждение вводом числа: список длинный и к моменту запуска уже уехал за край экрана.':
    'Confirmation by typing a number: the list is long and has scrolled off the screen by now.',
  'Удалить %s репозиториев БЕЗВОЗВРАТНО? Введите число %s: ':
    'Delete %s repositories PERMANENTLY? Type the number %s: ',
  'htpasswd/prune могут отсутствовать (репу заводили руками) — это не ошибка':
    'htpasswd/prune may be missing (the repo was created by hand) — that is not an error',
  'чтобы панель забыла удалённое сразу, а не через минуту':
    'so the panel forgets what was deleted at once, not a minute later',
  'готово: удалено': 'done, deleted',
  'удалён': 'deleted',
  'отменено': 'cancelled',
  'снап.': 'snap.',
  'лок снят штатно': 'lock released normally',
  'пароль из env не подходит (репо от другого деплоя) — снимаю лок файлом':
    'the password from env does not fit (repo from another deployment) — removing the lock file',
  'лок-файлы удалены': 'lock files removed',

  // --- Бэкапы ---
  'бэкап идёт с {from} и не уложился в окно (до {to}:00)':
    'backup running since {from}, past its window (by {to}:00)',
  'бэкап закончился в {at} — позже окна (до {to}:00)':
    'backup finished at {at} — past its window (by {to}:00)',
  'свежий': 'fresh',
  'не свежий': 'stale',
  'пропущен': 'skipped',
  'нет метрики': 'no metric',
  'не настроен': 'not configured',
  'последний': 'last',
  'Последний бэкап': 'Last backup',
  'Результат': 'Result',
  'успех': 'success',
  'Длительность': 'Duration',
  'Пропущен (лок занят)': 'Skipped (lock busy)',
  'да': 'yes',
  'Таймер': 'Timer',
  'включён': 'enabled',
  'активен': 'active',
  'неактивен': 'inactive',
  'Сервис': 'Service',
  'найден': 'found',
  'не найден': 'not found',
  'Статус читается с ноды без секретов (метрики + systemctl). Управление — в следующей фазе.':
    'Status is read from the node without secrets (metrics + systemctl). Management comes in the next phase.',
  '{n} нод · {p} проблем': '{n} nodes · {p} problems',
  '{n} нод · все свежие': '{n} nodes · all fresh',
  'Поиск: нода, группа…': 'Search: node, group…',
  'Ноды с restic-бэкапом не найдены. Агент определяет бэкап сам (метрики + systemd); если бэкап есть, но раздел пуст — обновите агент.':
    'No nodes with a restic backup found. The agent detects backups itself (metrics + systemd); if a backup exists but the section is empty — update the agent.',
  'Управление': 'Management',
  'Расписание (ежедневно)': 'Schedule (daily)',
  'Что бэкапить': 'What to back up',
  'Всё, кроме (exclude)': 'Everything except (exclude)',
  'Только (include)': 'Only (include)',
  'Сохранить пути': 'Save paths',
  '▶ Запустить бэкап сейчас': '▶ Run backup now',
  'Запустить бэкап сейчас?': 'Run backup now?',
  'запустить сейчас': 'run now',
  'идёт…': 'running…',
  'Обычно не нужно — бэкап идёт ночью по расписанию': 'Usually not needed — the backup runs at night on schedule',
  'Запустить бэкап сейчас? Обычно не нужно — он идёт ночью по расписанию.':
    'Run the backup now? Usually not needed — it runs at night on schedule.',
  'нет изменений': 'no changes',
  'Сохранено': 'Saved',
  'Серым — стандартный мусор, белым — добавленное вами.': 'Gray — standard junk, white — what you added.',
  'Применить новый список путей ({mode}, {n} шт.)?': 'Apply the new path list ({mode}, {n})?',
  'время в формате ЧЧ:ММ': 'time in HH:MM format',
  'список путей пуст': 'path list is empty',
  'готово': 'done',
  'Изменения пишутся прямо в конфиг бэкапа на ноде через узкий helper. Внимание: повторный прогон Ansible может их перезаписать.':
    'Changes are written straight to the backup config on the node via a narrow helper. Note: a re-run of Ansible may overwrite them.',
  'Статус читается без секретов. Управление доступно после установки helper на ноде (кнопка ниже).':
    'Status is read without secrets. Management becomes available after installing the helper on the node (button below).',
  'Чтобы управлять бэкапом из панели, поставьте узкий helper (root + sudoers только на бэкап-операции, не cluster-admin):':
    'To manage the backup from the panel, install the narrow helper (root + sudoers limited to backup operations only):',
  // --- Kubernetes ---
  '{n} кластеров': '{n} clusters',
  'Поиск: хост, группа, нода, под, воркоад…': 'Search: host, group, node, pod, workload…',
  'Kubernetes не найден ни на одном сервере. Агент определяет кластер сам (k0s/k3s/microk8s/kubeadm); если он есть, но раздел пуст — обновите агент.':
    'Kubernetes not found on any server. The agent detects the cluster itself (k0s/k3s/microk8s/kubeadm); if it exists but the section is empty — update the agent.',
  'Кластер найден, но агент не имеет доступа к kube-api. Включите read-only + точечное управление: скрипт создаёт ServiceAccount с узким RBAC (не cluster-admin) и кладёт токен для агента.':
    'Cluster found, but the agent has no kube-api access. Enable read-only + limited management: the script creates a ServiceAccount with narrow RBAC (not cluster-admin) and writes a token for the agent.',
  'Поды ({n})': 'Pods ({n})',
  'Воркоады ({n})': 'Workloads ({n})',
  'Ноды ({n})': 'Nodes ({n})',
  'Подов нет': 'No pods',
  'Воркоадов нет': 'No workloads',
  'ноды {r}/{n}': 'nodes {r}/{n}',
  'ноды {nr}/{nn} · поды {pr}/{pn} · ns {ns}': 'nodes {nr}/{nn} · pods {pr}/{pn} · ns {ns}',
  'Rollout restart «{n}» ({k})?': 'Rollout restart “{n}” ({k})?',
  'Rollout restart': 'Rollout restart',
  'Перезапустить под «{n}» (удалить — контроллер пересоздаст)?':
    'Restart pod “{n}” (delete — the controller will recreate it)?',
  'Перезапустить под': 'Restart pod',
  'перезапусков: {n}': 'restarts: {n}',
  'перезапусков за всё время: {n} (само по себе не проблема; тревога — только CrashLoopBackOff)':
    'restarts over lifetime: {n} (not a problem by itself; only CrashLoopBackOff is)',
  'Логи': 'Logs',
  'healthcheck: unhealthy': 'healthcheck: unhealthy',
  'перезапусков (RestartCount): {n}': 'restarts (RestartCount): {n}',
  'перезапусков за всё время (RestartCount): {n} (само по себе не проблема; тревога — на устойчивый crash-loop)':
    'restarts over lifetime (RestartCount): {n} (not a problem by itself; alerts fire only on sustained crash-loop)',
  'Перезапустить': 'Restart',
  'Остановить': 'Stop',
  'Запустить': 'Start',
  'перезапустить': 'restart',
  'остановить': 'stop',
  'запустить': 'start',
  '{a} контейнер «{c}»?': '{a} container “{c}”?',
  'не удалось': 'failed',
  'ошибка': 'error',
  'не удалось получить логи': 'failed to fetch logs',
  '(пусто)': '(empty)',
  'Пусто — контейнер ничего не писал в лог.': 'Empty — the container has written nothing to the log.',
  'Нет записей за выбранный период. Контейнер давно не писал в лог — попробуйте «400 строк».':
    'No entries for the selected period. The container hasn’t logged in a while — try “400 lines”.',
  'Запрашиваю логи у агента…': 'Requesting logs from the agent…',
  'Последние 400 строк. Логи не хранятся — читаются с ноды по запросу.':
    'Last 400 lines. Logs are not stored — fetched from the node on demand.',
  'Логи не хранятся — читаются с ноды по запросу (кап ~20 МБ).':
    'Logs are not stored — fetched from the node on demand (cap ~20 MB).',
  'Показаны последние 500 КБ из {mb} МБ — скачайте .txt для полного.':
    'Showing the last 500 KB of {mb} MB — download .txt for the full log.',
  '400 строк': '400 lines',
  'хвост': 'tail',
  'за час': 'last hour',
  'за день': 'last day',
  'Скачать .txt': 'Download .txt',
  'Docker установлен (версии видны). Список контейнеров скрыт — включите read-only доступ (docker-socket-proxy: только просмотр + restart, без exec/root). Новым нодам — флаг --docker при установке.':
    'Docker is installed (versions shown). Container list hidden — enable read-only access (docker-socket-proxy: view + restart only, no exec/root). For new nodes use the --docker install flag.',
  'OOM-киллы': 'OOM kills',
  'последние киллы': 'recent kills',
  'процесс неизвестен': 'process unknown',
  'OOM-killer': 'OOM killer',
  'IP-адреса домена': 'Domain IP addresses',
  'Время ответа · IP {ip}': 'Response time · IP {ip}',
  'Показать время ответа этого IP': 'Show this IP’s response time',
  'Ещё нет разбивки по IP — проверка идёт в фоне (или у домена один адрес).':
    'No per-IP breakdown yet — the check is running in the background (or the domain has a single address).',
  'HTTP Basic (логин/пароль)': 'HTTP Basic (login/password)',
  'Показать': 'Show',
  'Скрыть': 'Hide',
  'IP сервера (опционально)': 'Server IP (optional)',
  'На сервере уже стоит агент другой панели?': 'Already running an agent for another panel on this server?',
  'Допишите третий аргумент — любое имя этого инстанса — в самый конец команды, через пробел после токена:':
    'Append a third argument — any name for this instance — at the very end of the command, separated by a space after the token:',
  'Скопировать с « main»': 'Copy with " main"',
  '«main» — произвольное имя, оно лишь отличает панели между собой. Агенты будут работать рядом, каждый со своей панелью.':
    '"main" is an arbitrary name; it just distinguishes the panels. The agents will run side by side, each with its own panel.',
  'Ждём первый отчёт агента — статус обновится здесь сам…':
    'Waiting for the first agent report — the status updates here automatically…',
  'Агент подключился: {host} · v{ver} — метрики уже идут':
    'Agent connected: {host} · v{ver} — metrics are flowing',
  'Если панель закрыта фаерволом: адрес попадёт в data/agent_allow_ips, а хостовый скрипт ops/agent-firewall-sync.sh разрешит его в ufw/firewalld. Для Caddy-вайтлиста ничего не нужно — /api/agent/* уже открыт.':
    'If the panel host is firewalled: the address goes to data/agent_allow_ips and the host-side ops/agent-firewall-sync.sh allows it in ufw/firewalld. Nothing needed for the Caddy allow-list — /api/agent/* is already open.',
  'Адрес, с которого агент ходит в панель — для разрешения в фаерволе хоста панели (ufw/firewalld).':
    'The address the agent connects from — allowed in the panel host firewall (ufw/firewalld).',
  '+ Добавить сервер': '+ Add server',
  'Пока нет серверов. Нажмите «Добавить сервер» и выполните команду установки на ноде.':
    'No servers yet. Click “Add server” and run the install command on the node.',
  'Удалить сервер «{name}»? Агент на ноде продолжит слать — удалите его отдельно.':
    'Delete server “{name}”? The agent on the node keeps sending — remove it separately.',
  'оффлайн': 'offline',
  'Диск': 'Disk',
  'разд.': 'part.',
  'Диски': 'Disks',
  'Добавить сервер': 'Add server',
  'Создать': 'Create',
  'напр. Прод / БД': 'e.g. Prod / DB',
  '⏱ Файрвол панели открывается для IP новой ноды в течение ~2 мин. Если команда упала по таймауту (curl: timed out) — подождите минуту и повторите её же.':
    '⏱ The panel firewall opens for the new node’s IP within ~2 min. If the command timed out (curl: timed out) — wait a minute and re-run the same command.',
  'Агент сам включит всё применимое на ноде — Docker (read-only proxy), Kubernetes (узкий SA), доступ к бэкапам — вручную доделывать ничего не нужно. Отключить авто-настройку: добавить --no-auto.':
    'The agent auto-enables everything applicable on the node — Docker (read-only proxy), Kubernetes (narrow SA), backup access — nothing to do by hand. Disable auto-setup: add --no-auto.',
  'Сервер создан': 'Server created',
  'Выполните команду на сервере (под root). Токен показывается один раз.':
    'Run this command on the server (as root). The token is shown once.',
  'Скопировать команду': 'Copy command',
  'Скопировано ✓': 'Copied ✓',
  'аптайм': 'uptime',
  'агент': 'agent',
  'Доступна версия агента {v}. Отстают: {n}.':
    'Agent version {v} is available. Behind: {n}.',
  'Обновить сначала одну ноду (canary), потом остальные':
    'Update one node first (canary), then the rest',
  'Canary — 1 ноду': 'Canary — 1 node',
  'Обновить агент до {v} на всех отстающих нодах ({n})?':
    'Update agent to {v} on all {n} behind nodes?',
  'Обновить все ({n})': 'Update all ({n})',
  'Обновить агент этой ноды (проверит подпись и хеш)':
    'Update this node’s agent (verifies signature and hash)',
  'Отменить обновление': 'Cancel update',
  'отменить': 'cancel',
  'Пороги алертов, %': 'Alert thresholds, %',
  'Нагрузка, %': 'Load, %',
  'Сеть': 'Network',
  '↓ приём': '↓ in',
  '↑ отдача': '↑ out',
  'состав нагрузки': 'load breakdown',
  'по ядрам': 'per core',
  'Модель': 'Model',
  'частота': 'frequency',
  'температура': 'temperature',
  'троттлинг': 'throttling',
  'Алерт по температуре CPU, °C (0 = выкл)': 'CPU temperature alert, °C (0 = off)',
  'Алерт conntrack, % (0 = выкл)': 'Conntrack alert, % (0 = off)',
  'Алерт по температуре диска, °C (0 = выкл)': 'Disk temperature alert, °C (0 = off)',
  'Память': 'Memory',
  '↓ приём / ↑ отдача': '↓ in / ↑ out',
  'Диск I/O': 'Disk I/O',
  'Диск IOPS': 'Disk IOPS',
  '↓ чтение / ↑ запись': '↓ read / ↑ write',
  '↓ чтение': '↓ read',
  '↑ запись': '↑ write',
  'чтение': 'read',
  'запись': 'write',
  '↓ загрузка / ↑ выгрузка': '↓ in / ↑ out',
  '↓ загрузка': '↓ in',
  '↑ выгрузка': '↑ out',
  'загрузка': 'swap in',
  'выгрузка': 'swap out',
  'буфер записи': 'write buffer',
  'ожидают записи': 'awaiting write',
  'запись на диск': 'writeback',
  'ядро (slab)': 'kernel (slab)',
  'приём по интерфейсам': 'in per interface',
  'отдача по интерфейсам': 'out per interface',
  'ошибки/дропы по интерфейсам': 'errors/drops per interface',
  'ошибки': 'errors',
  'дропы': 'drops',
  'Процессы': 'Processes',
  'Топ по CPU': 'Top by CPU',
  'Топ по памяти': 'Top by memory',
  'загрузка (%util)': 'utilization (%util)',
  'задержка (await)': 'latency (await)',
  'Разделы': 'Sections',
  'Наверх': 'Back to top',
  'Текст сообщения': 'Message text',
  'ко всем': 'all',
  'Не слать алерты этого сервера:': 'Mute these alerts for this server:',
  'Не слать алерты этого монитора:': 'Mute these alerts for this monitor:',
  'Правила действуют на все. Пороги и исключения — в настройках конкретного сервера/монитора.':
    'Rules apply to all. Thresholds and exceptions live in the specific server/monitor settings.',
  'Недоступен / деградация': 'Down / degraded',
  'Частичная доступность (локации)': 'Partial availability (locations)',
  'Недоступен': 'Down',
  'Троттлинг CPU': 'CPU throttling',
  'Температура диска': 'Disk temperature',
  'Температура CPU': 'CPU temperature',
  'Перезагрузка': 'Reboot',
  'Антифлуд': 'Flood control',
  'Группировать при ≥ N алертов за цикл (0 = выкл)': 'Group at ≥ N alerts per cycle (0 = off)',
  'При массовых событиях (упал стек/аплинк) шлём один дайджест вместо потока сообщений.': 'On mass events (a stack/uplink going down) we send one digest instead of a flood of messages.',
  'Соединения': 'Connections',
  'соединения': 'connections',
  'заполнение': 'fill',
  'всего': 'total',
  'Сокеты': 'Sockets',
  'система': 'system',
  'юзер': 'user',
  'занято': 'used',
  'кэш/буфер': 'cache/buffer',
  'свободно': 'free',
  'доступно': 'available',
  'Ядра': 'Cores',
  'Load 1 / 5 / 15 мин': 'Load 1 / 5 / 15 min',
  'заполнение по разделам': 'usage by mount',
  'Серверов пока нет.': 'No servers yet.',
  'Все {n} онлайн': 'All {n} online',
  'Оффлайн: {n}': 'Offline: {n}',
  'Поиск: имя, группа, IP…': 'Search: name, group, IP…',
  'Поиск: имя, адрес, группа…': 'Search: name, address, group…',
  'Поиск: хост, группа, контейнер, образ…': 'Search: host, group, container, image…',
  'Выбрать': 'Select',
  'Выбрано: {n}': 'Selected: {n}',
  'Все видимые': 'All visible',
  'Удалить ({n})': 'Delete ({n})',
  'Выбрать монитор': 'Select monitor',
  'Удалить выбранные мониторы ({n} шт.) вместе с историей?':
    'Delete the selected monitors ({n}) along with their history?',
  'Название попадает в текст алерта: @упоминания в нём тегнут людей в Telegram.':
    'The name goes into alert text: @mentions in it will tag people in Telegram.',
  'Ничего не найдено.': 'Nothing found.',
  'Сервер недоступен': 'Server unavailable',
  'обновлено': 'updated',
  'Удалить сервер': 'Delete server',
  'Открыть на весь экран': 'Open fullscreen',
  'Сбросить зум': 'Reset zoom',
  'Выделите участок мышью, чтобы приблизить': 'Drag on the chart to zoom in',
  'Нет данных за период': 'No data for this range',
  'Имя': 'Name',
  'Диск — три уровня (0 = выключить уровень):': 'Disk — three levels (0 = disable a level):',
  'предупр.': 'warn',
  'проблема': 'problem',
  'критично': 'critical',
  'агент ещё не выходил на связь': 'agent has not reported yet',
  'Агент ещё не выходил на связь. Установите и запустите kervax-agent на ноде — метрики появятся здесь. Изменить или удалить сервер — кнопка ✎.':
    'The agent has not reported yet. Install and start kervax-agent on the node — metrics will appear here. Use ✎ to edit or delete the server.',

  // --- бэкап ---
  'Бэкап': 'Backup',
  'Бэкап содержит мониторы, серверы, локации и настройки — без метрик (тайм-серий) и без учётных записей: пользователей, их роли и доступы после восстановления придётся завести заново.':
    'A backup contains monitors, servers, locations and settings — without metrics (time-series) and without accounts: users, their roles and permissions have to be created again after a restore.',
  'Алерт по коннектам СУБД, % (0 = выкл)': 'Database connection alert, % (0 = off)',
  // --- сроки Kubernetes и Flux ---
  'Предупреждать о сроках Kubernetes за, дн. (0 = выкл)':
    'Warn about Kubernetes expiry this many days ahead (0 = off)',
  'Сроки ({n})': 'Expiry ({n})',
  'Сроки ⏳ ({n})': 'Expiry ⏳ ({n})',
  'Сроки ⚠ ({n})': 'Expiry ⚠ ({n})',
  'Сертификаты, kubeconfig-и и токены Flux с их сроками, плюс состояние доставки Flux. Собирает root-хелпер на самой ноде: панель токенов не видит.':
    'Certificates, kubeconfigs and Flux tokens with their expiry dates, plus the state of Flux delivery. Collected by a root helper on the node itself: the panel never sees the tokens.',
  'Доставка Flux встала: {n} ресурс(ов) не в Ready. Уже запущенное продолжает работать — по метрикам это не видно.':
    'Flux delivery has stopped: {n} resource(s) not Ready. Everything already running keeps running — metrics will not show this.',
  'Ресурсы Flux': 'Flux resources',
  'сертификат control-plane': 'control-plane certificate',
  'сертификат kubelet': 'kubelet certificate',
  'токен Flux': 'Flux token',
  'Проверять с сервера (сайт закрыт снаружи)': 'Probe from the server (the site is closed from outside)',
  'снаружи, как обычно': 'from outside, as usual',
  'Агент на выбранном сервере постучится на localhost с этим именем хоста и пришлёт результат. Панель к сайту ходить не будет: снаружи он всё равно закрыт. Это проверка изнутри — она не докажет, что сайт виден посетителям. Белый список сайта должен пускать 127.0.0.1, иначе проверка получит обрыв.':
    'The agent on the chosen server will knock on localhost with this host name and report back. The panel will not visit the site: it is closed from outside anyway. This is a probe from within — it does not prove the site is reachable for visitors. The site allow-list must let 127.0.0.1 through, otherwise the probe gets a dropped connection.',
  'локально': 'local',
  'Проверяется изнутри сервера {srv}: панель к сайту не ходит, снаружи он закрыт':
    'Probed from within {srv}: the panel does not visit this site, it is closed from outside',
  'Проверять локально, с самого сервера (сайт закрыт снаружи)':
    'Probe locally, from the server itself (the site is closed from outside)',
  'Панель к сайту ходить не будет: снаружи он всё равно закрыт. Проверит агент на том сервере, чей веб-сервер обслуживает этот домен — панель найдёт его сама. Он постучится на localhost с этим именем хоста. Это проверка изнутри: она не докажет, что сайт виден посетителям. Белый список сайта должен пускать 127.0.0.1, иначе проверка получит обрыв.':
    'The panel will not visit the site: it is closed from outside anyway. It is probed by the agent on the server whose web server serves this domain — the panel finds it itself. The agent knocks on localhost with this host name. This is a probe from within: it does not prove the site is reachable for visitors. The site allow-list must let 127.0.0.1 through, otherwise the probe gets a dropped connection.',
  'Проверять локально включено, но домен не найден ни на одном сервере — проверять некому':
    'Local probing is on, but the domain was not found on any server — nobody can check it',
  'Похоже на белый список': 'Looks like an allow-list',
  '🏠 проверять локально: подходит {n} сайт(ов)': '🏠 probe locally: {n} site(s) qualify',
  'Эти сайты панель снаружи проверить не может — похоже на белый список. Те, чей домен держит ваш сервер, она предлагает проверять изнутри него. А если сайт ОТВЕЧАЕТ «доступ запрещён» — он жив, и довольно считать этот код нормой: изнутри там проверять нечего, прокси закрывает сайт сам.':
    'The panel cannot check these sites from outside — that looks like an allow-list. For those whose domain is served by one of your servers it offers a probe from within. And when a site ANSWERS with "forbidden" it is alive, so it is enough to treat that code as normal: there is nothing to probe from within, the proxy closes the site itself.',
  'отвечает {code} — считать нормой': 'answers {code} — treat as normal',
  'принять код': 'accept the code',
  'включить': 'enable',
  'включить для всех {n}': 'enable for all {n}',
  'Проверять локально, с самого сервера': 'Probe locally, from the server itself',
  'Алерты не придут': 'No alerts will arrive',
  'По этим объектам не сработает ни один алерт: они не попадают ни в одну область действия правил. Метрики собираются и графики рисуются — но когда что-то сломается, не придёт ничего.':
    'No alert will fire for these: they fall outside the scope of every rule. Metrics are still collected and charts still drawn — but when something breaks, nothing will arrive.',
  'Кнопка дописывает объект в область всех включённых правил: уведомления по нему пойдут туда же, куда по остальным.':
    'The button adds the object to the scope of every enabled rule: its notifications will go wherever the others go.',
  'не включён ни один тип алертов': 'no alert type is enabled',
  'объект без группы, а области алертов заданы по группам':
    'the object has no group, while alert scopes are defined by group',
  'группа «{g}» не входит ни в одну область алертов':
    'group “{g}” is not in the scope of any alert',
  'включить алерты': 'enable alerts',
  'включить алерты для всех {n}': 'enable alerts for all {n}',
  'включите типы алертов: ⚙ → Алерты': 'enable alert types: ⚙ → Alerts',
  'задайте группу, которая уже в области': 'assign a group that is already in scope',
  'Данных пока нет.': 'No data yet.',
  'Ресурсы Flux не в Ready — доставка встала': 'Flux resources are not Ready — delivery has stopped',
  'Сертификаты или токены истекают в ближайшие две недели':
    'Certificates or tokens expire within the next two weeks',
  'Flux ⚠ {n}': 'Flux ⚠ {n}',
  'сроки ⏳ {n}': 'expiry ⏳ {n}',
  'коннекты: {u} из {m} ({p}%)': 'connections: {u} of {m} ({p}%)',
  'Занято подключений на {c}': 'Connection slots in use on {c}',
  'этом движке': 'this engine',
  'Скачать бэкап': 'Download backup',
  'Восстановить из файла': 'Restore from file',
  'Автобэкап на сервере': 'Auto-backup on the server',
  'Панель сама сохраняет бэкапы на диск. Интервал 0 = выключить.':
    'The panel saves backups to disk itself. Interval 0 = off.',
  'Раз в, часов': 'Every, hours',
  'Хранить файлов': 'Keep files',
  'Создать сейчас': 'Create now',
  'Бэкапы на сервере': 'Backups on the server',
  'Пока нет файлов.': 'No files yet.',
  'Бэкап создан на сервере.': 'Backup created on the server.',
  'Файл не является корректным JSON.': 'The file is not valid JSON.',
  'Восстановить из «{name}»? Текущие мониторы, серверы и настройки будут заменены. Учётные записи в копию не входят и не восстановятся.':
    'Restore from “{name}”? Current monitors, servers and settings will be replaced. Accounts are not part of the backup and will not be restored.',
  'Восстановлено записей: {n}. Обновляю…': 'Restored {n} records. Reloading…',
  'Скачать': 'Download',

  // --- алерты / инциденты / uptime ---
  'Алерты': 'Alerts',
  'Уведомления о падении/восстановлении мониторов и скором истечении сертификатов.':
    'Notifications about monitor downtime/recovery and soon-expiring certificates.',
  'Токен бота': 'Bot token',
  'Адрес API (для обхода блокировок)': 'API URL (bypass blocking)',
  'Свой прокси/зеркало Bot API, если api.telegram.org недоступен. Пусто = по умолчанию.':
    'Your Bot API proxy/mirror if api.telegram.org is unreachable. Empty = default.',
  'Алерты серверов': 'Server alerts',
  'Плейсхолдеры: {server} {value} {threshold}.':
    'Placeholders: {server} {value} {threshold}.',
  'Группы': 'Groups',
  'Все серверы': 'All servers',
  'Группы: {n}': 'Groups: {n}',
  'Серверы: {n}': 'Servers: {n}',
  'Группы (нет)': 'Groups (none)',
  'Серверы (нет)': 'Servers (none)',
  'выключен': 'off',
  'Всё': 'All',
  'нет': 'none',
  'URL или домен': 'URL or domain',
  'Схему можно не писать — определим сами (приоритет https).':
    'You can omit the scheme — we detect it (https first).',
  'Алерты сайтов': 'Site alerts',
  'Поиск монитора…': 'Search monitor…',
  'Куда': 'Apply to',
  'Текст': 'Text',
  'Поиск группы…': 'Search group…',
  'Поиск сервера…': 'Search server…',
  'Сохранить правила': 'Save rules',
  'Не слать алерты (пауза)': 'Mute alerts (pause)',
  'Временно тушит все уведомления. Инциденты продолжают фиксироваться.':
    'Temporarily silences all notifications. Incidents are still recorded.',
  '⏸ Алерты на паузе': '⏸ Alerts paused',
  'Каналы настроены ✓': 'Channels configured ✓',
  'Каналы не настроены': 'Channels not configured',
  'Заполните Telegram (токен и Chat ID) или Webhook.':
    'Fill in Telegram (token and Chat ID) or a Webhook.',
  'Тест': 'Test',
  'Сохранено.': 'Saved.',
  'Тестовое уведомление отправлено.': 'Test notification sent.',
  'Не отправлено — проверьте настройки.': 'Not sent — check settings.',
  'Аптайм за 24ч': 'Uptime, 24h',
  '{n} откр. инцидентов': '{n} open incidents',

  // --- детали монитора / графики ---
  'Открыть детали': 'Open details',
  'Открыть': 'Open',
  'Аптайм 24ч': 'Uptime 24h',
  'Аптайм 7д': 'Uptime 7d',
  'Аптайм 30д': 'Uptime 30d',
  '1ч': '1h',
  '3ч': '3h',
  '6ч': '6h',
  '24ч': '24h',
  '7д': '7d',
  '30д': '30d',
  '90д': '90d',
  'Время ответа': 'Response time',
  'Осталось дней сертификата': 'Certificate days left',
  'дн.': 'd',
  'Инциденты': 'Incidents',
  'Инцидентов не было.': 'No incidents yet.',
  'Журнал проверок': 'Check log',
  'Только сбои': 'Failures only',
  'Все': 'All',
  'Сбоев не было.': 'No failures yet.',
  'Пока пусто.': 'Empty so far.',
  'до {t}': 'until {t}',
  'идёт сейчас': 'ongoing',

  // --- главная / сводка ---
  'На главную': 'Home',
  'Открыть →': 'Open →',
  'Мониторов пока нет.': 'No monitors yet.',
  'Все {n} в норме': 'All {n} healthy',
  'Проблемы: {n}': 'Issues: {n}',
  '…ещё {n}': '…{n} more',
  'Скоро': 'Coming soon',
  'Ресурсы нод (CPU/RAM/диск) и свои проверки по SSH без агента.':
    'Node resources (CPU/RAM/disk) and custom SSH checks, agentless.',

  // --- SSL / домен ---
  'SSL': 'SSL',
  'домен': 'domain',
  'мониторов: {n}': '{n} monitors',
  'Частично': 'Partial',
  'Недоступен из части локаций': 'Not reachable from some locations',
  'Не отвечает из этих точек проверки: {list}': 'Not answering from: {list}',
  'Точка проверки «{name}»: не отвечают {down} из {total} мониторов — похоже на проблему самой точки, а не сайтов.':
    'Probe location "{name}": {down} of {total} monitors are not answering — this looks like the location itself, not the sites.',
  'истёк': 'expired',
  'сегодня': 'today',
  'Поставить сайты на мониторинг': 'Add sites to monitoring',
  'Поставить на мониторинг': 'Add to monitoring',
  'Домены, найденные на серверах': 'Domains found on servers',
  'На серверах найдено доменов вне мониторинга: {n}':
    'Domains found on servers but not monitored: {n}',
  'Посмотреть и добавить': 'Review and add',
  'на мониторинг: {n}': 'monitor: {n}',
  'найдено доменов: {n}': 'domains found: {n}',
  'вне мониторинга: {n}': 'not monitored: {n}',
  'все уже под мониторингом': 'all are already monitored',
  'фильтр по домену или ноде…': 'filter by domain or node…',
  'только новые': 'new only',
  'выбрать все ({n})': 'select all ({n})',
  'снять выбор': 'clear selection',
  'новых: {n} из {m}': '{n} new of {m}',
  'всё покрыто ({m})': 'all covered ({m})',
  'Все найденные домены уже стоят на мониторинге.':
    'Every discovered domain is already monitored.',
  'будет создано мониторов: {n}': 'monitors to create: {n}',
  'выбрано {n}, за раз добавим {m}': '{n} selected, {m} added per run',
  'создано мониторов: {n}': 'monitors created: {n}',
  'ненужных: {n}': 'not needed: {n}',
  'скрыть ненужные ({n})': 'hide not needed ({n})',
  'не нужны ({n})': 'not needed ({n})',
  'Мониторить не нужно — убрать из предложений':
    'Not worth monitoring — drop from suggestions',
  'Вернуть в предложения': 'Bring back to suggestions',
  'группа': 'group',
  'добавляем…': 'adding…',
  'Место на диске': 'Disk space',
  'свободно {f} из {tt}': '{f} free of {tt}',
  'Обновить': 'Refresh',
  'контейнер упал и не поднялся — по нему отправлен алерт':
    'container is down and did not come back — an alert was sent',
  'контейнер постоянно перезапускается — по нему отправлен алерт':
    'container keeps restarting — an alert was sent',
  'Приглушить': 'Mute',
  'без алерта': 'no alert',
  'По этому инциденту алерт не отправлялся.': 'No alert was sent for this incident.',
  'Алерт уходит после {n} неудачных проверок подряд — это около {m} мин непрерывного сбоя. Этот инцидент закончился раньше. Порог меняется в настройках монитора.':
    'An alert is sent after {n} consecutive failures — about {m} min of continuous downtime. This incident ended sooner. The threshold is in the monitor settings.',
  'Мои алерты в Telegram': 'My Telegram alerts',
  'О панели Kervax': 'About Kervax',
  'версия': 'version',
  'Лицензия': 'License',
  'Следит за сайтами, серверами, Docker, Kubernetes и бэкапами — и пишет в Telegram, когда что-то ломается.':
    'Watches sites, servers, Docker, Kubernetes and backups — and messages you on Telegram when something breaks.',
  'Поставить свой логотип': 'Use your own logo',
  'свернуть настройку логотипа': 'hide logo settings',
  'У логотипа свой фон или он тёмный — под него подложена светлая плашка.':
    'The logo has its own background or is dark — a light plate is placed behind it.',
  'Фон прозрачный, логотип светлый — плашка не нужна.':
    'Transparent background and a light logo — no plate needed.',
  'Исходный код на GitHub': 'Source code on GitHub',
  'Сообщить о проблеме': 'Report an issue',
  'Заменяет логотип в шапке и на экране входа. PNG, SVG, WebP или JPEG до {n} КБ.':
    'Replaces the logo in the header and on the login screen. PNG, SVG, WebP or JPEG up to {n} KB.',
  'Файл логотипа': 'Logo file',
  'Файл больше {n} КБ — уменьшите логотип': 'The file is over {n} KB — make the logo smaller',
  'Не удалось прочитать файл': 'Could not read the file',
  'тёмная тема': 'dark theme',
  'светлая тема': 'light theme',
  'Подложка': 'Plate',
  'авто (по картинке)': 'auto (from the image)',
  'всегда': 'always',
  'никогда': 'never',
  'Подпись рядом с логотипом': 'Caption next to the logo',
  'например, название компании': 'e.g. your company name',
  'Сохранить логотип': 'Save logo',
  'сохраняем…': 'saving…',
  'Логотип сохранён': 'Logo saved',
  'Вернуть стандартный': 'Restore the default',
  'Вернули стандартный логотип': 'Default logo restored',
  'Приходят алерты только по тому, что доступно вашей учётной записи — по её разделам и группам.':
    'You only get alerts for what your account can see — its sections and groups.',
  'Привязано: чат {id}': 'Linked: chat {id}',
  'Не привязано — алерты не приходят': 'Not linked — no alerts are delivered',
  'свой бот': 'own bot',
  'Получать алерты': 'Receive alerts',
  '1. Напишите боту {bot} это сообщение:': '1. Send this message to {bot}:',
  '2. Потом нажмите «Проверить привязку».': '2. Then press "Check the link".',
  'Проверить привязку': 'Check the link',
  'проверяем…': 'checking…',
  'Привязать Telegram': 'Link Telegram',
  'Готово — чат привязан': 'Done — chat linked',
  'Отправить тест': 'Send a test',
  'Тестовое сообщение отправлено': 'Test message sent',
  'Отвязать': 'Unlink',
  'Отвязано': 'Unlinked',
  'Настроить вручную': 'Manual setup',
  'скрыть ручные настройки': 'hide manual setup',
  'ID чата': 'Chat ID',
  'Токен своего бота (пусто — общий бот панели)':
    "Own bot token (empty — the panel's shared bot)",
  'задан — введите новый, чтобы заменить': 'set — type a new one to replace',
  'Свой бот нужен, только если хотите отдельного бота или свой прокси к Telegram. Для группы добавьте бота в неё и укажите её ID.':
    'You only need your own bot for a separate bot or your own Telegram proxy. For a group, add the bot to it and enter the group ID.',
  'без группы': 'no group',
  'Создаётся HTTPS-монитор на каждый домен. Первая проверка — на ближайшем тике планировщика.':
    'One HTTPS monitor per domain. The first probe runs on the scheduler\'s next tick.',
  'Уже в мониторинге — открыть монитор': 'Already monitored — open the monitor',
  'Уже в мониторинге': 'Already monitored',
  'Маска или regexp — монитору нужен конкретный адрес':
    'Wildcard or regexp — a monitor needs a concrete address',
  'нет данных': 'no data',
  'Следить за SSL-сертификатом (валидность и срок)':
    'Watch TLS certificate (validity & expiry)',
  'Следить за сроком регистрации домена': 'Watch domain registration expiry',
  'предупредить за, дн.': 'warn, days before',
  'SSL истекает': 'SSL expiring',
  'Домен истекает': 'Domain expiring',
  'SSL-сертификат': 'TLS certificate',
  'Домен': 'Domain',
}

type Ctx = {
  lang: Lang
  setLang: (l: Lang) => void
  t: (s: string, params?: Record<string, string | number>) => string
}

const I18nContext = createContext<Ctx>({
  lang: 'ru',
  setLang: () => {},
  t: (s) => s,
})

function fill(s: string, params?: Record<string, string | number>): string {
  if (!params) return s
  return s.replace(/\{(\w+)\}/g, (_, k) =>
    k in params ? String(params[k]) : `{${k}}`,
  )
}

// Текущий язык на уровне модуля — чтобы не-React форматтеры (байты, время «назад»)
// тоже локализовались. Синхронизируется из провайдера; смена языка ре-рендерит.
let _curLang: Lang = (localStorage.getItem('kervax_lang') as Lang) || 'ru'
// eslint-disable-next-line react-refresh/only-export-components
export const currentLang = (): Lang => _curLang

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(
    () => (localStorage.getItem('kervax_lang') as Lang) || 'ru',
  )
  _curLang = lang
  const setLang = useCallback((l: Lang) => {
    localStorage.setItem('kervax_lang', l)
    _curLang = l
    setLangState(l)
  }, [])
  const t = useCallback(
    (s: string, params?: Record<string, string | number>) =>
      fill(lang === 'en' ? EN[s] ?? s : s, params),
    [lang],
  )
  return (
    <I18nContext.Provider value={{ lang, setLang, t }}>
      {children}
    </I18nContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export const useI18n = () => useContext(I18nContext)

// Перевод ВНЕ React-дерева: форматтеры, генераторы копипаст-скриптов, разбор
// ошибок в api.ts. Хук там недоступен, а без перевода английская панель
// показывала бы русские подписи — язык берём из той же модульной переменной.
// eslint-disable-next-line react-refresh/only-export-components
export function tr(s: string, params?: Record<string, string | number>): string {
  return fill(_curLang === 'en' ? EN[s] ?? s : s, params)
}
