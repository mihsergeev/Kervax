"""Хранилище редактируемых настроек панели (в БД, поверх env-дефолтов)."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AppSetting

ALERTS_KEY = "alerts"
MUTED_KEY = "alerts_muted"
# состояние опроса бота (кнопки под алертами): offset getUpdates и что видно в настройках
TG_POLL_KEY = "tg_poll"
# эскалация: кому повторять алерт, на который никто не нажал "взял"
ESC_KEY = "alerts_escalation"
# чаты по группам: алерты выбранных групп серверов и сайтов идут еще и в свои чаты
ROUTES_KEY = "alerts_routes"
# какой чат по группам последним не принял сообщение: видно в настройках
ROUTES_STATE_KEY = "alerts_routes_state"
# локации со сломанным прокси: {id: {since, err, notified}} - один алерт на прокси, а не по сайтам
LOC_BROKEN_KEY = "locations_broken"
ESC_KINDS_DEFAULT = ["down", "offline"]
UNCOVERED_KEY = "uncovered_digest_sent"
VAULT_META_KEY = "backup_vault_meta"  # соль/итерации/verifier сейфа (СЕКРЕТОВ НЕТ)
RETENTION_KEY = "retention"
BACKUP_KEY = "backup"
BACKUP_LAST_KEY = "backup_last"
SERVER_RULES_KEY = "server_alert_rules"
SITE_RULES_KEY = "site_alert_rules"

# Типы серверных алертов: ключ → (подпись, дефолтный шаблон текста).
# Плейсхолдеры: {server} {group} {value} {threshold} {severity}.
# Типы серверных алертов. Текст — только «суть» (detail); collector оборачивает
# дефолт в богатый формат «<иконка> «<сервер-ссылкой>» — <detail>» (как у сайтов).
# Плейсхолдеры: {server} {group} {value} {threshold} {severity} {streak} {victim}.
SERVER_ALERT_KINDS: dict[str, tuple[str, str]] = {
    "offline": ("Недоступен", "недоступен, агент не шлёт метрики"),
    # {cause} - кто ест ресурс и не наплыв ли это (пусто, если сказать нечего):
    # без него алерт «CPU 92%» требовал отдельного разбора на сервере
    "cpu": ("CPU", "CPU {value}% ≥ {threshold}%{cause}"),
    "mem": ("RAM", "RAM {value}% ≥ {threshold}%{cause}"),
    # без слова-серьёзности: уровень и так виден по ведущей иконке (⚠️/🔴/🚨),
    # а «(предупреждение)» только удлиняло строку. {severity} остаётся доступным
    # тем, кто пишет свой шаблон
    # {cause} - на что ушло место и сколько освобождается без риска (helper diskusage-setup)
    "disk": ("Диск", "диск {value}% ≥ {threshold}%{cause}"),
    "temp": ("Температура CPU", "CPU {value}°C ≥ {threshold}°C"),
    "throttle": ("Троттлинг CPU", "устойчивый тепловой троттлинг CPU ({streak} интервала подряд)"),
    "conntrack": ("Conntrack", "таблица conntrack заполнена на {value}% ≥ {threshold}%"),
    "db_conn": ("Коннекты СУБД", "{engine}: занято {used} из {limit} подключений ({value}% ≥ {threshold}%)"),
    "disktemp": ("Температура диска", "диск {value}°C ≥ {threshold}°C"),
    # Поломка физического диска (helper diskhealth-setup): RAID развалился, диск не отвечает
    # или пропал, SMART говорит о неисправности, за сутки выросли счетчики ошибок
    "disk_health": ("Диск: поломка", "{detail}"),
    # Inode кончаются отдельно от места (агент 2.15): {cause} - где скопились файлы и когда
    # кончатся при нынешнем росте
    "inode": ("Диск: inode", "inode на {mount}: занято {value}% ≥ {threshold}%{cause}"),
    # Прогноз планировщика по истории заполнения: место или inode кончатся через дни
    "disk_forecast": ("Диск: скоро заполнится", "{detail}"),
    # Упавшие юниты systemd (helper units-setup): что упало, почему и последняя строка лога
    "units": ("Systemd: упал юнит", "{detail}"),
    # Сервис, который systemd сам перезапускает (Restart=), падает по кругу и не бывает failed
    # (helper units-setup 0.2): три прироста счетчика перезапусков за 15 минут
    "unit_loop": ("Systemd: юнит в цикле перезапусков", "{unit} перезапускается в цикле: {count} за {window} мин{why}"),
    # Обычный CronJob кластера (не дамп СУБД - для них backup_cron) упал или перестал отрабатывать
    "kube_cron": ("Kubernetes: CronJob не отрабатывает", "CronJob не отрабатывает: {jobs}"),
    # Одинаковые процессы давно крутят по ядру каждый (агент 2.21): зависшие браузеры, воркеры в
    # пустом цикле. Общий CPU при этом может быть далек от порога
    "cpu_spin": ("CPU: процессы в пустом цикле", "{detail}"),
    # Прогон бэкапа добавил в репозиторий в разы больше обычного (helper backup-setup 0.32):
    # кто-то начал складывать в бэкап временные файлы или дампы, и бэкап-сервер начнет пухнуть
    "backup_growth": ("Бэкап: резкий прирост", "{detail}"),
    "docker_sock": ("Docker: внешний прокси с docker-сокетом", "{detail}"),
    # Контейнер privileged, с опасными capabilities, docker-сокетом или корнем хоста на запись,
    # хотя по назначению ему это не нужно (docker_risk)
    "docker_risk": ("Docker: контейнер с ключами от хоста", "{detail}"),
    # Вторая учетка с uid 0 или учетка с shell и пустым паролем (access_audit)
    "access_risk": ("Доступ: второй root или пустой пароль", "{detail}"),
    # Панель проверяет порты ноды снаружи (агент 2.26 сообщает, что слушает): база, очередь,
    # Docker API, kubelet (проблема) или служебный порт Kubernetes, агента, экспортера
    # (предупреждение) доступны из интернета
    "exposed": ("Сеть: открыто в интернет то, что должно быть закрыто", "{detail}"),
    # Порт, который за месяц ни разу не был открыт снаружи, стал доступен из интернета
    "port_new": ("Сеть: в интернет открылся новый порт", "{detail}"),
    # Сервер задыхается (агент 2.26): процессы ждут память или диск (PSI), процесс завис в D
    # (агент 2.40), кончаются файловые дескрипторы. {cause} - кто ест память или какой диск занят
    # и для зависшего вторая строка "↳ ..." - что это значит
    "pressure": ("Сервер задыхается", "{detail}{cause}"),
    # Сеть ноды: TCP теряет пакеты, сервис не успевает принимать соединения, conntrack
    # выбрасывает новые, ошибки и дропы на интерфейсах
    "net_health": ("Сеть: потери и ошибки", "{detail}"),
    # Агент 2.31 раз в 30 секунд пробует открыть TCP-соединение с заданными адресами (вход VPN с
    # выходом, приложение с базой): за 5 минут ни одна попытка не прошла или не прошла каждая пятая
    "link": ("Сеть: связь между серверами", "{detail}"),
    # Трафик за расчетный период по настоящим сетевым картам против лимита тарифа: лимит
    # исчерпан, израсходовано 90% или к концу периода при нынешнем темпе выйдет больше лимита
    "traffic": ("Сеть: лимит трафика тарифа", "{detail}"),
    # Нода кластера NotReady или под давлением (MemoryPressure, DiskPressure, PIDPressure)
    "kube_node": ("Kubernetes: нода не готова или под давлением", "{detail}"),
    # Разовый Job (миграция, задача не по расписанию) упал: BackoffLimitExceeded и т.п.
    "kube_job": ("Kubernetes: упал Job", "{detail}"),
    # Изменился доступ к серверу (helper changes-setup): новый SSH-ключ, новый пользователь с
    # shell или uid 0, кто-то добавлен в sudo, изменились sudoers
    "access_change": ("Сервер: изменился доступ", "{detail}"),
    # Раз в неделю helper backup-setup восстанавливает из снапшота несколько файлов и читает
    # дампы: файл не совпал с диском, дамп не читается, проверка не выполнилась или давно не шла
    "backup_restore": ("Бэкап: проверка восстановления", "{detail}"),
    # Медленный цикл: контейнер или под перезапускается раз в час-другой (5 и больше за сутки).
    # Быстрый цикл (3 за 15 минут) - docker_loop и CrashLoopBackOff, это предупреждение
    "docker_restarts": ("Docker: часто перезапускается", "{detail}"),
    "kube_restarts": ("Kubernetes: под часто перезапускается", "{detail}"),
    # Дно памяти контейнера, пода или процесса растет 4 суток подряд: утечка. Проблема, если
    # свободной памяти при таком росте хватит меньше чем на трое суток
    "mem_leak": ("Память: растет день за днем", "{detail}"),
    # SSH открыт в интернет (внешняя проверка портов) и пускает root по паролю (проблема) или
    # идет перебор паролей, а fail2ban/sshguard/crowdsec нет (предупреждение). helper changes-setup 0.3
    "ssh_risk": ("SSH: опасный вход", "{detail}"),
    # Здоровье баз (helper dbstat-setup 0.5): реплика стоит или отстает, транзакция идет часами
    # или висит незакрытой, запросы ждут блокировку, возраст XID ползет к wraparound, таблица
    # давно без VACUUM
    "db_health": ("СУБД: реплика, транзакции, запись, VACUUM", "{detail}"),
    # nginx -t, apachectl -t, caddy validate или haproxy -c не проходит (helper webserver-setup
    # 0.23): сейчас работает старый конфиг, следующий reload или перезапуск уронит сайты
    "web_config": ("Веб-сервер: конфиг не проходит проверку", "{detail}"),
    # Сертификат, который отдает nginx, истекает через две недели и меньше или уже истек
    # (helper webserver-setup 0.24); сайты за белым списком мониторы не видят
    "web_cert": ("Веб-сервер: сертификат истекает", "{detail}"),
    # Порт, который слушался неделю и больше, не виден дольше 15 минут: сервис вне systemd и
    # Docker (nohup, screen, supervisor) тихо умер
    "listen_gone": ("Сервер: пропал порт", "{detail}"),
    # Задержка диска 100 мс и выше держится дольше выдержки, 500 мс - проблема
    "disk_slow": ("Диск тормозит", "{detail}"),
    # GPU NVIDIA (helper gpu-setup): драйвер не отвечает, карта пропала с шины, ошибка Xid, после
    # которой нужен сброс или замена карты, сбойная память ждет исключения
    "gpu": ("GPU: драйвер, пропала карта, ошибки Xid", "{detail}"),
    # Карта замедлилась из-за температуры или дошла до порога замедления (предупреждение - за
    # 5 градусов или выше рабочего предела)
    "gpu_heat": ("GPU: перегрев", "{detail}"),
    # vLLM, SGLang, TEI (агент 2.36): /metrics не отвечает при живом процессе, очередь при полном
    # KV-кэше, задержка в разы выше обычной держится под нагрузкой (разовый всплеск - только в разделе LLM)
    "llm": ("LLM: не отвечает, очередь, задержки", "{detail}"),
    "reboot": ("Перезагрузка", "перезагружен (аптайм сброшен)"),
    "oom": ("OOM-killer", "OOM-kill: ядро убило {value} процесс(ов){victim} из-за нехватки памяти"),
    "docker_down": ("Docker: контейнер упал", "контейнер {container} не работает: {why}"),
    "docker_loop": ("Docker: перезапуски", "контейнер {container} постоянно перезапускается ({restarts} рестарта за {window} мин)"),
    "backup_missing": ("Бэкап: не настроен", "на сервере не настроен бэкап (галочка «не требуется» снимает алерт)"),
    "backup_failed": ("Бэкап: ошибка", "последний бэкап завершился с ошибкой"),
    "backup_stale": ("Бэкап: не свежий", "бэкап не обновлялся {days} дн. (последний успех устарел)"),
    # причина теперь у каждого репозитория в {repos}, а висячий лок ушел в backup_lock
    "backup_repo": ("Бэкап-сервер: репозитории", "репозитории требуют внимания: {repos}"),
    # Висячий лок дольше суток (helper backupserver-setup 0.23 отдает самый старый): чистка
    # и restic check по репозиторию не идут, место на бэкап-сервере растет
    # Новая нода пишет бэкап на наш сервер, а агента на ней нет: сломанный бэкап панель
    # заметит только по ритму бэкапов репозитория, а агент сказал бы сразу
    "backup_unmonitored": ("Бэкап-сервер: клиент без агента",
                           "сюда пишет бэкап нода без агента в панели: {repos}. Её сломанный бэкап "
                           "панель заметит только по ритму, ежедневный через 30 часов, а агент на "
                           "ноде сказал бы сразу"),
    # prune-скрипт не открывает репозиторий, падает, пропускает чистку или не запускается: нет
    # ни ротации, ни недельной проверки, а алерт ротации сказал бы только через несколько дней
    "backup_prune": ("Бэкап-сервер: чистка не работает",
                     "чистка репозиториев не работает: {repos}"),
    # DNS ноды (агент 2.25): имя не из кэша резолвится дольше 1,5 с или все резолверы тормозят,
    # 10 минут подряд. {details} - что именно и какие резолверы
    "dns": ("DNS на ноде", "DNS тормозит или не отвечает: {details}"),
    # Недельная проверка целостности (restic check в нашем prune-скрипте) не прошла
    "backup_check": ("Бэкап-сервер: проверка репозитория не прошла",
                     "restic check нашел ошибки в репозиториях: {repos}. Вывод проверки в логе "
                     "prune-скрипта на бэкап-сервере (/app/rest-server/system/logs)"),
    "backup_lock": ("Бэкап-сервер: репозиторий залочен",
                    "висячий лок держится больше суток, чистка и проверка не идут: {repos}"),
    "backup_dump": ("Бэкап: дамп СУБД не снимается", "дамп не обновляется: {engines} ({reason})"),
    "backup_dump_space": ("Бэкап: дамп пропущен, мало места", "дамп не снят из-за нехватки места (свободно {free}%): {engines} — расчистите раздел"),
    "backup_cron": ("Бэкап: дамп-CronJob не отрабатывает", "дамп-CronJob не отрабатывает (приостановлен / прогон упал / давно нет успешного дампа): {jobs}"),
    "backup_custom": ("Бэкап: свой бэкап не отрабатывает", "свой бэкап не отрабатывает: {jobs}"),
    "backup_rotation": ("Бэкап-сервер: ротация встала",
                        "старые снапшоты не вычищаются: {repos}"),
    "queue": ("Очередь выше порога",
              "очередь {queue} ({source}): {value} сообщений ≥ {threshold}"),
    # Очередь полчаса растет и не убывает (или три часа не уменьшилась ни на одно сообщение) - без
    # ручного порога; очереди мертвых писем и служебные не судятся (queue_watch)
    "queue_stuck": ("Очередь растет и не разбирается", "{detail}"),
    "queue_failed": ("Всплеск упавших задач", "{detail}"),
    # Воркер под supervisor или pm2 не запускается (FATAL, errored, BACKOFF дольше трех минут),
    # завершился с ошибкой или падает и поднимается (helper workers-setup)
    "workers": ("Воркер упал или падает и поднимается", "{detail}"),
    # копии крона копятся или одна висит в разы дольше обычного (helper cron-setup)
    "cron_stuck": ("Крон наслаивается или висит", "{detail}"),
    # строк с ошибками в логе контейнера за час в разы больше обычного (helper logerr-setup)
    "log_errors": ("Всплеск ошибок в логах", "{detail}"),
    # пул PHP-FPM подолгу упирается в pm.max_children или рвет запросы по таймауту (helper fpm-setup)
    "fpm": ("PHP-FPM не справляется", "{detail}"),
    "web_5xx": ("Веб: пошли ошибки 5xx",
                "{value}% запросов за 15 минут закончились ошибкой 5xx ({errs} из {total}){where}"),
    "kube_pod": ("Kubernetes: под не поднимается",
                 "не поднимаются поды: {pods}"),
    "clock": ("Время: сдвиг часов", "часы разошлись на {value} с временем панели — ломает TOTP/TLS/корреляцию логов, синхронизируйте время"),
    # Дата — только половина новости: без «что делать» «ИСТЁК 49 дн. назад» вызывает
    # не действие, а недоумение, ведь кластер работает. Но и рассуждать в алерте не
    # нужно — было три строки, из них две объясняли, почему всё ещё жив. Осталось
    # две: что и где сверху, срок и следующий шаг снизу.
    "kube_expiry": ("Kubernetes: истекает срок",
                    "{what} {where}\n↳ {value}, {date}{more}{advice}"),
    # Первая строка отвечает на «что случилось», вторая — «где именно», третья — «куда
    # смотреть». Раньше это был сырой вывод Flux одной простынёй, и прочитать его можно
    # было, только зная Flux наизусть.
    "flux_down": ("Flux: доставка встала",
                  "доставка встала: {reason}\n↳ {what} {where}{more}\n↳ {message}{hint}"),
    # Упали только репозитории чартов, а развернутое работает на закешированном чарте:
    # не придут лишь новые версии чартов, изменения из Git применяются как раньше
    "flux_stale": ("Flux: репозиторий чартов недоступен",
                   "репозиторий чартов недоступен, развернутое работает, не придут только новые "
                   "версии чартов\n{what} {where}{more}\n{message}{hint}"),
    # Сводка, а не событие: одно сообщение раз в сутки про ноды, где появилось
    # новое (СУБД, кластер, веб-сервер, докер), а покрытия под это ещё нет. Тут
    # нечего «восстанавливать» — пункт закрывается прогоном плейбука.
    "uncovered": ("Появилось новое без покрытия",
                  "новое без покрытия мониторингом:\n{list}\n↳ поставить: {cmd}"),
    # В панель с одного адреса идет поток отказов (неверный токен агента, удаленный push,
    # перебор адресов API) или запросов слишком много (app/request_watch.py). {who} - сервер
    # панели или чужой адрес, {what} - сколько и какие, {hint} - на что похоже
    "bogus_requests": ("Панель: левые запросы",
                       "в панель идут левые запросы {who}: {what}\n↳ {hint}"),
}

# Прежние («Kervax: сервер …») дефолты серверных правил. Если в БД сохранён один
# из них — считаем его НЕ кастомным и рендерим новым богатым форматом (без «Kervax:»,
# имя сервера — ссылкой), а не гоняем старый текст. Иначе установки, где форму
# серверных алертов однажды сохранили, застряли бы на старом «Kervax:»-тексте.
LEGACY_SERVER_DEFAULTS: frozenset[str] = frozenset({
    # клиент без агента до 1.4.118: тогда сломанный бэкап панель видела только через 3 дня
    "сюда пишет бэкап нода без агента в панели: {repos}. Если ее бэкап "
    "сломается, панель узнает только через 3 дня, когда репозиторий устареет",
    # репозитории бэкап-сервера до 1.4.107: без причин, висячий лок был одной из них
    "репозитории требуют внимания (устарели/битые/залочены): {repos}",
    # дефолт "контейнер упал" до 1.4.98: без причины - тревогу поднимала и остановка руками
    "контейнер {container} не работает ({state}, restart-policy: {policy})",
    # сроки Kubernetes до 1.4.69: совет шел всегда, через точку, даже пустой
    "{what} {where}\n↳ {value}, {date}{more} · {advice}",
    # первый текст алерта 5xx (1.4.58): со «сверху», которое читалось непонятно
    "ошибки 5xx: {value}% за 15 минут ({errs} из {total}){top}",
    # дефолты CPU и RAM до 1.4.48 - без {cause}
    "CPU {value}% ≥ {threshold}%",
    "RAM {value}% ≥ {threshold}%",
    # дефолт диска до 1.4.76 - без {cause} (на что ушло место)
    "диск {value}% ≥ {threshold}%",
    # дефолт дампа до 1.4.42 знал только ноды с бэкапом («хотя бэкап идёт»); без бэкапа
    # дамп теперь тоже проверяется, и причина приходит в {reason}
    "дамп не обновляется, хотя бэкап идёт: {engines} (файловый снапшот живой базы может не восстановиться)",
    "🖥 Kervax: сервер «{server}» — недоступен, агент не шлёт метрики",
    "🖥 Kervax: сервер «{server}» — CPU {value}% ≥ {threshold}%",
    "🖥 Kervax: сервер «{server}» — RAM {value}% ≥ {threshold}%",
    "🖥 Kervax: сервер «{server}» — диск {value}% ≥ {threshold}% ({severity})",
    "🌡 Kervax: сервер «{server}» — CPU {value}°C ≥ {threshold}°C",
    "🥵 Kervax: сервер «{server}» — устойчивый тепловой троттлинг CPU ({streak} интервала подряд)",
    "🔗 Kervax: сервер «{server}» — таблица conntrack заполнена на {value}% ≥ {threshold}%",
    "🌡 Kervax: сервер «{server}» — диск {value}°C ≥ {threshold}°C",
    # дефолт диска со словом «(предупреждение)» — до того, как уровень стал виден
    # по иконке; у кого он сохранён в БД, тот получит новый короткий текст
    "диск {value}% ≥ {threshold}% ({severity})",
    "🔄 Kervax: сервер «{server}» — перезагружен (аптайм сброшен)",
    "🧠 Kervax: сервер «{server}» — OOM-kill: ядро убило {value} процесс(ов){victim} из-за нехватки памяти",
    # прежний трёхстрочный формат сроков Kubernetes: у кого он лежит в БД, тот
    # получит новый короткий текст, а не застрянет на старом как на «своём»
    "{what} {where}\n↳ {value}, {date}{more}\n↳ {advice}",
})

# Типы алертов сайтов/мониторов. Плейсхолдеры: {name} {group} {message} {status}.
# Дефолт (текст == значению ниже) рендерится «богатым» форматом в collector:
#   «<хост> <иконка> — <имя-ссылкой на монитор> — <текст>» (см. _alert_text).
# Кастомный текст пользователя рендерится через .format() как обычная строка.
SITE_ALERT_KINDS: dict[str, tuple[str, str]] = {
    "down": ("Недоступен / деградация", "🔴 {name} — {message}"),
    "recovery": ("Восстановление", "✅ {name} — {message}"),
    # сбои вперемешку с успешными проверками: N подряд не набирается (см. flaky.py)
    "flaky": ("Отвечает через раз", "🟠 {name} — {message}"),
    "ssl": ("SSL-сертификат", "🔐 {name} — {message}"),
    "domain": ("Домен", "🌐 {name} — {message}"),
    "locpart": ("Частичная доступность (локации)", "🌍 {name}\n{message}"),
}

# Прежние (до-ребрендовые/«Kervax:») дефолты сайтовых правил. Если в БД сохранён
# один из них — считаем его НЕ кастомным и рендерим новым богатым форматом, а не
# гоняем старый шаблон. Иначе установки, где форму алертов однажды сохранили,
# застряли бы на старом тексте с «Kervax».
LEGACY_SITE_DEFAULTS: frozenset[str] = frozenset({
    "🔴 Kervax: «{name}» — недоступен: {message}",
    "✅ Kervax: монитор «{name}» снова в норме",
    "🔐 Kervax: «{name}» — {message}",
    "🌐 Kervax: «{name}» — {message}",
    "🌍 Kervax: «{name}»\n{message}",
})


# Доступ ansible к списку нод (GET /api/ansible/servers). Храним только хеш токена: сам
# токен показываем один раз при выпуске, как токен агента. used_at - когда ansible
# последний раз спрашивал, чтобы в настройках было видно, что связка работает.
_ANSIBLE_KEY = "ansible_access"


async def get_ansible_access(session: AsyncSession) -> dict:
    raw = await _get_raw(session, _ANSIBLE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def set_ansible_access(session: AsyncSession, data: dict | None) -> None:
    await _set_raw(session, _ANSIBLE_KEY, json.dumps(data) if data else "")


async def get_raw(session: AsyncSession, key: str) -> str | None:
    return await _get_raw(session, key)


async def set_raw(session: AsyncSession, key: str, value: str) -> None:
    await _set_raw(session, key, value)


async def _get_raw(session: AsyncSession, key: str) -> str | None:
    return await session.scalar(
        select(AppSetting.value).where(AppSetting.key == key)
    )


async def _set_raw(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_alert_config(session: AsyncSession, settings: Settings) -> dict:
    """Эффективная конфигурация алертов: значение из БД, иначе — из env."""
    raw = await _get_raw(session, ALERTS_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "telegram_token": data.get("telegram_token") or settings.alert_telegram_token,
        "telegram_chat": data.get("telegram_chat") or settings.alert_telegram_chat,
        "telegram_api": (
            data.get("telegram_api") or settings.alert_telegram_api
        ).rstrip("/"),
        "webhook": data.get("webhook") or settings.alert_webhook,
        "flood_threshold": int(
            data.get("flood_threshold", settings.alert_flood_threshold)
        ),
        # кнопки под алертами и команды бота: панель читает апдейты бота. Выключено по
        # умолчанию - если этого бота уже читает кто-то еще, мы бы забирали его апдейты
        "tg_buttons": bool(data.get("tg_buttons", False)),
        "tz": data.get("tz") or settings.alert_tz,
        # чаты по группам: отдельный ключ, чтобы сохранение основных настроек их не стирало
        "routes": await get_routes(session),
    }


async def set_alert_config(
    session: AsyncSession,
    telegram_token: str,
    telegram_chat: str,
    webhook: str,
    telegram_api: str = "",
    flood_threshold: int = 6,
    tg_buttons: bool = False,
    tz: str = "",
) -> None:
    await _set_raw(
        session,
        ALERTS_KEY,
        json.dumps(
            {
                "telegram_token": telegram_token.strip(),
                "telegram_chat": telegram_chat.strip(),
                "telegram_api": telegram_api.strip().rstrip("/"),
                "webhook": webhook.strip(),
                "flood_threshold": max(0, min(1000, int(flood_threshold))),
                "tg_buttons": bool(tg_buttons),
                "tz": tz.strip(),
            }
        ),
    )


def _esc_clean(data: dict) -> dict:
    def num(v, lo, hi, dflt):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return dflt
    kinds = data.get("kinds")
    return {
        "enabled": bool(data.get("enabled", False)),
        "after_min": num(data.get("after_min"), 5, 720, 15),
        "repeat": num(data.get("repeat"), 1, 5, 1),
        "chat": str(data.get("chat") or "").strip()[:64],
        "users": [str(u)[:64] for u in (data.get("users") or []) if str(u).strip()][:50],
        "kinds": [str(k)[:40] for k in kinds][:50] if isinstance(kinds, list) else list(ESC_KINDS_DEFAULT),
    }


def _names(v) -> list[str]:
    out: list[str] = []
    for x in v if isinstance(v, list) else []:
        x = str(x).strip()[:128]
        if x and x not in out:
            out.append(x)
    return out[:100]


def _routes_clean(data: dict) -> dict:
    """main: "all" - основной чат получает все алерты, "rest" - только то, что не ушло в чаты по
    группам. chats: [{name, chat, server_groups, site_groups}], чат без групп ничего не получает."""
    chats = []
    for c in data.get("chats") or []:
        if not isinstance(c, dict):
            continue
        chat = str(c.get("chat") or "").strip()[:64]
        if not chat:
            continue
        chats.append({
            "name": str(c.get("name") or "").strip()[:64],
            "chat": chat,
            "server_groups": _names(c.get("server_groups")),
            "site_groups": _names(c.get("site_groups")),
        })
    return {"main": "rest" if data.get("main") == "rest" else "all", "chats": chats[:20]}


async def get_routes(session: AsyncSession) -> dict:
    raw = await _get_raw(session, ROUTES_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return _routes_clean(data if isinstance(data, dict) else {})


async def set_routes(session: AsyncSession, data: dict) -> None:
    await _set_raw(session, ROUTES_KEY, json.dumps(_routes_clean(data), ensure_ascii=False))


async def get_routes_state(session: AsyncSession) -> dict:
    """{chat: {"err": текст, "at": когда}} - чаты, которые последний раз не приняли сообщение."""
    raw = await _get_raw(session, ROUTES_STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def set_routes_state(session: AsyncSession, data: dict) -> None:
    await _set_raw(session, ROUTES_STATE_KEY, json.dumps(data, ensure_ascii=False))


async def get_escalation(session: AsyncSession) -> dict:
    """Эскалация: включена ли, через сколько минут без "взял", сколько повторов, в какой чат и
    каким учеткам, по каким видам алертов."""
    raw = await _get_raw(session, ESC_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return _esc_clean(data if isinstance(data, dict) else {})


async def set_escalation(session: AsyncSession, data: dict) -> None:
    await _set_raw(session, ESC_KEY, json.dumps(_esc_clean(data), ensure_ascii=False))


async def get_tg_poll(session: AsyncSession) -> dict:
    """Состояние опроса бота: offset, статус (ok, conflict, error, off), когда, имя бота."""
    raw = await _get_raw(session, TG_POLL_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def set_tg_poll(session: AsyncSession, data: dict) -> None:
    await _set_raw(session, TG_POLL_KEY, json.dumps(data, ensure_ascii=False))


async def get_muted(session: AsyncSession) -> bool:
    """Глобальная пауза алертов (галочка «не слать»). По умолчанию False."""
    return await _get_raw(session, MUTED_KEY) == "1"


async def set_muted(session: AsyncSession, muted: bool) -> None:
    await _set_raw(session, MUTED_KEY, "1" if muted else "0")


async def get_uncovered_sent(session: AsyncSession) -> float:
    """Когда в последний раз уходила сводка о непокрытом (unix, 0 = никогда).

    Метка в БД, а не в памяти планировщика: рестарт контейнера не должен
    оборачиваться повторной сводкой, а два одинаковых сообщения подряд быстрее
    всего учат не читать сводку вовсе."""
    raw = await _get_raw(session, UNCOVERED_KEY)
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


async def set_uncovered_sent(session: AsyncSession, ts: float) -> None:
    await _set_raw(session, UNCOVERED_KEY, str(int(ts)))


async def get_retention(session: AsyncSession, settings: Settings) -> dict:
    """Сроки хранения тайм-серий (дней): значение из БД, иначе — из env."""
    raw = await _get_raw(session, RETENTION_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "server_days": int(
            data.get("server_days", settings.server_metric_retention_days)
        ),
        "sample_days": int(data.get("sample_days", settings.sample_retention_days)),
    }


async def set_retention(session: AsyncSession, server_days: int, sample_days: int) -> None:
    await _set_raw(
        session,
        RETENTION_KEY,
        json.dumps(
            {
                "server_days": max(1, min(3650, int(server_days))),
                "sample_days": max(1, min(3650, int(sample_days))),
            }
        ),
    )


async def get_backup_config(session: AsyncSession, settings: Settings) -> dict:
    """Настройки автобэкапа: интервал (часы, 0 = выкл) и сколько файлов хранить."""
    raw = await _get_raw(session, BACKUP_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "interval_hours": int(
            data.get("interval_hours", settings.backup_interval_hours)
        ),
        "keep": int(data.get("keep", settings.backup_keep)),
    }


async def _get_rules(session: AsyncSession, key: str, kinds: dict) -> dict:
    """Правила алертов: kind → {enabled, text, scope_type, scope}. text пусто → дефолт."""
    raw = await _get_raw(session, key)
    data = json.loads(raw) if raw else {}
    out: dict = {}
    for kind, (_label, default_text) in kinds.items():
        r = data.get(kind) or {}
        st = r.get("scope_type")
        out[kind] = {
            "enabled": bool(r.get("enabled", True)),
            "text": (r.get("text") or "").strip() or default_text,
            "scope_type": st if st in ("all", "groups", "servers", "checks") else "all",
            "scope": r.get("scope") if isinstance(r.get("scope"), list) else [],
        }
    return out


async def _set_rules(session: AsyncSession, key: str, kinds: dict, rules: dict) -> None:
    clean: dict = {}
    for kind, (_label, default_text) in kinds.items():
        r = rules.get(kind) or {}
        st = r.get("scope_type")
        clean[kind] = {
            "enabled": bool(r.get("enabled", True)),
            "text": (str(r.get("text") or "").strip()[:500]) or default_text,
            "scope_type": st if st in ("all", "groups", "servers", "checks") else "all",
            "scope": r.get("scope") if isinstance(r.get("scope"), list) else [],
        }
    await _set_raw(session, key, json.dumps(clean, ensure_ascii=False))


async def get_server_alert_rules(session: AsyncSession) -> dict:
    return await _get_rules(session, SERVER_RULES_KEY, SERVER_ALERT_KINDS)


async def set_server_alert_rules(session: AsyncSession, rules: dict) -> None:
    await _set_rules(session, SERVER_RULES_KEY, SERVER_ALERT_KINDS, rules)


async def get_site_alert_rules(session: AsyncSession) -> dict:
    return await _get_rules(session, SITE_RULES_KEY, SITE_ALERT_KINDS)


async def set_site_alert_rules(session: AsyncSession, rules: dict) -> None:
    await _set_rules(session, SITE_RULES_KEY, SITE_ALERT_KINDS, rules)


async def set_backup_config(session: AsyncSession, interval_hours: int, keep: int) -> None:
    await _set_raw(
        session,
        BACKUP_KEY,
        json.dumps(
            {
                "interval_hours": max(0, min(24 * 30, int(interval_hours))),
                "keep": max(1, min(365, int(keep))),
            }
        ),
    )


async def get_vault_meta(session: AsyncSession) -> dict:
    """Параметры сейфа доступов. Пусто = сейф не заведён. Соль и verifier секретами
    не являются: без vault-пароля из них ничего не выводится."""
    raw = await _get_raw(session, VAULT_META_KEY)
    d = json.loads(raw) if raw else {}
    return {
        "salt": d.get("salt", ""),
        "iterations": int(d.get("iterations", 0) or 0),
        "verifier_nonce": d.get("verifier_nonce", ""),
        "verifier": d.get("verifier", ""),
    }


async def set_vault_meta(session: AsyncSession, meta: dict) -> None:
    await _set_raw(session, VAULT_META_KEY, json.dumps(meta))
