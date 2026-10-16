import { useState } from 'react'
import { finishOnboard, type Server } from './api'
import type { Section } from './App'
import { backupAlive, hasOwnFileBackup, shortList, srvIssues } from './serverUtils'

type T = (s: string, p?: Record<string, string | number>) => string

type Row = { key: string; ok: boolean | null; text: string; act?: { label: string; onClick: () => void } }

// что в пункте "Что сломано" главное: до " - " или ": ", без подробностей
const head = (s: string) => s.split(/ - |: /)[0]

// Подключение нового сервера (backend app/onboard.py): что еще не сделано и что нашлось. Пока
// подключение идет, в Telegram уходят только недоступность и критичный диск, остальное копится
// здесь и в "Что сломано"; при закрытии то, что осталось, становится известными проблемами.
export function OnboardBlock({ server: s, t, canManage, onChanged, onOpenSection }: {
  server: Server
  t: T
  canManage: boolean
  onChanged: () => void
  onOpenSection?: (section: Section, srv?: boolean, sec?: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const ob = s.onboard
  if (!ob) return null
  const r = s.last_report ?? {}
  const rows: Row[] = []
  if (!s.first_report_at) {
    rows.push({ key: 'agent', ok: false, text: t('агент еще не отчитался: поставьте его командой, которую панель выдала при добавлении') })
  } else {
    rows.push(s.online
      ? { key: 'agent', ok: true, text: t('агент на связи') }
      : { key: 'agent', ok: false, text: t('агент отчитывался, но сейчас не на связи') })
    const miss = (s.helper_advice ?? []).map((h) => h.label)
    if (r.docker?.present && !r.docker.access) miss.push(t('доступ к Docker'))
    if (r.kube?.present && !r.kube.access) miss.push(t('доступ к Kubernetes'))
    if (s.fresh) rows.push({ key: 'helpers', ok: null, text: t("установщик ставит helper'ы: первые 20 минут после первого отчета") })
    // команда переустановки helper'ов - в плашке под этим блоком; доступ к Docker и Kubernetes
    // включается в их разделах
    else if (miss.length) rows.push({ key: 'helpers', ok: false, text: (s.helper_advice ?? []).length
      ? t('не хватает: {list} - команда ниже', { list: shortList(miss, t, 4) })
      : t('не хватает: {list}', { list: shortList(miss, t, 4) }) })
    else rows.push({ key: 'helpers', ok: true, text: t("helper'ы на месте") })
    const toBackups = onOpenSection ? { label: t('Бэкапы'), onClick: () => onOpenSection('backups', false, 'coverage') } : undefined
    if (r.backup_server?.present) rows.push({ key: 'backup', ok: true, text: t('это сервер бэкапов') })
    else if (backupAlive(r.backup)) rows.push({ key: 'backup', ok: true, text: t('бэкап настроен') })
    else if (hasOwnFileBackup(s)) rows.push({ key: 'backup', ok: true, text: t('свой бэкап, панель за ним следит') })
    else if (s.backup_not_required) rows.push({ key: 'backup', ok: true, text: t('бэкап не требуется (отмечено)') })
    else rows.push({ key: 'backup', ok: false, act: toBackups, text: t('бэкап не настроен: настройте или отметьте, что он не нужен') })
  }
  // куда пойдут алерты: основной чат и чаты групп (имена, без id)
  const where = [
    ...(ob.main ? [t('основной чат')] : []),
    ...(ob.chats ?? []).map((c) => (c ? t('чат {name}', { name: c }) : t('чат группы'))),
  ]
  if (ob.tg === undefined) {
    // список отдал не /api/servers (свежий ответ одного сервера): куда пойдут, не знаем
  } else if (!ob.tg || !where.length) {
    rows.push({ key: 'alerts', ok: false, text: t('алерты в Telegram никуда не уйдут: не настроен чат') })
  } else {
    rows.push({
      key: 'alerts', ok: true,
      text: t('алерты пойдут: {where}', { where: where.join(', ') }) + (s.group_name ? '' : ` (${t('сервер без группы')})`),
    })
  }
  const found = srvIssues(s, t).map((i) => head(i.text))
  const until = new Date(ob.until).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const finish = async () => {
    setBusy(true)
    try {
      await finishOnboard(s.id)
      onChanged()
    } catch {
      // ошибку покажет следующий опрос: подключение останется открытым
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="onb-card">
      <div className="onb-head">
        <b>{t('Подключение сервера')}</b>
        <span className="muted small">{t('закроется само {when}', { when: until })}</span>
      </div>
      <div className="muted small">
        {t('Пока идет подключение, в Telegram уходят только недоступность и критичный диск. Остальное видно здесь и в "Что сломано".')}
      </div>
      <div className="onb-list">
        {rows.map((x) => (
          <div key={x.key} className="onb-row">
            <span className={`lk-dot ${x.ok === true ? 'lk-dot-ok' : x.ok === false ? 'lk-dot-warn' : ''}`} />
            <span className="onb-txt">{x.text}</span>
            {x.act && <button className="ghost small-btn" onClick={x.act.onClick}>{x.act.label} →</button>}
          </div>
        ))}
      </div>
      {found.length > 0 && (
        <div className="onb-found small">
          <b>{t('Нашлось при подключении')}:</b> {shortList(found, t, 4)}
          <div className="muted">
            {t('Когда закроете подключение, это станет известными проблемами: алерт придет, только если станет хуже или сломается новое.')}
          </div>
        </div>
      )}
      {canManage && (
        <div className="onb-foot">
          <button className="small-btn" disabled={busy} onClick={finish}>
            {t('Закрыть подключение')}
          </button>
        </div>
      )}
    </div>
  )
}
