import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ApiError,
  getAnsibleAccess,
  getAnsibleNodes,
  issueAnsibleToken,
  revokeAnsibleToken,
  type AnsibleAccess,
  type AnsibleNode,
} from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { useI18n } from './i18n'
import { useEscape } from './useEscape'
import { NetTile } from './NetLinks'
import { fmtSetupVersion, sinceShort } from './serverUtils'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpenServer: (id: number) => void
}

// Короткое имя панели для группы kervax_<имя>: kervax.example.org -> example
function panelName(host: string): string {
  const parts = host.split('.').filter((p) => p && p !== 'www' && p !== 'kervax')
  return parts.length > 1 ? parts[0] : host
}

type Gid = 'outdated' | 'docker_sock' | 'proxy_old' | 'restic_old'
type Group = { id: Gid; inv: string; label: string; nav: string; lead: string; cmd: string; level: 1 | 2; has: (n: AnsibleNode) => boolean }

// Группы инвентаря, которые собирает плагин kervax_panels.py, и чем каждую лечить
const GROUPS: Group[] = [
  {
    id: 'outdated',
    inv: 'kervax_outdated',
    label: 'Что обновит плейбук',
    nav: 'Helper-скрипты',
    lead: 'Ноды на связи, где устарел helper или не включен сторож агента. Плейбук ставит только устаревшее, остальное пропускает.',
    cmd: 'ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated',
    level: 1,
    has: (n) => n.rollout,
  },
  {
    id: 'docker_sock',
    inv: 'kervax_docker_sock',
    label: 'Прокси с docker-сокетом',
    nav: 'docker-сокет',
    lead: 'Внешний прокси (caddy, traefik) держит docker-сокет: взлом прокси - root на ноде. Плейбук ставит прокси docker-api только на чтение и переключает на него.',
    cmd: 'ansible-playbook playbooks/caddy_harden.yml -l kervax_docker_sock',
    level: 2,
    has: (n) => n.issues.includes('docker_sock'),
  },
  {
    id: 'proxy_old',
    inv: 'kervax_proxy_old',
    label: 'Старый образ прокси',
    nav: 'Старый прокси',
    lead: 'Образ внешнего прокси старше года. caddy обновляет тот же caddy_harden.yml (роль ставит закрепленную версию), traefik - руками: между версиями у него меняются настройки.',
    cmd: 'ansible-playbook playbooks/caddy_harden.yml -l kervax_proxy_old',
    level: 1,
    has: (n) => n.issues.includes('proxy_old'),
  },
  {
    id: 'restic_old',
    inv: 'kervax_restic_old',
    label: 'Старый restic на сервере бэкапов',
    nav: 'Старый restic',
    lead: 'restic самого сервера бэкапов старше целевой версии. Обычно его обновляет установка helper-скрипта, если скачать не вышло - одной командой.',
    cmd: "ansible kervax_restic_old -b -m command -a '/lib65/kervax/kervax-backupserver-helper restic-update'",
    level: 1,
    has: (n) => n.issues.includes('restic_old'),
  },
]

const SECS: SecDef[] = [
  { id: 'over', label: 'Сводка', group: '' },
  ...GROUPS.map((g) => ({ id: g.id, label: g.nav, group: 'Группы инвентаря' })),
  { id: 'all', label: 'Все ноды', group: 'Группы инвентаря' },
  { id: 'tok', label: 'Токен', group: 'Доступ' },
]

// Окно Ansible как карточка сервера: что ansible получит от панели (группы инвентаря с нодами
// и командами) и токен, по которому плагин инвентаря эти группы спрашивает
export function AnsibleModal({ onClose, onUnauthorized, onOpenServer }: Props) {
  const { t } = useI18n()
  useEscape(onClose)
  const [acc, setAcc] = useState<AnsibleAccess | null>(null)
  const [nodes, setNodes] = useState<AnsibleNode[] | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [sec, setSec] = useState('over')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState('')
  const [tick, setTick] = useState(0)
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    Promise.all([getAnsibleAccess(), getAnsibleNodes()])
      .then(([a, n]) => {
        setAcc(a)
        setNodes(n.nodes)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else setErr(e instanceof Error ? e.message : String(e))
      })
  }, [tick, onUnauthorized])

  async function run(fn: () => Promise<unknown>) {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      setTick((x) => x + 1)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }
  const issue = () => {
    if (acc?.enabled && !window.confirm(t('Выпустить новый токен? Старый сразу перестанет работать.'))) return
    run(async () => {
      const r = await issueAnsibleToken()
      setToken(r.token)
      setCopied('')
    })
  }
  const revoke = () => {
    if (!window.confirm(t('Отключить доступ ansible? Плагин инвентаря перестанет получать ноды этой панели.'))) return
    run(async () => {
      await revokeAnsibleToken()
      setToken(null)
    })
  }
  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const copy = (id: string, text: string) => {
    navigator.clipboard?.writeText(text)
    setCopied(id)
  }
  const cmdBox = (id: string, text: string) => (
    <div className="agent-advice-cmd">
      <pre>{text}</pre>
      <button className="ghost" onClick={() => copy(id, text)}>
        {copied === id ? t('Скопировано') : t('Копировать')}
      </button>
    </div>
  )

  const members = (g: Group) => (nodes ?? []).filter(g.has)
  // устарело, но нода молчит: в kervax_outdated не попадет, пока не вернется
  const waiting = (nodes ?? []).filter((n) => !n.online && (n.outdated.length > 0 || n.watchdog_off))
  const issueN = GROUPS.slice(1).reduce((a, g) => a + members(g).length, 0)
  const enabled = !!acc?.enabled
  const ago = (iso: string | null | undefined) => {
    const a = sinceShort(iso, t)
    return a ? t('{a} назад', { a }) : t('только что')
  }
  const fmt = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '')
  const state = !acc
    ? ''
    : !enabled
      ? t('доступ выключен')
      : acc.used_at
        ? t('ansible спрашивал {a}', { a: ago(acc.used_at) })
        : t('токен выпущен, ansible еще не спрашивал')

  const info: Record<string, SecInfo> = {}
  const side: Record<string, string> = {}
  for (const g of GROUPS) {
    const n = members(g).length
    info[g.id] = { level: n ? g.level : 0, n, line: '' }
    side[g.id] = '0'
  }
  if (nodes) side.all = String(nodes.length)
  if (acc && !enabled) side.tok = t('выкл')

  // что именно в ноде относится к группе: версии helper'ов, прокси, restic
  const what = (g: Group, n: AnsibleNode) => {
    if (g.id === 'outdated')
      return (
        <>
          {n.outdated.map((a) => (
            <span key={a.name} className="acc-tag acc-tag-warn">
              {a.installed
                ? `${a.name} ${fmtSetupVersion(a.installed)} → ${fmtSetupVersion(a.current)}`
                : t('{h}: поставить {b}', { h: a.name, b: fmtSetupVersion(a.current) })}
            </span>
          ))}
          {n.watchdog_off && <span className="acc-tag acc-tag-warn">{t('сторож агента не включен')}</span>}
        </>
      )
    if (g.id === 'docker_sock')
      return n.sock.map((p) => (
        <span key={p.name} className="muted small mono">
          {p.name} ({p.image})
        </span>
      ))
    if (g.id === 'proxy_old')
      return n.proxy_old.map((p) => (
        <span key={p.name} className="muted small mono">
          {p.name}: {p.version || p.image}, {t('сборка {d}', { d: new Date(p.built * 1000).toLocaleDateString([], { month: '2-digit', year: 'numeric' }) })}
        </span>
      ))
    return <span className="muted small mono">restic {n.restic} → {n.restic_target}</span>
  }
  const nodeName = (n: AnsibleNode) => (
    <span className="ans-node">
      <span className={`lk-dot${n.online ? ' lk-dot-ok' : ''}`} />
      <button className="linklike" onClick={() => onOpenServer(n.id)}>
        {n.name}
      </button>
    </span>
  )
  const table = (rows: AnsibleNode[], col: string, cell: (n: AnsibleNode) => ReactNode) => (
    <table className="sec-tbl">
      <thead>
        <tr>
          <th>{t('Нода')}</th>
          <th className="hide-m">{t('Адрес')}</th>
          <th>{col}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((n) => (
          <tr key={n.id} className={n.online ? '' : 'ans-off'}>
            <td>{nodeName(n)}</td>
            <td className="hide-m mono muted">{n.ips[0] ?? ''}</td>
            <td>
              <div className="ans-chips">{cell(n)}</div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
  const offNote = !enabled && acc && (
    <div className="al-note">
      {t('Доступ ansible выключен: ansible этой панели не видит, группы в инвентарь не попадут.')}{' '}
      <button className="linklike" onClick={() => go('tok')}>
        {t('Включить')}
      </button>
    </div>
  )

  const group = GROUPS.find((g) => g.id === sec)
  const snippet = token
    ? `  - name: ${panelName(location.hostname)}\n    url: ${location.origin}\n    token: ${token}\n`
    : ''
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>Ansible</h3>
          {state && <span className={`small ${enabled ? 'muted' : 't-degraded'}`}>{state}</span>}
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={info} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {group && <span className="ans-grp">{group.inv}</span>}
                {cur.id === 'all' && <span className="ans-grp">kervax</span>}
                {t(group ? group.label : cur.label)}
              </h4>
            </div>
            {err && <p className="form-error">{err}</p>}
            {!nodes || !acc ? (
              !err && <p className="muted small">{t('загрузка…')}</p>
            ) : cur.id === 'over' ? (
              <div className="srv-sec">
                <p className="muted small al-lead">
                  {t('Что ansible получит от этой панели: группы нод для плейбуков. Helper-скрипты на нодах ставит человек плейбуком, панель сама root-скрипты не запускает.')}
                </p>
                {offNote}
                <div className="net-tiles srv-tiles">
                  <button className="srv-tile-btn" onClick={() => go('all')} title={t('к разделу')}>
                    <NetTile label={t('Нод в инвентаре')} value={String(nodes.length)} sub={t('группа kervax')} />
                  </button>
                  <button className="srv-tile-btn" onClick={() => go('outdated')} title={t('к разделу')}>
                    <NetTile
                      label={t('Обновит плейбук')}
                      value={String(members(GROUPS[0]).length)}
                      sub="kervax_outdated"
                      tone={members(GROUPS[0]).length ? 'warn' : 'ok'}
                    />
                  </button>
                  <button
                    className="srv-tile-btn"
                    onClick={() => go(GROUPS.slice(1).find((g) => members(g).length)?.id ?? 'docker_sock')}
                    title={t('к разделу')}
                  >
                    <NetTile label={t('В группах проблем')} value={String(issueN)} sub={t('прокси и restic')} tone={issueN ? 'bad' : 'ok'} />
                  </button>
                </div>
                <div className="srv-nav-grp rt-cap">{t('Что запустить')}</div>
                {GROUPS.filter((g) => members(g).length).map((g) => (
                  <div key={g.id} className="al-box">
                    <div className="al-box-head">
                      <span className="ans-grp">{g.inv}</span>
                      <b>{t(g.label)}</b>
                      <button className="ghost small" onClick={() => go(g.id)}>
                        {t('нод: {n}', { n: members(g).length })} ›
                      </button>
                    </div>
                    {cmdBox('o' + g.id, g.cmd)}
                  </div>
                ))}
                {issueN + members(GROUPS[0]).length === 0 && (
                  <div className="form-ok small">{t('Плейбукам нечего делать: helper-скрипты везде свежие, проблем нет.')}</div>
                )}
                {waiting.length > 0 && (
                  <p className="muted small">
                    {t('Не на связи, обновятся, когда вернутся: {list}.', { list: waiting.map((n) => n.name).join(', ') })}
                  </p>
                )}
              </div>
            ) : group ? (
              <div className="srv-sec">
                <p className="muted small al-lead">{t(group.lead)}</p>
                {offNote}
                {cmdBox(group.id, group.cmd)}
                <div className="srv-nav-grp rt-cap">{t('Ноды в группе: {n}', { n: members(group).length })}</div>
                {members(group).length ? (
                  table(members(group), group.id === 'outdated' ? t('Что обновится') : t('Что там'), (n) => what(group, n))
                ) : (
                  <div className="form-ok small">{t('В группе ни одной ноды.')}</div>
                )}
                {group.id === 'outdated' && waiting.length > 0 && (
                  <p className="muted small">
                    {t('Не на связи, в группу не попадут, пока не вернутся: {list}.', { list: waiting.map((n) => n.name).join(', ') })}
                  </p>
                )}
              </div>
            ) : cur.id === 'all' ? (
              <div className="srv-sec">
                <p className="muted small al-lead">
                  {t('Все ноды панели, от которых был хоть один отчет агента. Ansible находит их в своем инвентаре по имени, hostname или адресу.')}
                </p>
                {table(nodes, t('Группы'), (n) => {
                  const gs = GROUPS.filter((g) => g.has(n))
                  return gs.length ? (
                    gs.map((g) => (
                      <span key={g.id} className={`acc-tag ${g.level >= 2 ? 'acc-tag-bad' : 'acc-tag-warn'}`}>
                        {g.inv}
                      </span>
                    ))
                  ) : (
                    <span className="muted small">{n.online ? '-' : t('не на связи')}</span>
                  )
                })}
              </div>
            ) : (
              <div className="srv-sec">
                <p className="muted small al-lead">
                  {t('Плагин инвентаря (inventory_plugins/kervax_panels.py) спрашивает у каждой панели ее ноды по этому токену и собирает группы kervax_*. Тогда одна команда обновляет helper-скрипты на всех панелях сразу.')}
                </p>
                {!enabled ? (
                  <div className="al-note">{t('Ansible не видит ноды этой панели: доступ выключен.')}</div>
                ) : (
                  <div className="ans-kv small">
                    <span className="muted">{t('Токен выпущен')}</span>
                    <span>{fmt(acc.created_at)}</span>
                    <span className="muted">{t('Ansible спрашивал')}</span>
                    <span>
                      {acc.used_at ? (
                        <>
                          {fmt(acc.used_at)} <span className="muted">({ago(acc.used_at)})</span>
                        </>
                      ) : (
                        <span className="t-degraded">{t('еще ни разу')}</span>
                      )}
                    </span>
                  </div>
                )}
                {token && (
                  <>
                    <div className="al-note">
                      {t('Токен показывается один раз. Добавьте эти строки в inventories/kervax_panels.yml, в список panels:')}
                    </div>
                    {cmdBox('tok', snippet)}
                  </>
                )}
                <div className="ans-acts">
                  {enabled && (
                    <button className="ghost danger-btn" onClick={revoke} disabled={busy}>
                      {t('Отключить')}
                    </button>
                  )}
                  <button onClick={issue} disabled={busy}>
                    {busy ? t('…') : enabled ? t('Выпустить новый токен') : t('Выпустить токен')}
                  </button>
                </div>
                <p className="muted small">
                  {t('Токен открывает только список нод: имена, адреса и что на них обновить. Без секретов и без управления. Если плейбук запускали позже, чем здесь написано, у этой панели в kervax_panels.yml старый токен или неверный адрес.')}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
