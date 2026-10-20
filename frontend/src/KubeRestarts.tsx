import { useState } from 'react'
import { updateServer, type KubeLast, type KubeRestartPod, type KubeRestarts, type Server } from './api'
import { useI18n } from './i18n'
import { shortList } from './serverUtils'

// Раздел "Перезапуски" окна кластера: сюда ведет алерт kube_restarts. У каждого пода, который
// перезапускался за сутки, - когда, как кончился прошлый запуск (OOMKilled при лимите, убит по
// пробе, exit 1 и строка лога), лог прошлого запуска и готовые команды kubectl. Пачки (поды
// разных ворклоадов разом) - отдельным блоком: это беда ноды или kube-api, а не подов.

// с этой версии агент присылает прошлое завершение пода и умеет лог прошлого запуска (logs_prev)
export const PREV_LOGS_AGENT = '2.43'
const BURSTS_MUTE = 'restarts:bursts'

function verAtLeast(a: string | undefined | null, b: string): boolean {
  if (!a) return false
  const pa = a.split('.').map((x) => parseInt(x, 10) || 0)
  const pb = b.split('.').map((x) => parseInt(x, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0)
  }
  return true
}

const hhmm = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// kubectl так, как он есть на ноде: у k0s и microk8s - своей подкомандой
export function kubectlOf(flavor?: string): string {
  return flavor === 'k0s' ? 'sudo k0s kubectl' : flavor === 'microk8s' ? 'microk8s kubectl' : 'kubectl'
}

function CopyCmd({ text }: { text: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <div className="agent-advice-cmd">
      <pre>{text}</pre>
      <button
        className="ghost small"
        onClick={() => {
          navigator.clipboard?.writeText(text)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? t('Скопировано') : t('Копировать')}
      </button>
    </div>
  )
}

// подробности прошлого завершения, которых нет в одной строке причины
function lastDetails(l: KubeLast, t: (k: string, p?: Record<string, string | number>) => string): string[] {
  const out = [t('контейнер {c}', { c: l.c })]
  if (l.r) out.push(`${l.r}, exit ${l.x}`)
  if (l.t) out.push(t('завершился в {t}', { t: hhmm(new Date(l.t * 1000)) }))
  if (l.lim && l.r !== 'OOMKilled') out.push(t('лимит памяти {v}', { v: l.lim }))
  return out
}

function RestartRow({
  server: s,
  p,
  flavor,
  canAct,
  busy,
  onPrevLogs,
  onMute,
}: {
  server: Server
  p: KubeRestartPod
  flavor?: string
  canAct: boolean
  busy: boolean
  onPrevLogs: () => void
  onMute: (on: boolean) => void
}) {
  const { t } = useI18n()
  const hot = p.alert && !p.muted
  const tone = hot ? 't-degraded' : 't-up'
  const times = p.times.slice().reverse()
  const kc = kubectlOf(flavor)
  const c = p.last?.c ? ` -c ${p.last.c}` : ''
  const cmds = `${kc} -n ${p.ns} logs ${p.name}${c} --previous --tail 100\n${kc} -n ${p.ns} describe pod ${p.name}`
  const prevOk = verAtLeast(s.agent_version, PREV_LOGS_AGENT)
  return (
    <div className={`loc-res docker-row job-row ${tone}`}>
      <div className="job-main">
        <span className={`lk-dot ${hot ? 'lk-dot-warn' : 'lk-dot-ok'}`} />
        <div className="docker-c-main">
          <div className="docker-c-name mono">
            {p.name}
            <span className="muted small"> · {p.ns}</span>
            <span className={`type-chip${hot ? ' t-degraded' : ''}`}>⟳ {t('{d} за сутки', { d: p.n })}</span>
            {p.solo < p.n && (
              <span className="type-chip" title={t('Перезапуски вместе с подами других ворклоадов: беда ноды или kube-api, в счет пода не идут')}>
                {t('в пачках: {n}', { n: p.n - p.solo })}
              </span>
            )}
            {p.muted && <span className="type-chip">🔕 {t('не алертит')}</span>}
          </div>
          <div className="docker-c-img mono muted small">
            {t('когда: {list}', { list: shortList(times.map((x) => hhmm(new Date(x))), t, 12) })}
          </div>
        </div>
        <div className={`docker-c-status mono small ${tone}`}>
          {hot ? t('повод для алерта') : p.alert ? t('заглушен') : p.kind === 'exit0' ? t('плановый') : t('реже порога')}
        </div>
      </div>
      <div className="job-log">
        {p.why ? (
          <div className="small">
            <b>{t('Почему:')}</b> {p.why}
          </div>
        ) : (
          <div className="muted small">
            {prevOk
              ? t('Причину прошлого завершения агент еще не прислал.')
              : t('Причину прошлого завершения присылает агент {v} и новее, у этой ноды {cur}. Пока смотрите командами ниже.', {
                  v: PREV_LOGS_AGENT,
                  cur: s.agent_version || '?',
                })}
          </div>
        )}
        {p.last && <div className="muted small mono">{lastDetails(p.last, t).join(' · ')}</div>}
        {p.last?.log && (!p.why.includes(p.last.log.slice(0, 40)) || p.last.log.length > 140) && (
          <pre className="web-err-lines mono">{p.last.log}</pre>
        )}
        {p.last?.probe && p.last.pmsg && p.last.pmsg.length > 100 && <pre className="web-err-lines mono">{p.last.pmsg}</pre>}
        <div className="job-log-head small">
          {prevOk && s.online ? (
            <button className="ghost small" onClick={onPrevLogs}>
              📄 {t('Лог прошлого запуска')}
            </button>
          ) : (
            <span className="muted">{t('Лог прошлого запуска и описание пода:')}</span>
          )}
          {canAct && (p.alert || p.muted) && (
            <button className="ghost small" disabled={busy} onClick={() => onMute(!p.muted)}>
              {p.muted ? t('Снова алертить') : `🔕 ${t('Не алертить о перезапусках')}`}
            </button>
          )}
        </div>
        <CopyCmd text={cmds} />
      </div>
    </div>
  )
}

export function KubeRestartsSec({
  server: s,
  data,
  flavor,
  canAct,
  onPrevLogs,
  onChanged,
}: {
  server: Server
  data: KubeRestarts
  flavor?: string
  canAct: boolean
  onPrevLogs: (p: KubeRestartPod) => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const mutes = s.alert_mutes ?? []
  const setMute = async (key: string, on: boolean) => {
    setBusy(key)
    setErr(null)
    try {
      await updateServer(s.id, { alert_mutes: on ? [...new Set([...mutes, key])] : mutes.filter((k) => k !== key) })
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(null)
    }
  }
  const bursts = data.bursts.slice().reverse()
  const burstsHot = bursts.length >= data.burst_min
  const burstsMuted = mutes.includes(BURSTS_MUTE)
  return (
    <div className="srv-sec">
      <p className="muted small kube-hint">
        {t('Поды, которые перезапускались за сутки: когда и как кончился прошлый запуск. В Telegram - OOMKilled от 3 раз за сутки, падения и убийства по пробе от 5 раз (не считая пачек), пачки от {n} за сутки; выход с кодом 0 - плановый, только здесь.', { n: data.burst_min })}
      </p>
      {bursts.length > 0 && (
        <div className={burstsHot && !burstsMuted ? 'net-prob' : 'net-note'}>
          <div>
            {t('Пачек за сутки: {n}. Поды разных ворклоадов перезапускались разом, и это подвисание ноды: в эти минуты на ней было давление или поды упали по таймауту пробы и kube-api (leader election lost). Чинить ноду, а не поды: нагрузка, память, диск под etcd. Плановый выход с кодом 0 и заглушенные поды в пачки не идут.', { n: bursts.length })}
          </div>
          <div className="mono small kube-bursts">
            {bursts.slice(0, 8).map((b) => (
              <div key={b.t}>
                {hhmm(new Date(b.t))} - {shortList(b.pods.map((x) => x.split('/')[1] ?? x), t, 4)}
                {b.psi && ` (${t('ждали: CPU {c}%, память {m}%, диск {d}%', {
                  c: Math.round(b.psi.c),
                  m: Math.round(Math.max(b.psi.ms, b.psi.mf)),
                  d: Math.round(Math.max(b.psi.is, b.psi.if)),
                })})`}
              </div>
            ))}
          </div>
          {canAct && burstsHot && (
            <button className="ghost small" disabled={busy === BURSTS_MUTE} onClick={() => setMute(BURSTS_MUTE, !burstsMuted)}>
              {burstsMuted ? t('Снова алертить о пачках') : `🔕 ${t('Не алертить о пачках')}`}
            </button>
          )}
        </div>
      )}
      {err && <p className="form-error">{err}</p>}
      <div className="loc-results docker-clist">
        {data.pods.map((p) => (
          <RestartRow
            key={`${p.ns}/${p.name}`}
            server={s}
            p={p}
            flavor={flavor}
            canAct={canAct}
            busy={busy === p.mute}
            onPrevLogs={() => onPrevLogs(p)}
            onMute={(on) => setMute(p.mute, on)}
          />
        ))}
      </div>
    </div>
  )
}
