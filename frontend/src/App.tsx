import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, IMP_ADMIN_KEY, getPanelErrorCount, getToken, impersonating, me, setReadOnly, setToken, stopImpersonation, type Role } from './api'
import { AuthProvider } from './auth'
import { useI18n, type Lang } from './i18n'
import { useUrlSection } from './deeplink'
import { BrandLogo, useBranding } from './BrandLogo'
import { AboutModal } from './AboutModal'
import { AccountModal } from './AccountModal'
import { AlertsModal } from './AlertsModal'
import { MaintModal } from './MaintModal'
import { AlertHistoryModal } from './AlertHistoryModal'
import { ReportModal } from './ReportModal'
import { SearchModal } from './SearchModal'
import { DepMapModal } from './DepMapModal'
import { VulnsModal } from './VulnsModal'
import { ChecksPage } from './ChecksPage'
import { HomePage } from './HomePage'
import { LocationsModal } from './LocationsModal'
import { RetentionModal } from './RetentionModal'
import { AnsibleModal } from './AnsibleModal'
import { BackupModal } from './BackupModal'
import { LoginPage } from './LoginPage'
import { ServersPage } from './ServersPage'
import { DockerPage } from './DockerPage'
import { KuberPage } from './KuberPage'
import { ServicesPage } from './ServicesPage'
import { BackupsPage } from './BackupsPage'
import { UsersModal } from './UsersModal'
import { ErrorBoundary } from './ErrorBoundary'
import { ImpersonationBar } from './ImpersonationBar'
import { ProblemModal } from './ProblemModal'
import { PanelErrorsModal } from './PanelErrorsModal'
import { setUiSection } from './errorLog'

type Modal =
  | 'account'
  | 'about'
  | 'alerts'
  | 'locations'
  | 'retention'
  | 'ansible'
  | 'backup'
  | 'users'
  | 'history'
  | 'report'
  | 'maint'
  | 'search'
  | 'depmap'
  | 'vulns'
  | 'problem'
  | 'errors'
  | null
export type Section = 'home' | 'sites' | 'servers' | 'docker' | 'kuber' | 'services' | 'backups'
const SECTIONS: Section[] = ['home', 'sites', 'servers', 'docker', 'kuber', 'services', 'backups']

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
  const [logsOpen, setLogsOpen] = useState(false) // галка "может читать логи" учетки только для просмотра
  const [username, setUsername] = useState('')
  // админ вошел под этой учеткой: кто, до какого времени и что ей видно (плашка сверху)
  const [imp, setImp] = useState<{ by: string; expires: string; serverGroups: string[]; siteGroups: string[] } | null>(null)
  // диплинк из алерта/главной: ?check=<id> → «Сайты» и деталь монитора,
  // ?server=<id> → «Серверы», ?docker/?kube/?backup=<id> → соответствующий раздел с
  // раскрытой карточкой этого хоста. Состояния мутабельны — с главной открываем сразу
  // нужный хост (openTarget), а раздел «съедает» id (onConsumed), чтобы не переоткрывать.
  const urlNum = (k: string) => {
    const p = new URLSearchParams(window.location.search).get(k)
    return p ? Number(p) : null
  }
  const urlSection = new URLSearchParams(window.location.search).get('section')
  const [openCheckId, setOpenCheckId] = useState<number | null>(() => urlNum('check'))
  // ?check=<id>&sec=exp - раздел карточки монитора (из алерта, после F5)
  const [openCheckSec, setOpenCheckSec] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('check') ? p.get('sec') : null
  })
  const [openServerId, setOpenServerId] = useState<number | null>(() => urlNum('server'))
  const [openDockerId, setOpenDockerId] = useState<number | null>(() => urlNum('docker'))
  // ?docker=<id>&sec=host - раздел карточки Docker-ноды (из алерта, с главной, после F5)
  const [openDockerSec, setOpenDockerSec] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('docker') ? p.get('sec') : null
  })
  const [openKubeId, setOpenKubeId] = useState<number | null>(() => urlNum('kube'))
  // ?kube=<id>&ktab=expiry - из алерта про сроки: кластер сразу в разделе "Сроки". ?sec= пишет
  // сама карточка кластера, по нему открываем после F5 и по отправленной ссылке
  const [openKubeTab, setOpenKubeTab] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('ktab') ?? (p.get('kube') ? p.get('sec') : null)
  })
  const [openBackupId, setOpenBackupId] = useState<number | null>(() => urlNum('backup'))
  // ?backup=<id>&sec=coverage - раздел окна бэкапа ноды (из алерта, с главной, после F5)
  const [openBackupSec, setOpenBackupSec] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('backup') ? p.get('sec') : null
  })
  const [openBackupSrvId, setOpenBackupSrvId] = useState<number | null>(() => urlNum('backupsrv'))
  // ?backupsrv=<id>&sec=cleanup - раздел окна сервера бэкапов
  const [openBackupSrvSec, setOpenBackupSrvSec] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('backupsrv') ? p.get('sec') : null
  })
  // ?services=<id>&sec=queues - раздел карточки сервисов ноды (из алерта, после F5). &queues=1 -
  // так вели в очереди старые алерты
  const [openServicesId, setOpenServicesId] = useState<number | null>(() => urlNum('services'))
  const [openServicesSec, setOpenServicesSec] = useState<string | null>(() => {
    const p = new URLSearchParams(window.location.search)
    return p.get('services') ? (p.get('queues') === '1' ? 'queues' : p.get('sec')) : null
  })
  // открыть конкретный элемент в разделе (с главной «Требует внимания»/карточек).
  // srv=true для раздела «Бэкапы» → открыть карточку бэкап-СЕРВЕРА, а не клиента.
  const openTarget = useCallback((sec: Section, id?: number, srv?: boolean, detailSec?: string) => {
    if (id != null) {
      // секция ВНУТРИ детали сервера (msec-*): пункт про бэкап должен приземлять сразу
      // в «Бэкап», а не в начало длинной страницы метрик
      setDeepSec(detailSec ?? null)
      if (sec === 'sites') {
        setOpenCheckId(id)
        setOpenCheckSec(detailSec || null)
      }
      else if (sec === 'servers') setOpenServerId(id)
      else if (sec === 'docker') {
        setOpenDockerId(id)
        setOpenDockerSec(detailSec || null)
      }
      else if (sec === 'kuber') {
        setOpenKubeId(id)
        if (detailSec) setOpenKubeTab(detailSec) // вкладка кластера: "ноды без агента" -> nodes
      }
      else if (sec === 'services') {
        setOpenServicesId(id)
        setOpenServicesSec(detailSec || null)
      }
      else if (sec === 'backups') {
        if (srv) {
          setOpenBackupSrvId(id)
          setOpenBackupSrvSec(detailSec || null)
        } else {
          setOpenBackupId(id)
          setOpenBackupSec(detailSec || null)
        }
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
                : SECTIONS.includes(urlSection as Section)
                  ? (urlSection as Section)
                  : 'home',
  )
  // адрес показывает раздел и открытую карточку, чтобы ссылку можно было отдать другому
  useUrlSection(section)
  // раздел - в журнал ошибок панели вместе с неудачным запросом (errorLog.ts)
  useEffect(() => {
    setUiSection(authed ? section : authed === false ? 'login' : '')
  }, [authed, section])
  // раздел виден, если список не задан (значит все) или он в списке
  const canSee = useCallback(
    (sec: Section) => sections.length === 0 || sections.includes(sec),
    [sections],
  )
  // раздел из адреса может быть закрыт для этой учётки (ссылкой поделились с тем, кому
  // видна только часть панели) - тогда показываем главную
  useEffect(() => {
    if (section !== 'home' && sections.length > 0 && !sections.includes(section)) {
      setSection('home')
    }
  }, [sections, section])
  const [branding, reloadBranding] = useBranding()
  const [menuOpen, setMenuOpen] = useState(false)
  const [modal, setModal] = useState<Modal>(null)
  // Ctrl+K (на любой раскладке - по коду клавиши) или "/" вне поля ввода - поиск по парку
  useEffect(() => {
    if (!authed) return
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const typing = !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
      if ((e.ctrlKey || e.metaKey) && e.code === 'KeyK') {
        e.preventDefault()
        setModal('search')
      } else if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault()
        setModal((m) => m ?? 'search')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [authed])
  // неразобранные ошибки панели: админу числом у шестеренки, без алертов
  const [errOpen, setErrOpen] = useState(0)
  const errCount = authed && role === 'admin' ? errOpen : 0
  const loadErrCount = useCallback(() => {
    getPanelErrorCount()
      .then((c) => setErrOpen(c.open))
      .catch(() => undefined)
  }, [])
  useEffect(() => {
    if (!authed || role !== 'admin') return
    loadErrCount()
    const id = window.setInterval(loadErrCount, 5 * 60_000)
    return () => window.clearInterval(id)
  }, [authed, role, loadErrCount])
  const [theme, setThemeState] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('kervax_theme') as 'dark' | 'light') || 'dark',
  )


  // Вернуться к себе: токен админа на место и страница заново - в памяти страниц не должно
  // остаться ничего, загруженного с правами пользователя. Протухший токен входа роняет в 401
  // сразу несколько запросов: второй вызов не должен принять админа за вышедшего и стереть его токен
  const leaving = useRef(false)
  const returnToSelf = useCallback(() => {
    if (leaving.current) return
    leaving.current = true
    stopImpersonation().finally(() => window.location.assign(window.location.pathname))
  }, [])

  const loadMe = useCallback(() => {
    me()
      .then((u) => {
        setRole(u.role)
        setReadOnly(u.role === 'viewer', !!u.can_read_logs)
        setLogsOpen(!!u.can_read_logs)
        setSections(u.sections ?? [])
        setUsername(u.username)
        setImp(
          u.impersonator && u.imp_expires
            ? { by: u.impersonator, expires: u.imp_expires, serverGroups: u.server_groups ?? [], siteGroups: u.site_groups ?? [] }
            : null,
        )
        setAuthed(true)
      })
      .catch((err) => {
        // вход под чужой учеткой истек или закрыт в другой вкладке - обратно к себе
        if (err instanceof ApiError && err.status === 401 && (impersonating() || leaving.current)) return returnToSelf()
        if (err instanceof ApiError && err.status === 401) setToken(null)
        setAuthed(false)
      })
  }, [returnToSelf])

  // токен общий для всех вкладок: вошли под чужой учеткой или вернулись к себе в одной - другие
  // перезагружаются, иначе показывали бы админа, а спрашивали бы с правами пользователя
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === IMP_ADMIN_KEY) window.location.assign(window.location.pathname)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // по истечении срока входа под чужой учеткой - сразу к себе, не дожидаясь отказа сервера
  useEffect(() => {
    if (!imp) return
    const ms = new Date(imp.expires).getTime() - Date.now()
    const id = window.setTimeout(returnToSelf, Math.max(ms, 0) + 1000)
    return () => window.clearTimeout(id)
  }, [imp, returnToSelf])

  useEffect(() => {
    if (getToken()) loadMe()
    else setAuthed(false)
  }, [loadMe])

  const logout = useCallback(() => {
    // выход под чужой учеткой (и ее протухший токен) закрывает только этот вход
    if (impersonating() || leaving.current) return returnToSelf()
    setToken(null)
    setAuthed(false)
    setMenuOpen(false)
    setModal(null)
  }, [returnToSelf])

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
        canWrite: role !== 'viewer',
        canLogs: role !== 'viewer' || logsOpen,
        isAdmin: role === 'admin',
        sections,
        username,
      }}
    >
    <div className="app">
      {authed && imp && (
        <ImpersonationBar
          username={username}
          role={role}
          sections={sections}
          serverGroups={imp.serverGroups}
          siteGroups={imp.siteGroups}
          by={imp.by}
          expires={imp.expires}
          onReturn={returnToSelf}
        />
      )}
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
        {authed && (
          <button
            className="ghost search-btn"
            onClick={() => setModal('search')}
            title={t('Поиск по всем серверам')}
            aria-label={t('Поиск')}
          >
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" strokeWidth="1.6" />
              <line x1="10.4" y1="10.4" x2="14" y2="14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
            <span className="search-btn-label">{t('Поиск')}</span>
            <kbd>Ctrl K</kbd>
          </button>
        )}
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
              {errCount > 0 && (
                <span className="pe-gear-n" title={t('Неразобранных ошибок панели: {n}', { n: errCount })}>
                  {errCount > 99 ? '99+' : errCount}
                </span>
              )}
            </button>
            {menuOpen && (
              <>
                <div
                  className="menu-catcher"
                  onClick={() => setMenuOpen(false)}
                />
                <div className="menu-pop menu-pop-right">
                  {role === 'viewer' && (
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
                          setModal('ansible')
                          setMenuOpen(false)
                        }}
                      >
                        Ansible
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
                      <button
                        className="menu-item pe-menu-item"
                        onClick={() => {
                          setModal('errors')
                          setMenuOpen(false)
                        }}
                      >
                        {t('Ошибки панели')}
                        {errCount > 0 && <span className="pe-menu-n">{errCount}</span>}
                      </button>
                      <div className="menu-divider" />
                    </>
                  )}
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('maint')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Окна обслуживания')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('report')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Отчет')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('depmap')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Карта зависимостей')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('vulns')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Уязвимости')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('history')
                      setMenuOpen(false)
                    }}
                  >
                    {t('История алертов')}
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('account')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Моя учетка')}
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
                  <button
                    className="menu-item"
                    onClick={() => {
                      setModal('problem')
                      setMenuOpen(false)
                    }}
                  >
                    {t('Сообщить о проблеме')}
                  </button>
                  <div className="menu-divider" />
                  <button className="menu-item menu-item-danger" onClick={logout}>
                    {imp ? t('Вернуться к себе') : t('Выйти')}
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
        <ErrorBoundary resetKey={section} onReport={() => setModal('problem')}>
        {section === 'home' ? (
          <HomePage onNavigate={setSection} onOpen={openTarget} onUnauthorized={logout} />
        ) : section === 'sites' ? (
          <ChecksPage
            onUnauthorized={logout}
            openCheckId={openCheckId}
            openSec={openCheckSec}
            onConsumed={() => {
              setOpenCheckId(null)
              setOpenCheckSec(null)
            }}
          />
        ) : section === 'docker' ? (
          <DockerPage
            onUnauthorized={logout}
            openHostId={openDockerId}
            openSec={openDockerSec}
            onConsumed={() => {
              setOpenDockerId(null)
              setOpenDockerSec(null)
            }}
          />
        ) : section === 'kuber' ? (
          <KuberPage
            onUnauthorized={logout}
            openHostId={openKubeId}
            openTab={openKubeTab}
            onConsumed={() => {
              setOpenKubeId(null)
              setOpenKubeTab(null)
            }}
          />
        ) : section === 'services' ? (
          <ServicesPage
            onUnauthorized={logout}
            openServerId={openServicesId}
            openSec={openServicesSec}
            onConsumed={() => {
              setOpenServicesId(null)
              setOpenServicesSec(null)
            }}
            onOpenCheck={(id) => openTarget('sites', id)}
          />
        ) : section === 'backups' ? (
          <BackupsPage
            onUnauthorized={logout}
            openHostId={openBackupId}
            openSec={openBackupSec}
            openSrvHostId={openBackupSrvId}
            openSrvSec={openBackupSrvSec}
            onConsumed={() => {
              setOpenBackupId(null)
              setOpenBackupSec(null)
            }}
            onSrvConsumed={() => {
              setOpenBackupSrvId(null)
              setOpenBackupSrvSec(null)
            }}
          />
        ) : (
          <ServersPage
            onUnauthorized={logout}
            openServerId={openServerId}
            openServerSec={deepSec ?? openServerSec}
            onConsumed={() => setOpenServerId(null)}
            onOpen={openTarget}
          />
        )}
        </ErrorBoundary>
      ) : (
        <LoginPage onLogin={loadMe} />
      )}

      {modal === 'account' && <AccountModal onClose={() => setModal(null)} onUnauthorized={logout} />}
      {modal === 'about' && (
        <AboutModal onClose={() => setModal(null)} onBrandingChanged={reloadBranding} />
      )}
      {modal === 'history' && (
        <AlertHistoryModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(kind, id) => {
            setModal(null)
            openTarget(kind === 'server' ? 'servers' : 'sites', id)
          }}
        />
      )}
      {modal === 'report' && (
        <ReportModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(kind, id) => {
            setModal(null)
            openTarget(kind === 'server' ? 'servers' : 'sites', id)
          }}
        />
      )}
      {modal === 'depmap' && (
        <DepMapModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(sec, id, sub) => {
            setModal(null)
            openTarget(sec, id, undefined, sub)
          }}
        />
      )}
      {modal === 'vulns' && (
        <VulnsModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(sec, id, sub) => {
            setModal(null)
            openTarget(sec, id, undefined, sub)
          }}
        />
      )}
      {modal === 'search' && (
        <SearchModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(sec, id, detail) => {
            setModal(null)
            openTarget(sec, id, false, detail)
          }}
        />
      )}
      {modal === 'alerts' && (
        <AlertsModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'maint' && (
        <MaintModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpen={(kind, id) => {
            setModal(null)
            openTarget(kind === 'server' ? 'servers' : 'sites', id)
          }}
        />
      )}
      {modal === 'locations' && (
        <LocationsModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpenCheck={(id) => {
            setModal(null)
            openTarget('sites', id)
          }}
        />
      )}
      {modal === 'retention' && (
        <RetentionModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'ansible' && (
        <AnsibleModal
          onClose={() => setModal(null)}
          onUnauthorized={logout}
          onOpenServer={(id) => {
            setModal(null)
            openTarget('servers', id)
          }}
        />
      )}
      {modal === 'backup' && (
        <BackupModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'users' && (
        <UsersModal onClose={() => setModal(null)} onUnauthorized={logout} />
      )}
      {modal === 'problem' && <ProblemModal onClose={() => setModal(null)} onUnauthorized={logout} />}
      {modal === 'errors' && (
        <PanelErrorsModal
          onClose={() => {
            setModal(null)
            loadErrCount()
          }}
          onUnauthorized={logout}
          onChanged={loadErrCount}
          onOpenServer={(id) => {
            setModal(null)
            openTarget('servers', id)
          }}
        />
      )}
    </div>
    </AuthProvider>
  )
}
