import { useState } from 'react'
import { acceptWebfiles, setWebfilesMode, type Server, type WebFile, type WebRoot, type WpEvent } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'

// Раздел "Файлы сайтов" в "Сервисах" ноды (backend app/webfiles.py, helper webfiles-setup):
// веб-корни, которые helper нашел сам, последняя выкладка и признаки - новые php и правки кода не
// через git/деплой. Корень с доменом своего монитора показывает признаки и в карточке сайта. Каталог,
// смонтированный в несколько контейнеров, - одна строка, контейнеры через запятую. Что WordPress
// обновил сам (перевод, плагин, тему, ядро), и файлы, которые плагин держит сам (drop-in по шаблону,
// конфиг без кода, закрытый .htaccess), - серыми строками, не признаком. По каждому файлу -
// что о нем знает git, кто владелец и когда записан. Где правят руками (рабочая копия на ветке
// разработки или так выбрано), правки разработчика - серой строкой, признаки - только то, что
// похоже на взлом.

const fmtDateTime = (ts: string) =>
  new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

type T = (k: string, p?: Record<string, string | number>) => string

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

// что о всех файлах правки знает git (как edit_head в backend): не как в HEAD, новые, в .gitignore
export function gitHead(kind: string, branch: string, t: T): string {
  if (kind === 'dirty') return gitDirtyHead(branch, t)
  if (kind === 'new') return t('новые файлы, которых нет в git')
  if (kind === 'ignored') return t('изменены файлы вне git (в .gitignore)')
  return ''
}

// заголовок правки: что знает git, иначе чей это файл WordPress
export function editHead(files: WebFile[], wp: boolean, branch: string, t: T): string {
  const kinds = new Set(files.map((f) => f.g ?? ''))
  const [one] = [...kinds]
  return (kinds.size === 1 && gitHead(one, branch, t)) || wpEditHead(files.map((f) => f.p), wp, t)
}

// что за файл: похож на веб-шелл, что о нем знает git, новый или изменен, кто владелец, когда
export function fileNote(f: WebFile, t: T): string {
  const what = f.x
    ? t('похож на веб-шелл')
    : f.g === 'dirty' ? t('отличается от HEAD')
      : f.g === 'new' ? t('новый, git его не знает')
        : f.g === 'ignored' ? (f.k === 'new' ? t('новый, в .gitignore') : t('изменен, в .gitignore'))
          : f.k === 'new' ? t('новый') : f.k === 'chg' ? t('изменен') : ''
  const who = f.o ? (f.w ? t('владелец {o} (веб-сервер)', { o: f.o }) : t('владелец {o}', { o: f.o })) : ''
  const when = f.t ? t('записан {t}', { t: fmtDateTime(new Date(f.t * 1000).toISOString()) }) : ''
  return [what, who, when].filter(Boolean).join(', ')
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

function clearedText(why: string, p: string, t: T, slug = ''): string {
  if (why === 'gone') return t('признак снят сам: {p} удален', { p })
  if (why === 'back') return t('признак снят сам: {p} вернули как было', { p })
  if (why === 'head') return t('признак снят сам: {p} теперь как в git', { p })
  if (why === 'plugin') return t('признак снят сам: {p} записал плагин {s} по своему шаблону', { p, s: slug })
  if (why === 'data') return t('признак снят сам: в {p} нет кода, только настройки плагина', { p })
  if (why === 'deny') return t('признак снят сам: {p} только закрывает доступ', { p })
  return t('признак снят сам: {p} - пакет перевода WordPress', { p })
}

// файлы, которые плагин держит сам (by - вердикты helper'а 0.7): кто и что это
function genText(x: { files: string[]; n: number; by: string[] }, t: T): string {
  const f = sameFiles(x, t)
  const [one] = x.by
  if (x.by.length === 1 && one.startsWith('plugin:'))
    return t('плагин {s} сам записал свой файл по шаблону: {f}', { s: one.slice(7), f })
  if (x.by.length === 1 && one === 'data') return t('плагин записал свои настройки, кода в файле нет: {f}', { f })
  if (x.by.length === 1 && one === 'deny') return t('плагин закрыл доступ к каталогу (.htaccess): {f}', { f })
  return t('файлы, которые плагины пишут сами, не правка: {f}', { f })
}

export function webfilesBad(s: Server): number {
  return (s.webfiles?.roots ?? []).filter((r) => r.php.length > 0 || r.edit.length > 0).length
}

const SHOW = 5

function sameFiles(x: { files: string[]; n: number }, t: T): string {
  return x.files.join(', ') + (x.n > x.files.length ? ' ' + t('и еще {n}', { n: x.n - x.files.length }) : '')
}

function FileRows({ files, t }: { files: WebFile[]; t: T }) {
  return (
    <>
      {files.slice(0, SHOW).map((f) => {
        const note = fileNote(f, t)
        return (
          <span key={f.p} className="muted small">
            <span className="mono">{f.p}</span>{note ? ' - ' + note : ''}
          </span>
        )
      })}
      {files.length > SHOW && <span className="muted small">{t('и еще {n}', { n: files.length - SHOW })}</span>}
    </>
  )
}

export function WebFiles({ s, onChanged }: { s: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const roots = s.webfiles?.roots ?? []
  const run = async (id: string, call: () => Promise<unknown>) => {
    setBusy(id)
    setErr('')
    try {
      await call()
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
    const shell = key === 'php' && files.some((f) => f.x)
    const head = key === 'edit' ? editHead(files, !!r.wp, r.branch ?? '', t) : ''
    return (
      <div className="tamper-row">
        <span className={`lk-dot ${key === 'php' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
        <span className="tamper-ico">{key === 'php' ? '🐘' : '✏️'}</span>
        <span className="tamper-txt">
          {key === 'php' ? (shell ? t('Похоже на веб-шелл') : t('Новые php')) : t('Файлы кода изменились не через git/деплой')}
          {head && <span className="small">{head}</span>}
          <FileRows files={files} t={t} />
          {key === 'edit' && <span className="muted small">{editNote(t, t('это наше'))}</span>}
        </span>
        {since && <span className="muted small tamper-when">{t('с {t}', { t: fmtDateTime(since) })}</span>}
        {!isViewer && (
          <button className="ghost small" disabled={!!busy} onClick={() => run(r.key + key, () => acceptWebfiles(s.id, r.key, key))}>
            {t('это наше')}
          </button>
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
                r.branch ? t('ветка {b}', { b: r.branch }) : '',
                r.layer
                  ? t('записано в контейнере файлов кода: {n}', { n: r.files })
                  : r.big ? t('файлов кода больше {n}, список неполный', { n: r.files }) : t('файлов кода: {n}', { n: r.files }),
                r.deploy
                  ? r.deploy.git
                    ? t('выкладка git {k} {t}, файлов: {n}', { k: r.deploy.git, t: fmtDateTime(r.deploy.at), n: r.deploy.n })
                    : t('выкладка {t}, файлов: {n}', { t: fmtDateTime(r.deploy.at), n: r.deploy.n })
                  : '',
                r.site && (r.php.length || r.edit.length) ? t('признаки и в карточке сайта') : '',
              ].filter(Boolean).join(' · ')}
            </span>
            {!isViewer && (
              <label className="muted small webfiles-mode">
                {t('здесь правят руками')}{' '}
                <select
                  value={r.mode ?? 'auto'}
                  disabled={!!busy}
                  onChange={(e) => run(r.key + 'mode', () => setWebfilesMode(s.id, r.key, e.target.value as 'auto' | 'dev' | 'strict'))}
                >
                  <option value="auto">{r.mode === 'auto' || !r.mode ? (r.dev ? t('сам: да, ветка {b}', { b: r.branch ?? '' }) : t('сам: нет')) : t('сам, по ветке')}</option>
                  <option value="dev">{t('да')}</option>
                  <option value="strict">{t('нет, каждая правка - признак')}</option>
                </select>
              </label>
            )}
          </div>
          {sign(r, 'php')}
          {sign(r, 'edit')}
          {r.dev && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">🧑‍💻</span>
              <span className="tamper-txt muted small">
                {t('здесь правят руками: правки разработчика не признак, следим за php в загрузках, веб-шеллами и файлами, которые записал сам сайт')}
                <FileRows files={r.dev_work ?? []} t={t} />
              </span>
            </div>
          )}
          {r.cleared && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">✅</span>
              <span className="tamper-txt muted small">{clearedText(r.cleared.why, r.cleared.p, t, r.cleared.slug)}</span>
              <span className="muted small tamper-when">{fmtDateTime(r.cleared.at)}</span>
            </div>
          )}
          {r.same && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">🔁</span>
              <span className="tamper-txt muted small">
                {r.same.git === false
                  ? t('перезаписаны тем же содержимым: {f}', { f: sameFiles(r.same, t) })
                  : t('перезаписаны тем же содержимым, что в git: {f}', { f: sameFiles(r.same, t) })}
              </span>
              <span className="muted small tamper-when">{fmtDateTime(r.same.at)}</span>
            </div>
          )}
          {r.gen && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-idle" />
              <span className="tamper-ico">🧩</span>
              <span className="tamper-txt muted small">{genText(r.gen, t)}</span>
              <span className="muted small tamper-when">{fmtDateTime(r.gen.at)}</span>
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
