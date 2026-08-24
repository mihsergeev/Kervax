// Скрипт массовой зачистки выведенных из эксплуатации репозиториев. Отдельный модуль
// (без React) — чтобы генератор можно было прогнать тестом: он печатает `rm -rf` по
// боевым данным, и ошибка здесь стоит дороже любой другой в панели.

import { tr } from './i18n'
import { byteUnits } from './units'

export type CleanupRepo = {
  name: string
  snapshots: number
  size_bytes?: number
  last_activity?: number
}

// Имя репозитория попадает в путь `rm -rf`. Панель получает его от агента, но полагаться
// на это нельзя: любое имя со слэшем, пробелом или «..» превратило бы зачистку одной
// репы в снос чужого каталога. Пропускаем только то, из чего вредный путь не собрать.
const SAFE_NAME = /^[A-Za-z0-9._-]+$/

export function safeRepoName(name: string): boolean {
  return SAFE_NAME.test(name) && name !== '.' && name !== '..'
}

function human(n: number): string {
  const [, , mb, gb, tb] = byteUnits()
  const g = n / 1e9
  return g >= 1000 ? `${(g / 1000).toFixed(1)} ${tb}` : g >= 1 ? `${g.toFixed(0)} ${gb}` : `${Math.round(n / 1e6)} ${mb}`
}

function ago(ts?: number): string {
  if (!ts) return tr('никогда')
  const d = Math.round((Date.now() / 1000 - ts) / 86400)
  return d > 0 ? tr('{n} д назад', { n: d }) : tr('сегодня')
}

/** Скрипт удаления. `root` — корень rest-server, `server` — имя ноды (только в шапку). */
export function bulkCleanupScript(repos: CleanupRepo[], root: string, server: string): string {
  const ok = repos.filter((r) => safeRepoName(r.name))
  if (ok.length === 0) return ''
  const total = ok.reduce((a, r) => a + (r.size_bytes || 0), 0)
  const pad = Math.max(...ok.map((r) => r.name.length))
  const list = ok
    .map((r) => `#   ${r.name.padEnd(pad)}  ${String(r.snapshots).padStart(3)} ${tr('снап.')}  ${human(r.size_bytes || 0).padStart(8)}  ${ago(r.last_activity)}`)
    .join('\n')
  return `#!/usr/bin/env bash
# ${tr('Kervax: зачистка репозиториев на {server}.', { server })}
#
# ${tr('БУДЕТ УДАЛЕНО БЕЗВОЗВРАТНО: {n} шт., {size}.', { n: ok.length, size: human(total) })}
${list}
#
# ${tr('Других копий этих данных у панели нет. Сверьте список ВЫШЕ перед запуском.')}
# ${tr('Выполнять от root на бэкап-сервере.')}
set -eu
B=${root}
REPOS="${ok.map((r) => r.name).join(' ')}"
N=${ok.length}

# ${tr('Подтверждение вводом числа: список длинный и к моменту запуска уже уехал за край экрана.')}
printf '${tr('Удалить %s репозиториев БЕЗВОЗВРАТНО? Введите число %s: ')}' "$N" "$N"
read -r ANSWER
[ "$ANSWER" = "$N" ] || { echo "${tr('отменено')}"; exit 1; }

for R in $REPOS; do
  rm -rf "$B/data/$R"
  # ${tr('htpasswd/prune могут отсутствовать (репу заводили руками) — это не ошибка')}
  htpasswd -D "$B/data/.htpasswd" "$R" 2>/dev/null || true
  rm -f "$B/system/scripts/restic-prune-$R.sh" "$B/system/envs/$R.env"
  rm -f "/etc/cron.d/kervax-prune-$R"
  echo "${tr('удалён')}: $R"
done

# ${tr('чтобы панель забыла удалённое сразу, а не через минуту')}
/lib65/kervax/kervax-backupserver-helper refresh
echo "${tr('готово: удалено')} $N"
`
}
