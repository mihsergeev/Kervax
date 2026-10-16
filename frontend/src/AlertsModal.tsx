import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  getAlerts,
  getServerAlertRules,
  getSiteAlertRules,
  putAlerts,
  putServerAlertRules,
  putSiteAlertRules,
  testAlertRoute,
  testAlerts,
  type AlertConfig,
  type AlertRoute,
  type AlertRoutes,
  type Escalation,
  type ServerAlertRules as Rules,
} from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { AlertRulesList, ScopedNote, scopedKinds, unscopeRules } from './ServerAlertRules'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

// Настройки алертов как карточка сервера: меню разделов слева, на экране один раздел, а
// "Сохранить" всегда внизу окна и сохраняет все сразу - и каналы, и правила серверов и сайтов

// то, что правится в окне и уходит в PUT /alerts
type Form = {
  telegram_token: string
  telegram_chat: string
  telegram_api: string
  webhook: string
  flood_threshold: number
  muted: boolean
  tg_buttons: boolean
  tz: string
  escalation: Escalation
  routes: AlertRoutes
}

const formOf = (c: AlertConfig): Form => ({
  telegram_token: c.telegram_token,
  telegram_chat: c.telegram_chat,
  telegram_api: c.telegram_api,
  webhook: c.webhook,
  flood_threshold: c.flood_threshold,
  muted: c.muted,
  tg_buttons: c.tg_buttons,
  tz: c.tz,
  escalation: c.escalation,
  routes: c.routes,
})

const SECS: SecDef[] = [
  { id: 'where', label: 'Чаты и группы', group: 'Куда слать' },
  { id: 'bot', label: 'Бот и адрес API', group: 'Куда слать' },
  { id: 'hook', label: 'Webhook', group: 'Куда слать' },
  { id: 'buttons', label: 'Кнопки и эскалация', group: 'Как слать' },
  { id: 'flood', label: 'Антифлуд и пауза', group: 'Как слать' },
  { id: 'srv', label: 'Алерты серверов', group: 'Что слать' },
  { id: 'site', label: 'Алерты сайтов', group: 'Что слать' },
]

const LEAD: Record<string, string> = {
  where: 'Куда уходят алерты каждой группы. Основной чат получает все или только то, что не забрали чаты групп.',
  bot: 'Токен выдает @BotFather. Один бот шлет во все чаты: основной, чаты групп и эскалацию.',
  hook: 'Для своих систем: каждый алерт уходит POST-запросом, текст в поле text JSON. Webhook получает все алерты, без деления по группам.',
  buttons: 'Что можно сделать с алертом прямо в Telegram.',
  flood: 'Когда что-то упало массово, и когда алерты нужно на время выключить.',
  srv: 'Какие алерты о серверах слать и каким текстом. Пороги и исключения - в карточке сервера.',
  site: 'Какие алерты о сайтах слать и каким текстом. Пороги и исключения - в карточке монитора.',
}

const key = (x: unknown) => JSON.stringify(x)

export function AlertsModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  // последний конфиг с сервера: то, что не правится (опрос бота, группы, сбои чатов)
  const [cfg, setCfg] = useState<AlertConfig | null>(null)
  const [f, setF] = useState<Form | null>(null)
  const [srv, setSrv] = useState<Rules | null>(null)
  const [site, setSite] = useState<Rules | null>(null)
  // как сейчас сохранено: с этим сравниваем, есть ли несохраненное
  const [saved, setSaved] = useState({ f: '', srv: '', site: '' })
  const [sec, setSec] = useState('where')
  const mainRef = useRef<HTMLDivElement>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  const loaded = useCallback((c: AlertConfig) => {
    setCfg(c)
    setF(formOf(c))
    setSaved((s) => ({ ...s, f: key(formOf(c)) }))
  }, [])

  useEffect(() => {
    getAlerts()
      .then((c) => {
        loaded(c)
        // ничего не настроено - начинаем с бота
        if (!c.telegram_token && !c.webhook) setSec('bot')
      })
      .catch(() => onUnauthorized())
    getServerAlertRules()
      .then((r) => {
        setSrv(r)
        setSaved((s) => ({ ...s, srv: key(r.rules) }))
      })
      .catch(fail)
    getSiteAlertRules()
      .then((r) => {
        setSite(r)
        setSaved((s) => ({ ...s, site: key(r.rules) }))
      })
      .catch(fail)
  }, [onUnauthorized, fail, loaded])

  // новый раздел открывается с начала, а не с места, куда долистали предыдущий
  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo(0, 0)
  }
  const set = (p: Partial<Form>) => {
    setF((x) => (x ? { ...x, ...p } : x))
    setMsg(null)
  }
  const editSrv = (r: Rules) => {
    setSrv(r)
    setMsg(null)
  }
  const editSite = (r: Rules) => {
    setSite(r)
    setMsg(null)
  }

  const dirtyF = f !== null && key(f) !== saved.f
  const dirtySrv = srv !== null && key(srv.rules) !== saved.srv
  const dirtySite = site !== null && key(site.rules) !== saved.site
  const dirty = dirtyF || dirtySrv || dirtySite

  // Сохраняет каналы и возвращает актуальный конфиг
  async function persist(): Promise<AlertConfig> {
    if (!f) throw new Error(t('загрузка…'))
    const c = await putAlerts(f)
    loaded(c)
    return c
  }

  // все сразу: каналы и правила, что из этого менялось
  async function saveAll(): Promise<AlertConfig> {
    const c = dirtyF || !cfg ? await persist() : cfg
    if (srv && dirtySrv) {
      const r = await putServerAlertRules(srv.rules)
      setSrv(r)
      setSaved((s) => ({ ...s, srv: key(r.rules) }))
    }
    if (site && dirtySite) {
      const r = await putSiteAlertRules(site.rules)
      setSite(r)
      setSaved((s) => ({ ...s, site: key(r.rules) }))
    }
    return c
  }

  // проверка чата по группам: сначала сохраняем каналы, чтобы чат и его группы уже были
  async function testRoute(chat: string): Promise<string> {
    try {
      await persist()
      const r = await testAlertRoute(chat)
      return r.sent ? '' : r.errors.join('; ') || t('Не отправлено — проверьте настройки.')
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
      return e instanceof Error ? e.message : t('Ошибка')
    }
  }

  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      await saveAll()
      setMsg(t('Сохранено.'))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  // Тест проверяет то, что на экране: сперва сохраняем, затем шлём тест.
  async function test() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const c = await saveAll()
      if (!c.enabled) {
        setErr(t('Заполните Telegram (токен и Chat ID) или Webhook.'))
        return
      }
      const r = await testAlerts()
      if (r.sent) setMsg(t('Тестовое уведомление отправлено.'))
      else setErr(r.errors.join('; ') || t('Не отправлено — проверьте настройки.'))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }

  // числа в меню: сломанные чаты, бот без токена, бот не читается, старые ограничения правил
  const info: Record<string, SecInfo> = {}
  const mark = (n: number, level: 1 | 2): SecInfo => ({ level: n ? level : 0, n, line: '' })
  const scopedSrv = srv ? scopedKinds(srv).length : 0
  const scopedSite = site ? scopedKinds(site).length : 0
  if (f && cfg) {
    const broken = f.routes.chats.filter((c) => cfg.routes_state[c.chat]).length
    info.where = mark(broken + (f.telegram_token.trim() && !f.telegram_chat.trim() ? 1 : 0), 2)
    info.bot = mark(!f.telegram_token.trim() && !f.webhook.trim() ? 1 : 0, 2)
    info.buttons = mark(f.tg_buttons && ['conflict', 'error'].includes(cfg.tg_poll.status) ? 1 : 0, 2)
  }
  info.srv = mark(scopedSrv, 1)
  info.site = mark(scopedSite, 1)

  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  const state = !cfg
    ? null
    : f?.muted
      ? { dot: 'warn', text: t('⏸ Алерты на паузе') }
      : cfg.enabled
        ? { dot: 'ok', text: t('Каналы настроены') }
        : { dot: 'bad', text: t('Каналы не настроены') }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Алерты')}</h3>
          {state && (
            <span className="al-state muted small">
              <span className={`lk-dot lk-dot-${state.dot}`} />
              {state.text}
            </span>
          )}
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={info} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>
            <p className="muted small al-lead">{t(LEAD[cur.id])}</p>
            {!f || !cfg ? (
              <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                {cur.id === 'where' && (
                  <>
                    {!f.telegram_token.trim() && (
                      <div className="al-note al-note-warn">
                        <span>{t('Сначала укажите токен бота: без него в Telegram ничего не уйдет.')}</span>
                        <button className="ghost small" onClick={() => go('bot')}>
                          {t('Бот и адрес API')} ›
                        </button>
                      </div>
                    )}
                    <RouteMap f={f} groups={cfg.route_groups} />
                    {scopedSrv + scopedSite > 0 && (
                      <ScopedNote
                        n={scopedSrv + scopedSite}
                        onUnscope={() => {
                          if (srv) editSrv(unscopeRules(srv))
                          if (site) editSite(unscopeRules(site))
                        }}
                      />
                    )}
                    <MainChat f={f} set={set} />
                    <RoutesForm
                      routes={f.routes}
                      groups={cfg.route_groups}
                      state={cfg.routes_state}
                      onChange={(routes) => set({ routes })}
                      onTest={testRoute}
                    />
                  </>
                )}

                {cur.id === 'bot' && (
                  <>
                    <label className="field">
                      <span>{t('Токен бота')}</span>
                      <input
                        value={f.telegram_token}
                        onChange={(e) => set({ telegram_token: e.target.value })}
                        placeholder="123456:ABC…"
                      />
                    </label>
                    <label className="field">
                      <span>{t('Адрес API (для обхода блокировок)')}</span>
                      <input
                        value={f.telegram_api}
                        onChange={(e) => set({ telegram_api: e.target.value })}
                        placeholder="https://api.telegram.org"
                      />
                    </label>
                    <p className="muted small">
                      {t('Свой прокси/зеркало Bot API, если api.telegram.org недоступен. Пусто = по умолчанию.')}
                    </p>
                  </>
                )}

                {cur.id === 'hook' && (
                  <label className="field">
                    <span>URL</span>
                    <input value={f.webhook} onChange={(e) => set({ webhook: e.target.value })} placeholder="https://…" />
                  </label>
                )}

                {cur.id === 'buttons' && (
                  <>
                    <label className="check-toggle">
                      <input type="checkbox" checked={f.tg_buttons} onChange={(e) => set({ tg_buttons: e.target.checked })} />
                      <span>
                        <b>{t('Кнопки под алертами и команды боту')}</b>
                        <span className="muted small">
                          {t('Под алертом: заглушить на час, до утра или насовсем, «взял». Команды в чате: /status, /mutes, /id. Панель сама читает сообщения бота: если этого бота уже читает другая программа или вторая панель, заведите для панели отдельного бота.')}
                        </span>
                      </span>
                    </label>
                    {f.tg_buttons ? (
                      <>
                        <label className="field al-narrow">
                          <span>{t('Часовой пояс («до утра» = до 9:00)')}</span>
                          <input value={f.tz} onChange={(e) => set({ tz: e.target.value })} placeholder="Europe/Moscow" />
                        </label>
                        <TgPollState poll={cfg.tg_poll} />
                        <EscalationForm
                          esc={f.escalation}
                          users={cfg.esc_candidates}
                          onChange={(escalation) => set({ escalation })}
                        />
                      </>
                    ) : (
                      <p className="muted small">
                        {t('Эскалация работает вместе с кнопками: алерт повторяется, пока никто не нажал «взял».')}
                      </p>
                    )}
                  </>
                )}

                {cur.id === 'flood' && (
                  <>
                    <label className="field al-narrow">
                      <span>{t('Группировать при ≥ N алертов за цикл (0 = выкл)')}</span>
                      <input
                        type="number"
                        min={0}
                        max={1000}
                        value={f.flood_threshold}
                        onChange={(e) => set({ flood_threshold: Math.max(0, Number(e.target.value) || 0) })}
                      />
                    </label>
                    <p className="muted small">
                      {t('При массовых событиях (упал стек/аплинк) шлём один дайджест вместо потока сообщений.')}
                    </p>
                    <label className="check-toggle">
                      <input type="checkbox" checked={f.muted} onChange={(e) => set({ muted: e.target.checked })} />
                      <span>
                        <b>{t('Не слать алерты (пауза)')}</b>
                        <span className="muted small">
                          {t('Временно тушит все уведомления. Инциденты продолжают фиксироваться.')}
                        </span>
                      </span>
                    </label>
                  </>
                )}

                {cur.id === 'srv' && <AlertRulesList data={srv} onChange={editSrv} />}
                {cur.id === 'site' && (
                  <AlertRulesList
                    data={site}
                    onChange={editSite}
                    hint={t('По умолчанию алерт выглядит так: «адрес 🔴 — имя-ссылкой на монитор — текст ошибки». Свой текст в поле выше отключает адрес и ссылку.')}
                  />
                )}
              </div>
            )}
          </div>
        </div>

        <div className="al-foot">
          {err ? (
            <span className="form-error small">{err}</span>
          ) : msg ? (
            <span className="form-ok small">{msg}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')}
            </span>
          ) : null}
          <span className="al-foot-btns">
            <button className="ghost" onClick={test} disabled={busy || !f}>
              {t('Тест')}
            </button>
            <button onClick={save} disabled={busy || !dirty}>
              {busy ? t('…') : t('Сохранить')}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}

// Карта "группа -> куда уходят ее алерты": видно сразу, не теряется ли что-то
function RouteMap({ f, groups }: { f: Form; groups: AlertConfig['route_groups'] }) {
  const { t } = useI18n()
  const main = f.telegram_chat.trim() ? [t('Основной чат')] : []
  const chats = f.routes.chats.filter((c) => c.chat.trim())
  const to = (g: string, field: 'server_groups' | 'site_groups') => {
    const hit = chats.filter((c) => c[field].includes(g)).map((c) => c.name.trim() || c.chat)
    return [...(f.routes.main === 'all' || !hit.length ? main : []), ...hit]
  }
  const rows: { g: string; kind: string; dest: string[] }[] = []
  for (const g of [...new Set([...groups.servers, ...groups.sites])]) {
    const s = groups.servers.includes(g) ? to(g, 'server_groups') : null
    const w = groups.sites.includes(g) ? to(g, 'site_groups') : null
    if (s && w && key(s) === key(w)) rows.push({ g, kind: t('серверы и сайты'), dest: s })
    else {
      if (s) rows.push({ g, kind: t('серверы'), dest: s })
      if (w) rows.push({ g, kind: t('сайты'), dest: w })
    }
  }
  rows.push({ g: t('Без группы'), kind: t('серверы и сайты'), dest: main })
  rows.push({ g: t('Общие алерты'), kind: t('про саму панель'), dest: main })
  return (
    <div className="al-flow">
      {rows.map((r) => (
        <div key={`${r.g}/${r.kind}`} className={`al-flow-row${r.dest.length ? '' : ' al-flow-lost'}`}>
          <span className="al-flow-g">
            <b>{r.g}</b>
            <span className="muted small">{r.kind}</span>
          </span>
          <span className="al-flow-ar">→</span>
          <span className="al-flow-to">
            {r.dest.length ? (
              r.dest.map((d, i) => (
                <span key={d} className={`al-dest${i === 0 && main.length && d === main[0] ? ' al-dest-main' : ''}`}>
                  {d}
                </span>
              ))
            ) : (
              <span className="t-down small">{t('никуда')}</span>
            )}
          </span>
        </div>
      ))}
      {f.webhook.trim() && <p className="muted small">{t('Webhook получает все алерты, без деления по группам.')}</p>}
    </div>
  )
}

function MainChat({ f, set }: { f: Form; set: (p: Partial<Form>) => void }) {
  const { t } = useI18n()
  const routes = f.routes
  return (
    <div className="al-box">
      <div className="al-box-head">
        <b>{t('Основной чат')}</b>
      </div>
      <label className="field al-narrow">
        <span>Chat ID</span>
        <input value={f.telegram_chat} onChange={(e) => set({ telegram_chat: e.target.value })} placeholder="-1001234567890" />
      </label>
      {routes.chats.length > 0 && (
        <div className="field route-main">
          <span>{t('Основной чат получает')}</span>
          <div className="win-switch">
            <button
              type="button"
              className={`win-btn${routes.main === 'all' ? ' win-btn-active' : ''}`}
              onClick={() => set({ routes: { ...routes, main: 'all' } })}
            >
              {t('все алерты')}
            </button>
            <button
              type="button"
              className={`win-btn${routes.main === 'rest' ? ' win-btn-active' : ''}`}
              onClick={() => set({ routes: { ...routes, main: 'rest' } })}
            >
              {t('только остальное')}
            </button>
          </div>
          <span className="muted small">
            {routes.main === 'rest'
              ? t('Алерты групп, у которых есть свой чат, в основной чат не попадают. Туда идут остальные группы, серверы без группы и общие алерты панели.')
              : t('Основной чат получает все алерты, а чаты по группам - копии про свои группы.')}
          </span>
        </div>
      )}
    </div>
  )
}

// Как идет чтение бота: после сохранения галки опрос стартует в течение 20 секунд.
function TgPollState({ poll }: { poll: AlertConfig['tg_poll'] }) {
  const { t } = useI18n()
  if (poll.status === 'ok')
    return (
      <p className="muted small">
        <span className="sdot sdot-up" /> {t('Бот {bot} читается, кнопки работают', { bot: poll.bot })}
      </p>
    )
  if (poll.status === 'conflict')
    return (
      <p className="form-error">
        {t('Этого бота уже читает другая программа или панель. Заведите для панели отдельного бота у @BotFather и укажите его токен.')}
      </p>
    )
  if (poll.status === 'error')
    return <p className="form-error">{t('Бот не отвечает: {err}', { err: poll.error })}</p>
  return <p className="muted small">{t('Опрос бота запустится в течение 20 секунд после сохранения.')}</p>
}

// виды алертов, которые имеет смысл повторять: то, что ломает работу прямо сейчас
const ESC_KINDS: [string, string][] = [
  ['down', 'Сайт недоступен'],
  ['offline', 'Сервер недоступен'],
  ['disk', 'Диск'],
  ['disk_health', 'Диск: поломка'],
  ['units', 'Systemd: упал юнит'],
  ['ssl', 'Сертификат'],
  ['kube_node', 'Kubernetes: нода не готова или под давлением'],
  ['backup_failed', 'Бэкап: ошибка'],
]

function EscalationForm({
  esc,
  users,
  onChange,
}: {
  esc: Escalation
  users: AlertConfig['esc_candidates']
  onChange: (e: Escalation) => void
}) {
  const { t } = useI18n()
  const set = (p: Partial<Escalation>) => onChange({ ...esc, ...p })
  const toggle = (list: string[], v: string, on: boolean) =>
    on ? [...list.filter((x) => x !== v), v] : list.filter((x) => x !== v)
  return (
    <div className="al-box">
      <label className="check-toggle">
        <input type="checkbox" checked={esc.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <span>
          <b>{t('Эскалация')}</b>
          <span className="muted small">
            {t('Если на алерт никто не нажал «взял» и беда не ушла, повторить его в другой чат или выбранным людям в личку.')}
          </span>
        </span>
      </label>
      {esc.enabled && (
        <>
          <div className="form-grid">
            <label className="field">
              <span>{t('Через, минут')}</span>
              <input
                type="number"
                min={5}
                max={720}
                value={esc.after_min}
                onChange={(e) => set({ after_min: Math.max(5, Number(e.target.value) || 15) })}
              />
            </label>
            <label className="field">
              <span>{t('Сколько раз повторить')}</span>
              <input
                type="number"
                min={1}
                max={5}
                value={esc.repeat}
                onChange={(e) => set({ repeat: Math.min(5, Math.max(1, Number(e.target.value) || 1)) })}
              />
            </label>
            <label className="field">
              <span>{t('Чат эскалации (ID, можно пусто)')}</span>
              <input value={esc.chat} onChange={(e) => set({ chat: e.target.value })} placeholder="-1001234567890" />
            </label>
          </div>
          <div className="form-grid">
            <div className="field">
              <span>{t('Кому в личку')}</span>
              {users.map((u) => (
                <label key={u.username} className="tg-row">
                  <input
                    type="checkbox"
                    disabled={!u.linked}
                    checked={esc.users.includes(u.username)}
                    onChange={(e) => set({ users: toggle(esc.users, u.username, e.target.checked) })}
                  />
                  {u.username}
                  {!u.linked && <span className="muted small"> {t('(Telegram не привязан)')}</span>}
                </label>
              ))}
            </div>
            <div className="field">
              <span>{t('Какие алерты повторять')}</span>
              {ESC_KINDS.map(([k, label]) => (
                <label key={k} className="tg-row">
                  <input
                    type="checkbox"
                    checked={esc.kinds.includes(k)}
                    onChange={(e) => set({ kinds: toggle(esc.kinds, k, e.target.checked) })}
                  />
                  {t(label)}
                </label>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

const NEW_ROUTE: AlertRoute = { name: '', chat: '', server_groups: [], site_groups: [] }

// Чаты по группам: разработчикам проекта - алерты только его серверов и сайтов, а основной чат
// получает все или только то, что никуда больше не ушло.
function RoutesForm({
  routes,
  groups,
  state,
  onChange,
  onTest,
}: {
  routes: AlertRoutes
  groups: AlertConfig['route_groups']
  state: AlertConfig['routes_state']
  onChange: (r: AlertRoutes) => void
  onTest: (chat: string) => Promise<string>
}) {
  const { t } = useI18n()
  const [tested, setTested] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<number | null>(null)
  const setChat = (i: number, p: Partial<AlertRoute>) =>
    onChange({ ...routes, chats: routes.chats.map((c, j) => (j === i ? { ...c, ...p } : c)) })
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  // группа могла пропасть с серверов, а в чате остаться - показываем и ее, чтобы можно было снять
  const names = (all: string[], mine: string[]) => [...all, ...mine.filter((g) => !all.includes(g))]
  const test = async (i: number) => {
    setBusy(i)
    const err = await onTest(routes.chats[i].chat)
    setTested((x) => ({ ...x, [i]: err || 'ok' }))
    setBusy(null)
  }
  const pick = (i: number, field: 'server_groups' | 'site_groups', all: string[]) => {
    const mine = routes.chats[i][field]
    const list = names(all, mine)
    if (!list.length) return <span className="muted small">{t('групп нет')}</span>
    return list.map((g) => {
      const on = mine.includes(g)
      return (
        <button
          key={g}
          type="button"
          className={`al-chip${on ? ' al-chip-on' : ''}`}
          aria-pressed={on}
          onClick={() => setChat(i, { [field]: toggle(mine, g) })}
        >
          {on ? `✓ ${g}` : g}
        </button>
      )
    })
  }
  return (
    <>
      {routes.chats.map((c, i) => (
        <div key={i} className="al-box">
          <div className="al-box-head">
            <b>{c.name.trim() || t('Чат для групп')}</b>
            <span className="al-box-acts">
              <button className="ghost small" disabled={!c.chat.trim() || busy !== null} onClick={() => test(i)}>
                {busy === i ? t('…') : t('Проверить')}
              </button>
              <button
                className="ghost small"
                onClick={() => onChange({ ...routes, chats: routes.chats.filter((_, j) => j !== i) })}
              >
                {t('Убрать')}
              </button>
            </span>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>{t('Название')}</span>
              <input value={c.name} placeholder={t('напр. Разработка')} onChange={(e) => setChat(i, { name: e.target.value })} />
            </label>
            <label className="field">
              <span>Chat ID</span>
              <input value={c.chat} placeholder="-1001234567890" onChange={(e) => setChat(i, { chat: e.target.value })} />
            </label>
          </div>
          <div className="al-pick">
            <span className="muted small">{t('Группы серверов')}</span>
            <div className="al-chips">{pick(i, 'server_groups', groups.servers)}</div>
          </div>
          <div className="al-pick">
            <span className="muted small">{t('Группы сайтов')}</span>
            <div className="al-chips">{pick(i, 'site_groups', groups.sites)}</div>
          </div>
          {state[c.chat] && !tested[i] && (
            <p className="form-error small">
              {t('Последнее сообщение чат не принял ({at}): {err}. Такие алерты уходят в основной чат.', {
                at: new Date(state[c.chat].at).toLocaleString(),
                err: state[c.chat].err,
              })}
            </p>
          )}
          {tested[i] && tested[i] !== 'ok' && <p className="form-error small">{tested[i]}</p>}
          {tested[i] === 'ok' && <p className="form-ok small">{t('Проверочное сообщение отправлено.')}</p>}
        </div>
      ))}
      <button
        className="ghost al-add"
        onClick={() => onChange({ ...routes, chats: [...routes.chats, { ...NEW_ROUTE }] })}
      >
        {t('+ Чат для групп')}
      </button>
      <p className="muted small">
        {t('Алерты серверов и сайтов из выбранных групп уходят в отдельный чат: например, разработчикам - только про их проект. Бота нужно добавить в этот чат. Id группы видно в Telegram, а если включены команды боту, бот подскажет его в ответ на /id.')}
      </p>
    </>
  )
}
