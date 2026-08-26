import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  agentUpdate,
  agentUpdateCancel,
  ApiError,
  backupCommand,
  backupCommandStatus,
  createServer,
  deleteServer,
  getAgentRelease,
  listServers,
  serverMetrics,
  serverOomEvents,
  snoozeServer,
  snoozeServerAlert,
  updateServer,
  type ClockInfo,
  type HelperAdvice,
  type OomEvent,
  type ProcStat,
  type Server,
  type ServerEnroll,
  type ServerMetric,
} from './api'
import { StackedAreaChart, type Series } from './charts/StackedAreaChart'
import { fmtSetupVersion, srvIssues } from './serverUtils'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { currentLang, useI18n } from './i18n'
import { fmtBytes, rateUnits, timeUnits } from './units'
import {
  MuteChip,
  MutesBanner,
  SNOOZE_KINDS,
  SRV_ALERT_KINDS,
  collectMutes,
} from './mutes'
import { useAuth } from './auth'

type MetricKey =
  | 'cpu'
  | 'cores'
  | 'freq'
  | 'temp'
  | 'throttle'
  | 'mem'
  | 'oom'
  | 'swap'
  | 'memwb'
  | 'net'
  | 'netifrx'
  | 'netiftx'
  | 'neterr'
  | 'diskio'
  | 'diskiops'
  | 'diskutil'
  | 'disklat'
  | 'disktemp'
  | 'disk'
  | 'conntrack'
  | 'sockets'

// приглушённая палитра (Datadog-стиль) для overlay-графиков (ядра/интерфейсы/диски).
// Десатурированные тона: не мутнеют при наложении, различимы на 8+ линиях.
const CORE_COLORS = [
  '#5a8fc7', '#57a894', '#cf9b52', '#c77b95', '#8a7fb8', '#5fb0ad',
  '#d0796b', '#8faa5f', '#b58fb8', '#c98f5a', '#7f9bd0', '#6faf94',
]
// максимум линий/строк на overlay-графиках по сущностям (интерфейсы/диски) —
// при 20 интерфейсах график и readout не «распидарасит»: показываем топ-N по пику,
// остальное сворачиваем в «+N ещё».
const MAX_ENTITIES = 8
// стабильный цвет по имени сущности (хеш) — чтобы линия графика и строка readout
// совпадали по цвету независимо от порядка/капа
function hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return Math.abs(h)
}
function entColor(name: string, palette: string[]): string {
  return palette[hashStr(name) % palette.length]
}
// топ-N имён по пиковому значению за окно (для капа overlay-графиков)
function topEntities<T>(
  M: ServerMetric[],
  pick: (m: ServerMetric) => T[] | null,
  nameOf: (x: T) => string,
  valOf: (x: T) => number,
): string[] {
  const peak = new Map<string, number>()
  for (const m of M)
    for (const it of pick(m) ?? [])
      peak.set(nameOf(it), Math.max(peak.get(nameOf(it)) ?? 0, valOf(it)))
  return [...peak.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, MAX_ENTITIES)
    .map((e) => e[0])
    .sort()
}
type MetricChart = {
  key: MetricKey
  title: string
  ts: number[]
  series: Series[]
  mode?: 'stack' | 'mirror' | 'overlay'
  yMax?: number
  fmtY: (v: number) => string
  fmtV: (v: number) => string
}
const DISK_PALETTE = ['#5a8fc7', '#4fa79a', '#cf9b52', '#8a7fb8', '#c77b95', '#57a894']

// Строит конфиг графика метрики из массива снимков — общий для детали и полноэкрана.
function buildMetric(
  key: MetricKey,
  M: ServerMetric[],
  t: (s: string) => string,
): MetricChart {
  const ts = M.map((m) => new Date(m.ts).getTime())
  const pctY = (v: number) => `${Math.round(v)}%`
  const pctV = (v: number) => `${v.toFixed(1)}%`
  if (key === 'cpu')
    return {
      // overlay, а не стек: каждая метрика рисуется ОТ НУЛЯ — её линия на реальной
      // высоте (iowait 18% = линия на 18%). В стеке полоса стоит на сумме предыдущих
      // и читается ложно «высокой». Итог по CPU и так есть в плитке сверху.
      key, title: `CPU · ${t('состав нагрузки')}`, ts, mode: 'overlay', yMax: 100,
      fmtY: pctY, fmtV: pctV,
      series: [
        { name: t('система'), color: '#8a7fb8', values: M.map((m) => m.cpu_system) },
        { name: t('юзер'), color: '#5a8fc7', values: M.map((m) => m.cpu_user) },
        { name: 'iowait', color: '#cf9b52', values: M.map((m) => m.cpu_iowait) },
        { name: 'irq', color: '#c77b95', values: M.map((m) => m.cpu_irq) },
      ],
    }
  if (key === 'cores') {
    const nc = M.reduce((mx, m) => Math.max(mx, m.cpu_cores_pct?.length ?? 0), 0)
    return {
      key, title: `CPU · ${t('по ядрам')}`, ts, mode: 'overlay', yMax: 100, fmtY: pctY, fmtV: pctV,
      series: Array.from({ length: nc }, (_, i) => ({
        name: `#${i}`,
        color: CORE_COLORS[i % CORE_COLORS.length],
        values: M.map((m) => m.cpu_cores_pct?.[i] ?? null),
      })),
    }
  }
  if (key === 'freq')
    return {
      key, title: `CPU · ${t('частота')}`, ts, mode: 'overlay', fmtY: fmtMHz, fmtV: fmtMHz,
      series: [{ name: t('частота'), color: '#5a8fc7', values: M.map((m) => m.cpu_freq) }],
    }
  if (key === 'temp')
    return {
      key, title: `CPU · ${t('температура')}`, ts, mode: 'overlay', fmtY: fmtTempC, fmtV: fmtTempC,
      series: [{ name: t('температура'), color: '#cf9b52', values: M.map((m) => m.cpu_temp) }],
    }
  if (key === 'throttle')
    return {
      key, title: `CPU · ${t('троттлинг')}`, ts, mode: 'overlay',
      fmtY: (v) => String(Math.round(v)), fmtV: (v) => String(Math.round(v)),
      series: [{ name: t('троттлинг'), color: '#d06b6b', values: M.map((m) => m.cpu_throttle) }],
    }
  if (key === 'oom')
    return {
      key, title: `${t('Память')} · ${t('OOM-киллы')}`, ts, mode: 'overlay',
      fmtY: (v) => String(Math.round(v)), fmtV: (v) => String(Math.round(v)),
      series: [{ name: t('OOM-киллы'), color: '#d06b6b', values: M.map((m) => m.oom_kill) }],
    }
  if (key === 'mem')
    return {
      key, title: t('Память'), ts, yMax: 100, fmtY: pctY, fmtV: pctV,
      series: [
        {
          name: t('занято'), color: '#5a8fc7',
          values: M.map((m) =>
            m.mem_free != null && m.mem_cache != null
              ? Math.max(0, 100 - m.mem_free - m.mem_cache)
              : m.mem_percent,
          ),
        },
        { name: t('кэш/буфер'), color: '#4fa79a', values: M.map((m) => m.mem_cache) },
      ],
    }
  if (key === 'swap')
    return {
      key, title: `Swap · ${t('↓ загрузка / ↑ выгрузка')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ загрузка'), color: '#57a894', values: M.map((m) => m.swap_in) },
        { name: t('↑ выгрузка'), color: '#cf9b52', values: M.map((m) => m.swap_out) },
      ],
    }
  if (key === 'memwb')
    return {
      key, title: `${t('Память')} · ${t('буфер записи')}`, ts, mode: 'overlay',
      fmtY: fmtBytes, fmtV: fmtBytes,
      series: [
        { name: t('ожидают записи'), color: '#cf9b52', values: M.map((m) => m.mem_dirty) },
        { name: t('запись на диск'), color: '#c77b95', values: M.map((m) => m.mem_writeback) },
      ],
    }
  if (key === 'net')
    return {
      key, title: `${t('Сеть')} · ${t('↓ приём / ↑ отдача')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ приём'), color: '#57a894', values: M.map((m) => m.net_rx) },
        { name: t('↑ отдача'), color: '#5a8fc7', values: M.map((m) => m.net_tx) },
      ],
    }
  if (key === 'netifrx' || key === 'netiftx') {
    const tx = key === 'netiftx'
    // общий топ-N набор по пику rx+tx → приём и отдача показывают одни интерфейсы
    const names = topEntities(M, (m) => m.net_ifaces, (x) => x.if, (x) => Math.max(x.rx, x.tx))
    return {
      key, ts, mode: 'overlay', fmtY: fmtRate, fmtV: fmtRate,
      title: `${t('Сеть')} · ${tx ? t('отдача по интерфейсам') : t('приём по интерфейсам')}`,
      series: names.map((n) => ({
        name: n, color: entColor(n, CORE_COLORS),
        values: M.map((m) => {
          const it = m.net_ifaces?.find((x) => x.if === n)
          return it ? (tx ? it.tx : it.rx) : null
        }),
      })),
    }
  }
  if (key === 'neterr') {
    // одна линия на интерфейс = ошибки+дропы/сек (обычно 0 — плоско = здорово)
    const names = topEntities(M, (m) => m.net_ifaces, (x) => x.if, (x) => x.errs + x.drops)
    return {
      key, ts, mode: 'overlay', fmtY: fmtErr, fmtV: fmtErr,
      title: `${t('Сеть')} · ${t('ошибки/дропы по интерфейсам')}`,
      series: names.map((n) => ({
        name: n, color: entColor(n, CORE_COLORS),
        values: M.map((m) => {
          const it = m.net_ifaces?.find((x) => x.if === n)
          return it ? it.errs + it.drops : null
        }),
      })),
    }
  }
  if (key === 'diskutil' || key === 'disklat') {
    const lat = key === 'disklat'
    const names = topEntities(M, (m) => m.disk_devs, (x) => x.dev, (x) => x.util)
    return {
      key, ts, mode: 'overlay', yMax: lat ? undefined : 100,
      title: `${t('Диск')} · ${lat ? t('задержка (await)') : t('загрузка (%util)')}`,
      fmtY: lat ? fmtMs : pctY, fmtV: lat ? fmtMs : pctV,
      series: names.map((n) => ({
        name: n, color: entColor(n, DISK_PALETTE),
        values: M.map((m) => {
          const it = m.disk_devs?.find((x) => x.dev === n)
          return it ? (lat ? it.await : it.util) : null
        }),
      })),
    }
  }
  if (key === 'disktemp') {
    // только устройства с датчиком (drivetemp/nvme); без датчика (VM) — карточка скрыта
    const names = [
      ...new Set(M.flatMap((m) => (m.disk_devs ?? []).filter((x) => x.temp != null).map((x) => x.dev))),
    ].sort()
    return {
      key, ts, mode: 'overlay', fmtY: fmtTempC, fmtV: fmtTempC,
      title: `${t('Диск')} · ${t('температура')}`,
      series: names.map((n) => ({
        name: n, color: entColor(n, DISK_PALETTE),
        values: M.map((m) => m.disk_devs?.find((x) => x.dev === n)?.temp ?? null),
      })),
    }
  }
  if (key === 'conntrack')
    return {
      key, ts, mode: 'overlay', fmtY: fmtNum, fmtV: fmtNum,
      title: `conntrack · ${t('соединения')}`,
      series: [{ name: 'conntrack', color: '#5a8fc7', values: M.map((m) => m.conntrack_count) }],
    }
  if (key === 'sockets')
    return {
      key, ts, mode: 'overlay', fmtY: fmtNum, fmtV: fmtNum,
      title: t('Сокеты'),
      series: [
        { name: 'TCP', color: '#5a8fc7', values: M.map((m) => m.sock_tcp) },
        { name: 'time-wait', color: '#cf9b52', values: M.map((m) => m.sock_tcp_tw) },
        { name: 'UDP', color: '#57a894', values: M.map((m) => m.sock_udp) },
      ],
    }
  if (key === 'diskio')
    return {
      key, title: `${t('Диск I/O')} · ${t('↓ чтение / ↑ запись')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ чтение'), color: '#57a894', values: M.map((m) => m.disk_read) },
        { name: t('↑ запись'), color: '#cf9b52', values: M.map((m) => m.disk_write) },
      ],
    }
  if (key === 'diskiops')
    return {
      key, title: `${t('Диск IOPS')} · ${t('↓ чтение / ↑ запись')}`, ts, mode: 'mirror',
      fmtY: fmtIops, fmtV: fmtIops,
      series: [
        { name: t('↓ чтение'), color: '#57a894', values: M.map((m) => m.disk_read_iops) },
        { name: t('↑ запись'), color: '#cf9b52', values: M.map((m) => m.disk_write_iops) },
      ],
    }
  // disk: своя ось времени по точкам с дисками; крупные маунты — первыми (позади)
  const pts = M.filter((m) => m.disks && m.disks.length)
  const dmax = new Map<string, number>()
  for (const m of pts) for (const d of m.disks ?? []) dmax.set(d.mount, Math.max(dmax.get(d.mount) ?? 0, d.pct))
  return {
    key, title: `${t('Диск')} · ${t('заполнение по разделам')}`,
    ts: pts.map((m) => new Date(m.ts).getTime()), mode: 'overlay', yMax: 100, fmtY: pctY, fmtV: pctV,
    series: [...dmax.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([mount], i) => ({
        name: mount, color: DISK_PALETTE[i % DISK_PALETTE.length],
        values: pts.map((m) => m.disks?.find((d) => d.mount === mount)?.pct ?? null),
      })),
  }
}

type Props = {
  onUnauthorized: () => void
  openServerId?: number | null
  openServerSec?: string | null
  onConsumed?: () => void
}

function pct(used?: number, total?: number): number | null {
  return total ? Math.round((used! / total) * 100) : null
}
function fmtUptime(s?: number): string {
  if (!s) return '—'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const en = currentLang() === 'en'
  const [D, H, M] = en ? ['d', 'h', 'm'] : ['д', 'ч', 'м']
  return d > 0 ? `${d}${D} ${h}${H}` : h > 0 ? `${h}${H} ${m}${M}` : `${m}${M}`
}
function fmtRel(iso: string | null): string {
  if (!iso) return '—'
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  const ago = (n: number, ru: string, en: string) =>
    currentLang() === 'en' ? `${n} ${en} ago` : `${n} ${ru} назад`
  if (sec < 60) return ago(Math.round(sec), 'с', 's')
  if (sec < 3600) return ago(Math.round(sec / 60), 'мин', 'min')
  if (sec < 86400) return ago(Math.round(sec / 3600), 'ч', 'h')
  return ago(Math.round(sec / 86400), 'д', 'd')
}
function toneOf(p: number | null): string {
  if (p == null) return ''
  return p >= 90 ? 't-down' : p >= 75 ? 't-degraded' : 't-up'
}

// компактный статус времени в шапке детали: цвет по модулю сдвига (норма<5с / предупр<30с /
// проблема). Сдвиг меряет панель (время ноды vs своё на приёме) — работает и без NTP у ноды.
function ClockStatus({ clock, skew }: { clock: ClockInfo; skew?: number }) {
  const { t } = useI18n()
  if (skew === undefined) {
    return <span className={clock.synced ? 't-up' : 't-degraded'}>
      {clock.synced ? t('синхронизировано') : t('не синхронизировано')}
    </span>
  }
  const a = Math.abs(skew)
  const tone = a >= 30 ? 't-down' : a >= 5 ? 't-degraded' : 't-up'
  const mag = a < 120 ? `${a} ${t('с')}` : `${Math.round(a / 60)} ${t('мин')}`
  return <span className={tone}>{a < 5 ? t('синхронизировано') : `${t('сдвиг')} ${mag}`}</span>
}

// команда синхронизации времени (copy-paste). Панель на ноду не лезет — печатает команду,
// оператор выполняет осознанно (шаг часов рискован для БД/приложений). Плюс аварийный
// HTTP-фолбэк на случай закрытого исходящего NTP: шаг часов по времени самой панели.
function ClockFix({ server: s, clock, skew, canManage }: {
  server: Server; clock: ClockInfo; skew?: number; canManage: boolean
}) {
  const { t } = useI18n()
  const [copied, setCopied] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // одноклик доступен, только если на ноде установлен timesync-хелпер (иначе — copy-paste)
  const hasHelper = !!s.last_report?.setup_versions?.['timesync-setup']
  const syncNow = async () => {
    if (!window.confirm(t('Синхронизировать время сейчас? Часы шагнут к точному времени; при большом расхождении это резкий скачок (для БД/приложений заметно).'))) return
    setBusy(true); setMsg(null)
    try {
      const c = await backupCommand(s.id, { action: 'timesync' })
      let last = c
      for (let i = 0; i < 60 && last.status !== 'done' && last.status !== 'error'; i++) {
        await new Promise((r) => setTimeout(r, 500))
        last = await backupCommandStatus(s.id, c.id)
      }
      const ok = last.status !== 'error'
      setMsg({ ok, text: last.result || (ok ? t('готово') : t('не удалось')) })
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : t('ошибка') })
    } finally {
      setBusy(false)
    }
  }
  const svc = (clock.service || '').toLowerCase()
  const main = svc.includes('chrony')
    ? `sudo systemctl enable --now ${clock.service} && sudo chronyc makestep`
    : 'sudo timedatectl set-ntp true'
  const httpFix =
    `sudo date -s "$(curl -sI ${window.location.origin}/ | grep -i '^date:' | cut -d' ' -f2- | tr -d '\\r')"`
  const copy = (txt: string, id: string) => {
    navigator.clipboard?.writeText(txt); setCopied(id); window.setTimeout(() => setCopied(null), 1500)
  }
  const cmdRow = (txt: string, id: string) => (
    <div className="agent-advice-cmd">
      <pre>{txt}</pre>
      <button className="ghost" onClick={() => copy(txt, id)}>
        {copied === id ? t('Скопировано') : t('Копировать')}
      </button>
    </div>
  )
  const off = Math.abs(skew ?? 0)
  return (
    <div className="clock-fix">
      <div className="form-error small">
        🕐 {off >= 5
          ? t('Часы разошлись с панелью на {n} — синхронизируйте время:', {
            n: off < 120
              ? `${off} ${timeUnits()[3]}`
              : `${Math.round(off / 60)} ${currentLang() === 'en' ? 'min' : 'мин'}`,
          })
          : t('Время не синхронизировано — включите NTP:')}
      </div>
      {/* одноклик, если стоит timesync-хелпер: агент шлёт команду, root-хелпер синкает
          (+ HTTP-фолбэк по времени панели, если исходящий NTP закрыт) */}
      {canManage && hasHelper && (
        <div className="clock-sync-row">
          <button className="restic-update-btn" disabled={busy} onClick={syncNow}>
            {busy ? t('синхронизирую…') : `🕐 ${t('Синхронизировать время')}`}
          </button>
          {msg && <span className={`small ${msg.ok ? 't-up' : 'form-error'}`}> · {msg.text}</span>}
        </div>
      )}
      {/* copy-paste — универсальная альтернатива и путь для нод без helper'а */}
      <details className="clock-manual">
        <summary className="muted small">{hasHelper ? t('или вручную') : t('команды для ручной синхронизации')}</summary>
        {cmdRow(main, 'main')}
        {!clock.service && (
          <div className="muted small">{t('Демон синхронизации не найден — команда выше включит systemd-timesyncd.')}</div>
        )}
        <div className="muted small clock-fix-fallback">
          {t('Если исходящий закрыт и до NTP не достучаться (UDP 123): открой порт (напр. sudo ufw allow out 123/udp) — либо разово выставь часы по времени панели (она достижима по HTTPS):')}
        </div>
        {cmdRow(httpFix, 'http')}
        {!hasHelper && (
          <div className="muted small">{t('Кнопка «в один клик» появится после установки timesync-хелпера (ansible kervax_helpers.yml).')}</div>
        )}
      </details>
    </div>
  )
}
function fmtRate(v?: number | null): string {
  const u = rateUnits()
  if (!v) return `0 ${u[0]}`
  let i = 0
  let x = v
  while (x >= 1024 && i < u.length - 1) {
    x /= 1024
    i++
  }
  return `${x.toFixed(x >= 10 || i === 0 ? 0 : 1)} ${u[i]}`
}
function fmtIops(v?: number | null): string {
  if (!v) return '0'
  return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v))
}
function fmtMHz(v?: number | null): string {
  if (!v) return '—'
  const en = currentLang() === 'en'
  return v >= 1000
    ? `${(v / 1000).toFixed(2)} ${en ? 'GHz' : 'ГГц'}`
    : `${Math.round(v)} ${en ? 'MHz' : 'МГц'}`
}
function fmtTempC(v?: number | null): string {
  return v == null ? '—' : `${Math.round(v)}°C`
}
function fmtMs(v?: number | null): string {
  if (v == null) return '—'
  const u = currentLang() === 'en' ? 'ms' : 'мс'
  return `${v >= 10 ? Math.round(v) : v.toFixed(2)} ${u}`
}
// ошибки/дропы (пакетов/сек) — редкие мелкие значения, храним 2 знака
function fmtErr(v?: number | null): string {
  if (!v) return '0'
  return v >= 10 ? String(Math.round(v)) : v.toFixed(2)
}
// компактное целое (сокеты/conntrack): 12 345 → «12.3k»
function fmtNum(v?: number | null): string {
  if (v == null) return '—'
  return v >= 10000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v))
}

const SRV_WINDOWS: { label: string; hours: number }[] = [
  { label: '1ч', hours: 1 },
  { label: '3ч', hours: 3 },
  { label: '6ч', hours: 6 },
  { label: '24ч', hours: 24 },
  { label: '7д', hours: 168 },
  { label: '30д', hours: 720 },
  { label: '90д', hours: 2160 },
]

// Сворачиваемая секция графиков (CPU/Память/Диск/Сеть/…). Состояние — в localStorage
// по id, чтобы масштабироваться на десятки графиков без бесконечной простыни.
function MetricSection({
  id,
  title,
  children,
}: {
  id: string
  title: string
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(
    () => localStorage.getItem(`kervax_sec_${id}`) !== '0',
  )
  const toggle = () => {
    const n = !open
    setOpen(n)
    localStorage.setItem(`kervax_sec_${id}`, n ? '1' : '0')
  }
  return (
    <div className="metric-section" id={`msec-${id}`}>
      <button className="metric-section-head" onClick={toggle}>
        <span className="metric-section-caret">{open ? '▾' : '▸'}</span>
        {title}
      </button>
      {open && <div className="metric-section-body">{children}</div>}
    </div>
  )
}

// Липкая боковая навигация по секциям детали сервера: клик → раскрыть (если
// свёрнута) и проскроллить; активная секция подсвечивается по позиции скролла.
// Какому разделу принадлежит карточка метрики: запасной путь для ссылок из алертов,
// когда самой карточки на этой ноде нет (условный рендер).
const SEC_OF_CARD: Record<string, string> = {
  cpu: 'cpu', throttle: 'cpu', temp: 'cpu',
  mem: 'mem', oom: 'mem',
  conntrack: 'conn', diskfill: 'disk', disktemp: 'disk', disk: 'disk',
}

const DETAIL_SECTIONS = [
  { id: 'cpu', label: 'CPU' },
  { id: 'mem', label: 'Память' },
  { id: 'net', label: 'Сеть' },
  { id: 'conn', label: 'Соединения' },
  { id: 'disk', label: 'Диск' },
  { id: 'proc', label: 'Процессы' },
]
function DetailNav({
  t,
  sections = DETAIL_SECTIONS,
}: {
  t: (s: string) => string
  sections?: { id: string; label: string }[]
}) {
  const [active, setActive] = useState('cpu')
  useEffect(() => {
    const els = sections.map((s) => document.getElementById(`msec-${s.id}`)).filter(
      (e): e is HTMLElement => e != null,
    )
    if (!els.length) return
    const io = new IntersectionObserver(
      (ents) => {
        const vis = ents
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (vis[0]) setActive(vis[0].target.id.replace('msec-', ''))
      },
      { rootMargin: '-8% 0px -70% 0px' },
    )
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [sections])
  const go = (id: string) => {
    const el = document.getElementById(`msec-${id}`)
    if (!el) return
    if (!el.querySelector('.metric-section-body')) {
      ;(el.querySelector('.metric-section-head') as HTMLElement | null)?.click()
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  return (
    <nav className="detail-nav" aria-label={t('Разделы')}>
      {sections.map((s) => (
        <button
          key={s.id}
          className={`detail-nav-item${active === s.id ? ' active' : ''}`}
          onClick={() => go(s.id)}
        >
          {t(s.label)}
        </button>
      ))}
    </nav>
  )
}

// Плавающая кнопка «наверх»: появляется при прокрутке детали, скроллит модалку
// (.modal-backdrop) к началу. Портал в body — иначе backdrop-filter на бэкдропе
// делает его containing-block для position:fixed и кнопка «уезжает» с контентом.
function ScrollTopBtn({ t }: { t: (s: string) => string }) {
  const [show, setShow] = useState(false)
  useEffect(() => {
    const sc = document.querySelector('.modal-backdrop') as HTMLElement | null
    if (!sc) return
    const on = () => setShow(sc.scrollTop > 500)
    sc.addEventListener('scroll', on, { passive: true })
    on()
    return () => sc.removeEventListener('scroll', on)
  }, [])
  const up = () =>
    document.querySelector('.modal-backdrop')?.scrollTo({ top: 0, behavior: 'smooth' })
  return createPortal(
    <button
      className={`scroll-top-btn${show ? '' : ' scroll-top-hidden'}`}
      onClick={up}
      title={t('Наверх')}
      aria-label={t('Наверх')}
    >
      ↑
    </button>,
    document.body,
  )
}

// Пиктограмма адреса: глобус = внешний (публичный) IP, домик = локальный (внутр. сеть).
// Раньше были текстовые метки «внеш»/«лок», но «лок» читалось как «залочен».
function AddrIcon({ kind }: { kind: 'ext' | 'loc' }) {
  return (
    <svg
      className="srv-addr-ic"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === 'ext' ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M3 12h18" />
          <path d="M12 3c2.6 2.6 2.6 15.4 0 18" />
          <path d="M12 3c-2.6 2.6-2.6 15.4 0 18" />
        </>
      ) : (
        <>
          <path d="M3 11l9-7 9 7" />
          <path d="M5 10.5V20h14v-9.5" />
        </>
      )}
    </svg>
  )
}

// Адрес(а) сервера с иконками: внешний (глобус) + локальный (домик, только если отличается).
// Контекстно-нейтрально (обёртка .srv-addr, не chip) — годится и в плашках-метах, и в
// компактной плитке .check-target. Нет ни одного IP → hostname.
function ServerAddr({ s, t }: { s: Server; t: (str: string) => string }) {
  const ext = s.external_ip
  const loc = s.local_ip
  const showLoc = loc && loc !== ext
  return (
    <span className="srv-addr">
      {!ext && !showLoc ? (
        s.hostname || '—'
      ) : (
        <>
          {ext && (
            <span className="srv-addr-part" title={t('внешний (публичный) IP')}>
              <AddrIcon kind="ext" />
              {ext}
            </span>
          )}
          {showLoc && (
            <span className="srv-addr-part" title={t('локальный IP (внутренняя сеть)')}>
              <AddrIcon kind="loc" />
              {loc}
            </span>
          )}
        </>
      )}
    </span>
  )
}

// Строка «реальных» значений под графиком (ГБ / ядра / load) — как список у диска.
function StatRow({ color, name, value }: { color?: string; name: string; value: string }) {
  return (
    <div className="loc-res">
      {color && <span className="mchart-dot" style={{ background: color }} />}
      <div className="loc-res-name">{name}</div>
      <div className="loc-res-msg mono">{value}</div>
    </div>
  )
}

// Readout по сущностям (интерфейсы/диски): сортировка по значению, топ-N + «+N ещё»,
// цвет строки = цвет линии (по хешу имени). Высота ограничена CSS-скроллом.
function EntStats<T>({ items, nameOf, sortVal, render, palette }: {
  items: T[]
  nameOf: (x: T) => string
  sortVal: (x: T) => number
  render: (x: T) => string
  palette: string[]
}) {
  const sorted = [...items].sort((a, b) => sortVal(b) - sortVal(a))
  const shown = sorted.slice(0, MAX_ENTITIES)
  const extra = sorted.length - shown.length
  return (
    <div className="loc-results chart-stats chart-stats-scroll">
      {shown.map((x) => (
        <StatRow
          key={nameOf(x)}
          color={entColor(nameOf(x), palette)}
          name={nameOf(x)}
          value={render(x)}
        />
      ))}
      {extra > 0 && (
        <div className="ent-more muted small">
          +{extra} {currentLang() === 'en' ? 'more' : 'ещё'}
        </div>
      )}
    </div>
  )
}

// «Тепловой» класс строки процесса по нагрузке: CPU — по %; память — по доле от
// общей RAM. crit(красный)/warn(жёлтый)/ok(зелёный)/'' (белый — почти не грузит).
function procTone(kind: 'cpu' | 'mem', cpu: number, rss: number, total: number): string {
  const v = kind === 'cpu' ? cpu : total > 0 ? (rss / total) * 100 : 0
  const [hi, mid, lo] = kind === 'cpu' ? [80, 40, 10] : [40, 20, 8]
  if (v >= hi) return 'proc-crit'
  if (v >= mid) return 'proc-warn'
  if (v >= lo) return 'proc-ok'
  return ''
}

// Список OOM-событий «когда · кого убило» — рендерится ПОД графиком OOM-киллов
// (как extra в chartCard). Пусто → ничего не показываем. Новые сверху.
function OomEventList({
  serverId,
  t,
}: {
  serverId: number
  t: (s: string, vars?: Record<string, string | number>) => string
}) {
  const [events, setEvents] = useState<OomEvent[] | null>(null)
  useEffect(() => {
    let alive = true
    serverOomEvents(serverId, 50)
      .then((e) => alive && setEvents(e))
      .catch(() => alive && setEvents([]))
    return () => {
      alive = false
    }
  }, [serverId])
  if (!events || events.length === 0) return null
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  return (
    <div className="oom-log">
      <div className="oom-log-cap muted small">{t('последние киллы')}</div>
      <div className="oom-log-list">
        {events.map((e, i) => (
          <div key={i} className="oom-log-row">
            <span className="oom-log-victim mono">
              {e.victim || t('процесс неизвестен')}
            </span>
            <span className="oom-log-when muted small mono">
              {fmt(e.ts)}
              {e.count > 1 ? ` · ×${e.count}` : ''}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// Карточка топ-процессов (снапшот): основная метрика крупно, вторая + PID мелко.
// Строки подсвечены по нагрузке (см. procTone). total — общая RAM для % памяти.
// приватная память ≈ RSS − общая (shared_buffers и т.п.). Снимает двойной учёт,
// из-за которого все бэкенды postgres показывали одинаковый гигантский RSS.
function privMem(p: ProcStat): number {
  return Math.max((p.rss ?? 0) - (p.shared ?? 0), 0)
}
// состояние процесса: показываем только «интересные» (не R/S), с расшифровкой.
const PROC_STATE: Record<string, string> = {
  D: 'непрер. сон (I/O)',
  Z: 'зомби',
  T: 'остановлен',
  t: 'под трассировкой',
  X: 'мёртв',
}

function ProcCard({ title, procs, kind, total, t }: {
  title: string
  procs?: ProcStat[]
  kind: 'cpu' | 'mem'
  total: number
  t: (s: string, vars?: Record<string, string | number>) => string
}) {
  return (
    <div className="chart-block chart-card">
      <div className="chart-cap">{title}</div>
      {procs && procs.length ? (
        <div className="loc-results chart-stats chart-stats-scroll">
          {procs.map((p) => {
            const priv = privMem(p)
            const tone = procTone(kind, p.cpu, priv, total)
            // основная метрика: для памяти — приватная; вторичная — cpu/память
            const mainMetric =
              kind === 'cpu' ? `${p.cpu.toFixed(1)}%` : fmtBytes(priv)
            const meta: string[] = [`PID ${p.pid}`]
            if (p.user) meta.push(p.user)
            if (p.threads && p.threads > 1) meta.push(t('{n} пот.', { n: p.threads }))
            // общую RAM показываем только если ощутимая (≥1 МБ) — иначе «+4 КБ общей» лишь шумит
            if (p.shared && p.shared >= 1 << 20) meta.push(t('+{v} общей', { v: fmtBytes(p.shared) }))
            meta.push(kind === 'cpu' ? fmtBytes(priv) : `${p.cpu.toFixed(1)}%`)
            const st = p.state && PROC_STATE[p.state]
            return (
              <div key={p.pid} className={`proc-row loc-res${tone ? ` ${tone}` : ''}`}>
                <div className="proc-head">
                  <span className="proc-comm mono">{p.comm}</span>
                  {st && (
                    <span className="type-chip off" title={t(st)}>
                      {p.state}
                    </span>
                  )}
                  <span className="proc-metric mono">{mainMetric}</span>
                </div>
                {p.cmdline && (
                  <div className="proc-cmd mono muted small" title={p.cmdline}>
                    {p.cmdline}
                  </div>
                )}
                <div className="proc-meta muted small mono">{meta.join(' · ')}</div>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="muted small">{t('нет данных')}</div>
      )}
    </div>
  )
}

function SrvTile({
  num,
  label,
  tone,
  active,
  onClick,
}: {
  num: number
  label: string
  tone?: 'up' | 'down' | 'degraded'
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      className={`stat-tile stat-tile-btn${active ? ' stat-tile-active' : ''}`}
      onClick={onClick}
    >
      <div className={`stat-num${tone ? ` t-${tone}` : ''}`}>{num}</div>
      <div className="stat-lbl">{label}</div>
    </button>
  )
}

function Meter({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="srv-meter">
      <div className="srv-meter-top">
        <span className="muted small">{label}</span>
        <span className={`srv-meter-val ${toneOf(value)}`}>
          {value == null ? '—' : `${value}%`}
        </span>
      </div>
      <div className="srv-bar">
        <span
          className={`srv-bar-fill ${toneOf(value)}`}
          style={{ width: `${value ?? 0}%` }}
        />
      </div>
    </div>
  )
}

export function ServersPage({ onUnauthorized, openServerId, openServerSec, onConsumed }: Props) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [enroll, setEnroll] = useState<ServerEnroll | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)
  // раздел, на который надо промотать деталь: ставится кликом по 🔥
  const [detailSec, setDetailSec] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'online' | 'offline' | 'problem'>(
    'all',
  )
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_srv_groupby') as 'none' | 'group') || 'none',
  )
  const [sortBy, setSortBy] = useState<'name' | 'cpu' | 'ram' | 'disk'>('name')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  // режим массового выбора (чекбоксы + удаление выбранных)
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  // drag-and-drop: перенос сервера в другую группу
  const dragRef = useRef<number | null>(null)
  const busyRef = useRef(false) // пауза автообновления, пока тащим / сохраняем перенос
  const [dragId, setDragId] = useState<number | null>(null)
  const [dragOverKey, setDragOverKey] = useState<string | null>(null)
  const setGrouping = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_srv_groupby', g)
  }
  const toggleCollapse = (k: string) =>
    setCollapsed((cur) => {
      const n = new Set(cur)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })

  const load = useCallback(() => {
    listServers()
      .then((s) => {
        setServers(s)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  useEffect(() => {
    load()
    // не дёргаем список, пока пользователь тащит сервер / сохраняется перенос —
    // иначе оптимистичное состояние мигнёт назад
    const id = window.setInterval(() => {
      if (!busyRef.current) load()
    }, 12000)
    return () => window.clearInterval(id)
  }, [load])

  // раздел из URL (?sec=diskfill) запоминаем ОТДЕЛЬНО: id ниже сразу «съедается»,
  // и сравнивать с ним на отрисовке уже нельзя — раздел терялся, деталь открывалась
  // в самом верху вместо нужной метрики
  const [urlSec, setUrlSec] = useState<string | null>(null)
  // диплинк из алерта/главной (?server=id) → открыть деталь, убрать параметр, «съесть» id
  useEffect(() => {
    if (openServerId) {
      setDetailId(openServerId)
      setUrlSec(openServerSec ?? null)
      window.history.replaceState({}, '', window.location.pathname)
      onConsumed?.()
    }
  }, [openServerId, openServerSec, onConsumed])

  // доступная (подписанная) версия агента для управляемого обновления
  const [avail, setAvail] = useState('')
  const [updBusy, setUpdBusy] = useState(false)
  useEffect(() => {
    getAgentRelease()
      .then((r) => setAvail(r.version))
      .catch(() => setAvail(''))
  }, [])

  // серверы, которые онлайн, отчитались о версии и отстают от доступной
  const behind = (servers ?? []).filter(
    (s) => s.online && s.agent_version && avail && s.agent_version !== avail,
  )
  // серверы с заглушёнными проверками — сводкой над списком, чтобы мьют было видно
  // не открывая карточку (иначе бессрочный мьют теряется молча)
  const muted = (servers ?? [])
    .map((s) => ({ s, items: collectMutes(s, t, ['alert']) }))
    .filter((x) => x.items.length > 0)
  async function rollout(serverIds?: number[]) {
    if (!avail) return
    setUpdBusy(true)
    try {
      const upd = await agentUpdate(avail, serverIds)
      setServers(upd)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setUpdBusy(false)
    }
  }

  const online = servers?.filter((s) => s.online).length ?? 0
  const total = servers?.length ?? 0
  // «проблемные» = оффлайн ИЛИ превышен порог (CPU/RAM/диск/темп/троттлинг/conntrack)
  const problems =
    servers?.filter((s) => srvIssues(s, t).length > 0).length ?? 0
  const detail = detailId != null ? servers?.find((s) => s.id === detailId) : undefined
  const q = query.trim().toLowerCase()
  const visible = (servers ?? []).filter((s) => {
    if (filter === 'online' && !s.online) return false
    if (filter === 'offline' && s.online) return false
    if (filter === 'problem' && srvIssues(s, t).length === 0) return false
    if (!q) return true
    return [s.name, s.group_name, s.external_ip, s.local_ip, s.hostname, s.os]
      .some((v) => v?.toLowerCase().includes(q))
  })
  // сырое (неокруглённое) значение метрики — чтобы близкие значения не «ничьились»
  const metric = (s: Server): number | null => {
    const rep = s.last_report
    if (!rep) return null
    if (sortBy === 'cpu') return rep.cpu_percent ?? null
    if (sortBy === 'ram')
      return rep.mem_total ? (rep.mem_used ?? 0) / rep.mem_total : null
    const d = rep.disks
    return d?.length
      ? Math.max(...d.filter((x) => x.total).map((x) => x.used / x.total))
      : null
  }
  const sorted =
    sortBy === 'name'
      ? [...visible].sort((a, b) => a.name.localeCompare(b.name))
      : [...visible].sort((a, b) => (metric(b) ?? -1) - (metric(a) ?? -1))
  const grouped: { key: string; label: string; items: Server[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: sorted }]
      : (() => {
          const map = new Map<string, Server[]>()
          for (const s of sorted) {
            const k = s.group_name?.trim() || ''
            if (!map.has(k)) map.set(k, [])
            map.get(k)!.push(s)
          }
          return [...map.keys()]
            .sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
            .map((k) => ({ key: k, label: k || t('Без группы'), items: map.get(k)! }))
        })()

  const serverGroups = [
    ...new Set((servers ?? []).map((s) => s.group_name?.trim()).filter(Boolean) as string[]),
  ].sort((a, b) => a.localeCompare(b))

  // ——— массовый выбор / удаление ———
  const canDrag = !isViewer && groupBy === 'group' && !selectMode
  const toggleSelected = (id: number) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const exitSelect = () => {
    setSelectMode(false)
    setSelected(new Set())
  }
  const deleteSelected = async () => {
    const ids = [...selected]
    if (ids.length === 0) return
    if (
      !window.confirm(
        t('Удалить выбранные серверы ({n} шт.) вместе с историей метрик?', {
          n: ids.length,
        }),
      )
    )
      return
    try {
      // отдельного bulk-эндпоинта у серверов нет — удаляем по одному
      for (const id of ids) await deleteServer(id)
      exitSelect()
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    }
  }

  // ——— перенос сервера в другую группу перетаскиванием ———
  const startDrag = (id: number) => {
    dragRef.current = id
    busyRef.current = true
    setDragId(id)
  }
  const endDrag = () => {
    // если drop уже обработал перенос (dragRef сброшен) — сохранение ещё в полёте,
    // busyRef снимет его finally; не трогаем, иначе поллинг затрёт оптимистику
    if (dragRef.current == null) return
    dragRef.current = null
    busyRef.current = false
    setDragId(null)
    setDragOverKey(null)
  }
  const dropOnGroup = async (key: string) => {
    const id = dragRef.current
    const srv = (servers ?? []).find((s) => s.id === id)
    // key '' → «Без группы»
    if (id == null || !srv || (srv.group_name?.trim() || '') === key) {
      dragRef.current = null
      busyRef.current = false
      setDragId(null)
      setDragOverKey(null)
      return
    }
    dragRef.current = null
    setDragId(null)
    setDragOverKey(null)
    // оптимистично переносим, чтобы не мигало
    setServers((prev) =>
      (prev ?? []).map((s) => (s.id === id ? { ...s, group_name: key } : s)),
    )
    try {
      await updateServer(id, { group_name: key })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      busyRef.current = false
      load()
    }
  }

  return (
    <div>
      {servers && (
        <div className="stat-tiles">
          {/* «Проблемы» — первой: жмут чаще всего */}
          <SrvTile
            num={problems}
            tone="degraded"
            label={t('Проблемы')}
            active={filter === 'problem'}
            onClick={() => setFilter((f) => (f === 'problem' ? 'all' : 'problem'))}
          />
          <SrvTile
            num={total}
            label={t('Всего')}
            active={filter === 'all'}
            onClick={() => setFilter('all')}
          />
          <SrvTile
            num={online}
            tone="up"
            label={t('Онлайн')}
            active={filter === 'online'}
            onClick={() => setFilter((f) => (f === 'online' ? 'all' : 'online'))}
          />
          <SrvTile
            num={total - online}
            tone="down"
            label={t('Оффлайн')}
            active={filter === 'offline'}
            onClick={() => setFilter((f) => (f === 'offline' ? 'all' : 'offline'))}
          />
        </div>
      )}

      {!isViewer && behind.length > 0 && (
        <div className="agent-update-banner">
          <span className="aub-text">
            {t('Доступна версия агента {v}. Отстают: {n}.', {
              v: avail,
              n: behind.length,
            })}
          </span>
          <div className="aub-actions">
            <button
              className="ghost"
              disabled={updBusy}
              onClick={() => rollout([behind[0].id])}
              title={t('Обновить сначала одну ноду (canary), потом остальные')}
            >
              {t('Canary — 1 ноду')}
            </button>
            <button
              disabled={updBusy}
              onClick={() => {
                if (
                  window.confirm(
                    t('Обновить агент до {v} на всех отстающих нодах ({n})?', {
                      v: avail,
                      n: behind.length,
                    }),
                  )
                )
                  rollout(behind.map((s) => s.id))
              }}
            >
              {updBusy ? t('…') : t('Обновить все ({n})', { n: behind.length })}
            </button>
          </div>
        </div>
      )}
      {/* заглушённое (пороги сервера) — сводкой: клик открывает карточку, там и снимают.
          Мьюты репозиториев и покрытия бэкапа живут на своей странице «Бэкапы». */}
      <MutesBanner
        entries={muted.map(({ s, items }) => ({ id: s.id, name: s.name, items }))}
        t={t}
        onOpen={(id) => setDetailId(id)}
      />

      {/* позитивный индикатор: все агенты на актуальной ПОДПИСАННОЙ версии */}
      {avail && behind.length === 0 && (servers?.some((s) => s.agent_version) ?? false) && (
        <div className="agent-update-ok">
          {t('✓ Все агенты на подписанной версии {v}', { v: avail })}
        </div>
      )}

      <div className="checks-head">
        <h2>{t('Серверы')}</h2>
        <div className="checks-head-actions">
          {servers && servers.length > 1 && (
            <div className="win-switch">
              {(['name', 'cpu', 'ram', 'disk'] as const).map((k) => (
                <button
                  key={k}
                  className={`win-btn${sortBy === k ? ' win-btn-active' : ''}`}
                  onClick={() => setSortBy(k)}
                >
                  {{ name: t('Имя'), cpu: 'CPU', ram: 'RAM', disk: t('Диск') }[k]}
                  {sortBy === k && k !== 'name' && ' ↓'}
                </button>
              ))}
            </div>
          )}
          {servers && servers.length > 1 && (
            <div className="win-switch">
              <button
                className={`win-btn${groupBy === 'group' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('group')}
              >
                {t('По группе')}
              </button>
              <button
                className={`win-btn${groupBy === 'none' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('none')}
              >
                {t('Без групп')}
              </button>
            </div>
          )}
          {selectMode ? (
            <>
              <span className="select-count">
                {t('Выбрано: {n}', { n: selected.size })}
              </span>
              <button
                className="ghost"
                onClick={() => setSelected(new Set(visible.map((s) => s.id)))}
              >
                {t('Все видимые')}
              </button>
              <button
                className="ghost danger-btn"
                disabled={selected.size === 0}
                onClick={deleteSelected}
              >
                {t('Удалить ({n})', { n: selected.size })}
              </button>
              <button className="ghost" onClick={exitSelect}>
                {t('Готово')}
              </button>
            </>
          ) : (
            <>
              {!isViewer && servers && servers.length > 0 && (
                <button className="ghost" onClick={() => setSelectMode(true)}>
                  {t('Выбрать')}
                </button>
              )}
              {!isViewer && (
                <button onClick={() => setAddOpen(true)}>{t('+ Добавить сервер')}</button>
              )}
            </>
          )}
        </div>
      </div>

      {servers && servers.length > 1 && !selectMode && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: имя, группа, IP…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}

      {err && <p className="form-error">{err}</p>}
      {servers && servers.length === 0 && (
        <div className="card muted">
          {t('Пока нет серверов. Нажмите «Добавить сервер» и выполните команду установки на ноде.')}
        </div>
      )}
      {servers && servers.length > 0 && visible.length === 0 && (
        <div className="card muted">{t('Ничего не найдено.')}</div>
      )}

      {grouped.map((g) => (
        <div
          key={g.key}
          className={`mon-group${
            canDrag && dragId != null && dragOverKey === g.key ? ' group-drag-over' : ''
          }`}
          onDragEnter={
            canDrag ? () => dragId != null && setDragOverKey(g.key) : undefined
          }
          onDragOver={canDrag ? (e) => e.preventDefault() : undefined}
          onDrop={
            canDrag
              ? (e) => {
                  e.preventDefault()
                  dropOnGroup(g.key)
                }
              : undefined
          }
        >
          {groupBy !== 'none' && (
            <button className="mon-group-head" onClick={() => toggleCollapse(g.key)}>
              <span className="mon-group-caret">{collapsed.has(g.key) ? '▸' : '▾'}</span>
              <span className="mon-group-name">{g.label}</span>
              <SrvGroupSummary items={g.items} />
            </button>
          )}
          {!collapsed.has(g.key) && (
            <div className="check-list">
              {g.items.map((s) => (
                <ServerRow
                  key={s.id}
                  server={s}
                  showGroup={groupBy !== 'group'}
                  onOpen={() =>
                    selectMode ? toggleSelected(s.id) : setDetailId(s.id)
                  }
                  onOpenSec={(sec) => {
                    if (selectMode) return toggleSelected(s.id)
                    setDetailSec(sec)
                    setDetailId(s.id)
                  }}
                  selecting={selectMode}
                  checked={selected.has(s.id)}
                  dragging={dragId === s.id}
                  drag={
                    canDrag
                      ? {
                          onDragStart: () => startDrag(s.id),
                          onDragEnd: endDrag,
                        }
                      : undefined
                  }
                />
              ))}
            </div>
          )}
        </div>
      ))}

      {addOpen && (
        <EnrollModal
          groups={serverGroups}
          onClose={() => setAddOpen(false)}
          onEnrolled={(e) => {
            setEnroll(e)
            setAddOpen(false)
            load()
          }}
          onUnauthorized={onUnauthorized}
        />
      )}
      {enroll && <InstallModal enroll={enroll} onClose={() => setEnroll(null)} />}
      {detail && (
        <ServerDetail
          server={detail}
          available={avail}
          groups={serverGroups}
          initialSection={detailSec ?? urlSec}
          onClose={() => { setDetailId(null); setDetailSec(null); setUrlSec(null) }}
          onChanged={load}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  )
}

function SrvGroupSummary({ items }: { items: Server[] }) {
  const { t } = useI18n()
  const off = items.filter((s) => !s.online).length
  const probs = items.filter((s) => s.online && srvIssues(s, t).length > 0).length
  return (
    <span className="mon-group-sum">
      <span className="mon-group-count">{t('{n} шт.', { n: items.length })}</span>
      {off > 0 && <span className="uptime-badge t-down">{off} ⏻</span>}
      {probs > 0 && <span className="uptime-badge t-degraded">{probs} ▼</span>}
      {off === 0 && probs === 0 && <span className="t-up mon-group-ok">✓</span>}
    </span>
  )
}

function ServerRow({
  server: s,
  onOpen,
  onOpenSec,
  showGroup = true,
  selecting,
  checked,
  dragging,
  drag,
}: {
  server: Server
  onOpen: () => void
  // клик по 🔥: открыть деталь сразу на разделе с проблемой (null = просто открыть)
  onOpenSec: (sec: string | null) => void
  showGroup?: boolean
  selecting?: boolean
  checked?: boolean
  dragging?: boolean
  drag?: { onDragStart: () => void; onDragEnd: () => void }
}) {
  const { t } = useI18n()
  const r = s.last_report ?? {}
  const memP = pct(r.mem_used, r.mem_total)
  const diskP = r.disks?.length
    ? Math.max(...r.disks.filter((d) => d.total).map((d) => Math.round((d.used / d.total) * 100)))
    : null
  // «горит» — сервер онлайн, но с активными превышениями (CPU/RAM/диск/темп/…)
  const issues = srvIssues(s, t)
  const onFire = s.online && issues.length > 0
  const muteItems = collectMutes(s, t, ['alert'])
  return (
    <div
      className={`check-row srv-row${selecting ? ' selecting' : ''}${
        dragging ? ' dragging' : ''
      }`}
    >
      {selecting && (
        <input
          type="checkbox"
          className="row-check"
          checked={!!checked}
          onChange={onOpen}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      {drag && (
        <span
          className="mon-grip"
          draggable
          onDragStart={drag.onDragStart}
          onDragEnd={drag.onDragEnd}
          onClick={(e) => e.stopPropagation()}
          title={t('Перетащите в другую группу')}
          aria-label={t('Перетащите в другую группу')}
        >
          ⠿
        </span>
      )}
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div
        className="check-main clickable"
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpen()}
      >
        <div className="check-name">
          {onFire && (
            <span
              className="srv-fire clickable"
              role="button"
              tabIndex={0}
              title={issues.map((i) => i.text).join(', ') + ' — ' + t('открыть график')}
              onClick={(e) => { e.stopPropagation(); onOpenSec(issues.find((i) => i.sec)?.sec ?? null) }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation(); e.preventDefault()
                  onOpenSec(issues.find((i) => i.sec)?.sec ?? null)
                }
              }}
            >
              🔥
            </span>
          )}
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && (
            <span className="type-chip group-chip">{s.group_name}</span>
          )}
          {s.last_report?.backup_server?.present && (
            <span className="type-chip" title={t('Сервер бэкапов (rest-server)')}>🗄 {t('бэкап-сервер')}</span>
          )}
          {muteItems.length > 0 && <MuteChip items={muteItems} t={t} />}
          {!s.online && <span className="type-chip off">{t('оффлайн')}</span>}
        </div>
        <div className="check-target mono"><ServerAddr s={s} t={t} /></div>
      </div>
      <div className="srv-meters">
        <Meter label="CPU" value={r.cpu_percent != null ? Math.round(r.cpu_percent) : null} />
        <Meter label="RAM" value={memP} />
        <Meter label={t('Диск')} value={diskP} />
      </div>
    </div>
  )
}

function EnrollModal({
  groups,
  onClose,
  onEnrolled,
  onUnauthorized,
}: {
  groups: string[]
  onClose: () => void
  onEnrolled: (e: ServerEnroll) => void
  onUnauthorized: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [group, setGroup] = useState('')
  const [agentIp, setAgentIp] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function submit() {
    setBusy(true)
    setErr(null)
    try {
      const e = await createServer({
        name,
        group_name: group || undefined,
        agent_ip: agentIp.trim() || undefined,
      })
      onEnrolled(e)
    } catch (ex) {
      if (ex instanceof ApiError && ex.status === 401) return onUnauthorized()
      setErr(ex instanceof Error ? ex.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Добавить сервер')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <label className="field">
          <span>{t('Название')}</span>
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>
        <label className="field">
          <span>{t('Группа')}</span>
          <input
            value={group}
            onChange={(e) => setGroup(e.target.value)}
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
          <span>{t('IP сервера (опционально)')}</span>
          <input
            value={agentIp}
            onChange={(e) => setAgentIp(e.target.value)}
            placeholder="203.0.113.10"
          />
          <span className="field-hint muted small">
            {t('Если панель закрыта фаерволом: адрес попадёт в data/agent_allow_ips, а хостовый скрипт ops/agent-firewall-sync.sh разрешит его в ufw/firewalld. Для Caddy-вайтлиста ничего не нужно — /api/agent/* уже открыт.')}
          </span>
        </label>
        {err && <p className="form-error">{err}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {t('Отмена')}
          </button>
          <button onClick={submit} disabled={!name.trim() || busy}>
            {busy ? t('…') : t('Создать')}
          </button>
        </div>
      </div>
    </div>
  )
}

function InstallModal({ enroll, onClose }: { enroll: ServerEnroll; onClose: () => void }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  // живая проверка: поллим сервер, пока агент не пришлёт первый отчёт
  const [connected, setConnected] = useState<Server | null>(null)
  useEffect(() => {
    if (connected) return
    const id = window.setInterval(async () => {
      try {
        const list = await listServers()
        const s = list.find((x) => x.id === enroll.server.id)
        if (s?.last_seen) setConnected(s)
      } catch {
        /* транзиентные сбои поллинга не мешают ожиданию */
      }
    }, 4000)
    return () => window.clearInterval(id)
  }, [enroll.server.id, connected])
  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Сервер создан')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Выполните команду на сервере (под root). Токен показывается один раз.')}
        </p>
        <pre className="install-cmd">{enroll.install_cmd}</pre>
        <p className="muted small">
          {t('Агент сам включит всё применимое на ноде — Docker (read-only proxy), Kubernetes (узкий SA), доступ к бэкапам — вручную доделывать ничего не нужно. Отключить авто-настройку: добавить --no-auto.')}
        </p>
        <details className="install-alt">
          <summary className="muted small">
            {t('На сервере уже стоит агент другой панели?')}
          </summary>
          <p className="muted small install-alt-hint">
            {t('Допишите третий аргумент — любое имя этого инстанса — в самый конец команды, через пробел после токена:')}
          </p>
          <pre className="install-cmd">
            {enroll.install_cmd}
            <span className="install-cmd-add"> main</span>
          </pre>
          <div className="modal-actions install-alt-actions">
            <button
              className="ghost"
              onClick={() => {
                navigator.clipboard?.writeText(`${enroll.install_cmd} main`)
                setCopied(true)
              }}
            >
              {t('Скопировать с « main»')}
            </button>
          </div>
          <p className="muted small">
            {t('«main» — произвольное имя, оно лишь отличает панели между собой. Агенты будут работать рядом, каждый со своей панелью.')}
          </p>
        </details>
        <p className="muted small install-fw-note">
          {t('⏱ Файрвол панели открывается для IP новой ноды в течение ~2 мин. Если команда упала по таймауту (curl: timed out) — подождите минуту и повторите её же.')}
        </p>
        {connected ? (
          <div className="install-status install-ok">
            ✓{' '}
            {t('Агент подключился: {host} · v{ver} — метрики уже идут', {
              host: connected.hostname || connected.name,
              ver: connected.agent_version || '?',
            })}
          </div>
        ) : (
          <div className="install-status install-wait">
            <span className="sdot sdot-unknown" />
            {t('Ждём первый отчёт агента — статус обновится здесь сам…')}
          </div>
        )}
        <div className="modal-actions">
          <button
            className={connected ? 'ghost' : ''}
            onClick={() => {
              navigator.clipboard?.writeText(enroll.install_cmd)
              setCopied(true)
            }}
          >
            {copied ? t('Скопировано ✓') : t('Скопировать команду')}
          </button>
          {connected && <button onClick={onClose}>{t('Готово')}</button>}
        </div>
      </div>
    </div>
  )
}

type ServerEditForm = {
  name: string
  group_name: string
  agent_ip: string
  cpu_alert_percent: number
  mem_alert_percent: number
  disk_alert_percent: number
  disk_warn_percent: number
  disk_crit_percent: number
  temp_alert_c: number
  conntrack_alert_percent: number
  db_conn_alert_percent: number
  kube_expiry_alert_days: number
  disk_temp_alert_c: number
  alert_mutes: string[]
  offline_after_seconds: number
  alert_sustain_min: number // «жарит дольше N минут» → алерт (хранится в сек)
}

// типы серверных алертов, которые можно точечно заглушить для сервера (метки = SERVER_ALERT_KINDS)

// Единый контрол приглушения алертов сервера: один дропдаун «что заглушить»
// (весь сервер ИЛИ конкретный тип) + 1ч/1д/1нед. Активные снузы — чипами ниже
// с «до …» и крестиком. kind === null → весь сервер, иначе тип.
function SnoozeBar({
  snoozeUntil,
  snoozes,
  mutes,
  onSnooze,
  onUnmute,
  busy,
}: {
  snoozeUntil: string | null
  snoozes: Record<string, string> | null
  mutes: string[]  // постоянные мьюты: без срока, снимаются только вручную
  onSnooze: (kind: string | null, hours: number) => void
  onUnmute: (kind: string) => void
  busy?: boolean
}) {
  const { t } = useI18n()
  const [scope, setScope] = useState('all') // 'all' = весь сервер, иначе тип
  const now = Date.now()
  const labelOf = (k: string) => SNOOZE_KINDS.find((x) => x.k === k)?.label ?? k
  const fmt = (u: string) =>
    new Date(u).toLocaleString([], {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  const wholeActive = !!snoozeUntil && new Date(snoozeUntil).getTime() > now
  const typeActive = Object.entries(snoozes ?? {})
    .filter(([, u]) => new Date(u).getTime() > now)
    .sort((a, b) => labelOf(a[0]).localeCompare(labelOf(b[0])))
  const durations = [
    { h: 1, l: t('1 час') },
    { h: 24, l: t('1 день') },
    { h: 24 * 7, l: t('1 неделя') },
    // -1 = навсегда: пишется не в снуз (он по определению временный), а в alert_mutes
    { h: -1, l: t('постоянно') },
  ]
  return (
    <div className="snooze-bar">
      <div className="snooze-row">
        <span className="muted small">🔕 {t('Заглушить:')}</span>
        <select
          className="snooze-scope"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
          disabled={busy}
        >
          <option value="all">{t('Весь сервер')}</option>
          {SNOOZE_KINDS.map(({ k, label }) => (
            <option key={k} value={k}>
              {t(label)}
            </option>
          ))}
        </select>
        {durations
          // «постоянно» для отдельного УРОВНЯ не даём: постоянное выключение уровня —
          // это порог 0 в настройках сервера. Иначе одно и то же жило бы в двух местах,
          // причём мьют уровня в форме порогов не виден — и о нём бы забыли.
          .filter(({ h }) => h > 0 || !scope.includes('@'))
          .map(({ h, l }) => (
            <button
              key={h}
              className="ghost"
              disabled={busy}
              onClick={() => onSnooze(scope === 'all' ? null : scope, h)}
            >
              {l}
            </button>
          ))}
      </div>
      {scope.includes('@') && (
        <div className="muted small">
          {t('Насовсем уровень выключается порогом: поставьте 0 в настройках сервера.')}
        </div>
      )}
      {(wholeActive || typeActive.length > 0 || mutes.length > 0) && (
        <div className="snooze-active-list">
          {mutes.map((k) => (
            <span key={'m' + k} className="snooze-active-chip snooze-perm">
              🔇 {t(labelOf(k))} · {t('постоянно')}
              <button className="chip-x" disabled={busy} title={t('Снять')}
                onClick={() => onUnmute(k)}>
                ✕
              </button>
            </span>
          ))}
          {wholeActive && (
            <span className="snooze-active-chip">
              🔕 {t('Весь сервер')} · {t('до {t}', { t: fmt(snoozeUntil!) })}
              <button
                className="chip-x"
                disabled={busy}
                title={t('Снять')}
                onClick={() => onSnooze(null, 0)}
              >
                ✕
              </button>
            </span>
          )}
          {typeActive.map(([k, u]) => (
            <span key={k} className="snooze-active-chip">
              🔕 {t(labelOf(k))} · {t('до {t}', { t: fmt(u) })}
              <button
                className="chip-x"
                disabled={busy}
                title={t('Снять')}
                onClick={() => onSnooze(k, 0)}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// Совет по правам агента: агент сообщил, что ему не хватает чего-то из systemd-юнита
// (напр. CAP_SYSLOG для имени OOM-жертвы). Показываем что и команду-фикс (drop-in).
// ЕДИНЫЙ баннер «сделай руками на сервере»: нехватка прав в systemd-юните агента +
// устаревшие setup-скрипты. Всё, что надо выполнить от root, собирается в ОДИН блок с
// ОДНОЙ кнопкой «Копировать» — иначе на ноде с пачкой проблем пришлось бы копировать
// по частям. Порядок: сначала drop-in юнита (перезапустит агент), потом helper'ы.
function ServerFixBanner({
  titles,
  command,
  helpers,
  t,
}: {
  titles: string[]
  command: string | null
  helpers: HelperAdvice[]
  t: (s: string, vars?: Record<string, string | number>) => string
}) {
  const [copied, setCopied] = useState(false)
  const parts: string[] = []
  if (command) parts.push(command)
  for (const h of helpers)
    // общий маршрут: персональные URL есть только у kube/backup/backupserver, поэтому
    // подсказка для остальных хелперов вела на 404 — команду нельзя было выполнить
    parts.push(`curl -fsSL ${window.location.origin}/api/agent/setup/${h.name}.sh | sudo bash`)
  const cmd = parts.join('\n')
  return (
    <div className="agent-advice">
      <div className="agent-advice-head">
        🔧 {t('Требуется ручное действие на сервере. Выполните от root:')}
      </div>
      {command && (
        <>
          <div className="agent-advice-group">
            ⚙️ {t('Агенту не хватает прав из systemd-юнита:')}
          </div>
          <ul className="agent-advice-list">
            {titles.map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </>
      )}
      {helpers.length > 0 && (
        <>
          <div className="agent-advice-group">🧩 {t('Setup-скрипты на ноде устарели:')}</div>
          <ul className="agent-advice-list">
            {helpers.map((h) => (
              <li key={h.name}>
                {t('{label} (helper «{helper}»): {a} → {b}', {
                  label: t(h.label),
                  helper: h.name,
                  a: fmtSetupVersion(h.installed),
                  b: fmtSetupVersion(h.current),
                })}
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="agent-advice-cmd">
        <pre>{cmd}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(cmd)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? t('Скопировано') : t('Копировать')}
        </button>
      </div>
      <div className="muted small">
        {command && t('OTA обновляет только бинарь, не юнит — поэтому вручную. На новых установках уже включено.')}
        {command && helpers.length > 0 && ' '}
        {helpers.length > 0 && t('Переустановка идемпотентна — обновит helper, ничего не сломает.')}
      </div>
    </div>
  )
}

function ServerEditCard({
  form,
  set,
  busy,
  err,
  groups,
  isVm,
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
  onSubmit: () => void
  onCancel: () => void
  onDelete: () => void
}) {
  const { t } = useI18n()
  const num = (v: string) => (v === '' ? 0 : Number(v))
  // на VM нет датчиков температуры → скрываем температурные алерты (CPU/диск)
  const kinds = SRV_ALERT_KINDS.filter(
    ({ k }) => !isVm || (k !== 'temp' && k !== 'disktemp'),
  )
  return (
    <div className="settings-group">
      {err && <p className="form-error">{err}</p>}
      {/* удаление — сверху и отдельно, чтобы не нажать случайно рядом с «Сохранить» */}
      <div className="edit-danger-top">
        <button className="ghost danger-btn" onClick={onDelete} disabled={busy}>
          🗑 {t('Удалить сервер')}
        </button>
      </div>
      <label className="field">
        <span>{t('Название')}</span>
        <input value={form.name} onChange={(e) => set({ name: e.target.value })} autoFocus />
      </label>
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
        <span>{t('IP сервера (опционально)')}</span>
        <input
          value={form.agent_ip}
          onChange={(e) => set({ agent_ip: e.target.value })}
          placeholder="203.0.113.10"
        />
        <span className="field-hint muted small">
          {t('Адрес, с которого агент ходит в панель — для разрешения в фаерволе хоста панели (ufw/firewalld).')}
        </span>
      </label>
      <h4>{t('Пороги алертов, %')}</h4>
      <div className="form-row">
        <label className="field">
          <span>CPU</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.cpu_alert_percent}
            onChange={(e) => set({ cpu_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>RAM</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.mem_alert_percent}
            onChange={(e) => set({ mem_alert_percent: num(e.target.value) })}
          />
        </label>
      </div>
      <p className="muted small">{t('Диск — три уровня (0 = выключить уровень):')}</p>
      <div className="form-row">
        <label className="field">
          <span>⚠️ {t('предупр.')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.disk_warn_percent}
            onChange={(e) => set({ disk_warn_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🔴 {t('проблема')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.disk_alert_percent}
            onChange={(e) => set({ disk_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🚨 {t('критично')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.disk_crit_percent}
            onChange={(e) => set({ disk_crit_percent: num(e.target.value) })}
          />
        </label>
      </div>
      {!isVm && (
        <label className="field">
          <span>🌡 {t('Алерт по температуре CPU, °C (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={120}
            value={form.temp_alert_c}
            onChange={(e) => set({ temp_alert_c: num(e.target.value) })}
          />
        </label>
      )}
      <div className="form-row">
        <label className="field">
          <span>🔗 {t('Алерт conntrack, % (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.conntrack_alert_percent}
            onChange={(e) => set({ conntrack_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🔌 {t('Алерт по коннектам СУБД, % (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={form.db_conn_alert_percent}
            onChange={(e) => set({ db_conn_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>⏳ {t('Предупреждать о сроках Kubernetes за, дн. (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={365}
            value={form.kube_expiry_alert_days}
            onChange={(e) => set({ kube_expiry_alert_days: num(e.target.value) })}
          />
        </label>
        {!isVm && (
          <label className="field">
            <span>🌡 {t('Алерт по температуре диска, °C (0 = выкл)')}</span>
            <input
              type="number"
              min={0}
              max={120}
              value={form.disk_temp_alert_c}
              onChange={(e) => set({ disk_temp_alert_c: num(e.target.value) })}
            />
          </label>
        )}
      </div>
      <label className="field">
        <span>{t('Считать оффлайн после, сек')}</span>
        <input
          type="number"
          min={30}
          max={86400}
          value={form.offline_after_seconds}
          onChange={(e) => set({ offline_after_seconds: num(e.target.value) })}
        />
      </label>
      <label className="field">
        <span>{t('Алертить, только если держится дольше, мин')}</span>
        <input
          type="number"
          min={0}
          max={1440}
          value={form.alert_sustain_min}
          onChange={(e) => set({ alert_sustain_min: num(e.target.value) })}
        />
        <span className="muted small">
          {t('Гасит кратковременные спайки CPU/RAM/температуры/conntrack. 0 = слать сразу.')}
        </span>
      </label>
      <div className="mute-group">
        <span className="muted small">{t('Не слать алерты этого сервера:')}</span>
        <div className="mute-chips">
          {kinds.map(({ k, label }) => {
            const muted = form.alert_mutes.includes(k)
            return (
              <button
                key={k}
                type="button"
                className={`mute-chip${muted ? ' mute-chip-on' : ''}`}
                onClick={() =>
                  set({
                    alert_mutes: muted
                      ? form.alert_mutes.filter((x) => x !== k)
                      : [...form.alert_mutes, k],
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
      <div className="modal-actions edit-actions">
        <button className="ghost" onClick={onCancel} disabled={busy}>
          {t('Отмена')}
        </button>
        <button onClick={onSubmit} disabled={busy || !form.name.trim()}>
          {busy ? t('…') : t('Сохранить')}
        </button>
      </div>
    </div>
  )
}

// Полноэкранный график одной метрики: пресеты периода + зум мышью (как в Grafana).
function ServerChartModal({
  server,
  metricKey,
  onClose,
}: {
  server: Server
  metricKey: MetricKey
  onClose: () => void
}) {
  const { t } = useI18n()
  const [hours, setHours] = useState(6)
  const [zoom, setZoom] = useState<{ from: number; to: number } | null>(null)
  const [metrics, setMetrics] = useState<ServerMetric[] | null>(null)
  // высота развёрнутого графика — под вьюпорт (реагирует на ресайз)
  const [vh, setVh] = useState(() => window.innerHeight)
  useEffect(() => {
    const on = () => setVh(window.innerHeight)
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  const bigH = Math.min(780, Math.max(380, Math.round(vh * 0.64)))

  useEffect(() => {
    setMetrics(null)
    const run = () =>
      serverMetrics(server.id, hours, zoom ?? undefined).then(setMetrics).catch(() => {})
    run()
    if (zoom) return // на зуме не автообновляем — фиксированное окно
    const id = window.setInterval(run, 15000)
    return () => window.clearInterval(id)
  }, [server.id, hours, zoom])

  const M = metrics ?? []
  const mc = buildMetric(metricKey, M, t)
  const spanH = zoom ? (zoom.to - zoom.from) / 3600 : hours
  const fmtT =
    spanH <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  const setWindow = (h: number) => {
    setZoom(null)
    setHours(h)
  }

  return (
    <div className="modal-backdrop chart-full-backdrop">
      <div className="card chart-full">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${server.online ? 'sdot-up' : 'sdot-down'}`} />
            <h3>{server.name}</h3>
            <span className="chart-full-metric">{mc.title}</span>
          </div>
          <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
            ✕
          </button>
        </div>
        <div className="srv-meta">
          {server.os && <span className="srv-meta-item">{server.os}</span>}
          {(server.external_ip || server.local_ip) && (
            <span className="srv-meta-item mono"><ServerAddr s={server} t={t} /></span>
          )}
        </div>

        <div className="detail-toolbar chart-full-bar">
          <div className="win-switch">
            {SRV_WINDOWS.map((w) => (
              <button
                key={w.hours}
                className={`win-btn${!zoom && hours === w.hours ? ' win-btn-active' : ''}`}
                onClick={() => setWindow(w.hours)}
              >
                {t(w.label)}
              </button>
            ))}
          </div>
          {zoom ? (
            <button className="ghost" onClick={() => setZoom(null)}>
              {t('Сбросить зум')} ✕
            </button>
          ) : (
            <span className="muted small">{t('Выделите участок мышью, чтобы приблизить')}</span>
          )}
        </div>

        {metrics == null ? (
          <div className="chart-empty">{t('загрузка…')}</div>
        ) : mc.ts.length ? (
          <StackedAreaChart
            ts={mc.ts}
            series={mc.series}
            mode={mc.mode}
            yMax={mc.yMax}
            fmtY={mc.fmtY}
            fmtV={mc.fmtV}
            fmtTime={fmtT}
            height={bigH}
            onZoom={(f, to) => setZoom({ from: f / 1000, to: to / 1000 })}
          />
        ) : (
          <div className="chart-empty">{t('Нет данных за период')}</div>
        )}
      </div>
    </div>
  )
}

function ServerDetail({
  server: s,
  available,
  groups,
  initialSection,
  onClose,
  onChanged,
  onUnauthorized,
}: {
  server: Server
  available: string
  groups: string[]
  initialSection?: string | null
  onClose: () => void
  onChanged: () => void
  onUnauthorized: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [updBusy, setUpdBusy] = useState(false)
  const [snoozing, setSnoozing] = useState(false)
  const doUpdate = async (fn: () => Promise<Server[]>) => {
    setUpdBusy(true)
    try {
      await fn()
      onChanged()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
    } finally {
      setUpdBusy(false)
    }
  }
  const [metrics, setMetrics] = useState<ServerMetric[] | null>(null)
  const [hours, setHours] = useState(6)
  const [zoomKey, setZoomKey] = useState<MetricKey | null>(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<ServerEditForm | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const r = s.last_report ?? {}

  useEffect(() => {
    setMetrics(null)
    const run = () => serverMetrics(s.id, hours).then(setMetrics).catch(() => {})
    run()
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [s.id, hours])

  // диплинк из алерта (?server=id&sec=…): открыть деталь сразу на нужном разделе
  const scrolledRef = useRef(false)
  useEffect(() => {
    if (!initialSection || scrolledRef.current || !s.last_report) return
    scrolledRef.current = true
    const id = window.setTimeout(() => {
      // Порядок: точная карточка → раздел с тем же именем → раздел, которому карточка
      // принадлежит. Третий шаг нужен потому, что карточки условны: датчика температуры
      // диска может не быть, и тогда mcard-disktemp в DOM отсутствует — без запасного
      // пути ссылка не сработала бы вообще.
      const el =
        document.getElementById(`mcard-${initialSection}`) ??
        document.getElementById(`msec-${initialSection}`) ??
        document.getElementById(`msec-${SEC_OF_CARD[initialSection] ?? ''}`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 200) // ждём вёрстку модалки
    return () => window.clearTimeout(id)
  }, [initialSection, s.last_report])

  async function del() {
    if (
      !window.confirm(
        t('Удалить сервер «{name}»? Агент на ноде продолжит слать — удалите его отдельно.', {
          name: s.name,
        }),
      )
    )
      return
    try {
      await deleteServer(s.id)
      onChanged()
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
    }
  }

  function startEdit() {
    setForm({
      name: s.name,
      group_name: s.group_name,
      agent_ip: s.agent_ip ?? '',
      cpu_alert_percent: s.cpu_alert_percent,
      mem_alert_percent: s.mem_alert_percent,
      disk_alert_percent: s.disk_alert_percent,
      disk_warn_percent: s.disk_warn_percent,
      disk_crit_percent: s.disk_crit_percent,
      temp_alert_c: s.temp_alert_c,
      conntrack_alert_percent: s.conntrack_alert_percent,
      db_conn_alert_percent: s.db_conn_alert_percent,
      kube_expiry_alert_days: s.kube_expiry_alert_days,
      disk_temp_alert_c: s.disk_temp_alert_c,
      alert_mutes: s.alert_mutes ?? [],
      offline_after_seconds: s.offline_after_seconds,
      alert_sustain_min: Math.round((s.alert_sustain_seconds ?? 900) / 60),
    })
    setSaveErr(null)
    setEditing(true)
  }

  async function submitEdit() {
    if (!form) return
    setSaving(true)
    setSaveErr(null)
    try {
      await updateServer(s.id, {
        name: form.name.trim(),
        group_name: form.group_name.trim(),
        agent_ip: form.agent_ip.trim(),
        cpu_alert_percent: form.cpu_alert_percent,
        mem_alert_percent: form.mem_alert_percent,
        disk_alert_percent: form.disk_alert_percent,
        disk_warn_percent: form.disk_warn_percent,
        disk_crit_percent: form.disk_crit_percent,
        temp_alert_c: form.temp_alert_c,
        conntrack_alert_percent: form.conntrack_alert_percent,
        db_conn_alert_percent: form.db_conn_alert_percent,
        kube_expiry_alert_days: form.kube_expiry_alert_days,
        disk_temp_alert_c: form.disk_temp_alert_c,
        alert_mutes: form.alert_mutes,
        offline_after_seconds: form.offline_after_seconds,
        alert_sustain_seconds: Math.max(0, Math.round(form.alert_sustain_min * 60)),
      })
      onChanged()
      setEditing(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setSaveErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setSaving(false)
    }
  }

  const M = metrics ?? []
  // для окон длиннее суток на оси/в тултипе показываем дату вместо времени
  const fmtT =
    hours <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  // абсолютные значения для аннотаций под графиками (текущий снимок).
  // «доступно» = сколько реально можно ещё занять (free + реклеймящийся кэш),
  // как `free -h` available — агент шлёт mem_used = total - available.
  const memTotalB = r.mem_total ?? 0
  const memCacheB = r.mem_cached ?? 0
  const memUsedB = r.mem_used ?? 0
  const memAvailB = Math.max(0, memTotalB - memUsedB)
  const loadStr = (r.load ?? []).slice(0, 3).map((x) => x.toFixed(2)).join(' / ')

  // Секция графика: кликабельная карточка → полноэкранный график этой метрики.
  const chartCard = (key: MetricKey, extra?: React.ReactNode) => {
    const mc = buildMetric(key, M, t)
    return (
      // якорь на КАРТОЧКУ: ссылка из алерта должна вести к самой метрике, а не к
      // началу раздела. У диска, например, раздел открывается графиками ввода-вывода,
      // а заполнение — в самом низу, и человек попадал не туда, о чём был алерт.
      <div className="chart-block chart-card" id={`mcard-${key}`}>
        <button className="chart-cap chart-cap-btn" onClick={() => setZoomKey(key)}>
          <span>{mc.title}</span>
          <span className="chart-expand" title={t('Открыть на весь экран')}>⤢</span>
        </button>
        {metrics == null ? (
          <div className="chart-empty">{t('загрузка…')}</div>
        ) : mc.ts.length ? (
          <StackedAreaChart
            ts={mc.ts}
            series={mc.series}
            mode={mc.mode}
            yMax={mc.yMax}
            fmtY={mc.fmtY}
            fmtV={mc.fmtV}
            fmtTime={fmtT}
            height={key === 'net' ? 200 : 180}
            onExpand={() => setZoomKey(key)}
          />
        ) : (
          <div className="chart-empty">—</div>
        )}
        {extra}
      </div>
    )
  }

  return (
    <>
    <div className="modal-backdrop">
      <div className="card detail detail-server">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          </div>
          <div className="detail-head-actions">
            {!editing && !isViewer && (
              <button className="ghost icon-btn" onClick={startEdit} title={t('Изменить')}>
                ✎
              </button>
            )}
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        {!editing && !isViewer && (
          <SnoozeBar
            snoozeUntil={s.snooze_until}
            snoozes={s.alert_snoozes}
            mutes={s.alert_mutes ?? []}
            busy={snoozing}
            onUnmute={async (kind) => {
              setSnoozing(true)
              try {
                await updateServer(s.id, {
                  alert_mutes: (s.alert_mutes ?? []).filter((x) => x !== kind),
                })
                onChanged()
              } catch (e) {
                if (e instanceof ApiError && e.status === 401) onUnauthorized()
              } finally {
                setSnoozing(false)
              }
            }}
            onSnooze={async (kind, hours) => {
              setSnoozing(true)
              try {
                if (hours < 0) {
                  // «постоянно» — это не снуз, а мьют: снуз всегда имеет срок и
                  // истекает, а тут нужно, чтобы порог молчал, пока его не вернут
                  const cur = s.alert_mutes ?? []
                  const add = kind === null ? SRV_ALERT_KINDS.map((x) => x.k) : [kind]
                  await updateServer(s.id, { alert_mutes: [...new Set([...cur, ...add])] })
                } else if (kind === null) await snoozeServer(s.id, hours)
                else await snoozeServerAlert(s.id, kind, hours)
                onChanged()
              } catch (e) {
                if (e instanceof ApiError && e.status === 401) onUnauthorized()
              } finally {
                setSnoozing(false)
              }
            }}
          />
        )}
        {!editing && !isViewer && (s.agent_fix_command || (s.helper_advice?.length ?? 0) > 0) && (
          <ServerFixBanner
            titles={s.agent_advice}
            command={s.agent_fix_command}
            helpers={s.helper_advice || []}
            t={t}
          />
        )}
        {s.last_report ? (
          <div className="srv-meta">
            {s.os && <span className="srv-meta-item">{s.os}</span>}
            {r.is_vm !== undefined && (
              <span className="srv-meta-item">
                {r.is_vm ? (r.virt ? `VM · ${r.virt}` : 'VM') : 'Baremetal'}
              </span>
            )}
            {s.hostname && <span className="srv-meta-item mono">{s.hostname}</span>}
            {(s.external_ip || s.local_ip) && <ServerAddr s={s} t={t} />}
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('аптайм')}</span> {fmtUptime(r.uptime_seconds)}
            </span>
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('агент')}</span> {s.agent_version || '—'}
              {!isViewer &&
                (s.target_agent_version && s.target_agent_version !== s.agent_version ? (
                  <>
                    {' '}
                    <span className="agent-upd-to">→ {s.target_agent_version}</span>
                    <button
                      className="agent-upd-btn ghost"
                      disabled={updBusy}
                      onClick={() => doUpdate(() => agentUpdateCancel([s.id]))}
                      title={t('Отменить обновление')}
                    >
                      {t('отменить')}
                    </button>
                  </>
                ) : (
                  available &&
                  s.agent_version &&
                  available !== s.agent_version && (
                    <button
                      className="agent-upd-btn"
                      disabled={updBusy}
                      onClick={() => doUpdate(() => agentUpdate(available, [s.id]))}
                      title={t('Обновить агент этой ноды (проверит подпись и хеш)')}
                    >
                      ↑ {available}
                    </button>
                  )
                ))}
            </span>
            {r.clock && (
              <span className="srv-meta-item">
                <span className="srv-meta-k">{t('время')}</span>{' '}
                <ClockStatus clock={r.clock} skew={r.clock_skew_sec} />
              </span>
            )}
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
            </span>
          </div>
        ) : (
          <div className="detail-target">
            {s.last_seen
              ? `${t('оффлайн')} · ${fmtRel(s.last_seen)}`
              : t('агент ещё не выходил на связь')}
          </div>
        )}

        {editing && form ? (
          <ServerEditCard
            form={form}
            set={(patch) => setForm({ ...form, ...patch })}
            busy={saving}
            err={saveErr}
            groups={groups}
            isVm={r.is_vm === true}
            onSubmit={submitEdit}
            onCancel={() => setEditing(false)}
            onDelete={del}
          />
        ) : !s.last_report ? (
          <div className="srv-nodata">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <div className="srv-nodata-title">{t('Сервер недоступен')}</div>
            <p className="muted small">
              {t('Агент ещё не выходил на связь. Установите и запустите kervax-agent на ноде — метрики появятся здесь. Изменить или удалить сервер — кнопка ✎.')}
            </p>
          </div>
        ) : (
        <>
        {/* сдвиг часов — прямо в шапке: важная штука (ломает TOTP/TLS/логи), плюс команда фикса */}
        {r.clock && Math.abs(r.clock_skew_sec ?? 0) >= 5 && (
          <ClockFix server={s} clock={r.clock} skew={r.clock_skew_sec} canManage={!isViewer} />
        )}
        <div className="uptime-tiles">
          <div className="uptime-tile">
            <div className={`uptime-val ${toneOf(r.cpu_percent != null ? Math.round(r.cpu_percent) : null)}`}>
              {r.cpu_percent != null ? `${Math.round(r.cpu_percent)}%` : '—'}
            </div>
            <div className="uptime-lbl">CPU · load {r.load?.[0] ?? '—'}</div>
          </div>
          <div className="uptime-tile">
            <div className={`uptime-val ${toneOf(pct(r.mem_used, r.mem_total))}`}>
              {pct(r.mem_used, r.mem_total) ?? '—'}%
            </div>
            <div className="uptime-lbl">RAM {fmtBytes(r.mem_used)} / {fmtBytes(r.mem_total)}</div>
          </div>
          {(() => {
            // диск — самый загруженный раздел (при нескольких показываем худший + их число)
            const ds = (r.disks ?? []).filter((d) => d.total)
            if (!ds.length) return null
            const worst = ds.reduce((a, b) => (b.used / b.total > a.used / a.total ? b : a))
            const p = Math.round((worst.used / worst.total) * 100)
            return (
              <div className="uptime-tile">
                <div className={`uptime-val ${toneOf(p)}`}>{p}%</div>
                <div className="uptime-lbl">
                  {t('Диск')} · {worst.mount}
                  {ds.length > 1 ? ` · ${ds.length} ${t('разд.')}` : ''}
                </div>
              </div>
            )
          })()}
          <div className="uptime-tile">
            <div className="uptime-val srv-net-val">
              <span className="t-up">↓ {fmtRate(r.net_rx)}</span>
              <span className="muted">↑ {fmtRate(r.net_tx)}</span>
            </div>
            <div className="uptime-lbl">{t('Сеть')} · swap {fmtBytes(r.swap_used)}</div>
          </div>
        </div>

        <div className="detail-toolbar">
          <div className="win-switch">
            {SRV_WINDOWS.map((w) => (
              <button
                key={w.hours}
                className={`win-btn${hours === w.hours ? ' win-btn-active' : ''}`}
                onClick={() => setHours(w.hours)}
              >
                {t(w.label)}
              </button>
            ))}
          </div>
        </div>

        <div className="detail-body">
          <DetailNav t={t} />
          <div className="detail-sections">
        <MetricSection id="cpu" title="CPU">
          <div className="chart-grid">
            {chartCard(
              'cpu',
              <div className="loc-results chart-stats">
                {r.cpu_model && <StatRow name={t('Модель')} value={r.cpu_model} />}
                <StatRow name={t('Ядра')} value={r.cpu_cores ? String(r.cpu_cores) : '—'} />
                <StatRow name={t('Load 1 / 5 / 15 мин')} value={loadStr || '—'} />
              </div>,
            )}
            {r.cpu_cores_pct?.length ? chartCard('cores') : null}
            {/* частота: на VM гость видит фиксированную хостовую частоту — бесполезно,
                поэтому показываем только на железе (is_vm !== true) */}
            {r.cpu_freq != null && !r.is_vm
              ? chartCard(
                  'freq',
                  <div className="loc-results chart-stats">
                    <StatRow color="#5a8fc7" name={t('частота')} value={fmtMHz(r.cpu_freq)} />
                  </div>,
                )
              : null}
            {r.cpu_temp != null
              ? chartCard(
                  'temp',
                  <div className="loc-results chart-stats">
                    <StatRow color="#cf9b52" name={t('температура')} value={fmtTempC(r.cpu_temp)} />
                  </div>,
                )
              : null}
            {r.cpu_throttle != null ? chartCard('throttle') : null}
          </div>
        </MetricSection>

        <MetricSection id="mem" title={t('Память')}>
          <div className="chart-grid">
            {chartCard(
              'mem',
              memTotalB > 0 ? (
                <div className="loc-results chart-stats">
                  <StatRow color="#5a8fc7" name={t('занято')} value={`${fmtBytes(memUsedB)} / ${fmtBytes(memTotalB)}`} />
                  <StatRow color="#4fa79a" name={t('кэш/буфер')} value={fmtBytes(memCacheB)} />
                  <StatRow color="#5b6472" name={t('доступно')} value={fmtBytes(memAvailB)} />
                  {(r.swap_total ?? 0) > 0 && (
                    <StatRow name="swap" value={`${fmtBytes(r.swap_used)} / ${fmtBytes(r.swap_total)}`} />
                  )}
                </div>
              ) : undefined,
            )}
            {(r.swap_total ?? 0) > 0 &&
              chartCard(
                'swap',
                <div className="loc-results chart-stats">
                  <StatRow color="#57a894" name={t('загрузка')} value={fmtRate(r.swap_in)} />
                  <StatRow color="#cf9b52" name={t('выгрузка')} value={fmtRate(r.swap_out)} />
                </div>,
              )}
            {chartCard(
              'memwb',
              <div className="loc-results chart-stats">
                <StatRow color="#cf9b52" name={t('ожидают записи')} value={fmtBytes(r.mem_dirty)} />
                <StatRow color="#c77b95" name={t('запись на диск')} value={fmtBytes(r.mem_writeback)} />
                <StatRow color="#8a7fb8" name={t('ядро (slab)')} value={fmtBytes(r.mem_slab)} />
              </div>,
            )}
            {r.oom_kill != null
              ? chartCard('oom', <OomEventList serverId={s.id} t={t} />)
              : null}
          </div>
        </MetricSection>

        <MetricSection id="net" title={t('Сеть')}>
          <div className="chart-grid">
            {chartCard(
              'net',
              <div className="loc-results chart-stats">
                <StatRow color="#57a894" name={t('↓ приём')} value={fmtRate(r.net_rx)} />
                <StatRow color="#5a8fc7" name={t('↑ отдача')} value={fmtRate(r.net_tx)} />
              </div>,
            )}
            {(r.net_ifaces?.length ?? 0) > 0 &&
              chartCard(
                'netifrx',
                <EntStats
                  items={r.net_ifaces!}
                  nameOf={(n) => n.if}
                  sortVal={(n) => n.rx + n.tx}
                  render={(n) => `↓ ${fmtRate(n.rx)}  ↑ ${fmtRate(n.tx)}`}
                  palette={CORE_COLORS}
                />,
              )}
            {(r.net_ifaces?.length ?? 0) > 0 && chartCard('netiftx')}
            {(r.net_ifaces?.length ?? 0) > 0 &&
              chartCard(
                'neterr',
                <EntStats
                  items={r.net_ifaces!}
                  nameOf={(n) => n.if}
                  sortVal={(n) => n.errs + n.drops}
                  render={(n) => `${t('ошибки')} ${fmtErr(n.errs)} · ${t('дропы')} ${fmtErr(n.drops)}`}
                  palette={CORE_COLORS}
                />,
              )}
          </div>
        </MetricSection>

        <MetricSection id="conn" title={t('Соединения')}>
          <div className="chart-grid">
            {(r.conntrack_max ?? 0) > 0 &&
              chartCard(
                'conntrack',
                <div className="loc-results chart-stats">
                  <StatRow
                    color="#5a8fc7"
                    name="conntrack"
                    value={`${fmtNum(r.conntrack_count)} / ${fmtNum(r.conntrack_max)}`}
                  />
                  <StatRow
                    name={t('заполнение')}
                    value={`${(((r.conntrack_count ?? 0) / (r.conntrack_max || 1)) * 100).toFixed(1)}%`}
                  />
                </div>,
              )}
            {chartCard(
              'sockets',
              <div className="loc-results chart-stats">
                <StatRow color="#5a8fc7" name="TCP" value={fmtNum(r.sock_tcp)} />
                <StatRow color="#cf9b52" name="time-wait" value={fmtNum(r.sock_tcp_tw)} />
                <StatRow color="#57a894" name="UDP" value={fmtNum(r.sock_udp)} />
                <StatRow name={t('всего')} value={fmtNum(r.sock_used)} />
              </div>,
            )}
          </div>
        </MetricSection>

        <MetricSection id="disk" title={t('Диск')}>
          <div className="chart-grid">
            {chartCard(
              'diskio',
              <div className="loc-results chart-stats">
                <StatRow color="#57a894" name={t('чтение')} value={fmtRate(r.disk_read)} />
                <StatRow color="#cf9b52" name={t('запись')} value={fmtRate(r.disk_write)} />
              </div>,
            )}
            {chartCard(
              'diskiops',
              <div className="loc-results chart-stats">
                <StatRow
                  color="#57a894"
                  name={t('чтение')}
                  value={r.disk_read_iops != null ? fmtIops(r.disk_read_iops) : '—'}
                />
                <StatRow
                  color="#cf9b52"
                  name={t('запись')}
                  value={r.disk_write_iops != null ? fmtIops(r.disk_write_iops) : '—'}
                />
              </div>,
            )}
            {(r.disk_devs?.length ?? 0) > 0 &&
              chartCard(
                'diskutil',
                <EntStats
                  items={r.disk_devs!}
                  nameOf={(d) => d.dev}
                  sortVal={(d) => d.util}
                  render={(d) => `${d.util}%`}
                  palette={DISK_PALETTE}
                />,
              )}
            {(r.disk_devs?.length ?? 0) > 0 &&
              chartCard(
                'disklat',
                <EntStats
                  items={r.disk_devs!}
                  nameOf={(d) => d.dev}
                  sortVal={(d) => d.await}
                  render={(d) => fmtMs(d.await)}
                  palette={DISK_PALETTE}
                />,
              )}
            {r.disk_devs?.some((d) => d.temp != null) &&
              chartCard(
                'disktemp',
                <EntStats
                  items={r.disk_devs.filter((d) => d.temp != null)}
                  nameOf={(d) => d.dev}
                  sortVal={(d) => d.temp ?? 0}
                  render={(d) => fmtTempC(d.temp)}
                  palette={DISK_PALETTE}
                />,
              )}
            {chartCard('disk')}
          </div>
          <div className="detail-inc" id="mcard-diskfill">
            <div className="chart-cap">{t('Диски')}</div>
            {r.disks?.length ? (
              <div className="loc-results">
                {r.disks.map((d) => (
                  <div key={d.mount} className="loc-res">
                    <span className={`sdot sdot-${toneOf(pct(d.used, d.total)) === 't-down' ? 'down' : toneOf(pct(d.used, d.total)) === 't-degraded' ? 'degraded' : 'up'}`} />
                    <div className="loc-res-name mono">{d.mount}</div>
                    <div className="loc-res-metric">{pct(d.used, d.total)}%</div>
                    <div className="loc-res-msg muted small">
                      {fmtBytes(d.used)} / {fmtBytes(d.total)}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="muted small">—</div>
            )}
          </div>
        </MetricSection>

        <MetricSection id="proc" title={t('Процессы')}>
          <div className="chart-grid">
            <ProcCard title={t('Топ по CPU')} procs={r.top_cpu} kind="cpu" total={r.mem_total ?? 0} t={t} />
            <ProcCard title={t('Топ по памяти')} procs={r.top_mem} kind="mem" total={r.mem_total ?? 0} t={t} />
          </div>
        </MetricSection>
          </div>
        </div>

        </>
        )}
        <ScrollTopBtn t={t} />
      </div>
    </div>
    {zoomKey && (
      <ServerChartModal server={s} metricKey={zoomKey} onClose={() => setZoomKey(null)} />
    )}
    </>
  )
}
