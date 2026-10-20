// Прокси в команде установки агента. Нужен ноде, чей адрес режут на пути к панели: команда
// качает установщик через прокси (curl --proxy) и передает его установщику (--proxy), а тот
// пишет proxy= в конфиг агента. Команду дополняем прямо в браузере: логин и пароль прокси
// не уходят на панель и нигде там не хранятся, они живут только в этой команде и в конфиге
// агента на ноде.

// http/https/socks5 с хостом. Пробелы и одинарная кавычка запрещены: строка идет в команду
// в одинарных кавычках, остальное (в том числе $ и ! в пароле) внутри них безопасно.
export function isProxyUrl(raw: string): boolean {
  return /^(https?|socks5):\/\/[^\s'@/]*@?[^\s'@/]+\/?$/.test(raw.trim())
}

// Команда из панели имеет вид "... curl -fsSL ... install.sh | sh -s -- [--ca-file X] URL TOKEN".
// Прокси встает в первый curl и первым аргументом установщика.
export function withProxy(cmd: string, proxy: string): string {
  const p = proxy.trim()
  if (!isProxyUrl(p)) return cmd
  const q = `'${p}'`
  return cmd.replace('curl -fsSL ', `curl -fsSL --proxy ${q} `).replace('| sh -s -- ', `| sh -s -- --proxy ${q} `)
}
