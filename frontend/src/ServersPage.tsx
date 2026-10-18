import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  agentUpdate,
  agentUpdateCancel,
  ApiError,
  dropServerKnown,
  setServerNote,
  backupCommand,
  backupCommandStatus,
  createServer,
  deleteServer,
  dockerCommand,
  dockerCommandStatus,
  getAgentRelease,
  getServerTimeline,
  getServerVulns,
  kubeCommand,
  kubeCommandStatus,
  listServers,
  rotateServerToken,
  serverMetrics,
  serverOomEvents,
  serverWebErrors,
  serverWebErrorLines,
  snoozeServer,
  snoozeServerAlert,
  scanExposure,
  ackChanges,
  updateServer,
  type AccessAccount,
  type ClockInfo,
  type DnsInfo,
  type ExposedPort,
  type DiskForecastItem,
  type FailedUnit,
  type DiskHealth,
  type DiskHealthDisk,
  type DiskHealthRaid,
  type DiskUsageDir,
  type DiskUsageFs,
  type DiskUsageItem,
  type DiskWear,
  type HelperAdvice,
  type OomEvent,
  type ProcStat,
  type WebErrorRow,
  type CpuGroup,
  type Server,
  type ServerEnroll,
  type GpuSum,
  type ServerMetric,
  type ServerReport,
  type ServerVulns,
  type TimelineResult,
} from './api'
import { StackedAreaChart, type Series } from './charts/StackedAreaChart'
import { OnboardBlock } from './OnboardBlock'
import { diskHealth, diskUsage, fmtSetupVersion, gpuSecLine, shortList, sinceShort, unitsBlock, srvIssues, type SrvIssue, webLogLabel, webRate, webSeriesName, webUnparsed } from './serverUtils'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { currentLang, useI18n } from './i18n'
import { useUrlCard, useUrlParam } from './deeplink'
import { CardNav, FreshChip, OvAreas, OvRows, secInfoOf, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { fmtBytes, rateUnits, timeUnits } from './units'
import {
  MuteChip,
  MutesBanner,
  SNOOZE_KINDS,
  UNIT_MUTE,
  WEB_MUTE,
  EXPOSED_MUTE,
  NOBOOT_MUTE,
  ACCESS_MUTE,
  muteKindLabel,
  SRV_ALERT_KINDS,
  collectMutes,
  isDecision,
  muteBrief,
  muteText,
  type MuteItem,
} from './mutes'
import { useAuth } from './auth'
import { AlertHistoryModal } from './AlertHistoryModal'
import type { Section } from './App'
import { NoteBar } from './NoteBar'
import { ServerDeps, useDependentChecks } from './ParentBar'
import { TimelineCard, TL_ICON } from './TimelineCard'
import { PackagesCard } from './PackagesCard'
import { VulnsCard } from './VulnsCard'
import { JournalCard, JournalModal, journalBlocker } from './UnitJournal'
import { LinksCard, NetTile, TrafficCard, type Tone } from './NetLinks'
import { DiskWritersBlock, NetUsersCard } from './Usage'
import { SoftwareCard } from './Software'
import { GpuCard, GpuChip } from './Gpu'
import { KnownServerList } from './KnownDialog'
import { MaintBanner, MaintChip } from './MaintModal'

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
  | 'web'
  | 'web5xx'
  | 'sockets'
  | 'gpuutil'
  | 'gpumem'
  | 'gputemp'
  | 'gpupower'

// приглушённая палитра (Datadog-стиль) для overlay-графиков (ядра/интерфейсы/диски).
// Десатурированные тона: не мутнеют при наложении, различимы на 8+ линиях.
// Классическая палитра Grafana: оттенки Node Exporter Full в их порядке — соседние
// линии не сливаются, а цвета узнаются с первого взгляда.
const CORE_COLORS = [
  '#73bf69', '#fade2a', '#5794f2', '#ff9830', '#f2495c', '#b877d9',
  '#8ab8ff', '#96d98d', '#ffb357', '#ff7383', '#ca95e5', '#6ed0e0',
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
  yNice?: boolean | 'bytes' // круглые деления оси (1-2-5); 'bytes' - в КБ/МБ
  fmtY: (v: number) => string
  fmtV: (v: number) => string
}
const DISK_PALETTE = ['#5794f2', '#73bf69', '#ff9830', '#b877d9', '#fade2a', '#6ed0e0']

// Ось графика идёт по ТОЧКАМ, а не по времени, поэтому у молчащей ноды она
// заканчивалась последним отчётом: часы на подписях замирали, линия доходила до
// правого края, и график выглядел как у живого сервера. Дорисовываем в хвост пустые
// метки времени — линия обрывается там, где нода замолчала, а дальше видно провал.
//
// Не до бесконечности: нода может молчать неделю, и тогда все данные сожмутся в
// точку у левого края. Хвост не длиннее самих данных — этого хватает, чтобы провал
// бросался в глаза, а прочитать графики всё ещё было можно.
// `end` — конец показываемого окна. Для живого окна это «сейчас», а у выделенного
// мышью участка в прошлом — его правая граница: дотягивать такой участок до текущего
// момента значит пририсовать к нему полчаса пустоты, которой он не просил.
function padToNow(mc: MetricChart, end: number): MetricChart {
  const now = end
  const n = mc.ts.length
  if (n < 2) return mc
  const step = (mc.ts[n - 1] - mc.ts[0]) / (n - 1)
  const gap = now - mc.ts[n - 1]
  // Меньше двух шагов — обычная задержка сбора, дыры тут нет.
  if (step <= 0 || gap < step * 2) return mc
  const add = Math.min(Math.round(gap / step), n, 500)
  const tail = Array.from({ length: add }, (_, i) => mc.ts[n - 1] + step * (i + 1))
  return {
    ...mc,
    ts: [...mc.ts, ...tail],
    series: mc.series.map((x) => ({
      ...x,
      values: [...x.values, ...Array<number | null>(add).fill(null)],
    })),
  }
}

// Строит конфиг графика метрики из массива снимков — общий для детали и полноэкрана.
// Палитра Grafana для состава CPU: их же роли в их же цветах, включая простой. Кто
// привык к Node Exporter Full, читает этот график, не глядя в легенду.
// Стек запросов по логам: цвет по месту в пятерке, крупнейший - снизу. Хеш по имени
// тут не годится: на пять рядов из двенадцати цветов почти всегда выпадают два одинаковых.
const WEB_COLORS = ['#5794f2', '#73bf69', '#fade2a', '#ff9830', '#b877d9']
const WEB_REST = '#6b7a90' // "остальные": нейтральный, не спорит с цветными полосами
// у частых кодов 5xx цвет постоянный: 502 на любом сервере одного цвета
const CODE_COLORS: Record<string, string> = {
  '500': '#e24d42', '502': '#ff9830', '503': '#fade2a', '504': '#b877d9',
}
const CODE_PALETTE = ['#f2495c', '#6ed0e0', '#ca95e5', '#8ab8ff', '#ffb357', '#96d98d']
// какие логи идут в стеке отдельными полосами: пять самых нагруженных за окно
function webStackNames(M: ServerMetric[]): string[] {
  const sum = new Map<string, number>()
  for (const m of M) for (const x of m.web_top ?? []) sum.set(x.k, (sum.get(x.k) ?? 0) + x.r)
  return [...sum.entries()]
    .filter((e) => e[1] > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, WEB_COLORS.length)
    .map((e) => e[0])
}

const GRAFANA_CPU = {
  system: '#eab839',
  user: '#5794f2',
  iowait: '#e24d42',
  irq: '#ef843c',
  // простой серо-синий, а не синий: рядом с «юзером» (#5794f2) синее на синем сливалось,
  // и по стеку не было видно, сколько процессора занято, а сколько свободно
  idle: '#46586e',
}

function buildMetric(
  key: MetricKey,
  M: ServerMetric[],
  t: (s: string) => string,
): MetricChart {
  const ts = M.map((m) => new Date(m.ts).getTime())
  const pctY = (v: number) => `${Math.round(v)}%`
  const pctV = (v: number) => `${v.toFixed(1)}%`
  if (key === 'cpu')
    // Стек до ста процентов вместе с простоем — та самая картинка, по которой
    // сразу видно, СКОЛЬКО процессора свободно, а не только чем занят занятый.
    return {
      key, title: `CPU · ${t('состав нагрузки')}`, ts, mode: 'stack', yMax: 100,
      fmtY: pctY, fmtV: pctV,
      series: [
        { name: t('система'), color: GRAFANA_CPU.system, values: M.map((m) => m.cpu_system) },
        { name: t('юзер'), color: GRAFANA_CPU.user, values: M.map((m) => m.cpu_user) },
        { name: 'iowait', color: GRAFANA_CPU.iowait, values: M.map((m) => m.cpu_iowait) },
        { name: 'irq', color: GRAFANA_CPU.irq, values: M.map((m) => m.cpu_irq) },
        {
          name: t('простой'),
          color: GRAFANA_CPU.idle,
          values: M.map((m) => {
            // замер без состава процессора — пропуск и у простоя: иначе на месте дыры
            // встала бы полоса «100% свободно», которой никто не измерял
            if (m.cpu_system == null && m.cpu_user == null) return null
            const busy = (m.cpu_system ?? 0) + (m.cpu_user ?? 0) +
              (m.cpu_iowait ?? 0) + (m.cpu_irq ?? 0)
            return Math.max(0, 100 - busy)
          }),
        },
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
      key, title: `CPU · ${t('частота')}`, ts, mode: 'overlay', yNice: true, fmtY: fmtMHz, fmtV: fmtMHz,
      series: [{ name: t('частота'), color: '#5794f2', values: M.map((m) => m.cpu_freq) }],
    }
  if (key === 'temp')
    return {
      key, title: `CPU · ${t('температура')}`, ts, mode: 'overlay', yNice: true,
      fmtY: fmtTempC, fmtV: fmtTempC,
      series: [{ name: t('температура'), color: '#ff9830', values: M.map((m) => m.cpu_temp) }],
    }
  if (key === 'throttle')
    return {
      key, title: `CPU · ${t('троттлинг')}`, ts, mode: 'overlay', yNice: true,
      fmtY: (v) => String(Math.round(v)), fmtV: (v) => String(Math.round(v)),
      series: [{ name: t('троттлинг'), color: '#f2495c', values: M.map((m) => m.cpu_throttle) }],
    }
  if (key === 'oom')
    return {
      key, title: `${t('Память')} · ${t('OOM-киллы')}`, ts, mode: 'overlay', yNice: true,
      fmtY: (v) => String(Math.round(v)), fmtV: (v) => String(Math.round(v)),
      series: [{ name: t('OOM-киллы'), color: '#f2495c', values: M.map((m) => m.oom_kill) }],
    }
  if (key === 'mem')
    return {
      key, title: t('Память'), ts, mode: 'stack', yMax: 100, fmtY: pctY, fmtV: pctV,
      series: [
        {
          name: t('занято'), color: '#ff9830',
          values: M.map((m) =>
            m.mem_free != null && m.mem_cache != null
              ? Math.max(0, 100 - m.mem_free - m.mem_cache)
              : m.mem_percent,
          ),
        },
        { name: t('кэш/буфер'), color: '#fade2a', values: M.map((m) => m.mem_cache) },
        // Свободное — синим до самого верха, как простой у процессора: ресурс, которого
        // осталось много, и выглядит свободным, а не пустым местом над графиком.
        { name: t('свободно'), color: GRAFANA_CPU.idle, values: M.map((m) => m.mem_free) },
      ],
    }
  if (key === 'swap')
    return {
      key, title: `Swap · ${t('↓ загрузка / ↑ выгрузка')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ загрузка'), color: '#73bf69', values: M.map((m) => m.swap_in) },
        { name: t('↑ выгрузка'), color: '#ff9830', values: M.map((m) => m.swap_out) },
      ],
    }
  if (key === 'memwb')
    return {
      key, title: `${t('Память')} · ${t('буфер записи')}`, ts, mode: 'overlay', yNice: 'bytes',
      fmtY: fmtBytes, fmtV: fmtBytes,
      series: [
        { name: t('ожидают записи'), color: '#ff9830', values: M.map((m) => m.mem_dirty) },
        { name: t('запись на диск'), color: '#b877d9', values: M.map((m) => m.mem_writeback) },
      ],
    }
  if (key === 'net')
    return {
      key, title: `${t('Сеть')} · ${t('↓ приём / ↑ отдача')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ приём'), color: '#73bf69', values: M.map((m) => m.net_rx) },
        { name: t('↑ отдача'), color: '#5794f2', values: M.map((m) => m.net_tx) },
      ],
    }
  if (key === 'netifrx' || key === 'netiftx') {
    const tx = key === 'netiftx'
    // общий топ-N набор по пику rx+tx → приём и отдача показывают одни интерфейсы
    const names = topEntities(M, (m) => m.net_ifaces, (x) => x.if, (x) => Math.max(x.rx, x.tx))
    return {
      key, ts, mode: 'overlay', yNice: 'bytes', fmtY: fmtRate, fmtV: fmtRate,
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
      key, ts, mode: 'overlay', yNice: true, fmtY: fmtErr, fmtV: fmtErr,
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
      key, ts, mode: 'overlay', yMax: lat ? undefined : 100, yNice: lat,
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
      key, ts, mode: 'overlay', yNice: true, fmtY: fmtTempC, fmtV: fmtTempC,
      title: `${t('Диск')} · ${t('температура')}`,
      series: names.map((n) => ({
        name: n, color: entColor(n, DISK_PALETTE),
        values: M.map((m) => m.disk_devs?.find((x) => x.dev === n)?.temp ?? null),
      })),
    }
  }
  if (key === 'web') {
    const title = `${t('Веб-сервер')} · ${t('запросов в минуту')}`
    // Стек по логам, как состав CPU: видно не только сколько запросов, но и на какой сайт
    // или под пришел рост. Пять самых нагруженных за окно, остальное - одной полосой.
    const names = webStackNames(M)
    if (!names.length)
      return {
        key, ts, mode: 'overlay', yNice: true, fmtY: fmtCount, fmtV: fmtCount, title,
        series: [{ name: t('запросов/мин'), color: '#5794f2', values: M.map((m) => m.web_rpm) }],
      }
    // минута без разбивки (история до 1.4.62) - пропуск у логов, все уходит в "остальные"
    const val = (m: ServerMetric, n: string) =>
      m.web_top ? (m.web_top.find((x) => x.k === n)?.r ?? 0) : null
    const rest = M.map((m) =>
      m.web_rpm == null ? null : Math.max(0, m.web_rpm - names.reduce((a, n) => a + (val(m, n) ?? 0), 0)),
    )
    return {
      key, ts, mode: 'stack', yNice: true, fmtY: fmtCount, fmtV: fmtCount, title,
      series: [
        ...names.map((n, i) => ({ name: n, color: WEB_COLORS[i], values: M.map((m) => val(m, n)) })),
        ...(rest.some((v) => (v ?? 0) >= 1) || M.some((m) => m.web_rpm != null && !m.web_top)
          ? [{ name: t('остальные'), color: WEB_REST, values: rest }]
          : []),
      ],
    }
  }
  if (key === 'web5xx') {
    const title = `${t('Веб-сервер')} · ${t('ошибок 5xx в минуту')}`
    // Стек по кодам: 502 и 504 - разные истории (апстрим упал / его не дождались), и на
    // одной красной линии их не различить. Код "5xx" шлет хелпер до 0.12, у него кодов
    // нет - такие ошибки идут в "остальные".
    const sum = new Map<string, number>()
    for (const m of M)
      for (const x of m.web_codes ?? []) if (x.c !== '5xx') sum.set(x.c, (sum.get(x.c) ?? 0) + x.n)
    const codes = [...sum.entries()]
      .filter((e) => e[1] > 0)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map((e) => e[0])
      .sort()
    if (!codes.length)
      return {
        key, ts, mode: 'overlay', yNice: true, fmtY: fmtCount, fmtV: fmtCount, title,
        series: [{ name: t('5xx/мин'), color: '#e24d42', values: M.map((m) => m.web_5xx) }],
      }
    const val = (m: ServerMetric, c: string) =>
      m.web_codes ? (m.web_codes.find((x) => x.c === c)?.n ?? 0) : null
    const rest = M.map((m) => {
      if (m.web_5xx == null) return null
      const x = Math.max(0, m.web_5xx - codes.reduce((a, c) => a + (val(m, c) ?? 0), 0))
      // нулевой остаток у минут с разбивкой не рисуем: серая линия легла бы поверх кодов
      return m.web_codes && x < 0.05 ? null : x
    })
    return {
      key, ts, mode: 'stack', yNice: true, fmtY: fmtCount, fmtV: fmtCount, title,
      series: [
        ...codes.map((c, i) => ({
          name: c,
          color: CODE_COLORS[c] ?? CODE_PALETTE[i % CODE_PALETTE.length],
          values: M.map((m) => val(m, c)),
        })),
        // минуты без разбивки (история до 1.4.62) тоже рисуем "остальными", иначе там дыра
        ...(rest.some((v) => (v ?? 0) >= 0.5) || M.some((m) => m.web_5xx != null && !m.web_codes)
          ? [{ name: t('остальные'), color: WEB_REST, values: rest }]
          : []),
      ],
    }
  }
  if (key === 'gpuutil' || key === 'gpumem' || key === 'gputemp' || key === 'gpupower') {
    // по линии на карту, цвет по номеру: GPU 0 на всех графиках одного цвета
    const f = key === 'gpuutil' ? 'u' : key === 'gpumem' ? 'm' : key === 'gputemp' ? 't' : 'p'
    const temp = key === 'gputemp'
    const names = [...new Set(M.flatMap((m) => (m.gpu ?? []).map((x) => x.g)))].sort((a, b) => Number(a) - Number(b))
    const what = key === 'gpuutil' ? t('загрузка') : key === 'gpumem' ? t('память') : temp ? t('температура') : t('питание, % лимита')
    return {
      key, ts, mode: 'overlay', yMax: temp ? undefined : 100, yNice: temp,
      title: `GPU · ${what}`, fmtY: temp ? fmtTempC : pctY, fmtV: temp ? fmtTempC : pctV,
      series: names.map((n) => ({
        name: `GPU ${n}`,
        color: CORE_COLORS[Number(n) % CORE_COLORS.length],
        values: M.map((m) => m.gpu?.find((x) => x.g === n)?.[f] ?? null),
      })),
    }
  }
  if (key === 'conntrack')
    return {
      key, ts, mode: 'overlay', yNice: true, fmtY: fmtNum, fmtV: fmtNum,
      title: `conntrack · ${t('соединения')}`,
      series: [{ name: 'conntrack', color: '#5794f2', values: M.map((m) => m.conntrack_count) }],
    }
  if (key === 'sockets')
    return {
      key, ts, mode: 'overlay', yNice: true, fmtY: fmtNum, fmtV: fmtNum,
      title: t('Сокеты'),
      series: [
        { name: 'TCP', color: '#5794f2', values: M.map((m) => m.sock_tcp) },
        { name: 'time-wait', color: '#ff9830', values: M.map((m) => m.sock_tcp_tw) },
        { name: 'UDP', color: '#73bf69', values: M.map((m) => m.sock_udp) },
      ],
    }
  if (key === 'diskio')
    return {
      key, title: `${t('Диск I/O')} · ${t('↓ чтение / ↑ запись')}`, ts, mode: 'mirror',
      fmtY: fmtRate, fmtV: fmtRate,
      series: [
        { name: t('↓ чтение'), color: '#73bf69', values: M.map((m) => m.disk_read) },
        { name: t('↑ запись'), color: '#ff9830', values: M.map((m) => m.disk_write) },
      ],
    }
  if (key === 'diskiops')
    return {
      key, title: `${t('Диск IOPS')} · ${t('↓ чтение / ↑ запись')}`, ts, mode: 'mirror',
      fmtY: fmtIops, fmtV: fmtIops,
      series: [
        { name: t('↓ чтение'), color: '#73bf69', values: M.map((m) => m.disk_read_iops) },
        { name: t('↑ запись'), color: '#ff9830', values: M.map((m) => m.disk_write_iops) },
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
  // открыть другой раздел панели (из карточки сервера: проблема бэкапа ведет в "Бэкапы")
  onOpen?: (sec: Section, id?: number, srv?: boolean, detailSec?: string) => void
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
export function fmtRel(iso: string | null): string {
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

// Прогноз заполнения из планировщика - только свежий: у замолчавшей ноды он устарел. Возраст
// меряем временем отчета, а не часами браузера (те бывают сдвинуты).
function diskForecast(s: Server, r: ServerReport): DiskForecastItem[] {
  const fc = s.disk_forecast
  if (!fc?.ts || !r.clock_unix || r.clock_unix - fc.ts > 7200) return []
  return fc.items ?? []
}

// Как в алерте: до двух суток - часами, дальше днями
function etaText(h: number, t: TFn): string {
  if (h < 1) return t('меньше чем через час')
  if (h < 48) return t('примерно через {n} ч', { n: Math.round(h) })
  return t('примерно через {n} дн', { n: Math.round(h / 24) })
}

// DNS ноды в шапке детали: время ответа каждого резолвера; медленнее 1,5 с или без ответа -
// красным (тот же порог, что у алерта на бэкенде). В подсказке - как нода ходит в DNS и сколько
// занял последний резолв имени не из кэша.
const DNS_SLOW_MS = 1500
function DnsStatus({ dns }: { dns: DnsInfo }) {
  const { t } = useI18n()
  const ms = (x: { ms: number; err?: string }) => (x.ms >= 0 ? `${x.ms} ${t('мс')}` : x.err && x.err !== 'timeout' ? x.err : t('нет ответа'))
  const bad = (x: { ms: number }) => x.ms < 0 || x.ms > DNS_SLOW_MS
  const mode = dns.mode === 'resolved' ? t('через systemd-resolved') : dns.mode === 'local' ? t('свой резолвер на ноде') : t('резолверы из resolv.conf')
  const miss = dns.miss_ts
    ? `${t('имя не из кэша')}: ${dns.miss_ms >= 0 ? `${dns.miss_ms} ${t('мс')}` : dns.miss_err && dns.miss_err !== 'timeout' ? dns.miss_err : t('не резолвится')} (${fmtRel(new Date(dns.miss_ts * 1000).toISOString())})`
    : ''
  const title = [mode, miss].filter(Boolean).join('\n')
  if (!dns.servers?.length) return <span className="muted" title={title}>{miss || '-'}</span>
  return (
    <span title={title}>
      {dns.servers.map((x, i) => (
        <span key={x.addr}>
          {i > 0 && ', '}
          {x.addr} <span className={bad(x) ? 't-down' : 't-up'}>{ms(x)}</span>
        </span>
      ))}
    </span>
  )
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
  if (v == null) return '-'
  const u = currentLang() === 'en' ? 'ms' : 'мс'
  return `${v >= 10 ? Math.round(v) : v.toFixed(2)} ${u}`
}
// ошибки/дропы (пакетов/сек) — редкие мелкие значения, храним 2 знака
function fmtErr(v?: number | null): string {
  if (!v) return '0'
  return v >= 10 ? String(Math.round(v)) : v.toFixed(2)
}
// Запросы и ошибки в минуту. За 7-30 дней точки усреднены и дробь осмысленна ("0.3
// ошибки в минуту"); округление до целого рисовало на оси "0 0 0 1 1 1".
function fmtCount(v?: number | null): string {
  if (v == null) return '—'
  if (v >= 10000) return `${(v / 1000).toFixed(1)}k`
  if (v >= 10 || Number.isInteger(v)) return String(Math.round(v))
  return String(Math.round(v * 10) / 10)
}
// компактное целое (сокеты/conntrack): 12 345 → «12.3k»
function fmtNum(v?: number | null): string {
  if (v == null) return '-'
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

// Разделы карточки сервера: меню слева по группам, на экране один раздел. Раньше все шло одной
// лентой, а над ней копились заметка, зависимости, заглушки, обновления ОС и плитки, и меню
// уезжало под них. Меню, карта и строки сводки общие с остальными карточками (cardLayout)
const DETAIL_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'cpu', label: 'CPU', group: 'Ресурсы' },
  { id: 'mem', label: 'Память', group: 'Ресурсы' },
  { id: 'disk', label: 'Диск', group: 'Ресурсы' },
  { id: 'net', label: 'Сеть', group: 'Ресурсы' },
  { id: 'gpu', label: 'GPU', group: 'Ресурсы' },
  { id: 'ports', label: 'Порты', group: 'Безопасность' },
  { id: 'access', label: 'SSH и доступы', group: 'Безопасность' },
  { id: 'updates', label: 'Обновления', group: 'Безопасность' },
  { id: 'vulns', label: 'Уязвимости', group: 'Безопасность' },
  { id: 'units', label: 'Службы', group: 'Система' },
  { id: 'proc', label: 'Процессы', group: 'Система' },
  { id: 'pkgs', label: 'Пакеты', group: 'Система' },
  { id: 'journal', label: 'Журнал', group: 'Система' },
  { id: 'changes', label: 'Изменения', group: 'Система' },
  { id: 'web', label: 'Веб', group: 'Сервисы' },
  { id: 'deps', label: 'Зависимости', group: 'Сервисы' },
  { id: 'alerts', label: 'Алерты', group: 'События' },
  { id: 'timeline', label: 'Хронология', group: 'События' },
]
const SEC_IDS = new Set(DETAIL_SECTIONS.map((x) => x.id))
// разделы с графиками: у них переключатель периода
const CHART_SECS = new Set(['cpu', 'mem', 'disk', 'net', 'gpu', 'web'])
// карта сервера на сводке: ресурсы там плитками, здесь остальное, у чего есть состояние
const AREA_SECS = ['ports', 'access', 'updates', 'vulns', 'units', 'proc', 'changes', 'web', 'deps', 'alerts']
// куда ведут проблемы, которые разбираются в другом разделе панели
const SECTION_LABEL: Record<string, string> = { services: 'Сервисы', kuber: 'Кубер', backups: 'Бэкапы', docker: 'Докер' }

// Какому разделу принадлежит карточка: ссылки из алертов и с главной ведут на карточку
// (?sec=diskfill), а открыть надо ее раздел. conn - раздел старой ленты, для старых ссылок;
// часы и DNS разбираются на сводке
const SEC_OF_CARD: Record<string, string> = {
  cores: 'cpu', freq: 'cpu', throttle: 'cpu', temp: 'cpu', cpueat: 'cpu',
  swap: 'mem', memwb: 'mem', oom: 'mem', memleak: 'mem',
  netifrx: 'net', netiftx: 'net', neterr: 'net', nethealth: 'net', links: 'net', traffic: 'net', netusers: 'net',
  conntrack: 'net', sockets: 'net',
  diskio: 'disk', diskiops: 'disk', diskutil: 'disk', disklat: 'disk', disktemp: 'disk', diskfill: 'disk',
  diskhealth: 'disk', du: 'disk', diskwrite: 'disk',
  exposure: 'ports', conn: 'ports', ssh: 'access', osupd: 'updates', software: 'updates',
  unitloop: 'units', noboot: 'units', pressure: 'proc', web5xx: 'web',
  clock: 'overview', dns: 'overview',
  gpuutil: 'gpu', gpumem: 'gpu', gputemp: 'gpu', gpupower: 'gpu',
}
// раздел, к которому ведет ссылка ?sec=: сам раздел или тот, где лежит карточка
function secOfLink(sec?: string | null): string | null {
  if (!sec) return null
  return SEC_IDS.has(sec) ? sec : (SEC_OF_CARD[sec] ?? null)
}

// Замечания и строка по разделам. Проблемы те же, что в "Что сломано" (srvIssues), советы - те
// же, что в "Требует действий" на главной. Уязвимости только справка, но на карте видно, что
// они есть. Проблемы, которые разбираются в другом разделе панели (Бэкапы, Кубер), сюда не
// попадают: они на сводке строками со ссылкой туда
function serverSecInfo(c: {
  s: Server
  t: TFn
  issues: SrvIssue[]
  vulns: ServerVulns | null
  kids: { id: number; name: string }[]
  servers: Server[]
  mutes: MuteItem[]
}): Record<string, SecInfo> {
  const { s, t, issues } = c
  const r = s.last_report ?? {}
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, level: number, text: string, since?: string | null) =>
    (items[sec] ??= []).push({ level: level >= 2 ? 2 : 1, text, since })
  for (const i of issues) {
    if (i.section || i.kind === 'offline') continue
    add(secOfLink(i.sec) ?? 'overview', i.tone === 't-down' ? 2 : 1, i.text, i.since)
  }
  for (const a of s.os_advice ?? []) add('updates', a.level, a.text)
  for (const a of s.sw_advice ?? []) add('updates', a.level, a.text)
  const nb = s.noboot_advice?.units ?? []
  if (nb.length) add('units', 1, t('после перезагрузки не запустятся {list}: работают, но не включены', { list: shortList(nb, t) }))
  // как в карточке SSH: при проблеме совет не повторяем
  if (!issues.some((i) => i.kind === 'ssh_risk')) for (const a of s.ssh_advice ?? []) add('access', a.level, a.text)
  const ac = s.access_advice
  if (ac?.stale.length) add('access', 1, t('учетки с доступом, в которые давно не входят: {list}', { list: shortList(ac.stale, t) }))
  if (ac?.weak.length) add('access', 1, t('слабые SSH-ключи у {list}', { list: shortList(ac.weak, t) }))

  // коротко, что в разделе, когда замечаний нет
  const ok: Record<string, string> = {}
  const ex = s.exposure
  if (ex) {
    const open = [...new Set(ex.ports.filter((p) => p.state === 'open').map((p) => p.port))].sort((a, b) => a - b)
    ok.ports = open.length
      ? t('снаружи открыто: {list}', { list: shortList(open.map(String), t, 6) })
      : t('снаружи ничего не открыто')
  } else ok.ports = t('снаружи еще не проверяли')
  const ssh = s.ssh
  ok.access = [
    ssh ? `SSH ${ssh.ports.join(', ')}` : '',
    ssh ? (ssh.password ? t('вход по паролю') : t('только ключи')) : '',
    ssh?.guard ?? '',
    s.access?.accounts.length ? t('учеток с доступом: {n}', { n: s.access.accounts.length }) : '',
  ].filter(Boolean).join(', ')
  const u = r.os_upd
  if (u)
    ok.updates = [
      u.updates > 0
        ? t('ждут установки: {n}, безопасности {s}', { n: u.updates, s: u.security })
        : u.updates === 0 ? t('все обновления установлены') : '',
      u.reboot ? t('нужна перезагрузка') : '',
    ].filter(Boolean).join(', ')
  const v = c.vulns
  ok.vulns = !v ? '' : !v.release ? t('сверка пока только для Ubuntu') : !v.feed_ts ? t('база уязвимостей еще не скачана') : t('важных нет')
  ok.units = unitsBlock(r) ? t('упавших служб нет') : t('нет данных')
  ok.gpu = gpuSecLine(s, t)
  const top = r.top_cpu?.[0]
  ok.proc = top ? t('больше всех CPU: {name}, {p}', { name: top.comm, p: `${top.cpu.toFixed(1)}%` }) : ''
  const last = s.changes?.log[0]
  ok.changes = last
    ? t('последнее: {text}, {d}', { text: last.text, d: new Date(last.ts).toLocaleDateString() })
    : t('изменений не было')
  const wr = webRate(r)
  ok.web = wr
    ? t('{n} запросов/мин, 5xx {p}', { n: fmtCount(wr.rpm), p: `${(wr.rpm ? ((wr.e5 ?? 0) / wr.rpm) * 100 : 0).toFixed(2)}%` })
    : t('сейчас нет данных')
  const parent = s.parent_id ? c.servers.find((x) => x.id === s.parent_id) : undefined
  const behind = c.servers.filter((x) => x.parent_id === s.id).length
  ok.deps = [
    parent ? t('стоит за {name}', { name: parent.name }) : '',
    behind ? t('за ним серверов: {n}', { n: behind }) : '',
    c.kids.length ? t('зависят мониторов: {n}', { n: c.kids.length }) : '',
  ].filter(Boolean).join(', ') || t('связей нет: ни за кем не стоит, мониторов на нем нет')
  const known = Object.keys(s.known_issues ?? {}).length
  ok.alerts = [
    c.mutes.length ? t('заглушено: {list}', { list: muteBrief(c.mutes, t) }) : t('ничего не заглушено'),
    known ? t('известных проблем: {n}', { n: known }) : '',
  ].filter(Boolean).join(', ')

  const out: Record<string, SecInfo> = {}
  for (const { id } of DETAIL_SECTIONS) out[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // справка и заглушки - желтым, но не проблемой: на карте видно, что там не пусто
  if (v?.release && v.feed_ts && v.summary.pkgs)
    out.vulns = { level: 1, n: v.summary.pkgs, line: t('важных CVE: {n}, пакетов: {p}', { n: v.summary.cves, p: v.summary.pkgs }) }
  if (c.mutes.length && !out.alerts.n) out.alerts = { level: 1, n: c.mutes.length, line: ok.alerts }
  if (parent && !parent.online && !out.deps.n)
    out.deps = { level: 1, n: 0, line: t('стоит за {name}, он не на связи', { name: parent.name }) }
  const idle = [!ex && 'ports', !unitsBlock(r) && 'units', !wr && 'web', !!v && (!v.release || !v.feed_ts) && 'vulns',
    !parent && !behind && !c.kids.length && 'deps']
  for (const id of idle) if (id && !out[id].level) out[id].idle = true
  return out
}

// "За сутки" на сводке: сколько проблем появилось, сколько ушло и сколько было изменений на
// сервере, и последние строки из той же хронологии. Действия людей в панели сюда не идут
const DAY_CHANGE = new Set(['deploy', 'crash', 'state', 'system', 'ports', 'access', 'config'])
function DayBlock({ day, onMore }: { day: TimelineResult | null; onMore: () => void }) {
  const { t } = useI18n()
  if (!day) return null
  const evs = day.events.filter((e) => e.cat === 'alert' || DAY_CHANGE.has(e.cat))
  const up = evs.filter((e) => e.cat === 'alert' && !e.recovery).length
  const gone = evs.filter((e) => e.cat === 'alert' && e.recovery).length
  const ch = evs.length - up - gone
  const when = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  return (
    <div className="day-box">
      <div className="day-head">
        <b>{t('За сутки')}</b>
        {evs.length > 0 ? (
          <>
            <span className="day-cnt">{t('появилось')} <b className={up ? 't-down' : ''}>{up}</b></span>
            <span className="day-cnt">{t('ушло')} <b className={gone ? 't-up' : ''}>{gone}</b></span>
            <span className="day-cnt">{t('изменений на сервере')} <b>{ch}</b></span>
          </>
        ) : (
          <span className="muted small">{t('алертов не было, на сервере ничего не менялось')}</span>
        )}
        <button className="ghost small row-act" onClick={onMore}>
          {t('вся хронология')} ›
        </button>
      </div>
      {evs.slice(0, 5).map((e, i) => (
        <div key={i} className={`day-row${e.cat === 'alert' && !e.recovery ? ' day-row-fire' : ''}`}>
          <span className="day-ic" aria-hidden="true">{e.cat === 'alert' ? (e.recovery ? '✅' : '🔥') : (TL_ICON[e.cat] ?? '•')}</span>
          <span className="day-text">{e.text}</span>
          <span className="day-time">{when(e.ts)}</span>
        </div>
      ))}
    </div>
  )
}

// Мини-график в плитке сводки: период одной линией, без осей. max - верх шкалы (у процентов
// 100, у сети - пик за период)
function TileSpark({ vals, max, color }: { vals: (number | null)[]; max?: number; color: string }) {
  const pts = vals.filter((v): v is number => v != null)
  if (pts.length < 2) return null
  // не больше 60 точек: средним по корзинам, чтобы за 90 дней не рисовать тысячи
  const n = Math.min(60, pts.length)
  const avg = Array.from({ length: n }, (_, i) => {
    const part = pts.slice(Math.floor((i * pts.length) / n), Math.floor(((i + 1) * pts.length) / n))
    return part.reduce((a, x) => a + x, 0) / (part.length || 1)
  })
  const hi = max ?? Math.max(...avg, 1)
  const d = avg.map((x, i) => `${((i / (n - 1)) * 100).toFixed(1)},${(23 - Math.min(x / hi, 1) * 21).toFixed(1)}`).join(' ')
  return (
    <svg className="tile-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={d} fill="none" stroke={color} strokeWidth="1.3" vectorEffect="non-scaling-stroke" />
    </svg>
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

// Ошибки 5xx по логам за сутки: куда ведёт ссылка из алерта. Где (домены, под или
// контейнер), сколько и в пике, какими кодами и на каких путях. Коды и пути присылает
// helper с 0.12; у старого - только числа.
function WebErrorList({
  server: s,
  t,
  onChanged,
  onUnauthorized,
}: {
  server: Server
  t: (s: string, vars?: Record<string, string | number>) => string
  onChanged: () => void
  onUnauthorized: () => void
}) {
  const serverId = s.id
  const { isViewer } = useAuth()
  const [rows, setRows] = useState<WebErrorRow[] | null>(null)
  const [picking, setPicking] = useState<string | null>(null) // лог, для которого выбирают срок
  const [openLines, setOpenLines] = useState<string | null>(null) // у какого лога раскрыты строки
  // Все строки с 5xx лога за сутки одним файлом: смотреть ошибку, не заходя на сервер
  const downloadLines = async (r: WebErrorRow) => {
    try {
      const all = await serverWebErrorLines(s.id, r.key || r.label || r.log, 24)
      const blob = new Blob([all.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
      a.href = url
      a.download = `${s.name}_5xx_${stamp}.txt`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
    }
  }
  const [busy, setBusy] = useState(false)
  // Заглушить 5xx одного лога. Сюда ведет ссылка из алерта, поэтому глушить - прямо
  // тут: боты с их 500 на robots.txt одного домена не должны держать в тишине весь
  // сервер, остальные логи продолжают алертить.
  const mute = async (kind: string, hours: number) => {
    setBusy(true)
    try {
      if (hours < 0) {
        await updateServer(s.id, { alert_mutes: [...new Set([...(s.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((s.alert_mutes ?? []).includes(kind))
          await updateServer(s.id, { alert_mutes: (s.alert_mutes ?? []).filter((x) => x !== kind) })
        if (s.alert_snoozes?.[kind]) await snoozeServerAlert(s.id, kind, 0)
      } else await snoozeServerAlert(s.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    let alive = true
    const load = () =>
      serverWebErrors(serverId, 24)
        .then((r) => alive && setRows(r))
        .catch(() => alive && setRows([]))
    load()
    const id = window.setInterval(load, 60000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [serverId])
  if (rows == null) return null
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  if (rows.length === 0)
    return (
      <div className="chart-card net-card net-card-wide">
        <div className="net-head">
          <div className="chart-cap">{t('Ответы 5xx за сутки')}</div>
        </div>
        <div className="muted small">{t('За сутки ответов 5xx не было.')}</div>
      </div>
    )
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Ответы 5xx за сутки')}</div>
        <span className="muted small">{t('по логам веб-сервера')}</span>
      </div>
      {rows.map((r) => {
        const codes = Object.entries(r.codes || {}).sort((a, b) => b[1] - a[1])
        const kind = WEB_MUTE + (r.key || r.label || r.log)
        const perm = (s.alert_mutes ?? []).includes(kind)
        const until = s.alert_snoozes?.[kind]
        const snoozed = !!until && new Date(until).getTime() > Date.now()
        return (
          <div key={r.key || r.log} className={`web-err-row${perm || snoozed ? ' web-err-muted' : ''}`}>
            <div className="web-err-head">
              <span className="web-err-where mono">
                {r.label || r.log.split('/').pop()}
                {r.gone && (
                  <span className="type-chip off" title={t('этого лога в отчете больше нет: сайты разнесли по своим логам или контейнер удалили, ошибки ниже - история')}>
                    {t('лог больше не пишется')}
                  </span>
                )}
              </span>
              <span className="web-err-count">
                {t('{n} ошибок за {m} мин, пик {p}/мин', { n: r.errors, m: r.minutes, p: r.peak })}
              </span>
            </div>
            {(perm || snoozed) ? (
              <div className="web-err-line small">
                <span className="snooze-active-chip snooze-perm">
                  🔇 {perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(until!) }) })}
                  {!isViewer && (
                    <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(kind, 0)}>
                      ✕
                    </button>
                  )}
                </span>
              </div>
            ) : !isViewer && (
              <div className="web-err-line small web-err-mute">
                {picking === kind ? (
                  <>
                    <span className="muted">🔕 {t('не алертить по этому логу:')}</span>
                    {[
                      { h: 1, l: t('1 час') },
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={busy} onClick={() => mute(kind, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={busy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </>
                ) : (
                  <button className="ghost small" onClick={() => setPicking(kind)}>
                    🔕 {t('не алертить по этому логу')}
                  </button>
                )}
              </div>
            )}
            {/* общий лог нескольких сайтов, разложенный по доменам ($host в формате) */}
            {(r.hosts?.length ?? 0) > 0 && (
              <div className="web-err-line small">
                <span className="muted">{t('по доменам')}: </span>
                <span className="mono">{(r.hosts ?? []).slice(0, 5).map((x) => `${x.h} (${x.n})`).join(', ')}</span>
              </div>
            )}
            {codes.length > 0 && (
              <div className="web-err-line small">
                <span className="muted">{t('коды')}: </span>
                <span className="mono">{codes.map(([c, n]) => `${c} - ${n}`).join(', ')}</span>
              </div>
            )}
            {r.paths.length > 0 && (
              <div className="web-err-line small">
                <span className="muted">{t('где падает')}: </span>
                <span className="mono">{r.paths.slice(0, 5).map((x) => `${x.p} (${x.n})`).join(', ')}</span>
              </div>
            )}
            {(r.lines?.length ?? 0) > 0 && (
              <div className="web-err-line small web-err-mute">
                <button className="ghost small" onClick={() => setOpenLines(openLines === kind ? null : kind)}>
                  📄 {t('строки ошибок ({n})', { n: r.lines_n })}
                </button>
                <button className="ghost small" onClick={() => downloadLines(r)}>
                  {t('скачать .txt')}
                </button>
              </div>
            )}
            {openLines === kind && <pre className="web-err-lines mono">{r.lines.join('\n')}</pre>}
            <div className="web-err-line muted small">
              {t('с {a} по {b}', { a: fmt(r.first_ts), b: fmt(r.last_ts) })}
            </div>
          </div>
        )
      })}
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

type TFn = (s: string, vars?: Record<string, string | number>) => string

const duLevel = (l: string, t: TFn) =>
  l === 'safe' ? t('безопасно') : l === 'careful' ? t('осторожно') : t('вручную')

function duLabel(it: DiskUsageItem, t: TFn): string {
  switch (it.id) {
    case 'journal': return t('Журнал systemd')
    case 'rotated-logs': return t('Старые ротированные логи: {n} файлов старше 7 дней', { n: it.count ?? 0 })
    case 'big-log': return t('Большой лог {p}', { p: it.path })
    case 'apt-cache': return t('Кэш пакетов apt')
    case 'dnf-cache': return t('Кэш пакетов dnf/yum')
    case 'tmp-dir':
    case 'vartmp-dir':
      return it.kind === 'restic'
        ? t('{p}: похоже на копию репозитория restic', { p: it.path })
        : it.kind === 'dump'
          ? t('{p}: дампы баз', { p: it.path })
          : it.kind === 'archive'
            ? t('{p}: архивы', { p: it.path })
            : t('{p}: старые файлы', { p: it.path })
    case 'tmp-old': return t('Файлы в /tmp старше 10 дней')
    case 'vartmp-old': return t('Файлы в /var/tmp старше 30 дней')
    case 'coredumps': return t('Дампы памяти упавших программ')
    case 'crash-reports': return t('Отчеты о падениях в /var/crash')
    case 'restic-cache': return t('Кэш restic {p}', { p: it.path })
    case 'docker-dangling': return t('Docker: образы без тега')
    case 'docker-build-cache': return t('Docker: кэш сборки')
    case 'docker-images': return t('Docker: образы, которые не использует ни один контейнер')
    case 'docker-containers': return t('Docker: остановленные контейнеры')
    case 'docker-volumes': return t('Docker: тома, не подключенные ни к одному контейнеру')
    case 'container-log': return t('Лог контейнера {name}', { name: it.name ?? '?' })
    case 'deleted-open': return t('Удаленные файлы, которые еще держат процессы')
    case 'old-kernels': return t('Старые ядра в /boot: {n} шт.', { n: it.count ?? 0 })
    default: return it.id
  }
}

function duHint(it: DiskUsageItem, t: TFn): string {
  switch (it.id) {
    case 'journal': return t('Останутся последние 200 МБ записей.')
    case 'big-log': return t('Содержимое лога пропадет. Чтобы не рос снова, проверьте для него logrotate.')
    case 'tmp-dir':
    case 'vartmp-dir':
      return it.fresh
        ? t('Старых файлов: {n}, но есть и свежие: каталогом пользуются. Удаляйте вручную, если уверены.', { n: it.count ?? 0 })
        : t('Старых файлов: {n}. Если каталог не нужен, удаляйте целиком.', { n: it.count ?? 0 })
    case 'tmp-old':
    case 'vartmp-old': return t('Обычно это мусор, но некоторые программы держат там рабочие файлы.')
    case 'restic-cache': return t('Пересоздастся при следующем бэкапе, тот пойдет медленнее. Не удаляйте во время бэкапа.')
    case 'docker-build-cache': return t('Следующая сборка образов пойдет без кэша.')
    case 'docker-images': return t('Скачаются или соберутся заново, когда понадобятся; образы, собранные только локально, придется собирать снова.')
    case 'docker-containers': return t('Контейнеры удалятся вместе с данными внутри них.')
    case 'docker-volumes': return t('В томах могут быть данные: посмотрите список и удаляйте вручную.')
    case 'container-log': return t('Содержимое лога пропадет. Чтобы не рос снова, задайте log-opts max-size в /etc/docker/daemon.json.')
    case 'deleted-open':
      return t('Место освободится после перезапуска: {p}.', {
        p: (it.procs ?? []).map((p) => `${p.comm} (pid ${p.pid})`).join(', ') || '?',
      })
    case 'old-kernels': return t('apt удалит ядра, которые больше не нужны; текущее останется.')
    default: return ''
  }
}

// Самые большие каталоги раздела деревом: родитель - ближайший каталог списка, который
// является началом пути; дети по убыванию размера, полоска - доля от занятого.
// Число файлов коротко: 4.1 млн, 700 тыс.
function fmtFiles(n: number, t: TFn): string {
  if (n >= 1e6) return t('{n} млн', { n: (n / 1e6).toFixed(1) })
  if (n >= 1e3) return t('{n} тыс.', { n: Math.round(n / 1e3) })
  return String(n)
}

function DuTree({ fs, t, files = false }: { fs: DiskUsageFs; t: TFn; files?: boolean }) {
  const [all, setAll] = useState(false)
  const state = files ? fs.itop_state : fs.top_state
  if (!files && fs.top_state === 'none') return null
  if (files && state === 'failed')
    return <div className="muted small">{t('Посчитать файлы не вышло: du на ноде не знает --inodes.')}</div>
  if (!files && fs.top_state === 'skipped')
    return (
      <div className="muted small">
        {t('Раздел слишком большой для полного обхода ({n} млн файлов), смотрите находки ниже.', {
          n: (fs.inodes / 1e6).toFixed(1),
        })}
      </div>
    )
  const top: DiskUsageDir[] = (files ? (fs.itop ?? []).map((x) => ({ path: x.path, bytes: x.files })) : [...fs.top]).sort(
    (a, b) => b.bytes - a.bytes,
  )
  const parentOf = (p: string) => {
    let best = ''
    for (const q of top)
      if (q.path !== p && p.startsWith(q.path.replace(/\/$/, '') + '/') && q.path.length > best.length) best = q.path
    return best
  }
  const kids = new Map<string, DiskUsageDir[]>()
  for (const d of top) {
    const pp = parentOf(d.path)
    kids.set(pp, [...(kids.get(pp) ?? []), d])
  }
  const rows: { d: DiskUsageDir; depth: number; parent: string }[] = []
  const walk = (p: string, depth: number) => {
    for (const d of kids.get(p) ?? []) {
      rows.push({ d, depth, parent: p })
      walk(d.path, depth + 1)
    }
  }
  walk('', 0)
  const shown = all ? rows : rows.slice(0, 12)
  const base = (files ? fs.iused : fs.used) || 1
  return (
    <div className="du-tree">
      {shown.map(({ d, depth, parent }) => (
        <div key={d.path} className="du-row" style={{ paddingLeft: `${depth * 14}px` }} title={d.path}>
          <span className="mono du-path">{parent ? d.path.slice(parent.length).replace(/^\//, '') : d.path}</span>
          <span className="du-bar">
            <span style={{ width: `${Math.min(100, (d.bytes / base) * 100)}%` }} />
          </span>
          <span className="mono du-size">{files ? fmtFiles(d.bytes, t) : fmtBytes(d.bytes)}</span>
        </div>
      ))}
      {rows.length > 12 && (
        <button className="ghost small du-more" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : `${t('показать все')} (${rows.length})`}
        </button>
      )}
      {state === 'partial' && (
        <div className="muted small">{t('Обход не успел целиком, показано то, что посчитано.')}</div>
      )}
      {!rows.length && state === 'ok' && (
        <div className="muted small">{files ? t('Каталогов с массой файлов нет.') : t('Крупных каталогов нет.')}</div>
      )}
    </div>
  )
}

// Здоровье дисков (helper diskhealth-setup): программный RAID, SMART, пропавшие диски и
// ошибки ввода-вывода в журнале ядра. Оценка та же, что у алерта "Диск: поломка" в
// коллекторе: красное - данные под угрозой сейчас, желтое - диск пора менять.
type DhTone = 'up' | 'degraded' | 'down'
const DH_GROW: [keyof DiskHealthDisk['grow'], string][] = [
  ['realloc', 'переназначенные сектора'],
  ['pending', 'нечитаемые сектора'],
  ['uncorr', 'неисправимые ошибки'],
  ['media', 'ошибки носителя'],
  ['crc', 'ошибки передачи (CRC)'],
]

function dhFailing(d: DiskHealthDisk): boolean {
  return d.failing || /FAIL/i.test(d.health)
}

type DhLv = 'ok' | 'warn' | 'bad'
const DH_LV: Record<DhTone, DhLv> = { up: 'ok', degraded: 'warn', down: 'bad' }

function dhTone(d: DiskHealthDisk): DhTone {
  const crit = parseInt(d.crit || '0', 16) || 0
  const worn = (d.wear ?? 0) >= 100
  if (!d.ok || (dhFailing(d) && !worn) || crit & ~0x02) return 'down'
  if (dhFailing(d) || crit & 0x02 || DH_GROW.some(([k]) => (d.grow?.[k] ?? 0) > 0) || (d.wear ?? 0) >= 90)
    return 'degraded'
  return 'up'
}

// дата из ISO "2027-01-05" как 05.01.2027
function isoDay(d: string): string {
  const [y, m, dd] = d.split('-')
  return y && m && dd ? `${dd}.${m}.${y}` : d
}

// прогноз износа диска одной строкой: когда 90% и 100% и откуда скорость
function wearForecast(w: DiskWear, t: TFn): string {
  return [
    w.d90 ? t('90% примерно к {d}', { d: isoDay(w.d90) }) : '',
    w.d100 ? t('100% примерно к {d}', { d: isoDay(w.d100) }) : '',
    t('{r}% в сутки', { r: w.rate.toFixed(w.rate < 0.1 ? 3 : 2) }),
    w.src === 'lifetime' ? t('по средней за всю жизнь диска') : t('по замеру'),
  ].filter(Boolean).join(' · ')
}

function dhPoh(h: number, t: TFn): string {
  if (h < 48) return t('{n} ч', { n: h })
  if (h < 24 * 365) return t('{n} дн', { n: Math.round(h / 24) })
  return t('{n} г.', { n: (h / 24 / 365).toFixed(1) })
}

function dhDiskState(d: DiskHealthDisk, t: TFn): string {
  if (!d.ok) {
    if (d.err === 'dead') return t('не определяется')
    if (d.err.startsWith('no_namespace')) return t('контроллер есть, диска нет')
    if (d.err.startsWith('state_')) return t('отключен ядром')
    return t('не отвечает')
  }
  if (dhFailing(d)) return 'SMART: FAILED'
  if (d.err === 'nosmart') return t('SMART недоступен')
  return d.health || '—'
}

function dhSync(sync: string, t: TFn): string {
  const m = sync.match(/^(recovery|resync|reshape|check)=([\d.]+)%$/)
  if (!m) return ''
  const n = Math.round(parseFloat(m[2]))
  return m[1] === 'check'
    ? t('плановая проверка {n}%', { n })
    : m[1] === 'reshape'
      ? t('перестройка {n}%', { n })
      : t('восстановление {n}%', { n })
}

// Кто пишет на диск за последний час (агент 2.33): контейнеры, поды, юниты systemd по io.stat их
// cgroup. Рядом со здоровьем и прогнозом износа: когда SSD изнашивается быстро, первым вопросом
// идет, кто именно пишет. Сутки - тот же темп, растянутый на 24 часа
// Плашка SMART - только сам статус: PASSED зеленый, даже когда плитка желтая из-за износа или
// секторов (это видно по рамке и по полосе). Выработавший ресурс SSD SMART тоже зовет FAILED, но
// такой диск еще работает - плашка желтая, как его строка в алерте
function dhChip(d: DiskHealthDisk): DhLv | 'mut' {
  if (d.err === 'virtual' || d.err === 'nosmart') return 'mut'
  if (!d.ok) return 'bad'
  if (dhFailing(d)) return (d.wear ?? 0) >= 100 ? 'warn' : 'bad'
  return 'ok'
}

// Полоса износа: от 90% красная, от 70% или при скором прогнозе желтая
function dhWearLv(d: DiskHealthDisk, fc?: DiskWear): DhLv {
  const w = d.wear ?? 0
  return w >= 90 ? 'bad' : w >= 70 || fc?.soon ? 'warn' : 'ok'
}

// Раздел массива -> диск: sda2 -> sda, nvme0n1p2 -> nvme0n1, целый диск как есть
function dhDiskOf(part: string): string {
  const m = /^(.*\d)p\d+$/.exec(part) || /^(\D+)\d+$/.exec(part)
  return m ? m[1] : part
}

function dhRaidDown(a: DiskHealthRaid): boolean {
  return a.state === 'inactive' || (a.total > 0 && a.active < a.total)
}

function dhSyncPct(sync: string): number | null {
  const m = sync.match(/^(?:recovery|resync|reshape|check)=([\d.]+)%$/)
  return m ? Math.round(parseFloat(m[1])) : null
}

// systemd-detect-virt -> как гипервизор называют люди
const DH_VIRT: Record<string, string> = {
  kvm: 'KVM', qemu: 'QEMU', vmware: 'VMware', microsoft: 'Hyper-V', xen: 'Xen', oracle: 'VirtualBox',
  amazon: 'AWS', google: 'Google Cloud', parallels: 'Parallels', bochs: 'Bochs', powervm: 'PowerVM', zvm: 'z/VM',
}

// Диск ВМ по имени из disk_devs агента: helper до 0.2 виртуальные диски не перечислял
function dhVirtualDisk(dev: string): DiskHealthDisk {
  return {
    dev, model: '', serial: '', type: '', size: null, rota: null, ok: true, err: 'virtual', health: '', failing: false,
    temp: null, wear: null, spare: null, spare_thr: null, media: null, crit: '', poh: null, realloc: null,
    pending: null, uncorr: null, crc: null, grow: {},
  }
}

const dhCrit = (d: DiskHealthDisk) => parseInt(d.crit || '0', 16) || 0

// Картинка диска по типу: планка NVMe, 2.5" SSD, HDD с пластиной; у диска ВМ контур пунктиром
function DhIcon({ d, lv }: { d?: DiskHealthDisk; lv: string }) {
  const p = {
    className: `dh-ico net-tone-${lv}`, width: 30, height: 30, viewBox: '0 0 26 26', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.5, 'aria-hidden': true,
  }
  if (d?.err === 'virtual')
    return (
      <svg {...p}>
        <rect x="4" y="2.5" width="18" height="21" rx="2" strokeDasharray="3 2.2" />
        <rect x="8" y="7" width="10" height="7" rx="1" fill="currentColor" stroke="none" opacity=".3" />
        <path d="M9 18.5h8" />
      </svg>
    )
  if (d?.type === 'nvme')
    return (
      <svg {...p}>
        <rect x="2" y="8" width="22" height="10" rx="1.5" />
        <path d="M5 18v-2M8 18v-2M11 18v-2" />
        <rect x="13" y="10.5" width="4" height="5" rx=".5" fill="currentColor" stroke="none" opacity=".55" />
        <rect x="18.5" y="10.5" width="3.5" height="5" rx=".5" fill="currentColor" stroke="none" opacity=".55" />
        <circle cx="5" cy="13" r="1" fill="currentColor" stroke="none" />
      </svg>
    )
  if (d?.rota === 1)
    return (
      <svg {...p}>
        <rect x="4" y="2.5" width="18" height="21" rx="2" />
        <circle cx="13" cy="11" r="5.5" />
        <circle cx="13" cy="11" r="1.2" fill="currentColor" stroke="none" />
        <path d="M19 19l-4.5-5.5" />
        <circle cx="7" cy="20.5" r=".8" fill="currentColor" stroke="none" />
      </svg>
    )
  return (
    <svg {...p}>
      <rect x="4" y="2.5" width="18" height="21" rx="2" />
      <rect x="7.5" y="6" width="11" height="8" rx="1" fill="currentColor" stroke="none" opacity=".35" />
      <path d="M8 20h10M8 17.5h4" />
    </svg>
  )
}

type DhPill = { lv: DhLv | 'info' | 'mut'; text: string }

// Сводка вместо плиток-счетчиков: что именно не так и с каким диском или массивом
function dhSummary(dh: DiskHealth, disks: DiskHealthDisk[], wear: DiskWear[] | undefined, t: TFn): DhPill[] {
  const out: DhPill[] = []
  const raid = dh.raid ?? []
  const down = raid.filter(dhRaidDown)
  for (const a of down)
    out.push({
      lv: 'bad',
      text: a.state === 'inactive'
        ? t('{dev} не собран', { dev: a.dev })
        : t('{dev} {lvl}: работает {a} из {n}', { dev: a.dev, lvl: a.level, a: a.active, n: a.total }) +
          (a.failed ? `, ${t('сбойный {d}', { d: a.failed })}` : ''),
    })
  if (raid.length && !down.length)
    out.push({
      lv: 'ok',
      text: raid.length === 1 ? t('{dev} собран', { dev: raid[0].dev }) : t('массивы {list} собраны', { list: raid.map((a) => a.dev).join(', ') }),
    })
  for (const a of raid) {
    const s = dhSync(a.sync, t)
    if (s) out.push({ lv: 'info', text: `${a.dev}: ${s}` })
  }
  for (const m of dh.missing ?? []) out.push({ lv: 'bad', text: t('{dev} пропал', { dev: m.dev }) })
  let ok = 0
  let phys = 0
  const nosmart: string[] = []
  for (const d of disks) {
    if (d.err === 'virtual') continue
    phys++
    const tn = dhTone(d)
    const fc = (wear ?? []).find((w) => w.serial === d.serial)
    if (tn === 'up') {
      if (fc?.soon && fc.d90) out.push({ lv: 'warn', text: `${d.dev}: ${t('90% износа примерно к {d}', { d: isoDay(fc.d90) })}` })
      else if (d.err === 'nosmart') nosmart.push(d.dev)
      else ok++
      continue
    }
    const why = [
      !d.ok || dhFailing(d) ? dhDiskState(d, t) : '',
      dhCrit(d) ? t('предупреждение NVMe {c}', { c: d.crit }) : '',
      (d.wear ?? 0) >= 90 ? t('износ {n}%', { n: d.wear ?? 0 }) : '',
      ...DH_GROW.filter(([k]) => (d.grow?.[k] ?? 0) > 0).map(([k, label]) => t('за сутки: {v}', { v: `${t(label)} +${d.grow[k]}` })),
    ].filter(Boolean)
    out.push({ lv: DH_LV[tn], text: `${d.dev}: ${why.join(', ') || dhDiskState(d, t)}` })
  }
  if (nosmart.length) out.push({ lv: 'mut', text: t('SMART не читается: {list}', { list: nosmart.join(', ') }) })
  if (ok) out.push({ lv: 'ok', text: ok === phys ? t('все диски в порядке') : t('остальные диски в порядке') })
  if (dh.virt !== 'none' && !dh.container)
    out.push({ lv: 'mut', text: t('виртуальная машина {v}: здоровье физических дисков видит только хостер', { v: DH_VIRT[dh.virt] ?? dh.virt }) })
  const io = dh.io?.count ?? 0
  out.push(io ? { lv: io >= 10 ? 'bad' : 'warn', text: t('ошибки ввода-вывода за 10 мин: {n}', { n: io }) } : { lv: 'ok', text: t('ошибок ввода-вывода нет') })
  return out
}

// Массив строкой: разделы подкрашены по своему диску - видно, что задевает сбойный диск
function DhArray({ a, byDev }: { a: DiskHealthRaid; byDev: Map<string, DiskHealthDisk> }) {
  const { t } = useI18n()
  const down = dhRaidDown(a)
  const lv: DhLv = down ? 'bad' : a.failed ? 'warn' : 'ok'
  const list = (s: string) => s.split(/\s+/).filter(Boolean)
  const parts = list(a.members)
  const failed = list(a.failed)
  // места в массиве, которые некому занять: диск пропал целиком
  const gap = a.total > 0 ? Math.max(0, a.total - parts.length - failed.length) : 0
  const pct = dhSyncPct(a.sync)
  return (
    <div className="dh-arr">
      <span className={`lk-dot lk-dot-${lv}`} />
      <span className="dh-arrname mono">{a.dev}</span>
      {a.level && <span className="dh-chip dh-chip-mut">{a.level}</span>}
      <div className="dh-parts">
        {parts.map((p) => {
          const d = byDev.get(dhDiskOf(p))
          const pl: DhLv = d && d.err !== 'virtual' ? DH_LV[dhTone(d)] : 'ok'
          return (
            <span key={p} className={`dh-part${pl !== 'ok' ? ` dh-part-${pl}` : ''}`}>
              <span className={`lk-dot lk-dot-${pl}`} />
              <span className="mono">{p}</span>
            </span>
          )
        })}
        {failed.map((p) => (
          <span key={'f-' + p} className="dh-part dh-part-bad">
            <span className="lk-dot lk-dot-bad" />
            <span className="mono">{p}</span> {t('сбой')}
          </span>
        ))}
        {list(a.spare).map((p) => (
          <span key={'s-' + p} className="dh-part dh-part-spare">
            <span className="mono">{p}</span> {t('запасной')}
          </span>
        ))}
        {Array.from({ length: gap }, (_, i) => (
          <span key={'g' + i} className="dh-part dh-part-gap">{t('нет диска')}</span>
        ))}
      </div>
      <div className={`dh-arrstate ${down ? 't-down' : 'muted'}`}>
        {pct != null && (
          <span className="dh-sync">
            <span className="dh-syncbar"><span style={{ width: `${pct}%` }} /></span>
            {dhSync(a.sync, t)}
          </span>
        )}
        <span>
          {a.state === 'inactive'
            ? t('не собран')
            : down
              ? t('работает {a} из {n} дисков', { a: a.active, n: a.total })
              : t('в порядке')}
        </span>
      </div>
    </div>
  )
}

// Плитка диска. Физический: SMART, износ с отметкой 90% и прогнозом, температура, наработка,
// серийник (по нему диск меняют в ЦОД). Диск ВМ: здоровья у него нет, только загрузка и ответ
function DhDiskTile({ d, fc, dev }: { d: DiskHealthDisk; fc?: DiskWear; dev?: { util: number; await: number } }) {
  const { t } = useI18n()
  const virtual = d.err === 'virtual'
  const lv: DhLv = virtual ? 'ok' : DH_LV[dhTone(d)]
  const kind = virtual ? '' : d.type === 'nvme' ? 'NVMe' : d.rota === 1 ? 'HDD' : d.rota === 0 ? 'SSD' : ''
  const head = [d.model, kind, d.size ? fmtBytes(d.size) : ''].filter(Boolean).join(' · ')
  const wl = dhWearLv(d, fc)
  const counters = [
    d.realloc ? t('переназначено {n}', { n: d.realloc }) : '',
    d.pending ? t('нечитаемых {n}', { n: d.pending }) : '',
    d.uncorr ? t('неисправимых {n}', { n: d.uncorr }) : '',
    d.media ? t('ошибок носителя {n}', { n: d.media }) : '',
    d.crc ? t('ошибок CRC {n}', { n: d.crc }) : '',
    dhCrit(d) ? t('предупреждение NVMe {c}', { c: d.crit }) : '',
  ].filter(Boolean)
  const grow = DH_GROW.filter(([k]) => (d.grow?.[k] ?? 0) > 0).map(([k, label]) => `${t(label)} +${d.grow[k]}`)
  const stats: [string, string, boolean][] = (
    virtual
      ? [[t('загрузка'), dev ? `${Math.round(dev.util)}%` : '', false], [t('ответ'), dev ? fmtMs(dev.await) : '', false]]
      : [[t('температура'), d.temp != null ? fmtTempC(d.temp) : '', false], [t('наработка'), d.poh != null ? dhPoh(d.poh, t) : '', false], [t('серийник'), d.serial, true]]
  ).filter(([, v]) => v) as [string, string, boolean][]
  const fcText =
    d.wear == null
      ? ''
      : d.wear >= 100
        ? t('ресурс записи исчерпан')
        : [
            d.wear >= 90 ? t('пора менять') : fc?.d90 ? t('90% примерно к {d}', { d: isoDay(fc.d90) }) : '',
            fc && fc.rate
              ? `${t('{r}% в сутки', { r: fc.rate.toFixed(fc.rate < 0.1 ? 3 : 2) })} ${fc.src === 'lifetime' ? t('по средней за жизнь') : t('по замеру')}`
              : '',
          ].filter(Boolean).join(' · ')
  return (
    <div className={`dh-tile${lv !== 'ok' ? ` dh-tile-${lv}` : ''}`}>
      <div className="dh-thead">
        <DhIcon d={d} lv={virtual ? 'muted' : lv} />
        <div className="dh-tid">
          <div className="dh-tname mono">{d.dev}</div>
          {head && <div className="dh-tmodel muted" title={head}>{head}</div>}
        </div>
        <span className={`dh-chip dh-chip-${dhChip(d)}`}>{virtual ? t('виртуальный') : dhDiskState(d, t)}</span>
      </div>
      {!virtual &&
        (!d.ok ? (
          <div className="dh-fc t-down">{t('SMART не читается: проверить диск, кабель или контроллер')}</div>
        ) : d.err === 'nosmart' ? (
          <div className="dh-fc muted">{t('SMART не читается: диск за аппаратным RAID или неизвестным контроллером')}</div>
        ) : d.wear != null ? (
          <>
            <div className="dh-wear muted">
              <span>{t('износ')}</span>
              <div className="dh-wbar">
                <span className={wl} style={{ width: `${Math.min(d.wear, 100)}%` }} />
                <i />
              </div>
              <b className={wl !== 'ok' ? `net-tone-${wl}` : undefined}>{d.wear}%</b>
            </div>
            {fcText && (
              <div className={`dh-fc ${d.wear >= 90 || fc?.soon ? 't-degraded' : 'muted'}`} title={fc ? wearForecast(fc, t) : undefined}>
                {fcText}
              </div>
            )}
          </>
        ) : (
          <div className="dh-fc muted">{d.rota === 1 ? t('HDD износ не сообщает, следим за секторами') : t('износ диск не сообщает')}</div>
        ))}
      {stats.length > 0 && (
        <div className="dh-stats">
          {stats.map(([k, v, mono]) => (
            <div key={k} className={mono ? 'dh-wide' : undefined}>
              <span>{k}</span>
              <b className={mono ? 'mono' : undefined} title={mono ? v : undefined}>{v}</b>
            </div>
          ))}
        </div>
      )}
      {counters.length > 0 && <div className="dh-cnt muted">{counters.join(' · ')}</div>}
      {grow.length > 0 && <div className="dh-cnt t-degraded">{t('за сутки: {v}', { v: grow.join(', ') })}</div>}
    </div>
  )
}

// Здоровье дисков: сводка, массивы, плитки. На дедике - физические диски со SMART, на ВМ -
// виртуальные (helper 0.2, со старым - имена из disk_devs агента): их здоровье видит хостер
function DiskHealthBlock({ r, wear }: { r: ServerReport; wear?: DiskWear[] }) {
  const { t } = useI18n()
  const dh = diskHealth(r)
  if (!dh) return null
  const bare = dh.virt === 'none' && !dh.container
  const vm = dh.virt !== 'none' && !dh.container
  const raid = dh.raid ?? []
  const missing = dh.missing ?? []
  const io = dh.io ?? { count: 0, last: [] }
  const devs = r.disk_devs ?? []
  let disks = dh.disks ?? []
  if (vm && !disks.length) disks = devs.map((x) => dhVirtualDisk(x.dev))
  // в контейнере сказать нечего: массивы и журнал ядра чужие, диски тоже
  if (!bare && !raid.length && !io.count && !disks.length) return null
  const byDev = new Map(disks.map((d) => [d.dev, d]))
  const pills = dhSummary(dh, disks, wear, t)
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-diskhealth">
      <div className="net-head">
        <div className="chart-cap">{t('Здоровье дисков')}</div>
      </div>
      <div className="dh-sum">
        {pills.map((p, i) => (
          <span key={i} className={`dh-pill${p.lv === 'warn' || p.lv === 'bad' ? ` dh-pill-${p.lv}` : ''}`}>
            <span className={p.lv === 'mut' ? 'lk-dot' : `lk-dot lk-dot-${p.lv}`} />
            {p.text}
          </span>
        ))}
      </div>
      {bare && !dh.smart && (
        <div className="muted small dh-note">
          {t('smartmontools не установлен, SMART не читается: его ставит helper diskhealth-setup.')}
        </div>
      )}
      {raid.length > 0 && (
        <div className="dh-arrays">
          {raid.map((a) => (
            <DhArray key={a.dev} a={a} byDev={byDev} />
          ))}
        </div>
      )}
      {(disks.length > 0 || missing.length > 0) && (
        <div className="dh-tiles">
          {disks.map((d) => (
            <DhDiskTile
              key={d.dev + d.serial}
              d={d}
              fc={(wear ?? []).find((w) => w.serial === d.serial)}
              dev={devs.find((x) => x.dev === d.dev)}
            />
          ))}
          {missing.map((m) => {
            const ago = Math.max(0, dh.ts - m.last)
            return (
              <div key={'gone-' + m.serial} className="dh-tile dh-tile-bad dh-tile-gone">
                <div className="dh-thead">
                  <DhIcon lv="bad" />
                  <div className="dh-tid">
                    <div className="dh-tname mono">{m.dev}</div>
                    {m.model && <div className="dh-tmodel muted">{m.model}</div>}
                  </div>
                  <span className="dh-chip dh-chip-bad">{t('пропал')}</span>
                </div>
                <div className="dh-fc t-down">
                  {ago >= 3600
                    ? t('последний раз виден {n} ч назад', { n: Math.floor(ago / 3600) })
                    : t('последний раз виден {n} мин назад', { n: Math.max(1, Math.floor(ago / 60)) })}
                  {m.serial ? ` · ${m.serial}` : ''}
                </div>
              </div>
            )
          })}
        </div>
      )}
      {bare && !disks.length && !raid.length && !missing.length && <div className="muted small">—</div>}
      {io.count > 0 && (
        <div className="dh-io">
          <div className={io.count >= 10 ? 'net-prob' : 'muted small'}>
            {t('Ошибки ввода-вывода в журнале ядра за 10 минут: {n}', { n: io.count })}
          </div>
          {io.last.length > 0 && <pre className="dh-io-lines mono">{io.last.join('\n')}</pre>}
        </div>
      )}
    </div>
  )
}

// Диски: место и inode по разделам, прогноз заполнения. Плитки сверху - самый заполненный
// раздел, ближайшее заполнение и самый загруженный диск; разделы строками с полоской
function DiskSpaceCard({ server: s, r, onChanged }: { server: Server; r: ServerReport; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const disks = r.disks ?? []
  if (!disks.length) return null
  const fc = diskForecast(s, r)
  const analyzed = new Set((diskUsage(r)?.fs ?? []).map((f) => f.mount))
  const rows = disks.map((d) => {
    const p = pct(d.used, d.total) ?? 0
    // inode с агента 2.15; точка красится по худшему из места и inode
    const ip = d.inodes ? Math.round(((d.inodes_used ?? 0) / d.inodes) * 100) : null
    return { d, p, ip, fc: fc.filter((i) => i.mount === d.mount) }
  })
  const level = (p: number) => (p >= 90 ? 'bad' : p >= 75 ? 'warn' : 'ok')
  const top = rows.reduce((a, b) => (b.p > a.p ? b : a))
  const soon = fc.length ? fc.reduce((a, b) => (b.eta_h < a.eta_h ? b : a)) : null
  const devs = r.disk_devs ?? []
  const busy = devs.length ? devs.reduce((a, b) => (b.util > a.util ? b : a)) : null
  // место под "Разобрать" держим во всех строках, чтобы полоски кончались ровно
  const actCol = !!s.disk_analyze && !isViewer && rows.some(({ d }) => !analyzed.has(d.mount))
  const grow = (i: DiskForecastItem) => t('растет на ~{r}% в сутки', { r: i.rate < 10 ? +i.rate.toFixed(1) : Math.round(i.rate) })
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-diskfill">
      <div className="net-head">
        <div className="chart-cap">{t('Диски')}</div>
      </div>
      <div className="net-tiles">
        <NetTile
          label={t('Заполнено больше всего')}
          value={`${top.p}%`}
          sub={`${top.d.mount} · ${t('свободно {n}', { n: fmtBytes(Math.max(0, top.d.total - top.d.used)) })}`}
          tone={level(top.p)}
        />
        <NetTile
          label={t('Прогноз заполнения')}
          value={
            !soon
              ? t('не грозит')
              : soon.eta_h < 1
                ? t('меньше часа')
                : soon.eta_h < 48
                  ? t('через {n} ч', { n: Math.round(soon.eta_h) })
                  : t('через {n} дн', { n: Math.round(soon.eta_h / 24) })
          }
          sub={soon ? `${soon.kind === 'inode' ? 'inode ' : ''}${soon.mount} · ${grow(soon)}` : t('в ближайший месяц')}
          tone={!soon ? 'ok' : soon.eta_h <= 24 ? 'bad' : soon.eta_h <= 5 * 24 ? 'warn' : undefined}
        />
        {busy && (
          <NetTile
            label={t('Самый загруженный диск')}
            value={`${busy.util}%`}
            sub={`${busy.dev} · ${t('ответ {v}', { v: fmtMs(busy.await) })}`}
            tone={busy.util >= 90 ? 'warn' : undefined}
          />
        )}
      </div>
      <div className="loc-results">
        {rows.map(({ d, p, ip, fc: items }) => {
          const lv = level(Math.max(p, ip ?? 0))
          // кнопка "Разобрать" - в конце строки; без нее (старый агент) подсказка уходит вниз
          const analyze = !analyzed.has(d.mount) && (
            <DiskAnalyze server={s} mount={d.mount} forecast={items.length > 0} onChanged={onChanged} />
          )
          return (
            <div key={d.mount} className="loc-res ds-row">
              <div className="ds-main">
                <span className={`lk-dot lk-dot-${lv}`} />
                <span className="ds-mount mono">{d.mount}</span>
                <div className="ds-bar">
                  <span className={level(p)} style={{ width: `${Math.min(p, 100)}%` }} />
                </div>
                <span className={`ds-pct${level(p) !== 'ok' ? ` net-tone-${level(p)}` : ''}`}>{p}%</span>
                <span className="ds-size muted">
                  {fmtBytes(d.used)} / {fmtBytes(d.total)}
                  {ip != null && (
                    <>
                      {' · '}
                      <span className={ip >= 75 ? toneOf(ip) : ''}>inode {ip}%</span>
                    </>
                  )}
                </span>
                {actCol && <span className="ds-act">{analyze}</span>}
              </div>
              <div className="ds-extra small">
                {items.map((i) => (
                  <div key={i.kind} className={i.eta_h <= 24 ? 't-down' : i.eta_h <= 5 * 24 ? 't-degraded' : ''}>
                    {(i.kind === 'inode' ? t('inode кончатся {when}', { when: etaText(i.eta_h, t) }) : t('заполнится {when}', { when: etaText(i.eta_h, t) })) +
                      ' ' +
                      t('(растет на ~{r}% в сутки)', { r: i.rate < 10 ? +i.rate.toFixed(1) : Math.round(i.rate) })}
                  </div>
                ))}
                {!s.disk_analyze && analyze}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Действия "Освободить": тот же каталог знают агент и helper diskusage-setup; какие из них
// разрешены на ноде, helper сообщает сам (du.fix.allow из /etc/kervax/fix.conf).
const DU_FIXABLE = new Set([
  'journal', 'rotated-logs', 'apt-cache', 'dnf-cache', 'coredumps', 'crash-reports',
  'docker-dangling', 'docker-build-cache', 'container-log',
])
const DU_FIX_AGENT = '2.13' // с этой версии агент передает команду в спул helper'а

// a >= b по числам через точку: "2.13" новее "2.9"
function verAtLeast(a: string | undefined | null, b: string): boolean {
  if (!a) return false
  const pa = a.split('.').map((x) => parseInt(x, 10) || 0)
  const pb = b.split('.').map((x) => parseInt(x, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0)
  }
  return true
}

type DuFixResult = { action: string; mode: string; bytes: number; count: number; sample: string[] }
type DuFixState =
  | { phase: 'preview' }
  | { phase: 'confirm'; data: DuFixResult }
  | { phase: 'run'; data: DuFixResult }
  | { phase: 'done'; data: DuFixResult }
  | { phase: 'error'; msg: string }

// "Разобрать" раздел, на который helper сам не смотрит (ниже 75%): прогноз обещает заполнение
// или просто интересно, что там лежит. helper держит разбор сутки, результат приезжает со
// следующим отчетом в блок "На что ушло место". Раздел с прогнозом панель разбирает и сама.
type AnalyzeState = 'idle' | 'busy' | 'sent' | string
function DiskAnalyze({ server: s, mount, forecast, onChanged }: {
  server: Server; mount: string; forecast: boolean; onChanged: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [st, setSt] = useState<AnalyzeState>('idle')
  if (isViewer) return null
  if (!s.disk_analyze) {
    return forecast ? (
      <div className="muted small">
        {t('Разбор места сам включается от 75%. Кнопка "Разобрать" появится с агентом 2.24 и helper diskusage-setup 0.6.')}
      </div>
    ) : null
  }
  const go = async () => {
    setSt('busy')
    try {
      const c = await backupCommand(s.id, { action: 'disk_fix', mode: 'run', fix: 'analyze', mount })
      let last = c
      for (let i = 0; i < 90 && last.status !== 'done' && last.status !== 'error'; i++) {
        await new Promise((res) => setTimeout(res, 1000))
        last = await backupCommandStatus(s.id, c.id)
      }
      if (last.status === 'done' && last.ok) {
        setSt('sent')
        // du по большому разделу идет минуты, а отчет агент шлет раз в несколько десятков секунд
        window.setTimeout(onChanged, 45_000)
        window.setTimeout(onChanged, 150_000)
      } else {
        setSt(last.result || (last.status === 'error' ? t('не удалось') : t('нода не ответила вовремя')))
      }
    } catch (e) {
      setSt(e instanceof Error ? e.message : t('не удалось'))
    }
  }
  if (st === 'sent') {
    return <div className="muted small">{t('Разбор запущен, появится ниже через минуту-две.')}</div>
  }
  return (
    <div className="du-analyze small">
      <button className="du-fix-btn" disabled={st === 'busy'} onClick={go}>
        {st === 'busy' ? t('Запрашиваю...') : t('Разобрать')}
      </button>
      {st !== 'idle' && st !== 'busy' && <span className="t-down"> {st}</span>}
    </div>
  )
}

// "На что ушло место" (helper diskusage-setup): дерево самых больших каталогов раздела и
// находки с готовыми командами. Безопасные находки и лог контейнера освобождаются кнопкой:
// сначала нода сама считает, что удалится (preview), человек подтверждает, потом run.
function DiskUsageBlock({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  const [copied, setCopied] = useState<string | null>(null)
  const [fix, setFix] = useState<Record<string, DuFixState>>({})
  const r = s.last_report ?? {}
  const du = diskUsage(r)
  const done = Object.values(fix).flatMap((x) => (x.phase === 'done' ? [x.data.bytes] : []))
  if (!du) {
    // Освободили - раздел ушел ниже 75%, helper убрал разбор. Без итога блок просто
    // исчезал вместе с "Освобождено", и казалось, что все сломалось.
    if (!done.length) return null
    return (
      <div className="chart-card net-card net-card-wide du-block" id="mcard-du">
        <div className="net-head">
          <div className="chart-cap">{t('На что ушло место')}</div>
        </div>
        <div className="du-fix-line small">
          <span className="t-up">{t('Освобождено {n}', { n: fmtBytes(done.reduce((a, b) => a + b, 0)) })}</span>
          <span className="muted">
            {t('Раздел больше не заполнен ({d}), разбор места скрыт: он появляется от 75%.', {
              d: (r.disks ?? []).map((d) => `${d.mount} ${pct(d.used, d.total)}%`).join(', '),
            })}
          </span>
        </div>
      </div>
    )
  }
  const age = Math.max(0, Math.round(((r.clock_unix || Date.now() / 1000) - du.ts) / 60))
  const autoThr = s.disk_warn_percent || s.disk_alert_percent || s.disk_crit_percent || 85
  const copy = (txt: string, id: string) => {
    navigator.clipboard?.writeText(txt)
    setCopied(id)
    window.setTimeout(() => setCopied(null), 1500)
  }
  const allow = new Set(du.fix?.allow ?? [])
  const agentOk = verAtLeast(s.agent_version, DU_FIX_AGENT)
  const canFix = (it: DiskUsageItem) =>
    isAdmin && agentOk && DU_FIXABLE.has(it.id) && allow.has(it.id) && (it.id !== 'container-log' || !!it.name)
  // почему кнопок нет, хотя находки для них есть
  const fixNote = !(isAdmin && du.items.some((i) => DU_FIXABLE.has(i.id)))
    ? ''
    : !du.fix
      ? t('Кнопки "Освободить" появятся после обновления helper diskusage-setup до 0.3 (ansible kervax_helpers.yml).')
      : !agentOk
        ? t('Кнопки "Освободить" появятся после обновления агента до {v}.', { v: DU_FIX_AGENT })
        : ''
  const set = (key: string, st: DuFixState | null) =>
    setFix((cur) => {
      const next = { ...cur }
      if (st) next[key] = st
      else delete next[key]
      return next
    })
  const command = async (it: DiskUsageItem, mode: 'preview' | 'run', maxMs: number) => {
    const c = await backupCommand(s.id, {
      action: 'disk_fix',
      mode,
      fix: it.id,
      container: it.id === 'container-log' ? it.name : undefined,
    })
    let last = c
    const t0 = Date.now()
    while (last.status !== 'done' && last.status !== 'error' && Date.now() - t0 < maxMs) {
      await new Promise((res) => setTimeout(res, 1000))
      last = await backupCommandStatus(s.id, c.id)
    }
    if (last.status === 'done' && last.ok) {
      try {
        return JSON.parse(last.result) as DuFixResult
      } catch {
        throw new Error(last.result || t('не удалось'))
      }
    }
    throw new Error(last.result || (last.status === 'error' ? t('не удалось') : t('нода не ответила вовремя')))
  }
  // самый частый отказ helper'а переводим: остальное он пишет коротко и по-английски
  const fixError = (e: unknown) => {
    const m = e instanceof Error ? e.message : String(e)
    return m.includes('is not allowed on this node')
      ? t('На этой ноде действие выключено: его нет в /etc/kervax/fix.conf.')
      : m
  }
  const preview = async (key: string, it: DiskUsageItem) => {
    set(key, { phase: 'preview' })
    try {
      set(key, { phase: 'confirm', data: await command(it, 'preview', 150_000) })
    } catch (e) {
      set(key, { phase: 'error', msg: fixError(e) })
    }
  }
  const run = async (key: string, it: DiskUsageItem, data: DuFixResult) => {
    set(key, { phase: 'run', data })
    try {
      set(key, { phase: 'done', data: await command(it, 'run', 16 * 60_000) })
      // helper сразу пересчитывает место, новые цифры приедут со следующим отчетом агента
      window.setTimeout(onChanged, 20_000)
    } catch (e) {
      set(key, { phase: 'error', msg: fixError(e) })
    }
  }
  return (
    <div className="chart-card net-card net-card-wide du-block" id="mcard-du">
      <div className="net-head">
        <div className="chart-cap">{t('На что ушло место')}</div>
        <span className="muted small">{t('замер {n} мин назад', { n: age })}</span>
      </div>
      {fixNote && <div className="muted small du-note">{fixNote}</div>}
      {s.disk_autofix ? (
        <div className="muted small du-note">
          🧹 {t('Автоочистка включена: от {p}% панель сама освобождает безопасное.', { p: autoThr })}
        </div>
      ) : (
        isAdmin && (
          <div className="muted small du-note">{t('Автоочистка выключена, включается в настройках сервера.')}</div>
        )
      )}
      {du.fs.map((fs) => {
        const items = du.items
          .filter((i) => i.mount === fs.mount)
          .sort((a, b) => (b.free || b.bytes) - (a.free || a.bytes))
        const safe = items.filter((i) => i.level === 'safe').reduce((sum, i) => sum + i.free, 0)
        return (
          <div key={fs.mount} className="du-fs">
            <div className="du-fs-head">
              <span className="mono">{fs.mount}</span>
              <span className="du-fs-pct">{fs.pct}%</span>
              <span className="muted small">
                {t('занято {u} из {s}, свободно {a}', {
                  u: fmtBytes(fs.used),
                  s: fmtBytes(fs.size),
                  a: fmtBytes(fs.avail),
                })}
              </span>
            </div>
            <DuTree fs={fs} t={t} />
            {(fs.ipct ?? 0) >= 75 && (
              <div className="du-items">
                <div className="du-sub">
                  {t('Где больше всего файлов')}
                  <span className="muted small">
                    {' '}
                    ·{' '}
                    {t('inode заняты на {p}%: {u} из {n}', {
                      p: fs.ipct ?? 0,
                      u: fmtFiles(fs.iused ?? 0, t),
                      n: fmtFiles(fs.itotal ?? 0, t),
                    })}
                  </span>
                </div>
                <DuTree fs={fs} t={t} files />
              </div>
            )}
            {items.length > 0 && (
              <div className="du-items">
                <div className="du-sub">
                  {t('Что можно освободить')}
                  {safe > 0 && <span className="muted small"> · {t('без риска ~{n}', { n: fmtBytes(safe) })}</span>}
                </div>
                {items.map((it) => {
                  const key = `${fs.mount}|${it.id}|${it.path}`
                  const cmd = it.fix ? `sudo ${it.fix}` : ''
                  const hint = duHint(it, t)
                  const st = fix[key]
                  return (
                    <div key={key} className="du-item">
                      <div className="du-item-head">
                        <span className={`du-lvl du-lvl-${it.level}`}>{duLevel(it.level, t)}</span>
                        <span className="du-item-name">{duLabel(it, t)}</span>
                        <span className="du-item-size mono">{fmtBytes(it.free || it.bytes)}</span>
                        {canFix(it) && !st && (
                          <button className="du-fix-btn" onClick={() => preview(key, it)}>
                            {it.id === 'container-log' ? t('Обнулить') : t('Освободить')}
                          </button>
                        )}
                      </div>
                      {hint && <div className="muted small du-item-hint">{hint}</div>}
                      {st?.phase === 'preview' && (
                        <div className="du-fix-line muted small">{t('Нода считает, что удалится...')}</div>
                      )}
                      {st?.phase === 'confirm' && (
                        <div className="du-fix">
                          <div>
                            {st.data.bytes > 0
                              ? t('Освободится ~{n}', { n: fmtBytes(st.data.bytes) })
                              : t('Освобождать нечего.')}
                            {st.data.count > 0 && (
                              <span className="muted"> · {t('файлов: {n}', { n: st.data.count })}</span>
                            )}
                          </div>
                          {st.data.sample.length > 0 && (
                            <pre className="du-fix-sample mono">
                              {st.data.sample.join('\n') + (st.data.count > st.data.sample.length ? '\n...' : '')}
                            </pre>
                          )}
                          <div className="du-fix-actions">
                            {st.data.bytes > 0 && (
                              <button onClick={() => run(key, it, st.data)}>{t('Выполнить')}</button>
                            )}
                            <button className="ghost" onClick={() => set(key, null)}>{t('Отмена')}</button>
                          </div>
                        </div>
                      )}
                      {st?.phase === 'run' && (
                        <div className="du-fix-line muted small">
                          {it.id.startsWith('docker-')
                            ? t('Выполняю, docker может чистить несколько минут...')
                            : t('Выполняю...')}
                        </div>
                      )}
                      {st?.phase === 'done' && (
                        <div className="du-fix-line small">
                          <span className="t-up">{t('Освобождено {n}', { n: fmtBytes(st.data.bytes) })}</span>
                          <span className="muted">{t('цифры обновятся со следующим отчетом')}</span>
                          <button className="ghost small" onClick={() => set(key, null)}>{t('закрыть')}</button>
                        </div>
                      )}
                      {st?.phase === 'error' && (
                        <div className="du-fix-line small">
                          <span className="form-error">{st.msg}</span>
                          <button className="ghost small" onClick={() => set(key, null)}>{t('закрыть')}</button>
                        </div>
                      )}
                      {cmd && (!st || st.phase === 'error') && (
                        <div className="agent-advice-cmd">
                          <pre>{cmd}</pre>
                          <button className="ghost" onClick={() => copy(cmd, key)}>
                            {copied === key ? t('Скопировано') : t('Копировать')}
                          </button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Базы перезапускать кнопкой не даем: потеря соединений и долгий старт - решение человека
const DB_IMAGE = /(postgres|mysql|mariadb|mongo|redis|valkey|keydb|clickhouse|elasticsearch|opensearch|rabbitmq|kafka|zookeeper|etcd|neo4j|influx|timescale|cassandra|memcached|minio|percona)/i

// Кто съел память: контейнеры по памяти (агент 2.14+) с перезапуском прямо отсюда - сюда
// ведет ссылка из алерта по памяти. Перезапуск - та же команда, что в разделе Docker.
// by - чем меряем: память (раздел "Память") или CPU (раздел "CPU", агент 2.21+). Перезапуск
// один и тот же: тот, кто съел ресурс, обычно и чинится перезапуском.
// Что течет памятью: дно (минимум за сутки) растет 4 суток подряд. Ряд минимумов показан целиком:
// по нему видно, что это ровный рост, а не разовый всплеск
function MemLeaks({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const items = s.mem_leaks ?? []
  if (!items.length) return null
  const kindName = { docker: t('контейнер'), pod: t('под'), proc: t('процесс') }
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-memleak">
      <div className="net-head">
        <div className="chart-cap">{t('Растет память')}</div>
      </div>
      <div className="net-sub">
        {t('Минимум памяти за сутки растет 4 суток подряд. Пила сборщика мусора и всплески минимум не двигают, а утечка поднимает его каждый день.')}
      </div>
      <div className="loc-results">
        {items.map((x) => {
          const [comm, user] = x.name.split('@')
          const bad = x.level >= 2
          return (
            <div key={x.key} className="loc-res ml-row">
              <span className={`lk-dot lk-dot-${bad ? 'bad' : 'warn'}`} />
              <div className="ml-main">
                <span className="acc-tag">{kindName[x.kind]}</span> <b>{x.kind === 'proc' ? `${comm} (${user})` : x.name}</b>
                <div className="muted small">{x.series.map((v) => fmtBytes(v)).join(', ')}</div>
              </div>
              <div className="ml-rate">
                <span className={bad ? 't-down' : 't-degraded'}>+{fmtBytes(x.rate)} {t('в сутки')}</span>
                {x.eta_days != null && x.eta_days < 30 && (
                  <div className="muted small">{t('памяти хватит на {d} сут', { d: x.eta_days })}</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Веб сейчас: запросы и 5xx за последнюю минуту по всем логам, самый нагруженный лог. Доля 5xx
// красная от порога алерта сервера (порог алерта - за 15 минут, тут минута: для глаза)
function WebNow({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const r = s.last_report
  const wr = webRate(r)
  if (!wr) return null
  const e5 = wr.e5 ?? 0
  const share = wr.rpm > 0 ? (e5 / wr.rpm) * 100 : 0
  const thr = s.web_5xx_alert_percent
  const logs = wr.logs ?? []
  const busy = logs.length ? logs.reduce((a, b) => (b.rpm > a.rpm ? b : a)) : null
  const un = webUnparsed(r)
  return (
    <div className="chart-card net-card net-card-wide now-card">
      <div className="net-tiles">
        <NetTile label={t('Запросов в минуту')} value={fmtCount(wr.rpm)} />
        <NetTile
          label={t('5xx в минуту')}
          value={fmtCount(e5)}
          sub={wr.rpm > 0 ? t('{p}% запросов', { p: share < 1 ? share.toFixed(2) : share.toFixed(1) }) : undefined}
          tone={thr > 0 && share >= thr ? 'bad' : e5 > 0 ? 'warn' : 'ok'}
        />
        {busy && busy.rpm > 0 && (
          <NetTile
            label={t('Самый нагруженный лог')}
            value={fmtCount(busy.rpm)}
            unit={t('в мин')}
            sub={webLogLabel(busy)}
            title={busy.log}
          />
        )}
        <NetTile
          label={t('Логов под наблюдением')}
          value={String(logs.length)}
          sub={t('с запросами за минуту: {n}', { n: logs.filter((l) => l.rpm > 0).length })}
        />
        {un > 0 && (
          <NetTile
            label={t('Без кода ответа')}
            value={fmtCount(un)}
            sub={t('формат лога не разобран')}
            tone="warn"
          />
        )}
      </div>
    </div>
  )
}

// Плитки сводки: CPU, память, худший раздел диска и сеть, с линией за выбранный период.
// Нажатие ведет в раздел. Замечание раздела (поломка диска, утечка памяти) - строкой в плитке
// и ее цветом
function SrvTiles({ r, M, info, onGo, gpu }: {
  r: ServerReport
  gpu?: GpuSum | null
  M: ServerMetric[]
  info: Record<string, SecInfo>
  onGo: (id: string) => void
}) {
  const { t } = useI18n()
  const tone = (p: number | null, sec: string): Tone | undefined => {
    const x = info[sec]?.level ?? 0
    if (x >= 2 || (p != null && p >= 90)) return 'bad'
    if (x >= 1 || (p != null && p >= 75)) return 'warn'
    return p == null ? undefined : 'ok'
  }
  const cpu = r.cpu_percent != null ? Math.round(r.cpu_percent) : null
  const mem = pct(r.mem_used, r.mem_total)
  // диск - самый заполненный раздел (при нескольких показываем худший и их число)
  const ds = (r.disks ?? []).filter((d) => d.total)
  const worst = ds.length ? ds.reduce((a, b) => (b.used / b.total > a.used / a.total ? b : a)) : null
  const dp = worst ? Math.round((worst.used / worst.total) * 100) : null
  const tile = (sec: string, label: string, value: string, sub: string, p: number | null, spark: ReactNode) => {
    const x = info[sec]
    return (
      <button key={sec} className="srv-tile-btn" onClick={() => onGo(sec)} title={t('к разделу')}>
        <NetTile label={x?.fresh ? <>{label} <FreshChip since={x.fresh} /></> : label} value={value} sub={sub} tone={tone(p, sec)}>
          {x && x.n > 0 && (
            <div className={`srv-tile-issue ${x.level >= 2 ? 't-down' : 't-degraded'}`} title={x.line}>{x.line}</div>
          )}
          {spark}
        </NetTile>
      </button>
    )
  }
  return (
    <div className="net-tiles srv-tiles">
      {tile('cpu', 'CPU', cpu != null ? `${cpu}%` : '—', `load ${r.load?.[0] ?? '—'}`, cpu,
        <TileSpark vals={M.map((m) => m.cpu_percent)} max={100} color="#5a8fc7" />)}
      {tile('mem', t('Память'), mem != null ? `${mem}%` : '—', `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}`, mem,
        <TileSpark vals={M.map((m) => m.mem_percent)} max={100} color="#4fa79a" />)}
      {worst &&
        tile('disk', t('Диск'), `${dp}%`, `${worst.mount}${ds.length > 1 ? ` · ${ds.length} ${t('разд.')}` : ''}`, dp,
          <TileSpark vals={M.map((m) => m.disk_percent)} max={100} color="#cf9b52" />)}
      {tile('net', t('Сеть'), `↓ ${fmtRate(r.net_rx)}`,
        `↑ ${fmtRate(r.net_tx)}${(r.swap_total ?? 0) > 0 ? ` · swap ${fmtBytes(r.swap_used)}` : ''}`, null,
        <TileSpark vals={M.map((m) => m.net_rx)} color="#57a894" />)}
      {gpu &&
        tile('gpu', 'GPU', gpu.util != null ? `${gpu.util}%` : '—',
          gpu.mem != null ? `${gpu.n} x ${gpu.models.join(', ')} · ${gpu.mem} / ${gpu.memt} ${t('ГБ')}` : `${gpu.n} x NVIDIA`, gpu.util,
          <TileSpark
            vals={M.map((m) => (m.gpu?.length ? m.gpu.reduce((a, x) => a + (x.u ?? 0), 0) / m.gpu.length : null))}
            max={100}
            color="#73bf69"
          />)}
    </div>
  )
}

// CPU сейчас: плитки над графиками, как stat-панели Grafana. Пороги цвета - алерта сервера
// (без него 90%), желтое - на подходе
function CpuNow({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const r = s.last_report
  if (!r || r.cpu_percent == null) return null
  const cpu = r.cpu_percent
  const hi = s.cpu_alert_percent || 90
  const load = r.load ?? []
  const cores = r.cpu_cores_pct ?? []
  const top = cores.length > 1 ? cores.reduce((a, v, i) => (v > cores[a] ? i : a), 0) : -1
  return (
    <div className="chart-card net-card net-card-wide now-card">
      <div className="net-tiles">
        <NetTile
          label={t('Загрузка CPU')}
          value={`${Math.round(cpu)}%`}
          sub={s.cpu_alert_percent ? t('алерт от {p}', { p: `${s.cpu_alert_percent}%` }) : undefined}
          tone={cpu >= hi ? 'bad' : cpu >= Math.min(75, hi - 10) ? 'warn' : undefined}
        />
        {load.length > 0 && (
          <NetTile
            label={t('Load average')}
            value={load[0].toFixed(2)}
            sub={load.length >= 3 ? t('5 и 15 мин: {a} / {b}', { a: load[1].toFixed(2), b: load[2].toFixed(2) }) : undefined}
          />
        )}
        {(r.cpu_cores ?? 0) > 0 && (
          <NetTile label={t('Ядра')} value={String(r.cpu_cores)} sub={r.cpu_model || undefined} title={r.cpu_model || undefined} />
        )}
        {top >= 0 && (
          <NetTile
            label={t('Самое загруженное ядро')}
            value={`${Math.round(cores[top])}%`}
            sub={t('ядро {n}', { n: top })}
            tone={cores[top] >= 95 ? 'warn' : undefined}
          />
        )}
        {r.cpu_temp != null && (
          <NetTile
            label={t('Температура')}
            value={fmtTempC(r.cpu_temp)}
            sub={s.temp_alert_c ? t('алерт от {p}', { p: fmtTempC(s.temp_alert_c) }) : undefined}
            tone={s.temp_alert_c && r.cpu_temp >= s.temp_alert_c ? 'bad' : undefined}
          />
        )}
        {r.cpu_freq != null && !r.is_vm && <NetTile label={t('Частота')} value={fmtMHz(r.cpu_freq)} />}
        {r.cpu_throttle != null && (
          <NetTile
            label={t('Тепловой троттлинг')}
            value={String(Math.round(r.cpu_throttle))}
            sub={t('за последний отчет')}
            tone={r.cpu_throttle > 0 ? 'warn' : 'ok'}
          />
        )}
      </div>
    </div>
  )
}

// Память сейчас: занято, доступно, swap и OOM-киллы плитками над графиками
function MemNow({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const r = s.last_report
  const total = r?.mem_total ?? 0
  if (!r || total <= 0) return null
  const used = r.mem_used ?? 0
  const p = (used / total) * 100
  const hi = s.mem_alert_percent || 90
  const swapT = r.swap_total ?? 0
  const swapP = swapT > 0 ? ((r.swap_used ?? 0) / swapT) * 100 : 0
  return (
    <div className="chart-card net-card net-card-wide now-card">
      <div className="net-tiles">
        <NetTile
          label={t('Занято')}
          value={`${Math.round(p)}%`}
          sub={t('{u} из {m}', { u: fmtBytes(used), m: fmtBytes(total) })}
          tone={p >= hi ? 'bad' : p >= Math.min(80, hi - 10) ? 'warn' : undefined}
        />
        <NetTile
          label={t('Доступно')}
          value={fmtBytes(Math.max(0, total - used))}
          sub={t('из них кэш {n}', { n: fmtBytes(r.mem_cached ?? 0) })}
        />
        {swapT > 0 && (
          <NetTile
            label="Swap"
            value={`${Math.round(swapP)}%`}
            sub={t('{u} из {m}', { u: fmtBytes(r.swap_used ?? 0), m: fmtBytes(swapT) })}
            tone={swapP >= 80 ? 'warn' : undefined}
          />
        )}
        {swapT > 0 && (
          <NetTile
            label={t('Обмен со swap')}
            value={fmtRate((r.swap_in ?? 0) + (r.swap_out ?? 0))}
            sub={t('загрузка {a} · выгрузка {b}', { a: fmtRate(r.swap_in), b: fmtRate(r.swap_out) })}
          />
        )}
        {r.oom_kill != null && (
          <NetTile
            label={t('OOM-киллы')}
            value={String(Math.round(r.oom_kill))}
            sub={t('за последний отчет')}
            tone={r.oom_kill > 0 ? 'bad' : 'ok'}
          />
        )}
      </div>
    </div>
  )
}

function MemContainers({ server: s, onChanged, by = 'mem' }: { server: Server; onChanged: () => void; by?: 'mem' | 'cpu' }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [st, setSt] = useState<Record<string, { busy?: boolean; ok?: boolean; msg?: string }>>({})
  const val = (c: { mem?: number; cpu?: number }) => (by === 'cpu' ? c.cpu ?? 0 : c.mem ?? 0)
  const list = (s.last_report?.docker?.containers ?? [])
    .filter((c) => c.state === 'running' && val(c) >= (by === 'cpu' ? CPU_SHOW_MIN : 1))
    .sort((a, b) => val(b) - val(a))
    .slice(0, 6)
  if (!list.length) return null
  // процент - от всей машины: памяти или всех ядер
  const total = by === 'cpu' ? (s.last_report?.cpu_cores || 0) * 100 : s.last_report?.mem_total || 0
  const restart = async (name: string) => {
    if (!window.confirm(t('Перезапустить контейнер {n}? Несколько секунд он будет недоступен.', { n: name }))) return
    setSt((cur) => ({ ...cur, [name]: { busy: true } }))
    try {
      const c = await dockerCommand(s.id, name, 'restart')
      let last = c
      const t0 = Date.now()
      while (last.status !== 'done' && last.status !== 'error' && Date.now() - t0 < 120_000) {
        await new Promise((res) => setTimeout(res, 1000))
        last = await dockerCommandStatus(s.id, c.id)
      }
      const ok = last.status === 'done' && !!last.ok
      setSt((cur) => ({ ...cur, [name]: { ok, msg: ok ? t('перезапущен') : last.result || t('не удалось') } }))
      if (ok) window.setTimeout(onChanged, 15_000)
    } catch (e) {
      setSt((cur) => ({ ...cur, [name]: { ok: false, msg: e instanceof Error ? e.message : String(e) } }))
    }
  }
  return (
    <div className="chart-card net-card">
      <div className="net-head">
        <div className="chart-cap">{by === 'cpu' ? t('Контейнеры по CPU') : t('Контейнеры по памяти')}</div>
      </div>
      <div className="loc-results">
        {list.map((c) => {
          const db = DB_IMAGE.test(c.image) || DB_IMAGE.test(c.name)
          const cs = st[c.name]
          return (
            <div key={c.name} className="loc-res mc-row">
              <div className="loc-res-name mono">{c.name}</div>
              <div className="loc-res-metric">{by === 'cpu' ? fmtCpu(c.cpu ?? 0, t) : fmtBytes(c.mem ?? 0)}</div>
              <div className="loc-res-msg muted small">
                {total ? `${Math.round((val(c) / total) * 100)}%` : ''}
              </div>
              {/* действие - в колонке одной ширины: иначе строка с кнопкой и строка с пометкой
                  про базу сдвигали цифры в разные стороны */}
              {(!isViewer || cs?.msg) && (
                <div className="mc-act-col">
                  {cs?.msg && <span className={`small ${cs.ok ? 't-up' : 'form-error'}`}>{cs.msg}</span>}
                  {!isViewer &&
                    (db ? (
                      <span className="muted small">{t('база: перезапуск только вручную')}</span>
                    ) : (
                      <button className="ghost small row-act" disabled={!!cs?.busy} onClick={() => restart(c.name)}>
                        {cs?.busy ? t('перезапускаю...') : t('Перезапустить')}
                      </button>
                    ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Упавшие юниты systemd (helper units-setup): что упало, почему и последние строки лога.
// Кнопки (агент 2.17): перезапустить или сбросить отметку - helper на ноде делает это, только
// пока юнит упал, рабочий сервис так не тронуть. Ненужный юнит можно не отслеживать: как лог
// с 5xx, остальные юниты ноды продолжают алертить.
const UNITS_AGENT = '2.17'
type UnitFixResult = { op: string; unit: string; state: string; result: string; rc: number; log: string[] }

function unitLevel(u: FailedUnit): number {
  if (u.unit.endsWith('.service')) return u.type === 'oneshot' ? 1 : 2
  return /\.(mount|automount|swap|socket)$/.test(u.unit) ? 2 : 1
}

function unitWhy(u: FailedUnit, t: TFn): string {
  const sig: Record<number, string> = { 6: 'ABRT', 9: 'KILL', 11: 'SEGV', 15: 'TERM' }
  switch (u.result) {
    case 'exit-code':
      return u.status ? t('код {n}', { n: u.status }) : t('ошибка запуска')
    case 'signal':
      return t('убит сигналом {s}', { s: u.status ? (sig[u.status] ?? String(u.status)) : '' }).trim()
    case 'timeout':
      return t('не уложился во время')
    case 'core-dump':
      return t('упал с дампом памяти')
    case 'start-limit-hit':
      return t('слишком часто перезапускался')
    case 'oom-kill':
      return t('убит из-за нехватки памяти')
    case 'resources':
      return t('не хватило ресурсов')
    case 'watchdog':
      return t('перестал отвечать watchdog')
    default:
      return u.result || t('упал')
  }
}

// Известные причины, у которых есть понятный выход: что случилось и, где можно, команда.
// Набор - с парка 05.10.2026: mdmonitor на семи нодах, unbound-resolvconf на четырех и т.д.
type UnitHint = { text: string; cmd?: string }
function unitHint(u: FailedUnit, all: FailedUnit[], t: TFn): UnitHint | null {
  const log = u.log.join('\n')
  const n = u.unit
  // у давно упавшего mdmonitor.service лог уже ротирован, причину видно у mdmonitor-oneshot
  const mdNoMail = all.some((x) => x.unit.startsWith('mdmonitor') && x.log.join('\n').includes('No mail address'))
  if (n.startsWith('mdmonitor') && (log.includes('No mail address') || (!log && mdNoMail)))
    return {
      text: t('mdadm некуда слать письма, RAID он на самом деле не мониторит. За RAID следит Kervax ("Диск: поломка"), мониторинг mdadm можно выключить (по всему парку - плейбук units_cleanup.yml):'),
      cmd: 'sudo systemctl disable --now mdmonitor-oneshot.timer && sudo systemctl mask --now mdmonitor.service && sudo systemctl reset-failed mdmonitor.service mdmonitor-oneshot.service',
    }
  if (n === 'unbound-resolvconf.service')
    return {
      text: t('unbound пытается прописаться в systemd-resolved, которого на ноде нет. Ничего полезного юнит не делает, его можно выключить (по всему парку - плейбук units_cleanup.yml):'),
      cmd: 'sudo systemctl mask --now unbound-resolvconf.service && sudo systemctl reset-failed unbound-resolvconf.service',
    }
  if (n.startsWith('systemd-networkd-wait-online'))
    return {
      text: t('При загрузке не дождались сети: обычно из-за интерфейса, который не нужен или не управляется networkd. Сервер при этом работает, достаточно сбросить отметку. Если повторяется на каждой загрузке - ограничьте ожидание нужным интерфейсом (--interface=) или выключите юнит.'),
    }
  if (n.endsWith('.socket') && u.result === 'resources')
    return {
      text: t('Сокет не смог занять свой порт: обычно его уже держит сам сервис (sshd со своим портом). Сервис при этом работает, лишний сокет можно выключить:'),
      cmd: `sudo systemctl disable --now ${n} && sudo systemctl reset-failed ${n}`,
    }
  if (log.includes('at least one source file could not be read'))
    return {
      text: t('restic закончил бэкап, но часть файлов прочитать не смог (код 3): снапшот есть, но неполный. Какие файлы - в журнале юнита:'),
      cmd: `sudo journalctl -u ${n} --since "2 days ago" --no-pager | grep -i -E "error|denied|no such"`,
    }
  if (n.startsWith('clamav-freshclam'))
    return {
      text: t('freshclam не может скачать базы антивируса, и они устаревают. Частая причина - зеркало ClamAV закрыто для адресов из России: нужно свое зеркало (PrivateMirror в freshclam.conf) или прокси.'),
    }
  if (n === 'openipmi.service')
    return {
      text: t('openipmi не нашел IPMI на этой машине. Если BMC нет или он не нужен, юнит можно выключить:'),
      cmd: 'sudo systemctl disable --now openipmi.service && sudo systemctl reset-failed openipmi.service',
    }
  if (n === 'console-setup.service')
    return {
      text: t('Неправильная раскладка консоли, на сервере ни на что не влияет. Поправить - sudo dpkg-reconfigure keyboard-configuration, или просто сбросить отметку.'),
    }
  if (n === 'certbot.service' && log.includes('manual'))
    return {
      text: t('Сертификат выпущен вручную (manual plugin), сам он не продлится. Продлите вручную или удалите ненужный:'),
      cmd: 'sudo certbot certificates && sudo certbot delete --cert-name <cert-name>',
    }
  return null
}

function unitStateText(d: UnitFixResult, t: TFn): string {
  if (d.op === 'reset') return t('отметка сброшена')
  if (d.state === 'active') return t('работает')
  if (d.state === 'activating') return t('запускается')
  if (d.state === 'inactive') return t('отработал без ошибок')
  return t('снова упал')
}

// Сервисы, которые systemd перезапускает по кругу (helper units-setup 0.2): failed они не становятся,
// поэтому среди упавших их нет. Приросты счетчика за окно считает бэкенд, строки приходят в problems.
// Давление на ресурсы (агент 2.26). PSI - доля времени, когда процессы ждали ресурс: some - хоть
// один, full - все разом. Пороги те же, что у алерта "Сервер задыхается" (collector).
const PSI_ROWS: { k: string; label: string; some: number; full: number }[] = [
  { k: 'cpu', label: 'CPU', some: 0, full: 0 },
  { k: 'mem', label: 'Память', some: 40, full: 10 },
  { k: 'io', label: 'Диск', some: 80, full: 50 },
]

// Обновления ОС (агент 2.26): что стоит, что ждет установки, когда перезагружаться. Сверху -
// что сделать (os_advice считает бэкенд: конец поддержки, перезагрузка, безопасность, apt).
function OsUpdates({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const u = s.last_report?.os_upd
  const adv = s.os_advice ?? []
  if (!u && !adv.length) return null
  const day = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleDateString() : '')
  const unatt = u?.unatt === 'on' ? t('включены') : u?.unatt === 'off' ? t('выключены') : u?.unatt === 'none' ? t('не установлены') : ''
  return (
    <div className="net-cards osupd-cards">
      <div className="chart-card net-card net-card-wide" id="mcard-osupd">
        <div className="net-head">
          <div className="chart-cap">{t('Обновления ОС')}</div>
        </div>
        {adv.map((a) => (
          <div key={a.kind} className={a.level >= 2 ? 'net-prob' : 'net-note'}>{a.text}</div>
        ))}
        {u && (
          <div className="net-tiles net-tiles-text">
            {u.updates >= 0 && (
              <NetTile
                label={t('Ждут установки')}
                value={String(u.updates)}
                sub={t('из них безопасности {n}', { n: u.security })}
                tone={u.security > 0 ? 'warn' : u.updates > 0 ? undefined : 'ok'}
              />
            )}
            {u.kernel && (
              <NetTile
                label={t('Ядро')}
                value={u.kernel}
                sub={u.kernel_new ? t('после перезагрузки {k}', { k: u.kernel_new }) : undefined}
                title={u.kernel}
              />
            )}
            <NetTile
              label={t('Перезагрузка')}
              value={u.reboot ? t('нужна') : t('не нужна')}
              sub={u.reboot && u.reboot_ts ? t('с {d}', { d: day(u.reboot_ts) }) : undefined}
              title={(u.reboot_pkgs ?? []).join(', ') || undefined}
              tone={u.reboot ? 'warn' : 'ok'}
            />
            {unatt && (
              <NetTile
                label={t('Автообновления')}
                value={unatt}
                sub={u.unatt_ts ? t('последний прогон {d}', { d: day(u.unatt_ts) }) : undefined}
                tone={u.unatt === 'on' ? 'ok' : 'warn'}
              />
            )}
            {u.apt_ts ? <NetTile label="apt update" value={day(u.apt_ts)} /> : null}
          </div>
        )}
      </div>
    </div>
  )
}

// Изменения на сервере (helper changes-setup): кто может войти и что запускается по расписанию.
// Изменения доступа подсвечены, пока их не отметили как свои.
const CHANGE_ICON: Record<string, string> = { key: '🔑', user: '👤', sudo: '🛡', cron: '⏰', unit: '⚙️', login: '🚪' }

function ChangesFeed({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState(false)
  const [all, setAll] = useState(false)
  const ch = s.changes
  if (!ch) return null
  const ack = ch.ack_ts ?? ''
  const fresh = ch.log.filter((e) => e.alert && e.ts > ack && Date.now() - Date.parse(e.ts) < 24 * 3600_000)
  const shown = all ? ch.log : ch.log.slice(0, 12)
  const doAck = async () => {
    setBusy(true)
    try {
      await ackChanges(s.id)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setBusy(false)
    }
  }
  const when = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-changes">
      <div className="net-head">
        <div className="chart-cap">{t('Изменения на сервере')}</div>
        {fresh.length > 0 && !isViewer && (
          <button className="ghost small row-act" disabled={busy} onClick={doAck}>
            {t('это мы, все в порядке')}
          </button>
        )}
      </div>
      <div className="net-sub">
        {t('SSH-ключей: {k}, пользователей с shell: {u}. Снимок раз в 10 минут, без содержимого ключей и кронов.', {
          k: ch.keys,
          u: ch.users,
        })}
      </div>
      {fresh.length > 0 && <div className="net-prob">{t('Изменился доступ: проверьте, что это свои.')}</div>}
      {ch.log.length === 0 && <div className="muted small">{t('Изменений пока не было.')}</div>}
      {shown.length > 0 && (
        <div className="ch-list small">
          {shown.map((e, i) => {
            const hot = e.alert && e.ts > ack
            return (
              <div key={i} className={`ch-row${hot ? ' ch-row-hot' : ''}`}>
                <span className="ch-time mono muted" title={new Date(e.ts).toLocaleString()}>{when(e.ts)}</span>
                <span className={`ch-text${hot ? ' t-down' : ''}`}>
                  {CHANGE_ICON[e.kind] ?? ''} {e.text}
                </span>
              </div>
            )
          })}
        </div>
      )}
      {ch.log.length > 12 && (
        <button className="ghost small row-act exp-more" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : t('все изменения: {n}', { n: ch.log.length })}
        </button>
      )}
    </div>
  )
}

function PressureCard({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const r = s.last_report
  const psi = r?.psi
  if (!psi && !r?.dstate && !r?.fds) return null
  const prob = (s.problems ?? []).find((p) => p.kind === 'pressure')
  const pc = (v: number) => `${v.toFixed(v > 0 && v < 10 ? 1 : 0)}%`
  const fdPct = r?.fds && r.fds.max > 0 ? (r.fds.used / r.fds.max) * 100 : null
  const ds = r?.dstate
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-pressure">
      <div className="net-head">
        <div className="chart-cap">{t('Давление на ресурсы')}</div>
      </div>
      {prob && <div className="net-prob">{prob.text}</div>}
      <div className="net-tiles">
        {psi &&
          PSI_ROWS.map((x) => {
            const some = psi[`${x.k}_some_60`]
            const full = psi[`${x.k}_full_60`]
            const some10 = psi[`${x.k}_some_10`]
            if (some == null && full == null && some10 == null) return null
            // пороги как у алерта "Сервер задыхается": по минутному среднему
            const hot = (x.some > 0 && (some ?? 0) >= x.some) || (x.full > 0 && (full ?? 0) >= x.full)
            const sub = [
              full != null ? t('все стоят {p}', { p: pc(full) }) : '',
              some10 != null ? t('за 10 с {p}', { p: pc(some10) }) : '',
            ].filter(Boolean)
            return (
              <NetTile
                key={x.k}
                label={t('{r}: ждут', { r: t(x.label) })}
                value={pc(some ?? 0)}
                sub={sub.join(' · ')}
                tone={hot ? 'bad' : undefined}
              />
            )
          })}
        {ds && (
          <NetTile
            label={t('В D-state')}
            value={String(ds.n)}
            sub={(ds.top?.length ?? 0) > 0 ? ds.top!.map((x) => `${x.c} (${x.n})`).join(', ') : t('ждут диск или сеть')}
            tone={ds.n >= 5 ? 'bad' : undefined}
          />
        )}
        {r?.fds && fdPct != null && (
          <NetTile
            label={t('Файловые дескрипторы')}
            value={`${fdPct.toFixed(fdPct < 1 ? 2 : 0)}%`}
            sub={t('{u} из {m}', { u: fmtNum(r.fds.used), m: fmtNum(r.fds.max) })}
            tone={fdPct >= 80 ? 'bad' : undefined}
          />
        )}
      </div>
      {psi && (
        <div className="net-foot">
          {t('Процент - доля времени за минуту, когда хоть один процесс ждал ресурс, "все стоят" - когда ждали все сразу. D-state - процессы, которые ждут диск или сеть и не прерываются.')}
        </div>
      )}
    </div>
  )
}

function LoopingUnits({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState(false)
  const [jr, setJr] = useState<string | null>(null)
  const rows = (s.problems ?? []).filter((p) => p.kind === 'unit_loop' && p.unit)
  if (!rows.length) return null
  const canJournal = !isViewer && !journalBlocker(s)
  const mute = async (unit: string) => {
    if (!window.confirm(t('Не алертить по {u}? Юнит перестанет алертить и как упавший, и как перезапускающийся.', { u: unit }))) return
    setBusy(true)
    try {
      await updateServer(s.id, { alert_mutes: [...new Set([...(s.alert_mutes ?? []), UNIT_MUTE + unit])] })
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-unitloop">
      <div className="net-head">
        <div className="chart-cap">{t('Юниты в цикле перезапусков')}</div>
      </div>
      {rows.map((p) => (
        <div key={p.unit} className="unit-row">
          <div className="unit-head">
            <span className="lk-dot lk-dot-bad" />
            <span className="t-down">{p.text}</span>
            {sinceShort(p.since, t) && <span className="muted small">{sinceShort(p.since, t)}</span>}
          </div>
          {(p.log?.length ?? 0) > 0 && <pre className="unit-log mono">{p.log!.join('\n')}</pre>}
          <div className="muted small unit-hint">
            {t('systemd поднимает сервис сам, поэтому среди упавших его нет. Причину обычно видно в логе: journalctl -u {u} -n 50', { u: p.unit! })}
          </div>
          {!isViewer && (
            <div className="unit-actions small">
              {canJournal && (
                <button className="ghost small row-act" onClick={() => setJr(p.unit!)}>
                  {t('Журнал')}
                </button>
              )}
              <button className="ghost small row-act" disabled={busy} onClick={() => mute(p.unit!)}>
                🔕 {t('не алертить по этому юниту')}
              </button>
            </div>
          )}
        </div>
      ))}
      {jr && <JournalModal server={s} unit={jr} onClose={() => setJr(null)} />}
    </div>
  )
}

// Что открыто в интернет. Панель раз в 6 часов стучится снаружи (через первую локацию-прокси,
// без нее - сама) ко всем портам, которые слушает нода: что видно из интернета, а что закрыто.
// Красное - опасное (база, очередь, Docker API, kubelet, node_exporter), желтое - служебное (порты
// Kubernetes, агенты и экспортеры метрик): снаружи должно быть закрыто и то и другое. Проверка с
// адреса самой панели показывает, что открыто для панели: файрвол ноды часто пускает её по белому
// списку, поэтому цветов и кнопок там нет.
type PortGroup = {
  port: number
  name: string
  what: string
  risk: boolean
  svc: boolean
  open: string[]
  other: ExposedPort['state']
  // сайты ноды идут через Cloudflare: only - напрямую порт закрыт, also - открыт и в обход него
  cf?: 'only' | 'also'
  // проверке открыт только по белому списку с ее адресом: для интернета закрыт
  allow?: boolean
}

// замок для колонок "открыто" и "закрыто"; облако - порт, открытый только для Cloudflare
function LockIcon({ open = false }: { open?: boolean }) {
  return (
    <svg className="exp-ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <rect x="3" y="7" width="10" height="7" rx="1.5" />
      <path d={open ? 'M5.5 7V5a2.5 2.5 0 0 1 4.9-.7' : 'M5.5 7V5a2.5 2.5 0 0 1 5 0v2'} />
    </svg>
  )
}

// Домены по общей части: dev.example.com и s3.example.com -> example.com: dev, s3. Общая часть -
// два последних уровня, а у example.com.cy и example.co.uk три
function siteGroups(sites: string[]): { base: string; subs: string[] }[] {
  const by = new Map<string, string[]>()
  for (const d of sites) {
    const p = d.split('.')
    const n = p.length > 2 && p[p.length - 1].length === 2 && p[p.length - 2].length <= 3 ? 3 : 2
    const base = p.slice(-n).join('.')
    by.set(base, [...(by.get(base) ?? []), p.slice(0, -n).join('.')])
  }
  return [...by.entries()].map(([base, subs]) => ({ base, subs }))
}

function CloudIcon() {
  return (
    <svg className="exp-ic exp-cf" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
      <path d="M12.2 13H4.3a3.3 3.3 0 0 1-.5-6.56A4.3 4.3 0 0 1 12 6.3 3.35 3.35 0 0 1 12.2 13z" />
    </svg>
  )
}

// Здоровье сети ноды (агент 2.26): потери TCP, очередь accept, conntrack. Пороги - как у алерта
// "Сеть: потери и ошибки" (collector.net_problems).
function NetHealth({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const tcp = s.last_report?.tcp
  if (!tcp) return null
  const prob = (s.problems ?? []).find((p) => p.kind === 'net_health')
  // повторы SYN (подключения без ответа) - не потери: вычитаем, как collector.net_problems
  const syn = Math.min(tcp.syn_retrans ?? 0, tcp.retrans)
  const out = Math.max(tcp.out - syn, 0)
  const again = tcp.retrans - syn
  const pct = out > 0 ? (again / out) * 100 : 0
  const lossy = out >= 100 && pct >= 10
  // к кому уходят повторы (агент 2.32): почти все к одному адресу - канал до него, алерта нет
  // (collector.retrans_peers, те же пороги)
  const ptotal = tcp.peers_total ?? 0
  const peers = ptotal >= 30 ? (tcp.peers ?? []) : []
  const dominant = peers.length > 0 && peers[0].retrans / ptotal >= 0.8
  const peerName = (p: (typeof peers)[number]) => {
    const what = p.port ? (p.proc ? `${p.proc}:${p.port}` : `:${p.port}`) : (p.proc ?? '')
    return what ? `${p.ip} (${what})` : p.ip
  }
  const rate = (v: number) => (v >= 10 ? v.toFixed(0) : v.toFixed(v > 0 && v < 0.1 ? 2 : 1))
  const perSec = (v: number, bad: boolean) => (
    { value: rate(v), unit: t('в сек'), tone: bad ? ('bad' as const) : undefined }
  )
  return (
    <div className="chart-card net-card nethealth" id="mcard-nethealth">
      <div className="net-head">
        <div className="chart-cap">{t('Здоровье сети')}</div>
      </div>
      {prob && <div className="net-prob">{prob.text}</div>}
      <div className="net-tiles">
        <NetTile
          label={t('TCP повторно')}
          value={`${pct.toFixed(pct < 1 ? 2 : 1)}%`}
          sub={`${rate(again)} ${t('из')} ${rate(out)} ${t('сегм/с')}`}
          tone={lossy ? (dominant ? 'warn' : 'bad') : 'ok'}
        />
        {syn >= 1 && (
          <NetTile
            label={t('Подключения без ответа')}
            {...perSec(syn, false)}
            sub={t('повторы SYN, в потери не входят')}
            title={t('(повторы SYN: торрент-клиент за VPN, мертвый адрес или сканер; в потери не входят)')}
            tone="warn"
          />
        )}
        <NetTile label={t('Очередь accept переполнена')} {...perSec(tcp.listen_ovf, tcp.listen_ovf >= 1)} />
        <NetTile label={t('conntrack выбросил')} {...perSec(tcp.ct_drop, tcp.ct_drop >= 1)} />
        <NetTile label={t('Входящие с ошибкой')} {...perSec(tcp.in_errs, false)} />
      </div>
      {peers.length > 0 && (
        <div className="net-foot">
          <div>
            {t('Повторы уходят')}:{' '}
            {peers.map((p) => `${peerName(p)} ${Math.round((p.retrans / ptotal) * 100)}%`).join(' · ')}
          </div>
          {dominant && lossy && (
            <div>{t('Почти все к одному адресу: это канал до него, а не сеть сервера, поэтому без алерта.')}</div>
          )}
        </div>
      )}
    </div>
  )
}

function OpenPorts({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [err, setErr] = useState('')
  const ex = s.exposure
  if (!ex && !(s.last_report?.listen?.length ?? 0)) return null
  // проверка с самой панели: "открыто" значит "открыто для панели", судить по ней нельзя
  const own = !!ex?.own
  const muted = new Set(ex?.muted ?? [])
  const by = new Map<number, PortGroup>()
  for (const x of ex?.ports ?? []) {
    const g = by.get(x.port) ?? { port: x.port, name: x.name, what: x.what, risk: x.risk, svc: !!x.svc, open: [], other: x.state }
    if (x.state === 'open' && x.allow) g.allow = true
    else if (x.state === 'open') g.open.push(x.ip)
    else if (g.other !== 'filtered' && g.other !== 'closed') g.other = x.state
    if (x.cf === 'also' || (x.cf === 'only' && !g.cf)) g.cf = x.cf
    by.set(x.port, g)
  }
  // красное - опасное, желтое - служебное: и то и другое снаружи должно быть закрыто
  const flagged = (g: PortGroup) => !own && g.open.length > 0 && !muted.has(String(g.port))
  const rank = (g: PortGroup) => (!g.open.length ? 3 : flagged(g) && g.risk ? 0 : flagged(g) && g.svc ? 1 : 2)
  const groups = [...by.values()].sort((a, b) => rank(a) - rank(b) || a.port - b.port)
  const opened = groups.filter((g) => g.open.length)
  // закрытое: сначала то, что открыто только для Cloudflare, потом только белому списку
  const closed = groups
    .filter((g) => !g.open.length && (g.other !== 'unknown' || g.allow))
    .sort((a, b) => Number(b.cf === 'only') - Number(a.cf === 'only') || Number(!!b.allow) - Number(!!a.allow))
  const unknown = groups.filter((g) => !g.open.length && g.other === 'unknown' && !g.allow)
  const allowed = closed.filter((g) => g.allow)
  const viaIps = (ex?.via_ips ?? []).join(', ')
  const fresh = new Set((ex?.new ?? []).map((x) => x.port))
  const dangerous = opened.filter((g) => flagged(g) && g.risk).length
  const service = opened.filter((g) => flagged(g) && !g.risk && g.svc).length
  const scan = async () => {
    setScanning(true)
    setErr('')
    try {
      await scanExposure(s.id)
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t('не вышло'))
    } finally {
      setScanning(false)
    }
  }
  const setMuted = async (port: number, on: boolean) => {
    if (on && !window.confirm(t('Порт {p} открыт намеренно? Панель перестанет алертить по нему.', { p: port }))) return
    setBusy(true)
    try {
      const key = EXPOSED_MUTE + port
      const cur = (s.alert_mutes ?? []).filter((k) => k !== key)
      await updateServer(s.id, { alert_mutes: on ? [...cur, key] : cur })
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setBusy(false)
    }
  }
  // адрес в строке - только если открытые порты на адресах ноды расходятся
  const openIps = [...new Set(opened.flatMap((g) => g.open))]
  const quietIps = (ex?.ips ?? []).filter((ip) => !openIps.includes(ip))
  const perRowIps = openIps.length > 1
  const chips = (list: PortGroup[]) =>
    list.map((g) => (
      <span className="exp-chip" key={g.port}>
        <span className="mono">{g.port}</span>
        {g.name ? ` ${g.name}` : ''}
      </span>
    ))
  const ago = ex ? sinceShort(ex.ts, t) : ''
  const toFix = opened.some((g) => flagged(g) && (g.risk || g.svc))
  const cfOnly = closed.filter((g) => g.cf === 'only')
  const cfAlso = opened.filter((g) => g.cf === 'also')
  const cfSites = ex?.cf?.sites ?? []
  const cfMore = (ex?.cf?.n ?? 0) - cfSites.length
  const closedSub = [
    cfOnly.length ? t('только через Cloudflare: {n}', { n: cfOnly.length }) : '',
    allowed.length ? t('только белому списку: {n}', { n: allowed.length }) : '',
    unknown.length ? t('не проверить: {n}', { n: unknown.length }) : '',
  ].filter(Boolean)
  const openRow = (g: PortGroup) => {
    const m = muted.has(String(g.port))
    const bad = flagged(g) && g.risk
    const warn = flagged(g) && g.svc
    const where = perRowIps && g.open.length < openIps.length ? g.open.join(', ') : ''
    return (
      <div key={g.port} className="exp-li">
        <span className={`lk-dot${bad ? ' lk-dot-bad' : warn ? ' lk-dot-warn' : own ? '' : ' lk-dot-info'}`} />
        <span className={`exp-port mono${bad ? ' net-tone-bad' : warn ? ' net-tone-warn' : ''}`}>{g.port}</span>
        <div className="exp-what">
          {g.name ? <b>{g.name}</b> : <span>{g.what || t('процесс не известен')}</span>}
          {!own && fresh.has(g.port) && <> <span className="exp-new">{t('новый')}</span></>}
          {g.cf === 'also' && (
            <>
              {' '}
              <span className="exp-tag exp-tag-cf" title={t('Сайты ноды идут через Cloudflare, а порт открыт и напрямую: защиту Cloudflare можно обойти по адресу ноды.')}>
                {t('в обход Cloudflare')}
              </span>
            </>
          )}
          {g.name && g.what && <> <span className="exp-proc">{g.what}</span></>}
          {where && <> <span className="exp-proc">{t('на {ips}', { ips: where })}</span></>}
        </div>
        {own ? null : m ? (
          <span className="muted exp-act">
            {t('намеренно')}{' '}
            {!isViewer && (
              <button className="ghost small row-act" disabled={busy} onClick={() => setMuted(g.port, false)}>
                {t('вернуть алерт')}
              </button>
            )}
          </span>
        ) : (
          (g.risk || g.svc || fresh.has(g.port)) &&
          !isViewer && (
            <button className="ghost small row-act exp-act" disabled={busy} onClick={() => setMuted(g.port, true)}>
              🔕 {t('открыт намеренно')}
            </button>
          )
        )}
      </div>
    )
  }
  const closedRow = (g: PortGroup) => (
    <div key={g.port} className="exp-li exp-li-closed">
      {g.cf === 'only' ? <CloudIcon /> : <LockIcon />}
      <span className="exp-port mono">{g.port}</span>
      <div className="exp-what">
        <span>{g.name || g.what || t('процесс не известен')}</span>
        {g.cf === 'only' && <> <span className="exp-tag exp-tag-cf">{t('только через Cloudflare')}</span></>}
        {g.allow && (
          <>
            {' '}
            <span className="exp-tag exp-tag-allow" title={t('Снаружи закрыт: проверка видит его открытым только потому, что ее адрес {ips} в белом списке файрвола ноды.', { ips: viaIps })}>
              {t('только белому списку')}
            </span>
          </>
        )}
      </div>
    </div>
  )
  return (
    <div className="chart-card net-card net-card-wide exp-card" id="mcard-exposure">
      <div className="net-head">
        <div className="chart-cap">{t('Что открыто в интернет')}</div>
        {!isViewer && (
          <button className="ghost small row-act" disabled={scanning} onClick={scan}>
            {scanning ? t('проверяю...') : t('проверить сейчас')}
          </button>
        )}
      </div>
      <div className="net-sub">
        {ex
          ? own
            ? t(ago ? 'проверено {ago} назад с адреса самой панели' : 'проверено только что с адреса самой панели', { ago })
            : t(ago ? 'проверено снаружи {ago} назад через {via}' : 'проверено снаружи только что через {via}', {
                ago,
                via: t(ex.via),
              })
          : t('Внешней проверки еще не было: панель проверяет раз в 6 часов.')}
        {ex && !own && viaIps && <> · {t('адрес проверки: {ips}', { ips: viaIps })}</>}
        {ex && ex.ips.length > 0 && <> · {t('адреса ноды: {ips}', { ips: ex.ips.join(', ') })}</>}
        {opened.length > 0 && quietIps.length > 0 && <> · {t('на {ips} ничего не открыто', { ips: quietIps.join(', ') })}</>}
        {err && <span className="t-down"> {err}</span>}
      </div>
      {(ex?.foreign ?? []).map((f) => (
        <div key={f.ip} className="net-note">
          {f.nat
            ? t('Нода ходит в интернет через NAT: панель видит ее с адреса {ip}, а там отвечает сам шлюз ({o}, у ноды {n}). Порты по этому адресу не про ноду, алертов по ним нет. Делать ничего не нужно: входящие идут на другие адреса.', { ip: f.ip, o: f.outside, n: f.node })
            : t('По адресу {ip} отвечает другой сервер: там {o}, а у этой ноды {n}. Порты по этому адресу не про эту ноду, поэтому алертов по ним нет.', { ip: f.ip, o: f.outside, n: f.node })}
        </div>
      ))}
      {ex && !own && allowed.length > 0 && (
        <div className="net-note">
          {t('Адрес проверки {ips} стоит в белом списке файрвола ноды (его добавляют под мониторинг). Порты {ports} файрвол пускает только по белому списку: проверке они открыты, а интернету нет. Они в колонке "Закрыто снаружи", алертов по ним нет. Чтобы проверка видела то же, что и интернет, добавьте в "Локациях" прокси в другой сети.', { ips: viaIps, ports: allowed.map((g) => g.port).join(', ') })}
        </div>
      )}
      {ex && own && opened.length > 0 && (
        <div className="net-note">
          {t('Панель смотрела со своего адреса, а файрвол ноды часто пускает панель по белому списку. Тогда здесь "открыт" и то, что для остальных закрыто, поэтому по этой проверке нет ни красного, ни алертов. Чтобы видеть, что открыто для всех, добавьте в "Локациях" прокси в другой сети.')}
        </div>
      )}
      {ex && ex.ips.length > 0 && groups.length > 0 && (
        <div className="net-tiles">
          <NetTile
            label={own ? t('Открыто для панели') : t('Открыто снаружи')}
            value={String(opened.length)}
            sub={t('из {n} портов, что слушает нода', { n: groups.length })}
          />
          {!own && (
            <NetTile
              label={t('Опасных')}
              value={String(dangerous)}
              sub={t('базы, очереди, Docker API, kubelet')}
              tone={dangerous ? 'bad' : 'ok'}
            />
          )}
          {!own && (
            <NetTile
              label={t('Служебных')}
              value={String(service)}
              sub={t('Kubernetes, агенты, экспортеры')}
              tone={service ? 'warn' : 'ok'}
            />
          )}
          <NetTile
            label={own ? t('Закрыто даже для панели') : t('Закрыто снаружи')}
            value={String(closed.length)}
            sub={closedSub.length ? closedSub.join(' · ') : undefined}
          />
        </div>
      )}
      {ex && !ex.ips.length && <div className="muted small">{t('Публичного адреса у ноды не нашлось: проверять нечего.')}</div>}
      {ex && ex.ips.length > 0 && !opened.length && !closed.length && groups.length > 0 && (
        <div className="muted small">
          {t('Проверить не вышло: {via} не отвечает или не пропускает соединения на эти порты.', { via: t(ex.via) })}
        </div>
      )}
      {(opened.length > 0 || closed.length > 0) && (
        <div className="exp-cols">
          <div className={`exp-col exp-col-open${dangerous ? ' exp-col-bad' : service ? ' exp-col-warn' : ''}`}>
            <div className="exp-col-h">
              <span className={dangerous ? 'net-tone-bad' : service ? 'net-tone-warn' : 'exp-tone-info'}>
                <LockIcon open />
              </span>
              {own ? t('Открыто для панели') : t('Открыто снаружи')} <span className="exp-col-n">{opened.length}</span>
            </div>
            {opened.length > 0 ? (
              <div className="exp-lst">{opened.map(openRow)}</div>
            ) : (
              <div className="exp-empty">✓ {t('ничего не открыто')}</div>
            )}
          </div>
          <div className="exp-col exp-col-closed">
            <div className="exp-col-h">
              <span className="net-tone-ok">
                <LockIcon />
              </span>
              {own ? t('Закрыто даже для панели') : t('Закрыто снаружи')} <span className="exp-col-n">{closed.length}</span>
            </div>
            {closed.length > 0 ? (
              <div className="exp-lst">{closed.map(closedRow)}</div>
            ) : (
              <div className="exp-empty muted">{t('ничего не закрыто')}</div>
            )}
          </div>
        </div>
      )}
      {(cfOnly.length > 0 || cfAlso.length > 0) && (
        <div className="exp-cf-box">
          <div className="exp-cf-h">
            <CloudIcon />
            {t('Сайты через Cloudflare')} <span className="exp-col-n">{ex?.cf?.n ?? cfSites.length}</span>
            {ex?.cf && ex.cf.checked < ex.cf.total && (
              <span className="exp-cf-sub">{t('проверено {c} из {n}', { c: ex.cf.checked, n: ex.cf.total })}</span>
            )}
          </div>
          {siteGroups(cfSites).map((g) => (
            <div key={g.base} className="exp-cf-group">
              {/* сам домен в списке - он и подпись группы, иначе подпись серым */}
              {g.subs.includes('') ? <span className="exp-chip">{g.base}</span> : <span className="exp-cf-base">{g.base}</span>}
              {g.subs.filter(Boolean).map((sub) => (
                <span key={sub} className="exp-chip">
                  {sub}
                </span>
              ))}
            </div>
          ))}
          {cfMore > 0 && <div className="exp-cf-sub">{t('и еще {n}', { n: cfMore })}</div>}
          {cfOnly.length > 0 && (
            <div className="exp-cf-line">
              <span className="net-tone-ok">
                <LockIcon />
              </span>
              <span>
                {t('Напрямую закрыто:')} <b className="mono">{cfOnly.map((g) => g.port).join(', ')}</b>
                {', '}
                {t('туда пускают только Cloudflare')}
              </span>
            </div>
          )}
          {cfAlso.length > 0 && (
            <div className="exp-cf-line">
              <span className="exp-cf">
                <LockIcon open />
              </span>
              <span>
                {t('Напрямую открыто:')} <b className="mono">{cfAlso.map((g) => g.port).join(', ')}</b>
                {', '}
                {t('защиту Cloudflare можно обойти по адресу ноды. Если так не задумано, пустите туда только адреса Cloudflare')}
              </span>
            </div>
          )}
        </div>
      )}
      {unknown.length > 0 && (
        <div className="small exp-closed">
          <span className="muted">{t('не проверить ({n}):', { n: unknown.length })}</span> {chips(unknown)}
        </div>
      )}
      {(toFix || (ex && !own && opened.length > 0)) && (
        <div className="net-foot">
          {toFix && (
            <div className="exp-fix">
              {t('Закройте порт файрволом или привяжите сервис к 127.0.0.1 либо к внутреннему адресу. Порт Docker, опубликованный через -p, обходит ufw: публикуйте его как 127.0.0.1:порт:порт.')}
            </div>
          )}
          {ex && !own && opened.length > 0 && (
            <div>
              {t('Красным - опасное: базы, очереди, etcd, kubelet, Docker API, node_exporter. Желтым - служебное: порты Kubernetes и сети подов, агенты и экспортеры метрик. Снаружи должно быть закрыто и то и другое, а если порт открыт специально, отметьте "открыт намеренно". Остальное (SSH, веб, ваши сервисы) - для сведения.')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Точечный мут из карточки: добавить или снять ключ в alert_mutes сервера
function useMuteToggle(s: Server, onChanged: () => void) {
  const [busy, setBusy] = useState('')
  const toggle = (key: string, on: boolean) => {
    const cur = s.alert_mutes ?? []
    setBusy(key)
    updateServer(s.id, { alert_mutes: on ? [...new Set([...cur, key])] : cur.filter((m) => m !== key) })
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(''))
  }
  return { busy, toggle }
}

// Доступы (helper changes-setup 0.5): кто может войти, чем, с sudo ли без пароля и когда входил.
// Последний вход - по журналу sshd, не только по lastlog: ansible и CI ходят без терминала.
function AccessCard({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const { busy, toggle } = useMuteToggle(s, onChanged)
  const v = s.access
  if (!v || v.accounts.length === 0) return null
  const probs = (s.problems ?? []).filter((p) => p.kind === 'access_risk')
  const pw: Record<string, string> = { P: t('задан'), L: t('заблокирован'), NP: t('пустой') }
  const root2 = (a: AccessAccount) => a.uid === 0 && a.name !== 'root'
  // красное - второй root и пустой пароль (алерт), желтое - заброшенная учетка и слабые ключи
  const tone = (a: AccessAccount) =>
    root2(a) || a.pw === 'NP' ? 'bad' : (a.stale && !a.muted) || a.weak.length > 0 ? 'warn' : a.access ? 'ok' : ''
  const rank: Record<string, number> = { bad: 0, warn: 1, ok: 2, '': 3 }
  const rows = [...v.accounts].sort((a, b) => rank[tone(a)] - rank[tone(b)])
  const stale = v.accounts.filter((a) => a.stale && !a.muted).length
  const nopw = v.accounts.filter((a) => a.nopasswd).length
  return (
    <div className="chart-card net-card" id="mcard-access">
      <div className="net-head">
        <div className="chart-cap">{t('Доступы')}</div>
      </div>
      {probs.map((p) => (
        <div key={p.text} className="net-prob">{p.text}</div>
      ))}
      <div className="net-tiles">
        <NetTile
          label={t('Могут войти')}
          value={String(v.accounts.filter((a) => a.access).length)}
          sub={t('из {n} учеток с shell', { n: v.accounts.length })}
        />
        <NetTile
          label={t('Давно не входили')}
          value={String(stale)}
          sub={t('больше 90 дней, а доступ есть')}
          tone={stale ? 'warn' : 'ok'}
        />
        <NetTile label={t('sudo без пароля')} value={String(nopw)} />
      </div>
      <div className="loc-results">
        {rows.map((a) => {
          const tn = tone(a)
          return (
            <div key={a.name} className="loc-res acc-row">
              <span className={`lk-dot${tn ? ` lk-dot-${tn}` : ''}`} />
              <div className="acc-main">
                <span className="mono acc-name">{a.name}</span>
                {root2(a) && <span className="acc-tag acc-tag-bad">uid 0</span>}
                <span className={`acc-tag${a.pw === 'NP' ? ' acc-tag-bad' : ''}`}>
                  {t('пароль')} {pw[a.pw] ?? '-'}
                </span>
                {a.keys > 0 && (
                  <span className={`acc-tag${a.weak.length > 0 ? ' acc-tag-warn' : ''}`} title={a.weak.join(', ')}>
                    {t('ключей: {n}', { n: a.keys })}
                    {a.weak.length > 0 ? `, ${t('слабых: {n}', { n: a.weak.length })}` : ''}
                  </span>
                )}
                {a.nopasswd && <span className="acc-tag">sudo {t('без пароля')}</span>}
              </div>
              <div className={`acc-last ${a.stale && !a.muted ? 't-degraded' : 'muted'}`}>
                {a.last ? t('вход {d}', { d: new Date(a.last * 1000).toLocaleDateString() }) : t('не входили')}
              </div>
              {a.stale && !isViewer && (
                <button className="ghost small row-act acc-act" disabled={busy === ACCESS_MUTE + a.name}
                  onClick={() => toggle(ACCESS_MUTE + a.name, !a.muted)}>
                  {a.muted ? t('вернуть') : t('учетка нужна')}
                </button>
              )}
            </div>
          )
        })}
      </div>
      <div className="net-foot">
        {t('Входы по журналу sshd видны с {d}. Ненужную учетку заблокируйте (usermod -L -e 1 <имя>) или уберите ее ключи.', {
          d: v.seen_since ? new Date(v.seen_since * 1000).toLocaleDateString() : '-',
        })}
      </div>
    </div>
  )
}

// Сервисы, которые работают, но после перезагрузки не запустятся (helper units-setup 0.3)
function NoBootUnits({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const { busy, toggle } = useMuteToggle(s, onChanged)
  const [copied, setCopied] = useState(false)
  const units = s.noboot?.units ?? []
  if (units.length === 0) return null
  const live = units.filter((u) => !u.muted)
  const cmd = `systemctl enable ${live.map((u) => u.unit).join(' ')}`
  return (
    <div className="chart-card net-card" id="mcard-noboot">
      <div className="net-head">
        <div className="chart-cap">{t('Не запустятся после перезагрузки')}</div>
      </div>
      <div className="net-sub">
        {t('Работают, но не включены: их запустили systemctl start без enable, и после ребута их не будет.')}
      </div>
      <div className="loc-results">
        {units.map((u) => (
          <div key={u.unit} className={`loc-res nb-row${u.muted ? ' loc-res-off' : ''}`}>
            <span className={`lk-dot${u.muted ? '' : ' lk-dot-warn'}`} />
            <span className="mono nb-name">{u.unit}</span>
            {u.started > 0 && (
              <span className="muted small nb-since">
                {t('работает с {d}', { d: new Date(u.started * 1000).toLocaleDateString() })}
              </span>
            )}
            {!isViewer && (
              <button className="ghost small row-act" disabled={busy === NOBOOT_MUTE + u.unit}
                onClick={() => toggle(NOBOOT_MUTE + u.unit, !u.muted)}>
                {u.muted ? t('вернуть') : t('не нужен после перезагрузки')}
              </button>
            )}
          </div>
        ))}
      </div>
      {live.length > 0 && (
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
      )}
    </div>
  )
}

// SSH: как пускает соединение из интернета (helper changes-setup 0.3). Судим только порт, открытый
// снаружи по внешней проверке: SSH за белым списком с паролем - не дыра
function SshLogin({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const v = s.ssh
  if (!v) return null
  const probs = (s.problems ?? []).filter((p) => p.kind === 'ssh_risk')
  const notes = probs.length > 0 ? probs : (s.ssh_advice ?? [])
  const openText = v.open === true ? t('открыт в интернет') : v.open === false ? t('закрыт снаружи') : t('снаружи не проверялся')
  const rootText = v.root === 'yes' ? (v.root_password ? t('по паролю') : t('по ключу')) : v.root === 'no' ? t('запрещен') : t('только по ключу')
  const top = v.fail_top ?? []
  return (
    <div className="chart-card net-card" id="mcard-ssh">
      <div className="net-head">
        <div className="chart-cap">SSH</div>
      </div>
      {notes.map((p) => (
        <div key={p.text} className={p.level >= 2 ? 'net-prob' : 'net-note'}>{p.text}</div>
      ))}
      <div className="net-tiles net-tiles-text">
        <NetTile label={t('Порт')} value={v.ports.join(', ')} sub={openText} />
        <NetTile
          label={t('Вход по паролю')}
          value={v.password ? t('включен') : t('выключен')}
          sub={v.password ? undefined : t('только ключи')}
          tone={v.password ? (v.open ? 'warn' : undefined) : 'ok'}
        />
        <NetTile label={t('Вход root')} value={rootText} tone={v.root_password ? 'bad' : v.root === 'no' ? 'ok' : undefined} />
        <NetTile
          label={t('Защита от перебора')}
          value={v.guard || t('нет')}
          tone={v.guard ? 'ok' : v.open ? 'warn' : undefined}
        />
        <NetTile
          label={t('Неудачных входов за 10 минут')}
          value={String(v.fail)}
          sub={v.accepted_password > 0 ? t('входов по паролю: {n}', { n: v.accepted_password }) : undefined}
        />
      </div>
      {top.length > 0 && (
        <div className="net-foot">
          <div>
            {t('перебор идет с')}: <span className="mono">{top.map((x) => `${x.ip} (${x.n})`).join(', ')}</span>
          </div>
        </div>
      )}
    </div>
  )
}

function FailedUnits({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [st, setSt] = useState<Record<string, { busy?: boolean; ok?: boolean; msg?: string; log?: string[] }>>({})
  const [picking, setPicking] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [jr, setJr] = useState<string | null>(null)
  const r = s.last_report
  const ub = unitsBlock(r)
  if (!ub || !ub.units.length) return null
  const canJournal = !isViewer && !journalBlocker(s)
  const agentOk = verAtLeast(r?.agent_version, UNITS_AGENT)
  const canFix = ub.fix && agentOk && !isViewer
  const ref = r?.clock_unix || ub.ts
  const ago = (u: FailedUnit) => {
    if (!u.since) return ''
    const d = Math.max(0, ref - u.since)
    return d >= 86400
      ? t('{n} дн назад', { n: Math.floor(d / 86400) })
      : d >= 3600
        ? t('{n} ч назад', { n: Math.floor(d / 3600) })
        : t('{n} мин назад', { n: Math.max(1, Math.floor(d / 60)) })
  }
  const run = async (unit: string, mode: 'restart' | 'reset') => {
    const q =
      mode === 'restart'
        ? t('Перезапустить {u}?', { u: unit })
        : t('Сбросить у {u} отметку "упал"? Сам юнит не запустится, алерт по нему закроется.', { u: unit })
    if (!window.confirm(q)) return
    setSt((cur) => ({ ...cur, [unit]: { busy: true } }))
    try {
      const c = await backupCommand(s.id, { action: 'unit_fix', mode, unit })
      let last = c
      const t0 = Date.now()
      while (last.status !== 'done' && last.status !== 'error' && Date.now() - t0 < 7 * 60_000) {
        await new Promise((res) => setTimeout(res, 1500))
        last = await backupCommandStatus(s.id, c.id)
      }
      let data: UnitFixResult | null = null
      try {
        data = JSON.parse(last.result || '')
      } catch {
        data = null
      }
      const ok = last.status === 'done' && !!last.ok
      setSt((cur) => ({
        ...cur,
        [unit]: { ok, msg: data ? unitStateText(data, t) : last.result || t('не удалось'), log: data?.log },
      }))
      if (ok) window.setTimeout(onChanged, 5000)
    } catch (e) {
      setSt((cur) => ({ ...cur, [unit]: { ok: false, msg: e instanceof Error ? e.message : String(e) } }))
    }
  }
  const mute = async (kind: string, hours: number) => {
    setBusy(true)
    try {
      if (hours < 0) {
        await updateServer(s.id, { alert_mutes: [...new Set([...(s.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((s.alert_mutes ?? []).includes(kind))
          await updateServer(s.id, { alert_mutes: (s.alert_mutes ?? []).filter((x) => x !== kind) })
        if (s.alert_snoozes?.[kind]) await snoozeServerAlert(s.id, kind, 0)
      } else await snoozeServerAlert(s.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const muted = (u: FailedUnit) => ((s.alert_mutes ?? []).includes(UNIT_MUTE + u.unit) ? 1 : 0)
  const copy = (text: string, key: string) => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(key)
      window.setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500)
    })
  }
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-units">
      <div className="net-head">
        <div className="chart-cap">{t('Упавшие юниты systemd')}</div>
      </div>
      {!agentOk && !isViewer && ub.fix && (
        <div className="muted small du-note">{t('Кнопки перезапуска появятся после обновления агента до 2.17.')}</div>
      )}
      {[...ub.units]
        // заглушенные - в конец: они уже разобраны, внимание нужно остальным
        .sort((a, b) => muted(a) - muted(b) || unitLevel(b) - unitLevel(a))
        .map((u) => {
          const kind = UNIT_MUTE + u.unit
          const perm = (s.alert_mutes ?? []).includes(kind)
          const until = s.alert_snoozes?.[kind]
          const snoozed = !!until && new Date(until).getTime() > Date.now()
          const us = st[u.unit]
          const hint = unitHint(u, ub.units, t)
          return (
            <div key={u.unit} className={`unit-row${perm || snoozed ? ' unit-muted' : ''}`}>
              <div className="unit-head">
                <span className={`lk-dot lk-dot-${unitLevel(u) === 2 ? 'bad' : 'warn'}`} />
                <span className="mono unit-name">{u.unit}</span>
                <span className={unitLevel(u) === 2 ? 't-down' : 't-degraded'}>{unitWhy(u, t)}</span>
                <span className="muted small">{ago(u)}</span>
              </div>
              {u.desc && u.desc !== u.unit && <div className="muted small unit-desc">{u.desc}</div>}
              {u.log.length > 0 && <pre className="unit-log mono">{u.log.join('\n')}</pre>}
              {hint && (
                <div className="small unit-hint">
                  {hint.text}
                  {hint.cmd && (
                    <div className="agent-advice-cmd">
                      <pre>{hint.cmd}</pre>
                      <button className="ghost" onClick={() => copy(hint.cmd!, u.unit)}>
                        {copied === u.unit ? t('Скопировано') : t('Копировать')}
                      </button>
                    </div>
                  )}
                </div>
              )}
              {us?.busy && <div className="muted small">{t('Выполняю...')}</div>}
              {us?.msg && (
                <div className="small">
                  <span className={us.ok ? 't-up' : 'form-error'}>{us.msg}</span>
                  {(us.log?.length ?? 0) > 0 && !us.ok && <pre className="unit-log mono">{us.log!.join('\n')}</pre>}
                </div>
              )}
              <div className="unit-actions small">
                {canJournal && (
                  <button className="ghost small row-act" onClick={() => setJr(u.unit)}>
                    {t('Журнал')}
                  </button>
                )}
                {canFix && !us?.busy && (
                  <>
                    <button className="ghost small row-act" onClick={() => run(u.unit, 'restart')}>
                      {t('Перезапустить')}
                    </button>
                    <button className="ghost small row-act" onClick={() => run(u.unit, 'reset')}>
                      {t('Сбросить')}
                    </button>
                  </>
                )}
                {perm || snoozed ? (
                  <span className="snooze-active-chip snooze-perm">
                    🔇 {perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(until!) }) })}
                    {!isViewer && (
                      <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(kind, 0)}>
                        ✕
                      </button>
                    )}
                  </span>
                ) : (
                  !isViewer &&
                  (picking === kind ? (
                    <>
                      <span className="muted">🔕 {t('не алертить по этому юниту:')}</span>
                      {[
                        { h: 24, l: t('1 день') },
                        { h: 24 * 7, l: t('1 неделя') },
                        { h: -1, l: t('постоянно') },
                      ].map(({ h, l }) => (
                        <button key={h} className="ghost small" disabled={busy} onClick={() => mute(kind, h)}>
                          {l}
                        </button>
                      ))}
                      <button className="ghost small" disabled={busy} onClick={() => setPicking(null)}>
                        {t('Отмена')}
                      </button>
                    </>
                  ) : (
                    <button className="ghost small" onClick={() => setPicking(kind)}>
                      🔕 {t('не алертить')}
                    </button>
                  ))
                )}
              </div>
            </div>
          )
        })}
      {jr && <JournalModal server={s} unit={jr} onClose={() => setJr(null)} />}
    </div>
  )
}

// Поды Kubernetes этой ноды по памяти (агент 2.16+ берет ее из cgroup пода, как kubectl
// top): кто съел память и кнопка перезапуска его контроллера (rollout restart - Kubernetes
// пересоздает поды по очереди). Поды баз кнопкой не перезапускаются, как и контейнеры.
function MemPods({ server: s, onChanged, by = 'mem' }: { server: Server; onChanged: () => void; by?: 'mem' | 'cpu' }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [st, setSt] = useState<Record<string, { busy?: boolean; ok?: boolean; msg?: string }>>({})
  const val = (p: { mem?: number; cpu?: number }) => (by === 'cpu' ? p.cpu ?? 0 : p.mem ?? 0)
  const list = (s.last_report?.kube?.pods ?? [])
    .filter((p) => val(p) >= (by === 'cpu' ? CPU_SHOW_MIN : 1))
    .sort((a, b) => val(b) - val(a))
    .slice(0, 6)
  if (!list.length) return null
  const total = by === 'cpu' ? (s.last_report?.cpu_cores || 0) * 100 : s.last_report?.mem_total || 0
  const restart = async (key: string, ns: string, ctrl: string) => {
    const [kind, name] = ctrl.split('/') as ['deployment' | 'statefulset' | 'daemonset', string]
    if (!window.confirm(t('Перезапустить {c} в {ns}? Kubernetes пересоздаст его поды по очереди.', { c: ctrl, ns })))
      return
    setSt((cur) => ({ ...cur, [key]: { busy: true } }))
    try {
      const c = await kubeCommand(s.id, ns, kind, name, 'rollout_restart')
      let last = c
      const t0 = Date.now()
      while (last.status !== 'done' && last.status !== 'error' && Date.now() - t0 < 120_000) {
        await new Promise((res) => setTimeout(res, 1000))
        last = await kubeCommandStatus(s.id, c.id)
      }
      const ok = last.status === 'done' && !!last.ok
      setSt((cur) => ({ ...cur, [key]: { ok, msg: ok ? t('перезапуск начат') : last.result || t('не удалось') } }))
      if (ok) window.setTimeout(onChanged, 30_000)
    } catch (e) {
      setSt((cur) => ({ ...cur, [key]: { ok: false, msg: e instanceof Error ? e.message : String(e) } }))
    }
  }
  return (
    <div className="chart-card net-card">
      <div className="net-head">
        <div className="chart-cap">{by === 'cpu' ? t('Поды по CPU') : t('Поды по памяти')}</div>
      </div>
      <div className="loc-results">
        {list.map((p) => {
          const key = `${p.ns}/${p.name}`
          const db = !!p.image || DB_IMAGE.test(p.name)
          const ps = st[key]
          return (
            <div key={key} className="loc-res mc-row mp-row" title={p.ctrl ? `${key} (${p.ctrl})` : key}>
              <div className="loc-res-name mono mp-name">
                <span className="muted">{p.ns}/</span>
                {p.name}
              </div>
              <div className="loc-res-metric">{by === 'cpu' ? fmtCpu(p.cpu ?? 0, t) : fmtBytes(p.mem ?? 0)}</div>
              <div className="loc-res-msg muted small">
                {total ? `${Math.round((val(p) / total) * 100)}%` : ''}
              </div>
              {(!isViewer || ps?.msg) && (
                <div className="mc-act-col">
                  {ps?.msg && <span className={`small ${ps.ok ? 't-up' : 'form-error'}`}>{ps.msg}</span>}
                  {!isViewer &&
                    (db ? (
                      <span className="muted small">{t('база: перезапуск только вручную')}</span>
                    ) : p.ctrl ? (
                      <button className="ghost small row-act" disabled={!!ps?.busy} onClick={() => restart(key, p.ns, p.ctrl!)}>
                        {ps?.busy ? t('перезапускаю...') : t('Перезапустить')}
                      </button>
                    ) : (
                      <span className="muted small">{t('без контроллера')}</span>
                    ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// контейнеры и поды мельче 5% одного ядра в "кто ест CPU" не показываем - это фон
const CPU_SHOW_MIN = 5

// CPU из "% одного ядра" (как top) в ядра: 4510 -> "45 ядер", 250 -> "2.5 ядра"
function fmtCpu(pct: number, t: TFn): string {
  const v = pct / 100
  if (v < 10) return t('{n} ядра', { n: v.toFixed(1) })
  const n = Math.round(v)
  const m10 = n % 10
  const m100 = n % 100
  return t(m10 === 1 && m100 !== 11 ? '{n} ядро' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? '{n} ядра' : '{n} ядер', { n })
}

const CPU_OWNER_KIND: Record<string, string> = { pod: 'под', container: 'контейнер', unit: 'юнит' }

// Кто ест CPU группами (агент 2.21): процессы с одним именем у одного владельца складываются, и
// видно, сколько из них давно крутят ядро вхолостую. Так видны 47 зависших chrome одного пода:
// в топе процессов их было бы восемь строк по ядру без владельца.
function CpuGroups({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const groups = s.last_report?.cpu_groups ?? []
  if (!groups.length) return null
  const total = (s.last_report?.cpu_cores || 0) * 100
  // агент воркера без доступа к kube-api пишет под как "pod <uid8>", имя знает панель
  const owner = (g: CpuGroup) => {
    const o = g.owner || ''
    return g.kind === 'pod' && o.startsWith('pod ') ? s.pod_names?.[o.slice(4)] ?? o.slice(4) : o
  }
  const age = (sec: number) =>
    sec >= 86400 ? t('{n} дн', { n: Math.floor(sec / 86400) }) : t('{n} ч', { n: Math.floor(sec / 3600) })
  return (
    <div className="chart-card net-card">
      <div className="net-head">
        <div className="chart-cap">{t('Процессы по CPU')}</div>
      </div>
      <div className="loc-results">
        {groups.map((g, i) => (
          <div key={`${g.comm}-${g.owner}-${i}`} className={`loc-res mc-row cg-row${(g.spin ?? 0) >= 3 ? ' t-degraded' : ''}`}>
            <div className="loc-res-name mono cg-name">
              {g.comm}
              {g.n > 1 ? ` x${g.n}` : ''}
              {g.owner && (
                <span className="muted">
                  {' · '}
                  {CPU_OWNER_KIND[g.kind || ''] ? `${t(CPU_OWNER_KIND[g.kind || ''])} ` : ''}
                  {owner(g)}
                </span>
              )}
            </div>
            <div className="loc-res-metric">{fmtCpu(g.cpu, t)}</div>
            <div className="loc-res-msg muted small">{total ? `${Math.round((g.cpu / total) * 100)}%` : ''}</div>
            {(g.spin ?? 0) >= 2 && (
              <span className="small t-degraded cg-spin">
                {t('{n} крутят по ядру вхолостую, старшему {age}', { n: g.spin ?? 0, age: age(g.spin_age ?? 0) })}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
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

export function ServersPage({ onUnauthorized, openServerId, openServerSec, onConsumed, onOpen }: Props) {
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
  // диплинк из алерта/главной (?server=id) → открыть деталь и «съесть» id
  useEffect(() => {
    if (openServerId) {
      setDetailId(openServerId)
      setUrlSec(openServerSec ?? null)
      onConsumed?.()
    }
  }, [openServerId, openServerSec, onConsumed])
  useUrlCard('server', detailId)

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
            {/* медленный канал (РФ-ноды за DPI): показываем прогресс, иначе выглядит как
                «обновление не идёт», хотя докачка продолжается сама */}
            {behind.some((s) => s.agent_update_note) && (
              <span className="muted small aub-progress">
                {behind
                  .filter((s) => s.agent_update_note)
                  .map((s) => `${s.name}: ${s.agent_update_note}`)
                  .join(' · ')}
              </span>
            )}
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
          servers={servers ?? []}
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
          servers={servers ?? []}
          available={avail}
          groups={serverGroups}
          initialSection={detailSec ?? urlSec}
          onClose={() => { setDetailId(null); setDetailSec(null); setUrlSec(null) }}
          onChanged={load}
          onUnauthorized={onUnauthorized}
          onOpenSection={
            onOpen
              ? (sec, srv, dsec) => {
                  setDetailId(null)
                  setDetailSec(null)
                  setUrlSec(null)
                  onOpen(sec, detail.id, srv, dsec)
                }
              : undefined
          }
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
    // Вся карточка — вход в сервер, как строки «Докера», «Сервисов» и «Сайтов». Раньше
    // открывали только имя с адресом: наведение на карточку ничего не подсвечивало, а
    // клик по нагрузке справа не делал ничего. Кнопкой (<button>) карточку не сделать:
    // внутри свои интерактивные элементы — чекбокс, ручка переноса, 🔥.
    <div
      className={`check-row srv-row clickable${selecting ? ' selecting' : ''}${
        dragging ? ' dragging' : ''
      }`}
      role="button"
      tabIndex={0}
      onClick={() => {
        // выделяли текст (скопировать IP) — это не клик по карточке
        if (window.getSelection()?.toString()) return
        onOpen()
      }}
      onKeyDown={(e) => {
        // Enter/пробел на чекбоксе или 🔥 — их собственные, карточку не открываем
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
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
      <div className="check-main">
        <div className="check-name">
          {onFire && (
            <span
              className="srv-fire clickable"
              role="button"
              tabIndex={0}
              title={issues.map((i) => i.text).join(', ') + ' — ' + t('открыть график')}
              onClick={(e) => { e.stopPropagation(); onOpenSec(issues.find((i) => i.sec && !i.section)?.sec ?? null) }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation(); e.preventDefault()
                  onOpenSec(issues.find((i) => i.sec && !i.section)?.sec ?? null)
                }
              }}
            >
              🔥
            </span>
          )}
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {s.note && (
            <span className="note-chip" title={s.note}>
              📝
            </span>
          )}
          {Object.keys(s.known_issues ?? {}).length > 0 && (
            <span className="note-chip" title={Object.values(s.known_issues ?? {}).map((k) => k.text).join('; ')}>
              📌
            </span>
          )}
          <MaintChip kind="server" id={s.id} />
          {showGroup && s.group_name && (
            <span className="type-chip group-chip">{s.group_name}</span>
          )}
          {s.last_report?.backup_server?.present && (
            <span className="type-chip" title={t('Сервер бэкапов (rest-server)')}>🗄 {t('бэкап-сервер')}</span>
          )}
          {s.gpu_sum && (
            <GpuChip sum={s.gpu_sum} bad={issues.some((i) => i.kind === 'gpu' || i.kind === 'gpu_heat')} t={t} />
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

// initial - готовые значения полей: так ноду кластера без агента добавляют из "Кубера"
// одной кнопкой, с ее именем, группой контроллера и адресом.
export function EnrollModal({
  servers,
  groups,
  initial,
  onClose,
  onEnrolled,
  onUnauthorized,
}: {
  servers: Server[]
  groups: string[]
  initial?: { name?: string; group?: string; ip?: string }
  onClose: () => void
  onEnrolled: (e: ServerEnroll) => void
  onUnauthorized: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState(initial?.name ?? '')
  const [group, setGroup] = useState(initial?.group ?? '')
  const [agentIp, setAgentIp] = useState(initial?.ip ?? '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Дубль видно до нажатия: имя занято - создавать нельзя (панель различает ноды по имени).
  // Адрес настоящий дубль, только если он стоит на самой ноде (local_ip). Совпал внешний адрес -
  // это серверы за одним NAT или edge с пробросом портов, их законно много, и пугать нечем
  const nameDup = servers.find((s) => s.name.trim().toLowerCase() === name.trim().toLowerCase())
  const ip = agentIp.trim()
  const ipDup = ip ? servers.find((s) => (s.local_ip || '').trim() === ip) : undefined
  const ipShared = ip && !ipDup
    ? servers.filter((s) => [s.agent_ip, s.external_ip].some((x) => (x || '').trim() === ip))
    : []

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
          {nameDup && (
            <span className="field-hint form-error small">
              {t('Сервер с таким именем уже есть ({state}). Панель различает ноды по имени.', {
                state: nameDup.online ? t('на связи') : t('оффлайн'),
              })}
            </span>
          )}
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
          {ipDup && !nameDup && (
            <span className="field-hint t-degraded small">
              {t('Этот адрес уже у сервера {name}. Если это та же нода, второй раз её заводить не нужно.', {
                name: ipDup.name,
              })}
            </span>
          )}
          {ipShared.length > 0 && !nameDup && (
            <span className="field-hint muted small">
              {t('Тот же внешний адрес у {names}. Для серверов за одним NAT или пробросом портов это нормально.', {
                names: ipShared.map((s) => s.name).join(', '),
              })}
            </span>
          )}
          <span className="field-hint muted small">
            {t('Если панель закрыта фаерволом: адрес попадёт в data/agent_allow_ips, а хостовый скрипт ops/agent-firewall-sync.sh разрешит его в ufw/firewalld. Для Caddy-вайтлиста ничего не нужно — /api/agent/* уже открыт.')}
          </span>
        </label>
        {err && <p className="form-error">{err}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {t('Отмена')}
          </button>
          <button onClick={submit} disabled={!name.trim() || !!nameDup || busy}>
            {busy ? t('…') : t('Создать')}
          </button>
        </div>
      </div>
    </div>
  )
}

export function InstallModal({ enroll, onClose, title }: { enroll: ServerEnroll; onClose: () => void; title?: string }) {
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
          <h3>{title ?? t('Сервер создан')}</h3>
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
  const labelOf = (k: string) => muteKindLabel(k, t) ?? SNOOZE_KINDS.find((x) => x.k === k)?.label ?? k
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
  const { isAdmin } = useAuth()
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
      {/* Панель начнет сама запускать удаление на ноде - решение админа (сервер проверяет) */}
      <label className="checkbox">
        <input
          type="checkbox"
          checked={form.disk_autofix}
          disabled={!isAdmin}
          onChange={(e) => set({ disk_autofix: e.target.checked })}
        />
        <span>🧹 {t('Автоочистка диска')}</span>
      </label>
      <p className="muted small">
        {t('Когда раздел дошел до порога предупреждения, панель сама освобождает безопасное: журнал, старые логи, кэши пакетов, мусор docker. Каждое действие не чаще раза в 6 часов, о каждом запуске пишет в канал алертов. Включает только админ, нужны агент 2.13 и helper diskusage-setup 0.3.')}
      </p>
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
          <span>🌐 {t('Алерт по ошибкам 5xx, % за 15 минут (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={100}
            step={0.01}
            value={form.web_5xx_alert_percent}
            onChange={(e) => set({ web_5xx_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>⏳ {t('Предупреждать о сроках Kubernetes за, дн. (через запятую, пусто = выкл)')}</span>
          <input
            type="text"
            placeholder="7, 1"
            value={form.kube_expiry_warn_days}
            onChange={(e) => set({ kube_expiry_warn_days: e.target.value })}
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
  const mc = padToNow(buildMetric(metricKey, M, t), zoom ? zoom.to * 1000 : Date.now())
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
            yNice={mc.yNice}
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
  servers,
  available,
  groups,
  initialSection,
  onClose,
  onChanged,
  onUnauthorized,
  onOpenSection,
}: {
  server: Server
  servers: Server[]
  available: string
  groups: string[]
  initialSection?: string | null
  onClose: () => void
  onChanged: () => void
  onUnauthorized: () => void
  // проблема разбирается в другом разделе панели (Бэкапы, Кубер): закрыть карточку и открыть там
  onOpenSection?: (section: Section, srv?: boolean, sec?: string) => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [updBusy, setUpdBusy] = useState(false)
  const [snoozing, setSnoozing] = useState(false)
  const [histOpen, setHistOpen] = useState(false)
  // команда установки заново (чек-лист подключения): новый токен, старая команда не сработает
  const [enroll, setEnroll] = useState<ServerEnroll | null>(null)
  const reissue = async () => {
    try {
      setEnroll(await rotateServerToken(s.id))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
    }
  }
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

  // открытый раздел: сводка или тот, куда вела ссылка; в адресе он виден (?sec=), ссылкой
  // на раздел можно поделиться
  const [active, setActive] = useState<string>(() => secOfLink(initialSection) ?? 'overview')
  useUrlParam('sec', active === 'overview' ? null : active)
  const layoutRef = useRef<HTMLDivElement>(null)
  // Другой раздел: показываем его с начала. Если страница уже прокручена ниже меню (длинный
  // раздел графиков), подкручиваем к началу разделов, иначе новый раздел открылся бы с середины
  const go = (id: string) => {
    setActive(id)
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }

  // Ссылка из алерта или с главной (?sec=diskfill): открыть раздел, а карточку - в центр экрана.
  // Графики догружаются и сдвигают ее вниз, поэтому через полторы секунды еще раз, если человек
  // сам не крутил страницу
  const hasReport = !!s.last_report
  const linkedRef = useRef<string | null>(null)
  useEffect(() => {
    if (!initialSection || !hasReport || linkedRef.current === initialSection) return
    const sec = secOfLink(initialSection) ?? 'overview'
    setActive(sec)
    if (sec === initialSection) {
      linkedRef.current = initialSection
      return
    }
    let touched = false
    const touch = () => { touched = true }
    window.addEventListener('wheel', touch, { once: true, passive: true })
    window.addEventListener('touchstart', touch, { once: true, passive: true })
    window.addEventListener('keydown', touch, { once: true })
    const show = (behavior: ScrollBehavior) =>
      document.getElementById(`mcard-${initialSection}`)?.scrollIntoView({ behavior, block: 'center' })
    // отметка - только когда прокрутили: эффект перезапускается (StrictMode в разработке), и
    // отмененный таймер иначе оставлял раздел без прокрутки к карточке
    const first = window.setTimeout(() => {
      linkedRef.current = initialSection
      show('smooth')
    }, 200)
    const again = window.setTimeout(() => { if (!touched) show('auto') }, 1700)
    return () => {
      window.clearTimeout(first)
      window.clearTimeout(again)
      window.removeEventListener('wheel', touch)
      window.removeEventListener('touchstart', touch)
      window.removeEventListener('keydown', touch)
    }
  }, [initialSection, hasReport])

  // Уязвимости - для карты сервера и пункта меню (сама карточка грузит их сама, когда открыта).
  // Перезапрос - когда сменились пакеты или ядро, как у карточки
  const [vulns, setVulns] = useState<ServerVulns | null>(null)
  const pkgN = r.pkgs?.n ?? 0
  const pkgHash = r.pkgs?.h ?? ''
  const kernelNow = r.os_upd?.kernel ?? ''
  useEffect(() => {
    if (!pkgN) return
    let alive = true
    getServerVulns(s.id)
      .then((v) => { if (alive) setVulns(v) })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [s.id, pkgN, pkgHash, kernelNow])
  const kids = useDependentChecks(s.id)
  // хронология за сутки для блока на сводке: грузится, пока сводка открыта, раз в минуту заново
  const [day, setDay] = useState<TimelineResult | null>(null)
  useEffect(() => {
    if (active !== 'overview' || !hasReport) return
    let alive = true
    const run = () =>
      getServerTimeline(s.id, 1)
        .then((v) => { if (alive) setDay(v) })
        .catch(() => {})
    run()
    const id = window.setInterval(run, 60000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [s.id, active, hasReport])

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
      disk_autofix: !!s.disk_autofix,
      temp_alert_c: s.temp_alert_c,
      conntrack_alert_percent: s.conntrack_alert_percent,
      db_conn_alert_percent: s.db_conn_alert_percent,
      web_5xx_alert_percent: s.web_5xx_alert_percent ?? 0.05,
      kube_expiry_warn_days: (s.kube_expiry_warn_days ?? []).join(', '),
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
        disk_autofix: form.disk_autofix,
        temp_alert_c: form.temp_alert_c,
        conntrack_alert_percent: form.conntrack_alert_percent,
        db_conn_alert_percent: form.db_conn_alert_percent,
        web_5xx_alert_percent: form.web_5xx_alert_percent,
        kube_expiry_warn_days: form.kube_expiry_warn_days.split(/[^0-9]+/).filter(Boolean).map(Number),
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

  // Веб: цвета строк под графиками - те же, что у полос стека (по подписи ряда).
  const wr = webRate(r)
  // Раздел держится и на истории за период, а не только на последнем отчете: блок
  // helper'а бывает пропущен (установка helper'а, ротация всех логов разом), и раздел
  // вместе со ссылкой из алерта пропадал бы на эту минуту
  const hasWeb = !!wr || M.some((m) => m.web_rpm != null)
  const webColors = new Map<string, string>()
  if (hasWeb)
    for (const k of ['web', 'web5xx'] as const)
      for (const x of buildMetric(k, M, t).series) webColors.set(`${k}|${x.name}`, x.color)
  const webColor = (k: MetricKey, name: string) => webColors.get(`${k}|${name}`)
  // коды 5xx последней минуты по всем логам
  const codesNow = Object.entries(
    (wr?.logs ?? []).reduce<Record<string, number>>((acc, l) => {
      for (const [c, n] of Object.entries(l.c5 ?? {})) acc[c] = (acc[c] ?? 0) + n
      return acc
    }, {}),
  ).sort((a, b) => b[1] - a[1])

  // Секция графика: кликабельная карточка → полноэкранный график этой метрики.
  const chartCard = (key: MetricKey, extra?: React.ReactNode) => {
    const mc = padToNow(buildMetric(key, M, t), Date.now())
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
            yNice={mc.yNice}
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

  const issues = srvIssues(s, t)
  const muteItems = collectMutes(s, t, ['alert'])
  const info = serverSecInfo({ s, t, issues, vulns, kids, servers, mutes: muteItems })
  // Пункт меню есть, только когда в разделе есть что показать. Открытый раздел остается всегда:
  // по ссылке на веб данные догружаются, и раздел не должен пропасть из-под рук
  const shown = (id: string): boolean => {
    switch (id) {
      case 'web': return hasWeb
      case 'gpu': return !!s.gpu
      case 'access': return !!(s.ssh || s.access?.accounts.length)
      case 'updates': return !!(r.os_upd || s.os_advice?.length || s.sw_advice?.length || r.docker?.containers?.length)
      case 'vulns':
      case 'pkgs': return !!r.pkgs?.n
      case 'journal': return !isViewer
      case 'changes': return !!s.changes
      default: return true
    }
  }
  const secs = DETAIL_SECTIONS.filter((x) => x.id === active || shown(x.id))
  const cur = DETAIL_SECTIONS.find((x) => x.id === active) ?? DETAIL_SECTIONS[0]
  // на сводке строками: проблемы из другого раздела панели (со ссылкой туда) и то, у чего нет
  // своего раздела. Часы и DNS не здесь: у них свои блоки с тем, что делать
  const ovIssues = issues.filter(
    (i) => i.section || (i.kind !== 'offline' && i.kind !== 'clock' && i.kind !== 'dns' && (secOfLink(i.sec) ?? 'overview') === 'overview'),
  )
  const known = s.known_issues ?? {}
  const ub = unitsBlock(r)

  const overview = (
    <div className="srv-sec">
      <OnboardBlock server={s} t={t} canManage={!isViewer} onChanged={onChanged} onOpenSection={onOpenSection}
        onInstall={isViewer ? undefined : reissue} />
      {!isViewer && (s.agent_fix_command || (s.helper_advice?.length ?? 0) > 0) && (
        <ServerFixBanner titles={s.agent_advice} command={s.agent_fix_command} helpers={s.helper_advice || []} t={t} />
      )}
      {/* сдвиг часов: ломает TOTP, TLS и логи, плюс команда фикса */}
      {r.clock && Math.abs(r.clock_skew_sec ?? 0) >= 5 && (
        <ClockFix server={s} clock={r.clock} skew={r.clock_skew_sec} canManage={!isViewer} />
      )}
      {/* DNS тормозит или не резолвит (сразу, проблемой - после выдержки алерта) либо дольше
          10 минут лежит один из резолверов: что именно и что с этим делать прямо сейчас. Совет -
          по тому, как нода ходит в DNS */}
      {(s.problems ?? []).filter((p) => p.kind === 'dns').map((p) => (
        <div key="dns" className="dns-problem small">
          <span className={p.level >= 2 ? 't-down' : 't-degraded'}>{p.text}</span>
          <div className="muted">
            {p.sub === 'resolver'
              ? t('Остальные резолверы отвечают, и имена резолвятся, но запасного нет: уберите неработающий из настроек сети ноды (netplan, /etc/resolv.conf) или замените рабочим.')
              : r.dns?.mode === 'local'
                ? t('Ноду обслуживает свой резолвер (unbound, dnsmasq): проверьте его и серверы, куда он пересылает запросы.')
                : r.dns?.mode === 'direct'
                  ? t('Пока резолверы провайдера тормозят, можно поставить первыми в /etc/resolv.conf публичные (nameserver 1.1.1.1 и 8.8.8.8): запросы идут к резолверам по порядку.')
                  : t('Пока резолверы провайдера тормозят, можно добавить на ноде публичные: DNS=1.1.1.1 8.8.8.8 в /etc/systemd/resolved.conf.d/. systemd-resolved спросит их вместе с резолверами сети и возьмет первый ответ.')}
          </div>
        </div>
      ))}
      <OvRows
        rows={ovIssues.map((i) => ({
          tone: i.tone,
          text: i.text,
          since: i.since,
          act: i.section && onOpenSection
            ? { label: t(SECTION_LABEL[i.section] ?? i.section), onClick: () => onOpenSection(i.section!, i.srv, i.sec) }
            : undefined,
        }))}
      />
      <DayBlock day={day} onMore={() => go('timeline')} />
      <SrvTiles r={r} M={M} info={info} onGo={go} gpu={s.gpu_sum} />
      <OvAreas
        cap={t('Сервер по областям')}
        items={AREA_SECS.filter((id) => secs.some((x) => x.id === id)).map((id) => ({
          id,
          label: DETAIL_SECTIONS.find((d) => d.id === id)!.label,
          info: info[id],
        }))}
        onGo={go}
      />
    </div>
  )

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
            {s.onboard && <span className="type-chip onb-chip">{t('подключается')}</span>}
            {/* что заглушено - значком, сами заглушки в разделе "Алерты" */}
            {muteItems.length > 0 && !editing && (
              <button className="srv-head-chip" onClick={() => go('alerts')}>
                <MuteChip items={muteItems} t={t} />
              </button>
            )}
            {/* заметка живет в редактировании сервера, здесь - значком с текстом в подсказке */}
            {s.note && !editing && (
              <button
                className="srv-head-chip srv-head-note"
                title={s.note}
                onClick={isViewer ? undefined : startEdit}
              >
                📝
              </button>
            )}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={() => setHistOpen(true)} title={t('История алертов')}>
              🔔
            </button>
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
        {!editing && <MaintBanner kind="server" id={s.id} />}
        {/* Карточка молчащей ноды показывала свежие на вид проценты и «обновлено N
            назад» мелким шрифтом среди прочей меты — выглядело как рабочий сервер.
            Говорим прямо, и сразу под именем: цифры ниже historical, а не текущие. */}
        {!editing && s.last_report && !s.online && (
          <div className="srv-offline-note">
            <span className="srv-offline-ic">🔴</span>
            <span>
              <b>{t('Сервер недоступен')}</b>{' '}
              {s.last_seen
                ? t('— агент молчит {ago}. Всё, что ниже, — последние данные, снятые тогда же.',
                    { ago: fmtRel(s.last_seen) })
                : t('— агент ещё ни разу не выходил на связь.')}
            </span>
          </div>
        )}
        {s.last_report ? (
          <div className="srv-meta srv-meta-line">
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
            {r.dns && (
              <span className="srv-meta-item">
                <span className="srv-meta-k">DNS</span> <DnsStatus dns={r.dns} />
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
          <>
            <NoteBar
              note={s.note}
              by={s.note_by}
              at={s.note_at}
              canEdit={!isViewer}
              onSave={async (text) => {
                try {
                  await setServerNote(s.id, text)
                  onChanged()
                } catch (e) {
                  if (e instanceof ApiError && e.status === 401) onUnauthorized()
                  throw e
                }
              }}
            />
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
          </>
        ) : !s.last_report ? (
          <div className="srv-nodata">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <div className="srv-nodata-title">{t('Сервер недоступен')}</div>
            <p className="muted small">
              {t('Агент ещё не выходил на связь. Установите и запустите kervax-agent на ноде — метрики появятся здесь. Изменить или удалить сервер — кнопка ✎.')}
            </p>
            {/* команда показывается один раз при добавлении; потеряли - новая с новым токеном */}
            {!isViewer && !s.first_report_at && (
              <>
                <button className="small-btn" onClick={reissue}>{t('Команда установки')}</button>
                <p className="muted small">{t('Панель выпустит новый токен: прежняя команда перестанет работать.')}</p>
              </>
            )}
          </div>
        ) : (
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={active} info={info} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              {CHART_SECS.has(active) && (
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
              )}
            </div>

        {active === 'overview' && overview}

        {active === 'cpu' && (
          <div className="srv-sec">
          <div className="net-cards now-cards">
            <CpuNow server={s} />
          </div>
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
          {/* кто ест CPU: сюда ведет алерт о процессах в пустом цикле (sec=cpueat) */}
          <div className="net-cards" id="mcard-cpueat">
            <MemContainers server={s} onChanged={onChanged} by="cpu" />
            <MemPods server={s} onChanged={onChanged} by="cpu" />
            <CpuGroups server={s} />
          </div>
          </div>
        )}

        {active === 'mem' && (
          <div className="srv-sec">
          <div className="net-cards now-cards">
            <MemNow server={s} />
          </div>
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
          <div className="net-cards">
            <MemLeaks server={s} />
            <MemContainers server={s} onChanged={onChanged} />
            <MemPods server={s} onChanged={onChanged} />
          </div>
          </div>
        )}

        {active === 'disk' && (
          <div className="srv-sec">
          <div className="net-cards">
            <DiskSpaceCard server={s} r={r} onChanged={onChanged} />
            <DiskUsageBlock server={s} onChanged={onChanged} />
            <DiskHealthBlock r={r} wear={s.wear_forecast} />
            <DiskWritersBlock server={s} r={r} />
          </div>
          <div className="chart-grid">
            {chartCard('disk')}
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
          </div>
          </div>
        )}

        {/* сеть вместе с соединениями: conntrack и сокеты раньше были отдельным разделом,
            и порты приходилось искать то в сети, то в соединениях */}
        {active === 'net' && (
          <div className="srv-sec">
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
          <div className="net-cards">
            <NetHealth server={s} />
            <LinksCard server={s} />
            <TrafficCard server={s} onChanged={onChanged} />
            <NetUsersCard server={s} r={r} />
          </div>
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
          </div>
        )}

        {active === 'gpu' && (
          <div className="srv-sec">
            <div className="net-cards">
              <GpuCard
                server={s}
                rows={issues
                  .filter((i) => !i.section && secOfLink(i.sec) === 'gpu')
                  .map((i) => ({ tone: i.tone, text: i.text, since: i.since }))}
              />
            </div>
            {(s.gpu?.gpus?.length ?? 0) > 0 && (
              <div className="chart-grid">
                {chartCard('gpuutil')}
                {chartCard('gpumem')}
                {chartCard('gputemp')}
                {chartCard('gpupower')}
              </div>
            )}
          </div>
        )}

        {active === 'ports' && (
          <div className="srv-sec">
            {s.exposure || r.listen?.length ? (
              <div className="net-cards">
                <OpenPorts server={s} onChanged={onChanged} />
              </div>
            ) : (
              <div className="srv-empty">{t('Снаружи еще не проверяли: проверка идет раз в 6 часов.')}</div>
            )}
          </div>
        )}

        {active === 'access' && (
          <div className="srv-sec">
            <div className="net-cards">
              <SshLogin server={s} />
              <AccessCard server={s} onChanged={onChanged} />
            </div>
          </div>
        )}

        {active === 'updates' && (
          <div className="srv-sec">
            <OsUpdates server={s} />
            <SoftwareCard server={s} />
          </div>
        )}

        {active === 'vulns' && (
          <div className="srv-sec">
            <div className="net-cards">
              <VulnsCard server={s} onUnauthorized={onUnauthorized} />
            </div>
          </div>
        )}

        {active === 'units' && (
          <div className="srv-sec">
            <div className="net-cards">
              <LoopingUnits server={s} onChanged={onChanged} />
              <FailedUnits server={s} onChanged={onChanged} />
              <NoBootUnits server={s} onChanged={onChanged} />
            </div>
            {!ub?.units.length && info.units.level === 0 && (
              <div className="srv-empty">
                {ub ? `✓ ${t('Упавших служб нет.')}` : t('Нет данных: на ноде не установлен helper units-setup.')}
              </div>
            )}
          </div>
        )}

        {active === 'proc' && (
          <div className="srv-sec">
            <div className="net-cards">
              <PressureCard server={s} />
            </div>
            <div className="chart-grid">
              <ProcCard title={t('Топ по CPU')} procs={r.top_cpu} kind="cpu" total={r.mem_total ?? 0} t={t} />
              <ProcCard title={t('Топ по памяти')} procs={r.top_mem} kind="mem" total={r.mem_total ?? 0} t={t} />
            </div>
          </div>
        )}

        {active === 'pkgs' && (
          <div className="srv-sec">
            <div className="net-cards">
              <PackagesCard server={s} onUnauthorized={onUnauthorized} />
            </div>
          </div>
        )}

        {active === 'journal' && (
          <div className="srv-sec">
            <div className="net-cards">
              <JournalCard server={s} />
            </div>
          </div>
        )}

        {active === 'changes' && (
          <div className="srv-sec">
            <div className="net-cards">
              <ChangesFeed server={s} onChanged={onChanged} />
            </div>
          </div>
        )}

        {/* Веб: сюда ведет ссылка из алерта по 5xx - сначала список, где ошибки, какие коды и
            пути, потом графики */}
        {active === 'web' && (
          <div className="srv-sec">
            <div className="net-cards now-cards">
              <WebNow server={s} />
              <WebErrorList server={s} t={t} onChanged={onChanged} onUnauthorized={onUnauthorized} />
            </div>
            <div className="chart-grid">
              {chartCard(
                'web',
                <div className="loc-results chart-stats">
                  <StatRow
                    color={webColor('web', t('запросов/мин'))}
                    name={t('запросов/мин')}
                    value={fmtCount(wr?.rpm)}
                  />
                  {(wr?.logs ?? [])
                    .filter((l) => l.rpm > 0)
                    .sort((a, b) => b.rpm - a.rpm)
                    .slice(0, 5)
                    .map((l) => (
                      <StatRow
                        key={l.log}
                        color={webColor('web', webSeriesName(webLogLabel(l)))}
                        name={webLogLabel(l)}
                        value={fmtCount(l.rpm)}
                      />
                    ))}
                </div>,
              )}
              {/* ошибки рядом с запросами: рост с нуля до 0.2% видно только так -
                  синтетический монитор такую долю не поймает */}
              {chartCard(
                'web5xx',
                <div className="loc-results chart-stats">
                  <StatRow
                    color={webColor('web5xx', t('5xx/мин'))}
                    name={t('5xx/мин')}
                    value={fmtCount(wr ? wr.e5 ?? 0 : null)}
                  />
                  {codesNow.map(([c, n]) => (
                    <StatRow key={c} color={webColor('web5xx', c)} name={c} value={fmtCount(n)} />
                  ))}
                  {webUnparsed(r) > 0 && (
                    <StatRow name={t('без кода ответа')} value={fmtCount(webUnparsed(r))} />
                  )}
                </div>,
              )}
            </div>
          </div>
        )}

        {active === 'deps' && (
          <div className="srv-sec">
            <ServerDeps server={s} servers={servers} kids={kids} canEdit={!isViewer} onSaved={onChanged} />
          </div>
        )}

        {active === 'alerts' && (
          <div className="srv-sec">
            {!isViewer ? (
              <SnoozeBar
                snoozeUntil={s.snooze_until}
                snoozes={s.alert_snoozes}
                mutes={(s.alert_mutes ?? []).filter((k) => !isDecision(k))}
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
            ) : muteItems.length > 0 ? (
              <div className="snooze-active-list">
                {muteItems.map((i) => (
                  <span key={i.label} className="snooze-active-chip">🔕 {muteText(i, t)}</span>
                ))}
              </div>
            ) : (
              <div className="srv-empty">{t('Ничего не заглушено.')}</div>
            )}
            {Object.keys(known).length > 0 && (
              <KnownServerList
                items={known}
                canEdit={!isViewer}
                onDrop={async (kind) => {
                  try {
                    await dropServerKnown(s.id, kind)
                    onChanged()
                  } catch (e) {
                    if (e instanceof ApiError && e.status === 401) onUnauthorized()
                  }
                }}
              />
            )}
            <div>
              <button className="ghost" onClick={() => setHistOpen(true)}>
                🔔 {t('История алертов')}
              </button>
            </div>
          </div>
        )}

        {active === 'timeline' && (
          <div className="srv-sec">
            <div className="net-cards">
              <TimelineCard serverId={s.id} onUnauthorized={onUnauthorized} />
            </div>
          </div>
        )}
          </div>
        </div>
        )}
        <ScrollTopBtn t={t} />
      </div>
    </div>
    {zoomKey && (
      <ServerChartModal server={s} metricKey={zoomKey} onClose={() => setZoomKey(null)} />
    )}
    {histOpen && (
      <AlertHistoryModal initialTarget={s.name} onClose={() => setHistOpen(false)} onUnauthorized={onUnauthorized} />
    )}
    {enroll && (
      <InstallModal enroll={enroll} title={t('Команда установки')} onClose={() => { setEnroll(null); onChanged() }} />
    )}
    </>
  )
}
