import { useEffect, useState } from 'react'
import {
  listLocations,
  listServers,
  type CheckForm,
  type CheckType,
  type Location,
  type Server,
} from './api'
import { useI18n } from './i18n'

// типы сайтовых алертов, которые можно точечно заглушить для монитора (ключи = SITE_ALERT_KINDS)
const MON_ALERT_KINDS: { k: string; label: string }[] = [
  { k: 'down', label: 'Недоступен / деградация' },
  { k: 'ssl', label: 'SSL-сертификат' },
  { k: 'domain', label: 'Домен' },
  { k: 'locpart', label: 'Частичная доступность (локации)' },
]

type Props = {
  form: CheckForm
  set: (p: Partial<CheckForm>) => void
  editing: boolean
  busy: boolean
  canSubmit: boolean
  onSubmit: () => void
  onCancel: () => void
  // внутри модалки детали карточку не оборачиваем в .card (модалка уже карточка)
  bare?: boolean
  // существующие имена групп — для автодополнения поля «Группа»
  groups?: string[]
}

export function CheckFormCard({
  form,
  set,
  editing,
  busy,
  canSubmit,
  onSubmit,
  onCancel,
  bare = false,
  groups = [],
}: Props) {
  const { t } = useI18n()
  const num = (v: string) => (v === '' ? 0 : Number(v))
  const [showPass, setShowPass] = useState(false)
  // подсказка валидности JSON-заголовков (не блокирует — бэкенд толерантен)
  const headersInvalid = (() => {
    const raw = (form.http_headers ?? '').trim()
    if (!raw) return false
    try {
      const v = JSON.parse(raw)
      return typeof v !== 'object' || v === null || Array.isArray(v)
    } catch {
      return true
    }
  })()

  // пороги напоминаний вводятся списком «14, 7, 1»; храним сырой текст для плавного ввода
  const daysStr = (v?: number[]) => (v && v.length ? v.join(', ') : '')
  const parseDays = (s: string) => s.split(/[^0-9]+/).filter(Boolean).map(Number)
  const [sslTxt, setSslTxt] = useState(() => daysStr(form.ssl_warn_days))
  const [domTxt, setDomTxt] = useState(() => daysStr(form.domain_warn_days))

  // список локаций для выбора «из каких проверять» (грузим для http)
  const [allLocs, setAllLocs] = useState<Location[]>([])
  useEffect(() => {
    if (form.type === 'http') listLocations().then(setAllLocs).catch(() => {})
  }, [form.type])

  // Сайт за белым списком: панель снаружи получит обрыв или 403, поэтому проверять
  // его может только агент на самом сервере.
  const [srvs, setSrvs] = useState<Server[]>([])
  useEffect(() => {
    if (form.type === 'http') listServers().then(setSrvs).catch(() => {})
  }, [form.type])

  const ids = form.location_ids // null = все, [] = ни одной, [id…] = подмножество
  const locChecked = (id: number) => ids == null || ids.includes(id)
  const toggleLoc = (id: number) => {
    const cur = ids == null ? allLocs.map((l) => l.id) : ids
    set({
      location_ids: locChecked(id) ? cur.filter((x) => x !== id) : [...cur, id],
    })
  }

  return (
    <div className={bare ? 'check-form' : 'card check-form'}>
      <div className="form-grid">
        <label className="field field-wide">
          <span>{t('Название')}</span>
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} autoFocus />
          <span className="field-hint muted small">
            {t('Название попадает в текст алерта: @упоминания в нём тегнут людей в Telegram.')}
          </span>
        </label>
        <label className="field">
          <span>{t('Тип')}</span>
          <select
            value={form.type}
            onChange={(e) => set({ type: e.target.value as CheckType })}
          >
            <option value="http">{t('HTTP(S) — сайт/эндпоинт')}</option>
            <option value="tcp_port">{t('TCP-порт')}</option>
          </select>
        </label>
        <label className="field field-wide">
          <span>{form.type === 'http' ? t('URL или домен') : t('Хост (домен/IP)')}</span>
          <input
            value={form.target}
            onChange={(e) => set({ target: e.target.value })}
            placeholder="example.com"
          />
          {form.type === 'http' && (
            <span className="field-hint muted small">
              {t('Схему можно не писать — определим сами (приоритет https).')}
            </span>
          )}
        </label>
        <label className="field field-wide">
          <span>{t('Группа')}</span>
          <input
            value={form.group_name ?? ''}
            onChange={(e) => set({ group_name: e.target.value })}
            placeholder={t('напр. Организация / Прод / VPN')}
            list="kervax-groups"
          />
          <datalist id="kervax-groups">
            {groups.map((g) => (
              <option key={g} value={g} />
            ))}
          </datalist>
        </label>
        {form.type === 'tcp_port' && (
          <label className="field">
            <span>{t('Порт')}</span>
            <input
              type="number"
              value={form.port || ''}
              placeholder="80"
              onChange={(e) => set({ port: num(e.target.value) })}
            />
          </label>
        )}
        <label className="field">
          <span>{t('Интервал, сек')}</span>
          <input
            type="number"
            value={form.interval_seconds ?? 60}
            onChange={(e) => set({ interval_seconds: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>{t('Порог «медленно», мс')}</span>
          <input
            type="number"
            value={form.degraded_ms ?? 2000}
            onChange={(e) => set({ degraded_ms: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>{t('Повторы при сбое')}</span>
          <input
            type="number"
            value={form.retries ?? 2}
            onChange={(e) => set({ retries: num(e.target.value) })}
            title={t('Считать «упавшим» и слать алерт, только если провалятся все попытки подряд.')}
          />
        </label>
        <label className="field">
          <span>{t('Алерт после N сбоев подряд')}</span>
          <input
            type="number"
            value={form.alert_after_failures ?? 2}
            onChange={(e) => set({ alert_after_failures: num(e.target.value) })}
            title={t('Сколько «плохих» проверок подряд до отправки алерта (гасит редкие флапы).')}
          />
        </label>
        <label className="field">
          <span>{t('Алерт «медленно» после N подряд')}</span>
          <input
            type="number"
            value={form.degraded_after_failures ?? 10}
            onChange={(e) => set({ degraded_after_failures: num(e.target.value) })}
            title={t('Отдельный, обычно больший порог для деградации (медленных ответов) — она шумнее падения.')}
          />
        </label>
        {form.type === 'http' && (
          <>
            <label className="field">
              <span>{t('Ожидаемый статус')}</span>
              <input
                value={form.expected_status ?? '200-399'}
                onChange={(e) => set({ expected_status: e.target.value })}
                placeholder="200-399"
              />
            </label>
            <label className="field">
              <span>{t('Слово в ответе (должно быть)')}</span>
              <input
                value={form.keyword_up ?? ''}
                onChange={(e) => set({ keyword_up: e.target.value })}
              />
            </label>
            <label className="field">
              <span>{t('Слово-стоп (не должно быть)')}</span>
              <input
                value={form.keyword_down ?? ''}
                onChange={(e) => set({ keyword_down: e.target.value })}
              />
            </label>
            <label className="field">
              <span>{t('Аутентификация')}</span>
              <select
                value={form.auth_method || ''}
                onChange={(e) => set({ auth_method: e.target.value })}
              >
                <option value="">{t('Нет')}</option>
                <option value="basic">{t('HTTP Basic (логин/пароль)')}</option>
              </select>
            </label>
            {form.auth_method === 'basic' && (
              <>
                <label className="field">
                  <span>{t('Логин')}</span>
                  <input
                    value={form.auth_user ?? ''}
                    onChange={(e) => set({ auth_user: e.target.value })}
                    autoComplete="off"
                  />
                </label>
                <label className="field">
                  <span>{t('Пароль')}</span>
                  <div className="pass-row">
                    <input
                      type={showPass ? 'text' : 'password'}
                      value={form.auth_pass ?? ''}
                      onChange={(e) => set({ auth_pass: e.target.value })}
                      autoComplete="new-password"
                    />
                    <button
                      type="button"
                      className="ghost icon-btn"
                      onClick={() => setShowPass((v) => !v)}
                      title={showPass ? t('Скрыть') : t('Показать')}
                    >
                      {showPass ? '🙈' : '👁'}
                    </button>
                  </div>
                </label>
              </>
            )}
            <label className="field field-wide">
              <span>{t('HTTP-заголовки (JSON, опционально)')}</span>
              <textarea
                className="headers-input mono"
                rows={3}
                value={form.http_headers ?? ''}
                onChange={(e) => set({ http_headers: e.target.value })}
                placeholder={'{"x-application-token": "…"}'}
                spellCheck={false}
              />
              <span className={`field-hint small ${headersInvalid ? 'form-error' : 'muted'}`}>
                {headersInvalid
                  ? t('⚠ Невалидный JSON — заголовки не применятся.')
                  : t('Доп. заголовки к запросу (напр. токен), если сайт без них отдаёт 401.')}
              </span>
            </label>
          </>
        )}
      </div>

      {form.type === 'http' && (
        <div className="expiry-opts">
          <div className="expiry-opt">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={form.check_ssl ?? true}
                onChange={(e) => set({ check_ssl: e.target.checked })}
              />
              {t('Следить за SSL-сертификатом (валидность и срок)')}
            </label>
            {(form.check_ssl ?? true) && (
              <label className="inline-days">
                <span>{t('напоминать за, дн.')}</span>
                <input
                  value={sslTxt}
                  onChange={(e) => {
                    setSslTxt(e.target.value)
                    set({ ssl_warn_days: parseDays(e.target.value) })
                  }}
                  placeholder="14, 7, 1"
                />
              </label>
            )}
          </div>
          <div className="expiry-opt">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={form.check_domain ?? true}
                onChange={(e) => set({ check_domain: e.target.checked })}
              />
              {t('Следить за сроком регистрации домена')}
            </label>
            {(form.check_domain ?? true) && (
              <label className="inline-days">
                <span>{t('напоминать за, дн.')}</span>
                <input
                  value={domTxt}
                  onChange={(e) => {
                    setDomTxt(e.target.value)
                    set({ domain_warn_days: parseDays(e.target.value) })
                  }}
                  placeholder="7, 1"
                />
              </label>
            )}
          </div>
        </div>
      )}
      {form.type === 'http' && (
        <label className="field field-wide">
          <span>{t('Проверять с сервера (сайт закрыт снаружи)')}</span>
          <select
            value={form.probe_server_id ?? ''}
            onChange={(e) =>
              set({ probe_server_id: e.target.value === '' ? null : Number(e.target.value) })
            }
          >
            <option value="">{t('снаружи, как обычно')}</option>
            {srvs.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <span className="field-hint muted small">
            {t('Агент на выбранном сервере постучится на localhost с этим именем хоста и пришлёт результат. Панель к сайту ходить не будет: снаружи он всё равно закрыт. Это проверка изнутри — она не докажет, что сайт виден посетителям. Белый список сайта должен пускать 127.0.0.1, иначе проверка получит обрыв.')}
          </span>
        </label>
      )}
      {form.type === 'http' && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.ignore_tls ?? false}
            onChange={(e) => set({ ignore_tls: e.target.checked })}
          />
          {t('Игнорировать ошибки TLS/SSL (самоподписанный / истёкший / чужой хост)')}
        </label>
      )}
      {form.type === 'http' && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.check_all_ips ?? false}
            onChange={(e) => set({ check_all_ips: e.target.checked })}
          />
          {t('Проверять все IP-адреса домена (ловит мёртвый бэкенд за балансировщиком)')}
        </label>
      )}
      {form.type === 'http' && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.check_locations ?? false}
            onChange={(e) => set({ check_locations: e.target.checked })}
          />
          {t('Проверять из локаций (прокси)')}
        </label>
      )}
      {form.type === 'http' && (form.check_locations ?? false) && (
        <div className="loc-pick">
          {allLocs.length === 0 ? (
            <div className="muted small">{t('Локаций пока нет — добавьте в ⚙ → Локации.')}</div>
          ) : (
            <>
              <div className="muted small">{t('Из каких локаций проверять:')}</div>
              {allLocs.map((l) => (
                <label className="checkbox loc-pick-item" key={l.id}>
                  <input
                    type="checkbox"
                    checked={locChecked(l.id)}
                    onChange={() => toggleLoc(l.id)}
                  />
                  {l.name}
                  {!l.url && <span className="type-chip">{t('напрямую')}</span>}
                  {!l.enabled && <span className="type-chip off">{t('выкл')}</span>}
                </label>
              ))}
            </>
          )}
        </div>
      )}
      <div className="mute-group">
        <span className="muted small">{t('Не слать алерты этого монитора:')}</span>
        <div className="mute-chips">
          {MON_ALERT_KINDS.map(({ k, label }) => {
            const mutes = form.alert_mutes ?? []
            const muted = mutes.includes(k)
            return (
              <button
                key={k}
                type="button"
                className={`mute-chip${muted ? ' mute-chip-on' : ''}`}
                onClick={() =>
                  set({
                    alert_mutes: muted ? mutes.filter((x) => x !== k) : [...mutes, k],
                  })
                }
              >
                {muted ? '🔕 ' : ''}
                {t(label)}
              </button>
            )
          })}
        </div>
      </div>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={form.enabled ?? true}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        {t('Включён')}
      </label>
      <div className="modal-actions">
        <button className="ghost" onClick={onCancel}>
          {t('Отмена')}
        </button>
        <button onClick={onSubmit} disabled={!canSubmit}>
          {busy ? t('…') : editing ? t('Сохранить') : t('Добавить')}
        </button>
      </div>
    </div>
  )
}
