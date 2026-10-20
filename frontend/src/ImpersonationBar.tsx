import { useEffect, useState } from 'react'
import { ROLE_LABEL, scopeText } from './access'
import { useI18n } from './i18n'

// Плашка над шапкой, пока админ смотрит панель глазами другой учетки (backend/app/impersonate.py):
// чья учетка, что ей видно, сколько осталось и "Вернуться к себе". Видна на всех страницах и
// поверх окон: перепутать, под кем сейчас, нельзя.

type Props = {
  username: string
  role: string
  sections: string[]
  serverGroups: string[]
  siteGroups: string[]
  by: string
  expires: string
  onReturn: () => void
}

export function ImpersonationBar({ username, role, sections, serverGroups, siteGroups, by, expires, onReturn }: Props) {
  const { t } = useI18n()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(id)
  }, [])
  const left = Math.max(0, Math.ceil((new Date(expires).getTime() - now) / 60_000))
  const scope = scopeText({ role, sections, server_groups: serverGroups, site_groups: siteGroups }, t)
  return (
    <div className="imp-bar" role="status">
      <div className="imp-bar-text">
        <b>{t('Вы вошли как {user}', { user: username })}</b>
        <span className="imp-bar-scope">
          {' '}({t(ROLE_LABEL[role] ?? role)}, {scope})
        </span>
        <span className="imp-bar-note">
          {t('Только просмотр, изменения отклоняются. Вошел {admin}, осталось {n} мин.', { admin: by, n: left })}
        </span>
      </div>
      <button className="imp-bar-btn" onClick={onReturn}>
        {t('Вернуться к себе')}
      </button>
    </div>
  )
}
