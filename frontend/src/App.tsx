import { useCallback, useEffect, useState } from 'react'
import { ApiError, getToken, me, setToken, type Role } from './api'
import { AuthProvider } from './auth'
import { useI18n, type Lang } from './i18n'
import { BrandLogo, useBranding } from './BrandLogo'
import { AboutModal } from './AboutModal'
import { AlertsModal } from './AlertsModal'
import { ChecksPage } from './ChecksPage'
import { HomePage } from './HomePage'
import { LocationsModal } from './LocationsModal'
import { RetentionModal } from './RetentionModal'
import { BackupModal } from './BackupModal'
import { LoginPage } from './LoginPage'
import { ServersPage } from './ServersPage'
import { DockerPage } from './DockerPage'
import { KuberPage } from './KuberPage'
import { ServicesPage } from './ServicesPage'
import { BackupsPage } from './BackupsPage'
import { PasswordModal } from './PasswordModal'
import { TwoFAModal } from './TwoFAModal'
import { TelegramModal } from './TelegramModal'
import { UsersModal } from './UsersModal'

type Modal =
  | 'password'
  | '2fa'
  | 'telegram'
  | 'about'
  | 'alerts'
  | 'locations'
  | 'retention'
  | 'backup'
  | 'users'
  | null
export type Section = 'home' | 'sites' | 'servers' | 'docker' | 'kuber' | 'services' | 'backups'

// Ширина полосы прокрутки этого браузера: у классической (Windows/Linux) около 15px,
// у оверлейной (macOS, мобильные) — 0. Нужна, чтобы вернуть её ширину отступом на
// время открытой модалки: фон в этот момент перестаёт скроллиться, полоса исчезает и
// содержимое дёргается вбок. Постоянно резервировать место нельзя — тогда справа
// всегда пустая полоса, а до самой полосы приходится тянуться мышью левее края окна.
function useScrollbarWidth(): void {
  useEffect(() => {
    const apply = () => {
      const w = window.innerWidth - document.documentElement.clientWidth
      document.documentElement.style.setProperty('--scrollbar-w', `${Math.max(w, 0)}px`)
    }
    apply()
    window.addEventListener('resize', apply)
    return () => window.removeEventListener('resize', apply)
  }, [])
}

export default function App() {
  useScrollbarWidth()
  const { t, lang, setLang } = useI18n()
  const [authed, setAuthed] = useState<boolean | null>(null)
  const [role, setRole] = useState<Role>('admin')
  // разделы, разрешённые учётке; пустой список = все (см. backend/app/deps.py)
  const [sections, setSections] = useState<string[]>([])
  const [username, setUsername] = useState('')
  // диплинк из алерта/главной: ?check=<id> → «Сайты» и деталь монитора,
  // ?server=<id> → «Серверы», ?docker/?kube/?backup=<id> → соответствующий раздел с
  // раскрытой карточкой этого хоста. Состояния мутабельны — с главной открываем сразу
  // нужный хост (openTarget), а раздел «съедает» id (onConsumed), чтобы не переоткрывать.
  const urlNum = (k: string) => {
    const p = new URLSearchParams(window.location.search).get(k)
    return p ? Number(p) : null
  }
  const [openCheckId, setOpenCheckId] = useState<number | null>(() => urlNum('check'))
  const [openServerId, setOpenServerId] = useState<number | null>(() => urlNum('server'))
  const [openDockerId, setOpenDockerId] = useState<number | null>(() => urlNum('docker'))
  const [openKubeId, setOpenKubeId] = useState<number | null>(() => urlNum('kube'))
  const [openBackupId, setOpenBackupId] = useState<number | null>(() => urlNum('backup'))
  const [openBackupSrvId, setOpenBackupSrvId] = useState<number | null>(() => urlNum('backupsrv'))
  // ?services=<id>[&queues=1] — из алерта по очереди RabbitMQ: открыть ноду и сразу очереди
  const [openServicesId, setOpenServicesId] = useState<number | null>(() => urlNum('services'))
  const [openQueues] = useState<boolean>(
    () => new URLSearchParams(window.location.search).get('queues') === '1',
  )
  // открыть конкретный элемент в разделе (с главной «Требует внимания»/карточек).
  // srv=true для раздела «Бэкапы» → открыть карточку бэкап-СЕРВЕРА, а не клиента.
  const openTarget = useCallback((sec: Section, id?: number, srv?: boolean, detailSec?: string) => {
    if (id != null) {
      // секция ВНУТРИ детали сервера (msec-*): пункт про бэкап должен приземлять сразу
      // в «Бэкап», а не в начало длинной страницы метрик
      setDeepSec(detailSec ?? null)
      if (sec === 'sites') setOpenCheckId(id)
      else if (sec === 'servers') setOpenServerId(id)
      else if (sec === 'docker') setOpenDockerId(id)
      else if (sec === 'kuber') setOpenKubeId(id)
      else if (sec === 'services') setOpenServicesId(id)
      else if (sec === 'backups') {
        if (srv) setOpenBackupSrvId(id)
        else setOpenBackupId(id)
      }
    }
    setSection(sec)
  }, [])
  // раздел дашборда сервера из диплинка (?server=id&sec=mem) — открыть сразу на нём
  const [openServerSec] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get('sec'),
  )
  // секция детали сервера, заданная кликом из «Требует действий» (не из URL)
  const [deepSec, setDeepSec] = useState<string | null>(null)
  const [section, setSection] = useState<Section>(() =>
    openCheckId
      ? 'sites'
      : openServerId
        ? 'servers'
        : openDockerId
          ? 'docker'
          : openKubeId
            ? 'kuber'
            : openBackupId || openBackupSrvId
              ? 'backups'
              : openServicesId
                ? 'services'
                : 'home',
  )
  // раздел виден, если список не задан (значит все) или он в списке
  const canSee = useCallback(
    (sec: Section) => sections.length === 0 || sections.includes(sec),
    [sections],
  )
  const [branding, reloadBranding] = useBranding()
  const [menuOpen, setMenuOpen] = useState(false)
  const [modal, setModal] = useState<Modal>(null)
  const [theme, setThemeState] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('kervax_theme') as 'dark' | 'light') || 'dark',
  )

  const loadMe = useCallback(() => {
    me()
      .then((u) => {
        setRole(u.role)
        setSections(u.sections ?? [])
        setUsername(u.username)
        setAuthed(true)
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) setToken(null)
        setAuthed(false)
      })
  }, [])

  useEffect(() => {
    if (getToken()) loadMe()
    else setAuthed(false)
  }, [loadMe])

  const logout = useCallback(() => {
    setToken(null)
    setAuthed(false)
    setMenuOpen(false)
    setModal(null)
  }, [])

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    setThemeState(next)
    localStorage.setItem('kervax_theme', next)
    if (next === 'light') document.documentElement.setAttribute('data-theme', 'light')
    else document.documentElement.removeAttribute('data-theme')
  }

  // editor правит объекты, но учётки и настройки панели остаются за админом
  return (
    <AuthProvider
      value={{
        role,
        isViewer: role === 'viewer',
        isAdmin: role === 'admin',
        sections,
        username,
      }}
    >
    <div className="app">
      <div className="header">
        {authed && (
          <button
            className="brand brand-link"
            onClick={() => setSection('home')}
            title={t('На главную')}
          >
            <BrandLogo where="header" branding={branding} />
          </button>
        )}
        {authed && (
          <div className="topnav">
            {canSee('sites') && (
              <button
                className={`navlink${section === 'sites' ? ' navlink-active' : ''}`}
                onClick={() => setSection('sites')}
              >
                {t('Сайты')}
              </button>
            )}
            {canSee('servers') && (
              <button
                className={`navlink${section === 'servers' ? ' navlink-active' : ''}`}
                onClick={() => setSection('servers')}
              >
                {t('Серверы')}
              </button>
            )}
            {/* Сайты и Серверы — самостоятельные сущности, дальше идут СРЕЗЫ по тем же
                серверам. Показываем это чертой и капсулой: навигация не меняется (все
                вкладки по-прежнему в один клик), меняется только читаемость структуры. */}
            <span className="nav-sep" aria-hidden="true" />
            <div className="navgroup">
              {canSee('docker') && (
                <button
                  className={`navlink${section === 'docker' ? ' navlink-active' : ''}`}
                  onClick={() => setSection('docker')}
                >
                  {t('Докер')}
                </button>
              )}
              {canSee('kuber') && (
                <button
                  className={`navlink${section === 'kuber' ? ' navlink-active' : ''}`}
                  onClick={() => setSection('kuber')}
                >
                  {t('Кубер')}
                </button>
              )}
              {canSee('services') && (
                <button
                  className={`navlink${section === 'services' ? ' navlink-active' : ''}`}
                  onClick={() => setSection('services')}
                >
                  {t('Сервисы')}
                </button>
              )}
              {canSee('backups') && (
                <button
                  className={`navlink${section === 'backups' ? ' navlink-active' : ''}`}
                  onClick={() => setSection('backups')}
                >
                  {t('Бэкапы')}
                </button>
              )}
            </div>
          </div>
        )}
        <div className="spacer" />
        <button className="ghost" onClick={toggleTheme}>
          {t('Тема')}
        </button>
        <button
          className="ghost"
          onClick={() => setLang((lang === 'ru' ? 'en' : 'ru') as Lang)}
        >
          {lang === 'ru' ? 'EN' : 'RU'}
        </button>
        {authed && (
          <div className="menu-wrap">
            <button
              className="ghost icon-btn"
              onClick={() => setMenuOpen((v) => !v)}
              aria-label={t('Меню')}
            >
              <span className="menu-gear">⚙</span>
            </button>
            {menuOpen && (
              <>
                <div
                  className="menu-catcher"
                  onClick={() => setMenuOpen(false)}
                />
                <div className="menu-pop menu-pop-right">
                  {role !== 'admin' && (
                    <>
                      <div className="menu-role-badge">
                        {t('Только просмотр')}
                      </div>
                      <div className="menu-divider" />
                    </>
                  )}
                  {role === 'admin' && (
                    <>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setModal('alerts')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Алерты')}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setModal('locations')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Локации')}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setModal('retention')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Хранение данных')}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setModal('backup')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Бэкап')}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setModal('users')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Пользователи')}
                      </button>
                      <div className="menu-divider" />
                    </>
                  )}
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('password')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Сменить пароль')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('2fa')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Двухфакторная аутентификация')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('telegram')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Мои алерты в Telegram')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('about')
                      setMenuOpen(false)
                    }}
                  >
                    {t('О панели Kervax')}
                  </button>
                  <div className="menu-divider" />
                  <button className="menu-item menu-item-danger" onClick={logout}>
                    {t('Выйти')}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {authed === null ? (
        <p className="muted">{t('загрузка…')}</p>
      ) : authed ? (
        section === 'home' ? (
          <HomePage onNavigate={setSection} onOpen={openTarget} onUnauthorized={logout} />
        ) : section === 'sites' ? (
          <ChecksPage onUnauthorized={logout} openCheckId={openCheckId} onConsumed={() => setOpenCheckId(null)} />
        ) : section === 'docker' ? (
          <DockerPage onUnauthorized={logout} openHostId={openDockerId} onConsumed={() => setOpenDockerId(null)} />
        ) : section === 'kuber' ? (
          <KuberPage onUnauthorized={logout} openHostId={openKubeId} onConsumed={() => setOpenKubeId(null)} />
        ) : section === 'services' ? (
          <ServicesPage
            onUnauthorized={logout}
            openServerId={openServicesId}
            openQueues={openQueues}
            onConsumed={() => setOpenServicesId(null)}
            onOpenCheck={(id) => openTarget('sites', id)}
          />
        ) : section === 'backups' ? (
          <BackupsPage
            onUnauthorized={logout}
            openHostId={openBackupId}
            openSrvHostId={openBackupSrvId}
            onConsumed={() => setOpenBackupId(null)}
            onSrvConsumed={() => setOpenBackupSrvId(null)}
          />
        ) : (
          <ServersPage
            onUnauthorized={logout}
            openServerId={openServerId}
            openServerSec={deepSec ?? openServerSec}
            onConsumed={() => setOpenServerId(null)}
          />
        )
      ) : (
        <LoginPage onLogin={loadMe} />
      )}

      {modal === 'password' && (
        <PasswordModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'about' && (
        <AboutModal onClose={() => setModal(null)} onBrandingChanged={reloadBranding} />
      )}
      {modal === 'telegram' && (
        <TelegramModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === '2fa' && (
        <TwoFAModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'alerts' && (
        <AlertsModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'locations' && (
        <LocationsModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'retention' && (
        <RetentionModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'backup' && (
        <BackupModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'users' && (
        <UsersModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
    </div>
    </AuthProvider>
  )
}
