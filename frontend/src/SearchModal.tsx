import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, search, type SearchItem, type SearchResult } from './api'
import { useI18n } from './i18n'
import { useEscape } from './useEscape'
import type { Section } from './App'

// Поиск по всему парку (backend app/search.py): Ctrl+K или кнопка в шапке. Стрелки выбирают,
// Enter открывает карточку там, где это видно: порт - в "Соединениях", ключ - в "Процессах".

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (section: Section, id: number, sec?: string) => void
  historyOwner: string | null
}

const recentKey = (owner: string) => `kervax_search_recent:${owner}`

function loadRecent(owner: string | null): string[] {
  if (owner === null) return []
  try {
    const v = JSON.parse(localStorage.getItem(recentKey(owner)) || '[]')
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 6) : []
  } catch {
    return []
  }
}

function saveRecent(owner: string | null, q: string) {
  if (owner === null) return
  try {
    const next = [q, ...loadRecent(owner).filter((x) => x !== q)].slice(0, 6)
    localStorage.setItem(recentKey(owner), JSON.stringify(next))
  } catch {
    // приватное окно или запрет хранилища: просто без истории
  }
}

export function SearchModal({ onClose, onUnauthorized, onOpen, historyOwner }: Props) {
  const { t } = useI18n()
  useEscape(onClose)
  const [q, setQ] = useState('')
  const [res, setRes] = useState<SearchResult | null>(null)
  const [failure, setFailure] = useState<{ query: string; text: string } | null>(null)
  const [cur, setCur] = useState(0)
  const [recent] = useState(() => loadRecent(historyOwner))
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
          setFailure(null)
          setCur(0)
        })
        .catch((e) => {
          if (!alive) return
          if (e instanceof ApiError && e.status === 401) return onUnauthorized()
          setFailure({ query, text: e instanceof Error ? e.message : String(e) })
        })
    }, 180)
    return () => {
      alive = false
      clearTimeout(tm)
    }
  }, [query, long, onUnauthorized])

  // ответ на прошлый запрос не показываем: пока печатают, список не прыгает чужими строками
  const shown = long && res && res.q === query ? res : null
  const err = long && failure?.query === query ? failure.text : ''
  const busy = long && !shown && !err
  const flat = useMemo(() => (shown ? shown.groups.flatMap((g) => g.items.map((it) => ({ g: g.kind, it }))) : []), [shown])

  useEffect(() => {
    listRef.current?.querySelector('.search-item-active')?.scrollIntoView({ block: 'nearest' })
  }, [cur])

  const open = (it: SearchItem) => {
    saveRecent(historyOwner, query)
    // пакет: карточка пакетов (PackagesCard) откроется уже с ним в фильтре
    if (it.sec === 'pkgs') {
      try {
        sessionStorage.setItem('kervax_pkgs_q', JSON.stringify({ id: it.id, q: it.title, ts: Date.now() }))
      } catch {
        // без хранилища карточка просто откроется с пустым фильтром
      }
    }
    onOpen(it.section as Section, it.id, it.sec || undefined)
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
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
      package: t('Пакеты'),
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
      ver: t('версия'),
    })[k] ?? k

  const subText = (g: string, it: SearchItem) => {
    // у пакета: версия, на скольких серверах, первые из них
    if (g === 'package' && it.sub.length >= 2) {
      const [ver, n, ...names] = it.sub
      const more = Number(n) - names.length
      return `${ver} · ${names.join(', ')}${more > 0 ? ' ' + t('и еще {n}', { n: more }) : ''}`
    }
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
      <div className="card modal search-modal" role="dialog" aria-label={t('Поиск')}
        onClick={(e) => e.stopPropagation()}>
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
          {err && <p role="alert" className="form-error search-empty">{err}</p>}
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
