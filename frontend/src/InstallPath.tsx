import { useEffect, useRef, useState } from 'react'
import { installVia, listLocations, listServers, type Location, type Server } from './api'
import { useI18n } from './i18n'
import { isProxyUrl } from './installProxy'

// Как агент пойдет в панель: напрямую, через сервер-ретранслятор, через локацию или через свой
// прокси. Ретранслятор и локацию знает панель: адрес прокси с логином приходит от нее
// (install-via), и сервер сразу попадает в список адресов, которые ретранслятор пускает. Свой
// прокси панель не видит вовсе: он дописывается в команду здесь же (installProxy).
export type InstallMode = 'direct' | 'relay' | 'location' | 'custom'

const MODES: { k: InstallMode; label: string }[] = [
  { k: 'direct', label: 'Напрямую' },
  { k: 'relay', label: 'Через ретранслятор' },
  { k: 'location', label: 'Через локацию' },
  { k: 'custom', label: 'Свой прокси' },
]

const RELAY_STATE: Record<string, string> = {
  ok: 'работает',
  no_helper: 'helper не поставлен',
  waiting: 'helper еще не выходил на связь',
  error: 'ошибка',
  stale: 'давно не отвечал',
}

export function InstallPath({
  server,
  initial = 'direct',
  onProxy,
}: {
  server: Server
  initial?: InstallMode
  onProxy: (proxy: string) => void
}) {
  const { t } = useI18n()
  const [mode, setMode] = useState<InstallMode>(initial)
  const [relays, setRelays] = useState<Server[]>([])
  const [locs, setLocs] = useState<Location[]>([])
  const [relayId, setRelayId] = useState<number | null>(server.relay_via_id ?? null)
  const [locId, setLocId] = useState<number | null>(null)
  const [ip, setIp] = useState(server.agent_ip || server.external_ip || '')
  const [ipApplied, setIpApplied] = useState(ip)
  const [custom, setCustom] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // через ретранслятор уже ходил (до окна или выбран в нем): уход с него снимает адрес из его списка
  const viaRelay = useRef(!!server.relay_via_id)

  useEffect(() => {
    listServers()
      .then((list) => setRelays(list.filter((s) => s.relay_enabled && s.id !== server.id)))
      .catch(() => {})
    listLocations()
      .then((list) => setLocs(list.filter((l) => l.enabled && l.url.trim())))
      .catch(() => {})
  }, [server.id])

  // единственный вариант выбирается сам
  useEffect(() => {
    if (relayId == null && relays.length === 1) setRelayId(relays[0].id)
  }, [relays, relayId])
  useEffect(() => {
    if (locId == null && locs.length === 1) setLocId(locs[0].id)
  }, [locs, locId])

  const ipBad = ipApplied.trim() !== '' && !/^[0-9a-fA-F.:]+$/.test(ipApplied.trim())
  const customBad = custom.trim() !== '' && !isProxyUrl(custom)

  // свой прокси панели не нужен вовсе: подставляется в команду сразу, по мере ввода
  const ownProxy = useRef('')
  ownProxy.current = mode === 'custom' && !customBad ? custom.trim() : ''
  useEffect(() => {
    let gone = false
    setErr('')
    // use - что подставить в команду из ответа: адрес ретранслятора или локации, а для
    // "напрямую" и своего прокси - свое (панель отвечает пустым адресом)
    const ask = async (body: Parameters<typeof installVia>[1], use: (p: string) => string) => {
      setBusy(true)
      try {
        const res = await installVia(server.id, body)
        if (!gone) onProxy(use(res.proxy))
        viaRelay.current = body.mode === 'relay'
      } catch (e) {
        if (!gone) {
          onProxy(use(''))
          setErr(e instanceof Error ? e.message : t('Ошибка'))
        }
      } finally {
        if (!gone) setBusy(false)
      }
    }
    const ipOk = ipApplied.trim() !== '' && !ipBad
    if (mode === 'relay') {
      if (relayId == null || !ipOk) onProxy('')
      else ask({ mode: 'relay', relay_id: relayId, ip: ipApplied.trim() }, (p) => p)
    } else if (mode === 'location') {
      if (locId == null || ipBad) onProxy('')
      else ask({ mode: 'location', location_id: locId, ip: ipApplied.trim() }, (p) => p)
    } else if (viaRelay.current || (mode === 'custom' && ipOk)) {
      // уход с ретранслятора снимает адрес из его списка; IP для своего прокси тоже запоминаем
      ask({ mode, ip: mode === 'custom' && ipOk ? ipApplied.trim() : '' }, () => ownProxy.current)
    } else {
      onProxy(ownProxy.current)
    }
    return () => {
      gone = true
    }
    // onProxy и t - стабильные по смыслу, перезапрос по ним не нужен; свой прокси - эффектом ниже,
    // иначе каждая буква в поле ходила бы в панель
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, relayId, locId, ipApplied, ipBad, server.id])

  useEffect(() => {
    // свой прокси: по мере ввода, без панели
    if (mode === 'custom') onProxy(ownProxy.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [custom, customBad, mode])

  const needIp = mode === 'relay' || mode === 'location'
  return (
    <div className="install-path">
      <div className="muted small">{t('Как агент пойдет в панель')}</div>
      <div className="win-switch install-path-modes">
        {MODES.map((m) => (
          <button
            key={m.k}
            className={`win-btn${mode === m.k ? ' win-btn-active' : ''}`}
            onClick={() => setMode(m.k)}
          >
            {t(m.label)}
          </button>
        ))}
      </div>
      {mode === 'direct' && (
        <p className="muted small install-alt-hint">
          {t('Агент ходит в панель сам. Если адрес сервера режут на пути к панели (ответ панели не доходит), выберите ретранслятор или локацию.')}
        </p>
      )}
      {mode === 'relay' &&
        (relays.length ? (
          <label className="field">
            <span>{t('Ретранслятор')}</span>
            <select value={relayId ?? ''} onChange={(e) => setRelayId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">{t('— выберите —')}</option>
              {relays.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} · {t(RELAY_STATE[r.relay?.state ?? ''] ?? 'helper не поставлен')}
                </option>
              ))}
            </select>
            <span className="field-hint muted small">
              {t('Сервер сразу попадает в список адресов, которые ретранслятор пускает: helper откроет ему порт в течение минуты.')}
            </span>
          </label>
        ) : (
          <p className="muted small install-alt-hint">
            {t('Ретрансляторов нет. Включите "Ретранслятор для агентов" в настройках сервера, который сам достает до панели.')}
          </p>
        ))}
      {mode === 'location' &&
        (locs.length ? (
          <label className="field">
            <span>{t('Локация')}</span>
            <select value={locId ?? ''} onChange={(e) => setLocId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">{t('— выберите —')}</option>
              {locs.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                  {l.exit_country ? ` · ${l.exit_country}` : ''}
                </option>
              ))}
            </select>
            <span className="field-hint muted small">
              {t('Через прокси локации. IP сервера должен быть разрешен в самом прокси, если он пускает не всех.')}
            </span>
          </label>
        ) : (
          <p className="muted small install-alt-hint">{t('Локаций с адресом прокси нет: их заводят в окне Локации.')}</p>
        ))}
      {mode === 'custom' && (
        <label className="field">
          <span>{t('Прокси')}</span>
          <input
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="http://user:password@proxy.example.com:3128"
            autoComplete="off"
            spellCheck={false}
          />
          <span className={`field-hint small ${customBad ? 'form-error' : 'muted'}`}>
            {customBad
              ? t('Нужен вида http://логин:пароль@хост:порт (подходят и https://, socks5://), без пробелов и кавычек.')
              : t('Он попадет в команду и в конфиг агента на сервере, панель его не хранит.')}
          </span>
        </label>
      )}
      {(needIp || mode === 'custom') && (
        <label className="field">
          <span>
            {t('Внешний IP сервера')}
            {mode === 'relay' ? '' : ` (${t('опционально')})`}
          </span>
          <input
            value={ip}
            onChange={(e) => setIp(e.target.value)}
            onBlur={() => setIpApplied(ip)}
            onKeyDown={(e) => e.key === 'Enter' && setIpApplied(ip)}
            placeholder="203.0.113.10"
            spellCheck={false}
          />
          <span className={`field-hint small ${ipBad ? 'form-error' : 'muted'}`}>
            {ipBad
              ? t('IP сервера указан неверно')
              : t('Через прокси панель видит адрес прокси, а не сервера: страна и проверки снаружи идут по этому адресу.')}
          </span>
        </label>
      )}
      {busy && <p className="muted small">{t('Подставляю в команду…')}</p>}
      {err && <p className="form-error small">{t(err)}</p>}
    </div>
  )
}
