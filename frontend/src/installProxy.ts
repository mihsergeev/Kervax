// Прокси в команде установки агента. Нужен ноде, чей адрес режут на пути к панели: команда
// качает установщик через прокси, а установщик пишет proxy= в конфиг агента. Команду дополняем
// прямо в браузере: логин и пароль прокси не уходят на панель и нигде там не хранятся, они живут
// только в этой команде и в конфиге агента на ноде.

// http/https/socks5 с хостом. Пробелы и одинарная кавычка запрещены: строка идет в команду
// в одинарных кавычках, остальное (в том числе $ и ! в пароле) внутри них безопасно.
export function isProxyUrl(raw: string): boolean {
  return /^(https?|socks5):\/\/[^\s'@/]*@?[^\s'@/]+\/?$/.test(raw.trim())
}

// Команда из панели имеет вид "[echo <корень> | base64 -d > X && ]sh -c '<повторы>' - [--ca-file X] URL TOKEN".
// Прокси встает переменной KERVAX_PROXY перед sh -c: ее берут curl в повторах и install.sh. Так
// пароль прокси не попадает в аргументы процессов (их видит ps любого пользователя ноды).
export function withProxy(cmd: string, proxy: string): string {
  const p = proxy.trim()
  if (!isProxyUrl(p)) return cmd
  return cmd.replace("sh -c '", `KERVAX_PROXY='${p}' sh -c '`)
}

// Путь через GRE: команда сначала поднимает туннель (начало от панели, app/gre.install_cmd), потом
// идет в панель на ее адрес в туннеле - переменной KERVAX_CONNECT перед sh -c, как прокси: ее берут
// curl в повторах (--resolve) и install.sh, который пишет агенту connect=
export function withGre(cmd: string, gre: { cmd: string; connect: string } | null): string {
  if (!gre || !gre.cmd || !/^[0-9.]+$/.test(gre.connect)) return cmd
  return `${gre.cmd} && ${cmd.replace("sh -c '", `KERVAX_CONNECT=${gre.connect} sh -c '`)}`
}

// Что команда печатает, пока ждет панель: бэкенд пишет по-русски (app/api/servers.py,
// INSTALL_WAIT_MSG и INSTALL_GIVEUP_MSG, app/gre.py INSTALL_*_MSG), английский интерфейс
// подставляет свой перевод
export const INSTALL_MSGS = [
  'Панель пока закрыта для этого сервера, ждем 10 с (curl $?, попытка $i/24)',
  'Панель так и не ответила: проверьте IP сервера в ее настройках и исходящий 443 (или прокси)',
  'Для GRE нужна команда ip (пакет iproute2)',
  'GRE: к хосту панели сервер ходит с адреса $l, а в панели его IP $3. За NAT GRE не работает, другой адрес - укажите его в окне установки',
  'GRE: интерфейс kervax-gre не поднялся, нет модуля ядра ip_gre?',
]

export function localizeInstallCmd(cmd: string, t: (s: string) => string): string {
  return INSTALL_MSGS.reduce((c, m) => c.replace(m, t(m)), cmd)
}
