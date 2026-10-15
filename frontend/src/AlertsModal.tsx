import { useEffect, useState } from 'react'
import {
  ApiError,
  getAlerts,
  putAlerts,
  testAlertRoute,
  testAlerts,
  type AlertConfig,
  type AlertRoute,
  type AlertRoutes,
  type Escalation,
} from './api'
import { ServerAlertRulesSection, SiteAlertRulesSection } from './ServerAlertRules'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

export function AlertsModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [tgToken, setTgToken] = useState('')
  const [tgChat, setTgChat] = useState('')
  const [tgApi, setTgApi] = useState('')
  const [webhook, setWebhook] = useState('')
  const [flood, setFlood] = useState(6)
  const [muted, setMuted] = useState(false)
  const [buttons, setButtons] = useState(false)
  const [tz, setTz] = useState('')
  const [poll, setPoll] = useState<AlertConfig['tg_poll'] | null>(null)
  const [esc, setEsc] = useState<Escalation | null>(null)
  const [escUsers, setEscUsers] = useState<AlertConfig['esc_candidates']>([])
  const [routes, setRoutes] = useState<AlertRoutes | null>(null)
  const [routeGroups, setRouteGroups] = useState<AlertConfig['route_groups']>({ servers: [], sites: [] })
  const [routesState, setRoutesState] = useState<AlertConfig['routes_state']>({})
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getAlerts()
      .then((c) => {
        setTgToken(c.telegram_token)
        setTgChat(c.telegram_chat)
        setTgApi(c.telegram_api)
        setWebhook(c.webhook)
        setFlood(c.flood_threshold)
        setMuted(c.muted)
        setButtons(c.tg_buttons)
        setTz(c.tz)
        setPoll(c.tg_poll)
        setEsc(c.escalation)
        setEscUsers(c.esc_candidates)
        setRoutes(c.routes)
        setRouteGroups(c.route_groups)
        setRoutesState(c.routes_state)
        setEnabled(c.enabled)
      })
      .catch(() => onUnauthorized())
  }, [onUnauthorized])

  // Сохраняет то, что сейчас в форме, и возвращает актуальный конфиг.
  async function persist() {
    const c = await putAlerts({
      telegram_token: tgToken,
      telegram_chat: tgChat,
      telegram_api: tgApi,
      webhook,
      flood_threshold: flood,
      muted,
      tg_buttons: buttons,
      tz,
      ...(esc ? { escalation: esc } : {}),
      ...(routes ? { routes } : {}),
    })
    setTgApi(c.telegram_api)
    setEnabled(c.enabled)
    setMuted(c.muted)
    setFlood(c.flood_threshold)
    setButtons(c.tg_buttons)
    setTz(c.tz)
    setPoll(c.tg_poll)
    setEsc(c.escalation)
    setRoutes(c.routes)
    setRoutesState(c.routes_state)
    return c
  }

  // проверка чата по группам: сначала сохраняем форму, чтобы чат и его группы уже были
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
      await persist()
      setMsg(t('Сохранено.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  // Тест проверяет то, что на экране: сперва сохраняем форму, затем шлём тест.
  async function test() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const c = await persist()
      if (!c.enabled) {
        setErr(t('Заполните Telegram (токен и Chat ID) или Webhook.'))
        return
      }
      const r = await testAlerts()
      if (r.sent) setMsg(t('Тестовое уведомление отправлено.'))
      else setErr(r.errors.join('; ') || t('Не отправлено — проверьте настройки.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Алерты')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Уведомления о падении/восстановлении мониторов и скором истечении сертификатов.')}
        </p>

        <div className="settings-group">
          <h4>Telegram</h4>
          <label className="field">
            <span>{t('Токен бота')}</span>
            <input value={tgToken} onChange={(e) => setTgToken(e.target.value)} placeholder="123456:ABC…" />
          </label>
          <label className="field">
            <span>Chat ID</span>
            <input value={tgChat} onChange={(e) => setTgChat(e.target.value)} placeholder="-1001234567890" />
          </label>
          <label className="field">
            <span>{t('Адрес API (для обхода блокировок)')}</span>
            <input
              value={tgApi}
              onChange={(e) => setTgApi(e.target.value)}
              placeholder="https://api.telegram.org"
            />
          </label>
          <p className="muted small">
            {t('Свой прокси/зеркало Bot API, если api.telegram.org недоступен. Пусто = по умолчанию.')}
          </p>
          <label className="check-toggle">
            <input type="checkbox" checked={buttons} onChange={(e) => setButtons(e.target.checked)} />
            <span>
              <b>{t('Кнопки под алертами и команды боту')}</b>
              <span className="muted small">
                {t('Под алертом: заглушить на час, до утра или насовсем, «взял». Команды в чате: /status, /mutes. Панель сама читает сообщения бота: если этого бота уже читает другая программа или вторая панель, заведите для панели отдельного бота.')}
              </span>
            </span>
          </label>
          {buttons && (
            <>
              <label className="field">
                <span>{t('Часовой пояс («до утра» = до 9:00)')}</span>
                <input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/Moscow" />
              </label>
              {poll && <TgPollState poll={poll} />}
              {esc && <EscalationForm esc={esc} users={escUsers} onChange={setEsc} />}
            </>
          )}
        </div>

        {routes && (
          <RoutesForm
            routes={routes}
            groups={routeGroups}
            state={routesState}
            onChange={setRoutes}
            onTest={testRoute}
          />
        )}

        <div className="settings-group">
          <h4>Webhook</h4>
          <label className="field">
            <span>URL</span>
            <input value={webhook} onChange={(e) => setWebhook(e.target.value)} placeholder="https://…" />
          </label>
        </div>

        <div className="settings-group">
          <h4>{t('Антифлуд')}</h4>
          <label className="field">
            <span>{t('Группировать при ≥ N алертов за цикл (0 = выкл)')}</span>
            <input
              type="number"
              min={0}
              max={1000}
              value={flood}
              onChange={(e) => setFlood(Math.max(0, Number(e.target.value) || 0))}
            />
          </label>
          <p className="muted small">
            {t('При массовых событиях (упал стек/аплинк) шлём один дайджест вместо потока сообщений.')}
          </p>
        </div>

        <div className="settings-group">
          <label className="check-toggle">
            <input
              type="checkbox"
              checked={muted}
              onChange={(e) => setMuted(e.target.checked)}
            />
            <span>
              <b>{t('Не слать алерты (пауза)')}</b>
              <span className="muted small">
                {t('Временно тушит все уведомления. Инциденты продолжают фиксироваться.')}
              </span>
            </span>
          </label>
        </div>

        <p className="muted small">
          {muted
            ? t('⏸ Алерты на паузе')
            : enabled
              ? t('Каналы настроены ✓')
              : t('Каналы не настроены')}
        </p>

        <SiteAlertRulesSection onUnauthorized={onUnauthorized} />
        <ServerAlertRulesSection onUnauthorized={onUnauthorized} />

        {err && <p className="form-error">{err}</p>}
        {msg && <p className="form-ok">{msg}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={test} disabled={busy}>
            {t('Тест')}
          </button>
          <button onClick={save} disabled={busy}>
            {busy ? t('…') : t('Сохранить')}
          </button>
        </div>
      </div>
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
    <div className="settings-group">
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
    return list.map((g) => (
      <label key={g} className="tg-row">
        <input type="checkbox" checked={mine.includes(g)} onChange={() => setChat(i, { [field]: toggle(mine, g) })} />
        {g}
      </label>
    ))
  }
  return (
    <div className="settings-group">
      <h4>{t('Чаты по группам')}</h4>
      <p className="muted small">
        {t('Алерты серверов и сайтов из выбранных групп уходят в отдельный чат: например, разработчикам - только про их проект. Бота нужно добавить в этот чат. Id группы видно в Telegram, а если включены команды боту, бот подскажет его в ответ на /id.')}
      </p>
      {routes.chats.map((c, i) => (
        <div key={i} className="route-card">
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
          <div className="form-grid">
            <div className="field">
              <span>{t('Группы серверов')}</span>
              {pick(i, 'server_groups', groups.servers)}
            </div>
            <div className="field">
              <span>{t('Группы сайтов')}</span>
              {pick(i, 'site_groups', groups.sites)}
            </div>
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
          <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
            <button className="ghost" disabled={!c.chat.trim() || busy !== null} onClick={() => test(i)}>
              {busy === i ? t('…') : t('Проверить')}
            </button>
            <button
              className="ghost"
              onClick={() => onChange({ ...routes, chats: routes.chats.filter((_, j) => j !== i) })}
            >
              {t('Убрать')}
            </button>
          </div>
        </div>
      ))}
      <button className="ghost" onClick={() => onChange({ ...routes, chats: [...routes.chats, { ...NEW_ROUTE }] })}>
        {t('+ Добавить чат')}
      </button>
      {routes.chats.length > 0 && (
        <div className="field route-main">
          <span>{t('Основной чат получает')}</span>
          <div className="win-switch">
            <button
              type="button"
              className={`win-btn${routes.main === 'all' ? ' win-btn-active' : ''}`}
              onClick={() => onChange({ ...routes, main: 'all' })}
            >
              {t('все алерты')}
            </button>
            <button
              type="button"
              className={`win-btn${routes.main === 'rest' ? ' win-btn-active' : ''}`}
              onClick={() => onChange({ ...routes, main: 'rest' })}
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
