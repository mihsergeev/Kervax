import { tr } from './i18n'
import { shq } from './shell'

// Раскладка та же, что создаёт backupserver-setup.sh.
export const BACKUP_SERVER_ROOT = '/app/rest-server'
export const DEFAULT_BACKUP_DATA = `${BACKUP_SERVER_ROOT}/data`
const STORAGE_CONFIG = `${BACKUP_SERVER_ROOT}/.kervax-storage-mount`
const MANAGED_SUFFIX = '/kervax-rest-server/data'

export function safeBackupDataDir(path: string): boolean {
  if (path === DEFAULT_BACKUP_DATA) return true
  const mount = path.slice(0, -MANAGED_SUFFIX.length)
  return path.endsWith(MANAGED_SUFFIX) && mount.length <= 512
    && /^(\/[A-Za-z0-9._-]+)+$/.test(mount) && !mount.split('/').some((x) => x === '.' || x === '..')
}

// Перед удалением сверяем каталог из отчета с настройкой и меткой реального тома.
// После переноса команда из старой вкладки тоже должна остановиться.
export function backupStorageGuard(path: string): string[] {
  const failure = `printf '%s\\n' ${shq(tr('Хранилище бэкапов изменилось или недоступно. Обновите отчет и проверьте каталог на сервере.'))} >&2`
  if (!safeBackupDataDir(path)) return [failure, 'exit 1']
  const lines = [
    `DATA=${shq(path)}`, `STORAGE=${STORAGE_CONFIG}`,
    'wrong_storage() {', `  ${failure}`, '  exit 1', '}',
    // То же ограничение, что у helper: не выполнять команду поверх подменяемого пути.
    'secure_dir() {',
    '  local dir="$1" owner mode',
    '  while [ "$dir" != / ] && [ -n "$dir" ]; do',
    '    [ -d "$dir" ] && [ ! -L "$dir" ] || return 1',
    '    read -r owner mode < <(stat -c "%u %a" -- "$dir") || return 1',
    '    [ "$owner" = 0 ] && (( (8#$mode & 8#22) == 0 )) || return 1',
    '    dir="${dir%/*}"',
    '  done',
    '}',
    'secure_dir "${STORAGE%/*}" && secure_dir "$DATA" && [ ! -L "$DATA/.htpasswd" ] || wrong_storage',
  ]
  if (path === DEFAULT_BACKUP_DATA) {
    lines.push('[ ! -e "$STORAGE" ] && [ ! -L "$STORAGE" ] || wrong_storage')
  } else {
    lines.push(
      `MOUNT=${shq(path.slice(0, -MANAGED_SUFFIX.length))}`,
      'if [ -L "$STORAGE" ] || [ ! -f "$STORAGE" ] ||',
      '   ! { IFS= read -r configured && IFS= read -r marker; } < "$STORAGE" ||',
      '   [ "$configured" != "$MOUNT" ] || ! [[ "$marker" =~ ^[A-Za-z0-9]{16}$ ]] ||',
      '   ! mountpoint -q -- "$MOUNT" || ! mount_dev="$(stat -c %d -- "$MOUNT")" ||',
      '   ! root_dev="$(stat -c %d /)" || [ "$mount_dev" = "$root_dev" ] ||',
      '   [ ! -f "$DATA/.kervax-volume-$marker" ] || [ -L "$DATA/.kervax-volume-$marker" ]; then',
      '  wrong_storage',
      'fi',
    )
  }
  return lines
}
