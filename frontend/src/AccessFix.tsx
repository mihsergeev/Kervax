import type { OsFixStep } from './api'
import { FixCmd } from './FixSteps'
import { useI18n } from './i18n'

type T = (k: string, p?: Record<string, string | number>) => string

// Главный шаг: заголовок и пояснение по id (access_audit.disable_fix)
function mainText(st: OsFixStep, t: T): [string, string] {
  const p = { arg: st.arg ?? '' }
  switch (st.id) {
    case 'acc_alone':
      return ['', t('Через другие учетки за 90 дней никто не входил. Если отключить {arg}, войти на сервер может стать некому: сначала проверьте вход другой учеткой с sudo или через консоль.', p)]
    case 'acc_off':
      return [t('Отключить учетку {arg}', p), t('Срок учетки уходит в прошлое, пароль блокируется, ключи переименовываются в authorized_keys.disabled. SSH не пустит ни по паролю, ни по ключу, cron и su для нее тоже.')]
    case 'acc_off_nokeys':
      return [t('Отключить учетку {arg}', p), t('Срок учетки уходит в прошлое, пароль блокируется. SSH ее не пустит, cron и su для нее тоже.')]
    case 'acc_root_off':
      return [t('Отключить вход под {arg}', p), t('Пароль {arg} блокируется, ключи переименовываются в authorized_keys.disabled. sudo и cron root продолжат работать, срок учетки root не ставят: истекшая root ломает cron и su. Если в консоль входите root по паролю, после блокировки не войдете.', p)]
    case 'acc_root_lock':
      return [t('Отключить вход под {arg}', p), t('Пароль {arg} блокируется: sudo и cron root продолжат работать, срок учетки root не ставят, истекшая root ломает cron и su. Если в консоль входите root по паролю, после блокировки не войдете.', p)]
    case 'acc_root_keys':
      return [t('Отключить вход под {arg}', p), t('Ключи переименовываются в authorized_keys.disabled. Пароль {arg} не трогаем: по SSH паролем root и так не пускают, а в консоли он может пригодиться.', p)]
    default:
      return [st.id, '']
  }
}

// Как отключить заброшенную учетку. Раньше шаги шли одинаковыми блоками (проверка, блокировка,
// ключи, откат, удаление) и было непонятно, что копировать. Теперь: проверка мелко и явно как
// проверка, одно главное действие "Отключить" с одной командой (блокировка и ключи через &&),
// откат и удаление свернуты под строками "Если понадобится вернуть" и "Удалить совсем"
export function AccessFix({ steps }: { steps: OsFixStep[] }) {
  const { t } = useI18n()
  const check = steps.find((st) => st.id === 'acc_check')
  const main = steps.find((st) => st.id !== 'acc_check' && !st.undo && !st.alt)
  const undo = steps.find((st) => st.undo)
  const del = steps.find((st) => st.alt)
  const [head, note] = main ? mainText(main, t) : ['', '']
  return (
    <div className="accfix">
      {check?.cmd && (
        <div className="accfix-check">
          <div className="accfix-check-head">
            <span className="accfix-tag">{t('проверка')}</span>
            {t('Сначала проверить, что от {arg} ничего не работает', { arg: check.arg ?? '' })}
          </div>
          <div className="fix-b-note">
            {t('Процессы, crontab, lingering и сервисы с User={arg}. Пустой вывод и no crontab - можно отключать.', { arg: check.arg ?? '' })}
          </div>
          <FixCmd cmd={check.cmd} />
        </div>
      )}
      {main && (
        <div className={`accfix-main${main.cmd ? '' : ' accfix-warn'}`}>
          {head && <div className="accfix-main-head">{head}</div>}
          {note && <div className="fix-b-note">{note}</div>}
          {main.cmd && <FixCmd cmd={main.cmd} />}
          {main.cmd && <div className="fix-b-foot">{t('Учетка уйдет из заброшенных в течение 10 минут.')}</div>}
        </div>
      )}
      {undo?.cmd && (
        <details className="accfix-more">
          <summary>{t('Если понадобится вернуть')}</summary>
          <div className="fix-b-note">{t('Возвращает все как было до отключения.')}</div>
          <FixCmd cmd={undo.cmd} />
        </details>
      )}
      {del?.cmd && (
        <details className="accfix-more accfix-del">
          <summary>
            {t('Удалить совсем')}
            <span className="accfix-irrev">{t('необратимо')}</span>
          </summary>
          <div className="fix-b-note">
            {t('Домашний каталог останется без хозяина (userdel -r удалит и его). Лучше сначала поживите с отключенной.')}
          </div>
          <FixCmd cmd={del.cmd} />
        </details>
      )}
    </div>
  )
}
