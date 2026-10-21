import { useState } from 'react'
import { acceptTamper, type Check, type TamperSign } from './api'
import { useI18n } from './i18n'
import { fmtBytes } from './units'

// Раздел карточки сайта "Признаки взлома" (backend app/tamper.py): что именно не так, с какого
// времени и кнопка принять в базу. Сильные признаки красным, слабые желтым; "Разобрались" -
// все текущее как норма. Признаки, у которых причина ушла, пропадают сами.

const ICON: Record<string, string> = { empty: '📄', bot: '🤖', title: '📝', dom: '🌐', old: '🧩' }

type T = (k: string, p?: Record<string, string | number>) => string

const fmtDateTime = (ts: string) =>
  new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

const size = (n?: number | null) => (n == null ? '?' : fmtBytes(n))

function signText(s: TamperSign, t: T): [string, string] {
  if (s.key === 'empty')
    return [t('Главная опустела'), subOf(
      t('{size} вместо {was}', { size: size(s.size), was: size(Number(s.was)) }),
      s.title ? t('title был "{title}"', { title: s.title }) : '',
    )]
  if (s.key === 'title') return [t('Сменился title'), t('было "{was}", стало "{title}"', { was: String(s.was ?? ''), title: s.title ?? '' })]
  if (s.key === 'bot')
    return [t('Googlebot видит другую страницу'), t('у бота "{title}", {size}; у людей {human}', {
      title: s.title ?? '', size: size(s.size), human: size(s.human),
    })]
  if (s.key === 'dom') return [t('Новый внешний домен {d}', { d: s.domain ?? '' }), whereOf(s.src ?? '', t)]
  if (s.eol) return [t('{what} без поддержки', { what: s.what ?? '' }), t('ветка больше не получает исправлений')]
  return [t('{what} с дырой', { what: s.what ?? '' }), s.fix ? t('исправлено в {v}', { v: s.fix }) : '']
}

function subOf(...parts: string[]): string {
  return parts.filter(Boolean).join(', ')
}

function whereOf(src: string, t: T): string {
  if (src === 'script' || src === 'iframe') return t('{tag} на странице', { tag: src })
  if (src === 'inline') return t('в скрипте на странице')
  return src ? t('в {file}', { file: src }) : ''
}

function actOf(key: string, t: T): string {
  if (key === 'dom') return t('это наше')
  if (key === 'bot') return t('это норм')
  if (key === 'old') return t('отложить 7 дн')
  return t('новая норма')
}

export function TamperSigns({ check, canEdit, onChanged }: { check: Check; canEdit: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const v = check.tamper
  const act = async (key: string, arg = '') => {
    setBusy(key + arg)
    setErr('')
    try {
      await acceptTamper(check.id, key, arg)
      onChanged()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }
  if (!v) return <div className="muted small">{t('Еще не разбирали: главную открываем раз в час, когда сайт отвечает.')}</div>
  const n = v.signs.length
  const eng = subOf(
    v.engine.wp ? `WordPress ${v.engine.wp}` : v.engine.gen ?? '',
    v.engine.php ? `PHP ${v.engine.php}` : '',
  )
  return (
    <div className="tamper">
      {v.level === 'hack' && (
        <div className="tamper-banner tamper-banner-bad">
          <span className="tamper-ico">🕵️</span>
          <span className="tamper-txt">
            <b>{t('Похоже на взлом')}</b>
            <span className="muted small">{t('Сильные признаки пропадут сами, когда сайт вылечат. "Разобрались" принимает текущее как норму.')}</span>
          </span>
          {canEdit && (
            <button disabled={!!busy} onClick={() => act('all')}>{t('Разобрались')}</button>
          )}
        </div>
      )}
      {v.level === 'one' && (
        <div className="tamper-banner tamper-banner-warn">
          <span className="tamper-ico">🕵️</span>
          <span className="tamper-txt">
            <b>{t('Один признак')}</b>
            <span className="muted small">{t('Сам по себе не взлом, но надо глянуть.')}</span>
          </span>
        </div>
      )}
      {v.signs.map((s) => {
        const [what, sub] = signText(s, t)
        return (
          <div key={s.key + s.arg + (s.what ?? '')} className="tamper-row">
            <span className={`lk-dot ${s.strong ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
            <span className="tamper-ico">{ICON[s.key] ?? '⚠️'}</span>
            <span className="tamper-txt">
              {what}
              {sub && <span className="muted small mono-ish">{sub}</span>}
            </span>
            {s.since && <span className="muted small tamper-when">{t('с {t}', { t: fmtDateTime(s.since) })}</span>}
            {canEdit && (
              <button className="ghost small" disabled={!!busy} onClick={() => act(s.key, s.arg)}>{actOf(s.key, t)}</button>
            )}
          </div>
        )
      })}
      {n === 0 && <div className="small">{t('Признаков нет.')}</div>}
      <div className="muted small tamper-base">
        {subOf(
          v.base.title ? t('главная: "{title}", {size}', { title: v.base.title, size: size(v.size ?? v.base.size) }) : '',
          v.domains.length ? t('внешние домены: {list}', { list: v.domains.join(', ') }) : t('внешних доменов нет'),
          eng ? t('видно снаружи: {eng}', { eng }) : '',
          v.last_at ? t('разбор {t}', { t: fmtDateTime(v.last_at) }) : '',
        )}
      </div>
      {v.err && <div className="muted small">{t('Последний разбор не удался: {err}', { err: v.err })}</div>}
      {err && <div className="form-error">{err}</div>}
    </div>
  )
}
