// Крипто сейфа доступов — ЦЕЛИКОМ в браузере.
//
// Панель хранит только шифротекст: vault-пароль на сервер не уходит никогда, ключ
// живёт в памяти вкладки и стирается по бездействию. Поэтому дамп базы (и её бэкап)
// без пароля бесполезен — свойство «в БД нет секретов» сохраняется.
//
// Всё на нативном WebCrypto, без внешних библиотек: PBKDF2-SHA256 для вывода ключа
// (600 000 итераций — текущая рекомендация OWASP для PBKDF2-HMAC-SHA256) и
// AES-256-GCM для шифрования. Argon2 был бы лучше по стойкости к перебору на GPU,
// но тянуть WASM-зависимость в панель ради этого не стали.

import { tr } from './i18n'

const ITERATIONS = 600_000
const VERIFIER_TEXT = 'kervax-vault-v1' // расшифровался — пароль верный

export type VaultMeta = {
  salt: string
  iterations: number
  verifier_nonce: string
  verifier: string
}

// содержимое одной записи сейфа: всё, что нужно, чтобы достать бэкап
export type VaultSecret = {
  repo_url: string // rest:https://host:64101/имя
  repopass: string // пароль репозитория restic
  cacert?: string // PEM самоподписанного серта бэкап-сервера (для https)
  repo_local?: string // путь репо НА бэкап-сервере: восстановление вообще без сети
  note?: string
  saved_at: string
}

const enc = new TextEncoder()
const dec = new TextDecoder()

function b64(buf: ArrayBuffer | Uint8Array): string {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let s = ''
  for (const x of b) s += String.fromCharCode(x)
  return btoa(s)
}

// отдаём ArrayBuffer, а не вьюху: WebCrypto ждёт BufferSource, и на вьюхе
// TS 5.7 ругается несовместимостью ArrayBufferLike
function unb64(s: string): ArrayBuffer {
  const raw = atob(s)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out.buffer
}

export function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n)
  crypto.getRandomValues(b)
  return b
}

// пароль → ключ. Соль общая для сейфа (лежит в мете), поэтому вывод ключа делается
// ОДИН раз за сессию, а не на каждую запись: PBKDF2 с 600k итераций не бесплатен.
export async function deriveKey(password: string, saltB64: string, iterations = ITERATIONS) {
  const base = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, [
    'deriveKey',
  ])
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: unb64(saltB64), iterations, hash: 'SHA-256' },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  )
}

export async function encryptJson(key: CryptoKey, data: unknown) {
  const nonce = randomBytes(12) // GCM: 96 бит, свой на КАЖДУЮ запись
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce.buffer as ArrayBuffer },
    key,
    enc.encode(JSON.stringify(data)),
  )
  return { nonce: b64(nonce), ciphertext: b64(ct) }
}

export async function decryptJson<T>(key: CryptoKey, nonce: string, ciphertext: string): Promise<T> {
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: unb64(nonce) },
    key,
    unb64(ciphertext),
  )
  return JSON.parse(dec.decode(pt)) as T
}

// Мета нового сейфа: соль + verifier. Verifier — шифротекст известной строки; по нему
// клиент отличает «неверный пароль» от «повреждённые данные», не храня сам пароль.
export async function createMeta(password: string): Promise<VaultMeta> {
  const salt = b64(randomBytes(16))
  const key = await deriveKey(password, salt)
  const { nonce, ciphertext } = await encryptJson(key, VERIFIER_TEXT)
  return { salt, iterations: ITERATIONS, verifier_nonce: nonce, verifier: ciphertext }
}

export async function checkPassword(password: string, meta: VaultMeta): Promise<CryptoKey | null> {
  if (!meta.salt || !meta.verifier) return null
  const key = await deriveKey(password, meta.salt, meta.iterations || ITERATIONS)
  try {
    const v = await decryptJson<string>(key, meta.verifier_nonce, meta.verifier)
    return v === VERIFIER_TEXT ? key : null
  } catch {
    return null // GCM не сошёлся — пароль неверный
  }
}

// Готовые команды восстановления. Смысл сейфа не в том, чтобы показать пароль, а в
// том, чтобы из него можно было СРАЗУ достать данные — поэтому отдаём копипасту.
export function restoreCommands(s: VaultSecret, repo: string): string {
  const https = s.repo_url.startsWith('rest:https://')
  const ca = https && s.cacert ? ` --cacert /tmp/${repo}-ca.pem` : ''
  const lines = [
    `export RESTIC_REPOSITORY='${s.repo_url}'`,
    `export RESTIC_PASSWORD='${s.repopass}'`,
  ]
  if (https && s.cacert) {
    lines.push(`# ${tr('сертификат бэкап-сервера (самоподписанный) — создаём файл прямо здесь:')}`)
    lines.push(`cat > /tmp/${repo}-ca.pem <<'PEM'`, s.cacert.trim(), 'PEM')
  } else if (https) {
    // честно говорим, что блок неполный: без серта restic к https-репо не подключится
    lines.push(`# ${tr('сертификат бэкап-сервера получить не удалось — возьмите /app/rest-server-tls/cert.pem')}`)
    lines.push(`# ${tr('с бэкап-сервера и добавьте: restic --cacert <файл> …')}`)
  }
  lines.push(
    `restic${ca} snapshots`,
    `restic${ca} mount /mnt/restore        # ${tr('смотреть бэкап как файловую систему')}`,
    `restic${ca} restore latest --target /tmp/restore --include /etc`,
  )
  // Сертификат — не секрет и не обязателен: без него репозиторий тоже открывается.
  // Расписываем варианты, иначе выгрузка выглядит так, будто потеря серта фатальна.
  if (https) {
    const hostPort = s.repo_url.replace('rest:https://', '').split('/')[0]
    const overHttp = s.repo_url.replace('rest:https://', 'rest:http://').replace(/:\d+\//, ':64100/')
    lines.push(
      '',
      `# ${tr('сертификата под рукой нет? подойдёт любой из вариантов:')}`,
      `# ${tr('1) снять его с сервера — он отдаёт сертификат всем, кто подключается:')}`,
      `#    openssl s_client -showcerts -connect ${hostPort} </dev/null 2>/dev/null | openssl x509 > /tmp/${repo}-ca.pem`,
      `# ${tr('2) без проверки подлинности сервера (канал шифруется, подмену сервера не поймать):')}`,
      '#    restic --insecure-tls snapshots',
      `# ${tr('3) по http, если порт открыт:')} RESTIC_REPOSITORY='${overHttp}'`,
    )
  }
  if (s.repo_local) {
    lines.push(
      `# ${tr('4) прямо на бэкап-сервере, вообще без сети и TLS (от root, тем же паролем):')}`,
      `#    restic -r ${s.repo_local} snapshots`,
    )
  }
  return lines.join('\n')
}

// Человекочитаемая выгрузка: файл, из которого можно восстановиться, ничего больше
// не открывая. Сертификат вкладываем прямо в команды (heredoc), чтобы не таскать
// отдельные файлы — один документ самодостаточен.
const NEWLINE = String.fromCharCode(10)

export function plainExport(
  rows: { repo: string; server_name: string; secret: VaultSecret }[],
  when = new Date(),
): string {
  const head = [
    tr('Kervax — доступы к бэкапам'),
    tr('выгружено: {when} UTC · репозиториев: {n}',
       { when: when.toISOString().slice(0, 16).replace('T', ' '), n: rows.length }),
    '',
    tr('ВНИМАНИЕ: здесь пароли от бэкапов ОТКРЫТЫМ ТЕКСТОМ. Это ключи к данным:'),
    tr('храните файл в менеджере паролей или на шифрованном томе, не оставляйте на диске.'),
    tr('Порядок восстановления: поставьте restic, вставьте блок нужного репозитория,'),
    tr('дальше `restic snapshots` покажет копии, `restic mount` откроет их как папку.'),
  ]
  const body = rows.map((r, i) => {
    const https = r.secret.repo_url.startsWith('rest:https://')
    return [
      '',
      '='.repeat(78),
      `[${i + 1}/${rows.length}] ${r.repo}${r.server_name ? `   (${tr('нода')}: ${r.server_name})` : ''}`,
      '='.repeat(78),
      `${tr('репозиторий').padEnd(12)}: ${r.secret.repo_url}`,
      `${tr('пароль').padEnd(12)}: ${r.secret.repopass}`,
      `${tr('сертификат').padEnd(12)}: ${https
        ? (r.secret.cacert ? tr('вложен в команды ниже')
                           : tr('отсутствует — https без сертификата не подключится'))
        : tr('не нужен (http)')}`,
      r.secret.saved_at
        ? `${tr('снято').padEnd(12)}: ${r.secret.saved_at.slice(0, 16).replace('T', ' ')} UTC`
        : '',
      '',
      `--- ${tr('восстановление')} ---`,
      restoreCommands(r.secret, r.repo),
    ].filter(Boolean).join(NEWLINE)
  })
  return head.concat(body).join(NEWLINE) + NEWLINE
}
