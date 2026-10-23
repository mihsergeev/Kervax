import { useState } from 'react'
import { acceptWebfiles, type Server, type WebRoot, type WpEvent } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'

// Раздел "Файлы сайтов" в "Сервисах" ноды (backend app/webfiles.py, helper webfiles-setup):
// веб-корни, которые helper нашел сам, последняя выкладка и признаки - новые php и правки кода не
// через git/деплой. Корень с доменом своего монитора показывает признаки и в карточке сайта. Каталог,
// смонтированный в несколько контейнеров, - одна строка, контейнеры через запятую. Что WordPress
// обновил сам (перевод, плагин, тему, ядро), - серыми строками, не признаком.

const fmtDateTime = (ts: string) =>
  new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

type T = (k: string, p?: Record<string, string | number>) => string

function fileList(files: { p: string }[], t: T): string {
  const names = files.map((f) => f.p)
  return names.slice(0, 5).join(', ') + (names.length > 5 ? ' ' + t('и еще {n}', { n: names.length - 5 }) : '')
}

// чей файл WordPress, как wp_part в backend app/webfiles.py; index.php и wp-*.php в корне - ядро,
// только если корень точно WordPress
function wpPart(p: string, wp: boolean): string | null {
  const m = /(?:^|\/)wp-content\/(plugins|themes)\/([^/]+?)(?:\.php)?(?:\/|$)/.exec(p)
  if (m) return `${m[1].slice(0, -1)}:${m[2]}`
  if (/(?:^|\/)wp-(?:admin|includes)\//.test(p) || (wp && /^(?:index\.php|xmlrpc\.php|wp-(?!config\.php)[^/]+\.php)$/.test(p)))
    return 'core:'
  return null
}

// правка целиком в одном плагине, теме или ядре WordPress - так и назвать; wp - helper следит за
// версиями, значит версия точно не сменилась
export function wpEditHead(paths: string[], wp: boolean, t: T): string {
  const parts = new Set(paths.map((p) => wpPart(p, wp)))
  const [one] = [...parts]
  if (parts.size !== 1 || one == null) return ''
  const [kind, slug] = one.split(':')
  if (kind === 'plugin')
    return wp ? t('файлы плагина {s} изменились без смены версии', { s: slug }) : t('файлы плагина {s} изменились', { s: slug })
  if (kind === 'theme')
    return wp ? t('файлы темы {s} изменились без смены версии', { s: slug }) : t('файлы темы {s} изменились', { s: slug })
  return wp ? t('файлы ядра WordPress изменились без смены версии') : t('файлы ядра WordPress изменились')
}

// правка в рабочей копии git, не закоммиченная: так понятнее, чем "не через деплой"
export function gitDirtyHead(branch: string, t: T): string {
  return branch
    ? t('не закоммиченная правка в рабочей копии git (ветка {b})', { b: branch })
    : t('не закоммиченная правка в рабочей копии git')
}

// что значит признак правки и что с ним делать; btn - как называется кнопка рядом
export function editNote(t: T, btn: string): string {
  return t('файлы поменялись на сервере напрямую: правка руками, админка, плагин или взлом. Если это вы - нажмите "{btn}"', { btn })
}

function wpEventText(e: WpEvent, t: T): string {
  const p = { s: e.slug ?? '', loc: e.loc ?? '', from: e.from ?? '', to: e.to ?? '' }
  if (e.k === 'lang') {
    if (e.part === 'plugin') return t('WordPress сам обновил перевод плагина {s} ({loc})', p)
    if (e.part === 'theme') return t('WordPress сам обновил перевод темы {s} ({loc})', p)
    return t('WordPress сам обновил свой перевод ({loc})', p)
  }
  if (e.k === 'plugin') return t('обновлен плагин {s} {from} -> {to} (из админки или автообновлением)', p)
  if (e.k === 'theme') return t('обновлена тема {s} {from} -> {to} (из админки или автообновлением)', p)
  return t('обновлен WordPress {from} -> {to} (из админки или автообновлением)', p)
}

export function webfilesBad(s: Server): number {
  return (s.webfiles?.roots ?? []).filter((r) => r.php.length > 0 || r.edit.length > 0).length
}

export function WebFiles({ s, onChanged }: { s: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const roots = s.webfiles?.roots ?? []
  const act = async (root: string, key = '') => {
    setBusy(root + key)
    setErr('')
    try {
      await acceptWebfiles(s.id, root, key)
      onChanged()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }
  const sign = (r: WebRoot, key: 'php' | 'edit') => {
    const files = r[key]
    if (!files.length) return null
    const since = files.map((f) => f.since).sort()[0]
    const dirty = key === 'edit' && r.edit.every((f) => f.g === 'dirty')
    const head = key !== 'edit' ? '' : dirty ? gitDirtyHead(r.branch ?? '', t) : wpEditHead(files.map((f) => f.p), !!r.wp, t)
    return (
      <div className="tamper-row">
        <span className={`lk-dot ${key === 'php' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
        <span className="tamper-ico">{key === 'php' ? '🐘' : '✏️'}</span>
        <span className="tamper-txt">
          {key === 'php' ? t('Новые php') : t('Файлы кода изменились не через git/деплой')}
          {head && <span className="small">{head}</span>}
          <span className="muted small">{fileList(files, t)}</span>
          {key === 'edit' && <span className="muted small">{editNote(t, t('это наше'))}</span>}
        </span>
        {since && <span className="muted small tamper-when">{t('с {t}', { t: fmtDateTime(since) })}</span>}
        {!isViewer && (
          <button className="ghost small" disabled={!!busy} onClick={() => act(r.key, key)}>{t('это наше')}</button>
        )}
      </div>
    )
  }
  return (
    <div className="tamper">
      {roots.map((r) => (
        <div key={r.key} className="webfiles-root">
          <div className="webfiles-head">
            <span className="mono">{r.path}</span>
            <span className="muted small">
              {[
                r.where && r.where !== 'host' ? r.where : t('на хосте'),
                (r.pods ?? 0) > 1 ? t('подов: {n}', { n: r.pods ?? 0 }) : '',
                r.domains.length ? r.domains.slice(0, 3).join(', ') : '',
                r.layer
                  ? t('записано в контейнере файлов кода: {n}', { n: r.files })
                  : r.big ? t('файлов кода больше {n}, список неполный', { n: r.files }) : t('файлов кода: {n}', { n: r.files }),
                r.deploy
                  ? r.deploy.git
                    ? t('выкладка git {k} {t}: {n} файлов', { k: r.deploy.git, t: fmtDateTime(r.deploy.at), n: r.deploy.n })
                    : t('выкладка {t}: {n} файлов', { t: fmtDateTime(r.deploy.at), n: r.deploy.n })
                  : '',
                r.site && (r.php.length || r.edit.length) ? t('признаки и в карточке сайта') : '',
              ].filter(Boolean).join(' · ')}
            </span>
          </div>
          {sign(r, 'php')}
          {sign(r, 'edit')}
          {r.same && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">🔁</span>
              <span className="tamper-txt muted small">
                {t('перезаписаны тем же содержимым, что в git: {f}', {
                  f: r.same.files.join(', ') + (r.same.n > r.same.files.length ? ' ' + t('и еще {n}', { n: r.same.n - r.same.files.length }) : ''),
                })}
              </span>
              <span className="muted small tamper-when">{fmtDateTime(r.same.at)}</span>
            </div>
          )}
          {(r.wp_events ?? []).map((e) => (
            <div key={`${e.k}:${e.part ?? ''}:${e.slug ?? ''}:${e.loc ?? ''}`} className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">{e.k === 'lang' ? '🌐' : '⬆️'}</span>
              <span className="tamper-txt muted small">{wpEventText(e, t)}</span>
              <span className="muted small tamper-when">{fmtDateTime(e.at)}</span>
            </div>
          ))}
        </div>
      ))}
      {roots.length === 0 && <div className="muted small">{t('Веб-корней не нашлось.')}</div>}
      {err && <div className="form-error">{err}</div>}
    </div>
  )
}
