// Как в форматтере размеров: отображаемый ТБ равен 1024⁴ байт.
const FREE_LIMIT = 2 * 1024 ** 4

export function backupDiskTone(total: number, free: number): 'ok' | 'warn' | 'bad' {
  if (total <= 0 || free >= FREE_LIMIT || free * 10 >= total) return 'ok'
  return free * 20 <= total ? 'bad' : 'warn'
}
