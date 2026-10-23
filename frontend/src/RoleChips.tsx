import type { ReactNode } from 'react'
import type { Server } from './api'
import type { TFn } from './mutes'

// Роли сервера рядом с именем в списке и в шапке карточки: бэкап-сервер, ретранслятор (и нода за
// ним), Kubernetes, LLM. Вид как у плашки GPU: рамка и заливка своего спокойного цвета с иконкой.
// Цвета не статусные (зеленый, желтый, красный) и не синий групп, стили .role-* в App.css

const svg = (body: ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    {body}
  </svg>
)

const ICON = {
  // база данных: хранилище бэкапов
  backup: svg(
    <>
      <ellipse cx="12" cy="5.5" rx="8" ry="3" />
      <path d="M4 5.5v13c0 1.66 3.58 3 8 3s8-1.34 8-3v-13" />
      <path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3" />
    </>,
  ),
  // мачта с волнами: прокси для агентов
  relay: svg(
    <>
      <path d="M12 12v9" />
      <circle cx="12" cy="10" r="1.6" />
      <path d="M8.5 13.5a5 5 0 0 1 0-7M15.5 6.5a5 5 0 0 1 0 7" />
      <path d="M5.6 16.4a9 9 0 0 1 0-12.8M18.4 3.6a9 9 0 0 1 0 12.8" />
    </>,
  ),
  // штурвал Kubernetes одним цветом
  kube: svg(
    <>
      <circle cx="12" cy="12" r="7.5" />
      <circle cx="12" cy="12" r="2" />
      <path d="M12 2.5v7.5M12 14v7.5M2.5 12H10M14 12h7.5M5.3 5.3l5.3 5.3M13.4 13.4l5.3 5.3M18.7 5.3l-5.3 5.3M10.6 13.4l-5.3 5.3" />
    </>,
  ),
  // искра: языковая модель
  llm: svg(<path d="M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z" />),
}

function Chip({ role, icon, title, onClick, children }: {
  role: string; icon?: ReactNode; title: string; onClick?: () => void; children: ReactNode
}) {
  return (
    <span
      className={`role-chip role-${role}${onClick ? ' clickable' : ''}`}
      title={title}
      onClick={onClick && ((e) => { e.stopPropagation(); onClick() })}
    >
      {icon}
      {children}
    </span>
  )
}

export function RoleChips({ s, servers, t, onRelay }: {
  s: Server
  servers: Server[] // все серверы: имена нод за ретранслятором
  t: TFn
  onRelay?: () => void // клик по "ретранслятор": к его настройкам и состоянию
}) {
  const out: ReactNode[] = []
  if (s.last_report?.backup_server?.present)
    out.push(<Chip key="backup" role="backup" icon={ICON.backup} title={t('Сервер бэкапов (rest-server)')}>{t('бэкап-сервер')}</Chip>)
  // Ретранслятор Kervax (app/relay.py): у самого - чьи агенты через него ходят (выбрали его в
  // команде установки, relay_via_id), у ноды за ним - через какой
  if (s.relay_enabled) {
    const names = servers.filter((x) => x.relay_via_id === s.id && x.id !== s.id).map((x) => x.name).sort()
    const tip = names.length
      ? t('Ретранслятор Kervax: через него ходят агенты: {list}', { list: names.join(', ') })
      : t('Ретранслятор Kervax, сейчас никто не ходит')
    out.push(
      <Chip key="relay" role="relay" icon={ICON.relay} onClick={onRelay}
        title={onRelay ? `${tip}\n${t('Нажмите: настройки и состояние ретранслятора')}` : tip}>
        {t('ретранслятор')}
      </Chip>,
    )
  } else {
    const via = s.relay_via_id ? servers.find((x) => x.id === s.relay_via_id) : undefined
    if (via)
      out.push(
        <Chip key="via" role="via" icon={ICON.relay} title={t('Агент ходит в панель через ретранслятор {name}', { name: via.name })}>
          {t('через ретранслятор {name}', { name: via.name })}
        </Chip>,
      )
  }
  const kube = s.last_report?.kube
  if (kube?.present)
    out.push(
      <Chip key="kube" role="kube" icon={ICON.kube}
        title={[t('Нода Kubernetes'), kube.flavor, kube.version].filter(Boolean).join(' ')}>
        {kube.flavor || 'k8s'}
      </Chip>,
    )
  if (s.llm?.length)
    out.push(
      <Chip key="llm" role="llm" icon={ICON.llm}
        title={t('Сервисы LLM: {list}', { list: [...new Set(s.llm.map((x) => x.model || x.engine_name || x.name))].join(', ') })}>
        LLM
      </Chip>,
    )
  return <>{out}</>
}
