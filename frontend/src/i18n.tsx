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
  'Поле "{field}": {what}': 'Field "{field}": {what}',
  'обязательное поле': 'required',
  'должно быть не меньше {n}': 'must be at least {n}',
  'должно быть больше {n}': 'must be greater than {n}',
  'должно быть не больше {n}': 'must be at most {n}',
  'должно быть меньше {n}': 'must be less than {n}',
  'не может быть пустым': 'cannot be empty',
  'не короче {n} символов': 'at least {n} characters',
  'не длиннее {n} символов': 'at most {n} characters',
  'не больше {n} значений': 'at most {n} values',
  'недопустимое значение': 'invalid value',
  'допустимо: {list}': 'allowed: {list}',
  'нужно целое число': 'must be a whole number',
  'Сервер прислал непонятный ответ, обновите страницу': 'The server sent an unreadable response, reload the page',
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
  'Пароль (мин. {n})': 'Password (min. {n})',
  'Учётка «Только просмотр» видит все данные, но ничего не может менять.':
    'A "View only" account can see everything but change nothing.',
  'Удалить учётку «{name}»?': 'Delete account "{name}"?',
  'Не удалось войти': 'Sign in failed',
  'Неверный логин или пароль': 'Wrong login or password',
  'Код из приложения (2FA)': 'Code from the app (2FA)',
  'Неверный код 2FA': 'Invalid 2FA code',

  // --- смена пароля ---
  'Сменить пароль': 'Change password',
  'Текущий пароль': 'Current password',
  'Новый пароль (мин. {n} символов)': 'New password (min. {n} characters)',
  'Повторите новый пароль': 'Repeat new password',
  'Пароль изменён. Все прежние сессии завершены.':
    'Password changed. All previous sessions have been ended.',
  'Пароль должен быть не короче {n} символов':
    'Password must be at least {n} characters',
  'Пароли не совпадают': 'Passwords do not match',

  // --- 2FA ---
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
  'Проверяем…': 'Checking…',
  'Работает с замечаниями': 'Works with issues',
  'Не работает': 'Not working',
  'Проверить не удалось': 'Could not check',
  'Запрос к сайту идёт прямо сейчас': 'Requesting the site right now',
  'Сайт проверяет агент изнутри сервера {srv}': 'The agent is checking the site from inside {srv}',
  'Сайт проверяет агент изнутри сервера': 'The agent is checking the site from inside the server',
  'обычно это до 15 секунд': 'usually up to 15 seconds',
  'проверено изнутри сервера {srv}': 'checked from inside {srv}',
  'проверено из панели': 'checked from the panel',
  'Ответ проверки так и не пришёл.': 'The check result never arrived.',
  'работает': 'up',
  'деградация': 'degraded',
  'недоступен': 'down',
  'Изменить': 'Edit',
  'Удалить': 'Delete',
  'выкл': 'off',
  'чистка': 'rotation',
  'снято {n}': '{n} removed',
  'старейший': 'oldest',
  'политика допускает снапшоты возрастом до {n} дн.': 'policy allows snapshots up to {n} days old',
  'Чистка идет, а старые снапшоты остаются: похоже, это другая группа, менялись хост или пути бэкапа. Последние снапшоты каждой группы restic хранит, пока их не удалить вручную.':
    'Cleanup runs, yet old snapshots stay: they probably belong to another group, the host or backup paths changed. restic keeps the latest snapshots of every group until they are removed by hand.',
  '— агент молчит {ago}. Всё, что ниже, — последние данные, снятые тогда же.':
    '— the agent has been silent for {ago}. Everything below is the last data, taken back then.',
  '— агент ещё ни разу не выходил на связь.': '— the agent has never reported in.',
  'сервер не чистит': 'the server does not clean this up',
  'Ни своего prune-скрипта, ни блока в старом общем скрипте у репозитория нет: сервер его не чистит. Старое убирает только сам клиент, если у него это настроено.':
    'The repository has neither its own prune script nor a block in the old shared script: the server does not clean it. Anything old is removed only by the client itself, if it is set up to.',
  'чистит старый скрипт': 'cleaned by the old script',
  'последний прогон {ago}': 'last run {ago}',
  'старый скрипт чистки не запускается': 'the old cleanup script never runs',
  'Репозиторий описан в {script}, но этот скрипт не запланирован ни в cron, ни в systemd: старые снапшоты никто не удаляет.':
    'The repository is listed in {script}, but that script is not scheduled in cron or systemd: nobody removes old snapshots.',
  'свой prune-скрипт без метрик': 'own prune script, no metrics',
  'Свой prune-скрипт есть, но метрик он не пишет: когда он отработал, панель не знает.':
    'There is a prune script of its own, but it writes no metrics: the panel does not know when it last ran.',
  'удаляли': 'removed',
  'когда из репозитория последний раз удалялись снапшоты (кто бы их ни удалял)':
    'when snapshots were last removed from the repository, by whoever removed them',
  'удалений нет {n} дн.': 'no removals for {n} d',
  'панель следит за удалениями с {d}': 'the panel has been watching removals since {d}',
  'залочен {n} дн.': 'locked for {n} d',
  'Висячий лок держится больше суток: чистка и restic check по репозиторию не идут, пока его не снять.':
    'A stale lock has been held for over a day: cleanup and restic check cannot run on this repository until it is removed.',
  'висячий лок {n} ч': 'stale lock {n} h',
  'Лок не обновлялся больше 30 минут: процесс, который его взял, умер. Чистка и клиенты обычно снимают такие локи сами, если лок продержится сутки, придет алерт.':
    'The lock has not been refreshed for over 30 minutes: the process that took it is gone. Cleanup and clients usually remove such locks themselves, if it lasts a day an alert follows.',
  'У репозитория нет своего prune-скрипта: политика показана из старого общего конфига, но сервер по ней ничего не удаляет. Старое убирает только сам клиент, если у него это настроено.':
    'This repository has no prune script of its own: the retention shown comes from an old shared config the server never runs. Anything old is removed only by the client itself, if it is set up to.',
  'политика хранения не задана, судить не о чем': 'no retention policy, nothing to judge by',
  'последний прогон завершился ошибкой': 'the last run finished with an error',
  'Название': 'Name',
  'Тип': 'Type',
  'HTTP(S) — сайт/эндпоинт': 'HTTP(S) — site/endpoint',
  'TCP-порт': 'TCP port',
  'TLS-сертификат': 'TLS certificate',
  'URL': 'URL',
  'Порт': 'Port',
  'Интервал, сек': 'Interval, s',
  'Порог «медленно», мс': 'Slow threshold, ms',
  'Повторы при сбое': 'Retries on failure',
  'Считать «упавшим» и слать алерт, только если провалятся все попытки подряд.':
    'Mark down and alert only if all consecutive attempts fail.',
  'Алерт после N сбоев подряд': 'Alert after N failures in a row',
  'Алерт «медленно» после N подряд': 'Slow alert after N in a row',
  'Отдельный, обычно больший порог для деградации (медленных ответов) — она шумнее падения.':
    'A separate, usually higher threshold for degraded (slow) responses — noisier than an outage.',

  // --- хранение данных (retention) ---
  'Хранение данных': 'Data retention',
  'Метрики серверов': 'Server metrics',
  'полгода': 'half-year',
  'год': 'year',

  // --- локации (прокси) ---
  'Локации': 'Locations',
  'простой': 'idle',
  'Откуда проверять': 'Where to probe from',
  'С панели': 'From the panel',
  'Изнутри сервера': 'From the server itself',
  'Из локаций': 'From locations',
  'Проверять из локаций (прокси)': 'Check from locations (proxies)',
  'Адрес прокси': 'Proxy URL',
  'напр. Германия': 'e.g. Germany',
  'Удалить локацию «{name}»?': 'Delete location “{name}”?',
  'Включена': 'Enabled',
  'Ещё нет данных из локаций — проверка идёт в фоне.':
    'No location data yet — checks run in the background.',
  'проверено: {t}': 'checked: {t}',
  'socks5://… (пусто = напрямую)': 'socks5://… (empty = direct)',
  'напрямую': 'direct',
  'Время ответа · {loc}': 'Response time · {loc}',
  'Из каких локаций проверять:': 'Which locations to check from:',
  'Локаций пока нет — добавьте в ⚙ → Локации.':
    'No locations yet — add them in ⚙ → Locations.',

  // --- пороги напоминаний / массовое применение ---
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
  'На что ушло место': 'Where the disk space went',
  'Место на дисках': 'Disk space',
  '{u} из {s}': '{u} of {s}',
  'свободно {a}': '{a} free',
  '+{r} резерв': '+{r} reserved',
  'еще {r} - резерв root, обычным процессам он недоступен; df поэтому показывает больше процентов': '{r} more is the root reserve, regular processes cannot use it; that is why df shows a higher percentage',
  'заполнится {w}, +{r}%/сут': 'fills up {w}, +{r}%/day',
  'inode кончатся {w}': 'inodes run out {w}',
  'не растет': 'not growing',
  'Не разбирался: разбор места включается сам от 75%.': 'Not analyzed: the space breakdown turns on by itself from 75%.',
  'Раздел больше не заполнен, разбор места скрыт: он появляется от 75%.': 'The filesystem is no longer full, the breakdown is hidden: it shows up from 75%.',
  'автоочистка от {p}%': 'auto cleanup from {p}%',
  'автоочистка выкл': 'auto cleanup off',
  'включается в настройках сервера': 'turned on in the server settings',
  'Куда ушло': 'Where it went',
  'показать все ({n})': 'show all ({n})',
  'Каталоги не считались: в разделе {n} млн файлов, полный обход слишком долгий. Ниже то, что нашлось по известным местам.': 'Directories were not counted: the filesystem has {n} M files, a full walk takes too long. Below is what was found in known places.',
  'Мусора по известным местам нет (логи, журнал, docker, временные, кэши). Место занимают данные выше.': 'No junk in known places (logs, journal, docker, temp, caches). The space is taken by the data above.',
  'еще {n} на {s}': '{n} more, {s}',
  'как': 'how',
  'что это': 'what is it',
  'команды': 'commands',
  'скрыть команды': 'hide commands',
  'Без риска освободится {n}': '{n} can be freed safely',
  '{p}% станет {a}%': '{p}% becomes {a}%',
  'Освободить безопасное': 'Free up the safe ones',
  'Выполняю {i} из {n}: {name}...': 'Running {i} of {n}: {name}...',
  'Нагрузка дисков': 'Disk load',
  'лог контейнера {n}': 'container log {n}',
  'образы без контейнеров': 'images without containers',
  'образы без тега': 'untagged images',
  'тома без контейнеров': 'volumes without containers',
  'docker, {n} шт.': 'docker, {n} pcs',
  'кэш сборки': 'build cache',
  'журнал systemd': 'systemd journal',
  'останется 200 МБ': '200 MB will stay',
  'старые ротированные логи': 'old rotated logs',
  '{n} файлов, /var/log': '{n} files, /var/log',
  'кэш apt': 'apt cache',
  'кэш dnf/yum': 'dnf/yum cache',
  'есть свежие файлы': 'has fresh files',
  '{n} старых файлов': '{n} old files',
  'удаленные, но открытые файлы': 'deleted but still open files',
  'Освободить': 'Free up',
  'Автоочистка диска': 'Automatic disk cleanup',
  'Когда раздел дошел до порога предупреждения, панель сама освобождает безопасное: журнал, старые логи, кэши пакетов, мусор docker. Каждое действие не чаще раза в 6 часов, о каждом запуске пишет в канал алертов. Включает только админ, нужны агент 2.13 и helper diskusage-setup 0.3.':
    'When a filesystem reaches the warning threshold, the panel frees the safe things by itself: the journal, old logs, package caches, docker leftovers. Each action at most once in 6 hours, every run is reported to the alert channel. Only admins can turn it on; needs agent 2.13 and the diskusage-setup helper 0.3.',
  'Автоочистка включена: от {p}% панель сама освобождает безопасное.': 'Automatic cleanup is on: from {p}% the panel frees the safe things by itself.',
  'Автоочистка выключена, включается в настройках сервера.': 'Automatic cleanup is off; turn it on in the server settings.',
  'Раздел больше не заполнен ({d}), разбор места скрыт: он появляется от 75%.':
    'The filesystem is no longer filling up ({d}), so the breakdown is hidden: it appears from 75%.',
  'Разбор места сам включается от 75%. Кнопка "Разобрать" появится с агентом 2.24 и helper diskusage-setup 0.6.':
    'The disk breakdown starts by itself from 75%. The "Analyze" button comes with agent 2.24 and helper diskusage-setup 0.6.',
  'Разбор запущен, появится ниже через минуту-две.': 'The breakdown has started, it shows up below in a minute or two.',
  'Запрашиваю...': 'Requesting...',
  'Разобрать': 'Analyze',
  '{p}: похоже на копию репозитория restic': '{p}: looks like a copy of a restic repository',
  '{p}: дампы баз': '{p}: database dumps',
  '{p}: архивы': '{p}: archives',
  '{p}: старые файлы': '{p}: old files',
  'Старых файлов: {n}, но есть и свежие: каталогом пользуются. Удаляйте вручную, если уверены.':
    'Old files: {n}, but there are fresh ones too: the directory is in use. Delete by hand if you are sure.',
  'Старых файлов: {n}. Если каталог не нужен, удаляйте целиком.': 'Old files: {n}. If the directory is not needed, delete it as a whole.',
  'Контейнеры по памяти': 'Containers by memory',
  'Что сломано': 'What is broken',
  // упавшие юниты systemd
  'На сервере restic {v}, нужен {target}, как у клиентов':
    'The server has restic {v}, {target} is needed, as on the clients',
  'Обновить restic на сервере': 'Update restic on the server',
  'Обычно его обновляет установка helper\'а: значит, скачать с GitHub не вышло.':
    'Installing the helper usually updates it, so the download from GitHub failed.',
  'Будет стерто безвозвратно: {n} снапшотов, {sz}, последний {ago}. Других копий у панели нет.':
    'This erases for good: {n} snapshots, {sz}, the last one {ago}. The panel has no other copies.',
  'Если бэкап еще может понадобиться, приглушите репозиторий (🔕): данные останутся.':
    'If the backup may still be needed, mute the repository (🔕): the data stays.',
  'Репозиторий пустой и не инициализирован, удалять нечего, кроме мусора.':
    'The repository is empty and not initialized, there is nothing to delete but leftovers.',
  'Удалить на бэкап-сервере от root': 'Delete on the backup server as root',
  'Панель бэкапы не удаляет, это защита от взлома. Если репозиторий заводил Ansible, проверьте еще строку в sudo crontab -l.':
    'The panel does not delete backups, this protects against a break-in. If Ansible created the repository, also check for a line in sudo crontab -l.',
  'Снять висячие локи на бэкап-сервере от root': 'Remove stale locks on the backup server as root',
  'Живой лок идущего бэкапа или чистки (моложе 30 минут) команда не тронет. Пароль читается из env внутри команды и в списке процессов не виден.':
    'A live lock of a running backup or cleanup (younger than 30 minutes) is left alone. The password is read from the env inside the command and is not visible in the process list.',
  'Снял, проверить': 'Done, check',
  'Статус обновится сам в течение минуты.': 'The status updates by itself within a minute.',
  'перевести репозиторий на чистку панели': 'move the repository to the panel cleanup',
  'удалить старую группу или, если ее надо хранить, убрать в архив':
    'delete the old group or, if it must be kept, archive it',
  'посмотреть, что делает чистка и что удалит политика': 'check what the cleanup does and what the policy would delete',
  'завести чистку или убрать репозиторий в архив': 'set up a cleanup or archive the repository',
  'обновить helper backupserver-setup: по старому не видно, кто чистит репозиторий':
    'update the backupserver-setup helper: the old one does not show who cleans the repository',
  'Перевести их на чистку панели': 'Move them to the panel cleanup',
  'У каждого репозитория будет свой prune-скрипт с той же политикой и паролем, чистка раз в сутки и проверка целостности раз в неделю.':
    'Each repository gets its own prune script with the same policy and password, a daily cleanup and a weekly integrity check.',
  'Перевести на чистку панели можно по желанию: она снимает висячие локи, пишет метрики, раз в неделю проверяет целостность и не держит вечно старые группы снапшотов.':
    'Moving to the panel cleanup is optional: it removes stale locks, writes metrics, checks integrity weekly and does not keep old snapshot groups forever.',
  'включил helper до 0.34: выключить, как было, или подтвердить, что она нужна':
    'a helper before 0.34 turned it on: turn it off as it was, or confirm it is needed',
  'Прокси docker-сокета пускает только чтение: перезапуск, стоп и старт дают 403':
    'The docker socket proxy is read-only: restart, stop and start return 403',
  'Docker есть, но список контейнеров агенту не виден': 'Docker is here, but the agent cannot see the container list',
  'Заменить прокси на прокси с перезапуском': 'Replace the proxy with one that allows restarts',
  'Включить доступ на чтение': 'Enable read access',
  'Просмотр плюс перезапуск, стоп и старт, без exec, создания контейнеров и образов. Слушает только 127.0.0.1 хоста.':
    'Viewing plus restart, stop and start, no exec, no creating containers or images. Listens on the host\'s 127.0.0.1 only.',
  'docker-socket-proxy: просмотр и перезапуск, без exec и root. Новым нодам - флаг --docker при установке.':
    'docker-socket-proxy: viewing and restart, no exec or root. For new nodes, the --docker flag at install.',
  'Поды, которые перезапускались за сутки. В Telegram: OOMKilled от 3 раз, падения и убийства по пробе от 5, пачки от {n}. Выход с кодом 0 - плановый, только здесь.':
    'Pods restarted in the last 24 h. To Telegram: OOMKilled from 3 times, crashes and probe kills from 5, bursts from {n}. Exit code 0 is planned and shown only here.',
  'Пачек за сутки: {n}: поды разных ворклоадов перезапускались разом, подвисала нода':
    'Bursts in 24 h: {n}: pods of different workloads restarted at once, the node stalled',
  'Чинить ноду, а не поды: нагрузка, память, диск под etcd': 'Fix the node, not the pods: load, memory, the etcd disk',
  'В эти минуты на ноде было давление или поды упали по таймауту пробы и kube-api (leader election lost). Плановый выход с кодом 0 и заглушенные поды в пачки не идут.':
    'In those minutes the node was under pressure, or pods failed on probe and kube-api timeouts (leader election lost). Planned exit code 0 and muted pods do not count in bursts.',
  'Кластер есть, но у агента нет доступа к kube-api': 'There is a cluster, but the agent has no access to kube-api',
  'Включить доступ на чтение и точечное управление': 'Enable read access and targeted management',
  'через ansible: плейбук поставит kube-setup и перезапустит агент':
    'via ansible: the playbook installs kube-setup and restarts the agent',
  'Скрипт создает ServiceAccount с узким RBAC (не cluster-admin) и кладет токен для агента.':
    'The script creates a ServiceAccount with narrow RBAC (not cluster-admin) and leaves a token for the agent.',
  'Часы разошлись с панелью на {n}, синхронизируйте время': 'The clock is {n} off from the panel, sync the time',
  'Время не синхронизировано, включите NTP': 'Time is not synced, turn on NTP',
  'Включить синхронизацию': 'Turn on time sync',
  'Включить systemd-timesyncd: демона синхронизации на сервере нет':
    'Turn on systemd-timesyncd: the server has no time sync daemon',
  'до NTP не достучаться: открыть исходящий UDP 123 (sudo ufw allow out 123/udp)':
    'NTP is unreachable: open outgoing UDP 123 (sudo ufw allow out 123/udp)',
  'разово выставить часы по времени панели, она доступна по HTTPS':
    'set the clock once from the panel time, it is reachable over HTTPS',
  'Кнопка "в один клик" появится после установки timesync-хелпера (ansible kervax_helpers.yml).':
    'The one-click button appears after the timesync helper is installed (ansible kervax_helpers.yml).',
  'Поставить автообновления. Ставят только исправления безопасности из репозитория дистрибутива: сторонние (docker и прочие) не трогают, конфиги не меняют, сами не перезагружают.':
    'Install automatic updates. They install only security fixes from the distribution repository: third-party ones (docker and others) are left alone, configs are not changed, no reboots.',
  'Перезагрузить при ближайшем окне обслуживания, срочности нет. Обновлен только dbus, а отдельно его не перезапустить: от него зависят systemd-logind и сессии.':
    'Reboot in the next maintenance window, no hurry. Only dbus was updated, and it cannot be restarted on its own: systemd-logind and sessions depend on it.',
  'Перезагрузить, когда есть доступ к консоли сервера. Несколько минут сервер недоступен. Старое ядро остается в меню GRUB (Advanced options), если новое не загрузится.':
    'Reboot when you have access to the server console. The server is down for a few minutes. The old kernel stays in the GRUB menu (Advanced options) in case the new one does not boot.',
  'Подключить Ubuntu Pro: исправления безопасности до {date}. Токен - на ubuntu.com/pro, для личного использования бесплатно до 5 машин, для компании нужна подписка. Подключение панель видит с агента 2.39.':
    'Attach Ubuntu Pro: security fixes until {date}. The token is at ubuntu.com/pro, free for personal use on up to 5 machines, a company needs a subscription. The panel sees the attachment from agent 2.39.',
  'Переехать на следующую версию, по одной за раз. С перезагрузкой: заранее бэкап и доступ к консоли.':
    'Move to the next release, one at a time. It needs a reboot: have a backup and console access ready.',
  'Переехать на Debian {n} по release notes. sources.list на новое кодовое имя, apt full-upgrade и перезагрузка: заранее бэкап и доступ к консоли.':
    'Move to Debian {n} following the release notes. sources.list to the new codename, apt full-upgrade and a reboot: have a backup and console access ready.',
  'Переехать на следующую версию. RHEL, AlmaLinux и Rocky - через leapp, CentOS - переустановкой на AlmaLinux или Rocky.':
    'Move to the next release. RHEL, AlmaLinux and Rocky via leapp, CentOS by reinstalling as AlmaLinux or Rocky.',
  'Пункт уйдет сам в течение часа после этих шагов.': 'The item goes away by itself within an hour after these steps.',
  'Найти причину в логе': 'Find the cause in the log',
  'systemd поднимает сервис сам, поэтому среди упавших его нет.':
    'systemd brings the service back itself, so it is not among the failed units.',
  'Сначала добавить ключ для {arg}. Сейчас у нее только пароль, последний вход {on}: после выключения паролей она не войдет. Вместо <public-key> - свой публичный ключ (ssh-ed25519 ...), вход по ключу проверить в новом окне.':
    'First add a key for {arg}. It has only a password now, last login {on}: once passwords are off, it cannot log in. Put your public key (ssh-ed25519 ...) instead of <public-key> and check key login in a new window.',
  'Проверить вход по ключу для {arg} в новом окне. Она входила по паролю {on}, хотя ключ есть: если ключ не тот, добавьте нужный.':
    'Check key login for {arg} in a new window. It logged in with a password {on} although it has a key: if the key is wrong, add the right one.',
  'Выключить вход по паролю отдельным файлом. Имя на 00- нужно, чтобы файл читался раньше 50-cloud-init.conf: в sshd побеждает первое значение.':
    'Turn off password login with a separate file. The 00- name makes it load before 50-cloud-init.conf: in sshd the first value wins.',
  'Выключить вход по паролю в sshd_config. У этой версии OpenSSH нет sshd_config.d, строка встает первой. Копия - sshd_config.bak-nopass.':
    'Turn off password login in sshd_config. This OpenSSH version has no sshd_config.d, the line goes first. Backup: sshd_config.bak-nopass.',
  'Проверить, пока ничего не применено: ниже должно быть no. Если yes - значение задано раньше, найти где: sudo grep -rn PasswordAuthentication /etc/ssh/':
    'Check before anything is applied: below it must say no. If yes, the value is set earlier, find where: sudo grep -rn PasswordAuthentication /etc/ssh/',
  'Применить, открытые сессии не рвутся. Это окно не закрывайте, пока в новом не войдете по ключу: если не пускает, отсюда же откат.':
    'Apply, open sessions stay. Keep this window open until you log in with a key in a new one: if it fails, roll back from here.',
  'если что-то не так': 'if something is wrong',
  'пароль только из своих сетей, из интернета - по ключу. Вместо <cidr> - ваши адреса или сети через запятую (192.0.2.10,198.51.100.0/24), иначе пароль выключится везде. Этот файл вместо файла выше, дальше та же проверка и reload.':
    'password only from your own networks, key from the internet. Put your addresses or networks, comma separated, instead of <cidr> (192.0.2.10,198.51.100.0/24), otherwise passwords go off everywhere. Use this file instead of the one above, then the same check and reload.',
  'пароль нужен отовсюду: fail2ban от перебора. Банит адрес после нескольких неудачных входов, здесь порт {arg} (на RHEL пакет из EPEL). Пункт останется, уйдет только приписка про перебор.':
    'password needed from everywhere: fail2ban against brute force. It bans an address after a few failed logins, port {arg} here (on RHEL the package is from EPEL). The item stays, only the brute force note goes away.',
  'Через другие учетки за 90 дней никто не входил. Если отключить {arg}, войти на сервер может стать некому: сначала проверьте вход другой учеткой с sudo или через консоль.':
    'Nobody logged in with other accounts in 90 days. If you disable {arg}, nobody may be able to log in: first check login with another sudo account or the console.',
  'Убедиться, что от {arg} ничего не работает. Процессы, crontab, lingering и сервисы с User={arg}: пусто и no crontab - ничего нет.':
    'Make sure nothing runs as {arg}. Processes, crontab, lingering and services with User={arg}: empty and no crontab means nothing.',
  'Отключить: пароль заблокирован, срок учетки в прошлом. SSH не пустит ни по паролю, ни по ключу, cron и su для нее тоже. Обратимо.':
    'Disable: the password is locked, the account expiry is in the past. SSH lets it in neither with a password nor a key, cron and su stop too. Reversible.',
  'Заблокировать пароль {arg}: sudo и cron root продолжат работать. Срок учетки root не ставят, истекшая root ломает cron и su. Если в консоль входите root по паролю, после блокировки не войдете.':
    'Lock the {arg} password: sudo and root cron keep working. root gets no expiry, an expired root breaks cron and su. If you log in to the console as root with a password, you will not after the lock.',
  'Убрать ключи: файл переименовывается, а не удаляется. Панель считает доступом любой ключ в authorized_keys.':
    'Remove the keys: the file is renamed, not deleted. The panel counts any key in authorized_keys as access.',
  'если учетка понадобится': 'if the account is needed again',
  'удалить совсем - необратимо. Домашний каталог останется без хозяина (userdel -r удалит и его). Лучше сначала поживите с отключенной.':
    'delete for good, irreversible. The home directory is left without an owner (userdel -r deletes it too). Better live with it disabled first.',
  'Учетка уйдет из заброшенных в течение 10 минут.': 'The account leaves the stale list within 10 minutes.',
  'Входы по журналу sshd видны с {d}.': 'Logins from the sshd log are visible since {d}.',
  'Включить автозапуск': 'Enable start at boot',
  'Пункт уйдет сам в течение 10 минут после выключения паролей.':
    'The item goes away by itself within 10 minutes after passwords are off.',
  'Нужно действие на сервере': 'Action needed on the server',
  '{label} (helper {helper}): {a} -> {b}': '{label} (helper {helper}): {a} -> {b}',
  'Выполнить от root, одной командой': 'Run as root, one command',
  'OTA обновляет только бинарь, не юнит, поэтому вручную.': 'OTA updates only the binary, not the unit, hence by hand.',
  'Переустановка helper\'а ничего не ломает.': 'Reinstalling a helper breaks nothing.',
  'Убрать неработающий резолвер из настроек сети (netplan, /etc/resolv.conf) или заменить рабочим':
    'Remove the dead resolver from the network settings (netplan, /etc/resolv.conf) or replace it with a working one',
  'Остальные отвечают и имена резолвятся, но запасного нет.':
    'The others answer and names resolve, but there is no spare.',
  'Проверить свой резолвер ноды (unbound, dnsmasq) и серверы, куда он пересылает запросы':
    'Check the node\'s own resolver (unbound, dnsmasq) and the servers it forwards to',
  'Поставить первыми в /etc/resolv.conf публичные: nameserver 1.1.1.1 и 8.8.8.8':
    'Put public ones first in /etc/resolv.conf: nameserver 1.1.1.1 and 8.8.8.8',
  'Пока тормозят резолверы провайдера: запросы идут к резолверам по порядку.':
    'While the provider resolvers are slow: queries go to resolvers in order.',
  'Добавить публичные резолверы': 'Add public resolvers',
  'Пока тормозят резолверы провайдера: systemd-resolved спросит их вместе с резолверами сети и возьмет первый ответ.':
    'While the provider resolvers are slow: systemd-resolved asks them along with the network resolvers and takes the first answer.',
  'mdadm некуда слать письма (в mdadm.conf нет MAILADDR): за RAID он не следит и падает при каждом запуске':
    'mdadm has nowhere to send mail (no MAILADDR in mdadm.conf): it does not watch the RAID and fails on every start',
  'Выключить мониторинг mdadm на этом сервере': 'Turn off the mdadm monitor on this server',
  'по всему парку: плейбук не тронет ноды, где MAILADDR задан':
    'fleet-wide: the playbook skips nodes where MAILADDR is set',
  'За RAID следит Kervax, раздел Диск.': 'Kervax watches the RAID, see the Disk section.',
  'unbound пытается прописаться в systemd-resolved, которого на сервере нет':
    'unbound tries to register itself in systemd-resolved, which is not on this server',
  'Выключить юнит': 'Turn the unit off',
  'по всему парку': 'fleet-wide',
  'Сам unbound работает, этот юнит ему не нужен.': 'unbound itself works and does not need this unit.',
  'При загрузке не дождались сети: обычно мешает интерфейс, который не нужен или не под networkd':
    'The network was not ready at boot: usually an interface that is not needed or not managed by networkd',
  'Нажать "Сбросить": сервер работает, юнит нужен только при загрузке':
    'Press "Reset": the server works, the unit only matters at boot',
  'если повторяется на каждой загрузке: ждать только нужный интерфейс (--interface=) или выключить юнит':
    'if it repeats on every boot: wait only for the right interface (--interface=) or turn the unit off',
  'Сокет не смог занять порт: его уже держит сам сервис (например, sshd со своим портом)':
    'The socket could not take its port: the service itself holds it (for example, sshd with its own port)',
  'Выключить лишний сокет': 'Turn the extra socket off',
  'Перезапуск не поможет: порт занят. Сервис при этом работает.':
    'A restart will not help: the port is taken. The service itself works.',
  'restic не смог прочитать часть файлов (код 3): снапшот есть, но неполный':
    'restic could not read some files (exit code 3): the snapshot exists but is incomplete',
  'Найти в журнале, какие файлы': 'Find which files in the log',
  'Исключить их из бэкапа или выдать доступ': 'Exclude them from the backup or grant access',
  'freshclam не может скачать базы антивируса, они устаревают':
    'freshclam cannot download the antivirus databases, they get stale',
  'Прописать свое зеркало (PrivateMirror в freshclam.conf) или прокси: зеркало ClamAV закрыто для адресов из России':
    'Set your own mirror (PrivateMirror in freshclam.conf) or a proxy: the ClamAV mirror is closed to Russian addresses',
  'openipmi не нашел IPMI на этой машине': 'openipmi found no IPMI on this machine',
  'Если BMC нет или он не нужен, выключить юнит': 'If there is no BMC or it is not needed, turn the unit off',
  'Не применилась раскладка консоли, на сервере это ни на что не влияет':
    'The console keyboard layout was not applied; on a server this affects nothing',
  'Нажать "Сбросить"': 'Press "Reset"',
  'поправить раскладку': 'fix the layout',
  'Сертификат выпущен вручную (manual plugin), сам он не продлится':
    'The certificate was issued by hand (manual plugin) and will not renew itself',
  'Удалить ненужный сертификат или продлить его вручную': 'Delete the unneeded certificate or renew it by hand',
  'Упавшие юниты systemd': 'Failed systemd units',
  'Systemd: упавшие юниты': 'Systemd: failed units',
  'юнит': 'unit',
  'код {n}': 'exit code {n}',
  'ошибка запуска': 'failed to start',
  'убит сигналом {s}': 'killed by signal {s}',
  'не уложился во время': 'timed out',
  'упал с дампом памяти': 'crashed with a core dump',
  'слишком часто перезапускался': 'restarted too often',
  'не хватило ресурсов': 'ran out of resources',
  'убит из-за нехватки памяти': 'killed by the OOM killer',
  'перестал отвечать watchdog': 'stopped answering the watchdog',
  'упал': 'failed',
  'и': 'and',
  'Перезапуск не поможет: юнит упадет снова.': 'A restart will not help: the unit will fail again.',
  'Перезапуск не поможет: порт занят.': 'A restart will not help: the port is taken.',
  'одна причина': 'same cause',
  'не алертить по этим юнитам:': 'do not alert on these units:',
  'отметка сброшена': 'failed mark cleared',
  '{n} дн назад': '{n} d ago',
  'отработал без ошибок': 'finished without errors',
  'снова упал': 'failed again',
  'Перезапустить {u}?': 'Restart {u}?',
  'Сбросить у {u} отметку "упал"? Сам юнит не запустится, алерт по нему закроется.':
    'Clear the failed mark of {u}? The unit is not started, its alert closes.',
  'Кнопки перезапуска появятся после обновления агента до 2.17.': 'The restart buttons appear once the agent is updated to 2.17.',
  'не алертить по этому юниту:': 'do not alert on this unit:',
  'не алертить': 'do not alert',
  // прирост бэкапа (helper backup-setup 0.32)
  'Прирост бэкапа по прогонам': 'Backup growth per run',
  'сколько каждый прогон добавил в репозиторий, последние {n}, свежий справа': 'what each run added to the repository, last {n}, newest on the right',
  'добавлено {a} (после сжатия {s}), прочитано {p} за {d}': 'added {a} ({s} after compression), read {p} in {d}',
  'Последний прогон': 'Last run',
  'добавил {s} (до сжатия {a}) · прочитано {p} за {d}': 'added {s} ({a} before compression) · read {p} in {d}',
  'Обычно за прогон': 'Usually per run',
  'За прогон клиенты добавляют около {s}: {top}': 'Clients add about {s} per run: {top}',
  'сорт: прирост': 'sort: growth',
  'обычный прирост за прогон после сжатия': 'usual growth per run after compression',
  'Бэкап: резкий прирост': 'Backup: sudden growth',
  // кто ест CPU (агент 2.21)
  'Контейнеры по CPU': 'Containers by CPU',
  'Поды по CPU': 'Pods by CPU',
  'Процессы по CPU': 'Processes by CPU',
  '{n} ядро': '{n} core',
  '{n} ядра': '{n} cores',
  '{n} ядер': '{n} cores',
  'под': 'pod',
  'контейнер': 'container',
  '{n} крутят по ядру вхолостую, старшему {age}': '{n} spin a core idle, the oldest for {age}',
  'CPU: процессы в пустом цикле': 'CPU: processes in an idle loop',
  // ноды кластера без агента панели
  '{name}: нод кластера без агента - {n} ({nodes})': '{name}: cluster nodes without an agent - {n} ({nodes})',
  'Нод без агента: {n}. На них панель не видит диски, SMART, упавшие юниты и процессы - только то, что отдает Kubernetes.':
    'Nodes without an agent: {n}. On them the panel sees no disks, SMART, failed units or processes - only what Kubernetes reports.',
  'без агента': 'no agent',
  'агент не нужен': 'agent not needed',
  'отмечено, что агент здесь не нужен': 'marked as not needing an agent',
  'Добавить в панель': 'Add to the panel',
  'Агент не нужен': 'Agent not needed',
  'Вернуть подсказку': 'Show the hint again',
  // сроки кластера "не нужен, не следить"
  'не нужен, не следить': 'not needed, do not watch',
  'следить снова': 'watch again',
  'не следим': 'not watched',
  'не следим: {n}': 'not watched: {n}',
  'отметил {who}, {when}': 'marked by {who}, {when}',
  'не используется ни одним ingress': 'not used by any ingress',
  'не используется, можно удалить': 'not used, can be deleted',
  'не используется, можно удалить: {n}': 'not used, can be deleted: {n}',
  'удалить из кластера вручную:': 'to delete it from the cluster by hand:',
  // сроки сайтов: напомнить за 7/1 дней или не следить (ExpirySnooze.tsx)
  'напомню за {n} дн.': 'remind {n} d before',
  'не следить отметил {who}, {when}': 'marked as not watched by {who}, {when}',
  'отложил {who}, {when}. Продлят - отложка снимется сама': 'postponed by {who}, {when}. Once renewed, the postponement is lifted by itself',
  'за 7 дн.': '7 d before',
  'за 1 день': '1 day before',
  'напомнить за 7 дней': 'remind 7 days before',
  'напомнить за 1 день': 'remind 1 day before',
  'не следить за доменом': 'do not watch the domain',
  'не следить за сертификатом': 'do not watch the certificate',
  'Пункт уйдет с главной и из алертов навсегда. Вернуть - строкой "не следим" внизу блока или в карточке':
    'The item leaves the home page and alerts for good. Bring it back from the "not watched" line under the block or in the card',
  'Пункт уйдет с главной и из алертов, пока до конца больше {n} дн. Продлят - отложка снимется сама':
    'The item leaves the home page and alerts while more than {n} d are left. Once renewed, the postponement is lifted by itself',
  'напомнить:': 'remind:',
  'не следить': 'do not watch',
  'Пункт вернется на главную и в алерты по обычным порогам': 'The item returns to the home page and alerts at the usual thresholds',
  'отложено: {n}': 'postponed: {n}',
  'Напомнить позже или не следить за доменом': 'Remind later or stop watching the domain',
  'Напомнить позже или не следить за сертификатом': 'Remind later or stop watching the certificate',
  'Нажмите: напомнить позже или не следить': 'Click: remind later or stop watching',
  'Отложить сертификат': 'Postpone certificate',
  'Отложить домен': 'Postpone domain',
  'Сертификат отложен': 'Certificate postponed',
  'Домен отложен': 'Domain postponed',
  'Уберет пункт из проблем и алертов. Обновят секрет - панель снова будет за ним следить':
    'Removes the item from problems and alerts. Once the secret is renewed, the panel watches it again',
  // поды и ноды кластера по metrics-server
  'по metrics-server, как kubectl top': 'from metrics-server, like kubectl top',
  'сорт: память': 'sort: memory',
  'сорт: CPU': 'sort: CPU',
  'память {u} из {c}': 'memory {u} of {c}',
  'CPU {u} из {c}': 'CPU {u} of {c}',
  // поды Kubernetes по памяти
  'Поды по памяти': 'Pods by memory',
  'Перезапустить {c} в {ns}? Kubernetes пересоздаст его поды по очереди.':
    'Restart {c} in {ns}? Kubernetes will recreate its pods one by one.',
  'перезапуск начат': 'restart started',
  'без контроллера': 'no controller',
  // inode и прогноз заполнения
  'Диск: inode': 'Disk: inodes',
  'Диск: скоро заполнится': 'Disk: filling up soon',
  '{n} млн': '{n}M',
  '{n} тыс.': '{n}k',
  'Посчитать файлы не вышло: du на ноде не знает --inodes.': 'Could not count the files: du on the node does not support --inodes.',
  'Каталогов с массой файлов нет.': 'No directories with lots of files.',
  'Где больше всего файлов': 'Where most files are',
  'inode заняты на {p}%: {u} из {n}': 'inodes {p}% used: {u} of {n}',
  'inode кончатся {when}': 'inodes run out {when}',
  'заполнится {when}': 'fills up {when}',
  '(растет на ~{r}% в сутки)': '(growing ~{r}% a day)',
  'меньше чем через час': 'in less than an hour',
  'примерно через {n} ч': 'in about {n} h',
  'примерно через {n} дн': 'in about {n} days',
  // здоровье дисков (helper diskhealth-setup)
  'Здоровье дисков': 'Disk health',
  'Диск: поломка': 'Disk: failure',
  'smartmontools не установлен, SMART не читается: его ставит helper diskhealth-setup.':
    'smartmontools is not installed, so SMART is not read: the diskhealth-setup helper installs it.',
  'переназначенные сектора': 'reallocated sectors',
  'нечитаемые сектора': 'pending sectors',
  'неисправимые ошибки': 'uncorrectable errors',
  'ошибки носителя': 'media errors',
  'ошибки передачи (CRC)': 'transfer errors (CRC)',
  '{n} г.': '{n} y',
  'не определяется': 'not detected',
  'контроллер есть, диска нет': 'controller present, no disk',
  'отключен ядром': 'disabled by the kernel',
  'не отвечает': 'not responding',
  'SMART недоступен': 'SMART unavailable',
  'плановая проверка {n}%': 'scheduled check {n}%',
  'перестройка {n}%': 'reshape {n}%',
  'восстановление {n}%': 'rebuild {n}%',
  'не собран': 'not assembled',
  'работает {a} из {n} дисков': '{a} of {n} disks working',
  'в порядке': 'healthy',
  'сбойный {d}': 'failed {d}',
  'запасной {d}': 'spare {d}',
  'износ {n}%': 'wear {n}%',
  'наработка {v}': 'power-on {v}',
  'переназначено {n}': 'reallocated {n}',
  'нечитаемых {n}': 'pending {n}',
  'неисправимых {n}': 'uncorrectable {n}',
  'ошибок носителя {n}': 'media errors {n}',
  'ошибок CRC {n}': 'CRC errors {n}',
  'за сутки: {v}': 'in the last 24 hours: {v}',
  'пропал': 'vanished',
  'последний раз виден {n} ч назад': 'last seen {n} h ago',
  'последний раз виден {n} мин назад': 'last seen {n} min ago',
  'Ошибки ввода-вывода в журнале ядра за 10 минут: {n}': 'I/O errors in the kernel log over 10 minutes: {n}',
  'Перезапустить контейнер {n}? Несколько секунд он будет недоступен.': 'Restart the container {n}? It will be unavailable for a few seconds.',
  'перезапущен': 'restarted',
  'перезапускаю...': 'restarting...',
  'база: перезапуск только вручную': 'database: restart by hand only',
  'Обнулить': 'Truncate',
  'Выполнить': 'Run',
  'закрыть': 'close',
  'Нода считает, что удалится...': 'The node is working out what would be removed...',
  'Освободится ~{n}': 'About {n} will be freed',
  'Освобождать нечего.': 'Nothing to free.',
  'файлов: {n}': 'files: {n}',
  'Выполняю...': 'Running...',
  'Выполняю, docker может чистить несколько минут...': 'Running, docker may take a few minutes...',
  'Освобождено {n}': 'Freed {n}',
  'цифры обновятся со следующим отчетом': 'the numbers update with the next report',
  'нода не ответила вовремя': 'the node did not answer in time',
  'На этой ноде действие выключено: его нет в /etc/kervax/fix.conf.':
    'The action is switched off on this node: it is not in /etc/kervax/fix.conf.',
  'Кнопки "Освободить" появятся после обновления helper diskusage-setup до 0.3 (ansible kervax_helpers.yml).':
    'The "Free up" buttons appear once the diskusage-setup helper is updated to 0.3 (ansible kervax_helpers.yml).',
  'Кнопки "Освободить" появятся после обновления агента до {v}.':
    'The "Free up" buttons appear once the agent is updated to {v}.',
  'замер {n} мин назад': 'measured {n} min ago',
  'занято {u} из {s}, свободно {a}': '{u} of {s} used, {a} free',
  'Что можно освободить': 'What can be freed',
  'без риска ~{n}': 'safe to free ~{n}',
  'безопасно': 'safe',
  'осторожно': 'careful',
  'вручную': 'manual',
  'Раздел слишком большой для полного обхода ({n} млн файлов), смотрите находки ниже.':
    'The filesystem is too big for a full walk ({n} million files), see the findings below.',
  'Обход не успел целиком, показано то, что посчитано.': 'The walk did not finish, showing what was counted.',
  'Крупных каталогов нет.': 'No large directories.',
  'Журнал systemd': 'systemd journal',
  'Старые ротированные логи: {n} файлов старше 7 дней': 'Old rotated logs: {n} files older than 7 days',
  'Большой лог {p}': 'Large log {p}',
  'Кэш пакетов apt': 'apt package cache',
  'Кэш пакетов dnf/yum': 'dnf/yum package cache',
  'Файлы в /tmp старше 10 дней': 'Files in /tmp older than 10 days',
  'Файлы в /var/tmp старше 30 дней': 'Files in /var/tmp older than 30 days',
  'Дампы памяти упавших программ': 'Core dumps of crashed programs',
  'Отчеты о падениях в /var/crash': 'Crash reports in /var/crash',
  'Кэш restic {p}': 'restic cache {p}',
  'Docker: образы без тега': 'Docker: untagged images',
  'Docker: кэш сборки': 'Docker: build cache',
  'Docker: образы, которые не использует ни один контейнер': 'Docker: images no container uses',
  'Docker: остановленные контейнеры': 'Docker: stopped containers',
  'Docker: тома, не подключенные ни к одному контейнеру': 'Docker: volumes not attached to any container',
  'Лог контейнера {name}': 'Container log {name}',
  'Удаленные файлы, которые еще держат процессы': 'Deleted files still held open by processes',
  'Старые ядра в /boot: {n} шт.': 'Old kernels in /boot: {n}',
  'Останутся последние 200 МБ записей.': 'The latest 200 MB of entries stay.',
  'Содержимое лога пропадет. Чтобы не рос снова, проверьте для него logrotate.':
    'The log content is lost. To keep it from growing again, check logrotate for it.',
  'Обычно это мусор, но некоторые программы держат там рабочие файлы.':
    'Usually junk, but some programs keep working files there.',
  'Пересоздастся при следующем бэкапе, тот пойдет медленнее. Не удаляйте во время бэкапа.':
    'Rebuilt by the next backup, which will be slower. Do not delete it while a backup runs.',
  'Следующая сборка образов пойдет без кэша.': 'The next image build runs without cache.',
  'Скачаются или соберутся заново, когда понадобятся; образы, собранные только локально, придется собирать снова.':
    'They are pulled or built again when needed; images built only locally have to be rebuilt.',
  'Контейнеры удалятся вместе с данными внутри них.': 'The containers are removed together with the data inside them.',
  'В томах могут быть данные: посмотрите список и удаляйте вручную.':
    'Volumes may hold data: review the list and delete by hand.',
  'Содержимое лога пропадет. Чтобы не рос снова, задайте log-opts max-size в /etc/docker/daemon.json.':
    'The log content is lost. To keep it from growing again, set log-opts max-size in /etc/docker/daemon.json.',
  'Место освободится после перезапуска: {p}.': 'The space is freed after restarting: {p}.',
  'apt удалит ядра, которые больше не нужны; текущее останется.':
    'apt removes kernels that are no longer needed; the running one stays.',
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
  'Обновления': 'Updates',
  '…и ещё {n}': '…and {n} more',
  '{name}: бэкап-сервер — включите статистику (backupserver-setup)':
    '{name}: backup server — enable stats (backupserver-setup)',
  '{name}: перезапускается в цикле {list}': '{name}: {list} is in a restart loop',
  '{name}: перезапускаются в цикле {list}': '{name}: in a restart loop: {list}',
  'Все хосты ок': 'All hosts OK',
  'Все кластеры ок': 'All clusters OK',
  'Все свежие': 'All fresh',
  'Хостов': 'Hosts',
  'Контейнеров': 'Containers',
  'Кластеров': 'Clusters',
  'Подов': 'Pods',
  'Клиентов': 'Clients',
  'Серверов': 'Servers',
  '{what}: нод без настройки — {n}': '{what}: {n} node(s) not configured',
  '{name}: Docker без доступа — включите read-only proxy': '{name}: Docker without access — enable the read-only proxy',
  '{name}: не работает контейнер {list}': '{name}: container {list} is down',
  '{name}: не работают контейнеры {list}': '{name}: containers down: {list}',
  '{name}: Kubernetes без доступа — запустите kube-setup': '{name}: Kubernetes without access — run kube-setup',
  '{name}: нод NotReady — {n}': '{name}: nodes NotReady — {n}',
  '{name}: проблемных подов — {n}': '{name}: problem pods — {n}',
  '{name}: бэкап завершился с ошибкой': '{name}: backup failed',
  '{name}: бэкап завершился с ошибкой - {why}': '{name}: backup failed - {why}',
  '{name}: настройка бэкапа не прошла - {why}': '{name}: backup setup failed - {why}',
  '{name}: бэкап не настроен ({traces})': '{name}: no backup configured ({traces})',
  '{name}: бэкап не свежий': '{name}: backup is stale',
  '{name}: rest-server остановлен': '{name}: rest-server is stopped',
  '{name}: репозиториев с проблемой - {n} ({list})': '{name}: repositories with problems - {n} ({list})',
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
  'Покрытие': 'Coverage',
  'На серверах': 'On servers',
  'серверов: {n}': 'servers: {n}',
  'учетки с доступом, в которые давно не входят - проверьте, нужны ли они':
    'accounts with access that nobody has used for a long time - check whether they are needed',
  'слабые SSH-ключи - замените на ed25519': 'weak SSH keys - replace them with ed25519',
  'поддержка ОС кончилась или скоро кончится': 'OS support has ended or ends soon',
  'нужна перезагрузка после обновлений': 'a reboot is needed after updates',
  'обновлен только dbus - перезагрузить при случае': 'only dbus was updated - reboot when convenient',
  'ждут перезагрузки после обновления ядра или библиотек': 'waiting for a reboot after a kernel or library update',
  'обновлены пакеты': 'updated packages',
  // заглушка до нового у всех групп "Требует действий" (homeGroups.ts, ActGroup.tsx)
  'Заглушить до нового: пункт вернется сам': 'Mute until something new: the item comes back by itself',
  'если обновится новое ядро или пакет или сервер перезагрузится и снова попросит перезагрузку':
    'if a new kernel or package is updated or the server reboots and asks for a reboot again',
  'при смене версии ОС или когда сменится срок: кончится обычная поддержка, подключат Ubuntu Pro, подойдет конец ESM':
    'when the OS version changes or the deadline changes: standard support ends, Ubuntu Pro is attached, the end of ESM gets close',
  'если сменится причина: автообновления выключат, удалят или они перестанут запускаться':
    'if the reason changes: automatic updates are turned off, removed or stop running',
  'если после следующего apt update список снова застареет': 'if the list gets stale again after the next apt update',
  'если устареет другой прокси или сменится образ': 'if another proxy gets outdated or the image changes',
  'если появится еще нода без агента': 'if another node without the agent appears',
  'если к замене подойдет еще диск': 'if another disk gets close to replacement',
  'если сменится версия restic': 'if the restic version changes',
  'если появится еще база без дампа': 'if another database without a dump appears',
  'если бэкап пойдет по http на другой адрес': 'if the backup goes over http to another address',
  'если без поддержки окажется еще ПО или версия': 'if more software or another version loses support',
  'если поддержка кончается еще у какого-то ПО или версии': 'if support is ending for more software or another version',
  'если ключи от хоста появятся у другого контейнера или изменятся': 'if another container gets host keys or the keys change',
  'если появятся еще такие контейнеры': 'if more such containers appear',
  'если появятся еще такие юниты': 'if more such units appear',
  'если появятся еще такие учетки': 'if more such accounts appear',
  'если по паролю станут пускать и root': 'if root can also log in with a password',
  'если появится новое': 'if something new appears',
  'Заглушить: пункт вернется, если его поправят и он появится снова, или раньше руками - здесь или в карточке':
    'Mute: the item comes back if it is fixed and appears again, or earlier by hand - here or in the server card',
  'вернуть можно здесь или в карточке': 'you can restore them here or in the server card',
  'заглушить {name}': 'mute {name}',
  'до нового': 'until new',
  'снимется сам, если появится новое или пункт уйдет': 'lifts by itself when something new appears or the item goes away',
  'снимется сам, когда пункт уйдет': 'lifts by itself when the item goes away',
  'конец поддержки заглушен до нового': 'end of support muted until new',
  'обновления безопасности заглушены до нового': 'security updates muted until new',
  'список пакетов заглушен до нового': 'package list muted until new',
  'диски дойдут до 90% износа за полгода - запланируйте замену': 'disks reach 90% wear within six months - plan a replacement',
  'заглушено {n}': 'muted {n}',
  'заглушено {n}, показать': 'muted {n}, show',
  'скрыть заглушенные': 'hide muted',
  'вернуть все': 'restore all',
  'выбрать все': 'select all',
  'заглушить выбранные': 'mute selected',
  'заглушить выбранные ({n})': 'mute selected ({n})',
  'вернутся сами, если появится новое': 'they come back by themselves if something new appears',
  'заглушить до нового': 'mute until new',
  'заглушено до нового': 'muted until new',
  'при случае': 'when convenient',
  'перезагрузка заглушена до нового': 'reboot muted until new',
  'не установлены обновления безопасности': 'security updates are not installed',
  'список пакетов давно не обновлялся': 'the package list has not been refreshed for a long time',
  'SSH пускает по паролю': 'SSH accepts passwords',
  'после перезагрузки не запустятся юниты: работают, но не включены':
    'units will not start after a reboot: running but not enabled',
  'после перезагрузки не поднимутся контейнеры без restart policy':
    'containers without a restart policy will not come back after a reboot',
  'базы без отдельного дампа': 'databases without a separate dump',
  'Docker без доступа - включите read-only proxy': 'Docker without access - enable the read-only proxy',
  'Kubernetes без доступа - запустите kube-setup': 'Kubernetes without access - run kube-setup',
  'устарел образ внешнего прокси - обновите': 'the external proxy image is outdated - update it',
  'ноды кластера без агента': 'cluster nodes without the agent',
  'на сервере бэкапов старый restic - обновите': 'old restic on the backup server - update it',
  'бэкап-сервер без статистики - запустите backupserver-setup': 'backup server without statistics - run backupserver-setup',
  'новые серверы: проверьте, что нашлось при подключении, и закройте подключение в карточке':
    'new servers: check what was found at onboarding and close onboarding in the server card',
  'у контейнеров ключи от хоста - взлом приложения в них даст root на сервере':
    'containers hold keys to the host - breaking into the app gives root on the server',
  'По бэкапам': 'Backups',
  'выключить': 'turn off',
  'Выключить дампы {eng}? Локальные файлы дампов будут удалены; история останется в restic.':
    'Turn off {eng} dumps? Local dump files will be deleted; history stays in restic.',
  'дамп включён — снимается перед каждым бэкапом в /backup/{eng}, хранится {k} последних':
    'dump enabled — taken before every backup into /backup/{eng}, {k} latest kept',
  'ещё не снимался': 'not taken yet',
  // покрытие в две колонки
  'дамп {when}': 'dump {when}',
  'первый снимется ночью, около 03:00': 'first one runs at night, around 03:00',
  'первый снимется в бэкап в {time}': 'first one runs with the backup at {time}',
  'первый снимется в ближайший бэкап': 'first one runs with the next backup',
  'хранить {k}': 'keep {k}',
  'дамп не снимается': 'dump not taken',
  'бэкапятся только настройки (definitions): килобайты при любом объеме очередей': 'only the settings (definitions) are backed up: kilobytes whatever the queues hold',
  'мало': 'small',
  'примерный объем дампа: данные без индексов, индексы в дамп не идут': 'approximate dump size: data without indexes, a dump holds no indexes',
  'примерно столько придется бэкапить, замер раз в сутки': 'roughly this much to back up, measured once a day',
  'дамп без индексов, база {v}': 'dump without indexes, database {v}',
  'крупнейшая {name} {v}': 'largest {name} {v}',
  'сумма по базам, где объем посчитан; у остальных его пока нет': 'sum over the databases with a measured size; the rest are not measured yet',
  'примерно столько придется бэкапить по всем базам под вопросом': 'roughly this much to back up for all questionable databases',
  'от ~{v}': 'from ~{v}',
  'ждет первого бэкапа': 'waiting for the first backup',
  'пропущен: мало места': 'skipped: low disk space',
  'включить дамп': 'enable dump',
  'Настройки дампа: каталог, сколько хранить, порог свободного места': 'Dump settings: directory, how many to keep, free space threshold',
  'Выключить дамп': 'Turn off the dump',
  'Баз на ноде': 'Databases on the node',
  'и приглушено: {n}': 'plus muted: {n}',
  'Бэкапится': 'Backed up',
  'из них ждут первого бэкапа: {n}': 'waiting for the first backup: {n}',
  'дамп или свой бэкап': 'dump or own backup',
  'Под вопросом': 'Questionable',
  'только файловый снапшот': 'file snapshot only',
  'Сломано': 'Broken',
  'дамп не снимается, не в бэкапе': 'dump not taken, not in the backup',
  'нужен дамп: файловый снапшот живой базы может не восстановиться': 'needs a dump: a file snapshot of a live database may not restore',
  'все базы бэкапятся': 'all databases are backed up',
  'дампы снимаются ночью и лежат только на ноде': 'dumps run at night and stay on the node only',
  'дамп снимается перед бэкапом в {time}': 'the dump runs before the backup at {time}',
  'дамп снимается перед каждым бэкапом': 'the dump runs before every backup',
  'поменять время': 'change the time',
  'пока ни одной базы': 'no databases yet',
  'как это работает': 'how it works',
  'Файловый снапшот живой базы может не восстановиться: пока restic копирует файлы, база их меняет. Надежнее дамп.':
    'A file snapshot of a live database may not restore: the database changes its files while restic copies them. A dump is safer.',
  '{n} мин назад': '{n} min ago',
  '{n} ч назад': '{n} h ago',
  '{n} д назад': '{n} d ago',
  'Что на ноде': 'What is on the node',
  'Бэкап на этой ноде помечен как не требующийся, но данные на ней есть':
    'Backup is marked as not required for this node, but it does hold data',
  'Файловый бэкап на ноде не настроен. Что на ней обнаружено':
    'No file-level backup is configured on this node. What was detected',
  'бэкап не требуется': 'backup not required',
  'На ноде ничего не найдено': 'Nothing found on the node',
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
  'Синхронизировать время': 'Sync time',
  'синхронизирую…': 'syncing…',
  'Синхронизировать время сейчас? Часы шагнут к точному времени; при большом расхождении это резкий скачок (для БД/приложений заметно).':
    'Sync time now? The clock will step to the correct time; with a large drift this is a sudden jump (noticeable for DBs/apps).',
  'или вручную': 'or manually',
  'команды для ручной синхронизации': 'manual sync commands',
  'Восстановимость под вопросом ({n}):': 'Restorability in question ({n}):',
  '🛢 базы, которые бэкаплю сам: {n} — показать': '🛢 databases I back up myself: {n} — show',
  'свернуть базы (бэкаплю сам)': 'collapse (I back these up myself)',
  'Сверяется автоматически: точки монтирования, bind-mount ы контейнеров, живые СУБД и тома kubernetes.':
    'Checked automatically: mount points, container bind mounts and live databases. Kubernetes volumes are not checked yet.',
  'Репозиторий пустой и не инициализирован — удалять нечего, кроме мусора. Панель бэкапы не удаляет (это защита от взлома), выполните от root на бэкап-сервере:':
    'The repository is empty and never initialized — nothing to lose but junk. The panel never deletes backups (that is the anti-compromise guarantee); run as root on the backup server:',
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
  'Агенту не хватает прав из systemd-юнита:':
    'The agent lacks permissions from its systemd unit:',
  'Setup-скрипты на ноде устарели:': 'Setup scripts on the node are outdated:',
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
  'дамп включён — снимается раз в сутки по своему таймеру в {dir}, хранится {k} последних':
    'dump on — taken once a day on its own timer into {dir}, keeps {k} latest',
  'Файлового бэкапа на ноде нет: дамп снимается ночью, около 03:00 по времени ноды, и хранится только на ней. Восстановиться с самой ноды можно, копии за её пределами нет.':
    'The node has no file backup: the dump is taken at night, around 03:00 node time, and kept only on the node. It can be restored from the node itself, but there is no copy anywhere else.',
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
  'не отмечено — доступны все': 'nothing checked — all allowed',
  'не отмечено — видны все': 'nothing checked — all visible',
  'Групп пока нет': 'No groups yet',
  'все разделы': 'all sections',
  'все группы': 'all groups',
  'разделов: {n}': 'sections: {n}',
  'групп: {n}': 'groups: {n}',
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
  'Поиск: нода, IP, группа, сервис…': 'Search: node, IP, group, service…',
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
  'бессрочно': 'no expiry',
  'снимется сам после замены диска и сборки массива': 'lifts itself once the disk is replaced and the array is rebuilt',
  'мьют снят сам': 'mute lifted automatically',
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
  'Бэкап-сервер: репозиторий залочен': 'Backup server: repository locked',
  'История алертов': 'Alert history',
  'все': 'all',
  'только срабатывания': 'alerts only',
  'только отбои': 'recoveries only',
  'Поиск по тексту': 'Search the text',
  'Снять фильтр': 'Clear the filter',
  'Срабатываний: {f}, отбоев: {r}': 'Alerts: {f}, recoveries: {r}',
  'За этот период алертов не было.': 'No alerts in this period.',
  'Показать раньше': 'Show earlier',
  'без агента в панели: {n}': 'no agent in the panel: {n}',
  'Клиенты этих репозиториев не подключены к панели. Сломанный бэкап панель видит только по ритму: опоздавший бэкап через интервал и еще четверть его, ежедневный через 30 часов. Агент на ноде сказал бы сразу, как бэкап упал.':
    'The clients of these repositories are not in the panel. The panel sees a broken backup only by the rhythm: a late backup after its interval plus a quarter of it, a daily one after 30 hours. An agent on the node would tell right when the backup failed.',
  'нет в панели': 'not in the panel',
  'раз в сутки': 'daily',
  'раз в неделю': 'weekly',
  'раз в час': 'hourly',
  'раз в {n} ч': 'every {n} h',
  'раз в {n} дн.': 'every {n} days',
  'Бэкап опоздал: обычно {every}, последний {ago}.': 'The backup is late: usually {every}, the last one {ago}.',
  'Нет новых бэкапов {n} дн.': 'No new backups for {n} days.',
  'опоздал': 'late',
  'по времени последних снапшотов': 'from the times of the latest snapshots',
  'следующий к {time}': 'next by {time}',
  'В архив: хранить как есть, без алертов (сервера больше нет, разовый бэкап, проект заморожен)':
    'To the archive: keep as is, without alerts (the server is gone, a one-off backup, a frozen project)',
  'сервера больше нет': 'the server is gone',
  'больше не бэкапится': 'no longer backed up',
  'разовый бэкап': 'one-off backup',
  'проект заморожен': 'project frozen',
  'Группа {host} останется в репозитории как есть и больше не будет считаться встающей ротацией.':
    'The {host} group stays in the repository as is and no longer counts as a stalled rotation.',
  'Репозиторий останется как есть: чистка по политике у него ничего не удаляет, раз новых снапшотов нет. Он уйдет в раздел "Архив" и перестанет быть проблемой, а проверка целостности продолжится.':
    'The repository stays as is: the policy cleanup removes nothing from it while no new snapshots come. It moves to the "Archive" section and stops being a problem, and the integrity check goes on.',
  'Зачем в архив (необязательно)': 'Why archive it (optional)',
  'В архив': 'Archive',
  'в архив': 'archive',
  'группа {host} в архиве': 'group {host} archived',
  'старая группа {host}': 'old group {host}',
  'прежнее имя сервера {host}': 'former server name {host}',
  'пути': 'paths',
  'склеить с {host}': 'merge into {host}',
  'старые снапшоты не вычищаются': 'old snapshots are not being removed',
  'Отдать эти снапшоты новому имени: дальше их чистит общая политика': 'Hand these snapshots over to the new name: from then on the usual policy cleans them',
  'Чистка restic идет по группам (хост и теги) и последние снапшоты каждой группы держит всегда: группа, в которую больше не бэкапятся, сама не уйдет.':
    'restic cleans up group by group (host and tags) and always keeps the last snapshots of each group: a group nothing backs up into anymore never goes away by itself.',
  'На бэкап-сервере. Снапшоты прежнего имени получат имя {to}, и дальше их чистит общая политика: лишние уйдут ночной чисткой, остальные останутся, пока нужны политике. Данные не копируются. Без --apply команда только покажет, что сделает.':
    'On the backup server. The snapshots of the former name get the name {to} and the usual policy cleans them from then on: the extra ones go with the nightly cleanup, the rest stay while the policy needs them. No data is copied. Without --apply the command only shows what it would do.',
  'Склейку helper backupserver-setup умеет с версии {v}, здесь {cur}: обновите helper на этом сервере.':
    'The backupserver-setup helper can merge from version {v}, this server has {cur}: update the helper here.',
  'Хранить группу как есть и не считать её встающей ротацией': 'Keep the group as is and do not count it as a stalled rotation',
  'как удалить': 'how to remove',
  'вернуть': 'restore',
  'На бэкап-сервере. Без --apply команда только покажет, что удалится, с --apply удалит снапшоты группы, место вернет ночная чистка. Группу, в которую бэкапились последнюю неделю, команда удалять откажется.':
    'On the backup server. Without --apply the command only shows what would go, with --apply it removes the snapshots of the group, and the nightly cleanup frees the space. A group that got a backup within the last week is refused.',
  'архив': 'archive',
  'убрал {who}': 'archived by {who}',
  'в архиве с {d}': 'archived since {d}',
  'Вернуть из архива: снова следить за свежестью': 'Restore from the archive: watch freshness again',
  'Архив: {n} ({sz})': 'Archive: {n} ({sz})',
  'хранятся как есть, без алертов; проверка целостности идет': 'kept as is, without alerts; the integrity check goes on',
  'в архиве: {n}': 'archived: {n}',
  'данные {p}/{n}': 'data {p}/{n}',
  'Недельная проверка целостности: структура и индекс, плюс каждую неделю следующая часть данных. За {n} недель прочитывается весь репозиторий.':
    'Weekly integrity check: structure and index, plus the next part of the data every week. The whole repository is read in {n} weeks.',
  'проверка': 'check',
  'проверка не прошла': 'check failed',
  'restic check нашел ошибки, вывод в логе prune-скрипта на бэкап-сервере': 'restic check found errors, the output is in the prune script log on the backup server',
  'недельная проверка целостности (restic check)': 'weekly integrity check (restic check)',
  '{name}: залочен больше суток, чистка не идет - {list}': '{name}: locked for over a day, cleanup is not running - {list}',
  'Клиента этого репозитория нет в панели. Панель следит за ним по ритму бэкапов: опоздавший бэкап видно через интервал и еще четверть его, ежедневный через 30 часов. Агент на самой ноде сказал бы сразу, как бэкап упал.':
    'The client of this repository is not in the panel. The panel watches it by the backup rhythm: a late backup shows after its interval plus a quarter of it, a daily one after 30 hours. An agent on the node itself would tell right when the backup failed.',
  'Бэкап-сервер: ротация встала': 'Backup server: rotation stalled',
  'Бэкап-сервер: чистка не работает': 'Backup server: cleanup is not working',
  'Бэкап-сервер: проверка репозитория не прошла': 'Backup server: repository check failed',
  'Бэкап-сервер: клиент без агента': 'Backup server: client without an agent',
  'DNS на ноде': 'DNS on the node',
  'через systemd-resolved': 'through systemd-resolved',
  'свой резолвер на ноде': 'a resolver of its own on the node',
  'резолверы из resolv.conf': 'resolvers from resolv.conf',
  'имя не из кэша': 'a name not in cache',
  'не резолвится': 'does not resolve',
  'DNS - только предупреждения': 'DNS - warnings only',
  'Юниты в цикле перезапусков': 'Units in a restart loop',
  'Systemd: юниты в цикле перезапусков': 'Systemd: units in a restart loop',
  'Не алертить по {u}? Юнит перестанет алертить и как упавший, и как перезапускающийся.':
    'Stop alerting on {u}? The unit will alert neither as failed nor as restarting.',
  'не алертить по этому юниту': 'do not alert on this unit',
  'CronJob ⚠ ({n})': 'CronJob ⚠ ({n})',
  'CronJob ({n})': 'CronJob ({n})',
  'Задания по расписанию: последний запуск и последний успешный. Упавший или переставший отрабатывать CronJob дает алерт.':
    'Scheduled jobs: the last run and the last successful one. A CronJob that failed or stopped running raises an alert.',
  'запуск {a} назад': 'ran {a} ago',
  'успешный {a} назад': 'succeeded {a} ago',
  'еще не запускался': 'has not run yet',
  'идет': 'running',
  'приостановлен': 'suspended',
  'ок': 'ok',
  // что открыто в интернет
  'Что открыто в интернет': 'Open to the internet',
  'проверено снаружи {ago} назад через {via}': 'checked from outside {ago} ago via {via}',
  'проверено снаружи только что через {via}': 'checked from outside just now via {via}',
  'проверено {ago} назад с адреса самой панели': 'checked {ago} ago from the panel\'s own address',
  'проверено только что с адреса самой панели': 'checked just now from the panel\'s own address',
  'адреса ноды: {ips}': 'node addresses: {ips}',
  'на {ips} ничего не открыто': 'nothing open on {ips}',
  'Панель смотрела со своего адреса, а файрвол ноды часто пускает панель по белому списку. Тогда здесь "открыт" и то, что для остальных закрыто, поэтому по этой проверке нет ни красного, ни алертов. Чтобы видеть, что открыто для всех, добавьте в "Локациях" прокси в другой сети.':
    'The panel checked from its own address, and node firewalls often allow the panel by allowlist. Then "open" here includes ports closed to everyone else, so this check shows no red and sends no alerts. To see what is open to everyone, add a proxy in another network under "Locations".',
  'Красным - опасное: базы, очереди, etcd, kubelet, Docker API, node_exporter. Желтым - служебное: порты Kubernetes и сети подов, агенты и экспортеры метрик. Снаружи должно быть закрыто и то и другое, а если порт открыт специально, отметьте "открыт намеренно". Остальное (SSH, веб, ваши сервисы) - для сведения.':
    'Red is risky: databases, queues, etcd, kubelet, Docker API, node_exporter. Yellow is internal: Kubernetes and pod network ports, agents and metrics exporters. Both should be closed from outside, and if a port is open on purpose, mark it "open on purpose". The rest (SSH, web, your services) is for reference.',
  'не проверить ({n}):': 'could not check ({n}):',
  'Что слушает': 'Listening',
  'Снаружи': 'From outside',
  'процесс не известен': 'process unknown',
  'панель': 'panel',
  'Внешней проверки еще не было: панель проверяет раз в 6 часов.': 'No outside check yet: the panel checks every 6 hours.',
  'проверить сейчас': 'check now',
  'проверяю...': 'checking...',
  'не вышло': 'failed',
  'Публичного адреса у ноды не нашлось: проверять нечего.': 'The node has no public address: nothing to check.',
  'Проверить не вышло: {via} не отвечает или не пропускает соединения на эти порты.': 'Check failed: {via} does not answer or does not allow connections to these ports.',
  'открыт': 'open',
  'закрыт': 'closed',
  'за файрволом': 'firewalled',
  'не проверить': 'unknown',
  'новый': 'new',
  'намеренно': 'intended',
  'вернуть алерт': 'alert again',
  'открыт намеренно': 'open on purpose',
  'Порт {p} открыт намеренно? Панель перестанет алертить по нему.': 'Is port {p} open on purpose? The panel will stop alerting about it.',
  'Закройте порт файрволом или привяжите сервис к 127.0.0.1 либо к внутреннему адресу. Порт Docker, опубликованный через -p, обходит ufw: публикуйте его как 127.0.0.1:порт:порт.':
    'Close the port with a firewall or bind the service to 127.0.0.1 or an internal address. A Docker port published with -p bypasses ufw: publish it as 127.0.0.1:port:port.',
  'открытый порт': 'open port',
  'Сеть: открыто в интернет то, что должно быть закрыто': 'Network: open to the internet but should be closed',
  'Сеть: открылся новый порт': 'Network: new port opened',
  'Сеть: белые списки обходятся подменой CF-Connecting-IP': 'Network: allow lists are bypassed with a forged CF-Connecting-IP',
  'доверие к CF-Connecting-IP': 'trust in CF-Connecting-IP',
  // сервер задыхается
  'Давление на ресурсы': 'Resource pressure',
  'Сервер бэкапов: диск грузят prune и check репозиториев и прием бэкапов, это его обычная работа, алерт не шлем. Придет, если так продержится дольше 6 ч или процесс зависнет в ожидании диска.': 'Backup server: the disk is busy with repository prune and check and incoming backups, which is its normal work, so no alert is sent. It will come if this lasts longer than 6 h or a process hangs waiting for the disk.',
  'ждут (some)': 'waiting (some)',
  'все стоят (full)': 'all stalled (full)',
  'доля времени, когда процессы ждали ресурс: за 10 секунд / за минуту': 'share of time processes waited for the resource: 10 seconds / 1 minute',
  'В D-state (ждут диск или сеть): {n}': 'In D-state (waiting for disk or network): {n}',
  'Файловые дескрипторы: {u} из {m} ({p}%)': 'File descriptors: {u} of {m} ({p}%)',
  'Сервер задыхается': 'Server is choking',
  // сеть ноды
  'Здоровье сети': 'Network health',
  'TCP повторно': 'TCP retransmits',
  'Подключения без ответа': 'Unanswered connections',
  '(повторы SYN: торрент-клиент за VPN, мертвый адрес или сканер; в потери не входят)':
    '(SYN retries: a torrent client behind a VPN, a dead address or a scanner; not counted as loss)',
  'сегм/с': 'segm/s',
  'из': 'of',
  'Очередь accept переполнена': 'Accept queue overflows',
  'conntrack выбросил': 'Dropped by conntrack',
  'Входящие с ошибкой': 'Incoming with errors',
  'в сек': 'per sec',
  'Сеть: потери и ошибки': 'Network: loss and errors',
  // связь между серверами и трафик за месяц (NetLinks.tsx)
  'Сеть: связь между серверами': 'Network: links between servers',
  'Сеть: лимит трафика тарифа': 'Network: plan traffic limit',
  'Связь с другими серверами': 'Links to other servers',
  'Агент будет раз в 30 секунд открывать TCP-соединение с адресом и мерить время: вход VPN с выходом, приложение с базой. Алерт, если связь пропала или теряет попытки.':
    'Every 30 seconds the agent opens a TCP connection to the address and times it: a VPN entry to its exit, an app to its database. Alert when the link is gone or loses attempts.',
  'Связь проверяет агент 2.31 и новее: обновите агент.': 'Links are checked by agent 2.31 and newer: update the agent.',
  'Добавить связь': 'Add link',
  'другой адрес': 'other address',
  'что это (необязательно)': 'what it is (optional)',
  'Убрать': 'Remove',
  'Убрать связь с {name}? История ее проверок удалится.': 'Remove the link to {name}? Its check history will be deleted.',
  'ждем первую проверку': 'waiting for the first check',
  'сервер сам молчит': 'the server itself is silent',
  'неудач {p}%': '{p}% failed',
  'Время подключения': 'Connect time',
  'медиана': 'median',
  'худшее': 'worst',
  'Неудачные попытки, %': 'Failed attempts, %',
  'неудачи': 'failed',
  'Трафик за месяц': 'Monthly traffic',
  'Трафик считает агент 2.31 и новее: обновите агент.': 'Traffic is counted by agent 2.31 and newer: update the agent.',
  'Исходящий': 'Outgoing',
  'Входящий': 'Incoming',
  'исходящий': 'outgoing',
  'входящий': 'incoming',
  'весь трафик': 'all traffic',
  'Лимит тарифа': 'Plan limit',
  'К концу периода': 'By the end of the period',
  'прогноз появится через трое суток данных': 'the forecast appears after three days of data',
  'Считаем с {d}: раньше агент счетчики не отдавал, начало периода не вошло.':
    'Counting since {d}: the agent did not report counters before, the start of the period is missing.',
  'Прошлые периоды': 'Past periods',
  'Считается по сетевым картам: {list}. Туннели, мосты docker и veth не входят, иначе один пакет считался бы два-три раза.':
    'Counted on network cards: {list}. Tunnels, docker bridges and veth are left out, otherwise one packet would be counted two or three times.',
  'Агент не нашел у сервера настоящих сетевых карт.': 'The agent found no real network cards on the server.',
  'Задать лимит тарифа': 'Set plan limit',
  'Изменить лимит тарифа': 'Change plan limit',
  'лимит': 'limit',
  'хостер считает исходящий': 'hoster counts outgoing',
  'хостер считает весь трафик': 'hoster counts all traffic',
  'период с': 'period from day',
  'числа': 'of the month',
  'Пустой или 0 - без лимита: только данные, без алертов.': 'Empty or 0 means no limit: data only, no alerts.',
  'теряет {p}%': 'loses {p}%',
  'График за период': 'Chart for a period',
  'По дням': 'By day',
  'за период': 'this period',
  'только данные, без алертов': 'data only, no alerts',
  'при нынешнем темпе': 'at the current rate',
  '{u} из {l}, считается {what}': '{u} of {l}, counting {what}',
  'повторы SYN, в потери не входят': 'SYN retries, not counted as loss',
  'Повторы уходят': 'Retransmits go to',
  'Почти все к одному адресу: это канал до него, а не сеть сервера, поэтому без алерта.':
    'Almost all go to one address: that is the path to it, not the server network, so no alert.',
  // kubernetes: ноды, Job, гигиена подов
  'без лимита памяти': 'no memory limit',
  'без проб': 'no probes',
  'У контейнера нет лимита памяти: один такой под может съесть всю ноду.': 'A container has no memory limit: one such pod can eat the whole node.',
  'Нет ни liveness, ни readiness пробы: зависший под так и будет считаться работающим.': 'No liveness or readiness probe: a hung pod will keep counting as running.',
  'Упавшие за неделю Job, запущенные не по расписанию (миграции, разовые задачи): {n}': 'Jobs failed this week that were not started by a schedule (migrations, one-off tasks): {n}',
  'упал {a} назад': 'failed {a} ago',
  'Без лимита памяти: {a}, без проб: {b}. Не авария, но под без лимита может съесть ноду, а без проб зависший под никто не перезапустит.':
    'No memory limit: {a}, no probes: {b}. Not an outage, but a pod without a limit can eat the node, and nobody restarts a hung pod without probes.',
  'Kubernetes: ноды': 'Kubernetes: nodes',
  'Kubernetes: упавшие Job': 'Kubernetes: failed Jobs',
  // обновления ОС
  'Обновления ОС': 'OS updates',
  'включены': 'on',
  'выключены': 'off',
  'не установлены': 'not installed',
  'ядро {k}': 'kernel {k}',
  'обновлений {n}, из них безопасности {m}': '{n} updates, {m} security',
  'автообновления: {s}': 'auto-updates: {s}',
  'apt update: {d}': 'apt update: {d}',
  'нужна перезагрузка с {d}': 'reboot required since {d}',
  // изменения на сервере
  'Изменения на сервере': 'Server changes',
  'SSH-ключей: {k}, пользователей с shell: {u}. Снимок раз в 10 минут, без содержимого ключей и кронов.':
    'SSH keys: {k}, users with a shell: {u}. A snapshot every 10 minutes, without key or crontab contents.',
  'Изменился доступ: проверьте, что это свои.': 'Access changed: check that it was your people.',
  'это мы, все в порядке': 'that was us, all good',
  'Изменений пока не было.': 'No changes yet.',
  'все изменения: {n}': 'all changes: {n}',
  'Сервер: изменился доступ': 'Server: access changed',
  // проверка восстановления бэкапа
  'Восстановление': 'Restore',
  'проверено {d}: файлов совпало {n}{dumps}': 'checked {d}: {n} files matched{dumps}',
  ', дампы читаются: {e}': ', dumps readable: {e}',
  'Бэкап: проверка восстановления': 'Backup: restore check',
  // не переживет перезагрузку, доступы
  '{name}: после перезагрузки не запустятся {list}: работают, но не включены':
    '{name}: {list} will not start after a reboot: running but not enabled',
  '{name}: после перезагрузки не поднимутся контейнеры без restart policy: {list}':
    '{name}: containers without a restart policy will not come back after a reboot: {list}',
  '{name}: слабые SSH-ключи у {list} - замените на ed25519': '{name}: weak SSH keys for {list} - replace them with ed25519',
  '{name}: учетки с доступом, в которые давно не входят: {list} - проверьте, нужны ли они':
    '{name}: accounts with access that nobody has used for a long time: {list} - check whether they are still needed',
  'как отключить': 'how to disable',
  // SSH: как выключить вход по паролю, решение "вход по паролю нужен"
  'вход по паролю нужен': 'password login needed',
  'Доступы': 'Access',
  'Доступ: второй root или пустой пароль': 'Access: a second root or an empty password',
  'Доступы: заброшенные учетки и слабые ключи': 'Access: unused accounts and weak keys',
  'Не запустятся после перезагрузки': 'Will not start after a reboot',
  'Не нужны после перезагрузки: {list}': 'Not needed after a reboot: {list}',
  'Не переживет перезагрузку': 'Will not survive a reboot',
  'Работают, но не включены: их запустили systemctl start без enable, и после ребута их не будет.':
    'Running but not enabled: they were started with systemctl start without enable and will be gone after a reboot.',
  'Так задумано: не напоминать про этот контейнер': 'This is intended: do not remind about this container',
  'без пароля': 'no password',
  'заблокирован': 'locked',
  'задан': 'set',
  'и еще {n}': 'and {n} more',
  'ключей': 'keys',
  'не было': 'never',
  'не нужен после перезагрузки': 'not needed after a reboot',
  'нужна': 'needed',
  'после перезагрузки не поднимутся: нет restart policy': 'will not come back after a reboot: no restart policy',
  'последний вход': 'last login',
  'пустой': 'empty',
  'работает с {d}': 'running since {d}',
  'слабых: {n}': 'weak: {n}',
  'учетка': 'account',
  'учетка нужна': 'account needed',
  // решения по обновлениям ОС
  'обновить ОС нельзя': 'cannot upgrade the OS',
  'перезагружать нельзя': 'cannot reboot',
  'обновлять нельзя': 'cannot update',
  'все так и оставить': 'leave all as is',
  'как исправить': 'how to fix',
  'или': 'or',
  'Обновить список пакетов: ничего не ставит, только узнает, что вышло.': 'Refresh the package lists: installs nothing, only learns what is out.',
  'Чтобы список обновлялся сам каждый день (без автоустановки):': 'To refresh the lists by itself every day (without installing anything):',
  'Включить их: каждый день свежий список пакетов и исправления безопасности.': 'Turn them on: fresh package lists and security fixes every day.',
  'Поставить исправления безопасности сейчас, не дожидаясь ночного прогона. Лог - в /var/log/unattended-upgrades/.':
    'Install the security fixes now, without waiting for the nightly run. The log is in /var/log/unattended-upgrades/.',
  'Автообновления включены, но не запускались: проверить таймеры apt и последний лог.': 'Automatic updates are on but have not run: check the apt timers and the last log.',
  'Сначала: эти контейнеры без restart policy, после перезагрузки docker их не поднимет.': 'First: these containers have no restart policy, docker will not start them after a reboot.',
  'Сначала: эти сервисы работают, но не включены, после перезагрузки их не будет.': 'First: these services run but are not enabled, they will be gone after a reboot.',
  'Ubuntu Pro: исправления безопасности до {date}. Токен - на ubuntu.com/pro, для личного использования бесплатно до 5 машин, для компании нужна подписка. Подключение панель видит с агента 2.39.':
    'Ubuntu Pro: security fixes until {date}. The token is at ubuntu.com/pro, free for personal use on up to 5 machines, a company needs a subscription. The panel sees it from agent 2.39.',
  'Поставить исправления из ESM.': 'Install the fixes from ESM.',
  // контейнеры с ключами от хоста
  '{name}: у контейнеров ключи от хоста: {list} - взлом приложения в них даст root на сервере':
    '{name}: containers hold the keys to the host: {list} - breaking into the app inside gives root on the server',
  'docker-сокет': 'docker socket',
  'корень хоста на запись': 'host root writable',
  'Docker: контейнер с ключами от хоста': 'Docker: container with the keys to the host',
  'ключи от хоста по назначению': 'host access by design',
  'У контейнеров ключи от хоста: взлом приложения в них даст root на сервере.':
    'These containers hold the keys to the host: breaking into the app inside gives root on the server.',
  'у контейнеров ключи от хоста: взлом приложения в них даст root на сервере':
    'containers hold the keys to the host: breaking into the app inside gives root on the server',
  'Так задумано: убрать из "Требует действий" и не алертить по этому контейнеру':
    'This is intended: drop it from "Action needed" and do not alert on this container',
  'по назначению': 'by design',
  'По назначению: {list}': 'By design: {list}',
  'вернуть {name}': 'bring back {name}',
  // как исправить ключи от хоста (docker_risk.fixes)
  'Как исправить: {names}': 'How to fix: {names}',
  'docker-сокета в -v': 'the docker socket in -v',
  '{paths} на запись в -v': '{paths} writable in -v',
  // пропавший порт, диск тормозит
  'Сервер: пропал порт': 'Server: port gone',
  'пропавший порт': 'gone port',
  'Диск тормозит': 'Disk is slow',
  // сертификаты nginx
  'Веб-сервер: сертификат истекает': 'Web server: certificate expires',
  'сертификат {l} истек {n} дн назад ({f})': 'certificate {l} expired {n} days ago ({f})',
  'сертификат {l} истекает через {n} дн ({f})': 'certificate {l} expires in {n} days ({f})',
  // проверка конфигов веб-серверов
  'Веб-сервер: конфиг не проходит проверку': 'Web server: config fails the test',
  'конфиг в контейнере {c} не проходит проверку: {e}': 'the config in container {c} fails the test: {e}',
  'конфиг не проходит проверку: {e}': 'the config fails the test: {e}',
  'Следующий reload или перезапуск уронит сайты.': 'The next reload or restart will take the sites down.',
  // здоровье баз
  'СУБД: реплика, транзакции, запись, VACUUM': 'DB: replica, transactions, writes, VACUUM',
  'реплика, отстает на {l}': 'replica, {l} behind',
  'реплика, догнала': 'replica, caught up',
  'основная, реплик: {n}': 'primary, replicas: {n}',
  // что грузит базу
  'Что грузит базу': 'What loads the database',
  // ClickHouse и Redis (dbstat-setup 0.10)
  '{ms} мс': '{ms} ms',
  'кусков в партиции до {n}': 'up to {n} parts in a partition',
  'вставок за {m} мин: {n}': 'inserts in {m} min: {n}',
  'отклонено вставок: {n}': 'inserts rejected: {n}',
  'замедлено вставок: {n}': 'inserts delayed: {n}',
  'слияний сейчас: {n}': 'merges running: {n}',
  'мутаций в работе: {n}': 'mutations running: {n}',
  'реплицируемых таблиц: {n}': 'replicated tables: {n}',
  'в очереди репликации {n}': '{n} in the replication queue',
  'отцепленных кусков: {n}': 'detached parts: {n}',
  'запросов с ошибкой за час: {n}': 'failed queries in an hour: {n}',
  'самые долгие запросы заняли {d} за час': 'the longest queries took {d} in an hour',
  'query_log выключен: запросы не видны': 'query_log is off: queries are not visible',
  'запросы не вместились в отчет': 'the queries did not fit into the report',
  'всего {d}': '{d} in total',
  '{d} в среднем': '{d} on average',
  'прочитано {s}': 'read {s}',
  'память до {s}': 'memory up to {s}',
  'память {u} из {m}': 'memory {u} of {m}',
  'память {u}, без лимита': 'memory {u}, no limit',
  'снимок на диск не сохраняется': 'the snapshot is not saved to disk',
  'снимок {d} назад': 'snapshot {d} ago',
  'изменений после него: {n}': '{n} changes since',
  'AOF не пишется': 'AOF is not written',
  'вытеснено ключей за {m} мин: {n}': 'keys evicted in {m} min: {n}',
  'отклонено подключений: {n}': 'connections rejected: {n}',
  'тяжелее всех за {m} мин: {list}': 'heaviest in {m} min: {list}',
  '{c} {d} на {n} вызовов': '{c} {d} over {n} calls',
  'ClickHouse: куски, вставки, запросы': 'ClickHouse: parts, inserts, queries',
  'Redis: память, снимки, команды': 'Redis: memory, snapshots, commands',
  'на хосте': 'on the host',
  'скрыть запросы': 'hide queries',
  'запросы': 'queries',
  // кто пишет на диск
  'Кто пишет на диск': 'Who writes to disk',
  'сессии и cron': 'sessions and cron',
  'чтение {s} в час': 'reads {s} an hour',
  'запись {h} в час': 'writes {h} an hour',
  'около {d} в сутки': 'about {d} a day',
  'cgroup v1: в эти цифры попадает только прямая запись, отложенная через кэш страниц не видна':
    'cgroup v1: only direct writes get into these numbers, buffered writes through the page cache are not visible',
  'за последний час': 'over the last hour',
  'за {n} мин, в пересчете на час': 'over {n} min, per hour',
  'в час, за сутки и за неделю': 'per hour, per day and per week',
  'сейчас не пишет': 'not writing now',
  'за сутки {d}, за неделю {w}': '{d} a day, {w} a week',
  'История по дням копится с {d}.': 'The daily history is collected since {d}.',
  // кто качает трафик
  'Кто качает трафик': 'Who uses the traffic',
  // версии ПО
  // MySQL/MariaDB: нагрузка и советы (dbstat-setup 0.11)
  'innodb_buffer_pool_size {bp} при {mem} памяти{lim}, а данных {data}: InnoDB читает с диска {rd} в час. Если машина только под базу, обычно ставят 50-70% памяти. Меняется без перезапуска (SET GLOBAL innodb_buffer_pool_size), значение надо записать и в конфиг': 'innodb_buffer_pool_size {bp} with {mem} of memory{lim}, and {data} of data: InnoDB reads {rd} an hour from disk. On a machine dedicated to the database it is usually 50-70% of memory. It changes without a restart (SET GLOBAL innodb_buffer_pool_size), put the value into the config as well',
  'innodb_buffer_pool_size {bp}, а это больше 80% памяти ({mem}{lim}): подключениям, временным таблицам и самому серверу остается мало, на пике процесс убьет OOM': 'innodb_buffer_pool_size {bp}, which is over 80% of memory ({mem}{lim}): little is left for connections, temporary tables and the server itself, at a peak the OOM killer will take the process',
  'Redo пишется {redo} в час, а журнал {cur} ({v}): он переписывается по кругу чаще раза в полчаса, и InnoDB приходится сбрасывать страницы на диск раньше времени. Обычно журнала хватает на час записи, примерно {rec}': 'Redo is written at {redo} an hour and the log is {cur} ({v}): it wraps around more often than every half hour, and InnoDB has to flush pages to disk early. The log usually holds an hour of writes, about {rec}',
  'меняется без перезапуска': 'changes without a restart',
  'нужен перезапуск': 'needs a restart',
  'без перезапуска': 'no restart',
  'Запись ждала свободного места в буфере redo {n} раз за час: innodb_log_buffer_size {cur} мал, обычно ставят 64 МБ': 'Writes waited for room in the redo buffer {n} times an hour: innodb_log_buffer_size {cur} is small, 64 MB is usual',
  'Временных таблиц на диске {n} в час из {all}: они не помещаются в память, а работает меньший из лимитов tmp_table_size {tts} и max_heap_table_size {mhts}. Поднимать осторожно, лимит действует на каждую таблицу каждого запроса. Обычно дешевле поправить запросы с временными таблицами на диске': '{n} temporary tables an hour go to disk out of {all}: they do not fit in memory, and the smaller of tmp_table_size {tts} and max_heap_table_size {mhts} applies. Raise it with care, the limit applies to every table of every query. Fixing the queries with on-disk temporary tables is usually cheaper',
  'Бинлоги занимают {b} и не удаляются: binlog_expire_logs_seconds и expire_logs_days равны 0. Поставьте срок, например binlog_expire_logs_seconds = 604800 (7 дней), перезапуск не нужен. Реплика, если она есть, должна успевать забирать их за этот срок': 'Binary logs take {b} and are never removed: binlog_expire_logs_seconds and expire_logs_days are 0. Set a term, for example binlog_expire_logs_seconds = 604800 (7 days), no restart needed. A replica, if there is one, must fetch them within that term',
  'Docker при остановке ждет MySQL {to} с, потом убивает: с буфером {bp} InnoDB может не успеть сбросить страницы, и после docker stop или перезагрузки сервера база восстанавливается по redo как после аварии. В compose stop_grace_period: 5m (контейнер пересоздается), для ручной остановки docker stop -t 600': 'On stop Docker waits {to} s for MySQL, then kills it: with a {bp} buffer InnoDB may not manage to flush its pages, and after docker stop or a server reboot the database recovers from redo as after a crash. In compose stop_grace_period: 5m (the container is recreated), for a manual stop docker stop -t 600',
  '{n} запросов в секунду': '{n} queries a second',
  'изменений {n} в секунду': '{n} writes a second',
  'изменений {n} в час': '{n} writes an hour',
  'медленных запросов за час: {n}': 'slow queries in an hour: {n}',
  'чтение с диска мимо буфера InnoDB {s} в час': 'disk reads past the InnoDB buffer {s} an hour',
  'попаданий в буфер {p}%': 'buffer hits {p}%',
  'redo {s} в час': 'redo {s} an hour',
  'временных таблиц на диске {n} в час из {m}': 'on-disk temporary tables {n} an hour out of {m}',
  'ожиданий блокировок строк {n} в час, всего {d}': 'row lock waits {n} an hour, {d} in total',
  'дедлоков за час: {n}': 'deadlocks in an hour: {n}',
  'неудачных подключений {n} в час: неверный пароль или обрыв при входе': 'failed connections {n} an hour: a wrong password or a dropped login',
  'Самые долгие запросы покажет performance_schema: в MariaDB он по умолчанию выключен. Включается строкой performance_schema = ON в разделе [mysqld], нужен перезапуск': 'performance_schema will show the slowest queries: MariaDB keeps it off by default. It is turned on with performance_schema = ON in the [mysqld] section, a restart is needed',
  'Настройки MySQL': 'MySQL settings',
  'просматривает {n} строк за вызов': 'scans {n} rows a call',
  'без индекса': 'no index',
  'временных таблиц на диске {n}': 'on-disk temporary tables {n}',
  'MySQL в подах helper видит с версии 0.11: обновите dbstat-setup': 'the helper sees MySQL in pods since 0.11: update dbstat-setup',
  'под на другой ноде (helper видит поды только своей ноды) или база не ответила': 'the pod is on another node (the helper sees only the pods of its own node) or the database did not answer',
  'база не ответила: helper входит как root через сокет, /etc/mysql/debian.cnf или /root/.my.cnf': 'the database did not answer: the helper logs in as root over the socket, through /etc/mysql/debian.cnf or /root/.my.cnf',
  // GPU NVIDIA и LLM-сервисы (Gpu.tsx)
  '/metrics не отвечает: {e}': '/metrics does not answer: {e}',
  'E2E, весь запрос': 'E2E, whole request',
  'KV-кэш': 'KV cache',
  'LLM-сервисы': 'LLM services',
  'TPOT, на токен': 'TPOT, per token',
  'TTFT, до первого токена': 'TTFT, to the first token',
  'Xid за сутки: {n}': 'Xid in a day: {n}',
  'helper gpu-setup, раз в минуту': 'helper gpu-setup, every minute',
  'nvidia-smi нет: драйвер NVIDIA не установлен, карты на шине не используются ({list}).': 'no nvidia-smi: the NVIDIA driver is not installed, the cards on the bus are unused ({list}).',
  'p95 за час': 'p95 over an hour',
  '{n} ток/с': '{n} tok/s',
  'Вт': 'W',
  'ГБ': 'GB',
  'Генерация': 'Generation',
  'Запросов за {n} мин не было: задержки не посчитать.': 'No requests in {n} min: no latencies to compute.',
  'Задержки выше обычного за сутки': 'Latency above usual in the last 24 h',
  'p95 до первого токена до {v}, обычно {b}': 'p95 time to first token up to {v}, usually {b}',
  'p95 на токен до {v}, обычно {b}': 'p95 per token up to {v}, usually {b}',
  'запросов за 5 мин {n}': '{n} requests per 5 min',
  'в очереди до {n}': 'up to {n} queued',
  'очереди не было': 'no queue',
  'держалось под нагрузкой': 'held under load',
  'Запросы': 'Requests',
  'МГц': 'MHz',
  'Метрики выключены: SGLang надо запускать с --enable-metrics, тогда здесь будут очередь, KV-кэш и задержки.': 'Metrics are off: run SGLang with --enable-metrics to see the queue, KV cache and latencies here.',
  'На шине PCI карт NVIDIA: {n} ({list}). Загрузку, память, процессы и ошибки драйвера смотрит helper gpu-setup, на сервере его нет: он есть в "Требует действий".': 'NVIDIA cards on the PCI bus: {n} ({list}). Load, memory, processes and driver errors come from the gpu-setup helper, which is not on the server: it is listed in "Action needed".',
  'Отданы в виртуальные машины (vfio-pci): {list}': 'Given to virtual machines (vfio-pci): {list}',
  'Очередь': 'Queue',
  'Ошибки драйвера (Xid) за сутки': 'Driver errors (Xid) in a day',
  // очереди, которые растут и не разбираются (ServicesPage, mutes)
  'Очередь растет и не разбирается': 'Queue grows and is not consumed',
  'растет {a} и не разбирается, было {n}': 'growing for {a} and not consumed, was {n}',
  'нет потребителей {a}, было {n}': 'no consumers for {a}, was {n}',
  '{a} не отдает ни одного, было {n}': 'not a single message taken for {a}, was {n}',
  'нет потребителей': 'no consumers',
  'порог': 'threshold',
  'не алертить по этой очереди:': 'no alerts for this queue:',
  'Не алертить по этой очереди: на день, неделю или постоянно': 'No alerts for this queue: for a day, a week or for good',
  'Все очереди': 'All queues',
  'С проблемами: {n}': 'With problems: {n}',
  'Очереди в этих Redis не видны: helper не нашел пароль ({list}). Он берет REDIS_PASSWORD или --requirepass из настроек контейнера либо requirepass из redis.conf.':
    'Queues in these Redis instances are not visible: the helper did not find the password ({list}). It takes REDIS_PASSWORD or --requirepass from the container settings or requirepass from redis.conf.',
  'Таблицы очередей в этих базах не проверены: helper не смог войти ({list}). PostgreSQL он открывает через сокет под пользователем сервера или по POSTGRES_PASSWORD из настроек контейнера, MySQL - через debian.cnf или по MYSQL_ROOT_PASSWORD.':
    'Queue tables in these databases were not checked: the helper could not get in ({list}). It opens PostgreSQL over the socket as the server\'s own user or with POSTGRES_PASSWORD from the container settings, MySQL with debian.cnf or MYSQL_ROOT_PASSWORD.',
  'Не считаются каждую минуту: в таблице очередей больше миллиона строк, выполненные задачи из нее не удаляются ({list}).':
    'Not counted every minute: the queue table has more than a million rows, finished jobs are not deleted from it ({list}).',
  '{w}: {n} строк': '{w}: {n} rows',
  'Задач нет: все разобраны.': 'No jobs: all are done.',
  'стоит: самая старая задача ждет {a}': 'stands still: the oldest job waits {a}',
  'самая старая ждет {a}': 'the oldest waits {a}',
  'упало за час: {n}': 'failed in an hour: {n}',
  'за сутки: {n}': 'in a day: {n}',
  'за {p}: {n}': 'in {p}: {n}',
  'всплеск, обычно около {n} в час': 'a spike, usually about {n} an hour',
  'всплеск': 'a spike',
  'чаще всего: {list}': 'most often: {list}',
  '{job} в очереди {q}': '{job} in queue {q}',
  'очередь {q}': 'queue {q}',
  'не алертить о всплеске упавших:': 'no alerts on a spike of failed jobs:',
  'Не алертить о всплеске упавших задач этого брокера: на день, неделю или постоянно': 'No alerts on a spike of failed jobs of this broker: for a day, a week or for good',
  'упавшие задачи': 'failed jobs',
  'Всплеск упавших задач': 'A spike of failed jobs',
  'завершился': 'exited',
  'не запускается': 'does not start',
  'неизвестно': 'unknown',
  'останавливается': 'stopping',
  'не может запуститься': 'cannot start',
  'воркер': 'worker',
  'Воркеры': 'Workers',
  'работает {a}': 'up {a}',
  'в беде: {n}': 'in trouble: {n}',
  'работают: {n}': 'running: {n}',
  'программ: {n}': 'programs: {n}',
  'остановлены руками: {n}': 'stopped by hand: {n}',
  'падений за час: {n}': 'crashes in an hour: {n}',
  'плановых перезапусков за час: {n}': 'planned restarts in an hour: {n}',
  'не алертить по этому воркеру:': 'no alerts for this worker:',
  'Не алертить по этому воркеру: на день, неделю или постоянно': 'No alerts for this worker: for a day, a week or for good',
  '{d} дн {h} ч': '{d} d {h} h',
  'Все воркеры': 'All workers',
  'Не видны программы этих демонов: {list}.': 'Programs of these daemons are not visible: {list}.',
  'Воркер упал или падает и поднимается': 'A worker is down or keeps crashing',
  'Крон наслаивается или висит': 'A cron job piles up or hangs',
  'Всплеск ошибок в логах': 'A spike of errors in logs',
  'PHP-FPM не справляется': 'PHP-FPM cannot keep up',
  'пул PHP-FPM': 'PHP-FPM pool',
  'Все пулы': 'All pools',
  'пулов: {n}': 'pools: {n}',
  'воркеров {n} из {m}': '{n} of {m} workers',
  'соединений сейчас: {n}': 'connections now: {n}',
  'в очереди: {n}': 'waiting: {n}',
  'запросы ждали {n} мин за час': 'requests waited {n} min in an hour',
  'за час {n}, за сутки {m}': '{n} in an hour, {m} in a day',
  'упоры в pm.max_children': 'hit pm.max_children',
  'убито по таймауту': 'killed on timeout',
  'медленных': 'slow',
  'от {n} с': 'from {n} s',
  'pm.max_children = 5 - значение по умолчанию: пул никто не настраивал': 'pm.max_children = 5 is the default: nobody has tuned the pool',
  'не алертить об этом пуле:': 'no alerts on this pool:',
  'Не алертить о пуле PHP-FPM: на день, неделю или постоянно': 'No alerts on this PHP-FPM pool: for a day, a week or for good',
  'Конфиг не найден: {w}': 'Config not found: {w}',
  'Пулы PHP-FPM: сколько воркеров занято и сколько запросов ждет, упоры в pm.max_children, медленные и убитые по таймауту запросы и падения воркеров из лога FPM, падения php по журналу ядра. Алерт, когда пул не справляется подолгу: запросы ждали воркера от 15 минут из часа, пул упирался в предел от 60 раз за час, по таймауту убито от 20 запросов за час или воркеры упали от 3 раз за час.':
    'PHP-FPM pools: how many workers are busy and how many requests wait, hits of pm.max_children, slow requests, requests killed on timeout and crashed workers from the FPM log, php crashes from the kernel journal. An alert when a pool cannot keep up for long: requests waited for a worker 15 minutes of an hour or more, the pool hit its limit 60 times an hour or more, 20 requests or more an hour were killed on timeout, or workers crashed 3 times an hour or more.',
  'падения (SIGSEGV и др.)': 'crashes (SIGSEGV etc.)',
  'Ошибки в логах': 'Errors in logs',
  'ошибки в логе': 'errors in the log',
  'Все логи': 'All logs',
  'Со всплеском: {n}': 'With a spike: {n}',
  'за час: {n}': 'in an hour: {n}',
  'из {m} строк': 'of {m} lines',
  'логов с ошибками: {n}': 'logs with errors: {n}',
  'обычно около {n} в час': 'usually about {n} an hour',
  'обычно почти нет': 'usually almost none',
  'в Telegram не уходит: всплеск, а не авария (алерт - когда в 20 раз выше обычного держится 2 часа)':
    'Not sent to Telegram: a spike, not an outage (alerts when 20x above usual for 2 hours)',
  'всплески за сутки:': 'spikes in 24 h:',
  '{when} до {n} в час': '{when} up to {n}/h',
  'не алертить о всплеске в этом логе:': 'no alerts on a spike in this log:',
  'Не алертить о всплеске ошибок в этом логе: на день, неделю или постоянно': 'No alerts on a spike of errors in this log: for a day, a week or for good',
  'Логов пропущено за последнюю минуту: {n} (за минуту прочитано больше 64 МБ).': 'Logs skipped in the last minute: {n} (over 64 MB read in a minute).',
  'Строки с ошибками в логах контейнеров и подов: ERROR, FATAL, Exception, трейсбеки, [error] nginx, level=error. Алерт только на всплеск: за час от 200 строк и в пять раз больше обычного часа этого же лога; пока истории меньше 4 часов - от 20 000. Лог, где ошибки есть всегда, не алертит, строки стека за ошибки не считаются.':
    'Error lines in container and pod logs: ERROR, FATAL, Exception, tracebacks, nginx [error], level=error. An alert only on a spike: 200 lines or more in an hour and five times the usual hour of the same log; until there are 4 hours of history, 20 000 or more. A log that always carries errors does not alert, stack frames do not count as errors.',
  'крон': 'cron',
  'ошибки уходят в cron и выбрасываются: 2>&1 стоит перед перенаправлением в файл, а не после': 'errors go to cron and are thrown away: 2>&1 comes before the redirection to a file, not after it',
  'ошибки (stderr) уходят в cron и выбрасываются: почты нет': 'errors (stderr) go to cron and are thrown away: there is no mail',
  'вывод уходит в cron и выбрасывается: почты нет': 'the output goes to cron and is thrown away: there is no mail',
  'идут сейчас: {n}': 'running now: {n}',
  'от {u}': 'as {u}',
  'запуск {t}': 'started {t}',
  'обычно меньше минуты': 'usually under a minute',
  'обычно до {a}': 'usually up to {a}',
  'идут {n} копий, самая старая {a}': '{n} copies running, the oldest for {a}',
  'идет {a}': 'running for {a}',
  'процессор {a}': 'CPU {a}',
  'не алертить по этой задаче:': 'no alerts for this job:',
  'Не алертить по этой задаче: на день, неделю или постоянно': 'No alerts for this job: for a day, a week or for good',
  'задач: {n}': 'jobs: {n}',
  'Все задачи': 'All jobs',
  'Идут сейчас: {n}': 'Running now: {n}',
  'Задачи cron на хосте и в контейнерах. Алерт, когда задача наслаивается (новая копия запустилась, пока старая не кончилась, и обе идут дольше 15 минут) или висит в разы дольше обычного: обычное - самая долгая из прошлых копий, без истории - 6 часов. Копии с детьми и их процессор видны в строке.':
    'Cron jobs on the host and in containers. An alert when a job piles up (a new copy started before the old one ended, and both run longer than 15 minutes) or hangs many times longer than usual: the usual is the longest of the past copies, without history 6 hours. Copies with their children and their CPU are shown in the row.',
  'Почты нет ({w}): что задачи выводят, cron выбрасывает, за сутки {n} раз. Перенаправьте вывод в лог: >> /var/log/x.log 2>&1.':
    'No mail ({w}): whatever the jobs print, cron throws away, {n} times in a day. Redirect the output to a log: >> /var/log/x.log 2>&1.',
  'Почты нет ({w}): что задачи выводят, cron выбрасывает. Перенаправьте вывод в лог: >> /var/log/x.log 2>&1.':
    'No mail ({w}): whatever the jobs print, cron throws away. Redirect the output to a log: >> /var/log/x.log 2>&1.',
  'Программы под supervisor и pm2. Алерт, когда воркер не запускается (supervisor или pm2 перестал его поднимать, или три минуты не может запуститься), завершился с ошибкой или падает и поднимается: три перезапуска за 15 минут. Остановленные руками не судятся.': 'Programs under supervisor and pm2. An alert when a worker does not start (supervisor or pm2 gave up on it, or it cannot start for three minutes), exited with an error or keeps crashing: three restarts in 15 minutes. Programs stopped by hand are not judged.',
  'Без порога панель сама алертит очередь от сотни сообщений, которая полчаса растет и не разбирается, стоит больше часа или осталась без потребителей. Упавшие задачи алертят только всплеском: от сотни за час и в пять раз больше обычного. Очереди мертвых писем (dlq) не судятся.':
    'Without a threshold the panel alerts on its own when a queue of a hundred messages or more grows for half an hour and is not consumed, stands still for over an hour or is left without consumers. Failed jobs alert only on a spike: a hundred or more in an hour and five times the usual. Dead letter queues (dlq) are not judged.',
  'очередь': 'queue',
  'заглушено': 'muted',
  'Ошибки драйвера (Xid) за сутки и те, после которых GPU ждет сброса': 'Driver errors (Xid) in a day and those after which the GPU waits for a reset',
  'ждет сброса GPU: Xid {list}': 'GPU waits for a reset: Xid {list}',
  'ждет сброса': 'waits for a reset',
  'держится до перезагрузки сервера или драйвера NVIDIA': 'holds until the server or the NVIDIA driver is restarted',
  'Пропускная': 'Throughput',
  'Токены': 'Tokens',
  'в очереди {n}': '{n} queued',
  'в работе': 'running',
  'в работе {n}': '{n} running',
  'видно GPU {a} из {b} на шине PCI': '{a} of {b} GPUs on the PCI bus visible',
  'вытеснено {n}': '{n} preempted',
  'данные устарели: {n} назад': 'stale data: {n} ago',
  'доля': 'share',
  'драйвер': 'driver',
  'драйвер NVIDIA не отвечает': 'the NVIDIA driver does not answer',
  'драйвер NVIDIA не установлен': 'the NVIDIA driver is not installed',
  'за 5 минут, мини-графики за час': 'over 5 minutes, sparklines over an hour',
  'за {n} мин': 'in {n} min',
  'загрузка {p}%': 'load {p}%',
  'замедление с {n}°C': 'slowdown from {n}°C',
  'внутри контейнера :{p}': 'inside the container :{p}',
  'показания nvidia-smi застыли': 'nvidia-smi readings are frozen',
  'показания nvidia-smi застыли: им нельзя верить': 'nvidia-smi readings are frozen: do not trust them',
  '/metrics закрыт ключом ({e}). Чтобы панель видела запросы и ошибки, в конфиге LiteLLM: require_auth_for_metrics_endpoint: false.':
    '/metrics requires a key ({e}). For the panel to see requests and errors, set require_auth_for_metrics_endpoint: false in the LiteLLM config.',
  '/metrics закрыт ключом ({e}): без ключа панель метрики не читает.':
    '/metrics requires a key ({e}): the panel does not read metrics with a key.',
  'запр/с': 'req/s',
  'карт на шине: {n}, нет helper gpu-setup': 'cards on the bus: {n}, no gpu-setup helper',
  'не отвечают: {n}': 'not answering: {n}',
  'нет helper gpu-setup: загрузка не видна': 'no gpu-setup helper: load is not visible',
  'обычно {v}': 'usually {v}',
  'отменено {n}': '{n} aborted',
  'ошибок {n}': '{n} errors',
  'память': 'memory',
  'память {a} из {b} ГБ': 'memory {a} of {b} GB',
  'питание': 'power',
  'питание, % лимита': 'power, % of limit',
  'промпт {n} ток/с': 'prompt {n} tok/s',
  'работает {d}': 'up {d}',
  'режим': 'state',
  'такты': 'clocks',
  'темп.': 'temp.',
  'ток/с': 'tok/s',
  'упирается в лимит питания': 'hits the power limit',
  'пользователь': 'user',
  'Модели': 'Models',
  'GPU: драйвер, пропала карта, ошибки Xid': 'GPU: driver, lost card, Xid errors',
  'GPU: перегрев': 'GPU: overheating',
  'LLM: не отвечает, очередь, задержки': 'LLM: not answering, queue, latencies',
  'Версии ПО': 'Software versions',
  'сроки поддержки по endoflife.date на {d}': 'support dates from endoflife.date as of {d}',
  'кончилась {d}': 'ended {d}',
  'кончилась': 'ended',
  'срок не объявлен': 'no end date yet',
  'до {d}, через {n} дн.': 'until {d}, in {n} days',
  'хост: {list}': 'host: {list}',
  '{n} г. назад': '{n} years ago',
  '{n} дн. назад': '{n} days ago',
  'Версия не видна, в теге образа ее нет: {list}.': 'The version is not visible, the image tag has none: {list}.',
  'В окружении образа версии тоже нет.': 'The image environment has no version either.',
  'Агент 2.35 и новее берет версию из окружения образа: обновите агент.':
    'Agent 2.35 and newer takes the version from the image environment: update the agent.',
  'Образы старше года: {n}': 'Images older than a year: {n}',
  'собран {d}': 'built {d}',
  'Версии берутся из инвентаря баз, окружения и тега образа, на хосте - из пакетов сторонних репозиториев (pgdg, sury, nodesource, nginx.org, MariaDB, MySQL, Redis). Пакеты дистрибутива дистрибутив чинит, пока жива ОС, их срок - поддержка ОС.':
    'Versions come from the database inventory, the image environment and tag, on the host from packages of third-party repositories (pgdg, sury, nodesource, nginx.org, MariaDB, MySQL, Redis). Distribution packages are fixed by the distribution while the OS is supported, their end date is the OS support.',
  'ПО без поддержки - обновите': 'software out of support - upgrade',
  'скоро кончится поддержка ПО - запланируйте обновление': 'software support ends soon - plan an upgrade',
  '{name}: без поддержки {list}': '{name}: out of support {list}',
  '{name}: скоро кончится поддержка {list}': '{name}: support ends soon for {list}',
  // версии ПО: как исправить и решения
  '{name} {from} и {to} - разные основные версии: код под {from} скорее всего сразу не заведется, сначала проверить на копии.':
    '{name} {from} and {to} are different major versions: code written for {from} most likely will not run right away, test it on a copy first.',
  'Код приложения сначала проверить на {name} {to}, лучше на копии.': 'Test the application code on {name} {to} first, preferably on a copy.',
  '{name} {to} - новая основная версия: сначала прочитать, что в ней поменялось (upgrade guide).':
    '{name} {to} is a new major version: read what changed in it first (upgrade guide).',
  'Grafana при запуске переводит свою базу на новую схему: обратно только из копии данных.':
    'Grafana migrates its database to the new schema on start: going back is possible only from the data copy.',
  'В Traefik 3 часть правил и меток пишется иначе: сначала руководство по миграции с v2.':
    'In Traefik 3 some rules and labels are written differently: read the v2 migration guide first.',
  'Новый Redis читает старые данные, обратно только из копии. С 7.4 лицензия не BSD, замена на тех же данных - Valkey.':
    'The new Redis reads the old data, going back only from a copy. Since 7.4 the license is not BSD, a drop-in replacement on the same data is Valkey.',
  'Новый Valkey читает старые данные, обратно только из копии.': 'The new Valkey reads the old data, going back only from a copy.',
  'Это базовый образ ОС: пакеты, которые ставятся при запуске, в {name} {to} могут называться иначе.':
    'This is an OS base image: packages installed at start may have other names in {name} {to}.',
  'Поддерживаемой версии {name} новее {from} в таблице сроков нет: выбрать по сайту вендора.':
    'The support table has no supported {name} version newer than {from}: pick one on the vendor site.',
  'Узнать папку, compose-файл и сервис контейнера {c}, дальше все в этом же окне. Пусто - контейнер запущен не из compose.':
    'Find the directory, compose file and service of container {c}, then do everything in the same window. Empty means the container was not started by compose.',
  'Копия compose-файла для отката.': 'A copy of the compose file for rollback.',
  'Копия данных контейнера для отката, на это время он остановлен.': 'A copy of the container data for rollback, the container is stopped meanwhile.',
  'Поменять образ на {new}. Последняя строка скажет, вышло ли: НЕ ЗАМЕНЕНО - строка image в compose другая (образ из переменной или .env), ее поправить руками, grep выше показывает все строки image.':
    'Change the image to {new}. The last line says whether it worked: NOT REPLACED means the image line in compose looks different (image from a variable or .env), fix it by hand, grep above shows all image lines.',
  'Перезапустить и проверить: в логе нет ошибок, а последняя строка скажет, на новом ли образе контейнер (Up в ps этого не говорит). НЕ ОБНОВЛЕН - образ в compose не заменен или такого тега нет (up скажет not found), прежний контейнер продолжит работать.':
    'Restart and check: the log has no errors, and the last line says whether the container runs the new image (Up in ps does not tell that). NOT UPDATED means the image in compose was not replaced or there is no such tag (up says not found), the old container keeps running.',
  'Вернуть прежний compose-файл и перезапустить.': 'Put back the previous compose file and restart.',
  'Вернуть прежний compose-файл и данные из копии, перезапустить.': 'Put back the previous compose file and the data from the copy, restart.',
  'Вернуть прежний образ и данные из копии, перезапустить.': 'Put back the previous image and the data from the copy, restart.',
  'Контейнер не нужен - остановить и убрать проект. Данные в папках и томах остаются на диске.':
    'The container is not needed: stop and remove the project. Data in directories and volumes stays on disk.',
  'Контейнер не нужен - убрать только его, остальные сервисы проекта работают. Данные остаются на диске. Сервис убрать и из compose-файла, иначе он вернется при следующем up.':
    'The container is not needed: remove only it, the other services of the project keep running. Data stays on disk. Remove the service from the compose file too, or it comes back with the next up.',
  'Контейнер не нужен - остановить и удалить. Данные в папках и томах остаются на диске.':
    'The container is not needed: stop and delete it. Data in directories and volumes stays on disk.',
  'Скачать новый образ.': 'Pull the new image.',
  'Контейнер {c} запущен не из compose: пересоздать его тем же docker run, что и раньше, но с новым образом.':
    'Container {c} was not started by compose: recreate it with the same docker run as before, but with the new image.',
  'Контейнер {c} запущен не из compose, образ собран на этой ноде ({env}): поменять FROM в Dockerfile на {name} {to}, пересобрать и пересоздать контейнер тем же docker run.':
    'Container {c} was not started by compose and its image was built on this node ({env}): change FROM in the Dockerfile to {name} {to}, rebuild and recreate the container with the same docker run.',
  'Сохранить текущий образ под тегом kervax-old, для отката.': 'Keep the current image under the kervax-old tag, for rollback.',
  'Скачать свежий образ, перезапустить и проверить ps и лог. Версия не поменялась - автор образа его еще не пересобрал.':
    'Pull a fresh image, restart and check ps and the log. If the version stays the same, the image author has not rebuilt it yet.',
  'Вернуть прежний образ и перезапустить.': 'Put back the previous image and restart.',
  'Версия пришла из базового образа ({env}): найти FROM в Dockerfile и поменять на {name} {to}.':
    'The version comes from the base image ({env}): find FROM in the Dockerfile and change it to {name} {to}.',
  'Пересобрать со свежей базой, перезапустить и проверить ps и лог.': 'Rebuild with a fresh base, restart and check ps and the log.',
  'Вернуть прежний FROM и пересобрать так же.': 'Put back the previous FROM and rebuild the same way.',
  'Образ {image} собирают не здесь ({env}): поменять FROM на {name} {to} там, где его собирают, и выкатить как обычно.':
    'Image {image} is not built here ({env}): change FROM to {name} {to} where it is built and roll it out as usual.',
  'Имя образа уже перевешено на другую сборку: пересоздать контейнер (docker compose up -d). Версия не поменялась - пересобрать образ на {name} {to}.':
    'The image name already points to another build: recreate the container (docker compose up -d). If the version stays the same, rebuild the image on {name} {to}.',
  'Дамп всех баз, база при этом работает. Пустой файл - дамп не вышел, проверить пользователя в -U.':
    'Dump all databases while the database keeps running. An empty file means the dump failed, check the user in -U.',
  'Копия данных для отката, база на это время остановлена.': 'A copy of the data for rollback, the database is stopped meanwhile.',
  'Команды смены тега нет намеренно: после копии поменять тег на {to} в compose руками, up -d и смотреть лог до конца перевода данных.':
    'There is no tag change command on purpose: after the copy change the tag to {to} in compose by hand, run up -d and watch the log until the data conversion ends.',
  'PostgreSQL {to} не прочитает данные {from}: тег просто так не менять. Путь: рядом новый контейнер {to} с пустой папкой данных, в него дамп, проверка, переключение приложения. Расширения нужны и в новом образе.':
    'PostgreSQL {to} cannot read {from} data: do not just change the tag. The path: a new {to} container next to it with an empty data directory, the dump into it, a check, switching the application. Extensions are needed in the new image too.',
  'MySQL обновляется на месте, но по одной LTS за раз (5.7, 8.0, 8.4), совместимость проверит MySQL Shell: util.checkForServerUpgrade(). Обратно только из копии.':
    'MySQL upgrades in place, but one LTS at a time (5.7, 8.0, 8.4), MySQL Shell checks compatibility: util.checkForServerUpgrade(). Going back only from a copy.',
  'MariaDB обновляется на месте, после запуска нужен mariadb-upgrade (образ делает его сам при MARIADB_AUTO_UPGRADE=1). Обратно только из копии.':
    'MariaDB upgrades in place, after the start mariadb-upgrade is needed (the image runs it itself with MARIADB_AUTO_UPGRADE=1). Going back only from a copy.',
  'MongoDB - по одной основной версии за раз (5.0, 6.0, 7.0, 8.0), перед каждой setFeatureCompatibilityVersion. Обратно только из копии.':
    'MongoDB goes one major version at a time (5.0, 6.0, 7.0, 8.0), setFeatureCompatibilityVersion before each. Going back only from a copy.',
  'Elasticsearch - через последний выпуск своей основной версии (7.17, 8.19), что мешает, покажет Upgrade Assistant. Обратно только из снапшота.':
    'Elasticsearch goes through the last release of its major version (7.17, 8.19), Upgrade Assistant shows what blocks it. Going back only from a snapshot.',
  'ClickHouse обновляется на месте, лучше на LTS (X.3, X.8), сначала прочитать Backward Incompatible Changes в changelog. Обратно только из копии.':
    'ClickHouse upgrades in place, preferably to an LTS (X.3, X.8), read Backward Incompatible Changes in the changelog first. Going back only from a copy.',
  'RabbitMQ - через промежуточные версии по upgrade path, перед каждой rabbitmqctl enable_feature_flag all. Обратно только из копии.':
    'RabbitMQ goes through intermediate versions by the upgrade path, rabbitmqctl enable_feature_flag all before each. Going back only from a copy.',
  'ZooKeeper обновляется на месте по одному узлу, с 3.4 без снапшота нужен snapshot.trust.empty=true.':
    'ZooKeeper upgrades in place one node at a time, from 3.4 without a snapshot snapshot.trust.empty=true is needed.',
  'Поставить PHP {to} рядом с {from} с теми же расширениями, старый продолжает работать. Какого-то пакета нет - убрать его из списка в /tmp и повторить последнюю строку.':
    'Install PHP {to} next to {from} with the same extensions, the old one keeps running. If some package does not exist, remove it from the list in /tmp and repeat the last line.',
  'Перевести сайты на {to}: пулы из /etc/php/{from}/fpm/pool.d/ в /etc/php/{to}/fpm/pool.d/, у сайтов сокет php{to}-fpm.sock, CLI - update-alternatives --set php /usr/bin/php{to}.':
    'Move the sites to {to}: pools from /etc/php/{from}/fpm/pool.d/ to /etc/php/{to}/fpm/pool.d/, the php{to}-fpm.sock socket for the sites, CLI with update-alternatives --set php /usr/bin/php{to}.',
  'Проверить версию и конфиг FPM, перезагрузить веб-сервер и открыть сайты.': 'Check the version and the FPM config, reload the web server and open the sites.',
  'Когда сайты и кроны на {to} - удалить {from} (apt спросит, конфиги останутся).': 'When the sites and crons are on {to}, remove {from} (apt asks first, configs stay).',
  'Перевести репозиторий NodeSource на {to} и обновить nodejs. Строка после правки скажет, нашелся ли репозиторий.': 'Switch the NodeSource repository to {to} and upgrade nodejs. The line after the edit says whether the repository was found.',
  'Проверить версию (последняя строка скажет, новая ли), перезапустить приложения, нативные модули пересобрать (npm rebuild).':
    'Check the version (the last line says whether it is the new one), restart the applications, rebuild native modules (npm rebuild).',
  'Вернуть репозиторий на {from} и прежнюю версию.': 'Switch the repository back to {from} and the previous version.',
  'Копия конфигов.': 'A copy of the configs.',
  'Обновить nginx из репозитория nginx.org. Версия не поменялась - в sources.list.d старое имя дистрибутива или пакет зафиксирован (apt-mark showhold).':
    'Upgrade nginx from the nginx.org repository. If the version stays the same, sources.list.d has an old distribution name or the package is held (apt-mark showhold).',
  'Проверить версию, конфиг и что служба работает.': 'Check the version, the config and that the service runs.',
  'Вернуть прежнюю версию пакета.': 'Put back the previous package version.',
  'Ветку HAProxy задает репозиторий (PPA vbernat/haproxy-X.Y или backports): перевести его на {to}, apt update и apt install haproxy.':
    'The HAProxy branch comes from the repository (PPA vbernat/haproxy-X.Y or backports): switch it to {to}, apt update and apt install haproxy.',
  'Проверить конфиг и что служба работает.': 'Check the config and that the service runs.',
  'Дамп всех баз кластера {from}, он при этом работает.': 'Dump all databases of the {from} cluster while it keeps running.',
  'Переезд штатно: apt install postgresql-{to}, пустой кластер {to} main убрать (pg_dropcluster {to} main --stop), затем pg_upgradecluster {from} main. Старый остается остановленным, к нему можно вернуться.':
    'The standard move: apt install postgresql-{to}, remove the empty {to} main cluster (pg_dropcluster {to} main --stop), then pg_upgradecluster {from} main. The old one stays stopped, you can go back to it.',
  'Дамп всех баз (если root входит по паролю - добавить -p).': 'Dump all databases (if root logs in with a password, add -p).',
  'Выгрузить очереди, обменники и пользователей, без сообщений.': 'Export queues, exchanges and users, without messages.',
  'Копия папки данных при остановленной службе или штатный дамп.': 'A copy of the data directory with the service stopped, or a regular dump.',
  'Перевести репозиторий вендора {name} на {to} по его инструкции, apt update, обновить пакеты и проверить службу.':
    'Switch the {name} vendor repository to {to} by its guide, apt update, upgrade the packages and check the service.',
  'Пакеты не из apt: обновить {name} до {to} через dnf по инструкции вендора, перед этим копия данных и конфигов.':
    'Packages are not from apt: upgrade {name} to {to} with dnf by the vendor guide, copy the data and configs first.',
  'подойдет и {list}': '{list} also fit',
  'контейнер {c}': 'container {c}',
  'обновить нельзя': 'cannot upgrade',
  'отложено до конца поддержки': 'postponed until support ends',
  'отложить до конца поддержки': 'postpone until support ends',
  'по исходящему, как считает хостер': 'by outgoing, as the hoster counts',
  'по всему трафику, как считает хостер': 'by all traffic, as the hoster counts',
  'сейчас тихо': 'quiet now',
  '↑ {tx} ↓ {rx} в час': '↑ {tx} ↓ {rx} an hour',
  'за сутки ↑ {tx} ↓ {rx}': 'a day ↑ {tx} ↓ {rx}',
  'около ↑ {tx} ↓ {rx} в сутки': 'about ↑ {tx} ↓ {rx} a day',
  'Здесь все, что прошло через интерфейсы контейнера, вместе с обменом с другими контейнерами этого сервера: база, которая отдает приложению гигабайты, тариф не тратит. В тариф идет только то, что ушло через сетевую карту. Контейнеры с сетью хоста по отдельности не видны.':
    'This is everything that went through the container interfaces, including traffic with other containers on this server: a database that sends gigabytes to the app does not use the plan. Only what left through the network card counts toward the plan. Containers on the host network are not shown separately.',
  'Кто качает трафик и пишет на диск, по часам': 'Who uses the traffic and writes to disk, by hour',
  'временные файлы {s} в час': 'temporary files {s} an hour',
  'чтение мимо shared_buffers {s} в час': 'reads past shared_buffers {s} an hour',
  'WAL {s} в час': 'WAL {s} an hour',
  'полные копии страниц: {p}% записей': 'full page images: {p}% of records',
  'чекпоинты: {a} по времени, {b} внеплановых': 'checkpoints: {a} by time, {b} requested',
  'буфер WAL переполнялся {n} раз за час': 'WAL buffer filled up {n} times an hour',
  '{n} транзакций в секунду': '{n} transactions a second',
  'Что поправить в настройках': 'What to fix in the settings',
  'Настройки PostgreSQL': 'PostgreSQL settings',
  'Ядро держит {have} huge pages, а postgres работает без них: видимо, страницы выделены после его запуска. Нужен перезапуск postgres':
    'The kernel holds {have} huge pages, but postgres runs without them: the pages were probably reserved after it started. postgres needs a restart',
  'shared_buffers {sb} без huge pages: таблицы страниц на сервере занимают {pt}, каждое подключение отображает все буферы страницами по 4 КБ. Нужно vm.nr_hugepages = {need} в /etc/sysctl.d{hp} и перезапуск postgres. На работающем сервере ядро может не найти столько свободных блоков, надежнее выделять при остановленном postgres':
    'shared_buffers {sb} without huge pages: page tables on the server take {pt}, every connection maps all the buffers with 4 KB pages. Set vm.nr_hugepages = {need} in /etc/sysctl.d{hp} and restart postgres. On a running server the kernel may not find that many free blocks, reserving them while postgres is stopped is more reliable',
  ', huge_pages = try': ', huge_pages = try',
  'Чекпоинты чаще идут по объему WAL, чем по времени: при {wal} WAL в час max_wal_size {cur} мал, а после каждого чекпоинта страницы снова пишутся в WAL целиком. Примерно {rec} хватит, чтобы чекпоинты шли по checkpoint_timeout, перезапуск не нужен':
    'Checkpoints are triggered by WAL volume more often than by time: with {wal} of WAL an hour max_wal_size {cur} is too small, and after every checkpoint pages are written to WAL in full again. About {rec} lets checkpoints follow checkpoint_timeout, no restart needed',
  'Буфер WAL переполнялся {n} раз за час: запросы ждали записи WAL на диск. wal_buffers {cur}, обычно хватает 64-128 МБ (нужен перезапуск)':
    'The WAL buffer filled up {n} times an hour: queries waited for WAL to be written to disk. wal_buffers is {cur}, 64-128 MB is usually enough (needs a restart)',
  'wal_compression = {cur}{alias}: pglz самый медленный вариант, lz4 сжимает почти так же и в разы быстрее. Поставить lz4 можно без перезапуска':
    'wal_compression = {cur}{alias}: pglz is the slowest option, lz4 compresses almost as well and several times faster. Switching to lz4 needs no restart',
  ' (это pglz)': ' (that is pglz)',
  'Сжатие WAL выключено, а WAL {wal} в час, полные копии страниц - {fpi}% записей: wal_compression = lz4 уменьшит WAL и запись на диск, перезапуск не нужен':
    'WAL compression is off, while WAL is {wal} an hour and full page images are {fpi}% of records: wal_compression = lz4 cuts WAL and disk writes, no restart needed',
  'Docker при остановке ждет postgres {to} с, потом убивает: с буферами {sb} остановка может не успеть, и тогда после docker stop или перезагрузки сервера база восстанавливается как после аварии. В compose stop_grace_period: 5m (контейнер пересоздается), для ручной остановки docker stop -t 600':
    'On stop docker waits {to} s for postgres, then kills it: with {sb} of buffers the shutdown may not make it, and then after docker stop or a server reboot the database recovers as after a crash. In compose stop_grace_period: 5m (the container is recreated), for a manual stop docker stop -t 600',
  'Сигнал остановки SIGTERM: postgres ждет, пока отключатся все клиенты, а пул соединений сам не отключится. Нужен stop_signal: SIGINT':
    'The stop signal is SIGTERM: postgres waits for all clients to disconnect, and a connection pool never disconnects by itself. Use stop_signal: SIGINT',
  'Запросы пишут временные файлы {s} в час: сортировки и хеши не помещаются в work_mem ({wm}), это запись на диск и его износ. Поднимать work_mem осторожно: он выделяется на каждую операцию каждого подключения, а max_connections {mc}. Обычно дешевле поправить запросы с временными файлами':
    'Queries write {s} of temporary files an hour: sorts and hashes do not fit into work_mem ({wm}), which means disk writes and wear. Raise work_mem with care: it is allocated per operation of every connection, and max_connections is {mc}. Fixing the queries with temporary files is usually cheaper',
  'random_page_cost = {v}, а диски SSD: планировщик считает чтение по индексу в разы дороже последовательного и зря выбирает полный проход таблицы. Для SSD обычно 1.1, перезапуск не нужен':
    'random_page_cost = {v} on SSD disks: the planner thinks an index read costs several times a sequential one and picks full table scans for nothing. 1.1 is usual for SSD, no restart needed',
  'effective_cache_size {ecs} при {mem} памяти: планировщик думает, что кэша мало, и реже выбирает индексы. Обычно 50-75% памяти, перезапуск не нужен':
    'effective_cache_size {ecs} with {mem} of memory: the planner thinks the cache is small and picks indexes less often. 50-75% of memory is usual, no restart needed',
  'shared_buffers {sb} при {mem} памяти{lim}, а база намного больше буферов: обычно ставят около 25% памяти (нужен перезапуск)':
    'shared_buffers {sb} with {mem} of memory{lim}, while the database is much larger than the buffers: about 25% of memory is usual (needs a restart)',
  ' (лимит контейнера)': ' (container limit)',
  'shared_buffers {sb}, а это больше половины памяти ({mem}{lim}): остальному серверу и кэшу ОС остается мало, обычно ставят около 25% (нужен перезапуск)':
    'shared_buffers {sb} is more than half of memory ({mem}{lim}): little is left for the rest of the server and the OS cache, about 25% is usual (needs a restart)',
  ', лимит контейнера': ', container limit',
  'Самые долгие запросы покажет pg_stat_statements: добавьте его в shared_preload_libraries (нужен перезапуск PostgreSQL) и выполните CREATE EXTENSION pg_stat_statements':
    'pg_stat_statements shows the slowest queries: add it to shared_preload_libraries (needs a PostgreSQL restart) and run CREATE EXTENSION pg_stat_statements',
  'pg_stat_statements загружен, но расширение не создано ни в одной базе: выполните CREATE EXTENSION pg_stat_statements, перезапуск не нужен. Панель увидит его в течение часа':
    'pg_stat_statements is loaded, but the extension is not created in any database: run CREATE EXTENSION pg_stat_statements, no restart needed. The panel picks it up within an hour',
  'Тексты запросов pg_stat_statements занимают {s}: так бывает, когда один запрос приходит с разным числом значений (многострочный INSERT, длинный IN), и каждый вариант хранится отдельно со своим текстом. Они вытесняют из статистики другие запросы, а каждое чтение текстов поднимает файл в память целиком, поэтому новые тексты панель читает раз в 6 часов':
    'pg_stat_statements query texts take {s}: this happens when one query comes with a varying number of values (a multi-row INSERT, a long IN) and every variant is stored separately with its own text. They push other queries out of the statistics, and every read of the texts loads the whole file into memory, so the panel reads new texts every 6 hours',
  'Запросы не показаны: на сервере много инстансов, и отчет не вместил бы все': 'Queries are not shown: the server has many instances, and the report would not fit them all',
  'Собираю данные: первая картина через 15 минут, за полный час - через час': 'Collecting data: the first picture in 15 minutes, a full hour in an hour',
  'Запросы заняли {s} с за час - база почти не нагружена': 'Queries took {s} s in an hour - the database is almost idle',
  // как включить pg_stat_statements (db_health.pgss_fix)
  'откат': 'rollback',
  // зеленый бар: замечание исправили, пока оно было на экране (useFixDone)
  '{name}: контейнера больше нет, замечание закрыто': '{name}: the container is gone, issue closed',
  '{name}: образ обновлен до {image}, замечание закрыто': '{name}: image updated to {image}, issue closed',
  '{name}: образ {image} скачан заново, свежая сборка, замечание закрыто': '{name}: image {image} pulled again, fresh build, issue closed',
  '{name}: docker-сокет больше не смонтирован, замечание закрыто': '{name}: the docker socket is no longer mounted, issue closed',
  '{name}: ключей от хоста больше нет (было: {what}), замечание закрыто': '{name}: no more host keys (was: {what}), issue closed',
  '{name}: restart {policy}, поднимется после перезагрузки, замечание закрыто': '{name}: restart {policy}, will come back after a reboot, issue closed',
  'Сервер перезагружен, работает ядро {k}, замечание закрыто': 'Server rebooted, running kernel {k}, issue closed',
  'Сервер перезагружен, замечание закрыто': 'Server rebooted, issue closed',
  'Обновления безопасности установлены, автообновления включены, замечание закрыто': 'Security updates installed, automatic updates on, issue closed',
  'Обновления безопасности установлены, замечание закрыто': 'Security updates installed, issue closed',
  'Список пакетов обновлен, замечание закрыто': 'Package list updated, issue closed',
  'ОС обновлена: {os}, замечание закрыто': 'OS upgraded: {os}, issue closed',
  'ОС обновлена, замечание закрыто': 'OS upgraded, issue closed',
  'Исправлено: {text}': 'Fixed: {text}',
  'Учетка {name} удалена, замечание закрыто': 'Account {name} deleted, issue closed',
  'Учетка {name} отключена, войти по ней больше нельзя, замечание закрыто': 'Account {name} disabled, nobody can log in with it now, issue closed',
  'Учетка {name} больше не в заброшенных: по ней входили, замечание закрыто': 'Account {name} is no longer stale: someone logged in with it, issue closed',
  '{unit} включен, поднимется после перезагрузки, замечание закрыто': '{unit} enabled, will start after a reboot, issue closed',
  'Вход по паролю выключен, только ключи, замечание закрыто': 'Password login is off, keys only, issue closed',
  '{name} {cycle}: поддержка продлена, замечание закрыто': '{name} {cycle}: support extended, issue closed',
  '{name} обновлен с {from} до {to}, замечание закрыто': '{name} upgraded from {from} to {to}, issue closed',
  '{name} {cycle} на ноде больше нет, замечание закрыто': '{name} {cycle} is no longer on the node, issue closed',
  'pg_stat_statements включен в {name}, замечание закрыто': 'pg_stat_statements enabled in {name}, issue closed',
  'pg_stat_statements включен, замечание закрыто': 'pg_stat_statements enabled, issue closed',
  'как включить': 'how to enable',
  'Самые долгие запросы появятся примерно через час после включения pg_stat_statements: панель сравнивает их со снимком часовой давности':
    'The slowest queries show up about an hour after pg_stat_statements was enabled: the panel compares them with an hour-old snapshot',
  'Панель увидит расширение в течение часа, самые долгие запросы - еще примерно через час: она сравнивает со снимком часовой давности.':
    'The panel picks up the extension within an hour, the slowest queries about an hour later: it compares with an hour-old snapshot.',
  'Плашка уйдет при следующем сборе, до 15 минут. Самые долгие запросы появятся примерно через час: панель сравнивает со снимком часовой давности.':
    'The notice goes away with the next collection, within 15 minutes. The slowest queries show up in about an hour: the panel compares with an hour-old snapshot.',
  'Сначала посмотреть, что уже загружено и откуда задано. source = command line - параметр в command контейнера, ALTER SYSTEM его не перебьет, тогда путь через compose ниже. Вторая строка - команда контейнера, каталог compose, проект и сервис.':
    'First see what is loaded already and where it is set. source = command line means the parameter is in the container command, ALTER SYSTEM does not override it, then take the compose way below. The second line is the container command, the compose directory, project and service.',
  'Сначала посмотреть, что уже загружено и откуда задано. source = command line - параметр в команде запуска сервиса, ALTER SYSTEM его не перебьет.':
    'First see what is loaded already and where it is set. source = command line means the parameter is in the service start command, ALTER SYSTEM does not override it.',
  'Создать расширение в базе {db}: панель ищет его там первым, а статистика в нем общая на весь сервер, одной базы достаточно. Работает и до перезапуска и заодно проверяет, что расширение есть в сборке: при ошибке is not available дальше не идти, без него PostgreSQL после перезапуска не запустится (на RHEL оно в пакете postgresql-contrib).':
    'Create the extension in the {db} database: the panel looks there first, and its statistics cover the whole server, one database is enough. It works before the restart too and checks that the build has the extension: on an "is not available" error stop here, without it PostgreSQL will not start after the restart (on RHEL it is in the postgresql-contrib package).',
  'Дописать pg_stat_statements к shared_preload_libraries, сейчас там {cur}. ALTER SYSTEM пишет в postgresql.auto.conf в каталоге данных, база работает дальше, значение вступит в силу после перезапуска. Каждая библиотека в своих кавычках: строку через запятую PostgreSQL принял бы за одну библиотеку и не запустился бы.':
    'Add pg_stat_statements to shared_preload_libraries, now it holds {cur}. ALTER SYSTEM writes to postgresql.auto.conf in the data directory, the database keeps running, the value takes effect after a restart. Every library in its own quotes: PostgreSQL would take one comma-separated string for a single library and would not start.',
  'Добавить pg_stat_statements в shared_preload_libraries, сейчас там пусто. ALTER SYSTEM пишет в postgresql.auto.conf в каталоге данных, база работает дальше, значение вступит в силу после перезапуска.':
    'Add pg_stat_statements to shared_preload_libraries, now it is empty. ALTER SYSTEM writes to postgresql.auto.conf in the data directory, the database keeps running, the value takes effect after a restart.',
  'Если source не command line: добавить в shared_preload_libraries. Если в setting уже что-то есть, перечислите все через запятую, каждую в своих кавычках: {ok}. Одной строкой {bad} нельзя: PostgreSQL примет ее за одну библиотеку и не запустится.':
    'If source is not command line: add it to shared_preload_libraries. If setting holds something already, list them all separated by commas, each in its own quotes: {ok}. A single string {bad} will not do: PostgreSQL takes it for one library and does not start.',
  'Перезапустить контейнер, чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться, лучше в тихое время. -t 120 дает базе спокойно завершиться, обычно это секунды.':
    'Restart the container so the library loads: the database is down for a few seconds, applications lose their connections and have to reconnect, better at a quiet time. -t 120 lets the database shut down cleanly, it usually takes seconds.',
  'Перезапустить PostgreSQL ({unit}), чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться, лучше в тихое время.':
    'Restart PostgreSQL ({unit}) so the library loads: the database is down for a few seconds, applications lose their connections and have to reconnect, better at a quiet time.',
  'Перезапустить PostgreSQL, чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться. На Debian и Ubuntu postgresql перезапускает все кластеры, один - postgresql@<версия>-<кластер> (systemctl list-units postgresql*), на RHEL юнит бывает postgresql-<версия>.':
    'Restart PostgreSQL so the library loads: the database is down for a few seconds, applications lose their connections and have to reconnect. On Debian and Ubuntu postgresql restarts every cluster, a single one is postgresql@<version>-<cluster> (systemctl list-units postgresql*), on RHEL the unit may be postgresql-<version>.',
  'Параметр задан в command сервиса {svc} в compose, и ALTER SYSTEM его не перебьет: правится compose. Копия файла рядом (.bak-kervax), sed дописывает pg_stat_statements к shared_preload_libraries, diff показывает измененные строки. Должна поменяться одна, у {svc}: если поменялись и чужие или последняя строка - НЕ ДОБАВЛЕНО (значение задано иначе, через переменную), верните копию и поправьте руками.':
    'The parameter is set in the command of the {svc} service in compose, and ALTER SYSTEM does not override it: compose is edited. A copy of the file next to it (.bak-kervax), sed adds pg_stat_statements to shared_preload_libraries, diff shows the changed lines. Exactly one must change, in {svc}: if others changed too or the last line says NOT ADDED (the value is set another way, through a variable), restore the copy and edit by hand.',
  'Параметр задан в command сервиса в compose, и ALTER SYSTEM его не перебьет: правится compose. Найдите строку и поставьте shared_preload_libraries={v}, файл перед правкой скопируйте.':
    'The parameter is set in the service command in compose, and ALTER SYSTEM does not override it: compose is edited. Find the line and set shared_preload_libraries={v}, copy the file before editing.',
  'Пересоздать контейнер с новой командой: данные в томе остаются, база недоступна несколько секунд, приложения теряют соединения и должны переподключиться. --no-deps не трогает остальные сервисы проекта.':
    'Recreate the container with the new command: the data stays in the volume, the database is down for a few seconds, applications lose their connections and have to reconnect. --no-deps leaves the other services of the project alone.',
  'Если source = command line: вместо ALTER SYSTEM и перезапуска правится compose. В файле из второй строки первого шага у этого сервиса в command дописать ,pg_stat_statements к shared_preload_libraries=, файл перед правкой скопировать и пересоздать контейнер (каталог, проект и сервис оттуда же):':
    'If source = command line: compose is edited instead of ALTER SYSTEM and a restart. In the file from the second line of the first step add ,pg_stat_statements to shared_preload_libraries= in the command of this service, copy the file before editing and recreate the container (the directory, project and service come from there too):',
  'Параметр задан в команде запуска, и ALTER SYSTEM его не перебьет. Поставьте там shared_preload_libraries={v}: контейнер не из compose пересоздается с новой командой (данные в томе остаются), у сервиса на хосте команда в юните или в pg_ctl.conf. Текущая команда:':
    'The parameter is set in the start command, and ALTER SYSTEM does not override it. Set shared_preload_libraries={v} there: a container not from compose is recreated with the new command (the data stays in the volume), a host service has the command in its unit or in pg_ctl.conf. The current command:',
  'Проверить: в списке есть pg_stat_statements, а запрос отвечает числом, а не ошибкой.':
    'Check: pg_stat_statements is in the list, and the query answers with a number, not an error.',
  'Удалить расширение, вернуть прежний список ({cur}) и перезапустить.': 'Drop the extension, put the previous list back ({cur}) and restart.',
  'Удалить расширение, вернуть shared_preload_libraries как было (пусто) и перезапустить.':
    'Drop the extension, put shared_preload_libraries back as it was (empty) and restart.',
  'Удалить расширение, вернуть compose из копии и пересоздать контейнер.': 'Drop the extension, restore compose from the copy and recreate the container.',
  'Удалить расширение, вернуть shared_preload_libraries и перезапустить. Если в setting было что-то еще, вместо RESET верните прежний список: {ex}. Если меняли compose, верните файл и пересоздайте контейнер.':
    'Drop the extension, put shared_preload_libraries back and restart. If setting held something else, put the previous list back instead of RESET: {ex}. If compose was edited, restore the file and recreate the container.',
  'текст скрыт: в нем пароль или секрет': 'text hidden: it contains a password or a secret',
  'текст еще не прочитан': 'text not read yet',
  '{p}% времени запросов': '{p}% of query time',
  '{n} вызовов': '{n} calls',
  '{ms} мс в среднем': '{ms} ms on average',
  '{s} с в среднем': '{s} s on average',
  'временные файлы {s}': 'temporary files {s}',
  'база {db}': 'database {db}',
  // SSH
  'По адресу {ip} отвечает другой сервер: там {o}, а у этой ноды {n}. Порты по этому адресу не про эту ноду, поэтому алертов по ним нет.':
    'Another server answers at {ip}: it runs {o}, while this node runs {n}. Ports at this address are not this node, so they raise no alerts.',
  'Нода ходит в интернет через NAT: панель видит ее с адреса {ip}, а там отвечает сам шлюз ({o}, у ноды {n}). Порты по этому адресу не про ноду, алертов по ним нет. Делать ничего не нужно: входящие идут на другие адреса.':
    'The node reaches the internet through NAT: the panel sees it from {ip}, where the gateway itself answers ({o}, the node runs {n}). Ports at this address are not the node and raise no alerts. Nothing to do: incoming traffic uses other addresses.',
  'перебор идет с': 'brute force from',
  'SSH: опасный вход': 'SSH: risky login',
  'порт': 'port',
  'root': 'root',
  'открыт в интернет': 'open to the internet',
  'закрыт снаружи': 'closed from outside',
  'снаружи не проверялся': 'not checked from outside',
  'по паролю': 'with a password',
  'по ключу': 'with a key',
  'запрещен': 'forbidden',
  'только по ключу': 'key only',
  'вход по паролю': 'password login',
  'включен': 'enabled',
  'выключен, только ключи': 'disabled, keys only',
  'защита от перебора': 'brute-force protection',
  'неудачных входов за 10 минут': 'failed logins in 10 minutes',
  'входов по паролю: {n}': 'password logins: {n}',
  // соединения: плитки портов, SSH и доступов
  'Открыто снаружи': 'Open from outside',
  'Открыто для панели': 'Open to the panel',
  'из {n} портов, что слушает нода': 'of {n} ports the node listens on',
  'Опасных': 'Dangerous',
  'базы, очереди, Docker API, kubelet': 'databases, queues, Docker API, kubelet',
  'Служебных': 'Internal',
  'Kubernetes, агенты, экспортеры': 'Kubernetes, agents, exporters',
  'Закрыто снаружи': 'Closed from outside',
  'Закрыто даже для панели': 'Closed even to the panel',
  'не проверить: {n}': 'cannot check: {n}',
  'только через Cloudflare: {n}': 'Cloudflare only: {n}',
  'только через Cloudflare': 'Cloudflare only',
  'только белому списку: {n}': 'allow list only: {n}',
  'только белому списку': 'allow list only',
  'адрес проверки: {ips}': 'checked from: {ips}',
  'Снаружи закрыт: проверка видит его открытым только потому, что ее адрес {ips} в белом списке файрвола ноды.':
    'Closed from outside: the check sees it open only because its address {ips} is in the allow list of the node firewall.',
  'Адрес проверки {ips} стоит в белом списке файрвола ноды (его добавляют под мониторинг). Порты {ports} файрвол пускает только по белому списку: проверке они открыты, а интернету нет. Они в колонке "Закрыто снаружи", алертов по ним нет. Чтобы проверка видела то же, что и интернет, добавьте в "Локациях" прокси в другой сети.':
    'The check address {ips} is in the allow list of the node firewall (it is added there for monitoring). The firewall lets ports {ports} in by allow list only: they are open to the check but not to the internet. They are in the "Closed from outside" column, with no alerts. For the check to see what the internet sees, add a proxy in another network in "Locations".',
  'в обход Cloudflare': 'bypasses Cloudflare',
  'Белые списки обходятся подменой {h}': 'Allow lists are bypassed with a forged {h}',
  'IP клиента можно подделать заголовком {h}': 'The client IP can be forged with {h}',
  'Порты {p} открыты в обход Cloudflare: при прямом заходе с поддельным {h} логи, баны fail2ban, лимиты входа и само приложение видят чужой адрес. Белых списков (allow/deny) тут нет.':
    'Ports {p} are open past Cloudflare: a direct request with a forged {h} makes logs, fail2ban bans, login limits and the app itself see someone else\'s address. There are no allow lists (allow/deny) here.',
  '{who} {where} верит заголовку от любого адреса:': '{who} {where} trusts the header from any address:',
  'в поде {n}': 'in pod {n}',
  'в контейнере {n}': 'in container {n}',
  'Порты {p} открыты в обход Cloudflare, и прямой заход на адрес ноды с поддельным {h} проходит allow/deny у сайтов ({n}):':
    'Ports {p} are open past Cloudflare, and a direct request to the node address with a forged {h} gets through the allow/deny of sites ({n}):',
  'Пустите на {p} только адреса Cloudflare или {fix}.': 'Let only Cloudflare addresses reach {p}, or {fix}.',
  'в ConfigMap ingress-nginx задайте proxy-real-ip-cidr диапазонами Cloudflare': 'set proxy-real-ip-cidr in the ingress-nginx ConfigMap to the Cloudflare ranges',
  'в trusted_proxies оставьте только диапазоны Cloudflare': 'keep only the Cloudflare ranges in trusted_proxies',
  'уберите forwardedHeaders.insecure и оставьте в trustedIPs только диапазоны Cloudflare': 'remove forwardedHeaders.insecure and keep only the Cloudflare ranges in trustedIPs',
  'в set_real_ip_from оставьте только диапазоны Cloudflare': 'keep only the Cloudflare ranges in set_real_ip_from',
  'Так задумано: белые списки этого веб-сервера обходить можно? Панель перестанет алертить по нему.':
    'Intended: may the allow lists of this web server be bypassed? The panel will stop alerting about it.',
  'так задумано': 'intended',
  'Сайты ноды идут через Cloudflare, а порт открыт и напрямую: защиту Cloudflare можно обойти по адресу ноды.':
    'Sites on this node go through Cloudflare, but the port is open directly too: Cloudflare protection can be bypassed via the node address.',
  'на {ips}': 'on {ips}',
  'ничего не открыто': 'nothing is open',
  'ничего не закрыто': 'nothing is closed',
  'Сайты через Cloudflare': 'Sites behind Cloudflare',
  'проверено {c} из {n}': 'checked {c} of {n}',
  'Напрямую закрыто:': 'Closed directly:',
  'туда пускают только Cloudflare': 'only Cloudflare is let in',
  'Напрямую открыто:': 'Open directly:',
  'защиту Cloudflare можно обойти по адресу ноды. Если так не задумано, пустите туда только адреса Cloudflare':
    'Cloudflare protection can be bypassed via the node address. If that is not intended, allow only Cloudflare addresses there',
  'Вход по паролю': 'Password login',
  'только ключи': 'keys only',
  'Вход root': 'Root login',
  'Защита от перебора': 'Brute-force protection',
  'Неудачных входов за 10 минут': 'Failed logins in 10 minutes',
  'Могут войти': 'Can log in',
  'из {n} учеток с shell': 'of {n} accounts with a shell',
  'Давно не входили': 'Unused for a long time',
  'больше 90 дней, а доступ есть': 'over 90 days, access still works',
  'sudo без пароля': 'sudo without password',
  'вход {d}': 'login {d}',
  'не входили': 'never logged in',
  // диск: плитки мест, прогноза и здоровья
  'Заполнено больше всего': 'Fullest',
  'свободно {n}': '{n} free',
  'Прогноз заполнения': 'Fill forecast',
  'не грозит': 'not expected',
  'в ближайший месяц': 'within a month',
  'меньше часа': 'under an hour',
  'через {n} ч': 'in {n} h',
  'через {n} дн': 'in {n} d',
  'растет на ~{r}% в сутки': 'grows ~{r}% a day',
  'Самый загруженный диск': 'Busiest disk',
  'ответ {v}': 'latency {v}',
  'все в порядке': 'all fine',
  // здоровье дисков плитками
  'износ': 'wear',
  'наработка': 'power-on',
  'серийник': 'serial',
  'виртуальный': 'virtual',
  'ресурс записи исчерпан': 'write endurance is used up',
  'пора менять': 'time to replace',
  'по средней за жизнь': 'lifetime average',
  'HDD износ не сообщает, следим за секторами': 'HDDs do not report wear, the sectors are watched instead',
  'износ диск не сообщает': 'the disk does not report wear',
  'SMART не читается: проверить диск, кабель или контроллер': 'SMART cannot be read: check the disk, the cable or the controller',
  'SMART не читается: диск за аппаратным RAID или неизвестным контроллером':
    'SMART cannot be read: the disk is behind a hardware RAID or an unknown controller',
  'SMART не читается: {list}': 'SMART cannot be read: {list}',
  'предупреждение NVMe {c}': 'NVMe warning {c}',
  'сбой': 'failed',
  'запасной': 'spare',
  'нет диска': 'no disk',
  '{dev} {lvl}: работает {a} из {n}': '{dev} {lvl}: {a} of {n} working',
  '{dev} не собран': '{dev} is not assembled',
  '{dev} собран': '{dev} is assembled',
  'массивы {list} собраны': 'arrays {list} are assembled',
  '{dev} пропал': '{dev} is gone',
  '90% износа примерно к {d}': '90% wear around {d}',
  'все диски в порядке': 'all disks are fine',
  'остальные диски в порядке': 'the other disks are fine',
  'виртуальная машина {v}: здоровье физических дисков видит только хостер':
    'virtual machine {v}: only the host sees the health of the physical disks',
  'ошибки ввода-вывода за 10 мин: {n}': 'I/O errors in 10 min: {n}',
  'ошибок ввода-вывода нет': 'no I/O errors',
  // процессы: плитки давления
  '{r}: ждут': '{r}: waiting',
  'все стоят {p}': 'all stalled {p}',
  'за 10 с {p}': '10 s {p}',
  'В D-state': 'In D-state',
  'ждут диск или сеть': 'waiting for disk or network',
  'сменяются, зависших нет': 'they keep changing, none stuck',
  'ядро видит зависшие задачи: {n} за 10 мин': 'the kernel sees hung tasks: {n} in 10 min',
  'Файловые дескрипторы': 'File descriptors',
  '{u} из {m}': '{u} of {m}',
  // CPU и память: плитки "сейчас"
  'Загрузка CPU': 'CPU usage',
  'алерт от {p}': 'alert from {p}',
  '5 и 15 мин: {a} / {b}': '5 and 15 min: {a} / {b}',
  'Самое загруженное ядро': 'Busiest core',
  'ядро {n}': 'core {n}',
  'Температура': 'Temperature',
  'Частота': 'Frequency',
  'Тепловой троттлинг': 'Thermal throttling',
  'за последний отчет': 'in the last report',
  'Занято': 'Used',
  'Доступно': 'Available',
  'из них кэш {n}': 'cache {n} of it',
  'Обмен со swap': 'Swap traffic',
  'загрузка {a} · выгрузка {b}': 'in {a} · out {b}',
  'Плитки сверху - фильтр: нажмите, чтобы скрыть группу из ленты.': 'The tiles above are a filter: click one to hide that group from the feed.',
  'к разделу': 'go to section',
  // веб и обновления ОС: плитки
  'Запросов в минуту': 'Requests per minute',
  '5xx в минуту': '5xx per minute',
  '{p}% запросов': '{p}% of requests',
  'Самый нагруженный лог': 'Busiest log',
  'в мин': 'per min',
  'Логов под наблюдением': 'Logs watched',
  'с запросами за минуту: {n}': 'with requests in the last minute: {n}',
  'Без кода ответа': 'No response code',
  'формат лога не разобран': 'log format not parsed',
  'Ответы 5xx за сутки': '5xx responses in the last day',
  'по логам веб-сервера': 'from web server logs',
  'Ждут установки': 'Pending updates',
  'из них безопасности {n}': 'security {n}',
  'после перезагрузки {k}': 'after reboot {k}',
  'не нужна': 'not needed',
  'с {d}': 'since {d}',
  'Автообновления': 'Automatic updates',
  'последний прогон {d}': 'last run {d}',
  // главная: сводка плитками
  'Сайты работают': 'Sites up',
  'Серверы онлайн': 'Servers online',
  'Контейнеры Docker': 'Docker containers',
  'Поды Kubernetes': 'Kubernetes pods',
  'Серверов под бэкапом': 'Servers backed up',
  'из {n}': 'of {n}',
  'проблем: {n}': 'problems: {n}',
  'предупреждений: {n}': 'warnings: {n}',
  'известных: {n}': 'known: {n}',
  'все в норме': 'all good',
  'все свежие': 'all fresh',
  'хостов: {n}': 'hosts: {n}',
  'кластеров: {n}': 'clusters: {n}',
  'серверов бэкапа: {n}': 'backup servers: {n}',
  'Бэкап вышел за окно': 'Backup ran past its window',
  // Docker: сводка и модалка хоста плитками
  'Хосты с Docker': 'Docker hosts',
  'без доступа: {list}': 'no access: {list}',
  'не на связи: {list}': 'not reporting: {list}',
  'все на связи': 'all reporting',
  'Контейнеры запущены': 'Containers running',
  'остановлено: {n}': 'stopped: {n}',
  'все запущены': 'all running',
  'Аварии': 'Failures',
  'упали: {n}': 'down: {n}',
  'по кругу: {n}': 'restart loop: {n}',
  'часто перезапускаются: {n}': 'restart often: {n}',
  'Хосты с замечаниями': 'Hosts with findings',
  'docker-сокет у прокси: {list}': 'docker socket in a proxy: {list}',
  'ключи от хоста: {list}': 'host keys: {list}',
  'без restart policy: {list}': 'no restart policy: {list}',
  'старый прокси: {list}': 'old proxy: {list}',
  'Хосты': 'Hosts',
  'по контейнерам ушел алерт: упали или крутятся по кругу': 'an alert went out for containers: down or in a restart loop',
  'Запущено': 'Running',
  'Память контейнеров': 'Container memory',
  'Перезапусков за сутки': 'Restarts in a day',
  'Прокси держит docker-сокет': 'A proxy holds the docker socket',
  'Старый образ прокси': 'Old proxy image',
  'CI-агент: сколько образов у docker внутри, 0 или ошибка - privileged можно убрать':
    'CI agent: how many images the docker inside has, 0 or an error - privileged can go',
  'Operation not permitted в логе - не хватает одного права: добавьте его в cap_add (или устройство в devices), а не privileged обратно.':
    'Operation not permitted in the log - one permission is missing: add it to cap_add (or the device to devices), not privileged back.',
  'caddy из роли caddy-proxy:':
    'caddy from the caddy-proxy role:',
  'docker compose up -d в каталоге проекта, в логе docker-api не должно быть blocked':
    'docker compose up -d in the project directory, the docker-api log must have no blocked',
  'docker в сборках нужен - privileged по назначению, нажмите "по назначению" (безопаснее отдельная ВМ или sysbox)':
    'builds need docker - privileged is intended, press "intended" (safer: a separate VM or sysbox)',
  'docker-api пишет blocked - приложению нужен еще путь API: допишите его в -allowGET.':
    'docker-api logs blocked - the app needs one more API path: add it to -allowGET.',
  'image задан переменной: поставьте в ней {target} (обычно в .env рядом с compose), потом пересоздать сервис':
    'image is set by a variable: set it to {target} (usually in .env next to the compose file), then recreate the service',
  'restart {policy}: после перезагрузки докер его не поднимет':
    'restart {policy}: docker will not start it after a reboot',
  'sudo docker compose up -d <сервис> в папке проекта, откат - вернуть копию и тот же up -d':
    'sudo docker compose up -d <service> in the project directory, to roll back - restore the copy and the same up -d',
  '{list}: docker-сокет целиком, а это root на хосте. Нужен прокси сокета только на чтение, сам сокет из volumes убрать.':
    '{list}: the whole docker socket, which is root on the host. Put a read-only socket proxy in front, drop the socket from volumes.',
  '{what} от {when}, торчит в интернет, нужно {tag}':
    '{what} from {when}, faces the internet, needs {tag}',
  '{what} от {when}, торчит в интернет, своя сборка: только пересобрать':
    '{what} from {when}, faces the internet, a custom build: rebuild only',
  'В compose прокси убрать из volumes строку с docker.sock':
    'In the proxy compose file drop the docker.sock line from volumes',
  'В traefik 2.11 запрос с телом рвется через 60 с: для долгих загрузок верните --entryPoints.<имя>.transport.respondingTimeouts.readTimeout=0.':
    'traefik 2.11 cuts a request with a body after 60 s: for long uploads set --entryPoints.<name>.transport.respondingTimeouts.readTimeout=0.',
  'В {file} у {svc} {paths} на запись: смонтировать только нужный подкаталог или дописать :ro':
    'In {file} {svc} has {paths} writable: mount only the needed subdirectory or add :ro',
  'В {file} у {svc} заменить privileged: true на privileged: false':
    'In {file} for {svc} replace privileged: true with privileged: false',
  'В {file} у {svc} заменить privileged: true на {need}':
    'In {file} for {svc} replace privileged: true with {need}',
  'В {file} у {svc} убрать docker-сокет из volumes':
    'In {file} drop the docker socket from the volumes of {svc}',
  'В {file} у {svc} убрать из cap_add {caps}':
    'In {file} drop {caps} from the cap_add of {svc}',
  'В {file} у сервиса {svc} убрать из volumes строку с docker.sock':
    'In {file} drop the docker.sock line from the volumes of service {svc}',
  'Вместо {paths} смонтировать нужный подкаталог или дописать :ro':
    'Instead of {paths} mount the needed subdirectory or add :ro',
  'Готовый compose для {name} собран из {file}, что меняется:':
    'A ready compose file for {name} is built from {file}, the changes:',
  'Готовый файл соберет helper sockfix-setup, пока руками.':
    'The sockfix-setup helper will build a ready file, by hand for now.',
  'Для {name} готового файла нет: {why}{detail}.':
    'No ready file for {name}: {why}{detail}.',
  'Добавить сервису environment и networks, рядом сервис и сеть docker-api, как в примере':
    'Add environment and networks to the service, and the docker-api service and network next to it, as in the example',
  'Дописать в {file} руками: docker-api (прокси сокета только на чтение) и DOCKER_HOST сервису, GID из stat -c %g /var/run/docker.sock':
    'Add to {file} by hand: docker-api (a read-only socket proxy) and DOCKER_HOST for the service, GID from stat -c %g /var/run/docker.sock',
  'Если контейнер из compose, то же в файле (restart: unless-stopped), иначе следующий up вернет как было':
    'If the container is from compose, the same in the file (restart: unless-stopped), or the next up brings the old policy back',
  'Запомнить коды ответов доменов':
    'Save the response codes of the domains',
  'Копия compose рядом':
    'A copy of the compose file next to it',
  'Копия файла, потом в сервисе заменить privileged: true на privileged: false':
    'Copy the file, then in the service replace privileged: true with privileged: false',
  'Копия файла, потом в сервисе заменить privileged: true на {need}':
    'Copy the file, then in the service replace privileged: true with {need}',
  'Найти compose прокси: каталог, файлы, проект, сервис':
    'Find the proxy compose: directory, files, project, service',
  'Найти compose: папка, файлы, сервис (с новым агентом тут будут готовые команды)':
    'Find the compose: directory, files, service (a newer agent gives ready commands here)',
  'Не из compose: docker pull {target} и пересоздать контейнер той же командой docker run':
    'Not from compose: docker pull {target} and recreate the container with the same docker run',
  'Не из compose: пересоздать той же командой docker run, но без {what}':
    'Not from compose: recreate with the same docker run, but without {what}',
  'Образ старше caddy 2.5: бэкенд начнет получать Via и X-Forwarded-Host, а X-Forwarded-For от клиента отбрасывается.':
    'The image is older than caddy 2.5: the backend starts getting Via and X-Forwarded-Host, and X-Forwarded-For from the client is dropped.',
  'Пересоздать сервисы и сверить, последние строки скажут, ушел ли ключ':
    'Recreate the services and check, the last lines tell whether the key is gone',
  'Поднять FROM в Dockerfile до {target} и пересобрать':
    'Raise FROM in the Dockerfile to {target} and rebuild',
  'Поставить restart unless-stopped':
    'Set restart unless-stopped',
  'Применить, прокси пересоздастся за пару секунд':
    'Apply, the proxy is recreated in a couple of seconds',
  'С caddy 2.11 HTTPS-апстрим ({{upstreams https ...}}) получает Host апстрима, а не сайта: если бэкенду нужен исходный, добавьте header_up Host {hostport}.':
    'Since caddy 2.11 an HTTPS upstream ({{upstreams https ...}}) gets the Host of the upstream, not of the site: if the backend needs the original one, add header_up Host {hostport}.',
  'Сверить: оба контейнера работают, blocked в логе docker-api - 0':
    'Check: both containers run, blocked in the docker-api log is 0',
  'Сверить: сервис работает, лог без ошибок':
    'Check: the service runs, no errors in the log',
  'Сверить: сервис работает, лог без ошибок, коды те же (OK)':
    'Check: the service runs, no errors in the log, same codes (OK)',
  'Своя сборка (задана в {at}): пересобрать на свежем базовом образе':
    'A custom build (set in {at}): rebuild it on a fresh base image',
  'Скачать свежий {target} и пересоздать сервис, прежний образ останется как kervax-old':
    'Pull a fresh {target} and recreate the service, the old image stays as kervax-old',
  'Сменить image в {at} и пересоздать сервис':
    'Change image in {at} and recreate the service',
  'Сменить image на {target} в compose, потом docker compose pull и up -d --no-deps для этого сервиса':
    'Change image to {target} in compose, then docker compose pull and up -d --no-deps for this service',
  'Сменить image на {target} в compose, потом пересоздать сервис':
    'Change image to {target} in compose, then recreate the service',
  'Строку image не правлю сам: {why}.':
    'Not editing the image line myself: {why}.',
  'Убрать docker-сокет из volumes, если нужен список контейнеров - прокси сокета только на чтение (DOCKER_HOST=tcp://docker-api:2375)':
    'Drop the docker socket from volumes, if the app needs the container list - a read-only socket proxy (DOCKER_HOST=tcp://docker-api:2375)',
  'Убрать из cap_add {caps}':
    'Drop {caps} from cap_add',
  'вернуть как было':
    'restore as it was',
  'вернуть прежний compose':
    'restore the old compose file',
  'вернуть прежний compose, старый образ на диске':
    'restore the old compose file, the old image is on disk',
  'вернуть прежний образ':
    'restore the old image',
  'подробнее':
    'details',
  'приложение само управляет контейнерами (CI, деплой) - сокет по назначению, нажмите "по назначению"':
    'the app manages containers itself (CI, deploy) - the socket is intended, press "intended"',
  'строку {env} тоже убрать':
    'drop the {env} line too',
  'Ключи от хоста': 'Host keys',
  'Не поднимутся после перезагрузки': 'Will not start after a reboot',
  'Доступ только на чтение': 'Read-only access',
  'Нет доступа к контейнерам': 'No access to containers',
  // Кубер: сводка и модалка кластера плитками
  'Кластеры': 'Clusters',
  'Ноды готовы': 'Nodes ready',
  'под давлением: {n}': 'under pressure: {n}',
  'без агента: {n}': 'without agent: {n}',
  'все готовы': 'all ready',
  'Поды работают': 'Pods running',
  'падают: {n}': 'failing: {n}',
  'не готовы: {n}': 'not ready: {n}',
  'все работают': 'all running',
  'Кластеры с замечаниями': 'Clusters with findings',
  'Flux встал: {list}': 'Flux stuck: {list}',
  'сроки истекли: {list}': 'expired: {list}',
  'сроки скоро: {list}': 'expiring soon: {list}',
  'задания упали: {list}': 'jobs failed: {list}',
  'Память нод': 'Node memory',
  'Ближайший срок': 'Next expiry',
  'не в Ready: {n}': 'not Ready: {n}',
  'все в Ready': 'all Ready',
  'Нет доступа к kube-api': 'No access to kube-api',
  'поды падают: CrashLoopBackOff, ошибка образа или запуска': 'pods failing: CrashLoopBackOff, image or start error',
  'ноды не в Ready': 'nodes not Ready',
  // Сервисы: сводка, карточка ноды и очереди плитками
  'Ноды с сервисами': 'Nodes with services',
  'типов сервисов: {n}': 'service types: {n}',
  'Базы данных': 'Databases',
  'Веб-серверы': 'Web servers',
  'без монитора: {n}': 'not monitored: {n}',
  'конфиг с ошибкой: {list}': 'config errors: {list}',
  'сертификаты истекают: {list}': 'certificates expiring: {list}',
  'Очереди RabbitMQ': 'RabbitMQ queues',
  'переполнены: {list}': 'overflowing: {list}',
  'Ноды': 'Nodes',
  'переполненные очереди RabbitMQ': 'overflowing RabbitMQ queues',
  'переполненные очереди': 'overflowing queues',
  'Очереди: выше порога': 'Queues: above the threshold',
  'Очередь выше порога': 'Queue above the threshold',
  'конфиг веб-сервера не проходит проверку': 'web server config fails the check',
  'Сервисов': 'Services',
  'Сайтов': 'Sites',
  'все на мониторинге': 'all monitored',
  'без 5xx': 'no 5xx',
  'Базы': 'Databases',
  'Переполнено очередей: {n} (от {m} сообщений). Обычно это отставший или упавший консьюмер.': 'Overflowing queues: {n} (from {m} messages). Usually a consumer is lagging or down.',
  'самые нагруженные: {list}': 'busiest: {list}',
  'Очередей': 'Queues',
  'С сообщениями': 'With messages',
  'Сообщений': 'Messages',
  'Переполнены': 'Overflowing',
  'от {n} сообщений': 'from {n} messages',
  'Алерт по очередям': 'Queue alerts',
  // Бэкапы: сводка плитками, карточки бэкап-сервера и клиента
  'Клиенты': 'Clients',
  'ошибка: {n}': 'failed: {n}',
  'не свежий: {n}': 'not fresh: {n}',
  'свой бэкап с проблемой: {n}': 'own backup with a problem: {n}',
  'свой бэкап: {n}': 'own backup: {n}',
  'с проблемой: {n}': 'with a problem: {n}',
  'не требуется: {n}': 'not required: {n}',
  'у всех нод есть': 'every node has one',
  // плитки сводки Docker, Кубера, Сервисов и Бэкапов как фильтр списка
  'снять фильтр': 'clear filter',
  'показать в списке': 'filter the list',
  'фильтр: {f}': 'filter: {f}',
  'снять фильтр: {f}': 'clear filter: {f}',
  'остановлены: {list}': 'stopped: {list}',
  'упали: {list}': 'down: {list}',
  'по кругу: {list}': 'restart loop: {list}',
  'часто перезапускаются: {list}': 'restart often: {list}',
  'остановленные контейнеры': 'stopped containers',
  'аварии': 'failures',
  'хосты с замечаниями': 'hosts with findings',
  'Все контейнеры запущены.': 'All containers are running.',
  'Аварий нет.': 'No failures.',
  'Замечаний нет.': 'No findings.',
  'не готовы: {list}': 'not ready: {list}',
  'под давлением: {list}': 'under pressure: {list}',
  'без агента: {list}': 'without agent: {list}',
  'падают: {list}': 'failing: {list}',
  'ноды не готовы или с замечаниями': 'nodes not ready or with findings',
  'поды не работают': 'pods not running',
  'кластеры с замечаниями': 'clusters with findings',
  'Все ноды готовы.': 'All nodes are ready.',
  'Все поды работают.': 'All pods are running.',
  'базы данных': 'databases',
  'веб-серверы': 'web servers',
  'Баз данных не найдено.': 'No databases found.',
  'Веб-серверов не найдено.': 'No web servers found.',
  'Очередей нет.': 'No queues.',
  'восстановление не проверялось': 'restore not checked',
  'восстановление не прошло': 'restore check failed',
  'восстановление с замечаниями': 'restore check with warnings',
  'клиенты': 'clients',
  'без бэкапа': 'no backup',
  'бэкап-серверы': 'backup servers',
  'без пройденной проверки восстановления': 'no passed restore check',
  'Клиентов с бэкапом нет.': 'No backup clients.',
  'Бэкап есть у всех нод.': 'Every node has a backup.',
  'Бэкап-серверов нет.': 'No backup servers.',
  'Восстановление проверено у всех клиентов.': 'Restore checked for every client.',
  'диск до {p}%': 'disk up to {p}%',
  'Восстановление проверено': 'Restore verified',
  'не прошло: {n}': 'failed: {n}',
  'с замечаниями: {n}': 'with warnings: {n}',
  'свой бэкап с проблемой': 'own backup has a problem',
  'свой бэкап работает': 'own backup works',
  'диск занят на {p}%': 'disk {p}% used',
  'с дампами': 'with dumps',
  'обычно {s}': 'usually {s}',
  'не прошло': 'failed',
  'замечания': 'warnings',
  'проверено': 'verified',
  'есть {v}': '{v} available',
  'Репозитории': 'Repositories',
  'все ок': 'all ok',
  'Протокол': 'Protocol',
  'порт {p}': 'port {p}',
  'без TLS': 'no TLS',
  'для чистки и проверки': 'for prune and check',
  'Процент - доля времени за минуту, когда хоть один процесс ждал ресурс. "Все стоят" ядро считает по каждому CPU отдельно (его процессы ждут ресурс, и выполнять на нем нечего), поэтому на многоядерном сервере это не значит, что стоял весь сервер. D-state - процессы, которые ждут диск или сеть и не прерываются, зависший - тот, что не просыпается дольше 2 минут.':
    'The percentage is the share of the last minute when at least one process waited for the resource. The kernel counts "all stalled" per CPU (its processes wait for the resource and there is nothing else to run on it), so on a multi-core server it does not mean the whole server stood still. D-state processes wait for disk or network and cannot be interrupted, a stuck one has not woken up for more than 2 minutes.',
  // утечка памяти
  'Растет память': 'Memory keeps growing',
  'Минимум памяти за сутки растет 4 суток подряд. Пила сборщика мусора и всплески минимум не двигают, а утечка поднимает его каждый день. Алерт - только если при таком росте свободной памяти сервера не хватит на неделю.':
    'The daily memory minimum has grown 4 days in a row. Garbage collector sawtooth and spikes do not move the minimum, a leak raises it every day. Alerts only if at this rate the free memory of the server lasts less than a week.',
  'процесс': 'process',
  'в сутки': 'a day',
  'памяти хватит на {d} сут': 'memory lasts {d} days',
  'памяти хватит больше чем на месяц': 'memory lasts more than a month',
  'Память: растет день за днем': 'Memory: grows day after day',
  'рост памяти': 'memory growth',
  // у своего лимита памяти
  'У лимита памяти': 'At the memory limit',
  'Контейнеры, поды и юниты, занявшие свой лимит памяти. Кэш заполняет любой лимит, это нормально. Беда - когда при этом растет давление памяти (PSI full) или major faults: ядро выкидывает код и данные, процесс читает их с диска снова и тормозит, но не падает. Алерт - если так держится 10 минут.':
    'Containers, pods and units that filled their memory limit. Page cache fills any limit, that is normal. The trouble is when memory pressure (PSI full) or major faults grow at the same time: the kernel evicts code and data, the process reads them from disk again and slows down without crashing. Alerts if it lasts 10 minutes.',
  'Память: уперся в лимит': 'Memory: hit its limit',
  'у лимита памяти, так задумано': 'at the memory limit, intended',
  'используется {p}%': '{p}% used',
  'major faults {n}/с': 'major faults {n}/s',
  'refault {n}/с': 'refault {n}/s',
  'тормозит, но не падает': 'slow, but not crashing',
  'поднимите лимит памяти контейнера (mem_limit)': 'raise the container memory limit (mem_limit)',
  'поднимите limit памяти пода': 'raise the pod memory limit',
  'поднимите MemoryMax / MemoryHigh юнита': 'raise MemoryMax / MemoryHigh of the unit',
  'Так задумано: этот лимит памяти оставить как есть? Панель перестанет алертить по нему.':
    'Intended: keep this memory limit as it is? The panel will stop alerting about it.',
  'тормозил {from} - {to}': 'was slow {from} - {to}',
  'давление есть, ждем 10 минут': 'under pressure, waiting 10 minutes',
  'давления нет, это кэш': 'no pressure, it is cache',
  'у лимита памяти {l}': 'at memory limit {l}',
  'тормозит, но не падает. Подробности - в карточке сервера, раздел Память':
    'slow, but not crashing. Details are in the server card, Memory section',
  // часто перезапускается
  'Docker: часто перезапускается': 'Docker: restarts often',
  'Kubernetes: под часто перезапускается': 'Kubernetes: pod restarts often',
  'перезапуски пода': 'pod restarts',
  'перезапуски контейнера': 'container restarts',
  '{d} за сутки': '{d} a day',
  'перезапусков за сутки: {d}, за все время: {n}. Падает раз в час-другой: между падениями работает, но теряет соединения и очереди':
    'restarts in a day: {d}, in total: {n}. It falls over every hour or two: works in between but loses connections and queues',
  'Kubernetes: CronJob': 'Kubernetes: CronJob',
  'Время сервера - только предупреждения': 'Server time - warnings only',
  'Время сервера - предупр. и проблемы': 'Server time - warnings and problems',
  'Коннекты СУБД': 'DB connections',
  'RabbitMQ: очереди': 'RabbitMQ: queues',
  'Kubernetes: сроки': 'Kubernetes: expiry dates',
  'Flux: доставка встала': 'Flux: delivery stopped',
  'Flux: репозиторий чартов недоступен': 'Flux: chart repository unavailable',
  'чистка не работает': 'cleanup is not working',
  '{name}: чистка не работает - {list}': '{name}: cleanup is not working - {list}',
  'чистка и проверка не открывают репозиторий': 'cleanup and check cannot open the repository',
  'Бэкап: дамп СУБД': 'Backup: DB dump',
  'Бэкап: место под дампы': 'Backup: dump disk space',
  'Бэкап: дамп-CronJob': 'Backup: dump CronJob',
  'Бэкап: свои бэкапы': 'Backup: own backups',
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


  'Не бэкапить это — убрать из проблем': 'Do not back this up — clear from issues',
  'не бэкапится намеренно, в проблемы не идет': 'not backed up on purpose, not counted as an issue',
  'Вернуть в проблемы: панель снова будет напоминать, что это не в бэкапе': 'Move back to issues: the panel will remind again that this is not backed up',

  'Настраиваю и снимаю пробный дамп — подождите, на большой базе это может занять до минуты…':
    'Setting up and taking a test dump — please wait, on a large database this can take up to a minute…',
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
  'Читается на сервере без паролей: config (валидность), снапшоты, размер, свежесть, лок, политика хранения. Опоздавшие и устаревшие - красным и алертят. То, что хранится как есть (сервера больше нет, разовый бэкап, проект заморожен), уберите в архив (🗄), временное приглушите (🔕).':
    'Read on the server without passwords: config (validity), snapshots, size, freshness, lock, retention. Late and stale ones are red and alert. Move what is kept as is (the server is gone, a one-off backup, a frozen project) to the archive (🗄), mute what is temporary (🔕).',
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
  'подходящего пароля в env нет - снимаю висячие локи файлами': 'no matching password in env - removing stale lock files',
  'готово, живые локи (моложе 30 минут) не тронуты': 'done, live locks (younger than 30 minutes) left alone',
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
  'Поиск: нода, IP, группа…': 'Search: node, IP, group…',
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
  'Поиск: хост, IP, группа, нода, под, воркоад…': 'Search: host, IP, group, node, pod, workload…',
  'Kubernetes не найден ни на одном сервере. Агент определяет кластер сам (k0s/k3s/microk8s/kubeadm); если он есть, но раздел пуст — обновите агент.':
    'Kubernetes not found on any server. The agent detects the cluster itself (k0s/k3s/microk8s/kubeadm); if it exists but the section is empty — update the agent.',
  'Поды ({n})': 'Pods ({n})',
  'Ворклоады ({n})': 'Workloads ({n})',
  'Ноды ({n})': 'Nodes ({n})',
  'Подов нет': 'No pods',
  'Ворклоадов нет': 'No workloads',
  'ноды {r}/{n}': 'nodes {r}/{n}',
  'ноды {nr}/{nn} · поды {pr}/{pn} · ns {ns}': 'nodes {nr}/{nn} · pods {pr}/{pn} · ns {ns}',
  'Rollout restart «{n}» ({k})?': 'Rollout restart “{n}” ({k})?',
  'Rollout restart': 'Rollout restart',
  'Перезапустить под «{n}» (удалить — контроллер пересоздаст)?':
    'Restart pod “{n}” (delete — the controller will recreate it)?',
  'Перезапустить под': 'Restart pod',
  'перезапусков: {n}': 'restarts: {n}',
  'перезапусков за все время: {n} (само по себе не проблема, тревога - только CrashLoopBackOff)':
    'restarts over lifetime: {n} (not a problem by itself; only CrashLoopBackOff is)',
  'Логи': 'Logs',
  'healthcheck: unhealthy': 'healthcheck: unhealthy',
  'перезапусков (RestartCount): {n}': 'restarts (RestartCount): {n}',
  'перезапусков за все время (RestartCount): {n} (само по себе не проблема, тревога - на устойчивый crash-loop)':
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
  'Под уже пересоздан или удален, список обновлен': 'The pod has already been recreated or deleted, the list is refreshed',
  'Под уже удален, его лога больше нет': 'The pod is already deleted, its log is gone',
  'Контейнер уже пересоздан или удален, список обновлен': 'The container has already been recreated or deleted, the list is refreshed',
  'Учетная запись только для просмотра: изменения запрещены': 'View-only account: changes are not allowed',
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
  'OOM-киллы': 'OOM kills',
  'последние киллы': 'recent kills',
  'процесс неизвестен': 'process unknown',
  'OOM-killer': 'OOM killer',
  'контейнер {n}': 'container {n}',
  'под {n}': 'pod {n}',
  'имя неизвестно': 'name unknown',
  'сессия {n}': 'session {n}',
  'весь сервер': 'whole server',
  'лимит cgroup': 'cgroup limit',
  'systemd-oomd': 'systemd-oomd',
  'Нехватка памяти (OOM)': 'Out of memory (OOM)',
  // мьют OOM по виновнику или целиком
  'OOM из-за': 'OOM caused by',
  'OOM из-за {n}': 'OOM caused by {n}',
  'процесс {n}': 'process {n}',
  // после "из-за" (oomCulpritLabel gen)
  'контейнера {n}': 'container {n}',
  'пода {n}': 'pod {n}',
  'процесса {n}': 'process {n}',
  'любой OOM': 'any OOM',
  'не алертить:': 'do not alert:',
  'Не алертить': 'Do not alert',
  'из-за {n}': 'caused by {n}',
  'OOM из-за других служб будут приходить': 'OOM caused by other services still alerts',
  'не алертить об OOM из-за {n}': 'do not alert on OOM caused by {n}',
  'не алертить об OOM': 'do not alert on OOM',
  'Память занимал:': 'Memory held by:',
  'за 30 дней: {n}': 'last 30 days: {n}',
  'Кто занимал память и какие службы задело, видно с helper units-setup 0.5. Пока только счетчик агента:': 'Who held the memory and which services were hit shows with helper units-setup 0.5. For now only the agent counter:',
  'Убиты': 'Killed',
  'Процесс': 'Process',
  'Чей': 'Owner',
  'Больше всего памяти перед убийством': 'Most memory before the kill',
  'лимит cgroup {v}': 'cgroup limit {v}',
  'RAM {v}': 'RAM {v}',
  'занято процессами {v}': 'used by processes {v}',
  'кэш файлов {v}': 'file cache {v}',
  'общая память {v}': 'shared memory {v}',
  'swap нет': 'no swap',
  'swap занят {u} из {m}': 'swap used {u} of {m}',
  'Задело юниты:': 'Units hit:',
  'убит процесс': 'a process killed',
  'Память не досталась (это не виновники): {list}': 'Did not get memory (not the culprits): {list}',
  'OOM за 7 дней': 'OOM in 7 days',
  'последний {d}': 'last {d}',
  'убит OOM killer': 'killed by the OOM killer',
  'эпизод OOM': 'OOM episode',
  'Менеджер сессии пользователя, а не служба: поднимется сам при следующем входе, в упавшие не считается': 'The user session manager, not a service: it comes back on the next login and is not counted as failed',
  'IP-адреса домена': 'Domain IP addresses',
  'Время ответа · IP {ip}': 'Response time · IP {ip}',
  'Показать время ответа этого IP': 'Show this IP’s response time',
  'У домена один адрес: сравнивать нечего, его проверяет обычная проверка. Разбивка появится, когда адресов станет больше.':
    'The domain has a single address: nothing to compare, the regular check covers it. The breakdown appears once there are more addresses.',
  'Разбивки по адресам еще нет: появится после ближайшей проверки.': 'No per-address breakdown yet: it appears after the next check.',
  'у домена один адрес': 'the domain has one address',
  'Показать': 'Show',
  'IP сервера (опционально)': 'Server IP (optional)',
  'Нужен вида http://логин:пароль@хост:порт (подходят и https://, socks5://), без пробелов и кавычек.':
    'Use the form http://user:password@host:port (https:// and socks5:// work too), no spaces or quotes.',
  'агент ходит в панель через прокси': 'the agent reaches the panel through a proxy',
  'Напрямую': 'Direct',
  'Через ретранслятор': 'Through a relay',
  'Через локацию': 'Through a location',
  'Свой прокси': 'Own proxy',
  'helper не поставлен': 'helper not installed',
  'helper еще не выходил на связь': 'helper has not reported yet',
  'давно не отвечал': 'silent for a while',
  'Как агент пойдет в панель': 'How the agent reaches the panel',
  'Сервер сам достает панель': 'The server reaches the panel by itself',
  'Адрес режут по пути к панели': 'The address is blocked on the way to the panel',
  'Через прокси локации из окна Локации': 'Via a location proxy from the Locations window',
  'Ваш http или socks5 прокси': 'Your own http or socks5 proxy',
  'Выполните на сервере под root': 'Run on the server as root',
  'токен показывается один раз': 'the token is shown only once',
  'Сначала заполните поля выше: прокси попадет в команду': 'Fill in the fields above first: the proxy goes into the command',
  'Если curl упал по таймауту, подождите минуту и повторите: панель открывает адрес новой ноды не сразу':
    'If curl timed out, wait a minute and run it again: the panel opens the new node address with a short delay',
  'Агент сам настроит Docker, Kubernetes и доступ к бэкапам': 'The agent sets up Docker, Kubernetes and backup access by itself',
  'Docker через прокси только для чтения (read-only proxy), Kubernetes через узкий сервисный аккаунт (SA), плюс доступ к бэкапам. Вручную доделывать ничего не нужно. Без авто-настройки: допишите в конец команды --no-auto.':
    'Docker through a read-only proxy, Kubernetes through a narrow service account (SA), plus backup access. Nothing to finish by hand. To skip auto-setup, add --no-auto at the end of the command.',
  'Допишите в конец команды любое имя этого инстанса, например main. Агенты будут работать рядом, каждый со своей панелью.':
    'Add any name for this instance at the end of the command, for example main. The agents will run side by side, each with its own panel.',
  '{host}, v{ver}, метрики уже идут': '{host}, v{ver}, metrics are coming in',
  'Ждем первый отчет агента': 'Waiting for the first agent report',
  'Агент на связи': 'Agent is online',
  'статус сменится здесь сам': 'the status updates here by itself',
  'Ретранслятор': 'Relay',
  '— выберите —': '— choose —',
  'Сервер сразу попадает в список адресов, которые ретранслятор пускает: helper откроет ему порт в течение минуты.': 'The server goes straight into the list of addresses the relay lets in: its helper opens the port for it within a minute.',
  'Ретрансляторов нет. Включите "Ретранслятор для агентов" в настройках сервера, который сам достает до панели.': 'No relays yet. Switch on "Relay for agents" in the settings of a server that reaches the panel by itself.',
  'Локация': 'Location',
  'Через прокси локации. IP сервера должен быть разрешен в самом прокси, если он пускает не всех.': 'Through the location proxy. The server IP has to be allowed in that proxy, if it does not let everyone in.',
  'Локаций с адресом прокси нет: их заводят в окне Локации.': 'No locations with a proxy address: they are added in the Locations window.',
  'Он попадет в команду и в конфиг агента на сервере, панель его не хранит.': 'It goes into the command and the agent config on the server; the panel does not store it.',
  'Внешний IP сервера': 'Server external IP',
  'опционально': 'optional',
  'IP сервера указан неверно': 'The server IP is not valid',
  'Через прокси панель видит адрес прокси, а не сервера: страна и проверки снаружи идут по этому адресу.': 'Through a proxy the panel sees the proxy address, not the server: the country and outside checks use this address.',
  'Подставляю в команду…': 'Putting it into the command…',
  'Ретранслятор для агентов': 'Relay for agents',
  'Сервер станет прокси до панели для агентов, чей адрес режут на пути к ней. Пускает только до панели, только с адресов серверов, выбравших его в окне команды установки, и только с логином. Ставит helper relay-setup (появится в "Требует действий"), нужен python3. Включить можно, только если сервер сам ходит в панель напрямую.': 'The server becomes a proxy to the panel for agents whose address is blocked on the way to it. It lets through only to the panel, only from the servers that chose it in the install command window, and only with a login. The relay-setup helper installs it (it shows up in "Action needed"), python3 is required. It can be switched on only if the server reaches the panel directly.',
  'Порт ретранслятора': 'Relay port',
  'без фаервола: адреса проверяет сам прокси': 'no firewall: the proxy checks the addresses itself',
  'Работает: порт {port}, серверов {n}, фаервол {fw}, соединений {c}': 'Working: port {port}, servers {n}, firewall {fw}, connections {c}',
  'Helper relay-setup еще не поставлен: он в "Требует действий", ставится плейбуком kervax_helpers.yml.': 'The relay-setup helper is not installed yet: it is in "Action needed", the kervax_helpers.yml playbook installs it.',
  'Helper поставлен, ждем его первого обращения к панели (раз в минуту).': 'The helper is installed, waiting for its first call to the panel (once a minute).',
  'Helper давно не обращался к панели: сервер не на связи или не достает до нее.': 'The helper has not called the panel for a while: the server is offline or cannot reach the panel.',
  'Ошибка ретранслятора: {err}': 'Relay error: {err}',
  'Агент не достает до панели? Поставьте его через ретранслятор или локацию.': 'The agent cannot reach the panel? Install it through a relay or a location.',
  'Выбрать путь': 'Choose a path',
  'Ретранслятор работает только с панелью по https': 'The relay works only with a panel on https',
  'Агент сервера еще не выходил на связь: ретранслятор должен сам доставать до панели': 'The server agent has not reported yet: a relay has to reach the panel by itself',
  'Сервер не на связи: ретранслятор должен сам доставать до панели': 'The server is offline: a relay has to reach the panel by itself',
  'Агент этого сервера сам ходит в панель через прокси: напрямую сервер до панели не достает': 'This server agent reaches the panel through a proxy itself: the server cannot reach the panel directly',
  'Не известен внешний адрес сервера: укажите IP сервера в настройках': 'The server external address is unknown: set the server IP in its settings',
  'Выберите ретранслятор': 'Choose a relay',
  'На этом сервере ретранслятор выключен': 'The relay is switched off on that server',
  'Укажите внешний IP сервера: ретранслятор пускает только известные адреса': 'Set the server external IP: the relay lets in only known addresses',
  'Выберите локацию с адресом прокси': 'Choose a location with a proxy address',
  'На сервере уже стоит агент другой панели?': 'Already running an agent for another panel on this server?',
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
  'Сервер с таким именем уже есть ({state}). Панель различает ноды по имени.':
    'A server with this name already exists ({state}). The panel tells nodes apart by name.',
  'Этот адрес уже у сервера {name}. Если это та же нода, второй раз её заводить не нужно.':
    'This address already belongs to {name}. If it is the same node, there is no need to add it again.',
  'Тот же внешний адрес у {names}. Для серверов за одним NAT или пробросом портов это нормально.':
    'Same external address as {names}. That is normal for servers behind one NAT or port forwarding.',
  'на связи': 'online',
  'запросов в минуту: {n}': 'requests per minute: {n}',
  'ошибок 5xx в минуту': '5xx errors per minute',
  '5xx/мин': '5xx/min',
  '5xx: {n} ({p}%)': '5xx: {n} ({p}%)',
  'без кода ответа': 'no status code',
  'Веб': 'Web',
  'остальные': 'other',
  'строки ошибок ({n})': 'error lines ({n})',
  'скачать .txt': 'download .txt',
  'Веб: ошибки 5xx': 'Web: 5xx errors',
  'Kubernetes: поды': 'Kubernetes: pods',
  '5xx по логу': '5xx for log',
  'не алертить по этому логу': 'mute alerts for this log',
  'не алертить по этому логу:': 'mute alerts for this log:',
  'не алертит, постоянно': 'muted, permanently',
  'не алертит {t}': 'muted {t}',
  'Выпустить новый токен? Старый сразу перестанет работать.':
    'Issue a new token? The old one stops working right away.',
  'Отключить доступ ansible? Плагин инвентаря перестанет получать ноды этой панели.':
    'Turn off ansible access? The inventory plugin will stop getting the nodes of this panel.',
  'Токен показывается один раз. Добавьте эти строки в inventories/kervax_panels.yml, в список panels:':
    'The token is shown only once. Add these lines to inventories/kervax_panels.yml, under panels:',
  'Отключить': 'Turn off',
  'Выпустить новый токен': 'Issue a new token',
  'Выпустить токен': 'Issue a token',
  'За сутки ответов 5xx не было.': 'No 5xx responses in the last 24 hours.',
  'Ответы 5xx за сутки по логам веб-сервера': '5xx responses in the last 24 hours, by web server log',
  '{n} ошибок за {m} мин, пик {p}/мин': '{n} errors over {m} min, peak {p}/min',
  'коды': 'codes',
  'где падает': 'failing paths',
  'почему': 'why',
  'не принимает соединения': 'refuses connections',
  'не успевает принимать соединения: очередь сокета полна': 'cannot keep up with connections: the socket queue is full',
  'не запущен: сокета нет': 'is not running: no socket',
  'не пускает: нет прав на сокет': 'denies access: no permission on the socket',
  'не принял соединение вовремя': 'did not accept the connection in time',
  'не ответил вовремя': 'did not answer in time',
  'без живых серверов': 'has no live servers',
  'недоступен по сети': 'is unreachable over the network',
  'закрыл соединение, не ответив': 'closed the connection without an answer',
  'сбросил соединение': 'reset the connection',
  'прислал заголовок больше буфера': 'sent a header bigger than the buffer',
  'не прошел TLS': 'failed TLS',
  'ответил не по протоколу': 'answered off protocol',
  'ошибка связи': 'connection error',
  'по доменам': 'by domain',
  'с {a} по {b}': 'from {a} to {b}',
  'Алерт по ошибкам 5xx, % за 15 минут (0 = выкл)': '5xx error alert, % over 15 minutes (0 = off)',
  'Веб-сервер': 'Web server',
  'запросов в минуту': 'requests per minute',
  'запросов/мин': 'requests/min',
  'Добавить сервер': 'Add server',
  'Создать': 'Create',
  'напр. Прод / БД': 'e.g. Prod / DB',
  'Сервер создан': 'Server created',
  'Скопировать команду': 'Copy command',
  'Скопировано ✓': 'Copied ✓',
  'аптайм': 'uptime',
  'агент': 'agent',
  'Обновить сначала одну ноду (canary), потом остальные':
    'Update one node first (canary), then the rest',
  // "Обновить все" на главной и у баннера "Серверов" (FleetUpdate.tsx)
  'Обновить все': 'Update all',
  'ноды': 'nodes',
  'ноду': 'node',
  'ноде': 'node',
  'нодах': 'nodes',
  'Агент {v}: отстают нод - {n}': 'Agent {v}: {n} node(s) behind',
  'Обновить {n} {nodes}': 'Update {n} {nodes}',
  'Точно? {n} {nodes}': 'Sure? {n} {nodes}',
  'Агент {v} на всех нодах: {n}': 'Agent {v} on all nodes: {n}',
  'Агент {v}: обновились {done} из {n}, остальные сами не обновятся':
    'Agent {v}: {done} of {n} updated, the rest will not update on their own',
  'Повторить: {n}': 'Retry: {n}',
  'качают: {list}': 'downloading: {list}',
  'Снять обновление с тех, кто еще не обновился': 'Cancel the update on nodes that have not updated yet',
  'стоп': 'stop',
  '{helper} не стоит': '{helper} not installed',
  'helper\'ы: обновить на {n} {nodes}': 'helpers: update on {n} {nodes}',
  'helper\'ы обновлены на всех нодах: {n}': 'helpers updated on all nodes: {n}',
  'копировать еще раз': 'copy again',
  'helper\'ы: команда скопирована, запустите ее в репозитории ansible':
    'helpers: the command is copied, run it in your ansible repository',
  'helper\'ы: обновились {done} из {n}': 'helpers: {done} of {n} updated',
  'прогона так и не было, ждут: {list}': 'no run yet, waiting: {list}',
  'после прогона все еще старые: {list} - смотрите вывод плейбука':
    'still outdated after the run: {list} - check the playbook output',
  'Точно? агент на {a} + helper\'ы на {h}': 'Sure? agent on {a} + helpers on {h}',
  'запустил обновление helper\'ов': 'started a helper update',
  'canary - 1 нода': 'canary - 1 node',
  'Агент {v}: обновились {done} из {n}': 'Agent {v}: {done} of {n} updated',
  'агент отклонил обновление: {err}': 'the agent rejected the update: {err}',
  'сервер выключен в панели - обновление ему не ставится': 'the server is disabled in the panel - no update is set for it',
  'раскатку на этой ноде отменили': 'the rollout was cancelled on this node',
  'не на связи - обновится сам, когда агент вернется': 'offline - updates on its own when the agent is back',
  'за 15 минут не обновился - на ноде: journalctl -u kervax-agent -n 50; если агент очень старый и сам не обновляется, переустановите его командой из карточки':
    'not updated in 15 minutes - on the node: journalctl -u kervax-agent -n 50; if the agent is very old and does not self-update, reinstall it with the command from the server card',
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
  // одно слово на четыре смысла: загрузка GPU и диска - utilization, у swap и ответа свои ключи
  'загрузка': 'utilization',
  'из swap': 'swap in',
  'в swap': 'swap out',
  'загрузка ответа': 'download',
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
  // карточка сервера: меню разделов и сводка
  'Сводка': 'Overview',
  'Ресурсы': 'Resources',
  'Безопасность': 'Security',
  'SSH и доступы': 'SSH and access',
  'Службы': 'Units',
  'Изменения': 'Changes',
  'Зависимости': 'Dependencies',
  'События': 'Events',
  'Сервер по областям': 'Server by area',
  'Нагрузка': 'Apps',
  'Поды': 'Pods',
  'Перезапуски': 'Restarts',
  // раздел "Перезапуски" кластера (KubeRestarts.tsx)
  'перезапускались за сутки: {n}': 'restarted in a day: {n}',
  'прошлый запуск': 'previous run',
  'Лог прошлого запуска': 'Previous run log',
  'Лог прошлого запуска и описание пода:': 'Previous run log and pod description:',
  'Не алертить о пачках': 'Do not alert about bursts',
  'Не алертить о перезапусках': 'Do not alert about restarts',
  'Снова алертить': 'Alert again',
  'Снова алертить о пачках': 'Alert about bursts again',
  'ждали: CPU {c}%, память {m}%, диск {d}%': 'waited: CPU {c}%, memory {m}%, disk {d}%',
  'Перезапуски вместе с подами других ворклоадов: беда ноды или kube-api, в счет пода не идут':
    'Restarts together with pods of other workloads: a node or kube-api problem, not counted against the pod',
  'Почему:': 'Why:',
  'Причину прошлого завершения агент еще не прислал.': 'The agent has not sent how the previous run ended yet.',
  'Причину прошлого завершения присылает агент {v} и новее, у этой ноды {cur}. Пока смотрите командами ниже.':
    'Agent {v} or newer sends how the previous run ended, this node has {cur}. Use the commands below for now.',
  'в пачках: {n}': 'in bursts: {n}',
  'завершился в {t}': 'ended at {t}',
  'заглушен': 'muted',
  'когда: {list}': 'when: {list}',
  'лимит памяти {v}': 'memory limit {v}',
  'не алертит': 'no alerts',
  'плановый': 'planned',
  'повод для алерта': 'alerts',
  'поды перезапускаются пачками': 'pods restart in bursts',
  'реже порога': 'below threshold',
  'Завершенные': 'Completed',
  'Ворклоады': 'Workloads',
  'Кластер': 'Cluster',
  'Сроки': 'Expiry',
  'Кластер по областям': 'Cluster by area',
  'Нода по областям': 'Node by area',
  'Сервисы по областям': 'Services by area',
  'Бэкап по областям': 'Backup by area',
  'Прогоны': 'Runs',
  'Свои бэкапы': 'Own backups',
  'Расписание и пути': 'Schedule and paths',
  'Архив': 'Archive',
  'Чистка': 'Cleanup',
  'Нода': 'Node',
  'Проверка': 'Check',
  'Сигнал': 'Signal',
  'Записи DNS': 'DNS records',
  'Пути и адреса': 'Paths and addresses',
  'Сертификат и домен': 'Certificate and domain',
  'История': 'History',
  'Связи': 'Links',
  'Срок сертификата': 'Certificate expiry',
  '{list}: сервер не на связи, алерты монитора ждут его': '{list}: server is offline, the monitor alerts wait for it',
  'Доступность за {w}': 'Availability over {w}',
  'Клик по локации - ее время ответа на графике': 'Click a location to see its response time on the chart',
  'Когда уходит алерт': 'When the alert goes out',
  'Монитор по областям': 'Monitor by area',
  'Сейчас': 'Now',
  'Удалить монитор': 'Delete monitor',
  'адресов домена: {n}': 'domain addresses: {n}',
  'видят все: {list}': 'all see it: {list}',
  'все виды включены': 'all kinds are on',
  'за {w} не было, последний {t}': 'none over {w}, last {t}',
  'за {w}: {n}, последний {t}': 'over {w}: {n}, last {t}',
  'зависит от {list}': 'depends on {list}',
  'известная проблема: {text}': 'known issue: {text}',
  'монитор выключен': 'monitor is off',
  'не отвечает из: {list}': 'no response from: {list}',
  'не отвечает: {list}': 'no response: {list}',
  'медленно отвечает: {list}': 'slow to respond: {list}',
  'не слать: {list}': 'muted: {list}',
  'обычно {ms} мс': 'usually {ms} ms',
  'осталось {n} дн.': '{n} d left',
  'ответ': 'response',
  'показать основную проверку': 'show the main check',
  'после {n} неудачных проверок подряд, это около {m} мин': 'after {n} failed checks in a row, about {m} min',
  'последний сигнал {ago}': 'last signal {ago}',
  'последняя проверка {ago}': 'last check {ago}',
  'предупреждать за {d} дн.': 'warn {d} d ahead',
  'проверен': 'checked',
  'путей: {n}': 'paths: {n}',
  'раз в': 'every',
  'с локаций': 'from locations',
  'сейчас {ms} мс': 'now {ms} ms',
  'на каком сервере работает, панель не определила': 'the panel could not tell which server runs it',
  'связей нет: ни за кем не стоит, мониторов на нем нет': 'no links: not behind another server, no monitors on it',
  'СУБД': 'DBMS',
  'заглушено: {text}': 'muted: {text}',
  'docker-сокет у {list}': 'docker socket in {list}',
  'залочен больше суток: {list}': 'locked for over a day: {list}',
  'состав баз не виден': 'databases not visible',
  'сигнал раз в': 'signal every',
  'rest-server {v}, есть {t}': 'rest-server {v}, {t} is out',
  'rest-server остановлен': 'rest-server is stopped',
  'Прирост за прогон': 'Growth per run',
  'Проверка целостности': 'Integrity check',
  'больше всех {name}: {s}': 'largest {name}: {s}',
  'данные': 'data',
  'за 14 дней': 'over 14 days',
  'залочен больше суток, чистка не идет: {list}': 'locked for over a day, cleanup is not running: {list}',
  'клиенты ходят по HTTP, без TLS': 'clients connect over HTTP, without TLS',
  'на сервере бэкапов старый restic {v} - обновите': 'the backup server runs an old restic {v} - update it',
  'репозиториев с проблемой: {n} ({list})': 'repositories with a problem: {n} ({list})',
  'репозиториев: {n}, {sz}': 'repositories: {n}, {sz}',
  'репозиториев: {n}, все в порядке': 'repositories: {n}, all fine',
  'статистики репозиториев нет: включите ее (backupserver-setup)': 'no repository stats: enable them (backupserver-setup)',
  'чистка и проверка идут у всех': 'cleanup and checks run for all',
  'чистка не работает: {list}': 'cleanup is broken: {list}',
  'Настройка': 'Settings',
  'Прирост по прогонам': 'Growth per run',
  'helper на ноде устарел': 'the helper on the node is outdated',
  'restic {v}, есть {t}': 'restic {v}, {t} is available',
  'restic не найден': 'restic not found',
  '{db} - нужен отдельный дамп': '{db}: needs a separate dump',
  'Проверки восстановления еще не было: ее делает helper бэкапа на ноде (backup-setup 0.34 и новее).': 'No restore check yet: the backup helper on the node runs it (backup-setup 0.34 and newer).',
  'баз не найдено': 'no databases found',
  'бэкап завершился с ошибкой': 'backup finished with an error',
  'бэкап завершился с ошибкой: {why}': 'backup finished with an error: {why}',
  'Почему ошибка': 'Why it failed',
  'текст ошибки restic': 'restic error text',
  'без причины': 'no reason given',
  'настройка бэкапа не прошла: {why}': 'backup setup failed: {why}',
  'Переделать настройку': 'Redo the setup',
  'Удалить настройку': 'Remove the setup',
  'нужны агент {a} и helper backup-setup {h} - обновите их на сервере': 'needs agent {a} and helper backup-setup {h} - update them on the server',
  'Снять с {name} то, что успела поставить настройка бэкапа (юниты, env с паролем, скрипт, restic панели)? Репозиторий на бэкап-сервере останется.': 'Remove from {name} what the backup setup managed to install (units, the env file with the password, the script, the panel restic)? The repository on the backup server stays.',
  'на сервере уже есть бэкап - отметьте "переустановить поверх"': 'the server already has a backup - tick "reinstall over it"',
  'Прошлая настройка не прошла: {why}. Настройка поверх перепишет то, что она успела поставить.': 'The previous setup failed: {why}. Setting up over it rewrites what it managed to install.',
  '{traces} - настройка поставит бэкап заново.': '{traces} - the setup installs the backup anew.',
  'На сервере уже есть бэкап. Настройка поверх перепишет его на клиенте: расписание, пути, пароль и адрес репозитория. Репозиторий и история на бэкап-сервере останутся, если выбран тот же сервер.': 'The server already has a backup. Setting up over it rewrites it on the client: schedule, paths, password and repository address. The repository and its history on the backup server stay if the same server is chosen.',
  'переустановить поверх': 'reinstall over it',
  'На сервере уже есть бэкап: отметьте "переустановить поверх", если его надо переписать': 'The server already has a backup: tick "reinstall over it" if it should be rewritten',
  'Переустановить бэкап': 'Reinstall the backup',
  'настройка заново поверх этой: бэкап-сервер, пути, расписание': 'set it up again over this one: backup server, paths, schedule',
  '▶ Переустановить и запустить': '▶ Reinstall and run',
  'настройка не прошла: {n}': 'setup failed: {n}',
  'бэкап не свежий: последний {ago}': 'backup is not fresh: last one {ago}',
  'бэкапа еще не было': 'no backup yet',
  'бэкапится баз: {n}': 'databases backed up: {n}',
  'до {h}:00': 'until {h}:00',
  'исключено путей: {n}': 'excluded paths: {n}',
  'каждый день {time}': 'every day at {time}',
  'куда': 'to',
  'не бэкапится: {list}': 'not backed up: {list}',
  'последний {ago}': 'last one {ago}',
  'последний {ago}, добавил {s}': 'last one {ago}, added {s}',
  'проверки еще не было': 'not checked yet',
  'расписание': 'schedule',
  'свой бэкап не отрабатывает: {jobs}': 'own backup is not running: {jobs}',
  'только {n} путей': 'only {n} paths',
  'управление недоступно без helper': 'management needs the helper',
  'Сертификаты': 'Certificates',
  'Данные': 'Data',
  'Базы и хранилища': 'Databases and stores',
  '{n} запросов/мин': '{n} requests/min',
  'ближайший: {l} через {n} дн': 'nearest: {l} in {n} d',
  'всего {n}': '{n} in total',
  'всего {n}, без монитора {m}': '{n} in total, {m} without a monitor',
  'всего {n}, все на мониторинге': '{n} in total, all monitored',
  'истек {n} дн назад': 'expired {n} d ago',
  'конфиг проходит проверку': 'config passes the check',
  'очередей: {n}, сообщений: {m}': 'queues: {n}, messages: {m}',
  'переполнены: {n} (от {m} сообщений)': 'overflowing: {n} (from {m} messages)',
  'с сообщениями: {n}': 'with messages: {n}',
  'Доступ к хосту': 'Host access',
  'Замечания': 'Findings',
  'Прокси': 'Proxy',
  'не работает контейнер {list}': 'container not running: {list}',
  'не работают контейнеры {list}': 'containers not running: {list}',
  'перезапускается в цикле {list}': 'restarting in a loop: {list}',
  'перезапускаются в цикле {list}': 'restarting in a loop: {list}',
  'unhealthy: {list}': 'unhealthy: {list}',
  'docker-сокет у {list} - взлом прокси даст root на хосте': 'docker socket in {list}: a hacked proxy gets root on the host',
  'ключи от хоста у {list}': 'host keys in {list}',
  'образ собран больше года назад: {list}': 'image built more than a year ago: {list}',
  'не поднимутся после перезагрузки: {list}': 'will not come back after a reboot: {list}',
  'запущено {a} из {b}': '{a} of {b} running',
  'ключей от хоста ни у кого нет': 'no container has host keys',
  '{list}, образ моложе года': '{list}, image younger than a year',
  'все контейнеры поднимутся после перезагрузки': 'all containers come back after a reboot',
  'контейнеров': 'containers',
  'Ни у одного контейнера нет ключей от хоста: ни docker-сокета, ни privileged, ни опасных capabilities, ни корня хоста на запись.': 'No container has host keys: no docker socket, no privileged, no dangerous capabilities, no writable host root.',
  'Агент этой ноды еще не отдает ключи контейнеров от хоста: обновите агент.': 'The agent on this node does not report container host keys yet: update the agent.',
  'Внешний прокси {list}: образ собран меньше года назад.': 'External proxy {list}: the image was built less than a year ago.',
  'У всех контейнеров restart policy always или unless-stopped: после перезагрузки докер поднимет их сам.': 'All containers have restart policy always or unless-stopped: Docker brings them back after a reboot.',
  'Агент {v} не прислал restart policy контейнеров. Обновите его: кнопка "Обновить все" в разделе "Серверы" или стрелка у версии агента в карточке сервера.':
    'Agent {v} did not send container restart policies. Update it: the "Update all" button in "Servers" or the arrow next to the agent version on the server card.',
  'Гигиена подов': 'Pod hygiene',
  'падают поды: {list}': 'failing pods: {list}',
  'ноды не готовы (NotReady): {list}': 'nodes not ready (NotReady): {list}',
  'нод без агента: {list}': 'nodes without the agent: {list}',
  'работают {a} из {b}': '{a} of {b} running',
  'завершенных подов: {n}, упали {m}': 'completed pods: {n}, failed {m}',
  'в полном составе {a} из {b}': '{a} of {b} fully up',
  'не в полном составе: {list}': 'not fully up: {list}',
  'заданий: {n}, с проблемой {m}': 'jobs: {n}, with a problem {m}',
  'заданий: {n}, все отрабатывают': 'jobs: {n}, all running fine',
  'готовы {a} из {b}': '{a} of {b} ready',
  'ближайший: {what}, {d}': 'nearest: {what}, {d}',
  'ресурсов: {n}, все в Ready': 'resources: {n}, all Ready',
  'без лимита памяти {a}, без проб {b}': 'no memory limit {a}, no probes {b}',
  'у всех подов есть лимиты и пробы': 'all pods have limits and probes',
  // подпись перед числом, как нод/подов: по-русски так и пишется, по-английски множественное
  'namespace': 'namespaces',
  'нод': 'nodes',
  'подов': 'pods',
  'новое': 'new',
  'За сутки': 'Last 24 hours',
  'появилось': 'appeared',
  'ушло': 'gone',
  'изменений на сервере': 'changes on the server',
  'алертов не было, на сервере ничего не менялось': 'no alerts, nothing changed on the server',
  'вся хронология': 'full timeline',
  '{n} запросов/мин, 5xx {p}': '{n} requests/min, 5xx {p}',
  'больше всех CPU: {name}, {p}': 'top CPU: {name}, {p}',
  'важных CVE: {n}, пакетов: {p}': 'important CVEs: {n}, packages: {p}',
  'все обновления установлены': 'all updates installed',
  'ждут установки: {n}, безопасности {s}': 'pending: {n}, security {s}',
  'за ним серверов: {n}': 'servers behind it: {n}',
  'зависят мониторов: {n}': 'dependent monitors: {n}',
  'заглушено: {list}': 'muted: {list}',
  'известные проблемы: {list}': 'known issues: {list}',
  'изменений не было': 'no changes yet',
  'не задано': 'not set',
  'ничего не заглушено': 'nothing muted',
  'нужна перезагрузка': 'reboot required',
  'после перезагрузки не запустятся {list}: работают, но не включены': 'will not start after a reboot: {list} (running but not enabled)',
  'последнее: {text}, {d}': 'latest: {text}, {d}',
  'сверка пока только для Ubuntu': 'checked for Ubuntu only so far',
  'сейчас нет данных': 'no data right now',
  'слабые SSH-ключи у {list}': 'weak SSH keys: {list}',
  'снаружи еще не проверяли': 'not checked from outside yet',
  'снаружи ничего не открыто': 'nothing open from outside',
  'снаружи открыто: {list}': 'open from outside: {list}',
  'стоит за {name}': 'behind {name}',
  'стоит за {name}, он не на связи': 'behind {name}, which is offline',
  'упавших служб нет': 'no failed services',
  'учетки с доступом, в которые давно не входят: {list}': 'accounts with access nobody has used for a long time: {list}',
  'учеток с доступом: {n}': 'accounts with access: {n}',
  'Снаружи еще не проверяли: проверка идет раз в 6 часов.': 'Not checked from outside yet: the check runs every 6 hours.',
  'Упавших служб нет.': 'No failed services.',
  'Нет данных: на ноде не установлен helper units-setup.': 'No data: the units-setup helper is not installed on the node.',
  'Ничего не заглушено.': 'Nothing is muted.',
  'За ним стоят': 'Behind it',
  'Зависят мониторы': 'Dependent monitors',
  'никто': 'nobody',
  'ни один': 'none',
  'Пока этот сервер молчит, про них не пишем отдельно': 'While this server is silent, they get no separate alerts',
  'Пока сервер молчит, их алерты ждут его, а сами мониторы названы в сообщении про сервер': 'While the server is silent, their alerts wait for it, and the monitors are listed in the message about the server',
  'Наверх': 'Back to top',
  'Текст сообщения': 'Message text',
  'Не слать алерты этого сервера:': 'Mute these alerts for this server:',
  'Не слать алерты этого монитора:': 'Mute these alerts for this monitor:',
  'Недоступен / деградация': 'Down / degraded',
  'Отвечает через раз': 'Intermittent failures',
  'Частичная доступность (локации)': 'Partial availability (locations)',
  'Недоступен': 'Down',
  'Троттлинг CPU': 'CPU throttling',
  'Температура диска': 'Disk temperature',
  'Температура CPU': 'CPU temperature',
  'Перезагрузка': 'Reboot',
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
  'Поиск: хост, IP, группа, контейнер, образ…': 'Search: host, IP, group, container, image…',
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
  'Алерт по коннектам СУБД, % (0 = выкл)': 'Database connection alert, % (0 = off)',
  // --- сроки Kubernetes и Flux ---
  'Предупреждать о сроках Kubernetes за, дн. (через запятую, пусто = выкл)':
    'Warn about Kubernetes expiry, days before (comma separated, empty = off)',
  'Сроки ({n})': 'Expiry ({n})',
  'Сроки ⏳ ({n})': 'Expiry ⏳ ({n})',
  'Сертификаты, kubeconfig-и и токены Flux с их сроками. Собирает root-хелпер на самой ноде: панель токенов не видит.':
    'Certificates, kubeconfigs and Flux tokens with their expiry dates. Collected by a root helper on the node itself: the panel never sees the tokens.',
  'Flux ({n})': 'Flux ({n})',
  'Flux ⚠ ({n})': 'Flux ⚠ ({n})',
  'Ресурсы Flux и их готовность. Не в Ready — доставка встала: запущенное продолжает работать, а новые изменения из git не доезжают.':
    'Flux resources and whether they are Ready. Not Ready means delivery has stopped: what is running keeps running, but new changes from git do not arrive.',
  'Доставка Flux встала: {n} ресурс(ов) не в Ready. Уже запущенное продолжает работать — по метрикам это не видно.':
    'Flux delivery has stopped: {n} resource(s) not Ready. Everything already running keeps running — metrics will not show this.',
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
  'снаружи отвечает {code}': 'answers {code} from outside',
  'снаружи сбрасывает соединение': 'connection is reset from outside',
  'снаружи не подключается': 'no connection from outside',
  'уже {d}': 'for {d}',
  '{out}{d}, изнутри {srv} открывается': '{out}{d}, opens from within {srv}',
  '{out} и ни разу не открылся, домен на {srv}': '{out} and never opened, the domain is on {srv}',
  '{out} и ни разу не открылся': '{out} and never opened',
  'считать {code} нормой': 'treat {code} as normal',
  'проверять изнутри': 'probe from within',
  'применить ко всем {n}': 'apply to all {n}',
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
  'Создать сейчас': 'Create now',
  'Пока нет файлов.': 'No files yet.',
  'Бэкап создан на сервере.': 'Backup created on the server.',
  'Файл не является корректным JSON.': 'The file is not valid JSON.',
  'Восстановить из «{name}»? Текущие мониторы, серверы и настройки будут заменены. Учётные записи в копию не входят и не восстановятся.':
    'Restore from “{name}”? Current monitors, servers and settings will be replaced. Accounts are not part of the backup and will not be restored.',
  'Восстановлено записей: {n}. Обновляю…': 'Restored {n} records. Reloading…',
  'Скачать': 'Download',

  // --- алерты / инциденты / uptime ---
  'Алерты': 'Alerts',
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
  'Алерты сайтов': 'Site alerts',
  'Поиск монитора…': 'Search monitor…',
  'Куда': 'Apply to',
  'Текст': 'Text',
  'Поиск группы…': 'Search group…',
  'Поиск сервера…': 'Search server…',
  'Не слать алерты (пауза)': 'Mute alerts (pause)',
  'Кнопки под алертами и команды боту': 'Buttons under alerts and bot commands',
  'Часовой пояс («до утра» = до 9:00)': 'Time zone ("until morning" = until 9:00)',
  'Бот {bot} читается, кнопки работают': 'Bot {bot} is being read, buttons work',
  'Этого бота уже читает другая программа или панель. Заведите для панели отдельного бота у @BotFather и укажите его токен.':
    'Another program or panel already reads this bot. Create a separate bot for this panel with @BotFather and set its token.',
  'Бот не отвечает: {err}': 'The bot does not respond: {err}',
  'Эскалация': 'Escalation',
  'Сигнал не реже, чем раз в': 'A signal at least once every',
  '5 мин': '5 min',
  '15 мин': '15 min',
  '1 ч': '1 h',
  '6 ч': '6 h',
  '1 сутки': '1 day',
  'Период, минут': 'Period, minutes',
  'Допуск, минут (0 = десятая часть периода)': 'Grace, minutes (0 = a tenth of the period)',
  'Выдать новый адрес? Старый перестанет приниматься сразу, скрипт надо будет поправить.':
    'Issue a new address? The old one stops working at once, the script will need an update.',
  'Адрес для скрипта (секрет, не публикуйте):': 'Address for the script (a secret, do not publish it):',
  'Сигнал только после успешного прогона:': 'Signal only after a successful run:',
  'Сигнал всегда, с кодом выхода (ненулевой = ошибка, придет сразу):':
    'Signal every time, with the exit code (non-zero = failure, alerts at once):',
  'Последний сигнал: {when}': 'Last signal: {when}',
  'Сигнала еще не было': 'No signal yet',
  'скрипт сообщил об ошибке: {msg}': 'the script reported an error: {msg}',
  'Выдать новый адрес': 'Issue a new address',
  'Что важно знать дежурному: что сломано, кто в курсе, когда починят':
    'What the on-call person should know: what is broken, who is on it, when it gets fixed',
  'Убрать заметку': 'Remove the note',
  'Заметка приходит строкой в алертах': 'The note comes as a line in alerts',
  'добавить заметку': 'add a note',
  'Изменить заметку': 'Edit the note',
  'пока не починится': 'until it is fixed',
  '3 дня': '3 days',
  'неделя': 'a week',
  'месяц': 'a month',
  'до {d}': 'until {d}',
  'Известная проблема': 'Known issue',
  'Что известно: кто чинит, когда, где тикет': 'What is known: who is fixing it, when, where the ticket is',
  'Молчать': 'Stay quiet',
  'Алерт молчит, пока не станет хуже. Проблема ушла сама - отметка закроется, вышел срок - алерт придет как обычно. Раз в неделю бот напомнит, что она все еще висит.':
    'The alert stays quiet until it gets worse. If the problem goes away, the mark closes; if the time runs out, the alert comes as usual. Once a week the bot reminds that it is still there.',
  'Отметить': 'Mark',
  'Известные проблемы': 'Known issues',
  'стало хуже отмеченного, алерт ушел': 'got worse than marked, the alert was sent',
  'Снять отметку: если проблема осталась, придет алерт': 'Remove the mark: if the problem is still there, an alert comes',
  'снять': 'remove',
  'алерт о сбое молчит, отметка закроется, когда монитор поднимется':
    'the failure alert stays quiet, the mark closes when the monitor is back up',
  'Снять отметку: если монитор все еще лежит, придет алерт': 'Remove the mark: if the monitor is still down, an alert comes',
  'Известная проблема: алерт молчит, пока не станет хуже': 'Known issue: the alert stays quiet until it gets worse',
  'отметить как известную проблему': 'mark as a known issue',
  'Окна обслуживания': 'Maintenance windows',
  'Запись': 'Record',
  'DNS-сервер (IP)': 'DNS server (IP)',
  'системный': 'system',
  'Ожидаемый ответ, по строке': 'Expected answer, one per line',
  'Пусто - запомним первый ответ и пришлем алерт, когда он изменится. У MX можно без приоритета.':
    'Empty - the first answer is remembered and an alert comes when it changes. MX can go without priority.',
  'ответ совпадает целиком': 'the answer matches exactly',
  'ответ содержит все эти': 'the answer contains all of these',
  'ждем, что ответ содержит': 'expecting the answer to contain',
  'ждем ответ': 'expecting',
  'запомнили': 'remembered',
  'ответа еще не было: первый запомним и будем следить за изменениями': 'no answer yet: the first one will be remembered and watched for changes',
  'сейчас': 'now',
  'Принять новый ответ как правильный': 'Accept the new answer as correct',
  // --- признаки взлома сайта (TamperSigns.tsx) ---
  'Внешние домены': 'External domains',
  'Видно снаружи': 'Visible from outside',
  // --- признаки взлома сайта (TamperSigns.tsx) ---
  'Новые php в веб-корне': 'New php in the web root',
  'Файлы кода изменились не через git/деплой': 'Code files changed outside git/deploy',
  'файлы поменялись на сервере напрямую: правка руками, админка, плагин или взлом. Если это вы - нажмите "{btn}"':
    'the files were changed right on the server: a manual edit, the admin panel, a plugin or a break-in. If it was you, press "{btn}"',
  'не закоммиченная правка в рабочей копии git (ветка {b})': 'uncommitted edit in the git working copy (branch {b})',
  'не закоммиченная правка в рабочей копии git': 'uncommitted edit in the git working copy',
  'перезаписаны тем же содержимым, что в git: {f}': 'rewritten with the same content as in git: {f}',
  'файлы плагина {s} изменились без смены версии': 'files of plugin {s} changed without a version change',
  'файлы плагина {s} изменились': 'files of plugin {s} changed',
  'файлы темы {s} изменились без смены версии': 'files of theme {s} changed without a version change',
  'файлы темы {s} изменились': 'files of theme {s} changed',
  'файлы ядра WordPress изменились без смены версии': 'WordPress core files changed without a version change',
  'файлы ядра WordPress изменились': 'WordPress core files changed',
  'WordPress сам обновил перевод плагина {s} ({loc})': 'WordPress updated the translation of plugin {s} itself ({loc})',
  'WordPress сам обновил перевод темы {s} ({loc})': 'WordPress updated the translation of theme {s} itself ({loc})',
  'WordPress сам обновил свой перевод ({loc})': 'WordPress updated its own translation ({loc})',
  'обновлен плагин {s} {from} -> {to} (из админки или автообновлением)': 'plugin {s} updated {from} -> {to} (from the admin panel or by auto-update)',
  'обновлена тема {s} {from} -> {to} (из админки или автообновлением)': 'theme {s} updated {from} -> {to} (from the admin panel or by auto-update)',
  'обновлен WordPress {from} -> {to} (из админки или автообновлением)': 'WordPress updated {from} -> {to} (from the admin panel or by auto-update)',
  'на {where}': 'on {where}',
  'Файлы сайтов': 'Site files',
  'Новые php': 'New php',
  'файлов кода больше {n}, список неполный': 'more than {n} code files, the list is incomplete',
  'файлов кода: {n}': 'code files: {n}',
  'записано в контейнере файлов кода: {n}': 'code files written in the container: {n}',
  'выкладка {t}, файлов: {n}': 'deploy {t}, files: {n}',
  'выкладка git {k} {t}, файлов: {n}': 'deploy git {k} {t}, files: {n}',
  'ветка {b}': 'branch {b}',
  'новые файлы, которых нет в git': 'new files git does not know',
  'изменены файлы вне git (в .gitignore)': 'files outside git changed (in .gitignore)',
  'похож на веб-шелл': 'looks like a web shell',
  'отличается от HEAD': 'differs from HEAD',
  'новый, git его не знает': 'new, git does not know it',
  'новый, в .gitignore': 'new, in .gitignore',
  'изменен, в .gitignore': 'changed, in .gitignore',
  'изменен': 'changed',
  'владелец {o} (веб-сервер)': 'owner {o} (web server)',
  'владелец {o}': 'owner {o}',
  'записан {t}': 'written {t}',
  'признак снят сам: {p} удален': 'sign cleared by itself: {p} deleted',
  'признак снят сам: {p} вернули как было': 'sign cleared by itself: {p} restored',
  'признак снят сам: {p} теперь как в git': 'sign cleared by itself: {p} is now as in git',
  'признак снят сам: {p} - пакет перевода WordPress': 'sign cleared by itself: {p} is a WordPress language pack',
  'признак снят сам: {p} записал плагин {s} по своему шаблону': 'sign cleared by itself: {p} was written by plugin {s} from its template',
  'признак снят сам: в {p} нет кода, только настройки плагина': 'sign cleared by itself: {p} holds no code, only plugin settings',
  'признак снят сам: {p} только закрывает доступ': 'sign cleared by itself: {p} only denies access',
  'плагин {s} сам записал свой файл по шаблону: {f}': 'plugin {s} wrote its own file from its template: {f}',
  'плагин записал свои настройки, кода в файле нет: {f}': 'a plugin wrote its settings, the file holds no code: {f}',
  'плагин закрыл доступ к каталогу (.htaccess): {f}': 'a plugin denied access to a directory (.htaccess): {f}',
  'файлы, которые плагины пишут сами, не правка: {f}': 'files plugins write by themselves, not an edit: {f}',
  'Похоже на веб-шелл': 'Looks like a web shell',
  'здесь правят руками': 'edited here by hand',
  'сам: да, ветка {b}': 'auto: yes, branch {b}',
  'сам: нет': 'auto: no',
  'сам, по ветке': 'auto, by branch',
  'нет, каждая правка - признак': 'no, every edit is a sign',
  'здесь правят руками: правки разработчика не признак, следим за php в загрузках, веб-шеллами и файлами, которые записал сам сайт':
    'edited here by hand: developer edits are not a sign; php in uploads, web shells and files written by the site itself are watched',
  'перезаписаны тем же содержимым: {f}': 'rewritten with the same content: {f}',
  'признаки и в карточке сайта': 'signs are also in the site card',
  'Веб-корней не нашлось.': 'No web roots found.',
  'веб-корней: {n}, новых php и правок не через git/деплой нет': 'web roots: {n}, no new php or changes outside git/deploy',
  'Признаки взлома: новые php в файлах сайтов': 'Signs of compromise: new php in site files',
  'отметил новые файлы сайта своими': 'marked new site files as ours',
  'сменил, правят ли сайт руками': 'changed whether the site is edited by hand',
  // --- признаки взлома сайта (TamperSigns.tsx) ---
  'Признаки взлома': 'Signs of compromise',
  'Главная опустела': 'Home page went empty',
  '{size} вместо {was}': '{size} instead of {was}',
  'title был "{title}"': 'title was "{title}"',
  'Сменился title': 'Title changed',
  'было "{was}", стало "{title}"': 'was "{was}", now "{title}"',
  'Googlebot видит другую страницу': 'Googlebot sees a different page',
  'у бота "{title}", {size}; у людей {human}': 'bot gets "{title}", {size}; people get {human}',
  'Новый внешний домен {d}': 'New external domain {d}',
  '{what} без поддержки': '{what} is unsupported',
  'ветка больше не получает исправлений': 'this branch no longer gets fixes',
  '{what} с дырой': '{what} has a known hole',
  'исправлено в {v}': 'fixed in {v}',
  '{tag} на странице': '{tag} on the page',
  'в скрипте на странице': 'in a script on the page',
  'в {file}': 'in {file}',
  'это наше': 'this is ours',
  'это ок': 'this is fine',
  'отложить 7 дн': 'postpone 7 days',
  'Новые внешние домены': 'New external domains',
  'Версии с дырами': 'Versions with known holes',
  'Еще не разбирали: главную открываем раз в час, когда сайт отвечает.': 'Not checked yet: the home page is opened hourly while the site is up.',
  'Похоже на взлом': 'Looks compromised',
  'Сильные признаки пропадут сами, когда сайт вылечат. "Разобрались" принимает текущее как норму.': 'Strong signs clear by themselves once the site is fixed. "Sorted out" accepts the current state as normal.',
  'Разобрались': 'Sorted out',
  'Один признак': 'One sign',
  'Сам по себе не взлом, но надо глянуть.': 'Not a compromise by itself, but worth a look.',
  'с {t}': 'since {t}',
  'Признаков нет.': 'No signs.',
  'Последний разбор не удался: {err}': 'Last check failed: {err}',
  'разобрать не удалось: {err}': 'check failed: {err}',
  'признаков нет': 'no signs',
  'еще не разбирали': 'not checked yet',
  'принял признак взлома как норму': 'accepted a compromise sign as normal',
  'Зависит от сервера': 'Depends on server',
  'Пока сервер молчит, алерт монитора ждет его, а сам монитор назван в сообщении про сервер':
    'While the server is silent, the monitor alert waits for it, and the monitor is listed in the server message',
  'Сервер {srv} не на связи: алерт монитора ждет его, монитор назван в сообщении про сервер':
    'Server {srv} is not reporting: the monitor alert waits for it, the monitor is listed in the server message',
  'Стоит за сервером': 'Sits behind server',
  'Шлюз, VPN или гипервизор: пока он молчит, про этот сервер не пишем, он строкой в сообщении про него':
    'A gateway, VPN or hypervisor: while it is silent, this server gets no alert of its own and is listed in its message',
  'за {srv}, он тоже молчит': 'behind {srv}, which is silent too',
  'за ним': 'behind it',
  'зависит от': 'depends on',
  'зависят мониторы': 'monitors depending on it',
  'изменить': 'change',
  'найти самой панели': 'let the panel find it',
  'не на связи': 'not reporting',
  'ни за каким': 'none',
  'ни от какого': 'none',
  'ни от какого сервера не зависит': 'does not depend on any server',
  'сервер не на связи, алерты монитора ждут его': 'the server is not reporting, monitor alerts wait for it',
  'стоит за': 'sits behind',
  'указать сервер, от которого зависит': 'set the server it depends on',
  'указать, за каким сервером стоит': 'set the server it sits behind',
  'проверяется изнутри него': 'checked from inside it',
  'обслуживает домен': 'serves the domain',
  'это его адрес': 'it is its address',
  'задано вручную': 'set manually',
  '{n} дней': '{n} days',
  'Алертов пришло': 'Alerts received',
  'Аптайм': 'Uptime',
  'Бэкапы: что ломалось': 'Backups: what broke',
  'Все {n} мониторов отработали без сбоев': 'All {n} monitors ran without failures',
  'Лежал': 'Down for',
  'Монитор': 'Monitor',
  'Остальные {n} без сбоев': 'The other {n} had no failures',
  'Отчет': 'Report',
  // --- Левые запросы к панели (PanelRequestsSec) ---
  'Панель: левые запросы':
    'Panel: bogus requests',
  'За сутки отказов и всплесков не было.':
    'No rejections or bursts in the last 24 hours.',
  'Отказов':
    'Rejected',
  'Откуда':
    'From',
  'Панель считает запросы к своему API по адресам. Алерт - когда с одного адреса за {w} минут пришло {bad} отказов (неверный токен агента, удаленный push, перебор адресов) или больше {flood} запросов на каждый сервер за этим адресом, обычный агент шлет около {typ}. Неудачные входы считает защита входа.':
    'The panel counts requests to its API by address. An alert goes out when one address gets {bad} rejections within {w} minutes (a wrong agent token, a deleted push, probing of paths) or sends more than {flood} requests per server behind that address, while a normal agent sends about {typ}. Failed sign-ins are handled by sign-in protection.',
  'Пик в минуту':
    'Peak per minute',
  'С':
    'Since',
  'Сейчас левых запросов нет.':
    'No bogus requests right now.',
  'и еще адресов: {n}':
    'and {n} more addresses',
  'не сервер панели':
    'not a panel server',
  'серверы: {s}':
    'servers: {s}',
  'Запросы к панели':
    'Requests to the panel',
  'Левые запросы в панель':
    'Bogus requests to the panel',
  'к запросам к панели':
    'to requests to the panel',
  'сейчас {n}':
    'now {n}',
  'Левые запросы к панели':
    'Bogus requests to the panel',
  // --- Защита входа: блокировка адресов и белый список (LoginGuardSec) ---
  'Адрес или подсеть':
    'Address or subnet',
  'Белый список':
    'Allowlist',
  'Блокировать':
    'Block',
  'Ваш адрес сейчас:':
    'Your address now:',
  'До':
    'Until',
  'За сутки неудачных входов не было.':
    'No failed sign-ins in the last 24 hours.',
  'Заблокированы сейчас':
    'Blocked now',
  'Какие логины':
    'Logins tried',
  'Неудач':
    'Failures',
  'Неудачные входы за сутки':
    'Failed sign-ins in 24 hours',
  'Неудачных входов за {w} минут':
    'Failed sign-ins within {w} minutes',
  'Подпись':
    'Label',
  'Попыток в блокировке':
    'Attempts while blocked',
  'После {n} неудачных входов за {w} минут адрес блокируется {ban}, повторно за сутки - на сутки. С адресов из белого списка вход не блокируется никогда, но пароль и код с них все равно нужны.':
    'After {n} failed sign-ins within {w} minutes the address is blocked {ban}, a repeat within a day blocks it for a day. Addresses on the allowlist are never blocked, but they still need the password and the code.',
  'Последняя':
    'Last',
  'Правило':
    'Rule',
  'Пусто: блокируется любой адрес.':
    'Empty: any address can be blocked.',
  'Разблокировать':
    'Unblock',
  'Сейчас никто не заблокирован.':
    'Nobody is blocked now.',
  'белый список':
    'allowlist',
  'в белом списке ({n})':
    'on the allowlist ({n})',
  'ваш':
    'yours',
  'добавить мой адрес':
    'add my address',
  'заблокирован до {h}':
    'blocked until {h}',
  'мой адрес':
    'my address',
  'на 15 минут':
    'for 15 minutes',
  'на 6 часов':
    'for 6 hours',
  'на {n} мин':
    'for {n} min',
  'на сутки':
    'for a day',
  'на час':
    'for an hour',
  'например, офис':
    'e.g. office',
  'повторно':
    'repeat',
  'Адрес {k} разблокирован.':
    'Address {k} unblocked.',
  'Защита':
    'Protection',
  'Защита входа':
    'Sign-in protection',
  'Защита входа сохранена.':
    'Sign-in protection saved.',
  'блок {n}':
    'blocked {n}',
  'изменил защиту входа':
    'changed sign-in protection',
  'к защите входа':
    'to sign-in protection',
  'после {n} неудач за {w} мин, {ban}':
    'after {n} failures within {w} min, {ban}',
  'разблокировал адрес':
    'unblocked an address',
  'Слишком много неудачных входов с этого адреса, попробуйте через {n} мин.':
    'Too many failed sign-ins from this address, try again in {n} min.',
  // --- Моя учетка и О панели разделами (AccountModal, AboutModal) ---
  'Ваша учетка: пароль, вход по коду из приложения и свои алерты в Telegram. Роль, разделы и группы задает админ в окне Пользователи.':
    'Your account: password, sign-in with an app code and your own Telegram alerts. The role, sections and groups are set by an admin in the Users window.',
  'Вход':
    'Sign-in',
  'Код из приложения':
    'App code',
  'Моя учетка':
    'My account',
  'Пароль менялся':
    'Password changed',
  'Последние входы':
    'Recent sign-ins',
  'Последний раз пароль менялся {d}.':
    'The password was last changed on {d}.',
  'Смена пароля завершит все другие сессии: везде, кроме этого окна, придется войти заново.':
    'Changing the password ends every other session: you will have to sign in again everywhere except this window.',
  'Уведомления':
    'Notifications',
  'в журнале нет':
    'not in the log',
  'вкл':
    'on',
  'вход только по паролю':
    'password only',
  'заблокирован после перебора':
    'blocked after repeated failures',
  'под вашим логином':
    'with your login',
  'при входе спрашивается код':
    'a code is asked at sign-in',
  'приходят':
    'arrive',
  'чат {id}':
    'chat {id}',
  'чат не привязан':
    'chat not linked',
  'Логотип':
    'Logo',
  'О панели':
    'About',
  // --- окно История алертов разделами (AlertHistoryModal) ---
  'Все сообщения':
    'All messages',
  'И еще {n} реже - в ленте.':
    '{n} more, less often - in the feed.',
  'Каждый доставленный алерт и отбой хранится полгода. Что шумело - что именно и где, как в отчете: имя ведет к объекту, строка - к сообщениям про это.':
    'Every delivered alert and recovery is kept for half a year. What was noisy shows what exactly and where, as in the report: the name leads to the object, the row to the messages about it.',
  'Карточка':
    'Card',
  'Сообщения':
    'Messages',
  'Срабатываний':
    'Alerts fired',
  'Чаще всего':
    'Most often',
  'к сообщениям':
    'to the messages',
  'любой вид':
    'any kind',
  'отбоев: {n}':
    'recoveries: {n}',
  'отбой':
    'recovery',
  // --- окно Уязвимости разделами (VulnsModal) ---
  '{os}: исправления только в Ubuntu Pro':
    '{os}: fixes only in Ubuntu Pro',
  'База {rel} не скачалась: {err}':
    'The {rel} database did not download: {err}',
  'Базы Canonical от {d}: {list}':
    'Canonical databases from {d}: {list}',
  'Базы Canonical: {list}':
    'Canonical databases: {list}',
  'Без важных':
    'No important ones',
  'Без важных уязвимостей':
    'No important vulnerabilities',
  'Без обычной поддержки':
    'Out of standard support',
  'Где':
    'Where',
  'Ждут нового ядра':
    'Waiting for a new kernel',
  'Из исходного пакета {src} на серверах стоят: {list}.':
    'Installed from the {src} source package: {list}.',
  'Исправление только в Ubuntu Pro, а он не подключен: {list}.':
    'The fix is only in Ubuntu Pro, which is not attached: {list}.',
  'Исправлено в':
    'Fixed in',
  'Не проверены':
    'Not checked',
  'Остальные':
    'Others',
  'Пакет':
    'Package',
  'Пакеты и ядро сверены с базой Canonical: critical и эксплуатируемых уязвимостей, которые закрываются обновлением, нет.':
    'Packages and the kernel are checked against the Canonical database: no critical or exploited vulnerabilities that an update fixes.',
  'Пакеты обновить':
    'Packages to update',
  'С важными уязвимостями':
    'With important vulnerabilities',
  'Сверяется работающее ядро, а не установленное: новое начинает действовать только после перезагрузки.':
    'The running kernel is checked, not the installed one: a new kernel takes effect only after a reboot.',
  'Стоит':
    'Installed',
  'Что обновить':
    'What to update',
  'Эксплуатируется':
    'Exploited',
  'Эти серверы не с чем сверить: сверка пока только для Ubuntu, и нужен список пакетов от агента.':
    'These servers cannot be checked: checks work only for Ubuntu for now and need the package list from the agent.',
  'база уязвимостей не скачалась: {err}':
    'the vulnerability database did not download: {err}',
  'из проверенных {n}':
    'of {n} checked',
  'из проверенных {n}, с эксплуатируемыми: {k}':
    'of {n} checked, with exploited ones: {k}',
  'на серверах: {n}':
    'on servers: {n}',
  'от {d}':
    'from {d}',
  // --- карта зависимостей ---
  'Замолчит сервер - то, что ниже, не пришлет каждый свой алерт: оно перечислено в его сообщении, а свой алерт придет только от того, что не поднимется вместе с сервером.':
    'If the server goes silent, what is below does not send an alert each: it is listed in its message, and only what does not come back together with the server sends its own alert.',
  'Лежат':
    'Down',
  'Сервер {state}: алерты того, что на нем держится, ждут его.':
    'The server is {state}: alerts of what depends on it wait for it.',
  'ни с чем не связан':
    'not linked to anything',
  'серверов за ним: {n}':
    'servers behind it: {n}',
  'Алерт по этим мониторам приходит сам по себе, даже если молчит сервер, на котором они работают. Сервер задается в карточке монитора, в разделе Зависимости - туда и ведет имя.':
    'These monitors alert on their own, even when the server they run on is silent. The server is set in the monitor card, in the Dependencies section, which is where the name leads.',
  'Без связей':
    'Without links',
  'Держится':
    'Depends on it',
  'Домен на этом сервере':
    'Domain served by this server',
  'И еще {n} - уточните поиск.':
    '{n} more, refine the search.',
  'Карточка сервера':
    'Server card',
  'Мониторы без сервера':
    'Monitors without a server',
  'Ни один монитор не привязан к этим серверам, и за ними никто не стоит: замолчит такой сервер - придет только его алерт.':
    'No monitor is tied to these servers and no server stands behind them: if one goes silent, only its own alert arrives.',
  'Ничего не нашлось.':
    'Nothing found.',
  'Отвязаны вручную · {n}':
    'Unlinked by hand · {n}',
  'Панель не нашла сервер · {n}':
    'Server not found by the panel · {n}',
  'Привязаны вручную':
    'Linked by hand',
  'Проверяются изнутри сервера':
    'Checked from inside the server',
  'Серверы без связей':
    'Servers without links',
  'Серверы за ним':
    'Servers behind it',
  'Серверы за ним · {n}':
    'Servers behind it · {n}',
  'Серверы за шлюзом':
    'Servers behind a gateway',
  'Серверы, на которых что-то держится':
    'Servers something depends on',
  'Стоит за':
    'Behind',
  'Те же связи, по которым панель придерживает алерты: замолчал сервер - мониторы и серверы за ним не шумят по отдельности, а перечислены в его сообщении. Сервер монитора панель находит сама, когда он однозначен, иначе его задают в карточке монитора; шлюз сервера - в карточке сервера.':
    'The same links the panel uses to hold alerts: when a server goes silent, the monitors and servers behind it do not alert one by one but are listed in its message. The panel finds the server of a monitor itself when it is unambiguous, otherwise it is set in the monitor card; the gateway of a server is set in the server card.',
  'Фильтр: имя или адрес монитора…':
    'Filter: monitor name or address…',
  'Фильтр: имя сервера…':
    'Filter: server name…',
  'Цель - адрес сервера':
    'Target is the server address',
  'Что ляжет вместе с сервером':
    'What goes down with the server',
  'алерт по каждому придет отдельно':
    'each one alerts separately',
  'мониторов на них: {n}':
    'monitors on them: {n}',
  'на':
    'on',
  'на них ничего не держится':
    'nothing depends on them',
  'не на связи {ago}':
    'offline for {ago}',
  'отвязан вручную':
    'unlinked by hand',
  'сервер не найден':
    'server not found',
  'сервер не найден: {a}, отвязаны вручную: {b}':
    'server not found: {a}, unlinked by hand: {b}',
  'серверов: {s}, мониторов: {c}':
    'servers: {s}, monitors: {c}',
  'Карта зависимостей': 'Dependency map',
  'еще {n}': '{n} more',
  'найти сервер или монитор': 'find a server or monitor',
  'молчит': 'silent',
  'Связей пока нет: ни один монитор не привязан к серверу.':
    'No links yet: no monitor is tied to a server.',
  // --- хронология сервера ---
  'Хронология': 'Timeline',
  'Система': 'System',
  'Доступ и кроны': 'Access and cron',
  'Панель': 'Panel',
  'перевыпустил токен агента': 'reissued the agent token',
  'выбрал путь агента в панель': 'chose how the agent reaches the panel',
  'включил ретранслятор для агентов': 'switched on the relay for agents',
  'выключил ретранслятор для агентов': 'switched off the relay for agents',
  'указал, за каким сервером стоит': 'set the server it sits behind',
  'добавил сервер': 'added the server',
  'отметил известную проблему': 'marked a known issue',
  'снял известную проблему': 'cleared a known issue',
  'запустил бэкап': 'started a backup',
  'освободил место на диске': 'freed disk space',
  'отметил изменения доступа своими': 'acknowledged access changes',
  'проверил, что открыто в интернет': 'checked what is open to the internet',
  'отменил обновление агента': 'cancelled the agent update',
  'перенес проверки на сервер': 'moved checks to the server',
  'окно обслуживания': 'maintenance window',
  'до': 'until',
  'За этот период ничего не происходило.': 'Nothing happened in this period.',
  'показать еще ({n})': 'show more ({n})',
  'Контейнеры, система и порты записываются с 1.4.168, пакеты по журналу установок - с агента 2.28. Доступ и кроны - из снимков раз в 10 минут.':
    'Containers, system and ports are recorded since 1.4.168, packages from the install log since agent 2.28. Access and cron come from snapshots every 10 minutes.',
  'подробно': 'details',
  // --- пакеты на серверах ---
  'Пакеты': 'Packages',
  'установлено: {n}': 'installed: {n}',
  'имя или версия': 'name or version',
  'весь список': 'full list',
  // Уязвимости
  'Уязвимости': 'Vulnerabilities',
  'ядро ({src})': 'kernel ({src})',
  'эксплуатируется: {n}': 'exploited: {n}',
  'critical: {n}': 'critical: {n}',
  'только в Ubuntu Pro': 'Ubuntu Pro only',
  'обновить и перезагрузить': 'upgrade and reboot',
  'уже эксплуатируют (CISA KEV)': 'exploited in the wild (CISA KEV)',
  '{os}: обычная поддержка кончилась ({eol}), исправления выходят только в Ubuntu Pro.':
    '{os}: standard support ended ({eol}), fixes are only released in Ubuntu Pro.',
  'Pro подключен.': 'Pro is attached.',
  'Pro не подключен.': 'Pro is not attached.',
  'важных CVE: {n}': 'important CVEs: {n}',
  'важных нет': 'none important',
  'Сверка с базой уязвимостей пока только для Ubuntu.': 'Vulnerability matching is only available for Ubuntu so far.',
  'База уязвимостей не скачалась: {err}': 'The vulnerability database failed to download: {err}',
  'База уязвимостей еще не скачана: панель скачивает ее раз в сутки, первый раз - в течение 10 минут.':
    'The vulnerability database is not downloaded yet: the panel fetches it once a day, the first time within 10 minutes.',
  'Только critical и уже эксплуатируемые (CISA KEV), которые закрываются обновлением. База Canonical от {d}.':
    'Only critical and already exploited (CISA KEV) ones that an update fixes. Canonical database of {d}.',
  'скопировано': 'copied',
  'копировать': 'copy',
  'Ядро закрывается только перезагрузкой в новое: sudo apt full-upgrade, затем перезагрузка.':
    'The kernel is only fixed by rebooting into a new one: sudo apt full-upgrade, then reboot.',
  'Только critical и уже эксплуатируемые (CISA KEV), которые закрываются обновлением: средние и мелкие не показываются. Пакеты сверяются с базой Canonical, ядро - работающее, а не установленное.':
    'Only critical and already exploited (CISA KEV) ones that an update fixes: medium and minor ones are not shown. Packages are matched against the Canonical database, the kernel is the running one, not the installed one.',
  'CVE, пакет или сервер': 'CVE, package or server',
  'Базы уязвимостей еще не скачаны: панель скачивает их раз в сутки, первый раз - в течение 10 минут.':
    'Vulnerability databases are not downloaded yet: the panel fetches them once a day, the first time within 10 minutes.',
  'не Ubuntu': 'not Ubuntu',
  'список пакетов еще не пришел': 'the package list has not arrived yet',
  'база уязвимостей еще не скачана': 'the vulnerability database is not downloaded yet',
  'Список еще не пришел: агент пришлет его со следующим отчетом.':
    'The list has not arrived yet: the agent will send it with the next report.',
  'показаны первые {k} из {n}, уточните фильтр': 'showing the first {k} of {n}, narrow the filter',
  // --- где ломается сайт: фазы запроса и проверка изнутри ---
  'подключение': 'connect',
  'ожидание ответа': 'waiting for response',
  'без разбивки': 'no breakdown',
  'сервер': 'server',
  'Где оборвался запрос': 'Where the request broke',
  'Из чего сложилось время ответа': 'What the response time is made of',
  'оборвалось здесь': 'broke here',
  'Изнутри {s} при сбое открывался': 'From inside {s} it opened during the failure',
  'приложение живо, снаружи мешает сеть, блокировка или белый список':
    'the application is alive, the network, a block or an allowlist is in the way from outside',
  'Изнутри {s} при сбое тоже не открывался': 'From inside {s} it did not open either',
  'дело в самом сервере или приложении': 'the server or the application itself is at fault',
  '{t} назад': '{t} ago',
  'Спросили {s}, открывается ли сайт изнутри: ждем ответ агента':
    'Asked {s} whether the site opens from inside: waiting for the agent',
  '{s} не ответил на проверку изнутри': '{s} did not answer the inside check',
  // --- журнал systemd по запросу ---
  'Показаны последние {n} строк: больше за один раз агент не передает, сократите период или поднимите приоритет.':
    'Showing the last {n} lines: the agent does not send more at once, shorten the period or raise the priority.',
  'не удалось получить журнал': 'could not get the journal',
  'Имя юнита вида nginx.service или nginx': 'A unit name like nginx.service or nginx',
  'предупреждения': 'warnings',
  'Весь журнал': 'Whole journal',
  'Ядро': 'Kernel',
  'Журнал': 'Journal',
  'весь журнал': 'whole journal',
  '{n} строк': '{n} lines',
  'фильтр по тексту': 'filter by text',
  'строк: {k} из {n}': 'lines: {k} of {n}',
  'строк: {n}': 'lines: {n}',
  'Выберите юнит или весь журнал.': 'Pick a unit or the whole journal.',
  'Запрашиваю журнал у агента…': 'Asking the agent for the journal…',
  'Нет записей за выбранный период. Увеличьте период или выберите "все" вместо ошибок.':
    'No entries for this period. Widen the period or pick "all" instead of errors.',
  'Под фильтр ничего не попало.': 'Nothing matches the filter.',
  'Показаны последние {n} строк: за период их больше, сократите период или поднимите приоритет.':
    'Showing the last {n} lines: the period has more, shorten it or raise the priority.',
  'Журнал не хранится в панели - читается с ноды по запросу.':
    'The journal is not stored in the panel, it is read from the node on request.',
  'Журнал откроется после обновления агента до 2.29.': 'The journal opens after the agent is updated to 2.29.',
  'Журнал читается через helper units-setup 0.4: обновите его (блок "Обновления" на главной).':
    'The journal is read through the units-setup 0.4 helper: update it ("Updates" block on the home page).',
  'Юнит...': 'Unit...',
  // --- поиск по парку ---
  'Сайты на серверах': 'Sites on servers',
  'Контейнеры': 'Containers',
  'Порты': 'Ports',
  'Базы и сервисы': 'Databases and services',
  'Очереди': 'Queues',
  'SSH-ключи': 'SSH keys',
  'Юниты systemd': 'systemd units',
  'Кроны': 'Cron jobs',
  'Версии': 'Versions',
  'ОС': 'OS',
  'ядро': 'kernel',
  'база': 'database',
  'сервис': 'service',
  'есть неготовые': 'some not ready',
  'имя хоста': 'hostname',
  'заметка': 'note',
  'путь': 'path',
  'отпечаток': 'fingerprint',
  'адрес': 'address',
  'подов: {n}': 'pods: {n}',
  'Сервер, IP, домен, порт, образ, база, ключ...':
    'Server, IP, domain, port, image, database, key...',
  'Поиск': 'Search',
  'Поиск по всем серверам': 'Search across all servers',
  'Ищет по всем серверам сразу: имена, IP, домены и мониторы, контейнеры и образы, порты (5432), базы и очереди, поды, пользователи и SSH-ключи, юниты, кроны, версии ОС, ядра, SSH и Docker.':
    'Searches all servers at once: names, IPs, domains and monitors, containers and images, ports (5432), databases and queues, pods, users and SSH keys, units, cron jobs, OS, kernel, SSH and Docker versions.',
  'ищем...': 'searching...',
  'Ничего не нашлось': 'Nothing found',
  'показано {n} из {m}, уточните запрос': 'showing {n} of {m}, narrow the query',
  'выбрать': 'select',
  'Последний': 'Last',
  'Раз': 'Times',
  'Сайты со сбоями': 'Sites with failures',
  'Сайты, средний аптайм': 'Sites, average uptime',
  'Самый долгий': 'Longest',
  'Сбоев': 'Failures',
  'Сервер': 'Server',
  'Серверы не пропадали, алертов по ним не было': 'No server went missing and there were no alerts about them',
  'Серверы, недоступность': 'Servers, unavailability',
  'Что шумело': 'What was noisy',
  'без отбоев': 'recoveries not counted',
  'и еще {n} со сбоями': 'and {n} more with failures',
  'к прошлому периоду': 'vs the previous period',
  'как в прошлый период': 'same as the previous period',
  'в прошлый период было {n}': 'previous period: {n}',
  'лежит сейчас': 'down now',
  'пропадали: {n} из {m}': 'went missing: {n} of {m}',
  'сбоев не было': 'no failures',
  'сбоев: {n}, лежали {d}': 'failures: {n}, down for {d}',
  'сутки': '24 hours',
  'считаем...': 'calculating...',
  '{d} п.п.': '{d} pp',
  '{n} раз, последний {t}': '{n} times, last {t}',
  'Заметка': 'Note',
  'Новых проблем нет, известных: {n}': 'No new problems, known: {n}',
  'Плановые работы: пока окно идет, алерты по выбранным серверам и мониторам молчат. То, что осталось сломанным, придет алертом после окна, а через 5 минут после конца - сверка одним сообщением, все ли вернулось в норму.':
    'Planned work: while the window is on, alerts for the selected servers and monitors stay quiet. Whatever is still broken comes as an alert after the window, and 5 minutes after the end a single message checks that everything is back to normal.',
  'Новое окно': 'New window',
  'Окон пока нет.': 'No windows yet.',
  'идет до {t}': 'on until {t}',
  'прошло': 'past',
  'следующее {t}': 'next {t}',
  'серверы': 'servers',
  'мониторы': 'monitors',
  'Удалить окно "{name}"?': 'Delete the window "{name}"?',
  'каждый день': 'every day',
  'пн': 'Mon',
  'вт': 'Tue',
  'ср': 'Wed',
  'чт': 'Thu',
  'пт': 'Fri',
  'сб': 'Sat',
  'вс': 'Sun',
  'Что делаем': 'What is being done',
  'напр. обновление ядра и перезагрузка': 'e.g. kernel update and reboot',
  'Начало': 'Start',
  'Конец': 'End',
  'Повтор': 'Repeat',
  'разово': 'once',
  'по дням недели': 'on weekdays',
  'Повторы - в то же время и той же длины, что и первое окно, по часовому поясу алертов.':
    'Repeats happen at the same time and with the same length as the first window, in the alert time zone.',
  'Сверка после окна: прислать, все ли вернулось в норму': 'Check after the window: report whether everything is back to normal',
  'Создать окно': 'Create window',
  'выбрано: {n}': 'selected: {n}',
  'поиск': 'search',
  'Идет обслуживание': 'Maintenance in progress',
  'алерты молчат, то, что останется сломанным, придет после окна': 'alerts are quiet, whatever is still broken comes after the window',
  'Скоро обслуживание': 'Maintenance soon',
  'серверов {n}': 'servers: {n}',
  'мониторов {n}': 'monitors: {n}',
  'в {t}': 'at {t}',
  'сигнал не реже, чем раз в {period}': 'a signal at least once every {period}',
  '{n} сут': '{n} d',
  'Адрес для скрипта видят админ и редактор': 'Only admins and editors see the address for the script',
  'Если на алерт никто не нажал «взял» и беда не ушла, повторить его в другой чат или выбранным людям в личку.':
    'If nobody pressed "on it" and the problem is still there, repeat the alert to another chat or to chosen people in private.',
  'Через, минут': 'After, minutes',
  'Сколько раз повторить': 'How many times to repeat',
  'Чат эскалации (ID, можно пусто)': 'Escalation chat (ID, may be empty)',
  'Кому в личку': 'Private messages to',
  '(Telegram не привязан)': '(Telegram not linked)',
  'Какие алерты повторять': 'Which alerts to repeat',
  'Сайт недоступен': 'Site is down',
  'Systemd: упал юнит': 'Systemd: unit failed',
  'Сертификат': 'Certificate',
  'Kubernetes: нода не готова или под давлением': 'Kubernetes: node not ready or under pressure',
  'Опрос бота запустится в течение 20 секунд после сохранения.': 'Bot polling starts within 20 seconds after saving.',
  'Временно тушит все уведомления. Инциденты продолжают фиксироваться.':
    'Temporarily silences all notifications. Incidents are still recorded.',
  '⏸ Алерты на паузе': '⏸ Alerts paused',
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
  'За {w} инцидентов не было.': 'No incidents in the last {w}.',
  'За {w} сбоев не было.': 'No failures in the last {w}.',
  'За {w} проверок нет.': 'No checks in the last {w}.',
  'последний — {t}': 'the last one was {t}',
  'показать прежние ({n})': 'show earlier ({n})',
  'скрыть прежние': 'hide earlier',
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
  'Через раз': 'Intermittent',
  'через раз': 'intermittent',
  'Сбои идут вперемешку с успешными проверками, с {time}': 'Failures mixed with successful checks since {time}',
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
  'будет создано мониторов: {n}, из них изнутри сервера: {m}':
    'monitors to create: {n}, checked from inside the server: {m}',
  'выбрать доступные ({n})': 'select reachable ({n})',
  'нет в DNS': 'not in DNS',
  'ошибка TLS': 'TLS error',
  'порт закрыт': 'port closed',
  'нет ответа': 'no response',
  'обрыв связи': 'conn. reset',
  'нет связи': 'can\'t connect',
  'нода офлайн': 'node offline',
  'агент молчит': 'agent silent',
  'только что': 'just now',
  'снаружи': 'outside',
  'изнутри': 'inside',
  'Снаружи: монитор проверяет панель — как посетитель сайта.':
    'Outside: the panel checks the site, the way a visitor opens it.',
  'Изнутри сервера {srv}: монитор проверяет агент на нём — для сайтов, закрытых снаружи белым списком.':
    'Inside server {srv}: its agent checks the site — for sites closed to the outside by an allowlist.',
  'Проверить доступность не удалось: {err}': 'Could not check reachability: {err}',
  'Проверяем доступность снаружи и изнутри серверов: готово {n} из {m}':
    'Checking reachability from outside and inside the servers: {n} of {m} done',
  'открываются снаружи: {n}': 'open from outside: {n}',
  'только изнутри сервера: {n}': 'only from inside the server: {n}',
  'не открываются: {n}': 'do not open: {n}',
  'проверено {ago}': 'checked {ago}',
  'Проверить эти домены ещё раз — снаружи и изнутри серверов':
    'Check these domains again — from outside and inside the servers',
  'перепроверить': 'check again',
  'Создаётся HTTPS-монитор на каждый домен — с тем вариантом проверки, что выделен в строке: снаружи его проверяет панель, изнутри — агент на сервере сайта. Доступность здесь проверена один раз, итог хранится 12 часов.':
    'One HTTPS monitor per domain, checked the way highlighted in its row: from outside by the panel, from inside by the agent on the site\'s server. Reachability here is checked once and the result is kept for 12 hours.',
  'выбрано {n}, за раз добавим {m}': '{n} selected, {m} added per run',
  'Поставлено на мониторинг: {n}': 'Added to monitoring: {n}',
  'Ничего не добавлено': 'Nothing was added',
  'группа «{g}»': 'group "{g}"',
  'открыть монитор': 'open monitor',
  'не добавлены: {n}': 'not added: {n}',
  'уже на мониторинге': 'already monitored',
  'снаружи — изнутри некому': 'outside — nobody to check inside',
  'главная не открылась — ищем типовые пути изнутри…': 'home page failed — looking for typical paths from inside…',
  'Обычный GET на адрес MCP-сервера получает {code} — так и положено, сервер жив. Монитор будет проверять {path} и считать рабочими ответы 200–499, кроме 404.':
    'A plain GET to an MCP server endpoint gets {code} — that is expected, the server is alive. The monitor will check {path} and treat 200–499 except 404 as working.',
  'Главная не открылась, а {path} отвечает {code}: у сервиса просто нет главной страницы. Монитор будет проверять {path}.':
    'The home page failed, but {path} answers {code}: the service simply has no home page. The monitor will check {path}.',
  '{path} отвечает {code}, как живой MCP-сервер — мониторить его': '{path} answers {code} like a live MCP server — monitor it',
  '{path} отвечает {code} — мониторить его': '{path} answers {code} — monitor it',
  'Монитор проверяет этот путь: главной у сервиса нет.': 'The monitor checks this path: the service has no home page.',
  'Главная не открылась, но отвечает типовой путь (/health, /api, /mcp…) — у сервиса просто нет главной страницы.':
    'The home page failed, but a typical path (/health, /api, /mcp…) answers — the service simply has no home page.',
  'без главной, по типовому пути: {n}': 'no home page, via a typical path: {n}',
  'Домен не нашёлся ни на одной ноде — проверять изнутри некому, поэтому монитор проверяет панель снаружи.':
    'The domain was not found on any node — nobody can check it from inside, so the panel checks it from outside.',
  'вне мониторинга осталось доменов: {n}': 'domains still not monitored: {n}',
  'новых доменов вне мониторинга не осталось': 'no new unmonitored domains left',
  'добавить ещё': 'add more',
  'маска — нет конкретного хоста': 'wildcard — no specific host',
  'regexp в server_name': 'regexp in server_name',
  'не доменное имя': 'not a domain name',
  'недопустимые символы': 'invalid characters',
  'пустое имя': 'empty name',
  'ненужных: {n}': 'not needed: {n}',
  'скрыть ненужные ({n})': 'hide not needed ({n})',
  'не нужны ({n})': 'not needed ({n})',
  'Мониторить не нужно — убрать из предложений':
    'Not worth monitoring — drop from suggestions',
  'Вернуть в предложения': 'Bring back to suggestions',
  'группа': 'group',
  'Дополнительные пути': 'Additional paths',
  'Дополнительные пути: {list}': 'Additional paths: {list}',
  'Пути ещё не проверялись: результат появится после ближайшей проверки.':
    'The paths have not been checked yet, the result will appear after the next check.',
  'добавляем…': 'adding…',
  'Место на диске': 'Disk space',
  'свободно {f} из {tt}': '{f} free of {tt}',
  'Обновить': 'Refresh',
  'докер не смог запустить': 'docker could not start it',
  'Доступ к Docker (прокси)': 'Docker access (proxy)',
  'файл размечен необычно (табы или сбитые отступы)': 'the file has an unusual layout (tabs or broken indentation)',
  'файл старого формата compose v1, без services:': 'the file is in the old compose v1 format, without services:',
  'в файле несколько YAML-документов (---), какой из них правится, не угадать': 'the file holds several YAML documents (---), no telling which one to edit',
  'в файле якоря YAML (&, *, <<), такую разметку надежно не поправить': 'the file uses YAML anchors (&, *, <<), such a layout cannot be edited reliably',
  'нужный ключ записан в одну строку ([...] или {...})': 'a key it needs is written inline ([...] or {...})',
  'сервиса нет в файле, из которого его запустили': 'the service is not in the file it was started from',
  'сервис docker-api в файле уже есть': 'the file already has a docker-api service',
  'у сервиса extends': 'the service uses extends',
  'у сервиса network_mode': 'the service uses network_mode',
  'сокет монтируется не из этого файла': 'the socket is not mounted from this file',
  'смонтирован весь /run или /var/run, а не один сокет': 'all of /run or /var/run is mounted, not just the socket',
  'DOCKER_HOST уже задан': 'DOCKER_HOST is already set',
  'сам правлю только caddy-docker-proxy, у traefik и nginx-proxy настройка другая':
    'only caddy-docker-proxy is edited automatically, traefik and nginx-proxy are set up differently',
  'контейнер запущен не через docker compose': 'the container was not started by docker compose',
  'контейнер запущен из нескольких compose-файлов': 'the container was started from several compose files',
  'нужен docker compose v2': 'docker compose v2 is needed',
  'на хосте нет docker-сокета': 'there is no docker socket on the host',
  'docker compose не принял исправленный файл': 'docker compose did not accept the fixed file',
  'прокси смотрит в интернет и держит docker-сокет: взлом прокси даст root на хосте':
    'the proxy faces the internet and holds the docker socket: breaking into the proxy gives root on the host',
  'внешний прокси держит docker-сокет: взлом прокси даст root на хосте':
    'an internet-facing proxy holds the docker socket: breaking into the proxy gives root on the host',
  '{name}: docker-сокет у {list} - взлом прокси даст root на хосте':
    '{name}: {list} holds the docker socket - breaking into the proxy gives root on the host',
  '{name}: устарел образ внешнего прокси {list} - обновите':
    '{name}: the internet-facing proxy image {list} is outdated - update it',
  '{name}: на сервере бэкапов старый restic {v} - обновите':
    '{name}: the backup server runs an old restic {v} - update it',
  'restic, которым сервер сам чистит и проверяет репозитории (prune, forget, check)':
    'the restic the server itself cleans and checks the repositories with (prune, forget, check)',
  'На сервере restic {v}, а helper ставит {target} (ту же версию, что у клиентов). Обычно его обновляет установка helper\'а, если скачать не вышло, запустите на сервере:':
    'The server runs restic {v}, the helper installs {target} (the same version the clients run). Installing the helper normally updates it; if the download failed, run on the server:',
  'образ внешнего прокси собран больше года назад': 'the internet-facing proxy image was built more than a year ago',
  'у сервиса в compose нет image': 'the service has no image in compose',
  'свежий тег': 'a fresh tag',
  'Не обновлять: убрать из "Требует действий" и группы ansible kervax_proxy_old':
    'Do not update: remove from "Action needed" and from the kervax_proxy_old ansible group',
  'оставить как есть': 'keep as is',
  'Оставлены как есть: {list}': 'Kept as is: {list}',
  '{list}: старый образ оставлен как есть': '{list}: the old image is kept as is',
  'старый образ прокси оставлен': 'old proxy image kept',
  'лог больше не пишется': 'log no longer written',
  'этого лога в отчете больше нет: сайты разнесли по своим логам или контейнер удалили, ошибки ниже - история':
    'this log is no longer in the report: the sites got their own logs or the container was removed, the errors below are history',
  'Docker: внешний прокси с docker-сокетом': 'Docker: internet-facing proxy with the docker socket',
  'остановлен вручную: docker такой контейнер сам не поднимает, это не авария и тревоги нет':
    'stopped by hand: docker does not bring such a container back by itself, this is not an outage and no alert is sent',
  'контейнер упал и не поднялся — по нему отправлен алерт':
    'container is down and did not come back — an alert was sent',
  'контейнер постоянно перезапускается — по нему отправлен алерт':
    'container keeps restarting — an alert was sent',
  'Приглушить': 'Mute',
  'без алерта': 'no alert',
  'По этому инциденту алерт не отправлялся.': 'No alert was sent for this incident.',
  'Алерт уходит после {n} неудачных проверок подряд — это около {m} мин непрерывного сбоя. Этот инцидент закончился раньше. Порог меняется в настройках монитора.':
    'An alert is sent after {n} consecutive failures — about {m} min of continuous downtime. This incident ended sooner. The threshold is in the monitor settings.',
  'О панели Kervax': 'About Kervax',
  'версия': 'version',
  'Лицензия': 'License',
  'Следит за сайтами, серверами, Docker, Kubernetes и бэкапами — и пишет в Telegram, когда что-то ломается.':
    'Watches sites, servers, Docker, Kubernetes and backups — and messages you on Telegram when something breaks.',
  'Исходный код на GitHub': 'Source code on GitHub',
  'Сообщить о проблеме': 'Report an issue',
  'Заменяет логотип в шапке и на экране входа. PNG, SVG, WebP или JPEG до {n} КБ.':
    'Replaces the logo in the header and on the login screen. PNG, SVG, WebP or JPEG up to {n} KB.',
  'Файл логотипа': 'Logo file',
  'Команда отправлена на сервер': 'Command sent to the server',
  'Подтверждение в отчёте сервера': 'Confirmation in the server report',
  '{n} с': '{n} s',
  'Сервер проверяет доступ к базе (pg_dumpall --schema-only)':
    'The server checks access to the database (pg_dumpall --schema-only)',
  'Сервер проверяет доступ к базе (mysqldump --no-data)':
    'The server checks access to the database (mysqldump --no-data)',
  'Сервер проверяет доступ к ClickHouse': 'The server checks access to ClickHouse',
  'Сервер проверяет доступ к Redis': 'The server checks access to Redis',
  'Сервер проверяет доступ к RabbitMQ': 'The server checks access to RabbitMQ',
  'Сервер настраивает снимок кластера': 'The server sets up the cluster snapshot',
  'Сервер снимает пробную копию grafana.db':
    'The server takes a trial copy of grafana.db',
  'Сервер проверяет neo4j-admin в образе базы':
    'The server checks neo4j-admin in the database image',
  'Сервер настраивает дамп': 'The server sets up the dump',
  'Сервер выключает дамп и удаляет локальные файлы':
    'The server disables the dump and removes local files',
  'Команда ещё выполняется на сервере — статус обновится в течение минуты.':
    'The command is still running on the server — the status will update within a minute.',
  'перед каждым бэкапом': 'before every backup',
  'ежедневно по своему таймеру — файлового бэкапа на ноде нет, копия только локальная':
    'daily on its own timer — the node has no file backup, the copy is local only',
  'Дампы {engine} включены → {dir}: {when}, храним {keep} последних, порог свободного места {minfree}%.':
    '{engine} dumps enabled → {dir}: {when}, keeping the last {keep}, free space threshold {minfree}%.',
  'Каталог {dir} добавлен в список бэкапа.': '{dir} was added to the backup list.',
  'ВНИМАНИЕ: {dir} в исключениях — дампы не попадут в бэкап!':
    'WARNING: {dir} is excluded — the dumps will not reach the backup!',
  'Первый дамп снимется с ближайшим бэкапом по расписанию.':
    'The first dump will be taken with the next scheduled backup.',
  'Первый дамп снимется при ближайшем запуске таймера.':
    'The first dump will be taken on the next timer run.',
  'Пробный дамп не прошел - дампы НЕ включены: {err}':
    'The trial dump failed - dumps were NOT enabled: {err}',
  'Дампы {engine} выключены, локальные файлы удалены (история осталась в restic).':
    '{engine} dumps disabled, local files removed (the history stays in restic).',
  'Дампы {engine} и так не были включены.': '{engine} dumps were not enabled anyway.',
  'Неизвестный движок дампа: {engine}': 'Unknown dump engine: {engine}',
  'Каталог дампов должен быть абсолютным путём: {dir}':
    'The dump directory must be an absolute path: {dir}',
  'Недопустимый каталог дампов: {dir}': 'Invalid dump directory: {dir}',
  'Корень / для дампов использовать нельзя.': 'The root / cannot be used for dumps.',
  'Бэкап запущен.': 'Backup started.',
  'Готово.': 'Done.',
  'restic уже {v} — обновлять не нужно.': 'restic is already {v} — no update needed.',
  'restic обновлён до {v} (было {old}).': 'restic updated to {v} (was {old}).',
  'Не удалось скачать restic {v}': 'Could not download restic {v}',
  'Контрольная сумма restic не совпала — обновление отменено.':
    'The restic checksum did not match — the update was cancelled.',
  'Скачанный restic не запускается — обновление отменено.':
    'The downloaded restic does not run — the update was cancelled.',
  'На ноде не найден restic.': 'No restic found on the node.',
  'Таймер бэкапа не найден.': 'Backup timer not found.',
  'Скрипт бэкапа не найден.': 'Backup script not found.',
  'Неверное время.': 'Invalid time.',
  'Неверный режим.': 'Invalid mode.',
  'Недопустимый путь: {path}': 'Invalid path: {path}',
  'Не указаны пути.': 'No paths given.',
  'Бэкап настроен ({mode}, {time}).': 'Backup configured ({mode}, {time}).',
  'Бэкап переведён под управление панели ({mode}, {time}).':
    'Backup moved under panel management ({mode}, {time}).',
  'Бэкап уже под управлением панели — переносить нечего.':
    'The backup is already managed by the panel — nothing to migrate.',
  'Бэкап настроен не панелью — доступов для восстановления у панели нет.':
    'The backup was not set up by the panel — it has no restore credentials.',
  'Helper на ноде не знает эту команду — переустановите его.':
    'The helper on the node does not know this command — reinstall it.',
  'rest-server обновлён до {v} (было {old}), отвечает {code}.':
    'rest-server updated to {v} (was {old}), answering {code}.',
  'rest-server развёрнут: порт {port}, append-only и private-repos, HTTP {code}, ufw {ufw}.':
    'rest-server deployed: port {port}, append-only and private-repos, HTTP {code}, ufw {ufw}.',
  'rest-server уже развёрнут и работает (порт {port}, HTTP {code}, ufw {ufw}).':
    'rest-server is already deployed and running (port {port}, HTTP {code}, ufw {ufw}).',
  'Обновление образа не удалось: {err}': 'The image update failed: {err}',
  'HTTPS rest-server запущен на порту {port}.':
    'HTTPS rest-server started on port {port}.',
  'Репозиторий {name} создан.': 'Repository {name} created.',
  'Скрипты ротации пересобраны: {n}.': 'Rotation scripts regenerated: {n}.',
  'Сведения с ноды давно не обновлялись: helper бэкапа там не запускается. Переустановите его.':
    'The node has not updated this for a long time: the backup helper does not run there. Reinstall it.',
  'ежедневно {time}': 'daily {time}',
  'ежедневно': 'daily',
  'ежечасно': 'hourly',
  'еженедельно': 'weekly',
  'свои бэкапы: {n}': 'own backups: {n}',
  'свои бэкапы: {n}, с проблемой: {b}': 'own backups: {n}, failing: {b}',
  'таймер выключен': 'timer disabled',
  'не видно': 'not visible',
  'не отслеживается': 'not watched',
  'таймер': 'timer',
  'метрики': 'metrics',
  'свежий файл бэкапа': 'newest backup file',
  'последний успех': 'last success',
  'последний запуск': 'last run',
  'лог': 'log',
  'базы': 'databases',
  'Не видно, когда задание отработало: метрик у него нет, а файлов бэкапа панель не нашла.':
    'It is not visible when the job last ran: it has no metrics, and the panel found no backup files.',
  'Запуски видны по журналу cron, а итог — нет: скрипт не пишет ни метрик, ни отметки о результате, а файлов бэкапа панель не нашла.':
    'Runs are visible in the cron log, the outcome is not: the script writes neither metrics nor a status file, and the panel found no backup files.',
  'Таймер выключен. Алерт придет, если свежего бэкапа не будет дольше обычного.':
    'The timer is off. An alert comes if there is no fresh backup for longer than usual.',
  'Таймер выключен, задание еще ни разу не запускалось.': 'The timer is off, the job has never run yet.',
  'запускается': 'runs',
  'последний снимок restic': 'last restic snapshot',
  'отметка': 'status file',
  'по кэшу restic': 'from the restic cache',
  'Сервер находит под и проверяет доступ к базе':
    'The server finds the pod and checks access to the database',
  'У helper на этой ноде нет доступа к кластеру Kubernetes: дамп из пода включается на управляющей ноде.':
    'The helper on this node has no access to the Kubernetes cluster: dumps from pods are enabled on a control-plane node.',
  'В пространстве {ns} нет {kind} {name}.':
    'There is no {kind} {name} in namespace {ns}.',
  'У {kind} {name} в пространстве {ns} нет запущенного пода.':
    '{kind} {name} in namespace {ns} has no running pod.',
  'ClickHouse не пустил пользователя default: база требует пароль. Helper backup-setup 0.28 снимает схему из файлов, без входа в базу — обновите его на ноде.':
    'ClickHouse refused the default user: the database requires a password. The backup-setup helper 0.28 takes the schema from files without logging in — update it on the node.',
  'Не найден каталог со схемой ClickHouse: {dir}.': 'ClickHouse schema directory not found: {dir}.',
  'В каталоге со схемой ClickHouse не нашлось ни одной базы.':
    'No databases were found in the ClickHouse schema directory.',
  'Redis не ответил на ping: похоже, нужен пароль, которого нет в окружении базы.':
    'Redis did not answer ping: it seems to need a password that is not in its environment.',
  'Дамп из пода для {engine} не поддерживается.':
    'Dumps from a pod are not supported for {engine}.',
  'отслеживать': 'watch',
  'не отслеживать': 'do not watch',
  'Свои бэкапы на ноде': 'Own backups on the node',
  'Настроены без панели. Панель нашла их сама и следит за их работой, ничего на ноде не меняя.':
    'Set up without the panel. The panel found them by itself and watches them without changing anything on the node.',
  'скрыть неотслеживаемые': 'hide unwatched',
  'не отслеживаются ({n})': 'not watched ({n})',
  'Панельного бэкапа на ноде нет, но есть свой - за ним панель следит':
    'The node has no panel backup, but it has its own, and the panel watches it',
  'Свой бэкап (настроен без панели)': 'Own backup (set up without the panel)',
  '{name}: свой бэкап не отрабатывает — {jobs}':
    '{name}: own backup is failing — {jobs}',
  'На тёмной теме — отдельный вариант логотипа, его цвета подстроены под тему.':
    'The dark theme uses a separate logo variant, with its colours adjusted to the theme.',
  'На тёмной теме цвета логотипа подстроены под тему.':
    'On the dark theme the logo colours are adjusted to the theme.',
  'На тёмной теме логотип на плашке: у картинки свой фон или её нельзя перекрасить.':
    'On the dark theme the logo sits on a plate: the image has its own background or cannot be recoloured.',
  'На тёмной теме логотип показан как есть.': 'The dark theme shows the logo as is.',
  'На светлой теме цвета логотипа подстроены под тему.':
    'On the light theme the logo colours are adjusted to the theme.',
  'На светлой теме логотип на плашке: у картинки свой фон.':
    'On the light theme the logo sits on a plate: the image has its own background.',
  'Нужен, только если для тёмного фона у логотипа есть своя версия: цвета основного панель подстраивает под тему сама.':
    'Only needed if the logo has its own version for dark backgrounds: the panel adjusts the colours of the main one to the theme by itself.',
  'Оформление': 'Appearance',
  'авто: подстроить цвета под тему': 'auto: adjust colours to the theme',
  'всегда на плашке': 'always on a plate',
  'как есть': 'as is',
  'Вариант для тёмной темы (необязательно)': 'Dark theme variant (optional)',
  'Убрать вариант для тёмной темы': 'Remove the dark theme variant',
  'На тёмной теме — отдельный вариант логотипа.':
    'The dark theme uses a separate logo variant.',
  'На светлой теме логотип показан как есть.': 'The light theme shows the logo as is.',
  'Файл больше {n} КБ — уменьшите логотип': 'The file is over {n} KB — make the logo smaller',
  'Не удалось прочитать файл': 'Could not read the file',
  'тёмная тема': 'dark theme',
  'светлая тема': 'light theme',
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
  'Уже в мониторинге — открыть монитор': 'Already monitored — open the monitor',
  'Уже в мониторинге': 'Already monitored',
  'Маска или regexp — монитору нужен конкретный адрес':
    'Wildcard or regexp — a monitor needs a concrete address',
  'нет данных': 'no data',
  'предупредить за, дн.': 'warn, days before',
  'SSL истекает': 'SSL expiring',
  'Домен истекает': 'Domain expiring',
  'SSL-сертификат': 'TLS certificate',
  'Домен': 'Domain',
  // --- окно локаций разделами (LocationsModal) ---
  'Уйти без сохранения? Изменения пропадут.':
    'Leave without saving? The changes will be lost.',
  'Локация добавлена.':
    'Location added.',
  'Где недоступно':
    'Where it is down',
  'Новая локация':
    'New location',
  'Откуда панель проверяет сайты и порты нод':
    'Where the panel checks sites and node ports from',
  'Нужны только название и адрес. Прокси проверяется сразу, сохранить можно только рабочий.':
    'Only a name and an address are needed. The proxy is checked right away, and only a working one can be saved.',
  'Настройки':
    'Settings',
  'выход в интернет:':
    'exit to the internet:',
  'Выход в интернет:':
    'Exit to the internet:',
  'HTTP, HTTPS или SOCKS5, логин и пароль можно в адресе: socks5://user:pass@host:1080.':
    'HTTP, HTTPS or SOCKS5, a login and password can go in the address: socks5://user:pass@host:1080.',
  'Пустой адрес - проверка с самой панели, это основная проверка всех мониторов.':
    'An empty address means checking from the panel itself, which is the main check of all monitors.',
  'Проверять через нее сайты':
    'Check sites through it',
  'Выключено - только порты нод снаружи: для сайтов, открытых лишь своей сети, не будет ложных "недоступен".':
    'Off: only node ports from outside, so sites open only to your own network do not get a false "down".',
  'Удалить локацию':
    'Delete location',
  'основная проверка':
    'main check',
  'без сайтов':
    'no sites',
  'порты нод снаружи':
    'node ports from outside',
  'выключена':
    'off',
  'Локация выключена: через нее ничего не проверяется.':
    'The location is off: nothing is checked through it.',
  'Эта локация только для внешней проверки портов нод: через нее панель смотрит, какие порты видны из интернета. Сайты через нее не проверяются, поэтому сайты, открытые лишь своей сети, не дают ложных "недоступен".':
    'This location is only for checking node ports from outside: the panel uses it to see which ports are visible from the internet. Sites are not checked through it, so sites open only to your own network do not get a false "down".',
  'Сломан сам прокси: отсюда недоступны все сайты ({n}), с самой панели часть из них отвечает, а сам прокси не отвечает на проверку. Вместо алертов по каждому сайту приходит один алерт про прокси. Ошибка: {err}':
    'The proxy itself is broken: all sites ({n}) are down from here, some of them respond from the panel itself, and the proxy does not answer the check. Instead of an alert for every site, one alert about the proxy is sent. Error: {err}',
  'Проверить прокси':
    'Check proxy',
  'мониторов':
    'monitors',
  'сайтов проверяется':
    'sites checked',
  'доступно за сутки':
    'available over 24 h',
  'средний ответ':
    'average response',
  'недоступны сейчас':
    'down now',
  'Недоступны отсюда':
    'Down from here',
  'только отсюда':
    'only from here',
  'и из других':
    'from others too',
  '"Только отсюда" - из остальных мест сайт отвечает: похоже на блокировку или фильтр по стране, а не на падение.':
    '"Only from here": the site responds from the other places, which looks like blocking or a country filter rather than an outage.',
  'отсюда не проверяется':
    'not checked from here',
  'медленно':
    'slow',
  'Сайты, которые сейчас недоступны хоть откуда-то: с самой панели или через локации. Недоступен из одной, а из других отвечает - похоже на блокировку по стране или фильтр, а не на падение сайта.':
    'Sites that are down from at least one place right now: from the panel itself or through locations. Down from one place but responding from others looks like blocking by country or a filter, not a site outage.',
  'Прокси локации «{name}» не отвечает: ее столбец в таблице не в счет.':
    'The proxy of the location "{name}" does not respond: its column in the table does not count.',
  'Через прокси пока ничего не проверяется: добавьте локацию, тогда будет видно, откуда сайт недоступен.':
    'Nothing is checked through a proxy yet: add a location to see where a site is down from.',
  '✓ Все сайты отвечают отовсюду.':
    '✓ All sites respond from everywhere.',
  'Сайт':
    'Site',
  'Сама панель':
    'The panel itself',
  'Показаны первые 300 сайтов.':
    'The first 300 sites are shown.',
  'напрямую, с самой панели':
    'direct, from the panel itself',
  'SOCKS5-прокси':
    'SOCKS5 proxy',
  'HTTPS-прокси':
    'HTTPS proxy',
  'HTTP-прокси':
    'HTTP proxy',
  '+ Добавить локацию':
    '+ Add location',
  // --- окно алертов разделами (AlertsModal) ---
  'Чаты и группы':
    'Chats and groups',
  'Бот и адрес API':
    'Bot and API address',
  'Кнопки и эскалация':
    'Buttons and escalation',
  'Антифлуд и пауза':
    'Flood control and pause',
  'Куда слать':
    'Where to send',
  'Как слать':
    'How to send',
  'Что слать':
    'What to send',
  'Куда уходят алерты каждой группы. Основной чат получает все или только то, что не забрали чаты групп.':
    'Where the alerts of each group go. The main chat gets everything or only what the group chats did not take.',
  'Токен выдает @BotFather. Один бот шлет во все чаты: основной, чаты групп и эскалацию.':
    'The token comes from @BotFather. One bot sends to all chats: the main one, group chats and escalation.',
  'Для своих систем: каждый алерт уходит POST-запросом, текст в поле text JSON. Webhook получает все алерты, без деления по группам.':
    'For your own systems: every alert is sent as a POST request, the text is in the text field of the JSON. The webhook gets all alerts, without splitting by group.',
  'Что можно сделать с алертом прямо в Telegram.':
    'What you can do with an alert right in Telegram.',
  'Когда что-то упало массово, и когда алерты нужно на время выключить.':
    'When many things fail at once, and when alerts need to be turned off for a while.',
  'Какие алерты о серверах слать и каким текстом. Пороги и исключения - в карточке сервера.':
    'Which server alerts to send and with what text. Thresholds and exceptions are in the server card.',
  'Какие алерты о сайтах слать и каким текстом. Пороги и исключения - в карточке монитора.':
    'Which site alerts to send and with what text. Thresholds and exceptions are in the monitor card.',
  'Закрыть без сохранения? Изменения пропадут.':
    'Close without saving? The changes will be lost.',
  'Каналы настроены':
    'Channels configured',
  'Сначала укажите токен бота: без него в Telegram ничего не уйдет.':
    'Set the bot token first: without it nothing goes to Telegram.',
  'Под алертом: заглушить на час, до утра или насовсем, «взял». Команды в чате: /status, /mutes, /id. Панель сама читает сообщения бота: если этого бота уже читает другая программа или вторая панель, заведите для панели отдельного бота.':
    'Under each alert: mute for an hour, until morning or for good, "on it". Chat commands: /status, /mutes, /id. The panel reads the bot updates itself: if another program or a second panel already reads this bot, create a separate bot for this panel.',
  'Эскалация работает вместе с кнопками: алерт повторяется, пока никто не нажал «взял».':
    'Escalation works together with the buttons: the alert is repeated until somebody presses "on it".',
  'Есть несохраненные изменения':
    'There are unsaved changes',
  'Основной чат':
    'Main chat',
  'серверы и сайты':
    'servers and sites',
  'сайты':
    'sites',
  'Общие алерты':
    'Panel-wide alerts',
  'про саму панель':
    'about the panel itself',
  'никуда':
    'nowhere',
  'Webhook получает все алерты, без деления по группам.':
    'The webhook gets all alerts, without splitting by group.',
  'Чат для групп':
    'Group chat',
  '+ Чат для групп':
    '+ Group chat',
  'У видов алертов ({n}) осталось старое ограничение по группам или объектам: алерты остальных по ним не уходят никуда, даже в основной чат.':
    'Alert types ({n}) still have an old limit by group or object: alerts of everything else for them go nowhere, not even to the main chat.',
  // --- чаты по группам (AlertsModal) ---
  'групп нет':
    'no groups',
  'Алерты серверов и сайтов из выбранных групп уходят в отдельный чат: например, разработчикам - только про их проект. Бота нужно добавить в этот чат. Id группы видно в Telegram, а если включены команды боту, бот подскажет его в ответ на /id.':
    'Alerts of servers and sites from the selected groups go to a separate chat: for example, developers get only their project. Add the bot to that chat. The group id is shown in Telegram, and if bot commands are on, the bot replies to /id with it.',
  'напр. Разработка':
    'e.g. Development',
  'Последнее сообщение чат не принял ({at}): {err}. Такие алерты уходят в основной чат.':
    'The chat did not accept the last message ({at}): {err}. Such alerts go to the main chat.',
  'Проверочное сообщение отправлено.':
    'The test message was sent.',
  'Основной чат получает':
    'The main chat gets',
  'все алерты':
    'all alerts',
  'только остальное':
    'only the rest',
  'Алерты групп, у которых есть свой чат, в основной чат не попадают. Туда идут остальные группы, серверы без группы и общие алерты панели.':
    'Alerts of groups that have their own chat do not go to the main chat. It gets the other groups, servers without a group and panel-wide alerts.',
  'Основной чат получает все алерты, а чаты по группам - копии про свои группы.':
    'The main chat gets all alerts, and the group chats get copies about their groups.',
  'только группы: {list}':
    'only groups: {list}',
  'только серверов: {n}':
    'only servers: {n}',
  'только мониторов: {n}':
    'only monitors: {n}',
  'Снять ограничения':
    'Remove the limits',
  // --- новый монитор: пробная проверка (AddMonitor) ---
  'Это монитор сертификата старого типа: его настройки меняются только через API. Удалить его можно кнопкой выше, а за сертификатом следит и обычный монитор сайта.':
    'This is an old-style certificate monitor: its settings can only be changed through the API. You can delete it with the button above, and a regular site monitor watches the certificate too.',
  'Настройки поменялись - нажмите "проверить снова": ответ сравнится уже с ними.':
    'The settings changed - press "check again": the answer will be compared with them.',
  'Ответ совпадает с ожидаемым. Станет другим - придет алерт.':
    'The answer matches the expected one. If it becomes different, an alert arrives.',
  'Ответ тот же, что монитор запомнил. Изменится - придет алерт.':
    'The answer is the same as the one the monitor remembered. If it changes, an alert arrives.',
  'Монитор запомнит этот ответ и пришлет алерт, когда он изменится.':
    'The monitor will remember this answer and send an alert when it changes.',
  'Монитор запомнит этот ответ и пришлет алерт, когда он изменится. Если ответ должен быть другим, задайте его ниже.':
    'The monitor will remember this answer and send an alert when it changes. If the answer should be different, set it below.',
  'DNS сейчас не дает ответа: {msg}. Проверьте имя и тип записи, а если так и задумано, монитор можно выключить ниже.':
    'DNS gives no answer right now: {msg}. Check the name and the record type; if this is intended, you can disable the monitor below.',
  'DNS сейчас не дает ответа: {msg}. Если добавить так, монитор сразу будет красным и придет алерт. Проверьте имя и тип записи.':
    'DNS gives no answer right now: {msg}. If you add it like this, the monitor turns red at once and an alert arrives. Check the name and the record type.',
  'Ответ не тот, что ждем: {msg}. Монитор будет красным, пока ответ не станет таким.':
    'The answer is not the expected one: {msg}. The monitor stays red until the answer becomes the expected one.',
  'Ответ не тот, что ждем: {msg}. Если добавить так, монитор сразу будет красным и придет алерт.':
    'The answer is not the expected one: {msg}. If you add it like this, the monitor turns red at once and an alert arrives.',
  '{msg}. Монитор пришлет алерт; если новый ответ правильный, примите его кнопкой в карточке монитора.':
    '{msg}. The monitor will send an alert; if the new answer is right, accept it with the button in the monitor card.',
  '{h} ч {m} мин':
    '{h} h {m} min',
  'сайт или порт':
    'a site or a port',
  'название монитора':
    'monitor name',
  'нет сигнала {dur}, ждали каждые {every}':
    'no signal for {dur}, expected every {every}',
  'записей {type} нет':
    'no {type} records',
  'Так придет алерт, если скрипт не постучится вовремя. Сообщит об ошибке - придет сразу, с ее текстом.':
    'This is the alert if the script does not report in time. If it reports an error, the alert arrives at once, with its text.',
  'Так придет после сохранения: ответ DNS сейчас не тот.':
    'This arrives after saving: the DNS answer is wrong now.',
  'Так придет сразу после добавления: ответ DNS сейчас не тот.':
    'This arrives right after adding: the DNS answer is wrong now.',
  'Так придет алерт, если ответ DNS изменится или пропадет.':
    'This is the alert if the DNS answer changes or disappears.',
  'Эту запись смотрит еще монитор "{name}": оба шлют те же алерты.':
    'Monitor "{name}" watches this record too: both send the same alerts.',
  'На эту запись уже есть монитор "{name}": второй будет слать те же алерты.':
    'There is already a monitor "{name}" for this record: a second one would send the same alerts.',
  'свой':
    'custom',
  'Алерт, если сигнала нет дольше {dur} или скрипт сообщил об ошибке.':
    'An alert if there is no signal for longer than {dur} or the script reports an error.',
  'Адрес для скрипта появится в карточке монитора после сохранения.':
    'The address for the script appears in the monitor card after saving.',
  'Что отвечает DNS':
    'What DNS answers',
  'задать ожидаемый ответ':
    'set the expected answer',
  '- если ответ должен быть именно таким, а не просто не меняться.':
    '- if the answer must be exactly this, not just stay the same.',
  'Скрипт или крон сам стучится в панель после каждого прогона: бэкап, выгрузка, очередь.':
    'The script or cron job reports to the panel itself after each run: a backup, an export, a queue.',
  'Домен и какую запись спрашивать.':
    'The domain and which record to ask for.',
  'Имя поменялось - проверьте снова.':
    'The name changed - check again.',
  'Нажмите "Проверить": панель спросит запись у DNS и покажет ответ еще до сохранения.':
    'Press "Check": the panel asks DNS for the record and shows the answer before saving.',
  'напр. ночной бэкап':
    'e.g. nightly backup',
  '- допуск по времени, заглушенные алерты.':
    '- time allowance, muted alerts.',
  '- допуск по времени. Можно поменять и потом в карточке монитора.':
    '- time allowance. It can also be changed later in the monitor card.',
  '- интервал, DNS-сервер, заглушенные алерты.':
    '- interval, DNS server, muted alerts.',
  '- интервал и DNS-сервер. Можно поменять и потом в карточке монитора.':
    '- interval and DNS server. They can also be changed later in the monitor card.',
  'сейчас {dur}: крон стартует не в нулевую секунду, а скрипт идет не мгновенно':
    'now {dur}: cron does not start at second zero, and the script takes time',
  'ждать сигнал':
    'expect a signal',
  'раз в {every}':
    'every {every}',
  'если его нет {dur}':
    'if it is missing for {dur}',
  'Назовите монитор: название будет в алерте.':
    'Name the monitor: the name goes into the alert.',
  'запись {type}':
    'the {type} record',
  'С панели порт не открывается: {msg}. Изнутри сервера и из локаций проверяются только сайты. Проверьте адрес и порт, а если сервис на обслуживании, монитор можно выключить ниже.':
    'The port does not open from the panel: {msg}. Only sites can be checked from inside a server or from locations. Check the address and the port; if the service is under maintenance, you can disable the monitor below.',
  'Сайт просит вход (401), а монитор считает это сбоем. Выберите ниже: считать 401 нормой или дать доступ.':
    'The site asks to sign in (401), and the monitor counts it as a failure. Choose below: treat 401 as normal or give access.',
  'Открывается, но из {n} адресов домена с ошибкой отвечают {bad}: часть посетителей видит ошибку. Монитор этого не заметит, пока не включить "проверять все адреса" ниже.':
    'It opens, but of the {n} addresses of the domain {bad} answer with an error: some visitors see it. The monitor will not notice until "check every address" is turned on below.',
  'Сейчас адрес не открывается ниоткуда: {msg}. Проверьте адрес, а если сайт на обслуживании, монитор можно выключить ниже.':
    'The address opens from nowhere right now: {msg}. Check the address; if the site is under maintenance, you can disable the monitor below.',
  'после {n} сбоев подряд':
    'after {n} failures in a row',
  'так настроено у этого монитора':
    'this is how this monitor is set up',
  'не проверено: адрес поменялся':
    'not checked: the address changed',
  'Монитор выключен: алертов не будет, пока его не включат.':
    'The monitor is disabled: no alerts until it is enabled.',
  'Так придет после сохранения: адрес сейчас не открывается.':
    'This arrives after saving: the address does not open now.',
  'Этот адрес смотрит еще монитор "{name}": оба шлют те же алерты.':
    'Monitor "{name}" watches this address too: both send the same alerts.',
  'Вход и заголовки':
    'Sign-in and headers',
  'Проверять все адреса домена':
    'Check every address of the domain',
  'Монитор включен':
    'Monitor enabled',
  'выключенный не проверяется и не шлет алертов':
    'a disabled monitor is not checked and sends no alerts',
  '- интервал, ожидаемый ответ, пути, заголовки, повторы, локации, заглушенные алерты.':
    '- interval, expected answer, paths, headers, retries, locations, muted alerts.',
  '- интервал, порог "медленно", повторы, заглушенные алерты.':
    '- interval, the "slow" threshold, retries, muted alerts.',
  'Сертификат: напоминать за, дн.':
    'Certificate: remind, days before',
  'Порт открыт с панели.':
    'The port is open from the panel.',
  'С панели порт не открывается: {msg}. Изнутри сервера и из локаций проверяются только сайты, поэтому такой монитор сразу будет красным. Проверьте адрес и порт, а если сервис только поднимается, можно добавить выключенным.':
    'The port does not open from the panel: {msg}. Only sites can be checked from inside a server or from locations, so this monitor will be red right away. Check the address and the port; if the service is still coming up, you can add it disabled.',
  'Открывается, но из {n} адресов домена с ошибкой отвечают {bad}: часть посетителей видит ошибку. Включено "проверять все адреса", поэтому монитор будет красным, пока их не починят. Не нужно - снимите галочку ниже.':
    'It opens, but of the {n} addresses of the domain {bad} answer with an error: some visitors see it. "Check every address" is on, so the monitor stays red until they are fixed. Not needed? Untick it below.',
  'Сайт отвечает, но просит вход (401). Сейчас 401 считается нормой: монитор следит только, что сайт жив. Чтобы проверять с входом, выберите "дать доступ".':
    'The site answers but asks to sign in (401). For now 401 counts as normal: the monitor only watches that the site is alive. To check behind the sign-in, choose "give access".',
  'Открывается, но {msg}. Лучше всего проверять с панели: так его видят посетители.':
    'It opens, but {msg}. Checking from the panel is best: that is how visitors see it.',
  'Открывается с панели. Лучше всего проверять с панели: так его видят посетители.':
    'It opens from the panel. Checking from the panel is best: that is how visitors see it.',
  'Из локаций {names} не открывается.':
    'It does not open from these locations: {names}.',
  'С панели не открывается ({msg}), а изнутри {srv} работает. Проверять изнутри сервера: агент {srv} откроет сайт на localhost с этим именем. Такой монитор скажет, что сайт жив, но не скажет, видят ли его посетители.':
    'It does not open from the panel ({msg}), but works from inside {srv}. Check from inside the server: the agent on {srv} opens the site on localhost under this name. Such a monitor tells that the site is alive, not whether visitors can see it.',
  'С панели не открывается ({msg}). Жду ответ изнутри {srv}...':
    'It does not open from the panel ({msg}). Waiting for the answer from inside {srv}...',
  'С панели не открывается ({msg}), хотя из локаций {names} открывается: похоже, панель не пускают (белый список или блокировка). Монитор с панели будет красным: пустите адрес панели или проверяйте изнутри сервера, если сайт на сервере с агентом.':
    'It does not open from the panel ({msg}), though it opens from {names}: the panel seems to be kept out (an allowlist or a block). A monitor from the panel will be red: let the panel address in, or check from inside the server if the site runs on a server with the agent.',
  'С панели не открывается ({msg}). Проверяю остальное...':
    'It does not open from the panel ({msg}). Checking the rest...',
  'Сейчас адрес не открывается ниоткуда: {msg}. Если добавить так, монитор сразу будет красным и придет алерт. Проверьте адрес, а если сайт только поднимается, можно добавить выключенным.':
    'The address opens from nowhere right now: {msg}. Added like this, the monitor turns red at once and an alert goes out. Check the address; if the site is still coming up, you can add it disabled.',
  'сразу':
    'at once',
  'после 3 сбоев подряд':
    'after 3 failures in a row',
  'если лежит 5 минут':
    'if down for 5 minutes',
  'после первой неудачной проверки':
    'after the first failed check',
  'около {n} мин: единичный сбой не будит':
    'about {n} min: a single glitch wakes nobody',
  'для того, что иногда моргает':
    'for things that blink now and then',
  'изнутри {srv}':
    'from inside {srv}',
  'сервера':
    'the server',
  'с панели и из локаций':
    'from the panel and the locations',
  'с панели':
    'from the panel',
  'раз в {n} мин':
    'every {n} min',
  'раз в {n} с':
    'every {n} s',
  '{ok} из {n} адресов отвечают':
    '{ok} of {n} addresses answer',
  'жду агента {srv}...':
    'waiting for the agent on {srv}...',
  'недоступно':
    'not available',
  'сервер не на связи':
    'the server is offline',
  'агент не ответил за {n} с':
    'the agent did not answer within {n} s',
  'открывается из {ok} из {n}':
    'opens from {ok} of {n}',
  'нет: {list}':
    'not from: {list}',
  'Из интернета, как посетитель. Подходит почти всем сайтам.':
    'From the internet, like a visitor. Fits almost every site.',
  'Подключение к порту из интернета, с самой панели.':
    'A connection to the port from the internet, from the panel itself.',
  'Изнутри {srv}':
    'From inside {srv}',
  'Изнутри сервера проверяются только сайты.':
    'Only sites can be checked from inside a server.',
  'Домен не нашелся на серверах с агентом: проверять изнутри некому.':
    'The domain was not found on servers with the agent: nobody can check it from inside.',
  'Агент откроет сайт на самом сервере, через localhost. Для сайтов за белым списком.':
    'The agent opens the site on the server itself, via localhost. For sites behind an allowlist.',
  'домен есть в {web} на {srv}':
    'the domain is in {web} on {srv}',
  'Как с панели, и еще через прокси из других стран и сетей: видно, если сайт не открывается из какой-то сети.':
    'Like from the panel, plus through proxies in other countries and networks: shows when the site does not open from some network.',
  'Через локации проверяются только сайты.':
    'Only sites can be checked through locations.',
  'сервер не ответил на попытку соединения (ConnectTimeout)':
    'the server did not answer the connection attempt (ConnectTimeout)',
  'порт {port}: недоступен':
    'port {port}: unreachable',
  'монитор':
    'monitor',
  'доступен':
    'reachable',
  'Монитор добавится выключенным: алертов не будет, пока его не включат.':
    'The monitor is added disabled: no alerts until it is enabled.',
  'По правилам алертов этот монитор алерт о падении не шлет.':
    'By the alert rules this monitor sends no down alert.',
  'Так придет сразу после добавления: адрес сейчас не открывается.':
    'This arrives right after adding: the address does not open now.',
  'Так придет алерт, если адрес перестанет открываться.':
    'This is the alert you get if the address stops opening.',
  'Новый монитор':
    'New monitor',
  'Что проверять':
    'What to check',
  'Проверить снова':
    'Check again',
  'это сайт':
    'a site',
  'это TCP-порт {port}':
    'TCP port {port}',
  'Сайт, адрес API или хост:порт.':
    'A site, an API address or host:port.',
  'Нужен сайт на этом порту - допишите https:// в начале.':
    'Need a site on this port? Put https:// in front.',
  'Нужно другое:':
    'Something else:',
  'сигнал от скрипта':
    'a signal from a script',
  'DNS-записи':
    'DNS records',
  'Адрес поменялся - проверьте снова.':
    'The address changed, check it again.',
  'Нажмите "Проверить": панель откроет адрес с панели, изнутри сервера, где нашелся домен, и из локаций. Так видно, работает ли он, еще до сохранения.':
    'Press "Check": the panel opens the address from the panel, from inside the server where the domain was found, and from the locations. You see whether it works before saving.',
  'лучше так':
    'best',
  'На этот адрес уже есть монитор "{name}": второй будет слать те же алерты.':
    'This address already has a monitor "{name}": a second one would send the same alerts.',
  'открыть его':
    'open it',
  'Сайт просит вход':
    'The site asks to sign in',
  '401 - норма':
    '401 is fine',
  'дать доступ':
    'give access',
  'Заголовок, по строке':
    'Headers, one per line',
  'или логин (HTTP Basic)':
    'or a login (HTTP Basic)',
  'Заголовок пишется как "Имя: значение": {list}':
    'A header is written as "Name: value": {list}',
  'проверить снова':
    'check again',
  'в алерты не попадает, наблюдатели его не видят':
    'never goes into alerts, viewers do not see it',
  'Что еще проверить':
    'What else to check',
  'Следить за сертификатом':
    'Watch the certificate',
  'сейчас {n} дн.':
    'now {n} d',
  'напомню за {days} дн.':
    'reminders {days} days before',
  'Проверять все адреса домена ({n})':
    'Check every address of the domain ({n})',
  'иначе мертвый адрес за балансировщиком не видно':
    'otherwise a dead address behind a load balancer goes unseen',
  'Добавить выключенным':
    'Add disabled',
  'включите, когда заработает':
    'enable it once it works',
  'Слать алерт':
    'Send the alert',
  'тонкая настройка':
    'fine tuning',
  '- интервал, ожидаемый ответ, пути, заголовки. Все это можно поменять и потом в карточке монитора.':
    '- interval, expected answer, paths, headers. All of it can be changed later in the monitor card.',
  '- интервал и порог "медленно". Можно поменять и потом в карточке монитора.':
    '- interval and the "slow" threshold. Can be changed later in the monitor card.',
  'Тонкая настройка':
    'Fine tuning',
  'Не проверять сертификат':
    'Do not verify the certificate',
  'самоподписанный или выпущен на другое имя':
    'self-signed or issued for another name',
  'После правок нажмите "Проверить снова": проверка пойдет уже с ними.':
    'After changes press "Check again": the check runs with them.',
  'Так придет алерт':
    'What the alert looks like',
  'Будет:':
    'Result:',
  'проверять':
    'checked',
  'алерт':
    'alert',
  'выключенным':
    'disabled',
  'Сначала проверка, потом сохранение.':
    'Check first, then save.',
  'Сохраняю...':
    'Saving...',
  'Добавить монитор':
    'Add monitor',
  'просит вход (HTTP 401)':
    'asks to sign in (HTTP 401)',
  'Сайт просит вход (401): впишите заголовок или логин и пароль ниже и нажмите "проверить снова".':
    'The site asks to sign in (401): fill in a header or a login and password below and press "check again".',
  // --- лог упавшего Job и глушение алертов кластера (KuberPage) ---
  'под {pod}, конец лога': 'pod {pod}, end of the log',
  'Весь лог': 'Full log',
  'в лог ничего не написал': 'wrote nothing to the log',
  'пода уже нет, лога тоже': 'the pod is gone, and its log with it',
  'Лог пода {pod}': 'Log of pod {pod}',
  'Заглушить алерты подов и Job': 'Mute pod and Job alerts',
  'не алертить о подах, Job и CronJob этого кластера:': 'do not alert about pods, Jobs and CronJobs of this cluster:',
  'не алертят: {what}': 'not alerting: {what}',
  'поды, Job и CronJob': 'pods, Jobs and CronJobs',
  'не алертить об упавших Job:': 'do not alert about failed Jobs:',
  'не алертить об упавших Job': 'do not alert about failed Jobs',
  // --- лог последнего запуска CronJob и почему он упал или не запускается (KuberPage) ---
  'Лог последнего запуска': 'Log of the last run',
  'успешно': 'succeeded',
  'код {x}': 'code {x}',
  'приостановлен (suspend: true)': 'suspended (suspend: true)',
  'идет Job {job}': 'Job {job} is running',
  'чем кончился, неизвестно': 'outcome unknown',
  'Запусков еще не было': 'No runs yet',
  'строка лога:': 'log line:',
  'событие {r} {a} назад': 'event {r} {a} ago',
  'Лог не сохранился: Job удален по истории': 'The log was not kept: the Job was removed by history limits',
  'упавших Job хранится: {n} (failedJobsHistoryLimit)': 'failed Jobs kept: {n} (failedJobsHistoryLimit)',
  'Job удаляется через {n} с после конца (ttlSecondsAfterFinished)': 'the Job is removed {n} s after it ends (ttlSecondsAfterFinished)',
  'Пода этого Job в отчете нет, лог не найти': 'The pod of this Job is not in the report, the log cannot be found',
  // --- окно хранения данных разделами (RetentionModal) ---
  'История проверок':
    'Check history',
  'CPU, память, сеть и диски с агентов, связь между серверами, ошибки 5xx, OOM. Старше срока удаляется само.':
    'CPU, memory, network and disks from agents, links between servers, 5xx errors, OOM. Anything older than the term is deleted automatically.',
  'Время ответа и статусы мониторов, проверки через локации, адреса сайтов, закрытые инциденты. Старше срока удаляется само.':
    'Monitor response times and statuses, checks through locations, site addresses, closed incidents. Anything older than the term is deleted automatically.',
  'Метрики с агентов':
    'Agent metrics',
  'Связь между серверами':
    'Links between servers',
  'Ошибки 5xx по сайтам':
    '5xx errors by site',
  'OOM':
    'OOM',
  'Проверки мониторов':
    'Monitor checks',
  'Проверки через локации':
    'Checks through locations',
  'Адреса сайтов':
    'Site addresses',
  'Хронология серверов':
    'Server timeline',
  'Трафик по дням':
    'Daily traffic',
  'Команды Docker, Kubernetes и бэкапа':
    'Docker, Kubernetes and backup commands',
  'Строки ошибок 5xx: адреса клиентов, запросы':
    '5xx error lines: client addresses, requests',
  'Проверки домена при добавлении сайта':
    'Domain checks when adding a site',
  'Ручные проверки':
    'Manual checks',
  '14д':
    '14d',
  'Срок хранения':
    'Retention',
  'Постоянные сроки':
    'Fixed terms',
  'Без настройки':
    'Not configurable',
  'Сколько занимает база панели и из чего она состоит. Больше всего места у двух видов данных, срок которых настраивается.':
    'How much space the panel database takes and what it consists of. Most of it belongs to the two kinds of data whose retention is configurable.',
  'Эти данные чистятся по своим срокам: они маленькие или нужны ровно столько.':
    'This data is cleaned up on its own schedule: it is small or needed exactly that long.',
  'Остальное':
    'Everything else',
  'свой срок, дней':
    'custom term, days',
  'Сейчас {now}, прибавляется около {day} в сутки.':
    'Now {now}, growing by about {day} a day.',
  'Самая старая запись - {d}.':
    'The oldest record is from {d}.',
  'Сейчас {now} за {d} дн. При {w} дн. будет около':
    'Now {now} for {d} d. With {w} d it will be about',
  'база дорастет за {n} дн.':
    'the database gets there in {n} d',
  'Сейчас {now} за {d} дн. При {w} дн. данных станет около':
    'Now {now} for {d} d. With {w} d the data will shrink to about',
  'Лишнее удалится при ближайшей чистке, а место останется за базой под новые записи. Графики за период длиннее срока будут пустыми.':
    'The excess is deleted at the next cleanup, and the database keeps the space for new records. Charts for periods longer than the term will be empty.',
  'Из чего состоит':
    'What it consists of',
  '{n} записей':
    '{n} records',
  'База {n}':
    'Database {n}',
  'размеры таблиц примерные':
    'table sizes are estimates',
  'данных будет около':
    'data will be about',
  'После сохранения':
    'After saving',
  '{now}, +{day} в сутки':
    '{now}, +{day} a day',
  'Что':
    'What',
  'Срок':
    'Term',
  'Занимает':
    'Takes',
  'в ошибках 5xx':
    'in 5xx errors',
  'Настройки, серверы, мониторы, журнал действий':
    'Settings, servers, monitors, audit log',
  'пока есть':
    'while it exists',
  'данных будет около {n}':
    'data will be about {n}',
  'Старое удаляется {every}':
    'Old data is deleted {every}',
  // --- окно Ansible разделами (AnsibleModal) ---
  '{h}: поставить {b}': '{h}: install {b}',
  'Helper-скрипты': 'Helper scripts',
  'Старый прокси': 'Old proxy',
  'Старый restic': 'Old restic',
  'Ansible не видит ноды этой панели: доступ выключен.':
    'Ansible does not see the nodes of this panel: access is off.',
  'Ansible спрашивал':
    'Ansible asked',
  'ansible спрашивал {a}':
    'ansible asked {a}',
  'restic самого сервера бэкапов старше целевой версии. Обычно его обновляет установка helper-скрипта, если скачать не вышло - одной командой.':
    'restic on the backup server itself is older than the target version. Installing the helper script usually updates it; if the download failed, one command does it.',
  '{a} назад':
    '{a} ago',
  'Адрес':
    'Address',
  'В группах проблем':
    'In problem groups',
  'В группе ни одной ноды.':
    'No nodes in the group.',
  'Внешний прокси (caddy, traefik) держит docker-сокет: взлом прокси - root на ноде. Плейбук ставит прокси docker-api только на чтение и переключает на него.':
    'The edge proxy (caddy, traefik) holds the docker socket: breaking into the proxy means root on the node. The playbook installs a read-only docker-api proxy and switches to it.',
  'Все ноды':
    'All nodes',
  'Все ноды панели, от которых был хоть один отчет агента. Ansible находит их в своем инвентаре по имени, hostname или адресу.':
    'All panel nodes that sent at least one agent report. Ansible finds them in its inventory by name, hostname or address.',
  'Доступ ansible выключен: ansible этой панели не видит, группы в инвентарь не попадут.':
    'Ansible access is off: ansible does not see this panel, and the groups will not get into the inventory.',
  'Не на связи, в группу не попадут, пока не вернутся: {list}.':
    'Offline, they will not get into the group until they are back: {list}.',
  'Не на связи, обновятся, когда вернутся: {list}.':
    'Offline, they will be updated when they are back: {list}.',
  'Нод в инвентаре':
    'Nodes in the inventory',
  'Ноды в группе: {n}':
    'Nodes in the group: {n}',
  'Ноды на связи, где устарел helper или не включен сторож агента. Плейбук ставит только устаревшее, остальное пропускает.':
    'Online nodes with an outdated helper or without the agent watchdog. The playbook installs only what is outdated and skips the rest.',
  'Обновит плейбук':
    'Playbook will update',
  'Образ внешнего прокси старше года. caddy обновляет тот же caddy_harden.yml (роль ставит закрепленную версию), traefik - руками: между версиями у него меняются настройки.':
    'The edge proxy image is older than a year. caddy is updated by the same caddy_harden.yml (the role installs a pinned version), traefik by hand: its settings change between versions.',
  'Плагин инвентаря (inventory_plugins/kervax_panels.py) спрашивает у каждой панели ее ноды по этому токену и собирает группы kervax_*. Тогда одна команда обновляет helper-скрипты на всех панелях сразу.':
    'The inventory plugin (inventory_plugins/kervax_panels.py) asks every panel for its nodes with this token and builds the kervax_* groups. Then one command updates helper scripts on all panels at once.',
  'Плейбукам нечего делать: helper-скрипты везде свежие, проблем нет.':
    'Nothing for the playbooks to do: helper scripts are up to date everywhere, no problems.',
  'Прокси с docker-сокетом':
    'Proxy with the docker socket',
  'Старый restic на сервере бэкапов':
    'Old restic on the backup server',
  'Токен':
    'Token',
  'Токен выпущен':
    'Token issued',
  'Токен открывает только список нод: имена, адреса и что на них обновить. Без секретов и без управления. Если плейбук запускали позже, чем здесь написано, у этой панели в kervax_panels.yml старый токен или неверный адрес.':
    'The token opens only the node list: names, addresses and what to update on them. No secrets and no control. If the playbook ran later than shown here, kervax_panels.yml has an old token or a wrong address for this panel.',
  'Что ansible получит от этой панели: группы нод для плейбуков. Helper-скрипты на нодах ставит человек плейбуком, панель сама root-скрипты не запускает.':
    'What ansible gets from this panel: node groups for playbooks. A person installs helper scripts on nodes with a playbook; the panel never runs root scripts itself.',
  'Что запустить':
    'What to run',
  'Что обновит плейбук':
    'What the playbook will update',
  'Что обновится':
    'What gets updated',
  'Что там':
    'What is there',
  'группа kervax':
    'group kervax',
  'доступ выключен':
    'access is off',
  'еще ни разу':
    'never yet',
  'нод: {n}':
    'nodes: {n}',
  'прокси и restic':
    'proxy and restic',
  'сборка {d}':
    'built {d}',
  'сторож агента не включен':
    'agent watchdog is off',
  'токен выпущен, ansible еще не спрашивал':
    'token issued, ansible has not asked yet',
  'Группы инвентаря':
    'Inventory groups',
  'Нод':
    'Nodes',
  // --- окно Бэкап разделами (BackupModal) ---
  'новых бэкапов не будет': 'no new backups',
  'Автобэкап':
    'Auto backup',
  'Автобэкап {every}, хранить файлов: {n}':
    'Auto backup {every}, files to keep: {n}',
  'Автобэкап выключен: панель сама бэкап не делает, есть только то, что скачали руками.':
    'Auto backup is off: the panel makes no backups itself, there is only what was downloaded by hand.',
  'Автобэкап выключен: файлы на диске останутся, новых не будет.':
    'Auto backup is off: the files on disk stay, no new ones will appear.',
  'Восстановить':
    'Restore',
  'Всего {n}. Хранятся последние {k}, лишние удаляются при следующем бэкапе.':
    'Total {n}. The last {k} are kept, extra ones are deleted on the next backup.',
  'Выбрать файл…':
    'Choose a file…',
  'Как часто':
    'How often',
  'Когда':
    'When',
  'Мониторы, серверы, локации и настройки панели заменятся тем, что в файле. Пользователи остаются как есть. Файл старой версии без каких-то таблиц их не трогает.':
    'Monitors, servers, locations and panel settings are replaced with what is in the file. Users stay as they are. A file from an older version without some tables leaves those tables alone.',
  'Не входит: метрики и история (графики, алерты, хронология), журнал действий и пользователи - их роли и доступы после восстановления заводятся заново.':
    'Not included: metrics and history (charts, alerts, timeline), the action log and users - their roles and access are set up again after a restore.',
  'Последний автобэкап - {when}.':
    'Last auto backup: {when}.',
  'Размер':
    'Size',
  'Свежий бэкап прямо сейчас - файлом на этот компьютер. На диск панели он не пишется.':
    'A fresh backup right now, as a file on this computer. It is not written to the panel disk.',
  'Скачать и восстановить':
    'Download and restore',
  'Сколько файлов хранить':
    'How many files to keep',
  'Следующий - {when}.':
    'Next: {when}.',
  'Файлов {n} - это бэкапы за {d} дн., на диске около {size}.':
    '{n} files cover {d} days of backups, about {size} on disk.',
  'Файлов на диске':
    'Files on disk',
  'Файлы':
    'Files',
  'Что входит в бэкап':
    'What the backup includes',
  'автобэкап выключен':
    'auto backup is off',
  'бэкапов еще нет':
    'no backups yet',
  'в ближайшие минуты':
    'in the next few minutes',
  'еще ни одного файла':
    'no files yet',
  'последний {a}':
    'last {a}',
  'свое число':
    'custom number',
  'свой интервал, часов':
    'custom interval, hours',
  'следующий {when}':
    'next {when}',
  'хранить файлов: {n}':
    'files to keep: {n}',
  'через {n} мин':
    'in {n} min',
  'Инциденты мониторов':
    'Monitor incidents',
  'Скрытые домены':
    'Hidden domains',
  'Сейф бэкапов':
    'Backup vault',
  'Настройки панели':
    'Panel settings',
  'зашифрован паролем сейфа':
    'encrypted with the vault password',
  'алерты, Telegram, группы, сроки':
    'alerts, Telegram, groups, retention',
  '12 ч':
    '12 h',
  '2 дня':
    '2 days',
  'На сервере':
    'On the server',
  'Вручную':
    'By hand',
  'Бэкап панели - ее настройки одним файлом: мониторы, серверы, локации, алерты. Из него панель восстанавливается целиком или переезжает на другой сервер.':
    'The panel backup is its configuration in one file: monitors, servers, locations, alerts. The panel is restored from it as a whole or moved to another server.',
  'Панель сама сохраняет бэкап на свой диск по расписанию и хранит последние файлы, старые удаляет.':
    'The panel saves a backup to its own disk on a schedule, keeps the latest files and deletes old ones.',
  'Бэкапы на диске панели, новые сверху. Любой можно скачать или восстановить из него панель.':
    'Backups on the panel disk, newest first. Any of them can be downloaded or used to restore the panel.',
  'Свежий бэкап файлом на этот компьютер или восстановление из такого файла, например при переезде панели на другой сервер.':
    'A fresh backup as a file on this computer, or a restore from such a file, for example when moving the panel to another server.',
  // --- окно Пользователи разделами (UsersModal) ---
  'Админов: {n}':
    'Admins: {n}',
  'Админу область не режется: он управляет учетками и может снять ее себе сам.':
    'An admin has no scope limits: they manage accounts and could remove any limit themselves.',
  'Алерты в Telegram':
    'Telegram alerts',
  'В журнале записей нет.':
    'No log entries.',
  'Входы':
    'Logins',
  'Действий за 30 дней':
    'Actions in 30 days',
  'Доступ сохранен.':
    'Access saved.',
  'Журнал действий':
    'Action log',
  'Записей нет.':
    'No entries.',
  'Кто':
    'Who',
  'Кто и что менял в панели, входы и неудачные попытки. Показаны последние {n} записей.':
    'Who changed what in the panel, logins and failed attempts. The last {n} entries are shown.',
  'Может все, включая учетки и настройки панели.':
    'Can do everything, including accounts and panel settings.',
  'Неудачных входов за сутки':
    'Failed logins in 24 h',
  'Новая учетка':
    'New account',
  'Новый пароль (мин. {n})':
    'New password (min. {n})',
  'Объект':
    'Object',
  'Пароль обновлен, прежние входы этой учетки сброшены.':
    'Password updated, existing sessions of this account are signed out.',
  'Показаны первые 200 из {n}.':
    'The first 200 of {n} are shown.',
  'Последние действия':
    'Recent actions',
  'Последний вход':
    'Last login',
  'Правит в разрешенных разделах, учетки и настройки панели ему не видны.':
    'Edits in the allowed sections; accounts and panel settings are hidden.',
  'С 2FA':
    'With 2FA',
  'Создана':
    'Created',
  'Только логин, пароль и роль. Какие разделы и группы видны - в карточке учетки после создания.':
    'Only login, password and role. Which sections and groups are visible is set in the account card after it is created.',
  'Только смотрит, ничего не меняет.':
    'Only looks, changes nothing.',
  'Удаление':
    'Deletion',
  'Удалить учетку':
    'Delete account',
  'Учетка':
    'Account',
  'Учетка создана.':
    'Account created.',
  'Учетка создана. Какие разделы и группы ей видны - в доступе ниже.':
    'Account created. Which sections and groups it sees is set in the access block below.',
  'Учетки':
    'Accounts',
  'Учеток':
    'Accounts',
  'админ':
    'admin',
  'блокировка входа':
    'login lockout',
  'весь журнал учетки':
    'full log of the account',
  'включает сам пользователь':
    'each user turns it on',
  'включена':
    'on',
  'включил 2FA':
    'turned on 2FA',
  'восстановил панель из бэкапа':
    'restored the panel from a backup',
  'все разделы и группы':
    'all sections and groups',
  'вход':
    'login',
  'вход заблокирован':
    'login blocked',
  'вы: {u}':
    'you: {u}',
  'выключил 2FA':
    'turned off 2FA',
  'выпустил токен ansible':
    'issued the ansible token',
  'добавил локацию':
    'added a location',
  'добавил монитор':
    'added a monitor',
  'добавил окно обслуживания':
    'added a maintenance window',
  'добавил связь между серверами':
    'added a link between servers',
  'еще не входил':
    'has not logged in yet',
  'заглушил алерт сервера':
    'snoozed a server alert',
  'заметка к монитору':
    'monitor note',
  'заметка к серверу':
    'server note',
  'изменил автобэкап':
    'changed auto backup',
  'изменил алерты':
    'changed alerts',
  'изменил доступ учетки':
    'changed account access',
  'изменил локацию':
    'changed a location',
  'изменил монитор':
    'changed a monitor',
  'изменил окно обслуживания':
    'changed a maintenance window',
  'изменил сервер':
    'changed a server',
  'изменил сроки хранения':
    'changed retention',
  'импорт мониторов':
    'imported monitors',
  'не включена, включает сам пользователь в своем меню':
    'off, the user turns it on in their own menu',
  'не входил':
    'never',
  'не привязаны':
    'not linked',
  'неверный код 2FA':
    'wrong 2FA code',
  'неверный пароль':
    'wrong password',
  'отвязал Telegram':
    'unlinked Telegram',
  'отключил доступ ansible':
    'turned off ansible access',
  'открыл сейф бэкапов':
    'opened the backup vault',
  'посмотрел доступы бэкапа':
    'viewed backup credentials',
  'посмотрел, на что ушло место':
    'looked at what takes the space',
  'посмотрел, что можно освободить':
    'previewed what can be freed',
  'изменил свои уведомления в Telegram':
    'changed own Telegram notifications',
  'взял алерт в Telegram':
    'took an alert in Telegram',
  'заглушил алерт в Telegram':
    'snoozed an alert in Telegram',
  'вернул алерты в Telegram':
    'unmuted alerts in Telegram',
  'заглушил один вид алертов сервера':
    'snoozed one kind of server alert',
  'изменил правила алертов серверов':
    'changed server alert rules',
  'включил автоочистку диска':
    'turned on disk auto cleanup',
  'выключил автоочистку диска':
    'turned off disk auto cleanup',
  'известная проблема прошла':
    'known issue cleared',
  'истек срок известной проблемы':
    'known issue expired',
  'обновление агента':
    'agent update',
  'раскатка агента':
    'agent rollout',
  'перезапустил контейнер':
    'restarted a container',
  'остановил контейнер':
    'stopped a container',
  'запустил контейнер':
    'started a container',
  'посмотрел логи контейнера':
    'viewed container logs',
  'перезапустил поды':
    'restarted pods',
  'удалил под':
    'deleted a pod',
  'посмотрел логи пода':
    'viewed pod logs',
  'отметил, что агент на ноде не нужен':
    'marked a node as not needing an agent',
  'вернул ноду в проверку агентов':
    'put a node back into the agent check',
  'отметил, что за сроком в кластере не следим':
    'marked a cluster expiry as not watched',
  'вернул срок в кластере под присмотр':
    'put a cluster expiry back under watch',
  'отложил срок домена или сертификата':
    'postponed a domain or certificate expiry',
  'вернул срок домена или сертификата под присмотр':
    'put a domain or certificate expiry back under watch',
  'заглушил монитор':
    'snoozed a monitor',
  'изменил мониторы пачкой':
    'changed monitors in bulk',
  'добавил найденные сайты':
    'added discovered sites',
  'поменял порядок мониторов':
    'reordered monitors',
  'изменил правила алертов сайтов':
    'changed site alert rules',
  'выпустил новый адрес push':
    'issued a new push URL',
  'принял новые DNS-записи':
    'accepted new DNS records',
  'скрыл домены':
    'hid domains',
  'вернул домены':
    'brought domains back',
  'проверил чат алертов':
    'tested an alert chat',
  'сменил логотип':
    'changed the logo',
  'вернул стандартный логотип':
    'restored the default logo',
  'настроил бэкап сервера':
    'set up server backup',
  'изменил пути бэкапа':
    'changed backup paths',
  'изменил расписание бэкапа':
    'changed the backup schedule',
  'настроил дампы баз':
    'set up database dumps',
  'убрал дампы баз':
    'removed database dumps',
  'обновил restic':
    'updated restic',
  'обновил образ rest-server':
    'updated the rest-server image',
  'включил синхронизацию часов':
    'turned on clock sync',
  'починил юнит':
    'fixed a unit',
  'посмотрел журнал юнитов':
    'viewed the unit journal',
  'убрал из проверки бэкапа':
    'excluded from the backup check',
  'вернул в проверку бэкапа':
    'put back into the backup check',
  'заглушил репозиторий бэкапа':
    'muted a backup repository',
  'вернул алерты репозитория бэкапа':
    'unmuted a backup repository',
  'убрал репозиторий бэкапа в архив':
    'archived a backup repository',
  'вернул репозиторий бэкапа из архива':
    'unarchived a backup repository',
  'развернул сервер бэкапов':
    'deployed a backup server',
  'включил TLS на сервере бэкапов':
    'enabled TLS on a backup server',
  'перестал следить за своим бэкапом':
    'stopped watching a custom backup',
  'вернул слежку за своим бэкапом':
    'resumed watching a custom backup',
  'настроил сейф бэкапов':
    'set up the backup vault',
  'положил доступы в сейф':
    'stored credentials in the vault',
  'удалил запись из сейфа':
    'deleted a vault entry',
  'стер сейф бэкапов':
    'wiped the backup vault',
  'бэкап':
    'backup',
  'сейф бэкапов':
    'backup vault',
  'эксп.':
    'expl.',
  'правка':
    'edit',
  'привязал Telegram':
    'linked Telegram',
  'привязаны':
    'linked',
  'просмотр':
    'view',
  'с адресов: {n}':
    'from addresses: {n}',
  'сбросил пароль учетки':
    'reset an account password',
  'сменил свой пароль':
    'changed own password',
  'создал бэкап панели':
    'created a panel backup',
  'создал учетку':
    'created an account',
  'удалил локацию':
    'deleted a location',
  'удалил монитор':
    'deleted a monitor',
  'удалил мониторы':
    'deleted monitors',
  'удалил окно обслуживания':
    'deleted a maintenance window',
  'удалил связь между серверами':
    'deleted a link between servers',
  'удалил сервер':
    'deleted a server',
  'удалил учетку':
    'deleted an account',
  'учеток: {n}':
    'accounts: {n}',
  // --- окно Окна обслуживания разделами (MaintModal) ---
  'Окно идет: алерты по выбранным серверам и мониторам молчат.':
    'The window is in progress: alerts for the selected servers and monitors are silent.',
  'Окно прошло. Прошедшие разовые окна удаляются через месяц.':
    'The window is over. Past one-off windows are deleted after a month.',
  'Пока окно идет, алерты по выбранным серверам и мониторам молчат.':
    'While the window is in progress, alerts for the selected servers and monitors are silent.',
  'скоро': 'soon',
  '2 ч':
    '2 h',
  '30 мин':
    '30 min',
  '4 ч':
    '4 h',
  '8 ч':
    '8 h',
  '{name} - не на связи':
    '{name} - offline',
  '{name} - недоступен':
    '{name} - down',
  '{name} - тормозит':
    '{name} - slow',
  'Ближайшее':
    'Next',
  'Завел':
    'Created by',
  'Закончить сейчас':
    'End now',
  'Затронет':
    'Affects',
  'Идет сейчас':
    'In progress',
  'Настройки окна':
    'Window settings',
  'Начать сейчас':
    'Start now',
  'Окна':
    'Windows',
  'Окно':
    'Window',
  'Окно закончено, алерты снова идут.':
    'The window has ended, alerts are back on.',
  'Окно продлено на час.':
    'The window is extended by an hour.',
  'Окно создано. Сверку после окна и заметку можно поменять ниже.':
    'Window created. The after-window check and the note can be changed below.',
  'Окон: {n}':
    'Windows: {n}',
  'Повторяющихся':
    'Recurring',
  'Продлить на час':
    'Extend by an hour',
  'Прошедшие':
    'Past',
  'Сверка после окна':
    'After-window check',
  'Сейчас все в норме.':
    'Everything is fine right now.',
  'Сейчас не в норме: {list}.':
    'Not fine right now: {list}.',
  'Удалить окно':
    'Delete window',
  'Что делаем, когда и что затронет. Сверка после окна включена, ее и заметку можно поменять в разделе окна после создания.':
    'What is being done, when, and what it affects. The after-window check is on; it and the note can be changed in the window section after creation.',
  'Что затронет и как оно сейчас':
    'What it affects and how it is now',
  'Что затронуто и как оно сейчас':
    'What is affected and how it is now',
  'ближайшее {when}':
    'next {when}',
  'да, через 5 минут после конца':
    'yes, 5 minutes after the end',
  'идет: {n}':
    'in progress: {n}',
  'каждый день или по дням недели':
    'daily or on weekdays',
  'не запланировано':
    'nothing planned',
  'ничего':
    'nothing',
  'окон нет':
    'no windows',
  'прошедших: {n}':
    'past: {n}',
  'тормозит':
    'slow',
  // --- окно Отчет разделами (ReportModal, reportHtml) ---
  '{n} раз':
    '{n} times',
  '{n} раза':
    '{n} times',
  'Что было':
    'What happened',
  'И еще {n} реже - в истории алертов.':
    '{n} more, less often: see the alert history.',
  'Что именно срабатывало и где. Если одно и то же приходит каждый день - стоит починить, поправить порог или заглушить.':
    'What exactly fired and where. If the same thing fires every day, fix it, adjust the threshold or mute it.',
  '{m} мин {s} с':
    '{m} min {s} s',
  '{s} с':
    '{s} s',
  'Алертов за период не было.':
    'No alerts in the period.',
  'Алерты по дням':
    'Alerts by day',
  'Бэкапы, по которым приходили алерты: не сделался, устарел, не сходится ротация.':
    'Backups that triggered alerts: failed, stale, retention does not add up.',
  'За {n} дн.: {p}':
    '{n} days: {p}',
  'За период ничего не ломалось и не шумело.':
    'Nothing broke or made noise in the period.',
  'Мониторы, которые лежали или отвечали не всегда. Аптайм - по проверкам, простой - по инцидентам, пока монитор лежал.':
    'Monitors that were down or did not always respond. Uptime comes from checks, downtime from incidents while the monitor was down.',
  'Одним HTML-файлом: открывается в любом браузере, можно переслать или распечатать':
    'A single HTML file: opens in any browser, can be forwarded or printed',
  'Окон обслуживания за период не было.':
    'No maintenance windows in the period.',
  'Отчет {brand}':
    '{brand} report',
  'Панель не принимала отчеты {d}, раз: {n}. В простой серверов это не входит.':
    'The panel did not accept reports for {d}, times: {n}. This is not counted as server downtime.',
  'Плановые работы за период: пока окно шло, алерты по его серверам и мониторам молчали.':
    'Planned work in the period: while a window was in progress, alerts for its servers and monitors were silent.',
  'Простой по дням':
    'Downtime by day',
  'Работы':
    'Work',
  'Серверы, которые пропадали или по которым приходили алерты. Простой - по пропускам в отчетах агента: заглушенный алерт его не отменяет.':
    'Servers that went missing or triggered alerts. Downtime comes from gaps in agent reports: a muted alert does not cancel it.',
  'Скачать отчет':
    'Download report',
  'Сформирован {d}':
    'Generated {d}',
  'Хуже всего':
    'Worst',
  'Что ломалось':
    'What broke',
  'Что ломалось за период, сколько лежало и что шумело. Это не алерт: смотрят здесь, раз в неделю.':
    'What broke in the period, how long it was down and what made noise. This is not an alert: it is read here, once a week.',
  'алертов':
    'alerts',
  'за период пусто':
    'nothing in the period',
  'лежал {d}':
    'down {d}',
  'недоступен {d}':
    'unavailable {d}',
  'простой сайтов':
    'site downtime',
  'простой серверов':
    'server downtime',
  'сайт':
    'site',
  'Панель выпустит новый токен: прежняя команда перестанет работать.':
    'The panel issues a new token: the previous command stops working.',
  'Команда установки':
    'Install command',
  'адрес отвечает отказом ({where}), похоже на перезагрузку':
    'the address refuses ({where}), looks like a reboot',
  'предупреждения: {list}':
    'warnings: {list}',
  'проблемы: {list}':
    'issues: {list}',
  'с панели сервер тоже не отвечает':
    'the server does not answer the panel either',
  'с панели сервер тоже не отвечает, хотя на связи отвечал ({where})':
    'the server does not answer the panel either, though it did while online ({where})',
  'с панели не проверить: порты закрыты для нее':
    'cannot be checked from the panel: its ports are closed to it',
  'выключен штатно':
    'shut down cleanly',
  'ушел на перезагрузку':
    'went down for a reboot',
  'система остановлена штатно':
    'system stopped cleanly',
  'ждем возврата':
    'waiting for it to come back',
  'не вернулся':
    'did not come back',
  'агента остановили штатно':
    'the agent was stopped cleanly',
  '90% примерно к {d}':
    '90% around {d}',
  '100% примерно к {d}':
    '100% around {d}',
  '{r}% в сутки':
    '{r}% a day',
  'по средней за всю жизнь диска':
    'from the lifetime average',
  'по замеру':
    'measured',
  '{name}: диски {disks} дойдут до 90% износа примерно к {d} - запланируйте замену':
    '{name}: disks {disks} reach 90% wear around {d} - plan a replacement',
  'свежих данных нет':
    'no fresh data',
  'сервер отвечает ({where}), молчит агент':
    'the server answers ({where}), the agent is silent',
  'Подключение сервера':
    'Server onboarding',
  'закроется само {when}':
    'closes by itself on {when}',
  'Пока идет подключение, в Telegram уходят только недоступность и критичный диск. Остальное видно здесь и в "Что сломано".':
    'While onboarding is open, only unavailability and a critical disk go to Telegram. Everything else is shown here and in "What\'s broken".',
  'агент еще не отчитался: поставьте его на сервере':
    'the agent has not reported yet: install it on the server',
  'агент на связи':
    'the agent is reporting',
  'агент отчитывался, но сейчас не на связи':
    'the agent reported before but is not reporting now',
  'доступ к Docker':
    'Docker access',
  'доступ к Kubernetes':
    'Kubernetes access',
  'установщик ставит helper\'ы: первые 20 минут после первого отчета':
    'the installer is setting up helpers: the first 20 minutes after the first report',
  'не хватает: {list} - команда ниже':
    'missing: {list}, the command is below',
  'не хватает: {list}':
    'missing: {list}',
  'helper\'ы на месте':
    'helpers are in place',
  'это сервер бэкапов':
    'this is a backup server',
  'свой бэкап, панель за ним следит':
    'own backup, the panel watches it',
  'бэкап не требуется (отмечено)':
    'backup not required (marked)',
  'бэкап не настроен: настройте или отметьте, что он не нужен':
    'no backup: set it up or mark that it is not needed',
  'основной чат':
    'main chat',
  'чат {name}':
    'chat {name}',
  'чат группы':
    'group chat',
  'алерты в Telegram никуда не уйдут: не настроен чат':
    'alerts will not go anywhere in Telegram: no chat is set up',
  'алерты пойдут: {where}':
    'alerts go to: {where}',
  'сервер без группы':
    'server without a group',
  'Нашлось при подключении':
    'Found at onboarding',
  'Когда закроете подключение, это станет известными проблемами: алерт придет, только если станет хуже или сломается новое.':
    'When you close onboarding, these become known issues: an alert comes only if it gets worse or something new breaks.',
  'Закрыть подключение':
    'Close onboarding',
  'подключается':
    'onboarding',
  'уже было при подключении сервера, отмечено как известное':
    'was already there when the server was onboarded, marked as known',
  'Нажмите: открыть список в карточке, там же снять отметку':
    'Click to open the list in the server card, you can unmark it there',
  'Нажмите: открыть монитор, там же снять отметку':
    'Click to open the monitor, you can unmark it there',
  'сбой монитора':
    'monitor failure',
  'ретранслятор':
    'relay',
  'Ретранслятор Kervax: через него ходят агенты: {list}':
    'Kervax relay, agents going through it: {list}',
  'Ретранслятор Kervax, сейчас никто не ходит':
    'Kervax relay, no agents use it right now',
  'Нажмите: настройки и состояние ретранслятора':
    'Click to open relay settings and status',
  'Агент ходит в панель через ретранслятор {name}':
    'The agent reaches the panel through relay {name}',
  'через ретранслятор {name}':
    'via relay {name}',
  'Нода Kubernetes':
    'Kubernetes node',
  'Сервисы LLM: {list}':
    'LLM services: {list}',
  'Алерт по ним молчит, пока не станет хуже. Снимите отметку, если надо снова получать алерты.':
    'Alerts stay silent until it gets worse. Unmark to get alerts again.',
  '{name}: идет подключение - посмотрите, что нашлось, и закройте его в карточке':
    '{name}: onboarding is open, check what was found and close it in the server card',
  'закрыл подключение сервера':
    'closed server onboarding',
  // --- плитка и панель "Заглушено" (MutesPanel.tsx) ---
  'Заглушено': 'Muted',
  'бессрочно {n}': 'no expiry {n}',
  'истекут за сутки {n}': 'expire within a day {n}',
  'Бессрочно {n}': 'No expiry {n}',
  'Со сроком {n}': 'With expiry {n}',
  'Сервер или что заглушено': 'Server or what is muted',
  'Снять заглушку': 'Unmute',
  'заглушка снята': 'unmuted',
  'открыть карточку': 'open card',
  'снять может оператор или админ': 'an operator or admin can unmute',
  'пунктов покрытия: {n}': 'coverage items: {n}',
  // причина отказа дампа (helperText.ts, backup-setup 0.38)
  'helper на сервере не сообщил причину. Обновите helper backup-setup и нажмите еще раз: {cmd}':
    'the helper on the server gave no reason. Update the backup-setup helper and try again: {cmd}',
  'helper backup-setup{ver} не сообщил причину. Запустите ту же проверку на сервере, причина будет в выводе: {cmd}':
    'the backup-setup{ver} helper gave no reason. Run the same check on the server, the reason will be in its output: {cmd}',
  'helper backup-setup{ver} на сервере не передает ошибку проверки, так было до {since}.':
    'the backup-setup{ver} helper on the server does not pass on the error of the check, helpers before {since} lose it.',
  'Обновите helper и нажмите еще раз: {cmd}':
    'Update the helper and try again: {cmd}',
  'Частая причина у MySQL 8 на хосте: root без пароля не пускают, а новый helper входит через /etc/mysql/debian.cnf или /root/.my.cnf.':
    'A common cause with MySQL 8 on the host: root is not let in without a password, while the new helper logs in through /etc/mysql/debian.cnf or /root/.my.cnf.',
  'MySQL не пустил helper ({err}). Дампу нужен вход: положите в /root/.my.cnf пароль root или пользователя с правами SELECT, SHOW VIEW, TRIGGER, LOCK TABLES, EVENT, PROCESS и нажмите еще раз: {cmd}':
    'MySQL did not let the helper in ({err}). The dump needs a login: put the password of root, or of a user with SELECT, SHOW VIEW, TRIGGER, LOCK TABLES, EVENT, PROCESS, into /root/.my.cnf and try again: {cmd}',
  'root через сокет':
    'root over the socket',
  'На сервере нет mysqldump и mariadb-dump: не установлен клиент MySQL. Поставьте его (для MariaDB - mariadb-client) и нажмите еще раз: {cmd}':
    'There is no mysqldump or mariadb-dump on the server: the MySQL client is not installed. Install it (mariadb-client for MariaDB) and try again: {cmd}',
  'Проверка {engine} завершилась с кодом {rc} и ничего не написала. Запустите ее на сервере, причина будет в выводе: {cmd}':
    'The {engine} check exited with code {rc} and printed nothing. Run it on the server, the reason will be in its output: {cmd}',
  'Docker на сервере не знает контейнер {name}: его удалили или переименовали.':
    'Docker on the server does not know the container {name}: it was removed or renamed.',
  'На сервере нет neo4j-admin.':
    'There is no neo4j-admin on the server.',
  'grafana.db не найден ни на хосте, ни в томах.':
    'grafana.db was not found on the host or in volumes.',
  'На сервере нет ни sqlite3, ни python3 с модулем sqlite3. Поставьте sqlite3 и нажмите еще раз: {cmd}':
    'There is neither sqlite3 nor python3 with the sqlite3 module on the server. Install sqlite3 and try again: {cmd}',
  'Сервер ответил отказом без текста. Обновите helper backup-setup и нажмите еще раз: {cmd}':
    'The server refused with no text. Update the backup-setup helper and try again: {cmd}',
  'Агент на сервере не ответил вовремя: команда не дошла до сервера или helper бэкапа завис. Проверьте на сервере: {cmd}':
    'The agent on the server did not answer in time: the command did not reach the server or the backup helper hung. Check on the server: {cmd}',
  'Helper бэкапа на сервере не взял команду. Проверьте на сервере: {cmd}. Если helper не стоит, поставьте: {setup}':
    'The backup helper on the server did not pick up the command. Check on the server: {cmd}. If the helper is not installed, install it: {setup}',
  'На сервере нет helper backup-setup, через него панель включает дампы. Поставьте: {cmd}':
    'The backup-setup helper is not on the server, the panel enables dumps through it. Install it: {cmd}',
  // --- журнал ошибок панели (ProblemModal, PanelErrorsModal, ErrorBoundary) ---
  'Панель сама записывает, на что люди натыкаются: падения бэкенда, неудачные действия, команды агенту, которые не прошли, и ошибки интерфейса. Одинаковые складываются в одну строку со счетчиком. Хранится {d} дней, не больше {n} записей. В Telegram это не уходит.':
    'The panel records what people run into on its own: backend crashes, failed actions, agent commands that did not go through and interface errors. Identical ones fold into one row with a counter. Kept for {d} days, at most {n} records. None of this goes to Telegram.',
  'Сообщений людей':
    'Reports from people',
  'Сообщения людей':
    'Reports from people',
  'неразобранных':
    'not reviewed',
  'Неразобрано':
    'Not reviewed',
  'записей всех видов':
    'records of all kinds',
  'ошибок появилось или повторилось':
    'errors appeared or repeated',
  'Разобрано':
    'Reviewed',
  'за время хранения':
    'over the retention period',
  'Свежее':
    'Latest',
  'Неразобранных ошибок нет.':
    'No errors to review.',
  'Отмеченное как разобранное. Если ошибка повторится, она вернется в неразобранное с пометкой.':
    'Marked as reviewed. If an error happens again, it comes back to the review list with a mark.',
  'Пусто.':
    'Empty.',
  'Показаны последние {n}: сузьте период или поиск.':
    'Showing the latest {n}: narrow the period or the search.',
  'Ошибки панели':
    'Panel errors',
  'Поиск: текст, кто, сервер, адрес':
    'Search: text, who, server, address',
  'Все показанные разобраны':
    'Mark all shown as reviewed',
  'сколько раз':
    'how many times',
  'снова после разбора':
    'again after review',
  'кто':
    'who',
  'где':
    'where',
  'впервые':
    'first',
  'разобрал {who}':
    'reviewed by {who}',
  'Ошибки этого человека за 15 минут до сообщения':
    'This person\'s errors in the 15 minutes before the report',
  'Неудачные запросы вкладки':
    'Failed requests from the tab',
  'Параметры команды':
    'Command parameters',
  'Скрыть подробности':
    'Hide details',
  'Подробности':
    'Details',
  'Вернуть в неразобранное':
    'Back to review',
  'Бэкенд':
    'Backend',
  'Действия':
    'Actions',
  'Команды агенту':
    'Agent commands',
  'Интерфейс':
    'Interface',
  'сообщение':
    'report',
  'бэкенд':
    'backend',
  'действие':
    'action',
  'команда':
    'command',
  'интерфейс':
    'interface',
  'Что люди написали через кнопку Сообщить о проблеме, с их ошибками за 15 минут до сообщения.':
    'What people wrote with the Report an issue button, with their errors from the 15 minutes before.',
  'Необработанные исключения бэкенда (500): маршрут, кто и короткий traceback кода панели.':
    'Unhandled backend exceptions (500): route, who and a short traceback of the panel code.',
  'Изменяющие запросы, на которые панель ответила отказом, и формы панели, которые не прошли проверку бэкенда (422): поля и причины, без значений. Протухший вход (401) и неверный пароль сюда не пишутся.':
    'Changing requests the panel refused, and panel forms the backend rejected (422): fields and reasons, without values. An expired session (401) and a wrong password are not recorded here.',
  'Команды агенту и helper-скриптам, которые запустил человек и которые не прошли: вывод helper-скрипта и параметры команды.':
    'Agent and helper commands started by a person that failed: the helper output and the command parameters.',
  'Ошибки в браузере: исключения, необработанные промисы и упавшие при отрисовке экраны.':
    'Errors in the browser: exceptions, unhandled promises and screens that crashed while rendering.',
  'Неразобранное':
    'To review',
  'Разобранное':
    'Reviewed',
  'Отправлено, спасибо. Сообщение у админа в окне "Ошибки панели".':
    'Sent, thank you. The admin sees it in the "Panel errors" window.',
  'Приложено ошибок: {n}, неудачных запросов: {m}.':
    'Errors attached: {n}, failed requests: {m}.',
  'Ошибок за последние 15 минут не было: админ увидит только ваш текст и адрес страницы.':
    'There were no errors in the last 15 minutes: the admin sees only your text and the page address.',
  'Пара слов: что делали и что пошло не так. Адрес страницы, раздел, версия панели и ваши ошибки за последние 15 минут приложатся сами, скриншот не нужен.':
    'A few words: what you did and what went wrong. The page address, section, panel version and your errors from the last 15 minutes are attached automatically, no screenshot needed.',
  'например: в карточке бэкапа нажал "включить дамп", а под строкой красная ошибка без причины':
    'for example: in the backup card I clicked "enable dump" and got a red error with no reason',
  'Приложится':
    'Attached',
  'Страница':
    'Page',
  'Раздел':
    'Section',
  'Версия':
    'Version',
  'Неудачные запросы':
    'Failed requests',
  'за 15 минут не было':
    'none in 15 minutes',
  'Ошибки, которые панель записала сама, добавятся при отправке.':
    'Errors the panel recorded on its own are added when you send.',
  'Отправить':
    'Send',
  'Главная':
    'Home',
  'Этот экран сломался':
    'This screen crashed',
  'Ошибка уже записана в журнал ошибок панели. Можно перейти в другой раздел, перезагрузить страницу или добавить пару слов о том, что делали.':
    'The error is already in the panel error log. You can go to another section, reload the page or add a few words about what you were doing.',
  'Перезагрузить страницу':
    'Reload the page',
  'Неразобранных ошибок панели: {n}':
    'Panel errors to review: {n}',
  'Ошибки панели и сообщения о проблемах':
    'Panel errors and issue reports',
  'отметил ошибки панели разобранными':
    'marked panel errors as reviewed',
  'вернул ошибки панели в неразобранные':
    'returned panel errors to review',
  // вкладка "Чистка" сервера бэкапов (BsrvCleanup.tsx)
  'Пробный прогон, ничего не меняет: по каждому репозиторию helper покажет, сколько снапшотов удалит первая чистка (группы по хосту и тегам, как в чистке панели). Проверьте, что уходят только лишние старые снапшоты. Если restic не ответил, пароль в блоке не подходит, и такой репозиторий helper не перенесет.':
    'Dry run, changes nothing: for each repository the helper shows how many snapshots the first cleanup removes (grouped by host and tags, as the panel cleanup does). Check that only extra old snapshots go. If restic did not answer, the password in the block is wrong and the helper will not move that repository.',
  'Перенести: у репозитория появится свой prune-скрипт с той же политикой и паролем из блока, чистка раз в сутки через 12 часов после бэкапа клиента, раз в неделю проверка целостности. Блок в старом скрипте закомментируется, копия файла - /etc/systemd-rest.conf.kervax-bak-<время>. На этом шаге ничего не удаляется.':
    'Move it: the repository gets its own prune script with the same policy and the password from the block, a daily cleanup 12 hours after the client\'s backup and a weekly integrity check. The block in the old script is commented out, a copy of the file is /etc/systemd-rest.conf.kervax-bak-<time>. Nothing is removed at this step.',
  'Проверить: в cron есть время чистки, строк # kervax-moved в старом скрипте стало больше. Через минуту панель покажет у репозитория свою чистку.':
    'Check: cron has the cleanup time and the old script has more # kervax-moved lines. In a minute the panel shows the repository with its own cleanup.',
  'Первая чистка пройдет сама во время из cron. Чтобы не ждать, запустите ее сейчас, но не во время бэкапа клиента: она удалит снапшоты сверх политики, это необратимо. В строке итога - сколько снапшотов было и стало.':
    'The first cleanup runs by itself at the cron time. To not wait, run it now, but not during the client\'s backup: it removes snapshots beyond the policy, and that cannot be undone. The result line shows how many snapshots there were and are.',
  'Пока чистка не прошла: вернуть старый скрипт из копии и убрать перенос. Снапшоты, которые чистка уже удалила, откат не вернет.':
    'Before the cleanup has run: restore the old script from the copy and remove the move. Snapshots the cleanup already removed do not come back.',
  'Пробный прогон, ничего не меняет: по каждому имени helper скажет, что репозитория нет и его блок будет закомментирован.':
    'Dry run, changes nothing: for each name the helper says the repository is gone and its block will be commented out.',
  'Закомментировать эти блоки (приставка # kervax-gone). Копия файла - /etc/systemd-rest.conf.kervax-bak-<время>. Если после правки файл не разбирается (sh -n), helper оставит его как был. Бэкапы и репозитории это не трогает.':
    'Comment out these blocks (prefix # kervax-gone). A copy of the file is /etc/systemd-rest.conf.kervax-bak-<time>. If the edited file does not parse (sh -n), the helper leaves it as it was. Backups and repositories are not touched.',
  'Проверить: файл разбирается (OK), закомментированных блоков не меньше {n}.':
    'Check: the file parses (OK) and at least {n} blocks are commented out.',
  'Вернуть старый скрипт из копии.':
    'Restore the old script from the copy.',
  'Сначала без --apply: покажет, сколько снапшотов группы и за какие даты удалится. Ничего не меняет. Группу, в которую бэкапились последнюю неделю, команда удалять откажется.':
    'First without --apply: shows how many snapshots of the group and from which dates would go. Changes nothing. The command refuses to remove a group that got a backup within the last week.',
  'Удалить снапшоты группы. Это необратимо: восстановиться из них будет нельзя. Место вернет ночная чистка.':
    'Remove the group\'s snapshots. This cannot be undone: there is no restoring from them afterwards. The nightly cleanup gives the space back.',
  'Проверить: команда ответит, что снапшотов этой группы нет (no snapshots of host). Пункт уйдет после ночной чистки, она перепишет список групп.':
    'Check: the command answers that the group has no snapshots (no snapshots of host). The item goes away after the nightly cleanup rewrites the group list.',
  'Если группу надо хранить: кнопка "в архив" выше. На сервере ничего не меняется, пункт уходит.':
    'If the group must be kept: the "archive" button above. Nothing changes on the server, the item goes away.',
  'Сначала без --apply: покажет обе группы. Ничего не меняет.':
    'First without --apply: shows both groups. Changes nothing.',
  'Отдать снапшоты прежнего имени серверу {to}: данные не копируются, меняются только записи снапшотов. Дальше их чистит общая политика, лишние уйдут ночной чисткой, и это уже необратимо.':
    'Hand the old name\'s snapshots over to {to}: no data is copied, only the snapshot records change. From then on the common policy cleans them, the extra ones go with the nightly cleanup, and that cannot be undone.',
  'Что делала чистка в последних прогонах: сколько снапшотов было и стало, ошибки restic.':
    'What the cleanup did in its last runs: snapshots before and after, restic errors.',
  'Что удалит политика сейчас, без удаления и без блокировки репозитория. Если remove 0, а снапшотов больше политики, они в разных группах (хост, теги), и снапшоты старой группы держатся.':
    'What the policy would remove now, without removing and without locking the repository. If it says remove 0 while there are more snapshots than the policy keeps, they are in different groups (host, tags) and the old group\'s snapshots stay.',
  'Запустить чистку сейчас: она удалит снапшоты сверх политики, это необратимо.':
    'Run the cleanup now: it removes snapshots beyond the policy, and that cannot be undone.',
  'Что ответил restic в последнем прогоне чистки.':
    'What restic answered in the last cleanup run.',
  'Пароль для чистки лежит в env на сервере (строка RESTIC_PASSWORD). Правильный - у клиента в /etc/systemd-resta.conf или в панели, в данных восстановления его бэкапа.':
    'The cleanup takes the password from the env file on the server (the RESTIC_PASSWORD line). The right one is on the client in /etc/systemd-resta.conf or in the panel, in the recovery data of its backup.',
  'Политика хранения в скрипте пустая (все keep-* = 0), и чистка пропускается, чтобы не удалить все. Задайте ее в строке KEEP=(...), например --keep-daily 7 --keep-weekly 4 --keep-monthly 6.':
    'The retention policy in the script is empty (every keep-* is 0), so the cleanup is skipped to avoid removing everything. Set it in the KEEP=(...) line, for example --keep-daily 7 --keep-weekly 4 --keep-monthly 6.',
  'Есть ли запись в cron и запускал ли ее cron.':
    'Whether cron has the entry and whether cron ran it.',
  'Запустить чистку и посмотреть итог: в строке done должно быть success=1. Чистка удаляет снапшоты сверх политики, это необратимо.':
    'Run the cleanup and look at the result: the done line should say success=1. The cleanup removes snapshots beyond the policy, and that cannot be undone.',
  'Чистки у репозитория на сервере нет: ни своего prune-скрипта, ни блока в старом скрипте. Ее заводит панель, когда бэкап клиента настраивают через нее. Клиента больше нет или бэкап разовый - уберите репозиторий в архив (раздел "Репозитории").':
    'The repository has no cleanup on the server: neither its own prune script nor a block in the old script. The panel sets it up when the client\'s backup is configured through the panel. If the client is gone or the backup was a one-off, move the repository to the archive (the "Repositories" section).',
  'Нужен helper backupserver-setup {v} или новее: обновите helper\'ы на этом сервере (главная, "Требует действий").':
    'This needs helper backupserver-setup {v} or newer: update the helpers on this server (home page, "Action needed").',
  'старые снапшоты не вычищаются - {list}':
    'old snapshots are not removed - {list}',
  'старые снапшоты не вычищаются, перевести на чистку панели - {list}':
    'old snapshots are not removed, move to the panel cleanup - {list}',
  'старые снапшоты не вычищаются, удалить старые группы или в архив - {list}':
    'old snapshots are not removed, remove old groups or archive them - {list}',
  'старые снапшоты не вычищаются, у репозитория нет чистки - {list}':
    'old snapshots are not removed, the repository has no cleanup - {list}',
  'старые снапшоты не вычищаются, разобрать в Чистке - {list}':
    'old snapshots are not removed, see the Cleanup section - {list}',
  'Старый скрипт чистки не запускается: эти репозитории никто не чистит':
    'The old cleanup script does not run: nobody cleans these repositories',
  'Старый скрипт {script} чистит репозиториев: {n}, в {when} ({where})':
    'The old script {script} cleans {n} repositories at {when} ({where})',
  '{script} не запланирован ни в cron, ни в systemd.':
    '{script} is not scheduled in cron or systemd.',
  'Последний прогон {ago}.':
    'Last run {ago}.',
  'Еще на нем репозиториев в архиве: {n}, их чистить не нужно.':
    'It also has {n} archived repositories, they need no cleanup.',
  'В {script} блоки на удаленные репозитории: {n}':
    '{script} has blocks for repositories that are gone: {n}',
  'как убрать':
    'how to remove',
  'Скрипт запускается каждый день, проверяет каждый такой блок и шлет ошибку, в этом шуме тонут настоящие. Блоки стоит убрать.':
    'The script runs every day, checks each of these blocks and sends an error, and real errors drown in that noise. The blocks should go.',
  'Скрипт сейчас не запускается, и ошибок они не шлют. Если его снова запустят, придет ошибка по каждому, поэтому блоки лучше убрать.':
    'The script does not run now, so they send no errors. If it is started again, an error comes for each of them, so the blocks are better removed.',
  // чистка выключена вручную (helper backupserver-setup 0.34)
  'Перенести: у репозитория появится свой prune-скрипт с той же политикой и паролем из блока. Старый скрипт не запускался, поэтому чистка останется выключенной, как была: строка в /etc/cron.d/kervax-prune-<имя> закомментирована с меткой # kervax-prune-off. Блок в старом скрипте закомментируется, копия файла - /etc/systemd-rest.conf.kervax-bak-<время>. На этом шаге ничего не удаляется.':
    'Move it: the repository gets its own prune script with the same policy and the password from the block. The old script did not run, so the cleanup stays off as it was: the line in /etc/cron.d/kervax-prune-<name> is commented out under # kervax-prune-off. The block in the old script is commented out, a copy of the file is /etc/systemd-rest.conf.kervax-bak-<time>. Nothing is removed at this step.',
  'Включить чистку, если она нужна. Старый скрипт эти репозитории не чистил, и первая чистка удалит все сверх политики. Репозиторий заморожен намеренно - пропустите этот и следующий шаг, чистка останется выключенной.':
    'Turn the cleanup on if it is needed. The old script did not clean these repositories, and the first cleanup removes everything beyond the policy. If the repository is frozen on purpose, skip this step and the next one, the cleanup stays off.',
  'Что удалит первая чистка, без удаления и без блокировки репозитория.':
    'What the first cleanup would remove, without removing and without locking the repository.',
  'Включить чистку: строка в /etc/cron.d/kervax-prune-<имя> раскомментируется, чистка пойдет раз в сутки через 12 часов после бэкапа клиента. Первая же удалит снапшоты сверх политики, это необратимо.':
    'Turn the cleanup on: the line in /etc/cron.d/kervax-prune-<name> is uncommented and the cleanup runs daily 12 hours after the client\'s backup. The very first run removes snapshots beyond the policy, and that cannot be undone.',
  'Проверить: в файле строка с временем без # в начале. Через минуту пункт уйдет.':
    'Check: the file has a line with the time and no # in front. In a minute the item goes away.',
  'Выключить обратно, пока чистка не прошла.':
    'Turn it off again before the cleanup has run.',
  'Посмотреть: в crontab root строка этой чистки закомментирована руками, а в /etc/cron.d/kervax-prune-<имя> она идет. Ее включил helper до 0.34, когда перегенерировал скрипты.':
    'Look: in the root crontab the line of this cleanup is commented out by hand, while in /etc/cron.d/kervax-prune-<name> it runs. A helper older than 0.34 turned it on when it regenerated the scripts.',
  'Выключить, как было: строка в cron.d закомментируется с меткой # kervax-prune-off, обновления helper\'а ее больше не включат. Снапшоты, которые чистка уже удалила, не вернуть.':
    'Turn it off as it was: the cron.d line is commented out under # kervax-prune-off and helper updates no longer turn it on. Snapshots the cleanup already removed do not come back.',
  'Если чистка нужна: подтвердить, что она включена намеренно (метка # kervax-prune-on), пункт уйдет.':
    'If the cleanup is needed: confirm it is on on purpose (the # kervax-prune-on mark), and the item goes away.',
  'чистка выключена вручную, а бэкапы идут - {list}':
    'cleanup turned off by hand while backups keep coming - {list}',
  'чистка идет, хотя в crontab ее выключили руками - {list}':
    'cleanup runs although it was turned off by hand in crontab - {list}',
  'выключена {ago}':
    'off since {ago}',
  'чистка идет, хотя в crontab root ее строка закомментирована руками':
    'the cleanup runs although its line in the root crontab is commented out by hand',
  'чистка выключена вручную, а бэкапы идут: снапшотов {n}, растет без чистки':
    'cleanup turned off by hand while backups keep coming: {n} snapshots, growing without a cleanup',
  'чистка выключена вручную':
    'cleanup turned off by hand',
  'Включить: вкладка "Чистка", или на сервере бэкапов kervax-backupserver-helper prune-on {name}':
    'To turn it on: the "Cleanup" tab, or kervax-backupserver-helper prune-on {name} on the backup server',
  'образ заменен':
    'image replaced',
  'НЕ ЗАМЕНЕНО: строка image другая, поправьте ее руками':
    'NOT REPLACED: the image line is different, fix it by hand',
  'контейнер на новом образе':
    'the container runs the new image',
  'НЕ ОБНОВЛЕН: контейнер на прежнем образе, смотрите вывод выше':
    'NOT UPDATED: the container runs the old image, see the output above',
  'контейнер на свежем образе':
    'the container runs the fresh image',
  'ОБРАЗ ТОТ ЖЕ: автор образа его еще не пересобрал':
    'SAME IMAGE: its author has not rebuilt it yet',
  'репозиторий переведен':
    'repository switched',
  'НЕ ЗАМЕНЕНО: репозитория NodeSource в sources.list.d нет, поправьте руками':
    'NOT REPLACED: no NodeSource repository in sources.list.d, fix it by hand',
  'nodejs новой версии':
    'nodejs is the new version',
  'НЕ ОБНОВЛЕН: nodejs прежней версии':
    'NOT UPDATED: nodejs is the old version',
  'файл изменен':
    'file changed',
  'НЕ ИЗМЕНЕНО: нужной строки в блоке сервиса нет, поправьте руками':
    'NOT CHANGED: the service block has no such line, fix it by hand',
  'privileged снят':
    'privileged removed',
  'НЕ СНЯТ: контейнер все еще privileged':
    'NOT REMOVED: the container is still privileged',
  'docker-сокета у контейнера нет':
    'the container has no docker socket',
  'НЕ УБРАН: docker-сокет все еще у контейнера':
    'NOT REMOVED: the container still has the docker socket',
  'pg_stat_statements добавлен':
    'pg_stat_statements added',
  'НЕ ДОБАВЛЕНО: строка shared_preload_libraries другая, поправьте руками':
    'NOT ADDED: the shared_preload_libraries line is different, fix it by hand',
  'Ждем отчет агента после перезапуска, панель проверяет каждые {n} с.':
    'Waiting for the agent report after the restart, the panel checks every {n} s.',
  'Отчет агента в {time}: {c} все еще на {image}. Если перезапуск уже выполнен, образ не заменен: смотрите, что сказали последние строки замены образа и перезапуска.':
    'Agent report at {time}: {c} still runs {image}. If the restart is already done, the image was not replaced: see what the last lines of the image change and the restart said.',
  'Отчет агента в {time}: в {c} все еще {name} {from}. Если перезапуск уже выполнен, автор образа его еще не пересобрал.':
    'Agent report at {time}: {c} still has {name} {from}. If the restart is already done, the image author has not rebuilt it yet.',
  'Отчет агента в {time}: в {c} все еще {name} {from}. Если шаги уже выполнены, смотрите вывод последней команды.':
    'Agent report at {time}: {c} still has {name} {from}. If the steps are already done, see the output of the last command.',

  // --- вход админа под чужой учеткой (ImpersonationBar, UsersModal) ---
  'Только просмотр, изменения отклоняются. Вошел {admin}, осталось {n} мин.':
    'View only, changes are rejected. Signed in by {admin}, {n} min left.',
  'Вернуться к себе': 'Back to my account',
  'от имени {user}': 'as {user}',
  'войти как': 'sign in as',
  'вошел под учеткой': 'signed in as the account',
  'вышел из-под учетки': 'left the account',
  'неверный пароль при входе под учеткой': 'wrong password when signing in as an account',
  // окно Пользователи: список учеток
  'Роль - что можно менять, разделы - какие вкладки видны, группы - какие серверы и сайты доступны. "Все" - без ограничений.':
    'Role is what can be changed, sections are which tabs are visible, groups are which servers and sites are available. "All" means no limits.',
  'правка {e} · просмотр {v}': 'edit {e} · view {v}',
  '{n} из {m}': '{n} of {m}',
  'вы': 'you',
  'группы: все': 'groups: all',
  'разделы: {s}': 'sections: {s}',
  'серверы: {s} · сайты: {c}': 'servers: {s} · sites: {c}',
  'открыть учетку': 'open the account',
  'Посмотреть панель глазами этой учетки: только просмотр, на час': 'See the panel as this account does: view only, for an hour',
  // шаги версий ПО: фраза и строки контейнеров (Software.tsx)
  'ч': 'h',
  'дн': 'd',
  'нед': 'wk',
  'мес': 'mo',
  'г': 'y',
  'работает {n} {u}': 'up {n} {u}',
  '{name} {from} и {to} - разные основные версии: код сначала проверить на копии': '{name} {from} and {to} are different major versions: test the code on a copy first',
  'Код приложения сначала проверить на {name} {to}': 'Test the application code on {name} {to} first',
  '{name} {to} - новая основная версия: сначала прочитать upgrade guide': '{name} {to} is a new major version: read the upgrade guide first',
  'Grafana переведет свою базу на новую схему, обратно только из копии': 'Grafana migrates its database to a new schema, going back only from a copy',
  'Убрать только этот сервис, остальные в проекте работают. Убрать его и из compose-файла, иначе вернется при следующем up': 'Remove only this service, the rest of the project keeps running. Remove it from the compose file too, or it comes back on the next up',
  'Остановить и убрать проект, данные остаются на диске': 'Stop and remove the project, data stays on disk',
  'Остановить и удалить, данные остаются на диске': 'Stop and delete, data stays on disk',
  'Копия compose-файла': 'Copy of the compose file',
  'Копия данных, контейнер на это время остановится': 'Copy of the data, the container stops for that time',
  'Поменять образ на {newShort}': 'Change the image to {newShort}',
  'Перезапустить и проверить': 'Restart and check',
  'Вернуть прежний compose-файл и перезапустить': 'Restore the previous compose file and restart',
  'Вернуть прежний compose-файл и данные, перезапустить': 'Restore the previous compose file and data, restart',
  'Найти FROM в Dockerfile и поменять на {name} {to}': 'Find FROM in the Dockerfile and change it to {name} {to}',
  'Пересобрать и перезапустить': 'Rebuild and restart',
  'Вернуть прежний FROM и пересобрать': 'Restore the previous FROM and rebuild',
  'Сохранить текущий образ для отката': 'Keep the current image for rollback',
  'Скачать свежий образ и перезапустить': 'Pull a fresh image and restart',
  'Перевести репозиторий NodeSource на {to} и обновить nodejs': 'Switch the NodeSource repository to {to} and upgrade nodejs',
  'Проверить версию, перезапустить приложения': 'Check the version, restart the applications',
  'Вернуть репозиторий на {from} и прежнюю версию': 'Switch the repository back to {from} and the previous version',
  'НЕ ЗАМЕНЕНО в конце - строка image в compose другая (переменная, .env), поправить руками': 'NOT REPLACED at the end - the image line in compose is different (a variable, .env), fix it by hand',
  'НЕ ОБНОВЛЕН - образ в compose не заменен или такого тега нет, прежний контейнер работает': 'NOT UPDATED - the image in compose was not replaced or the tag does not exist, the previous container keeps running',
  'ОБРАЗ ТОТ ЖЕ - автор образа его еще не пересобрал': 'SAME IMAGE - its author has not rebuilt it yet',
  'НЕ ЗАМЕНЕНО - репозитория NodeSource нет, поправить руками': 'NOT REPLACED - there is no NodeSource repository, fix it by hand',
  'НЕ ОБНОВЛЕН - nodejs прежней версии; нативные модули пересобрать (npm rebuild)': 'NOT UPDATED - nodejs is still the old version; rebuild native modules (npm rebuild)',
  'ошибки в логе - откат ниже': 'errors in the log - rollback below',
  'выбрать версию': 'pick a version',
  'обновить FROM в сборке': 'update FROM in the build',
  'пересобрать здесь': 'rebuild here',
  'сменить тег на {tag}': 'change the tag to {tag}',
  'скачать свежий образ': 'pull a fresh image',
  'переезд через копию': 'move via a copy',
  'пересоздать контейнер': 'recreate the container',
  'обновить пакет {pkg}': 'upgrade the {pkg} package',
  'поменять FROM на {name} {to} в сборке образов {names} и выкатить. Образы собирают не здесь, на ноде делать ничего не надо': 'change FROM to {name} {to} in the build of the {names} images and deploy. The images are built elsewhere, nothing to do on the node',
  'поменять FROM на {name} {to} в сборке образа {names} и выкатить. Образ собирают не здесь, на ноде делать ничего не надо': 'change FROM to {name} {to} in the build of the {names} image and deploy. The image is built elsewhere, nothing to do on the node',
  'образ собирается на этой ноде: поменять FROM в Dockerfile на {name} {to} и пересобрать, команды ниже': 'the image is built on this node: change FROM in the Dockerfile to {name} {to} and rebuild, commands below',
  'сменить тег образа на {tag}: копия, замена строки в compose и перезапуск, команды ниже': 'change the image tag to {tag}: a copy, the compose line replaced and a restart, commands below',
  'скачать свежий образ и перезапустить {names}, команды ниже': 'pull a fresh image and restart {names}, commands below',
  'перейти на {name} {to} через копию данных, путь и команды ниже': 'move to {name} {to} via a data copy, the path and commands below',
  'перевести пакет {pkg} на {name} {to}, команды ниже': 'move the {pkg} package to {name} {to}, commands below',
  'перевести {pkg} на {name} {to}, как - ниже': 'move {pkg} to {name} {to}, how - below',
  'пакет {p}': 'package {p}',
  'как у {c}': 'same as {c}',
  'версия {v} из {k}': 'version {v} from {k}',
  'версия {v}': 'version {v}',
  'Образ тот же, что у {c}: шаги те же, в папке {d}.': 'Same image as {c}: same steps, in the {d} folder.',
  'откат, если что-то не так': 'rollback, if something is wrong',
  'пакет не нужен': 'package is not needed',
  'контейнер не нужен': 'container is not needed',
  'Пока шаги открыты, агент присылает отчет раз в {n} с, панель перечитывает их сама': 'While the steps are open, the agent reports every {n} s and the panel rereads them itself',
  'проверено в {time}': 'checked at {time}',
  'отчетов еще не было': 'no reports yet',
  'пункт уйдет сам в течение часа после обновления': 'the item goes away by itself within an hour after the upgrade',
  'пункт уйдет сам со следующим отчетом после обновления': 'the item goes away by itself with the next report after the upgrade',
  'скачать образ с {name} {to} и пересоздать {names} тем же docker run, команда ниже': 'pull the image with {name} {to} and recreate {names} with the same docker run, command below',
  'пересоздать с новым образом': 'recreate with the new image',

  // --- инвентаризация (Inventory.tsx, inventoryData.ts, inventoryHtml.ts) ---
  '{n} серв.': '{n} srv.',
  'Все группы': 'All groups',
  'Диски и RAID': 'Disks and RAID',
  'Итого': 'Total',
  'Итого: {n} серв.': 'Total: {n} srv.',
  'По группам': 'By group',
  'модели не видны: нет helper disk-health': 'models unknown: no disk-health helper',
  'пот.': 'thr.',
  'потоков всего': 'threads in total',
  'Инвентаризация': 'Inventory',
  'Парк': 'Fleet',
  'Что стоит на серверах: процессор, память, диски. Выберите группы или отдельные серверы и скачайте отчет в HTML или Excel.': 'What the servers have: CPU, memory, disks. Pick groups or single servers and download the report as HTML or Excel.',
  'вирт.': 'virt.',
  'диск': 'disk',
  '{v} ГГц': '{v} GHz',
  'ВМ': 'VM',
  'по разделам': 'by partitions',
  'Итого: {n}': 'Total: {n}',
  'Кратко': 'Short',
  'RAM, ГБ': 'RAM, GB',
  'Полно': 'Full',
  'Локальный IP': 'Local IP',
  'Страна': 'Country',
  'Процессор': 'CPU',
  'Потоков': 'Threads',
  'Частота, ГГц': 'Clock, GHz',
  'Дисков': 'Disks',
  'Диски, ТБ': 'Disks, TB',
  'Аптайм, дн': 'Uptime, days',
  'Устройство': 'Device',
  'Серийный номер': 'Serial number',
  'Размер, ТБ': 'Size, TB',
  'CPU (потоков)': 'CPU (threads)',
  '{kind}: дисков': '{kind}: disks',
  '{kind}: ТБ': '{kind}: TB',
  'Без helper disk-health, диски неизвестны': 'No disk-health helper, disks unknown',
  'Показатель': 'Metric',
  'Значение': 'Value',
  'Итоги': 'Totals',
  'Инвентаризация {brand}': '{brand} inventory',
  'дисков: {n}': 'disks: {n}',
  'Сервер / IP': 'Server / IP',
  'ОС / ядро': 'OS / kernel',
  'Серверов без helper disk-health: {n}. У них объем дисков по разделам, тип и модели неизвестны.': 'Servers without the disk-health helper: {n}. Their disk size is by partitions, type and models are unknown.',
  'Полная': 'Full',
  'Краткая': 'Short',
  'только Baremetal': 'Baremetal only',
  'только ВМ': 'VMs only',
  'с GPU': 'with GPU',
  'Нет helper disk-health: объем по разделам, тип неизвестен': 'No disk-health helper: size by partitions, type unknown',
  'агент еще не присылал данные': 'the agent has not sent data yet',
  'Фильтр': 'Filter',
  'имя, IP, CPU, ОС, страна': 'name, IP, CPU, OS, country',
  'Краткий': 'Short',
  'Полный': 'Full',
  'Скачать HTML': 'Download HTML',
  'Скачать Excel': 'Download Excel',
  'Ничего не выбрано': 'Nothing selected',
  'Диски неизвестны': 'Disks unknown',
  'нет helper disk-health': 'no disk-health helper',
  'Серверов нет.': 'No servers.',
  // куда идет бэкап, бэкап по http и перевод на https
  'открыть сервер бэкапов': 'open the backup server',
  'бэкап идет по http без шифрования: логин и пароль репозитория видны по дороге':
    'the backup goes over plain http: the repository login and password are visible on the way',
  'бэкап идет на {where} по http без шифрования: логин и пароль репозитория видны по дороге':
    'the backup goes to {where} over plain http: the repository login and password are visible on the way',
  'http оставлен': 'http kept',
  'Так задумано: убрать из "Требует действий"': 'This is intended: drop it from "Action needed"',
  'оставить http': 'keep http',
  'перевести на https': 'move to https',
  'бэкап по http оставлен': 'backup over http kept',
  'бэкап идет по http без шифрования - переведите на https': 'the backup goes over plain http - move it to https',
  '{name}: бэкап идет по http без шифрования на {dest} - переведите на https':
    '{name}: the backup goes over plain http to {dest} - move it to https',
  'TLS-порт клиенту на сервере бэкапов': 'the TLS port for the client on the backup server',
  'проверка по https и переключение': 'check over https and switch',
  'бэкап идет по https': 'the backup goes over https',
  'Перевод на TLS rest-server того же сервера бэкапов (порт 64101, сертификат самоподписанный): сервер бэкапов открывает клиенту порт, клиент читает репозиторий по https и переключается, только если это получилось. Не получилось - бэкап остается на http. Логин, пароль и репозиторий те же, снапшоты остаются.':
    'Moves to the TLS rest-server of the same backup server (port 64101, a self-signed certificate): the backup server opens the port for the client, the client reads the repository over https and switches only if that worked. If not, the backup stays on http. The login, password and repository stay the same, the snapshots stay.',
  'Перевести бэкап {name} на https? Если проверка по https не пройдет, бэкап останется на http.':
    'Move the backup of {name} to https? If the check over https fails, the backup stays on http.',
  'Перевести пока нельзя': 'Cannot move yet',
  'Перевести на https по очереди: {n}? У кого проверка по https не пройдет, бэкап останется на http.':
    'Move to https one by one: {n}? Where the check over https fails, the backup stays on http.',
  'по https': 'over https',
  'в очереди': 'queued',
  'Клиенты по http': 'Clients over http',
  'выбрать всех, кого можно': 'select all that can be moved',
  'перевести на https: {n}': 'move to https: {n}',
  'Переход на https': 'Move to https',
  'все клиенты по https': 'all clients over https',
  'по http: {list}': 'over http: {list}',
  'перевел бэкап сервера на https': 'moved the server backup to https',

  // --- постоянные разделы меню в окнах (ReportModal, DepMapModal, VulnsModal) ---
  'Алертов по бэкапам за период не было.': 'No backup alerts in this period.',
  'Таких мониторов нет: у каждого есть сервер.': 'No such monitors: each one has a server.',
  'Таких серверов нет: у каждого есть связи.': 'No such servers: each one has links.',
  'Таких серверов нет.': 'No such servers.',

  // --- галка "может читать логи" у учетки только для просмотра (UsersModal, LogsLocked) ---
  'Логи закрыты для этой учетки, может открыть админ': 'Logs are closed for this account, an admin can open them',
  'Может читать логи контейнеров и подов': 'Can read container and pod logs',
  'В логах бывают пароли и токены. Откроется и журнал systemd, менять учетка ничего не сможет.':
    'Logs may contain passwords and tokens. The systemd journal opens too, the account still cannot change anything.',
  'читает логи': 'reads logs',
  'разрешил учетке читать логи': 'allowed the account to read logs',
  'закрыл учетке логи': 'closed logs for the account',
  'посмотрел лог прошлого запуска пода': 'viewed the previous run log of a pod',

  // --- GitLab в "Сервисах" (GitlabSection, helper gitlab-setup) ---
  'Приложения': 'Applications',
  'GitLab устарел': 'GitLab is outdated',
  'есть обновление: {n}': 'update available: {n}',
  'последняя версия': 'latest version',
  'Версия GitLab против выпусков на Docker Hub и обязательных остановок обновления, бэкап, фоновые миграции и автообновление. Алерт, когда пора вмешаться: патч своей минорной не стоит дольше {p} дн, версия ниже обязательной остановки, вышедшей больше {s} дн назад, автообновление заблокировано или сломано, упали фоновые миграции.':
    'GitLab version against the releases on Docker Hub and the required upgrade stops, backup, background migrations and autoupgrade. Alert when it needs a hand: a patch of its own minor is not installed for over {p} days, the version is below a required stop released over {s} days ago, autoupgrade is blocked or broken, background migrations failed.',
  'Выпуски GitLab не скачались: {e}': 'GitLab releases were not fetched: {e}',
  'Выпуски GitLab еще не скачаны: актуальная версия появится после первой выборки': 'GitLab releases are not fetched yet: the current version shows after the first fetch',
  'выпуски с Docker Hub: {t}': 'releases from Docker Hub: {t}',
  'остановки: {s}': 'stops: {s}',
  'версия неизвестна': 'version unknown',
  'контейнер {s}': 'container {s}',
  'вышла {v}, переход на новую мажорную - руками': '{v} is out, a new major is a manual upgrade',
  'актуальная {v}': 'current {v}',
  'путь: {p}': 'path: {p}',
  'патч {v} вышел {n} дн назад': 'patch {v} was released {n} d ago',
  'есть патч {v}': 'patch {v} available',
  'ниже обязательной остановки {v}': 'below the required stop {v}',
  'сборка: {f}': 'built from {f}',
  'образ {i}': 'image {i}',
  'в {f}': 'in {f}',
  'данные {d}': 'data {d}',
  'конфиг {d}': 'config {d}',
  'последний {w} ({r}), {s}': 'last {w} ({r}), {s}',
  'без пропусков': 'nothing skipped',
  'архивов {n}, всего {s}': 'archives {n}, {s} in all',
  'архивов gitlab-backup нет': 'no gitlab-backup archives',
  'таймер {u} ({w})': 'timer {u} ({w})',
  'крон {w} ({f})': 'cron {w} ({f})',
  'только конфиг': 'config only',
  'и конфиг': 'and the config',
  'таймер {s}': 'timer {s}',
  'запуск {w}': 'ran {w}',
  'без части данных': 'part of the data skipped',
  'gitlab-secrets.json есть': 'gitlab-secrets.json present',
  'gitlab-secrets.json не найден': 'gitlab-secrets.json not found',
  'архив конфига {w}: {p}': 'config archive {w}: {p}',
  'архива конфига нет': 'no config archive',
  'конфиг в restic-бэкапе Kervax': 'config in the Kervax restic backup',
  'данные в restic-бэкапе Kervax (копия работающей базы)': 'data in the Kervax restic backup (a copy of a running database)',
  'Фоновые миграции': 'Background migrations',
  'идут: {n}': 'running: {n}',
  'все завершены ({n})': 'all finished ({n})',
  'Автообновление': 'Autoupgrade',
  'таймер включен, запуск {w}': 'timer on, next run {w}',
  'таймер включен': 'timer on',
  'таймер выключен, обновлять некому': 'timer off, nothing upgrades it',
  'последний прогон упал': 'the last run failed',
  'обновление ждет {n} дн': 'update waiting for {n} d',
  'бэкап перед шагом выключен': 'backup before a step is off',
  'сломан: {r}': 'broken: {r}',
  'заблокировано: {r}': 'blocked: {r}',
  'последний итог: {r}': 'last result: {r}',
  'gitlab-autoupgrade не стоит': 'gitlab-autoupgrade is not installed',
  'не алертить об этом GitLab:': 'do not alert about this GitLab:',
  'Не алертить об этом GitLab: на день, неделю или постоянно': 'Do not alert about this GitLab: for a day, a week or for good',
  'на паузе': 'paused',
  'упала': 'failed',
  'финализируется': 'finalizing',
  'статус {n}': 'status {n}',
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
