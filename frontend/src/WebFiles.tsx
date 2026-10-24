import { useState } from 'react'
import { acceptWebfiles, type Server, type WebChange, type WebFile, type WebRoot, type WpEvent } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'

// Раздел "Файлы сайтов" в "Сервисах" ноды (backend app/webfiles.py, helper webfiles-setup):
// веб-корни, которые helper нашел сам. Красное - только улики взлома: php похож на веб-шелл, php в
// загрузках, .htaccess запускает php, файл ядра или плагина WordPress не как в выпуске на
// wordpress.org. Все остальное - серые строки о том, что менялось: выкладка (с git и без) и кто
// записал, правки, обновления WordPress, файлы, которые плагин держит сам, перезапись тем же
// содержимым. Корень с доменом своего монитора показывает улики и в карточке сайта. Каталог,
// смонтированный в несколько контейнеров, - одна строка, контейнеры через запятую.

const fmtDateTime = (ts: string) =>
  new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

type T = (k: string, p?: Record<string, string | number>) => string

// почему файл - улика, как WHY в backend
function whyNote(f: WebFile, t: T): string {
  if (f.y === 'shell' || (!f.y && f.x)) return t('похож на веб-шелл')
  if (f.y === 'upload') return t('php в загрузках')
  if (f.y === 'exec') return t('запускает php')
  if (f.y === 'wporg') return t('не как в выпуске {v}', { v: f.ver ?? '' })
  if (f.y === 'wpextra') return t('нет в выпуске {v}', { v: f.ver ?? '' })
  return ''
}

// что за файл: почему улика, новый или изменен, кто владелец, когда
export function fileNote(f: WebFile, t: T): string {
  const what = [whyNote(f, t), f.k === 'new' ? t('новый') : f.k === 'chg' ? t('изменен') : ''].filter(Boolean).join(', ')
  const who = f.o ? (f.w ? t('владелец {o} (веб-сервер)', { o: f.o }) : t('владелец {o}', { o: f.o })) : ''
  const when = f.t ? t('записан {t}', { t: fmtDateTime(new Date(f.t * 1000).toISOString()) }) : ''
  return [what, who, when].filter(Boolean).join(', ')
}

// заголовок улики: у всех файлов одна причина - так и назвать (как php_head в backend)
export function phpTitle(files: WebFile[], t: T): string {
  const ys = new Set(files.map((f) => f.y || (f.x ? 'shell' : 'upload')))
  if (ys.size !== 1) return t('Признаки взлома в файлах')
  const [y] = [...ys]
  if (y === 'shell') return t('Похоже на веб-шелл')
  if (y === 'upload') return t('php в загрузках')
  if (y === 'exec') return t('.htaccess запускает php')
  const refs = new Set(files.map((f) => `${f.ref ?? ''}@${f.ver ?? ''}`))
  const v = files[0].ver ?? ''
  if (y === 'wpextra') return t('Лишний php в ядре WordPress {v}', { v })
  if (refs.size !== 1) return t('Файлы WordPress не как в выпуске')
  const ref = files[0].ref ?? ''
  return ref.startsWith('plugin:')
    ? t('Файлы плагина {s} не как в выпуске {v}', { s: ref.slice(7), v })
    : t('Файлы ядра WordPress не как в выпуске {v}', { v })
}

// что значит улика и что с ней делать
function phpNote(files: WebFile[], t: T): string {
  return files.every((f) => f.y === 'wporg' || f.y === 'wpextra')
    ? t('файл отличается от официального выпуска на wordpress.org: подмена или правка руками. Если правили вы - нажмите "{btn}"', { btn: t('это наше') })
    : t('так попадает внутрь взлом. Если файл ваш - нажмите "{btn}"', { btn: t('это наше') })
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
  if (why === 'wporg') return t('признак снят сам: {p} снова как в выпуске WordPress', { p })
  if (why === 'plugin') return t('признак снят сам: {p} записал плагин {s} по своему шаблону', { p, s: slug })
  if (why === 'data') return t('признак снят сам: в {p} нет кода, только настройки плагина', { p })
  if (why === 'deny') return t('признак снят сам: {p} только закрывает доступ', { p })
  return t('признак снят сам: {p} - пакет перевода WordPress', { p })
}

// файлы, которые плагин держит сам (by - вердикты helper'а): кто и что это
function genText(x: { files: string[]; n: number; by: string[] }, t: T): string {
  const f = sameFiles(x, t)
  const [one] = x.by
  if (x.by.length === 1 && one.startsWith('plugin:'))
    return t('плагин {s} сам записал свой файл по шаблону: {f}', { s: one.slice(7), f })
  if (x.by.length === 1 && one === 'data') return t('плагин записал свои настройки, кода в файле нет: {f}', { f })
  if (x.by.length === 1 && one === 'deny') return t('плагин закрыл доступ к каталогу (.htaccess): {f}', { f })
  return t('файлы, которые плагины пишут сами, не правка: {f}', { f })
}

// выкладка или правки: сколько, кто записал, какие
function changeText(x: WebChange & { git?: string }, deploy: boolean, t: T): string {
  const head = !deploy
    ? t('изменено файлов: {n}', { n: x.n })
    : x.git ? t('выкладка git {k}: файлов {n}', { k: x.git, n: x.n }) : t('выкладка без git: файлов {n}', { n: x.n })
  const who = x.o?.length ? t('записал {o}', { o: x.o.join(', ') }) : ''
  const f = x.files?.length ? sameFiles({ files: x.files, n: x.n }, t) : ''
  return [head, who].filter(Boolean).join(', ') + (f ? ' - ' + f : '')
}

function changeWhen(x: WebChange): string {
  return x.from ? `${fmtDateTime(x.from)} - ${fmtDateTime(x.at).split(', ').pop()}` : fmtDateTime(x.at)
}

export function webfilesBad(s: Server): number {
  return (s.webfiles?.roots ?? []).filter((r) => r.php.length > 0).length
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

function Gray({ ico, text, when }: { ico: string; text: string; when?: string }) {
  return (
    <div className="tamper-row">
      <span className="lk-dot lk-dot-idle" />
      <span className="tamper-ico">{ico}</span>
      <span className="tamper-txt muted small">{text}</span>
      {when && <span className="muted small tamper-when">{when}</span>}
    </div>
  )
}

export function WebFiles({ s, onChanged }: { s: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const roots = s.webfiles?.roots ?? []
  const accept = async (r: WebRoot) => {
    setBusy(r.key)
    setErr('')
    try {
      await acceptWebfiles(s.id, r.key)
      onChanged()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
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
                r.wp ? `WordPress ${r.wp}` : '',
                r.layer
                  ? t('записано в контейнере файлов кода: {n}', { n: r.files })
                  : r.big ? t('файлов кода больше {n}, список неполный', { n: r.files }) : t('файлов кода: {n}', { n: r.files }),
                r.site && r.php.length ? t('признаки и в карточке сайта') : '',
              ].filter(Boolean).join(' · ')}
            </span>
          </div>
          {r.php.length > 0 && (
            <div className="tamper-row">
              <span className="lk-dot lk-dot-bad" />
              <span className="tamper-ico">🚨</span>
              <span className="tamper-txt">
                {phpTitle(r.php, t)}
                <FileRows files={r.php} t={t} />
                <span className="muted small">{phpNote(r.php, t)}</span>
              </span>
              <span className="muted small tamper-when">{t('с {t}', { t: fmtDateTime(r.php.map((f) => f.since).sort()[0]) })}</span>
              {!isViewer && (
                <button className="ghost small" disabled={!!busy} onClick={() => accept(r)}>
                  {t('это наше')}
                </button>
              )}
            </div>
          )}
          {r.deploy && <Gray ico="📦" text={changeText(r.deploy, true, t)} when={changeWhen(r.deploy)} />}
          {r.edits && <Gray ico="✏️" text={changeText(r.edits, false, t)} when={changeWhen(r.edits)} />}
          {(r.checking ?? 0) > 0 && <Gray ico="🔎" text={t('сверяются с выпуском на wordpress.org: файлов {n}', { n: r.checking ?? 0 })} />}
          {r.cleared && <Gray ico="✅" text={clearedText(r.cleared.why, r.cleared.p, t, r.cleared.slug)} when={fmtDateTime(r.cleared.at)} />}
          {r.same && (
            <Gray
              ico="🔁"
              text={r.same.git === false
                ? t('перезаписаны тем же содержимым: {f}', { f: sameFiles(r.same, t) })
                : t('перезаписаны тем же содержимым, что в git: {f}', { f: sameFiles(r.same, t) })}
              when={fmtDateTime(r.same.at)}
            />
          )}
          {r.gen && <Gray ico="🧩" text={genText(r.gen, t)} when={fmtDateTime(r.gen.at)} />}
          {(r.wp_events ?? []).map((e) => (
            <Gray key={`${e.k}:${e.part ?? ''}:${e.slug ?? ''}:${e.loc ?? ''}`} ico={e.k === 'lang' ? '🌐' : '⬆️'}
              text={wpEventText(e, t)} when={fmtDateTime(e.at)} />
          ))}
        </div>
      ))}
      {roots.length === 0 && <div className="muted small">{t('Веб-корней не нашлось.')}</div>}
      {err && <div className="form-error">{err}</div>}
    </div>
  )
}
