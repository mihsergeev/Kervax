import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  backupCredentials,
  vaultList,
  vaultMeta,
  vaultOpened,
  vaultReset,
  vaultSave,
  vaultSetMeta,
  type Server,
  type VaultItemDto,
} from './api'
import { useI18n } from './i18n'
import {
  checkPassword,
  createMeta,
  decryptJson,
  encryptJson,
  plainExport,
  restoreCommands,
  type VaultSecret,
} from './vault'

// Сейф доступов к бэкапам. Пароль вводится один раз за сессию, живёт ТОЛЬКО в памяти
// вкладки (никакого localStorage) и стирается по бездействию: вкладку легко забыть
// открытой, а её содержимое — ключи от всех бэкапов парка.
const IDLE_LOCK_MS = 15 * 60 * 1000

type Row = { item: VaultItemDto; secret?: VaultSecret; err?: string }

// Поле пароля, которое Chrome не автозаполняет. `autocomplete="off"` он для паролей
// игнорирует: видел на странице пароль → считал её формой входа, подставлял туда
// сохранённый пароль сайта, а логин («admin») — в ближайшее текстовое поле, то есть
// в ПОИСК по бэкапам. Лечится двумя приёмами сразу: autocomplete=new-password (это
// «поле для НОВОГО пароля», сохранённые сюда не предлагают) и readOnly до фокуса —
// в readOnly-поля браузер не пишет вовсе.
function VaultPass({ value, onChange, placeholder, name, onEnter, disabled }: {
  value: string
  onChange: (v: string) => void
  placeholder: string
  name: string
  onEnter?: () => void
  disabled?: boolean
}) {
  const [ro, setRo] = useState(true)
  return (
    <input
      className="field-inp"
      type="password"
      name={name}
      autoComplete="new-password"
      readOnly={ro}
      disabled={disabled}
      onFocus={() => setRo(false)}
      placeholder={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => e.key === 'Enter' && onEnter?.()}
    />
  )
}

export function VaultPanel({ servers }: { servers: Server[] }) {
  const { t } = useI18n()
  const [meta, setMeta] = useState<Awaited<ReturnType<typeof vaultMeta>> | null>(null)
  const [pass, setPass] = useState('')
  const [pass2, setPass2] = useState('')
  const [key, setKey] = useState<CryptoKey | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [shown, setShown] = useState<string | null>(null)
  const [forgot, setForgot] = useState(false)
  const idle = useRef<number | null>(null)

  const lock = useCallback(() => {
    setKey(null)
    setPass('')
    setRows((r) => r.map((x) => ({ item: x.item })))
    setShown(null)
  }, [])

  // авто-замок по бездействию: продлеваем на действиях внутри панели
  useEffect(() => {
    if (!key) return
    const arm = () => {
      if (idle.current) window.clearTimeout(idle.current)
      idle.current = window.setTimeout(lock, IDLE_LOCK_MS)
    }
    arm()
    window.addEventListener('click', arm)
    window.addEventListener('keydown', arm)
    return () => {
      window.removeEventListener('click', arm)
      window.removeEventListener('keydown', arm)
      if (idle.current) window.clearTimeout(idle.current)
    }
  }, [key, lock])

  useEffect(() => {
    vaultMeta().then(setMeta).catch(() => setMeta(null))
  }, [])

  async function loadRows(k: CryptoKey) {
    const items = await vaultList()
    const out: Row[] = []
    for (const it of items) {
      try {
        out.push({ item: it, secret: await decryptJson<VaultSecret>(k, it.nonce, it.ciphertext) })
      } catch {
        out.push({ item: it, err: t('не расшифровалось (запись от другого пароля?)') })
      }
    }
    setRows(out)
  }

  async function create() {
    setErr('')
    if (pass.length < 12) return setErr(t('пароль сейфа — минимум 12 символов'))
    if (pass !== pass2) return setErr(t('пароли не совпадают'))
    setBusy(true)
    try {
      const m = await createMeta(pass)
      await vaultSetMeta(m)
      setMeta(m)
      const k = await checkPassword(pass, m)
      setKey(k)
      if (k) await loadRows(k)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // Забытый пароль: расшифровать нечем, поэтому единственный честный путь — стереть
  // и собрать заново с нод. Явно спрашиваем подтверждение: операция необратима.
  async function reset() {
    if (!window.confirm(t('Стереть сейф целиком? Записи будут удалены безвозвратно, доступы придётся собрать с нод заново.'))) return
    setBusy(true)
    setErr('')
    try {
      const r = await vaultReset()
      setMeta({ salt: '', iterations: 0, verifier_nonce: '', verifier: '' })
      setRows([])
      setForgot(false)
      setPass('')
      setMsg(t('сейф стёрт, записей удалено: {n}', { n: r.deleted }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function unlock() {
    setErr('')
    if (!meta) return
    setBusy(true)
    try {
      const k = await checkPassword(pass, meta)
      if (!k) return setErr(t('неверный пароль сейфа'))
      setKey(k)
      await loadRows(k)
      await vaultOpened()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // Сбор доступов: панель спрашивает их у нод (клиент, а если он мёртв — бэкап-сервер),
  // шифруем ЗДЕСЬ и отправляем только шифротекст. Открытым текстом секрет живёт лишь
  // в этой вкладке и только на время сбора.
  async function collect() {
    if (!key) return
    setBusy(true)
    setErr('')
    setMsg('')
    const targets = servers.filter((s) => s.last_report?.backup?.configured)
    const items: Omit<VaultItemDto, 'updated_at'>[] = []
    let failed = 0
    for (const s of targets) {
      try {
        const c = await backupCredentials(s.id)
        const repo = (c.repo_url || '').replace(/\/+$/, '').split('/').pop() || s.name
        const secret: VaultSecret = {
          repo_url: c.repo_url,
          repopass: c.repopass,
          cacert: c.cacert_pem || undefined, // именно СОДЕРЖИМОЕ: путь с чужой ноды бесполезен
          repo_local: c.repo_local || undefined,
          note: c.source ? t('источник: {s}', { s: c.source }) : undefined,
          saved_at: new Date().toISOString(),
        }
        const { nonce, ciphertext } = await encryptJson(key, secret)
        items.push({ repo, server_id: s.id, server_name: s.name, nonce, ciphertext })
      } catch {
        failed++
      }
    }
    try {
      if (items.length) await vaultSave(items)
      await loadRows(key)
      setMsg(
        t('в сейфе: {n}', { n: items.length }) +
          (failed ? ' · ' + t('не отдали доступы: {n}', { n: failed }) : ''),
      )
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function download(text: string, name: string, mime: string) {
    const url = URL.createObjectURL(new Blob([text], { type: mime }))
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
  }

  // Выгрузка «как есть»: один файл, из которого можно восстановиться, ничего больше
  // не открывая — адрес, пароль, сертификат и команды по каждому репозиторию.
  function exportPlain() {
    const ready = rows.filter((r) => r.secret).map((r) => ({
      repo: r.item.repo, server_name: r.item.server_name, secret: r.secret!,
    }))
    if (!ready.length) return
    void vaultOpened(t('выгрузка открытым текстом'))
    download(
      plainExport(ready),
      `kervax-backups-${new Date().toISOString().slice(0, 10)}.txt`,
      'text/plain;charset=utf-8',
    )
  }

  // Резервная копия самого сейфа: тот же шифротекст файлом. Читается только этой
  // панелью и только с vault-паролем — годится для хранения где угодно.
  function exportSealed() {
    download(
      JSON.stringify(
        { kervax_vault: 1, meta, items: rows.map((r) => r.item), exported_at: new Date().toISOString() },
        null, 2,
      ),
      `kervax-vault-sealed-${new Date().toISOString().slice(0, 10)}.json`,
      'application/json',
    )
  }

  if (meta === null) return null

  return (
    <div className="vault-box">
      <div className="backup-manage-head">🔐 {t('Доступы к бэкапам')}</div>
      {!meta.salt ? (
        <>
          <div className="muted small vault-hint">
            {t('Сейф хранит доступы в зашифрованном виде. Пароль задаёте вы, панель его не знает и не хранит — расшифровка идёт в браузере. Забытый пароль восстановить нельзя: сейф придётся собрать заново с нод.')}
          </div>
          <div className="vault-row">
            <VaultPass name="kervax-vault-new" placeholder={t('пароль сейфа')}
              value={pass} onChange={setPass} />
            <VaultPass name="kervax-vault-new2" placeholder={t('ещё раз')}
              value={pass2} onChange={setPass2} />
            <button className="svc-act" disabled={busy} onClick={create}>
              {busy ? '…' : t('создать сейф')}
            </button>
          </div>
        </>
      ) : !key ? (
        <>
          <div className="vault-row">
            <VaultPass name="kervax-vault-unlock" placeholder={t('пароль сейфа')}
              value={pass} onChange={setPass} onEnter={unlock} />
            <button className="svc-act" disabled={busy} onClick={unlock}>
              {busy ? '…' : t('открыть')}
            </button>
            <button className="svc-act svc-act-off" disabled={busy} onClick={() => setForgot(!forgot)}>
              {t('забыли пароль?')}
            </button>
          </div>
          {forgot && (
            <div className="muted small vault-hint">
              {t('Содержимое сейфа не расшифровать без пароля — ни панели, ни кому-либо ещё: ключ выводился только из него. Бэкапы при этом целы: пароли репозиториев лежат на самих нодах (у клиента и на бэкап-сервере), панель их оттуда и брала. Поэтому выход простой — стереть сейф, задать новый пароль и снова нажать «собрать доступы с нод». Потеряете только записи тех репозиториев, чей клиент И бэкап-сервер уже недоступны.')}
              <div className="vault-row">
                <button className="svc-act svc-act-off" disabled={busy} onClick={reset}>
                  {busy ? '…' : t('стереть сейф и задать пароль заново')}
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="vault-row">
            <button className="svc-act" disabled={busy} onClick={collect}>
              {busy ? '…' : t('собрать доступы с нод')}
            </button>
            <button className="svc-act" disabled={busy || rows.length === 0} onClick={exportPlain}>
              {t('скачать доступы')}
            </button>
            <button className="svc-act" disabled={busy || rows.length === 0} onClick={exportSealed}>
              {t('копия сейфа')}
            </button>
            <button className="svc-act svc-act-off" onClick={lock}>{t('закрыть сейф')}</button>
            <span className="muted small">{t('замок через 15 мин бездействия')}</span>
          </div>
          <div className="muted small vault-hint">
            {t('«Скачать доступы» — обычный текстовый файл: адрес, пароль, сертификат и команды по каждому репозиторию, восстанавливаться можно прямо из него. Пароли в нём открытым текстом, так что храните в менеджере паролей. «Копия сейфа» — то же самое, но зашифрованным: читается только этой панелью и только с паролем сейфа.')}
          </div>
          {msg && <div className="muted small">{msg}</div>}
          {rows.length === 0 && (
            <div className="muted small">{t('Сейф пуст — нажмите «собрать доступы с нод».')}</div>
          )}
          {rows.map((r) => (
            <div key={r.item.repo} className="vault-item">
              <div className="vault-item-head">
                <span className="mono">{r.item.repo}</span>
                <span className="muted small">{r.item.server_name}</span>
                {r.err ? (
                  <span className="form-error small">{r.err}</span>
                ) : (
                  <button className="svc-act" onClick={() => {
                    setShown(shown === r.item.repo ? null : r.item.repo)
                    if (shown !== r.item.repo) void vaultOpened(r.item.repo)
                  }}>
                    {shown === r.item.repo ? t('скрыть') : t('показать доступ')}
                  </button>
                )}
              </div>
              {shown === r.item.repo && r.secret && (
                <>
                  <pre className="vault-cmds mono">{restoreCommands(r.secret, r.item.repo)}</pre>
                  <button className="svc-act" onClick={() =>
                    navigator.clipboard.writeText(restoreCommands(r.secret!, r.item.repo))}>
                    {t('скопировать')}
                  </button>
                </>
              )}
            </div>
          ))}
        </>
      )}
      {err && <div className="form-error small">{err}</div>}
    </div>
  )
}
