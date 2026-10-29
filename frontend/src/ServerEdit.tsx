import { useState, type ReactNode } from 'react'
import { type GreView, type RelayView } from './api'
import { useAuth } from './auth'
import { CardNav, type SecDef } from './cardLayout'
import { UpdCmd, helperCmd } from './FleetUpdate'
import { useI18n } from './i18n'
import { SRV_ALERT_KINDS, kindLabel } from './mutes'
import { useEscape } from './useEscape'

// Окно правки сервера: меню слева по областям сервера, в разделе области ее пороги и какие ее
// алерты слать, порог и переключатель в одной строке. Сохранение одно, в подвале. Заметка
// сохраняется сама по себе (NoteBar), ее передает карточка

export type ServerEditForm = {
  name: string
  group_name: string
  agent_ip: string
  cpu_alert_percent: number
  mem_alert_percent: number
  disk_alert_percent: number
  disk_warn_percent: number
  disk_crit_percent: number
  disk_autofix: boolean
  temp_alert_c: number
  conntrack_alert_percent: number
  db_conn_alert_percent: number
  web_5xx_alert_percent: number
  kube_expiry_warn_days: string // "7, 1": по сообщению на каждый порог, пусто - выключено
  disk_temp_alert_c: number
  alert_mutes: string[]
  offline_after_seconds: number
  alert_sustain_min: number // «жарит дольше N минут» → алерт (хранится в сек)
  relay_enabled: boolean // ретранслятор для агентов (app/relay.py), сохраняется отдельно (setRelay)
  relay_port: number // 0 - по умолчанию
  gre_enabled: boolean // GRE до панели (backend app/gre.py), сохраняется отдельно (setGre)
  is_dev: boolean // сервер разработки (backend app/dev_mode.py)
}

export const RELAY_DEFAULT_PORT = 8790

// Области сервера: виды алертов SRV_ALERT_KINDS по разделам окна. Вид, которого здесь нет,
// попадает в "Сервер" - новый вид не пропадет из окна, даже если его забыли разложить
const AREAS: { id: string; label: string; kinds: string[] }[] = [
  { id: 'srv', label: 'Сервер', kinds: ['offline', 'reboot', 'listen_gone'] },
  { id: 'load', label: 'Нагрузка и память', kinds: ['cpu', 'mem', 'temp', 'throttle', 'pressure', 'cpu_spin', 'oom', 'mem_leak', 'mem_limit'] },
  { id: 'disk', label: 'Диск', kinds: ['disk', 'disk_forecast', 'inode', 'disk_health', 'disk_slow', 'disktemp'] },
  { id: 'net', label: 'Сеть', kinds: ['conntrack', 'net_health', 'link', 'traffic', 'vpn_torrent', 'vpn_heavy', 'port_new', 'exposed', 'cf_realip'] },
  { id: 'svc', label: 'Службы и очереди', kinds: ['units', 'unit_loop', 'workers', 'cron_stuck', 'queue_stuck', 'queue_failed', 'log_errors', 'fpm'] },
  { id: 'k8s', label: 'Docker и Kubernetes', kinds: ['docker_restarts', 'kube_restarts', 'kube_pod'] },
  { id: 'web', label: 'Веб', kinds: ['web_5xx', 'web_config', 'web_cert', 'gitlab'] },
  { id: 'db', label: 'СУБД и бэкап', kinds: ['db_health', 'backup_growth'] },
  { id: 'sec', label: 'Безопасность', kinds: ['ssh_risk', 'access_risk', 'access_change', 'docker_sock', 'docker_risk', 'webfiles', 'autoban', 'bogus_requests'] },
  { id: 'gpu', label: 'GPU и LLM', kinds: ['gpu', 'gpu_heat', 'llm'] },
]
const AREA_OF = new Map(AREAS.flatMap((a) => a.kinds.map((k) => [k, a.id] as const)))

// Остальное в alert_mutes: точечные заглушки (вид:что) и виды правил, заглушенные из карточки или
// кнопкой в Telegram. В старой форме их не было видно вовсе. Область - по началу ключа
const OTHER_AREA: [string, string][] = [
  ['leak:', 'load'], ['memlim:', 'load'], ['oom:', 'load'],
  ['exposed:', 'net'], ['cfip:', 'net'], ['dns', 'net'], ['torrent:', 'net'], ['vpnheavy:', 'net'],
  ['unit:', 'svc'], ['noboot', 'svc'], ['qstuck:', 'svc'], ['qfail:', 'svc'], ['worker:', 'svc'],
  ['cron:', 'svc'], ['logerr:', 'svc'], ['fpm:', 'svc'], ['queue', 'svc'],
  ['restarts:', 'k8s'], ['docker_', 'k8s'], ['kube_', 'k8s'], ['flux_', 'k8s'], ['proxyold:', 'k8s'],
  ['web_5xx:', 'web'], ['gitlab:', 'web'],
  ['db_conn', 'db'], ['backup_', 'db'], ['bkhttp:', 'db'],
  ['autoban:', 'sec'], ['ssh:', 'sec'], ['access', 'sec'], ['dockrisk:', 'sec'],
]
const KNOWN = new Set(SRV_ALERT_KINDS.map((x) => x.k))
const areaOfMute = (k: string) =>
  AREA_OF.get(k) ?? OTHER_AREA.find(([p]) => k.startsWith(p))?.[1] ?? 'srv'

const SECS: SecDef[] = [
  ...AREAS.map((a) => ({ id: a.id, label: a.label, group: '' })),
  { id: 'relay', label: 'Ретранслятор', group: 'Другое' },
  { id: 'gre', label: 'GRE до панели', group: 'Другое' },
  { id: 'del', label: 'Удаление', group: 'Другое' },
]

// температуры на ВМ не видны: датчиков нет, алертов по ним не будет
const HW_ONLY = ['temp', 'disktemp']

// Как дела у ретранслятора: что сказал helper relay-setup и когда
function RelayStatus({ relay, host }: { relay: RelayView; host: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const fw = relay.firewall === 'none' ? t('без фаервола: адреса проверяет сам прокси') : relay.firewall
  const line =
    relay.state === 'ok'
      ? t('Работает: порт {port}, серверов {n}, фаервол {fw}, соединений {c}', {
          port: relay.port,
          n: relay.clients,
          fw: fw || '?',
          c: relay.conns,
        })
      : relay.state === 'no_helper'
        ? t('Helper relay-setup еще не поставлен: он в "Требует действий", ставится плейбуком kervax_helpers.yml.')
        : relay.state === 'waiting'
          ? t('Helper поставлен, ждем его первого обращения к панели (раз в минуту).')
          : relay.state === 'stale'
            ? t('Helper давно не обращался к панели: сервер не на связи или не достает до нее.')
            : t('Ошибка ретранслятора: {err}', { err: relay.error })
  if (relay.state !== 'no_helper') {
    return <p className={`small ${relay.state === 'ok' ? 'muted' : 'form-error'}`}>📡 {line}</p>
  }
  // helper ставит человек плейбуком: готовая команда на одну эту ноду, без поиска в "Требует действий"
  const cmd = helperCmd([host])
  return (
    <div className="small form-error">
      📡 {line}
      <UpdCmd cmd={cmd} />
      <button
        type="button"
        className="ghost small"
        onClick={() => {
          try { navigator.clipboard?.writeText(cmd)?.catch(() => {}) } catch { /* команда видна в строке */ }
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? t('Скопировано ✓') : t('Скопировать команду')}
      </button>
    </div>
  )
}

// Как дела у GRE до панели: что сказал helper gre-setup, хост панели и агент
function GreStatus({ gre, host }: { gre: GreView; host: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const line =
    gre.state === 'ok'
      ? t('Работает: агент ходит в панель через туннель {node} -> {panel}, адрес сервера {peer}', {
          node: gre.node_addr,
          panel: gre.panel_addr,
          peer: gre.peer,
        })
      : gre.state === 'up'
        ? t('Туннель поднят, панель через него отвечает. Ждем первый отчет агента через туннель (нужен агент 2.56).')
        : gre.state === 'no_helper'
          ? t('Helper gre-setup еще не поставлен: он в "Требует действий", ставится плейбуком kervax_helpers.yml.')
          : gre.state === 'waiting'
            ? t('Helper поставлен, ждем его первого обращения к панели (раз в минуту).')
            : gre.state === 'host'
              ? t('Хост панели свою сторону туннеля еще не поднял: на нем нужен ops/gre-sync.sh (docs/install.md, раздел про GRE).')
              : gre.state === 'nat'
                ? t('У сервера нет публичного адреса на интерфейсе (он за NAT): GRE через NAT не проходит.')
                : gre.state === 'stale'
                  ? t('Helper давно не обращался к панели: сервер не на связи или не достает до нее.')
                  : gre.state === 'down'
                    ? t('Туннель не поднят: панель через него не отвечает. Агент пока ходит напрямую.')
                    : t('Ошибка GRE: {err}', { err: gre.error })
  const ok = gre.state === 'ok' || gre.state === 'up'
  if (gre.state !== 'no_helper') {
    return (
      <p className={`small ${ok ? 'muted' : 'form-error'}`}>
        🚇 {line}
        {!ok && gre.error && gre.state !== 'error' && <span className="muted"> ({gre.error})</span>}
      </p>
    )
  }
  // helper ставит человек плейбуком: готовая команда на одну эту ноду
  const cmd = helperCmd([host])
  return (
    <div className="small form-error">
      🚇 {line}
      <UpdCmd cmd={cmd} />
      <button
        type="button"
        className="ghost small"
        onClick={() => {
          try { navigator.clipboard?.writeText(cmd)?.catch(() => {}) } catch { /* команда видна в строке */ }
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? t('Скопировано ✓') : t('Скопировать команду')}
      </button>
    </div>
  )
}

export function ServerEditModal({
  form,
  set,
  busy,
  err,
  groups,
  isVm,
  relay,
  gre,
  host,
  note,
  initialSec,
  devHint,
  onSubmit,
  onCancel,
  onDelete,
}: {
  form: ServerEditForm
  set: (patch: Partial<ServerEditForm>) => void
  busy: boolean
  err: string | null
  groups: string[]
  isVm?: boolean
  relay?: RelayView | null
  gre?: GreView | null
  host: string // имя ноды для ansible -l (как в "Требует действий")
  note: ReactNode // заметка: сохраняется сразу, отдельно от формы
  initialSec?: string
  devHint?: boolean // имя или группа похожи на разработку, а пометки нет (backend dev_mode.looks_dev)
  onSubmit: () => void
  onCancel: () => void
  onDelete: () => void
}) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  const [sec, setSec] = useState(() => (SECS.some((x) => x.id === initialSec) ? initialSec! : 'srv'))
  const [form0] = useState(() => JSON.stringify(form))
  const dirty = JSON.stringify(form) !== form0
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onCancel()
  }
  useEscape(close)

  // пустое поле - NaN: при сохранении оно не уходит (раньше уходил 0 и бэкенд отвечал 422)
  const num = (v: string) => (v === '' ? NaN : Number(v))
  const nv = (x: number) => (Number.isFinite(x) ? x : '')
  const muted = (k: string) => form.alert_mutes.includes(k)
  const toggle = (k: string) =>
    set({ alert_mutes: muted(k) ? form.alert_mutes.filter((x) => x !== k) : [...form.alert_mutes, k] })
  const shown = (k: string) => !isVm || !HW_ONLY.includes(k)
  const others = (area: string) => form.alert_mutes.filter((k) => !KNOWN.has(k) && areaOfMute(k) === area)

  // в меню у области 🔕: в ней что-то заглушено, само не вернется
  const side: Record<string, string> = {}
  for (const k of form.alert_mutes) if (!KNOWN.has(k) || shown(k)) side[areaOfMute(k)] = '🔕'
  if (form.relay_enabled) side.relay = t('вкл')
  if (form.gre_enabled) side.gre = t('вкл')

  type NumKey = {
    [K in keyof ServerEditForm]: ServerEditForm[K] extends number ? K : never
  }[keyof ServerEditForm]
  // поле порога: число, единица, а у порогов с 0 = выкл вместо единицы "выкл"
  const thr = (key: NumKey, unit: string, o: { min?: number; max?: number; step?: number; off?: boolean } = {}) => {
    const v = form[key]
    const off = o.off && Number.isFinite(v) && v === 0
    return (
      <span className="se-thr">
        <input
          type="number"
          min={o.min ?? 0}
          max={o.max ?? 100}
          step={o.step}
          value={nv(v)}
          onChange={(e) => set({ [key]: num(e.target.value) } as Partial<ServerEditForm>)}
        />
        <span className={`se-unit${off ? ' se-unit-off' : ''}`} title={o.off ? t('0 = выкл') : undefined}>
          {off ? t('выкл') : unit}
        </span>
      </span>
    )
  }
  const kindRow = (k: string, threshold?: ReactNode, cap = t('порог')) => {
    const m = muted(k)
    const label = t(SRV_ALERT_KINDS.find((x) => x.k === k)?.label ?? k)
    return (
      <div key={k} className={`se-row${m ? ' se-row-off' : ''}`}>
        <span className="se-label">{m && '🔕 '}{label}</span>
        {threshold && (
          <span className="se-row-thr">
            <span className="muted small">{cap}</span>
            {threshold}
          </span>
        )}
        {m && <span className="se-not small">{t('не слать')}</span>}
        <button
          type="button"
          role="switch"
          aria-checked={!m}
          aria-label={label}
          className={`se-sw${m ? '' : ' se-sw-on'}`}
          title={m ? t('Не слать. Включить - алерт снова будет приходить') : t('Слать. Выключить - алерт с этого сервера не придет')}
          onClick={() => toggle(k)}
        />
      </div>
    )
  }
  // заглушка вне списка видов: снять можно, включить заново - там, где ее ставили
  const otherRow = (k: string) => (
    <div key={k} className="se-row se-row-off">
      <span className="se-label">🔕 {kindLabel(k, t)}</span>
      {k.includes(':') && (
        <span className="se-tag small" title={t('Заглушено одно это, остальные алерты такого вида приходят')}>
          {t('точечно')}
        </span>
      )}
      <button type="button" className="ghost small" onClick={() => toggle(k)}>
        {t('вернуть')}
      </button>
    </div>
  )
  // строка порога без своего переключателя: вида с таким ключом в списке нет, выключается нулем
  const thrLine = (label: string, hint: string, field: ReactNode) => (
    <div className="se-row">
      <span className="se-label">
        {label} <span className="muted small">{hint}</span>
      </span>
      <span className="se-row-thr">{field}</span>
    </div>
  )

  const area = AREAS.find((a) => a.id === sec)
  const rows = (a: (typeof AREAS)[number], thrOf: Record<string, ReactNode> = {}, capOf: Record<string, string> = {}) => (
    <div className="se-rows">
      <div className="se-rows-head small">
        <span>{t('алерт')}</span>
        <span>{t('слать')}</span>
      </div>
      {a.kinds.filter(shown).map((k) => kindRow(k, thrOf[k], capOf[k]))}
      {/* вид, который забыли разложить по областям */}
      {a.id === 'srv' && SRV_ALERT_KINDS.filter((x) => !AREA_OF.has(x.k)).map((x) => kindRow(x.k))}
      {others(a.id).map(otherRow)}
    </div>
  )

  let body: ReactNode = null
  if (area?.id === 'srv')
    body = (
      <>
        <label className="field">
          <span>{t('Название')}</span>
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} autoFocus />
        </label>
        <div className="se-pair">
          <label className="field">
            <span>{t('Группа')}</span>
            <input
              value={form.group_name}
              onChange={(e) => set({ group_name: e.target.value })}
              placeholder={t('напр. Прод / БД')}
              list="kervax-server-groups"
            />
            <datalist id="kervax-server-groups">
              {groups.map((g) => (
                <option key={g} value={g} />
              ))}
            </datalist>
          </label>
          <label className="field">
            <span title={t('Адрес, с которого агент ходит в панель — для разрешения в фаерволе хоста панели (ufw/firewalld).')}>
              {t('IP сервера (опционально)')} <span className="se-q">?</span>
            </span>
            <input
              value={form.agent_ip}
              onChange={(e) => set({ agent_ip: e.target.value })}
              placeholder="203.0.113.10"
            />
          </label>
        </div>
        {/* сервер разработки: поведение приложений не будит алертами (backend app/dev_mode.py) */}
        <div className="se-dev">
          <div className="win-switch" role="group" aria-label={t('Назначение')}>
            {([[false, t('Прод')], [true, t('Разработка')]] as const).map(([v, l]) => (
              <button
                key={l}
                type="button"
                className={`win-btn${form.is_dev === v ? ' win-btn-active' : ''}`}
                aria-pressed={form.is_dev === v}
                onClick={() => set({ is_dev: v })}
              >
                {l}
              </button>
            ))}
          </div>
          <span className="muted small">
            {form.is_dev
              ? t('Перезапуски подов и контейнеров, воркеры, очереди, базы, ошибки в логах и 5xx не шлются, в панели серым. Недоступность, диск, память, бэкапы и безопасность - как обычно.')
              : devHint
                ? t('По имени похоже на сервер разработки: там падения приложений бывают нормой.')
                : t('Разработка: поведение приложений не будит алертами, инфраструктура - как обычно.')}
          </span>
        </div>
        <div className="se-note">{note}</div>
        {rows(area, { offline: thr('offline_after_seconds', t('сек'), { min: 30, max: 86400 }) }, { offline: t('молчит дольше') })}
      </>
    )
  else if (area?.id === 'load')
    body = (
      <>
        {thrLine(
          t('Слать, если держится дольше'),
          t('гасит спайки CPU, RAM, температуры и conntrack, 0 = слать сразу'),
          thr('alert_sustain_min', t('мин'), { max: 1440 }),
        )}
        {rows(area, {
          cpu: thr('cpu_alert_percent', '%'),
          mem: thr('mem_alert_percent', '%'),
          temp: thr('temp_alert_c', '°C', { max: 120, off: true }),
        })}
      </>
    )
  else if (area?.id === 'disk')
    body = (
      <>
        <div className="se-levels">
          <span className="muted small">{t('Заполнение, 0 = уровень выключен')}</span>
          <div className="se-levels-row">
            <label className="se-level">
              <span>⚠️ {t('предупр.')}</span>
              {thr('disk_warn_percent', '%')}
            </label>
            <label className="se-level">
              <span>🔴 {t('проблема')}</span>
              {thr('disk_alert_percent', '%')}
            </label>
            <label className="se-level">
              <span>🚨 {t('критично')}</span>
              {thr('disk_crit_percent', '%')}
            </label>
          </div>
        </div>
        {/* Панель начнет сама запускать удаление на ноде - решение админа (сервер проверяет) */}
        <label className="checkbox se-check">
          <input
            type="checkbox"
            checked={form.disk_autofix}
            disabled={!isAdmin}
            onChange={(e) => set({ disk_autofix: e.target.checked })}
          />
          <span title={t('Каждое действие не чаще раза в 6 часов, о каждом запуске пишет в канал алертов. Нужны агент 2.13 и helper diskusage-setup 0.3.')}>
            🧹 {t('Автоочистка диска')} <span className="se-q">?</span>
          </span>
        </label>
        <p className="muted small se-hint">
          {t('С порога предупреждения панель сама чистит журнал, старые логи, кэши пакетов и мусор docker.')}
          {!isAdmin && ` ${t('Включает только админ.')}`}
        </p>
        {rows(area, { disktemp: thr('disk_temp_alert_c', '°C', { max: 120, off: true }) })}
      </>
    )
  else if (area?.id === 'net') body = rows(area, { conntrack: thr('conntrack_alert_percent', '%', { off: true }) })
  else if (area?.id === 'k8s')
    body = (
      <>
        {thrLine(
          t('Предупреждать о сроках Kubernetes'),
          t('за сколько дней, через запятую, пусто = выкл'),
          <span className="se-thr">
            <input
              type="text"
              className="se-days"
              placeholder="7, 1"
              value={form.kube_expiry_warn_days}
              onChange={(e) => set({ kube_expiry_warn_days: e.target.value })}
            />
            <span className="se-unit">{t('дн.')}</span>
          </span>,
        )}
        {rows(area)}
      </>
    )
  else if (area?.id === 'web')
    body = rows(area, { web_5xx: thr('web_5xx_alert_percent', '%', { step: 0.01, off: true }) }, { web_5xx: t('за 15 минут') })
  else if (area?.id === 'db')
    body = (
      <>
        {thrLine(
          t('Коннекты СУБД'),
          t('по самой нагруженной СУБД, 0 = выкл'),
          thr('db_conn_alert_percent', '%', { off: true }),
        )}
        {rows(area)}
      </>
    )
  else if (area) body = rows(area)
  else if (sec === 'relay')
    body = (
      <>
        {/* ретранслятор: прокси до панели для агентов, чей адрес режут на пути к ней */}
        <label className="checkbox se-check">
          <input
            type="checkbox"
            checked={form.relay_enabled}
            onChange={(e) => set({ relay_enabled: e.target.checked })}
          />
          <span title={t('Пускает только до панели, только с адресов серверов, выбравших его в окне команды установки, и только с логином. Ставит helper relay-setup (появится в "Требует действий"), нужен python3.')}>
            📡 {t('Ретранслятор для агентов')} <span className="se-q">?</span>
          </span>
        </label>
        <p className="muted small se-hint">
          {t('Сервер станет прокси до панели для агентов, чей адрес режут по пути. Включать, только если он сам ходит в панель напрямую.')}
        </p>
        {form.relay_enabled && (
          <label className="field se-port">
            <span>{t('Порт ретранслятора')}</span>
            <input
              type="number"
              min={1024}
              max={65535}
              value={form.relay_port || ''}
              placeholder={String(RELAY_DEFAULT_PORT)}
              onChange={(e) => set({ relay_port: num(e.target.value) })}
            />
          </label>
        )}
        {form.relay_enabled && relay && <RelayStatus relay={relay} host={host} />}
      </>
    )
  else if (sec === 'gre')
    body = (
      <>
        {/* GRE до панели: туннель до хоста панели для агента, чье соединение с панелью режут по объему */}
        <label className="checkbox se-check">
          <input
            type="checkbox"
            checked={form.gre_enabled}
            onChange={(e) => set({ gre_enabled: e.target.checked })}
          />
          <span title={t('Панель выделит серверу свою /30 в туннеле. Ставит helper gre-setup (появится в "Требует действий"), нужны python3 и публичный адрес на интерфейсе сервера. На хосте панели нужен ops/gre-sync.sh.')}>
            🚇 {t('GRE до панели')} <span className="se-q">?</span>
          </span>
        </label>
        <p className="muted small se-hint">
          {t('Для сервера, чье соединение с панелью режут после ~16кб: обновления агента и большие ответы не доходят. Агент пойдет в панель через GRE-туннель до ее хоста, внутри то же TLS-соединение. Не отвечает туннель - агент сам вернется на прямое соединение.')}
        </p>
        {form.gre_enabled && gre && <GreStatus gre={gre} host={host} />}
      </>
    )
  else if (sec === 'del')
    body = (
      <div className="se-danger">
        <span>
          <b>{t('Удалить сервер')}</b>
          <span className="muted small">{t('Агент на ноде продолжит слать, его удалить отдельно.')}</span>
        </span>
        <button className="ghost danger-btn" onClick={onDelete} disabled={busy}>
          🗑 {t('Удалить сервер')}
        </button>
      </div>
    )

  const cur = SECS.find((x) => x.id === sec)!
  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{form.name.trim() || t('Сервер')}</h3>
          <span className="muted small">{t('настройки')}</span>
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="al-body">
          <CardNav secs={SECS} active={sec} info={{}} side={side} onGo={setSec} />
          <div className="al-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>
            <div className="srv-sec se-sec">{body}</div>
          </div>
        </div>
        <div className="al-foot">
          {err ? (
            <span className="form-error small">{err}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')}
            </span>
          ) : null}
          <span className="al-foot-btns">
            <button className="ghost" onClick={close} disabled={busy}>
              {t('Отмена')}
            </button>
            <button onClick={onSubmit} disabled={busy || !dirty || !form.name.trim()}>
              {busy ? t('…') : t('Сохранить')}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}
