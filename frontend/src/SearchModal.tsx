import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, search, type SearchItem, type SearchResult } from './api'
import { useI18n } from './i18n'
import type { Section } from './App'

// Поиск по всему парку (backend app/search.py): Ctrl+K или кнопка в шапке. Стрелки выбирают,
// Enter открывает карточку там, где это видно: порт - в "Соединениях", ключ - в "Процессах".

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (section: Section, id: number, sec?: string) => void
}

const RECENT_KEY = 'kervax_search_recent'

function loadRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 6) : []
  } catch {
    return []
  }
}

function saveRecent(q: string) {
  try {
    const next = [q, ...loadRecent().filter((x) => x !== q)].slice(0, 6)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // приватное окно или запрет хранилища: просто без истории
  }
}

export function SearchModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [res, setRes] = useState<SearchResult | null>(null)
  const [err, setErr] = useState('')
  const [cur, setCur] = useState(0)
  const [recent] = useState(loadRecent)
  const listRef = useRef<HTMLDivElement>(null)
  const query = q.trim()
  const long = query.length >= 2

  useEffect(() => {
    if (!long) return
    let alive = true
    const tm = setTimeout(() => {
      search(query)
        .then((r) => {
          if (!alive) return
          setRes(r)
          setErr('')
          setCur(0)
        })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) return onUnauthorized()
          if (alive) setErr(String((e as Error).message || e))
        })
    }, 180)
    return () => {
      alive = false
      clearTimeout(tm)
    }
  }, [query, long, onUnauthorized])

  // ответ на прошлый запрос не показываем: пока печатают, список не прыгает чужими строками
  const shown = long && res && res.q === query ? res : null
  const busy = long && !shown && !err
  const flat = useMemo(() => (shown ? shown.groups.flatMap((g) => g.items.map((it) => ({ g: g.kind, it }))) : []), [shown])

  useEffect(() => {
    listRef.current?.querySelector('.search-item-active')?.scrollIntoView({ block: 'nearest' })
  }, [cur])

  const open = (it: SearchItem) => {
    saveRecent(query)
    onOpen(it.section as Section, it.id, it.sec || undefined)
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setCur((c) => Math.min(c + 1, Math.max(flat.length - 1, 0)))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setCur((c) => Math.max(c - 1, 0))
    } else if (e.key === 'Enter' && flat[cur]) {
      e.preventDefault()
      open(flat[cur].it)
    }
  }

  const kindLabel = (k: string) =>
    ({
      server: t('Серверы'),
      check: t('Мониторы'),
      site: t('Сайты на серверах'),
      container: t('Контейнеры'),
      port: t('Порты'),
      db: t('Базы и сервисы'),
      queue: t('Очереди'),
      pod: t('Кубер'),
      user: t('Пользователи'),
      key: t('SSH-ключи'),
      unit: t('Юниты systemd'),
      cron: t('Кроны'),
      version: t('Версии'),
    })[k] ?? k

  const tagLabel = (it: SearchItem) =>
    ({
      os: t('ОС'),
      kernel: t('ядро'),
      ssh: 'SSH',
      docker: 'Docker',
      compose: 'Compose',
      agent: t('агент'),
      engine: t('СУБД'),
      database: t('база'),
      service: t('сервис'),
      bad: t('есть неготовые'),
    })[it.tag] ?? ''

  const hintLabel = (k: string) =>
    ({
      ip: 'IP',
      hostname: t('имя хоста'),
      group: t('группа'),
      note: t('заметка'),
      path: t('путь'),
      process: t('процесс'),
      ns: 'namespace',
      pod: t('под'),
      fp: t('отпечаток'),
      addr: t('адрес'),
    })[k] ?? k

  const subText = (g: string, it: SearchItem) => {
    const parts = [...it.sub]
    // у пода последним приходит число подов контроллера
    if (g === 'pod' && parts.length === 3) parts[2] = t('подов: {n}', { n: parts[2] })
    const tag = tagLabel(it)
    if (tag) parts.push(tag)
    return parts.join(' · ')
  }

  let idx = -1
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal search-modal" onClick={(e) => e.stopPropagation()}>
        <input
          className="search-input"
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder={t('Сервер, IP, домен, порт, образ, база, ключ...')}
          aria-label={t('Поиск')}
        />
        <div className="search-results" ref={listRef}>
          {err && <p className="form-error search-empty">{err}</p>}
          {!long && (
            <div className="search-empty">
              {recent.length > 0 && (
                <div className="search-recent">
                  {recent.map((r) => (
                    <button key={r} className="ghost search-recent-btn" onClick={() => setQ(r)}>
                      {r}
                    </button>
                  ))}
                </div>
              )}
              <div className="muted small">
                {t(
                  'Ищет по всем серверам сразу: имена, IP, домены и мониторы, контейнеры и образы, порты (5432), базы и очереди, поды, пользователи и SSH-ключи, юниты, кроны, версии ОС, ядра, SSH и Docker.',
                )}
              </div>
            </div>
          )}
          {busy && <div className="search-empty muted small">{t('ищем...')}</div>}
          {shown && shown.groups.length === 0 && (
            <div className="search-empty muted small">{t('Ничего не нашлось')}</div>
          )}
          {shown?.groups.map((g) => (
            <div key={g.kind} className="search-group">
              <div className="search-group-head">
                <span>{kindLabel(g.kind)}</span>
                {g.total > g.items.length && (
                  <span>{t('показано {n} из {m}, уточните запрос', { n: g.items.length, m: g.total })}</span>
                )}
              </div>
              {g.items.map((it) => {
                idx += 1
                const i = idx
                return (
                  <button
                    key={`${g.kind}|${it.id}|${it.title}|${it.sub.join('|')}`}
                    className={`search-item${i === cur ? ' search-item-active' : ''}`}
                    onMouseMove={() => i !== cur && setCur(i)}
                    onClick={() => open(it)}
                  >
                    <span className="search-item-title">
                      <span className="search-item-name">{it.title}</span>
                      {it.hint_k && !it.sub.includes(it.hint_v) && (
                        <span className="search-hint">
                          {hintLabel(it.hint_k)}: {it.hint_v}
                        </span>
                      )}
                    </span>
                    <span className="search-item-sub">{subText(g.kind, it)}</span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div className="search-foot muted small">
          <span>↑↓ {t('выбрать')}</span>
          <span>Enter {t('открыть')}</span>
          <span>Esc {t('закрыть')}</span>
        </div>
      </div>
    </div>
  )
}
