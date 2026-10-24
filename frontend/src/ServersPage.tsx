import { localizeInstallCmd, withProxy } from './installProxy'
import { InstallPath, type InstallMode } from './InstallPath'
import { Fragment, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
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
  installStatus,
  installVia,
  type InstallStatus,
  rotateServerToken,
  setRelay,
  type RelayView,
  serverMetrics,
  serverOom,
  serverOomEvents,
  serverWebErrors,
  serverWebErrorLines,
  snoozeServer,
  snoozeServerAlert,
  scanExposure,
  ackChanges,
  updateServer,
  muteServerKeys,
  type AccessAccount,
  type ClockInfo,
  type DnsInfo,
  type ExposedPort,
  type MemLimitItem,
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
  type OomEpisode,
  type OomEvent,
  type OomSummary,
  type ProcStat,
  type WebErrorRow,
  type CpuGroup,
  type OsFixStep,
  type Server,
  type RealIpTrust,
  type ServerEnroll,
  type GpuSum,
  type ServerMetric,
  type ServerReport,
  type ServerVulns,
  type TimelineResult,
  cmdState,
  cmdWaitText,
} from './api'
import { StackedAreaChart, type ChartMark, type Series } from './charts/StackedAreaChart'
import { OnboardBlock } from './OnboardBlock'
import { diskHealth, diskUsage, fmtSetupVersion, limText, memLimFacts, gpuSecLine, shortList, sinceShort, unitsBlock, srvIpHit, srvIssues, srvMuted, tallyServers, type SrvIssue, webLogLabel, webRate, webSeriesName, webUnparsed } from './serverUtils'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { currentLang, useI18n } from './i18n'
import { useEscape } from './useEscape'
import { toastError } from './toast'
import { useUrlCard, useUrlParam } from './deeplink'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { fmtBytes, fmtDur, rateUnits, timeUnits } from './units'
import {
  MuteChip,
  SNOOZE_KINDS,
  UNIT_MUTE,
  WEB_MUTE,
  EXPOSED_MUTE,
  CFIP_MUTE,
  MEMLIM_MUTE,
  NOBOOT_MUTE,
  ACCESS_MUTE,
  osDecisionLabel,
  muteAutoNote,
  muteKindLabel,
  OOM_MUTE,
  oomCulpritLabel,
  SRV_ALERT_KINDS,
  collectMutes,
  isDecision,
  muteBrief,
  muteRows,
  mutesTileSub,
  muteText,
  type MuteItem,
} from './mutes'
import { MutesPanel } from './MutesPanel'
import { grpMuteTip } from './homeGroups'
import { useAuth } from './auth'
import { AlertHistoryModal } from './AlertHistoryModal'
import type { Section } from './App'
import { NoteBar } from './NoteBar'
import { ServerDeps, useDependentChecks } from './ParentBar'
import { TimelineCard, TL_ICON } from './TimelineCard'
import { PackagesCard } from './PackagesCard'
import { VulnsCard } from './VulnsCard'
import { JournalCard, JournalModal, journalBlocker } from './UnitJournal'
import { LockedLogs } from './LogsLocked'
import { LinksCard, NetTile, TrafficCard, type Tone } from './NetLinks'
import { DiskWritersBlock, NetUsersCard } from './Usage'
import { SoftwareCard } from './Software'
import { type FixStep, FixSteps, useFixDone } from './FixSteps'
import { GpuCard, GpuChip } from './Gpu'
import { RoleChips } from './RoleChips'
import { KnownServerList, knownItems, knownTitle } from './KnownDialog'
import { AgentUpdRow, agentBehind, helperCmd, hostOf, useFleetRollout } from './FleetUpdate'
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
// публичные резолверы рядом с резолверами сети: systemd-resolved берет первый ответ
const DNS_PUBLIC = "sudo mkdir -p /etc/systemd/resolved.conf.d && printf '[Resolve]\\nDNS=1.1.1.1 8.8.8.8\\n' | sudo tee /etc/systemd/resolved.conf.d/public-dns.conf && sudo systemctl restart systemd-resolved"

function ClockFix({ server: s, clock, skew, canManage }: {
  server: Server; clock: ClockInfo; skew?: number; canManage: boolean
}) {
  const { t } = useI18n()
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
      const ok = cmdState(last) === 'ok'
      setMsg({ ok, text: cmdState(last) === 'wait' ? cmdWaitText() : last.result || (ok ? t('готово') : t('не удалось')) })
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
  const off = Math.abs(skew ?? 0)
  return (
    <div className="clock-fix">
      <div className="form-error small">
        🕐 {off >= 5
          ? t('Часы разошлись с панелью на {n}, синхронизируйте время', {
            n: off < 120
              ? `${off} ${timeUnits()[3]}`
              : `${Math.round(off / 60)} ${currentLang() === 'en' ? 'min' : 'мин'}`,
          })
          : t('Время не синхронизировано, включите NTP')}
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
        <FixSteps
          steps={[{ id: 'ntp', cmd: main }, { id: 'port', alt: true }, { id: 'http', alt: true, cmd: httpFix }]}
          text={(st) => st.id === 'ntp'
            ? (clock.service ? t('Включить синхронизацию') : t('Включить systemd-timesyncd: демона синхронизации на сервере нет'))
            : st.id === 'port' ? t('до NTP не достучаться: открыть исходящий UDP 123 (sudo ufw allow out 123/udp)')
            : t('разово выставить часы по времени панели, она доступна по HTTPS')}
          foot={hasHelper ? undefined : t('Кнопка "в один клик" появится после установки timesync-хелпера (ansible kervax_helpers.yml).')} />
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
  swap: 'mem', memwb: 'mem', oom: 'mem', memleak: 'mem', memlimit: 'mem',
  netifrx: 'net', netiftx: 'net', neterr: 'net', nethealth: 'net', links: 'net', traffic: 'net', netusers: 'net',
  conntrack: 'net', sockets: 'net',
  diskio: 'disk', diskiops: 'disk', diskutil: 'disk', disklat: 'disk', disktemp: 'disk', diskfill: 'disk',
  diskhealth: 'disk', du: 'disk', diskwrite: 'disk',
  exposure: 'ports', conn: 'ports', ssh: 'access', osupd: 'updates', software: 'updates',
  unitloop: 'units', noboot: 'units', pressure: 'proc', web5xx: 'web',
  clock: 'overview', dns: 'overview', known: 'alerts',
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
  for (const a of s.os_advice ?? []) if (!a.muted) add('updates', a.level, a.text)
  for (const a of s.sw_advice ?? []) add('updates', a.level, a.text)
  const nb = s.noboot_advice?.units ?? []
  if (nb.length) add('units', 1, t('после перезагрузки не запустятся {list}: работают, но не включены', { list: shortList(nb, t) }))
  // как в карточке SSH: при проблеме совет не повторяем
  if (!issues.some((i) => i.kind === 'ssh_risk')) for (const a of s.ssh_advice ?? []) if (!a.muted) add('access', a.level, a.text)
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
  const known = knownItems(s, t)
  ok.alerts = [
    c.mutes.length ? t('заглушено: {list}', { list: muteBrief(c.mutes, t) }) : t('ничего не заглушено'),
    known.length ? t('известные проблемы: {list}', { list: shortList(known.map((x) => x.label), t, 3) }) : '',
  ].filter(Boolean).join(', ')

  const out: Record<string, SecInfo> = {}
  for (const { id } of DETAIL_SECTIONS) out[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // справка и заглушки - желтым, но не проблемой: на карте видно, что там не пусто
  if (v?.release && v.feed_ts && v.summary.pkgs)
    out.vulns = { level: 1, n: v.summary.cves, line: t('важных CVE: {n}, пакетов: {p}', { n: v.summary.cves, p: v.summary.pkgs }) }
  if (c.mutes.length && !out.alerts.n) out.alerts = { level: 1, n: c.mutes.length, line: ok.alerts }
  // заглушенная беда (упавшая служба, порт) - серым с пометкой, а не "упавших служб нет"
  markMuted(out, srvMuted(s, t).filter((i) => !i.section && i.kind !== 'offline')
    .map((i) => ({ sec: secOfLink(i.sec) ?? 'overview', text: i.text })), t)
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
  // агент за прокси: адрес соединения - это прокси, внешний IP тогда задан руками
  const proxy = s.last_report?.proxy
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
          {proxy && (
            <span className="srv-addr-part" title={t('агент ходит в панель через прокси')}>
              ↪ {proxy}
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

// Чей процесс: служба, контейнер, под, сессия пользователя (owner_kind из app/oom.py)
function oomOwner(k: { owner: string; owner_kind: string; owner_unknown?: boolean }, t: TFn): string {
  const n = k.owner_unknown ? `${k.owner} (${t('имя неизвестно')})` : k.owner
  if (k.owner_kind === 'container') return t('контейнер {n}', { n })
  if (k.owner_kind === 'pod') return t('под {n}', { n })
  if (k.owner_kind === 'user') return t('сессия {n}', { n: k.owner })
  if (k.owner_kind === 'host') return t('весь сервер')
  return k.owner
}

// Кого убили одной строкой: крупное с размером, по владельцу
function oomKillsLine(e: OomEpisode, t: TFn): string {
  const by = new Map<string, { names: string[]; anon: number; who: string; kind: string }>()
  for (const k of e.kills) {
    const key = `${k.owner_kind}:${k.owner}`
    const g = by.get(key) ?? { names: [], anon: 0, who: oomOwner(k, t), kind: k.owner_kind }
    const nm = k.name.replace(/^\((.*)\)$/, '$1') || (k.kind === 'oomd' ? 'cgroup' : '?')
    if (!g.names.includes(nm)) g.names.push(nm)
    g.anon += k.anon ?? 0
    by.set(key, g)
  }
  return [...by.values()]
    .sort((a, b) => b.anon - a.anon)
    .map((g) => {
      const size = g.kind !== 'user' && g.anon >= 10240 ? ` ${fmtBytes(g.anon * 1024)}` : ''
      return `${g.names.join(' + ')}${size}${g.who ? ` (${g.who})` : ''}`
    })
    .join(', ')
}

const OOM_KIND: Record<string, string> = { global: 'весь сервер', memcg: 'лимит cgroup', oomd: 'systemd-oomd' }

// Не алертить об OOM, пока не починят: по виновнику (кто занимал память, OOM из-за других служб
// приходят) или любой OOM сервера, на час, день, неделю или постоянно. Заглушенное - чипами со сроком
function OomMute({ server: s, culprit, onChanged }: { server: Server; culprit: string; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState(false)
  const [scope, setScope] = useState<'culprit' | 'all'>('culprit')
  const [busy, setBusy] = useState(false)
  const now = Date.now()
  const isOom = (k: string) => k === 'oom' || k.startsWith(OOM_MUTE)
  const perm = (s.alert_mutes ?? []).filter(isOom)
  const active: { key: string; until: string | null }[] = [
    ...perm.map((key) => ({ key, until: null })),
    ...Object.entries(s.alert_snoozes ?? {})
      .filter(([k, u]) => isOom(k) && new Date(u).getTime() > now && !perm.includes(k))
      .map(([key, until]) => ({ key, until })),
  ]
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const label = (k: string) =>
    k === 'oom' ? t('любой OOM') : t('OOM из-за {n}', { n: oomCulpritLabel(k.slice(OOM_MUTE.length), t, true) })
  const mute = async (kind: string, hours: number) => {
    setBusy(true)
    try {
      if (hours < 0) {
        await muteServerKeys(s.id, [kind])
        if (s.alert_snoozes?.[kind]) await snoozeServerAlert(s.id, kind, 0)
      } else if (hours === 0) {
        if ((s.alert_mutes ?? []).includes(kind))
          await muteServerKeys(s.id, [], [kind])
        if (s.alert_snoozes?.[kind]) await snoozeServerAlert(s.id, kind, 0)
      } else await snoozeServerAlert(s.id, kind, hours)
      setPicking(false)
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  const ck = culprit ? OOM_MUTE + culprit : ''
  // все уже заглушено: любой OOM или виновник последнего (другой виновник - снова кнопка)
  const free = !active.some((a) => a.key === 'oom' || a.key === ck)
  const kind = scope === 'all' || !ck ? 'oom' : ck
  if (!active.length && isViewer) return null
  return (
    <div className="oom-mute small">
      {active.map((a) => (
        <span key={a.key} className="snooze-active-chip snooze-perm">
          🔇 {label(a.key)}: {a.until ? t('не алертит {t}', { t: t('до {t}', { t: fmt(a.until) }) }) : t('не алертит, постоянно')}
          {!isViewer && (
            <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(a.key, 0)}>
              ✕
            </button>
          )}
        </span>
      ))}
      {free && !isViewer && (picking ? (
        <span className="oom-mute-pick">
          <span className="muted">🔕 {t('не алертить:')}</span>
          {ck && (
            <span className="oom-mute-scope">
              <button className={`ghost small${scope === 'culprit' ? ' active' : ''}`} onClick={() => setScope('culprit')}
                title={t('OOM из-за других служб будут приходить')}>
                {t('из-за {n}', { n: oomCulpritLabel(culprit, t, true) })}
              </button>
              <button className={`ghost small${scope === 'all' ? ' active' : ''}`} onClick={() => setScope('all')}>
                {t('любой OOM')}
              </button>
            </span>
          )}
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
          <button className="ghost small" disabled={busy} onClick={() => setPicking(false)}>
            {t('Отмена')}
          </button>
        </span>
      ) : (
        <button className="ghost small" onClick={() => setPicking(true)}>
          🔕 {ck ? t('не алертить об OOM из-за {n}', { n: oomCulpritLabel(culprit, t, true) }) : t('не алертить об OOM')}
        </button>
      ))}
    </div>
  )
}

// Нехватка памяти (OOM) за 30 дней - эпизоды из журнала ядра (helper units-setup 0.5): когда,
// что за OOM и кого убили; по клику кто занимал память, Mem-Info и задетые юниты. Без helper'а -
// прежний список "кого убило" по счетчику агента
function OomBlock({
  server: s,
  eps,
  open,
  onOpen,
  onChanged,
}: {
  server: Server
  eps: OomEpisode[] | null
  open: string | null
  onOpen: (id: string | null) => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [legacy, setLegacy] = useState<OomEvent[] | null>(null)
  const none = eps != null && eps.length === 0
  useEffect(() => {
    if (!none) return
    let alive = true
    serverOomEvents(s.id, 50)
      .then((e) => alive && setLegacy(e))
      .catch(() => alive && setLegacy([]))
    return () => {
      alive = false
    }
  }, [s.id, none])
  useEffect(() => {
    if (open) document.getElementById(`oom-ep-${open}`)?.scrollIntoView({ block: 'center' })
  }, [open, eps])
  if (eps == null || (!eps.length && !legacy?.length)) return null
  const when = (ts: number) =>
    new Date(ts * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const kb = (v?: number | null) => (v == null ? '—' : fmtBytes(v * 1024))
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-oom">
      <div className="net-head">
        <div className="chart-cap">{t('Нехватка памяти (OOM)')}</div>
        {eps.length > 0 && <span className="muted small">{t('за 30 дней: {n}', { n: eps.length })}</span>}
      </div>
      {eps.length > 0 && <OomMute server={s} culprit={s.oom?.culprit || eps[0].culprit || ''} onChanged={onChanged} />}
      {!eps.length && legacy && (
        <>
          <div className="muted small du-note">
            {t('Кто занимал память и какие службы задело, видно с helper units-setup 0.5. Пока только счетчик агента:')}
          </div>
          <div className="oom-log-list">
            {legacy.map((e, i) => (
              <div key={i} className="oom-log-row">
                <span className="oom-log-victim mono">{e.victim || t('процесс неизвестен')}</span>
                <span className="oom-log-when muted small mono">
                  {when(new Date(e.ts).getTime() / 1000)}
                  {e.count > 1 ? ` · x${e.count}` : ''}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {eps.map((e) => {
        const isOpen = open === e.id
        const sw = e.mem.swap_total
        return (
          <div key={e.id} className={`oom-ep${e.muted ? ' oom-ep-muted' : ''}`} id={`oom-ep-${e.id}`}>
            <button className="oom-ep-head" onClick={() => onOpen(isOpen ? null : e.id)} aria-expanded={isOpen}>
              <span className={`lk-dot lk-dot-${e.muted ? 'idle' : e.level >= 2 ? 'bad' : 'warn'}`} />
              <span className="mono small oom-ep-when">{when(e.start)}</span>
              <span className="muted small oom-ep-kind">{t(OOM_KIND[e.kind] ?? e.kind)}</span>
              <span className="oom-ep-text">
                {oomKillsLine(e, t)}
                {e.muted && <span className="muted small"> · 🔇 {t('не алертит')}</span>}
              </span>
            </button>
            {isOpen && (
              <div className="oom-ep-body">
                <div>
                  <div className="oom-ep-sub">{t('Убиты')}</div>
                  <table className="oom-tbl">
                    <thead>
                      <tr>
                        <th>{t('Процесс')}</th>
                        <th>pid</th>
                        <th>{t('Чей')}</th>
                        <th className="num">{t('Память')}</th>
                        <th className="num">oom_score_adj</th>
                      </tr>
                    </thead>
                    <tbody>
                      {e.kills.map((k, i) => (
                        <tr key={i}>
                          <td className="mono">{k.name || (k.kind === 'oomd' ? k.memcg : '?')}</td>
                          <td className="mono">{k.pid ?? '—'}</td>
                          <td>
                            {oomOwner(k, t)}
                            {k.owner_kind !== 'user' && (k.user || k.uid != null) ? (
                              <span className="muted"> · {k.user || `uid ${k.uid}`}</span>
                            ) : null}
                          </td>
                          <td className="num mono">{kb(k.anon)}</td>
                          <td className="num mono">{k.adj ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {e.top.length > 0 && (
                  <div>
                    <div className="oom-ep-sub">{t('Больше всего памяти перед убийством')}</div>
                    <table className="oom-tbl">
                      <tbody>
                        {e.top.map((x, i) => (
                          <tr key={i}>
                            <td className="mono">
                              {x.name}
                              {x.n > 1 ? ` x${x.n}` : ''}
                            </td>
                            <td className="muted">{x.user || (x.uid != null ? `uid ${x.uid}` : '')}</td>
                            <td className="num mono">{kb(x.anon)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="oom-mem">
                  {e.kind === 'memcg' && e.mem.limit != null && <span>{t('лимит cgroup {v}', { v: kb(e.mem.limit) })}</span>}
                  {e.mem.ram != null && <span>{t('RAM {v}', { v: kb(e.mem.ram) })}</span>}
                  {e.mem.anon != null && <span>{t('занято процессами {v}', { v: kb(e.mem.anon) })}</span>}
                  {e.mem.file != null && <span>{t('кэш файлов {v}', { v: kb(e.mem.file) })}</span>}
                  {e.mem.shmem != null && e.mem.shmem > 0 && <span>{t('общая память {v}', { v: kb(e.mem.shmem) })}</span>}
                  {sw === 0 && <span>{t('swap нет')}</span>}
                  {sw != null && sw > 0 && (
                    <span>{t('swap занят {u} из {m}', { u: kb(sw - (e.mem.swap_free ?? 0)), m: kb(sw) })}</span>
                  )}
                </div>
                {Object.keys(e.units).length > 0 && (
                  <div className="small">
                    <span className="muted">{t('Задело юниты:')} </span>
                    {Object.entries(e.units)
                      .map(([u, what]) => `${u} (${what.includes('failed') ? t('упал') : t('убит процесс')})`)
                      .join(', ')}
                  </div>
                )}
                {e.by.length > 0 && (
                  <div className="muted small">
                    {t('Память не досталась (это не виновники): {list}', { list: e.by.join(', ') })}
                  </div>
                )}
                {e.culprit && (
                  <div className="small">
                    <span className="muted">{t('Память занимал:')} </span>
                    {oomCulpritLabel(e.culprit, t)}
                  </div>
                )}
                <pre className="unit-log mono">{e.kills.map((k) => k.line).filter(Boolean).join('\n')}</pre>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Почему nginx отвечал 502/504 (helper 0.26): вид отказа upstream -> текст после его имени.
// Те же тексты, что в алерте (backend/app/web_upstream.py)
const UP_KIND: Record<string, string> = {
  refused: 'не принимает соединения',
  busy: 'не успевает принимать соединения: очередь сокета полна',
  nofile: 'не запущен: сокета нет',
  perm: 'не пускает: нет прав на сокет',
  ctimeout: 'не принял соединение вовремя',
  timeout: 'не ответил вовремя',
  nolive: 'без живых серверов',
  unreach: 'недоступен по сети',
  closed: 'закрыл соединение, не ответив',
  reset: 'сбросил соединение',
  bighdr: 'прислал заголовок больше буфера',
  resolve: 'не резолвится',
  ssl: 'не прошел TLS',
  bad: 'ответил не по протоколу',
  other: 'ошибка связи',
}
// имя upstream: контейнер с портом вместо его адреса, если имя известно
function upWho(x: { u: string; c?: string }): string {
  if (!x.c) return x.u
  if (/^\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(x.u)) return x.c + (x.u.includes(':') ? x.u.slice(x.u.indexOf(':')) : '')
  return `${x.c} (${x.u})`
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
        await muteServerKeys(s.id, [kind])
      } else if (hours === 0) {
        if ((s.alert_mutes ?? []).includes(kind))
          await muteServerKeys(s.id, [], [kind])
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
            {(r.ups?.length ?? 0) > 0 && (
              <div className="web-err-line small">
                <span className="muted">{t('почему')}: </span>
                <span className="mono">
                  {(r.ups ?? []).slice(0, 4).map((x) => `${upWho(x)} ${t(UP_KIND[x.k] ?? UP_KIND.other)} (${x.n})`).join(', ')}
                </span>
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
// является началом пути; дети по убыванию размера. Полоска узкая и стоит рядом с размером,
// цвет - по смыслу каталога (docker, кубер, журнал, логи, базы, временные, система).
// Число файлов коротко: 4.1 млн, 700 тыс.
function fmtFiles(n: number, t: TFn): string {
  if (n >= 1e6) return t('{n} млн', { n: (n / 1e6).toFixed(1) })
  if (n >= 1e3) return t('{n} тыс.', { n: Math.round(n / 1e3) })
  return String(n)
}

const DU_COLORS: [RegExp, string][] = [
  [/^\/var\/lib\/(docker|containerd)(\/|$)/, '#4b74ff'],
  [/^\/var\/lib\/(k0s|kubelet|rancher|k3s)(\/|$)/, '#8a7fb8'],
  [/^\/var\/log\/journal(\/|$)/, '#cf9b52'],
  [/^\/var\/log(\/|$)/, '#e0b85a'],
  [/^\/var\/lib\/(postgresql|mysql|clickhouse|redis|mongodb)(\/|$)/, '#57a894'],
  [/^\/(var\/)?tmp(\/|$)/, '#c77b95'],
  [/^\/(usr|boot|lib|snap|var\/cache)(\/|$)/, '#6b7489'],
]
const duColor = (p: string) => DU_COLORS.find(([re]) => re.test(p))?.[1]

function DuTree({ fs, t, files = false }: { fs: DiskUsageFs; t: TFn; files?: boolean }) {
  const [all, setAll] = useState(false)
  const state = files ? fs.itop_state : fs.top_state
  if (files && state === 'failed')
    return <div className="muted small">{t('Посчитать файлы не вышло: du на ноде не знает --inodes.')}</div>
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
  const LIMIT = 8
  const shown = all ? rows : rows.slice(0, LIMIT)
  const base = (files ? fs.iused : fs.used) || 1
  return (
    <div className="dk-tree">
      {shown.map(({ d, depth, parent }) => (
        <div key={d.path} className="dk-tree-row" title={d.path}>
          <span className="dk-tree-path mono" style={{ paddingLeft: `${depth * 12}px` }}>
            {parent ? d.path.slice(parent.replace(/\/$/, '').length) : d.path}
          </span>
          <span className="dk-tree-bar">
            <span style={{ width: `${Math.min(100, (d.bytes / base) * 100)}%`, background: files ? undefined : duColor(d.path) }} />
          </span>
          <span className="dk-tree-size mono">{files ? fmtFiles(d.bytes, t) : fmtBytes(d.bytes)}</span>
        </div>
      ))}
      {rows.length > LIMIT && (
        <button className="dk-link dk-more" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : t('показать все ({n})', { n: rows.length })}
        </button>
      )}
      {state === 'partial' && <div className="muted small">{t('Обход не успел целиком, показано то, что посчитано.')}</div>}
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
  // разбор приехал со свежим отчетом - "Разбор запущен" больше не нужен: помним, при каком
  // отчете разбора его запустили
  const duTs = diskUsage(s.last_report)?.ts
  const [sentAt, setSentAt] = useState<number | undefined>(undefined)
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
        setSentAt(duTs)
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
  if (st === 'sent' && sentAt === duTs) {
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

// Место на дисках: один блок вместо плиток "Диски" и отдельного "На что ушло место". На раздел одна
// строка: процент как у алерта (занято/всего), свободно для обычных процессов и резерв root отдельно -
// df считает резерв занятым и показывает больше процентов. Под разобранным разделом (helper
// diskusage-setup) слева дерево каталогов, справа находки строками; пояснение и команда - по клику
// на строку. Безопасные находки и лог контейнера освобождаются кнопкой: нода сначала считает, что
// удалится (preview), человек подтверждает, потом run. "Освободить безопасное" - все безопасные
// подряд по одному подтверждению, без preview каждой: они того же уровня, что у автоочистки.

// длинные хэши docker в командах коротко, полная команда - в подсказке и при копировании
const shortCmd = (c: string) => c.replace(/\b([0-9a-f]{12})[0-9a-f]{52}\b/g, '$1...')

// короткое название для строки находки, путь или уточнение - серым рядом
function duShort(it: DiskUsageItem, t: TFn): { name: string; sub: string } {
  switch (it.id) {
    case 'big-log': return { name: it.path.split('/').pop() || it.path, sub: it.path }
    case 'container-log': return { name: t('лог контейнера {n}', { n: it.name ?? '?' }), sub: 'docker' }
    case 'docker-images': return { name: t('образы без контейнеров'), sub: 'docker' }
    case 'docker-dangling': return { name: t('образы без тега'), sub: 'docker' }
    case 'docker-volumes': return { name: t('тома без контейнеров'), sub: it.count ? t('docker, {n} шт.', { n: it.count }) : 'docker' }
    case 'docker-build-cache': return { name: t('кэш сборки'), sub: 'docker' }
    case 'docker-containers': return { name: t('остановленные контейнеры'), sub: 'docker' }
    case 'journal': return { name: t('журнал systemd'), sub: t('останется 200 МБ') }
    case 'rotated-logs': return { name: t('старые ротированные логи'), sub: t('{n} файлов, /var/log', { n: it.count ?? 0 }) }
    case 'apt-cache': return { name: t('кэш apt'), sub: '/var/cache/apt' }
    case 'dnf-cache': return { name: t('кэш dnf/yum'), sub: '' }
    case 'tmp-dir':
    case 'vartmp-dir': return { name: it.path, sub: it.fresh ? t('есть свежие файлы') : t('старых файлов: {n}', { n: it.count ?? 0 }) }
    case 'deleted-open':
      return { name: t('удаленные, но открытые файлы'), sub: (it.procs ?? []).map((p) => `${p.comm} ${p.pid}`).join(', ') }
    default: return { name: duLabel(it, t), sub: '' }
  }
}

const duSize = (it: DiskUsageItem) => it.free || it.bytes
const duKey = (it: DiskUsageItem) => `${it.mount}|${it.id}|${it.path}|${it.name ?? ''}`
const dkLevel = (p: number) => (p >= 90 ? 'bad' : p >= 75 ? 'warn' : 'ok')

function DkCopy({ text }: { text: string }) {
  const { t } = useI18n()
  const [ok, setOk] = useState(false)
  return (
    <button
      className="dk-link"
      onClick={() => {
        navigator.clipboard?.writeText(text)
        setOk(true)
        window.setTimeout(() => setOk(false), 1500)
      }}
    >
      {ok ? t('скопировано') : t('копировать')}
    </button>
  )
}

function DkCmd({ cmd }: { cmd: string }) {
  return (
    <div className="dk-cmd">
      <code title={cmd}>{shortCmd(cmd)}</code>
      <DkCopy text={cmd} />
    </div>
  )
}

type DkBulk =
  | { phase: 'confirm' }
  | { phase: 'run'; i: number; n: number; name: string; freed: number; errs: string[] }
  | { phase: 'done'; freed: number; errs: string[] }

function DiskSpaceCard({ server: s, r, onChanged }: { server: Server; r: ServerReport; onChanged: () => void }) {
  const { t } = useI18n()
  const { isAdmin, isViewer } = useAuth()
  const [fix, setFix] = useState<Record<string, DuFixState>>({})
  const [bulk, setBulk] = useState<Record<string, DkBulk>>({})
  const [open, setOpen] = useState<string | null>(null)
  const [more, setMore] = useState<Record<string, boolean>>({})
  const [cmds, setCmds] = useState<string | null>(null)
  const disks = r.disks ?? []
  if (!disks.length) return null
  const du = diskUsage(r)
  const fcAll = diskForecast(s, r)
  const fcFresh = !!s.disk_forecast?.ts && !!r.clock_unix && r.clock_unix - s.disk_forecast.ts <= 7200
  const age = du ? Math.max(0, Math.round(((r.clock_unix || Date.now() / 1000) - du.ts) / 60)) : null
  const autoThr = s.disk_warn_percent || s.disk_alert_percent || s.disk_crit_percent || 85
  const allow = new Set(du?.fix?.allow ?? [])
  const agentOk = verAtLeast(s.agent_version, DU_FIX_AGENT)
  const canFix = (it: DiskUsageItem) =>
    isAdmin && agentOk && DU_FIXABLE.has(it.id) && allow.has(it.id) && (it.id !== 'container-log' || !!it.name)
  // почему кнопок нет, хотя находки для них есть
  const fixNote = !(isAdmin && du?.items.some((i) => DU_FIXABLE.has(i.id)))
    ? ''
    : !du?.fix
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
  const setB = (mount: string, st: DkBulk | null) =>
    setBulk((cur) => {
      const next = { ...cur }
      if (st) next[mount] = st
      else delete next[mount]
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
    return m.includes('is not allowed on this node') ? t('На этой ноде действие выключено: его нет в /etc/kervax/fix.conf.') : m
  }
  const preview = async (key: string, it: DiskUsageItem) => {
    setOpen(key)
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
  const runAll = async (mount: string, list: DiskUsageItem[]) => {
    let freed = 0
    const errs: string[] = []
    for (let i = 0; i < list.length; i++) {
      const it = list[i]
      setB(mount, { phase: 'run', i: i + 1, n: list.length, name: duShort(it, t).name, freed, errs })
      try {
        freed += (await command(it, 'run', 16 * 60_000)).bytes
      } catch (e) {
        errs.push(`${duShort(it, t).name}: ${fixError(e)}`)
      }
    }
    setB(mount, { phase: 'done', freed, errs })
    window.setTimeout(onChanged, 20_000)
  }
  // освобождено по разделу: после очистки раздел уходит ниже 75%, helper убирает разбор, и без
  // итога блок пропадал вместе с "Освобождено" - казалось, что все сломалось
  const freedOn = (mount: string) =>
    Object.entries(fix).reduce((a, [k, x]) => a + (k.startsWith(mount + '|') && x.phase === 'done' ? x.data.bytes : 0), 0) +
    (bulk[mount]?.phase === 'done' ? (bulk[mount] as { freed: number }).freed : 0)
  const devs = r.disk_devs ?? []
  return (
    <div className="chart-card net-card net-card-wide dk-card" id="mcard-diskfill">
      <div className="net-head">
        <div className="chart-cap">{t('Место на дисках')}</div>
        <span className="dk-head-r muted small">
          {age != null && <span>{t('замер {n} мин назад', { n: age })}</span>}
          {du && s.disk_autofix ? (
            <span title={t('Автоочистка включена: от {p}% панель сама освобождает безопасное.', { p: autoThr })}>
              {t('автоочистка от {p}%', { p: autoThr })}
            </span>
          ) : (
            du && isAdmin && <span title={t('включается в настройках сервера')}>{t('автоочистка выкл')}</span>
          )}
        </span>
      </div>
      {fixNote && <div className="muted small">{fixNote}</div>}
      {disks.map((d) => {
        const p = d.total ? Math.round((d.used / d.total) * 100) : 0
        const avail = d.avail ?? Math.max(0, d.total - d.used)
        // резерв root (ext4 ~5%): обычным процессам недоступен, df считает его занятым
        const reserve = Math.max(0, d.total - d.used - avail)
        const ip = d.inodes ? Math.round(((d.inodes_used ?? 0) / d.inodes) * 100) : null
        const fc = fcAll.filter((i) => i.mount === d.mount)
        const fsp = fc.find((i) => i.kind === 'space')
        const fino = fc.find((i) => i.kind === 'inode')
        const fs = du?.fs.find((f) => f.mount === d.mount) ?? null
        const items = (du?.items ?? []).filter((i) => i.mount === d.mount).sort((a, b) => duSize(b) - duSize(a))
        const safeItems = items.filter((i) => i.level === 'safe')
        const safe = safeItems.reduce((a, i) => a + i.free, 0)
        const bulkList = safeItems.filter(canFix)
        const lv = dkLevel(Math.max(p, ip ?? 0))
        const tone = (h: number) => (h <= 24 ? 'dk-t-bad' : h <= 5 * 24 ? 'dk-t-warn' : '')
        const rate = (x: number) => (x < 10 ? +x.toFixed(1) : Math.round(x))
        const freed = freedOn(d.mount)
        // раздел без разбора: заметная строка, если он уже заполнен или растет и есть что нажать или
        // что сказать (прогноз), иначе только кнопка в конце строки
        const loud = !fs && (fc.length > 0 || (p >= 50 && !!s.disk_analyze && !isViewer))
        const quietAnalyze = !fs && !loud && !!s.disk_analyze && !isViewer
        const tree = fs && fs.top_state !== 'skipped' && fs.top_state !== 'none' && fs.top.length > 0
        const b = bulk[d.mount]
        const showAll = more[d.mount]
        const LIMIT = 6
        const shown = showAll ? items : items.slice(0, LIMIT)
        const rest = items.slice(LIMIT)
        const safeCmds = safeItems.filter((i) => i.fix).map((i) => `sudo ${i.fix}`)
        return (
          <div key={d.mount} className="dk-part">
            <div className="dk-line">
              <span className={`lk-dot lk-dot-${lv}`} />
              <span className="dk-mount mono">{d.mount}</span>
              <span className={`dk-pct dk-t-${dkLevel(p)}`}>{p}%</span>
              <span className="dk-bar">
                <span className={`dk-fill-${dkLevel(p)}`} style={{ width: `${Math.min(100, p)}%` }} />
              </span>
              <span className="dk-nums">
                {t('{u} из {s}', { u: fmtBytes(d.used), s: fmtBytes(d.total) })}
                <span className="dk-sep">·</span>
                <span
                  title={
                    reserve > d.total * 0.01
                      ? t('еще {r} - резерв root, обычным процессам он недоступен; df поэтому показывает больше процентов', { r: fmtBytes(reserve) })
                      : undefined
                  }
                >
                  {t('свободно {a}', { a: fmtBytes(avail) })}
                  {reserve > d.total * 0.01 && <span className="muted"> {t('+{r} резерв', { r: fmtBytes(reserve) })}</span>}
                </span>
                {ip != null && ip >= 50 && (
                  <>
                    <span className="dk-sep">·</span>
                    <span className={ip >= 75 ? 'dk-t-warn' : ''}>inode {ip}%</span>
                  </>
                )}
                {quietAnalyze && (
                  <span className="dk-analyze-q">
                    <DiskAnalyze server={s} mount={d.mount} forecast={false} onChanged={onChanged} />
                  </span>
                )}
              </span>
              {(fsp || fino || fcFresh) && (
                <span className="dk-fc">
                  {fsp && (
                    <span className={tone(fsp.eta_h)}>
                      {t('заполнится {w}, +{r}%/сут', { w: etaText(fsp.eta_h, t), r: rate(fsp.rate) })}
                    </span>
                  )}
                  {fino && (
                    <span className={tone(fino.eta_h)}>
                      {fsp && ', '}
                      {t('inode кончатся {w}', { w: etaText(fino.eta_h, t) })}
                    </span>
                  )}
                  {!fsp && !fino && <span className="muted">{t('не растет')}</span>}
                </span>
              )}
            </div>
            {loud && (
              <div className="dk-na muted small">
                <span>{t('Не разбирался: разбор места включается сам от 75%.')}</span>
                <DiskAnalyze server={s} mount={d.mount} forecast={fc.length > 0} onChanged={onChanged} />
              </div>
            )}
            {!fs && freed > 0 && (
              <div className="dk-na small">
                <span className="t-up">{t('Освобождено {n}', { n: fmtBytes(freed) })}</span>
                <span className="muted">{t('Раздел больше не заполнен, разбор места скрыт: он появляется от 75%.')}</span>
              </div>
            )}
            {fs && (
              <div className={`dk-body${tree || (fs.ipct ?? 0) >= 75 ? '' : ' dk-body-one'}`}>
                {fs.top_state === 'skipped' && (
                  <div className="muted small dk-wide">
                    {t('Каталоги не считались: в разделе {n} млн файлов, полный обход слишком долгий. Ниже то, что нашлось по известным местам.', {
                      n: (fs.inodes / 1e6).toFixed(1),
                    })}
                  </div>
                )}
                {(tree || (fs.ipct ?? 0) >= 75) && (
                  <div className="dk-col">
                    {tree && (
                      <>
                        <div className="dk-sub">{t('Куда ушло')}</div>
                        <DuTree fs={fs} t={t} />
                      </>
                    )}
                    {(fs.ipct ?? 0) >= 75 && (
                      <>
                        <div className="dk-sub">
                          {t('Где больше всего файлов')}
                          <span className="muted">
                            {' '}
                            · {t('inode заняты на {p}%: {u} из {n}', { p: fs.ipct ?? 0, u: fmtFiles(fs.iused ?? 0, t), n: fmtFiles(fs.itotal ?? 0, t) })}
                          </span>
                        </div>
                        <DuTree fs={fs} t={t} files />
                      </>
                    )}
                  </div>
                )}
                <div className="dk-col">
                  <div className="dk-sub">
                    {t('Что можно освободить')}
                    {items.length > 0 && <span className="muted"> · {fmtBytes(items.reduce((a, i) => a + duSize(i), 0))}</span>}
                  </div>
                  {!items.length && (
                    <div className="muted small">
                      {t('Мусора по известным местам нет (логи, журнал, docker, временные, кэши). Место занимают данные выше.')}
                    </div>
                  )}
                  <div className="dk-finds">
                    {shown.map((it) => {
                      const key = duKey(it)
                      const l = duShort(it, t)
                      const st = fix[key]
                      const isOpen = open === key
                      const hint = duHint(it, t)
                      const toggle = () => setOpen(isOpen ? null : key)
                      return (
                        <div key={key} className={`dk-find${isOpen || st ? ' dk-find-open' : ''}`}>
                          <div className="dk-find-main" onClick={toggle}>
                            <span className={`dk-lv dk-lv-${it.level}`}>{duLevel(it.level, t)}</span>
                            <span className="dk-find-name" title={duLabel(it, t)}>
                              {l.name}
                              {l.sub && <span className="dk-find-sub muted"> {l.sub}</span>}
                            </span>
                            <span className="dk-find-size mono">{fmtBytes(duSize(it))}</span>
                            <span className="dk-find-act" onClick={(e) => e.stopPropagation()}>
                              {canFix(it) && !st ? (
                                <button className="dk-btn dk-btn-sm" onClick={() => preview(key, it)}>
                                  {it.id === 'container-log' ? t('Обнулить') : t('Освободить')}
                                </button>
                              ) : !st && (it.fix || hint) ? (
                                <button className="dk-link" onClick={toggle}>
                                  {isOpen ? t('скрыть') : it.fix ? t('как') : t('что это')}
                                </button>
                              ) : null}
                            </span>
                          </div>
                          {(isOpen || st) && (
                            <div className="dk-find-more">
                              {hint && <div className="muted small">{hint}</div>}
                              {st?.phase === 'preview' && <div className="muted small">{t('Нода считает, что удалится...')}</div>}
                              {st?.phase === 'confirm' && (
                                <div className="du-fix">
                                  <div>
                                    {st.data.bytes > 0 ? t('Освободится ~{n}', { n: fmtBytes(st.data.bytes) }) : t('Освобождать нечего.')}
                                    {st.data.count > 0 && <span className="muted"> · {t('файлов: {n}', { n: st.data.count })}</span>}
                                  </div>
                                  {st.data.sample.length > 0 && (
                                    <pre className="du-fix-sample mono">
                                      {st.data.sample.join('\n') + (st.data.count > st.data.sample.length ? '\n...' : '')}
                                    </pre>
                                  )}
                                  <div className="du-fix-actions">
                                    {st.data.bytes > 0 && <button onClick={() => run(key, it, st.data)}>{t('Выполнить')}</button>}
                                    <button className="ghost" onClick={() => set(key, null)}>{t('Отмена')}</button>
                                  </div>
                                </div>
                              )}
                              {st?.phase === 'run' && (
                                <div className="muted small">
                                  {it.id.startsWith('docker-') ? t('Выполняю, docker может чистить несколько минут...') : t('Выполняю...')}
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
                              {it.fix && (!st || st.phase === 'error') && <DkCmd cmd={`sudo ${it.fix}`} />}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                  {rest.length > 0 && (
                    <button className="dk-link dk-more" onClick={() => setMore({ ...more, [d.mount]: !showAll })}>
                      {showAll ? t('свернуть') : t('еще {n} на {s}', { n: rest.length, s: fmtBytes(rest.reduce((a, i) => a + duSize(i), 0)) })}
                    </button>
                  )}
                  {safe > 0 && (
                    <div className="dk-safe">
                      <div className="dk-safe-txt">
                        <b>{t('Без риска освободится {n}', { n: fmtBytes(safe) })}</b>
                        <span className="muted">
                          {' '}
                          · {t('{p}% станет {a}%', { p, a: Math.round(((d.used - safe) / (d.total || 1)) * 100) })} ·{' '}
                          {safeItems.map((i) => duShort(i, t).name).join(', ')}
                        </span>
                      </div>
                      <div className="dk-safe-act">
                        {bulkList.length > 0 && !b && (
                          <button className="dk-btn" onClick={() => setB(d.mount, { phase: 'confirm' })}>{t('Освободить безопасное')}</button>
                        )}
                        {safeCmds.length > 0 && (
                          <button className="dk-link" onClick={() => setCmds(cmds === d.mount ? null : d.mount)}>
                            {cmds === d.mount ? t('скрыть команды') : t('команды')}
                          </button>
                        )}
                      </div>
                      {b?.phase === 'confirm' && (
                        <div className="du-fix dk-wide">
                          <div>
                            {t('Освободится ~{n}', { n: fmtBytes(bulkList.reduce((a, i) => a + i.free, 0)) })}
                            <span className="muted">
                              {': '}
                              {bulkList.map((i) => `${duShort(i, t).name} ${fmtBytes(i.free)}`).join(', ')}
                            </span>
                          </div>
                          <div className="du-fix-actions">
                            <button onClick={() => runAll(d.mount, bulkList)}>{t('Выполнить')}</button>
                            <button className="ghost" onClick={() => setB(d.mount, null)}>{t('Отмена')}</button>
                          </div>
                        </div>
                      )}
                      {b?.phase === 'run' && (
                        <div className="muted small dk-wide">
                          {t('Выполняю {i} из {n}: {name}...', { i: b.i, n: b.n, name: b.name })}
                        </div>
                      )}
                      {b?.phase === 'done' && (
                        <div className="du-fix-line small dk-wide">
                          <span className="t-up">{t('Освобождено {n}', { n: fmtBytes(b.freed) })}</span>
                          <span className="muted">{t('цифры обновятся со следующим отчетом')}</span>
                          {b.errs.map((e) => (
                            <span key={e} className="form-error">{e}</span>
                          ))}
                          <button className="ghost small" onClick={() => setB(d.mount, null)}>{t('закрыть')}</button>
                        </div>
                      )}
                      {cmds === d.mount && (
                        <div className="dk-cmd dk-wide">
                          <code title={safeCmds.join('\n')}>{safeCmds.map(shortCmd).join('\n')}</code>
                          <DkCopy text={safeCmds.join('\n')} />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )
      })}
      {devs.length > 0 && (
        <div className="muted small">
          {t('Нагрузка дисков')}:{' '}
          {devs.map((x, i) => (
            <span key={x.dev} className={x.util >= 90 ? 'dk-t-warn' : ''}>
              {i > 0 && ', '}
              <span className="mono">{x.dev}</span> {Math.round(x.util)}% {t('ответ {v}', { v: fmtMs(x.await) })}
            </span>
          ))}
        </div>
      )}
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
// по нему видно, что это ровный рост, а не разовый всплеск. Уровень 0 - растет, но свободной памяти
// сервера хватит больше чем на неделю: серым, без алерта
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
        {t('Минимум памяти за сутки растет 4 суток подряд. Пила сборщика мусора и всплески минимум не двигают, а утечка поднимает его каждый день. Алерт - только если при таком росте свободной памяти сервера не хватит на неделю.')}
      </div>
      <div className="loc-results">
        {items.map((x) => {
          const [comm, user] = x.name.split('@')
          const bad = x.level >= 2
          return (
            <div key={x.key} className="loc-res ml-row">
              <span className={`lk-dot lk-dot-${bad ? 'bad' : x.level ? 'warn' : 'info'}`} />
              <div className="ml-main">
                <span className="acc-tag">{kindName[x.kind]}</span> <b>{x.kind === 'proc' ? `${comm} (${user})` : x.name}</b>
                <div className="muted small">{x.series.map((v) => fmtBytes(v)).join(', ')}</div>
              </div>
              <div className="ml-rate">
                <span className={bad ? 't-down' : x.level ? 't-degraded' : ''}>+{fmtBytes(x.rate)} {t('в сутки')}</span>
                {x.eta_days != null && (
                  <div className="muted small">
                    {x.eta_days < 30 ? t('памяти хватит на {d} сут', { d: x.eta_days }) : t('памяти хватит больше чем на месяц')}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// У своего лимита памяти (app/mem_limit.py): кто тормозит сейчас - давление памяти или major faults
// держатся 10 минут, кто у лимита без давления (кэш занял лимит - это нормально), что было за сутки.
// "так задумано" - memlim:<ключ>, как у белых списков
function MemLimitsCard({ server: s, isViewer, onChanged }: { server: Server; isViewer: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const ml = s.mem_limits
  if (!ml || (!ml.bad.length && !ml.near.length && !ml.past.length)) return null
  const kindName = { docker: t('контейнер'), pod: t('под'), unit: t('юнит') }
  const hm = (iso?: string) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')
  const advice = { docker: t('поднимите лимит памяти контейнера (mem_limit)'), pod: t('поднимите limit памяти пода'), unit: t('поднимите MemoryMax / MemoryHigh юнита') }
  const setDecided = async (key: string, on: boolean) => {
    if (on && !window.confirm(t('Так задумано: этот лимит памяти оставить как есть? Панель перестанет алертить по нему.'))) return
    setBusy(true)
    try {
      const k = MEMLIM_MUTE + key
      await muteServerKeys(s.id, on ? [k] : [], on ? [] : [k])
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  const row = (x: MemLimitItem, tone: 'bad' | 'warn' | 'info', sub: string) => (
    <div key={x.key + (x.from ?? '')} className="loc-res ml-row">
      <span className={`lk-dot lk-dot-${tone}`} />
      <div className="ml-main">
        <span className="acc-tag">{kindName[x.kind]}</span> <b>{x.name}</b>
        <div className="muted small">{sub}</div>
      </div>
      <div className="ml-rate">
        <span className={tone === 'bad' ? 't-down' : tone === 'warn' ? 't-degraded' : ''}>{limText(x.v.lim)}</span>
        {x.decided !== undefined && !isViewer && (
          <div>
            {x.decided ? (
              <span className="muted small">
                {t('так задумано')}{' '}
                <button className="ghost small row-act" disabled={busy} onClick={() => setDecided(x.key, false)}>
                  {t('вернуть алерт')}
                </button>
              </span>
            ) : (
              <button className="ghost small row-act" disabled={busy} onClick={() => setDecided(x.key, true)}>
                🔕 {t('так задумано')}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-memlimit">
      <div className="net-head">
        <div className="chart-cap">{t('У лимита памяти')}</div>
      </div>
      <div className="net-sub">
        {t('Контейнеры, поды и юниты, занявшие свой лимит памяти. Кэш заполняет любой лимит, это нормально. Беда - когда при этом растет давление памяти (PSI full) или major faults: ядро выкидывает код и данные, процесс читает их с диска снова и тормозит, но не падает. Алерт - если так держится 10 минут.')}
      </div>
      <div className="loc-results">
        {ml.bad.map((x) =>
          row(x, x.decided ? 'info' : (x.level ?? 1) >= 2 ? 'bad' : 'warn',
            `${memLimFacts(x.v, t)} - ${t('тормозит, но не падает')}${x.decided ? '' : `; ${advice[x.kind]}`}`),
        )}
        {ml.past.map((x) =>
          row(x, 'info', `${t('тормозил {from} - {to}', { from: hm(x.from), to: hm(x.to) })}: ${memLimFacts(x.v, t)}`),
        )}
        {ml.near.map((x) =>
          row(x, x.hot ? 'warn' : 'info', x.hot ? `${memLimFacts(x.v, t)} - ${t('давление есть, ждем 10 минут')}` : `${memLimFacts(x.v, t)} - ${t('давления нет, это кэш')}`),
        )}
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
function SrvTiles({ r, M, info, onGo, gpu, oom }: {
  r: ServerReport
  gpu?: GpuSum | null
  oom?: OomSummary | null
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
      {oom && oom.n7 > 0 && (
        <button key="oom" className="srv-tile-btn" onClick={() => onGo('mem')} title={t('к разделу')}>
          <NetTile label={t('OOM за 7 дней')} value={String(oom.n7)} sub={oom.last_text} tone="bad" />
        </button>
      )}
      {worst &&
        tile('disk', t('Диск'), `${dp}%`, `${worst.mount}${ds.length > 1 ? ` · ${ds.length} ${t('разд.')}` : ''}`, dp,
          <TileSpark vals={M.map((m) => m.disk_percent)} max={100} color="#cf9b52" />)}
      {tile('net', t('Сеть'), `↓ ${fmtRate(r.net_rx)}`,
        `↑ ${fmtRate(r.net_tx)}${(r.swap_total ?? 0) > 0 ? ` · swap ${fmtBytes(r.swap_used)}` : ''}`, null,
        <TileSpark vals={M.map((m) => m.net_rx)} color="#57a894" />)}
      {gpu &&
        tile('gpu', 'GPU', gpu.util != null ? `${gpu.util}%` : '—',
          gpu.mem != null ? `${gpu.n} x ${gpu.models.join(', ')} · ${gpu.memt != null ? `${gpu.mem} / ${gpu.memt}` : gpu.mem} ${t('ГБ')}` : `${gpu.n} x NVIDIA`, gpu.util,
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
            // желтым - только устойчивый (алерт панели), одиночный всплеск счетчика это норма
            tone={s.alert_since?.throttle ? 'warn' : r.cpu_throttle > 0 ? undefined : 'ok'}
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
        {s.oom != null && (
          <NetTile
            label={t('OOM за 7 дней')}
            value={String(s.oom.n7)}
            sub={s.oom.n7 > 0 && s.oom.last_at ? t('последний {d}', { d: new Date(s.oom.last_at * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }) : undefined}
            tone={s.oom.n7 > 0 ? 'bad' : 'ok'}
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

// Известные причины, у которых есть понятный выход: причина одной строкой, шаги FixSteps (номер,
// текст, команда), мелкой строкой внизу - почему перезапуск не поможет и почему не страшно.
// noRestart - кнопки "Перезапустить" у такого юнита нет. group - юниты с одной причиной (mdmonitor и
// mdmonitor-oneshot) идут одной карточкой с одной командой на всех.
// Набор - с парка 05.10.2026: mdmonitor на семи нодах, unbound-resolvconf на четырех и т.д.
type UnitStep = FixStep & { text: string }
type UnitHint = { what: string; steps: UnitStep[]; foot?: string; noRestart?: boolean; group?: string }
// плейбук сам смотрит, где есть что убирать, поэтому сразу на весь парк
const CLEANUP = 'ansible-playbook playbooks/units_cleanup.yml -l kervax'
function unitHint(u: FailedUnit, all: FailedUnit[], t: TFn): UnitHint | null {
  const log = u.log.join('\n')
  const n = u.unit
  const again = t('Перезапуск не поможет: юнит упадет снова.')
  const fleet = (text: string): UnitStep => ({ id: 'fleet', alt: true, text, cmd: CLEANUP })
  // у давно упавшего mdmonitor.service лог уже ротирован, причину видно у mdmonitor-oneshot
  const mdNoMail = all.some((x) => x.unit.startsWith('mdmonitor') && x.log.join('\n').includes('No mail address'))
  if (n.startsWith('mdmonitor') && (log.includes('No mail address') || (!log && mdNoMail))) {
    // как в units_cleanup.yml: таймер oneshot выключить, mdmonitor.service замаскировать (его
    // запускает еще и правило udev), отметку сбросить у тех, что упали. Таймер трогаем, только
    // если oneshot есть на ноде: на RHEL его нет, и disable оборвал бы всю цепочку
    const md = all.filter((x) => x.unit.startsWith('mdmonitor')).map((x) => x.unit)
    const cmd = [
      ...(md.includes('mdmonitor-oneshot.service') ? ['sudo systemctl disable --now mdmonitor-oneshot.timer'] : []),
      'sudo systemctl mask --now mdmonitor.service',
      `sudo systemctl reset-failed ${md.join(' ')}`,
    ].join(' && ')
    return {
      what: t('mdadm некуда слать письма (в mdadm.conf нет MAILADDR): за RAID он не следит и падает при каждом запуске'),
      steps: [
        { id: 'off', text: t('Выключить мониторинг mdadm на этом сервере'), cmd },
        fleet(t('по всему парку: плейбук не тронет ноды, где MAILADDR задан')),
      ],
      foot: `${again} ${t('За RAID следит Kervax, раздел Диск.')}`,
      noRestart: true,
      group: 'mdmonitor',
    }
  }
  if (n === 'unbound-resolvconf.service')
    return {
      what: t('unbound пытается прописаться в systemd-resolved, которого на сервере нет'),
      steps: [
        { id: 'off', text: t('Выключить юнит'), cmd: 'sudo systemctl mask --now unbound-resolvconf.service && sudo systemctl reset-failed unbound-resolvconf.service' },
        fleet(t('по всему парку')),
      ],
      foot: `${again} ${t('Сам unbound работает, этот юнит ему не нужен.')}`,
      noRestart: true,
    }
  if (n.startsWith('systemd-networkd-wait-online'))
    return {
      what: t('При загрузке не дождались сети: обычно мешает интерфейс, который не нужен или не под networkd'),
      steps: [
        { id: 'reset', text: t('Нажать "Сбросить": сервер работает, юнит нужен только при загрузке') },
        { id: 'iface', alt: true, text: t('если повторяется на каждой загрузке: ждать только нужный интерфейс (--interface=) или выключить юнит') },
      ],
      noRestart: true,
    }
  if (n.endsWith('.socket') && u.result === 'resources')
    return {
      what: t('Сокет не смог занять порт: его уже держит сам сервис (например, sshd со своим портом)'),
      steps: [{ id: 'off', text: t('Выключить лишний сокет'), cmd: `sudo systemctl disable --now ${n} && sudo systemctl reset-failed ${n}` }],
      foot: t('Перезапуск не поможет: порт занят. Сервис при этом работает.'),
      noRestart: true,
    }
  if (log.includes('at least one source file could not be read'))
    return {
      what: t('restic не смог прочитать часть файлов (код 3): снапшот есть, но неполный'),
      steps: [
        { id: 'find', text: t('Найти в журнале, какие файлы'), cmd: `sudo journalctl -u ${n} --since "2 days ago" --no-pager | grep -i -E "error|denied|no such"` },
        { id: 'fix', text: t('Исключить их из бэкапа или выдать доступ') },
      ],
    }
  if (n.startsWith('clamav-freshclam'))
    return {
      what: t('freshclam не может скачать базы антивируса, они устаревают'),
      steps: [{ id: 'mirror', text: t('Прописать свое зеркало (PrivateMirror в freshclam.conf) или прокси: зеркало ClamAV закрыто для адресов из России') }],
      foot: again,
      noRestart: true,
    }
  if (n === 'openipmi.service')
    return {
      what: t('openipmi не нашел IPMI на этой машине'),
      steps: [{ id: 'off', text: t('Если BMC нет или он не нужен, выключить юнит'), cmd: 'sudo systemctl disable --now openipmi.service && sudo systemctl reset-failed openipmi.service' }],
      foot: again,
      noRestart: true,
    }
  if (n === 'console-setup.service')
    return {
      what: t('Не применилась раскладка консоли, на сервере это ни на что не влияет'),
      steps: [
        { id: 'reset', text: t('Нажать "Сбросить"') },
        { id: 'fix', alt: true, text: t('поправить раскладку'), cmd: 'sudo dpkg-reconfigure keyboard-configuration' },
      ],
      foot: again,
      noRestart: true,
    }
  if (n === 'certbot.service' && log.includes('manual'))
    return {
      what: t('Сертификат выпущен вручную (manual plugin), сам он не продлится'),
      steps: [{ id: 'del', text: t('Удалить ненужный сертификат или продлить его вручную'), cmd: 'sudo certbot certificates && sudo certbot delete --cert-name <cert-name>' }],
      foot: again,
      noRestart: true,
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

// Шаг исправления пункта об ОС словами: команды собирает бэкенд под ноду, тексты здесь по id
function osFixText(st: OsFixStep, t: (k: string, p?: Record<string, string | number>) => string): string {
  switch (st.id) {
    case 'apt_update': return t('Обновить список пакетов: ничего не ставит, только узнает, что вышло.')
    case 'apt_daily': return t('Чтобы список обновлялся сам каждый день (без автоустановки):')
    case 'unatt_install': return t('Поставить автообновления. Ставят только исправления безопасности из репозитория дистрибутива: сторонние (docker и прочие) не трогают, конфиги не меняют, сами не перезагружают.')
    case 'unatt_enable': return t('Включить их: каждый день свежий список пакетов и исправления безопасности.')
    case 'unatt_run': return t('Поставить исправления безопасности сейчас, не дожидаясь ночного прогона. Лог - в /var/log/unattended-upgrades/.')
    case 'unatt_check': return t('Автообновления включены, но не запускались: проверить таймеры apt и последний лог.')
    case 'noboot_docker': return t('Сначала: эти контейнеры без restart policy, после перезагрузки docker их не поднимет.')
    case 'noboot_units': return t('Сначала: эти сервисы работают, но не включены, после перезагрузки их не будет.')
    case 'reboot_soft': return t('Перезагрузить при ближайшем окне обслуживания, срочности нет. Обновлен только dbus, а отдельно его не перезапустить: от него зависят systemd-logind и сессии.')
    case 'reboot': return t('Перезагрузить, когда есть доступ к консоли сервера. Несколько минут сервер недоступен. Старое ядро остается в меню GRUB (Advanced options), если новое не загрузится.')
    case 'pro_attach': return t('Подключить Ubuntu Pro: исправления безопасности до {date}. Токен - на ubuntu.com/pro, для личного использования бесплатно до 5 машин, для компании нужна подписка. Подключение панель видит с агента 2.39.', { date: st.arg ?? '' })
    case 'pro_run': return t('Поставить исправления из ESM.')
    case 'release_upgrade': return t('Переехать на следующую версию, по одной за раз. С перезагрузкой: заранее бэкап и доступ к консоли.')
    case 'debian_upgrade': return t('Переехать на Debian {n} по release notes. sources.list на новое кодовое имя, apt full-upgrade и перезагрузка: заранее бэкап и доступ к консоли.', { n: st.arg ?? '' })
    case 'el_upgrade': return t('Переехать на следующую версию. RHEL, AlmaLinux и Rocky - через leapp, CentOS - переустановкой на AlmaLinux или Rocky.')
    default: return st.id
  }
}

// Шаги "как исправить" у советов по ОС: разметка и "Копировать" - общий FixSteps, здесь тексты по
// id и когда пункт уйдет сам. SSH и доступы (accessFixText), pg_stat_statements в "Что грузит базу"
// (ServicesPage) зовут FixSteps со своими текстами
function OsFix({ steps }: { steps: OsFixStep[] }) {
  const { t } = useI18n()
  return (
    <FixSteps steps={steps} text={(st) => osFixText(st, t)}
      foot={t('Пункт уйдет сам в течение часа после этих шагов.')} />
  )
}

// Обновления ОС (агент 2.26): что стоит, что ждет установки, когда перезагружаться. Сверху -
// что сделать (os_advice считает бэкенд: конец поддержки, перезагрузка, безопасность, apt), у
// каждого пункта "как исправить" - готовые команды под эту ноду.
// Обновить ОС можно не всегда (старый сервер, софт не встанет на новую версию): тогда у пункта
// решение "обновить ОС нельзя" и т.п., он гаснет здесь и уходит из "Требует действий"
function OsUpdates({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const { busy, toggle } = useMuteToggle(s, onChanged)
  const [busyAll, setBusyAll] = useState(false)
  const [allErr, setAllErr] = useState<string | null>(null)
  const [open, setOpen] = useState('')
  const u = s.last_report?.os_upd
  const adv = s.os_advice ?? []
  // пункт ушел, пока был на экране: что изменилось - по свежему отчету агента
  const fixed = useFixDone(s.online ? s.os_advice : null, (a) => a.kind, (a) => {
    switch (a.kind) {
      case 'reboot': return u?.kernel
        ? t('Сервер перезагружен, работает ядро {k}, замечание закрыто', { k: u.kernel })
        : t('Сервер перезагружен, замечание закрыто')
      case 'security': return u?.unatt === 'on'
        ? t('Обновления безопасности установлены, автообновления включены, замечание закрыто')
        : t('Обновления безопасности установлены, замечание закрыто')
      case 'apt': return t('Список пакетов обновлен, замечание закрыто')
      case 'eol': return s.os ? t('ОС обновлена: {os}, замечание закрыто', { os: s.os }) : t('ОС обновлена, замечание закрыто')
      default: return t('Исправлено: {text}', { text: a.text })
    }
  }, { live: (a) => !a.muted })
  if (!u && !adv.length && !fixed.done.length) return null
  const live = adv.filter((a) => !a.muted)
  // обновлен только dbus (бэкенд os_support.soft_reboot): не "нужна", а при случае
  const softReboot = adv.some((a) => a.kind === 'reboot' && a.soft)
  const keepAll = () => {
    setBusyAll(true)
    setAllErr(null)
    muteServerKeys(s.id, live.map((a) => a.key))
      .then(onChanged)
      .catch((e) => setAllErr(e instanceof Error ? e.message : t('Ошибка')))
      .finally(() => setBusyAll(false))
  }
  const day = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleDateString() : '')
  const unatt = u?.unatt === 'on' ? t('включены') : u?.unatt === 'off' ? t('выключены') : u?.unatt === 'none' ? t('не установлены') : ''
  return (
    <div className="net-cards osupd-cards">
      <div className="chart-card net-card net-card-wide" id="mcard-osupd">
        <div className="net-head">
          <div className="chart-cap">{t('Обновления ОС')}</div>
          {!isViewer && live.length > 1 && (
            <button className="ghost small row-act" disabled={busyAll} onClick={keepAll}>
              {t('все так и оставить')}
            </button>
          )}
        </div>
        {allErr && <p className="form-error small">{allErr}</p>}
        {fixed.bars}
        {adv.map((a) => {
          // заглушенное до нового (та же заглушка, что галками на главной) снимается своим ключом
          const mk = a.muted_new && a.new_key ? a.new_key : a.key
          return (
          <div key={a.kind}>
            <div className={`osupd-adv ${a.muted ? 'osupd-adv-off' : a.level >= 2 ? 'net-prob' : 'net-note'}`}>
              <span className="osupd-adv-text">
                {a.text}
                {(a.pkgs?.length ?? 0) > 0 && (
                  <small className="osupd-adv-pkgs muted">{t('обновлены пакеты')}: {a.pkgs!.join(', ')}</small>
                )}
              </span>
              {a.muted && <span className="osupd-adv-why">{a.muted_new ? t('заглушено до нового') : osDecisionLabel(a.kind, t)}</span>}
              {(a.fix?.length ?? 0) > 0 && (
                <button className="ghost small row-act" onClick={() => setOpen(open === a.kind ? '' : a.kind)}>
                  {open === a.kind ? t('скрыть') : t('как исправить')}
                </button>
              )}
              {!isViewer && a.new_key && !a.muted && (
                <button className="ghost small row-act" disabled={busy === a.new_key || busyAll}
                  title={grpMuteTip(`os-${a.kind}${a.soft ? '-soft' : ''}`, t)}
                  onClick={() => toggle(a.new_key!, true)}>
                  {t('заглушить до нового')}
                </button>
              )}
              {!isViewer && a.key && (
                <button className="ghost small row-act" disabled={busy === mk || busyAll} onClick={() => toggle(mk, !a.muted)}>
                  {a.muted ? t('вернуть') : osDecisionLabel(a.kind, t)}
                </button>
              )}
            </div>
            {open === a.kind && a.fix && <OsFix steps={a.fix} />}
          </div>
          )
        })}
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
              value={u.reboot ? (softReboot ? t('при случае') : t('нужна')) : t('не нужна')}
              sub={u.reboot && u.reboot_ts ? t('с {d}', { d: day(u.reboot_ts) }) : undefined}
              title={(u.reboot_pkgs ?? []).join(', ') || undefined}
              tone={u.reboot ? (softReboot ? undefined : 'warn') : 'ok'}
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
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
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
  // Сервер бэкапов: ждут только диск - его работа (prune и check репозиториев, прием бэкапов), алерт
  // только через 6 ч (_BSRV_IO_HOLD_S в collector). Плитка красная, строки беды нет: сказать почему
  const io = PSI_ROWS.find((x) => x.k === 'io')!
  const ioHot = !!psi && ((psi.io_some_60 ?? 0) >= io.some || (psi.io_full_60 ?? 0) >= io.full)
  const bsrvBusy = !prob && ioHot && !!r?.backup_server?.present
  const pc = (v: number) => `${v.toFixed(v > 0 && v < 10 ? 1 : 0)}%`
  const fdPct = r?.fds && r.fds.max > 0 ? (r.fds.used / r.fds.max) * 100 : null
  const ds = r?.dstate
  // красное - только зависшие (агент 2.40, как их видит ядро) и рост счетчика hung_task. Число в D
  // под тяжелым чтением держится сутками, но процессы в нем каждую секунду другие
  const stuck = ds?.stuck ?? []
  const hungNew = ds?.hung_new ?? 0
  const dsSub = !ds
    ? ''
    : stuck.length
      ? stuck.slice(0, 3).map((x) => `${x.comm} (PID ${x.pid}) ${fmtDur(x.sec)}`).join(', ') +
        (stuck.length > 3 ? `, ${t('и еще {n}', { n: stuck.length - 3 })}` : '')
      : hungNew
        ? t('ядро видит зависшие задачи: {n} за 10 мин', { n: hungNew })
        : [
            (ds.top ?? []).map((x) => `${x.c} (${x.n})`).join(', '),
            ds.stuck && ds.n > 0 ? t('сменяются, зависших нет') : '',
          ]
            .filter(Boolean)
            .join(' · ') || t('ждут диск или сеть')
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-pressure">
      <div className="net-head">
        <div className="chart-cap">{t('Давление на ресурсы')}</div>
      </div>
      {prob && <div className="net-prob">{prob.text}</div>}
      {bsrvBusy && (
        <div className="net-note">
          {t('Сервер бэкапов: диск грузят prune и check репозиториев и прием бэкапов, это его обычная работа, алерт не шлем. Придет, если так продержится дольше 6 ч или процесс зависнет в ожидании диска.')}
        </div>
      )}
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
            sub={dsSub}
            tone={stuck.length || hungNew ? 'bad' : undefined}
            title={stuck.map((x) => `${x.comm} PID ${x.pid}${x.wchan ? `, wchan ${x.wchan}` : ''}`).join('\n') || undefined}
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
          {t('Процент - доля времени за минуту, когда хоть один процесс ждал ресурс. "Все стоят" ядро считает по каждому CPU отдельно (его процессы ждут ресурс, и выполнять на нем нечего), поэтому на многоядерном сервере это не значит, что стоял весь сервер. D-state - процессы, которые ждут диск или сеть и не прерываются, зависший - тот, что не просыпается дольше 2 минут.')}
        </div>
      )}
    </div>
  )
}

function LoopingUnits({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer, canLogs } = useAuth()
  const [busy, setBusy] = useState(false)
  const [jr, setJr] = useState<string | null>(null)
  const rows = (s.problems ?? []).filter((p) => p.kind === 'unit_loop' && p.unit)
  if (!rows.length) return null
  // журнал учетке только для просмотра - с галкой "может читать логи", без нее кнопка приглушена
  const hasJournal = !journalBlocker(s)
  const canJournal = canLogs && hasJournal
  const mute = async (unit: string) => {
    if (!window.confirm(t('Не алертить по {u}? Юнит перестанет алертить и как упавший, и как перезапускающийся.', { u: unit }))) return
    setBusy(true)
    try {
      await muteServerKeys(s.id, [UNIT_MUTE + unit])
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
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
          <FixSteps steps={[{ id: 'log', cmd: `sudo journalctl -u ${p.unit} -n 50 --no-pager` }]}
            text={() => t('Найти причину в логе')}
            foot={t('systemd поднимает сервис сам, поэтому среди упавших его нет.')} />
          {(!isViewer || hasJournal) && (
            <div className="unit-actions small">
              {canJournal ? (
                <button className="ghost small row-act" onClick={() => setJr(p.unit!)}>
                  {t('Журнал')}
                </button>
              ) : (
                hasJournal && <LockedLogs className="ghost small row-act">{t('Журнал')}</LockedLogs>
              )}
              {!isViewer && (
                <button className="ghost small row-act" disabled={busy} onClick={() => mute(p.unit!)}>
                  🔕 {t('не алертить по этому юниту')}
                </button>
              )}
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
// Красное - опасное (база, очередь, Docker API, kubelet, Prometheus и node_exporter), желтое - служебное
// (порты Kubernetes, агенты мониторинга): снаружи должно быть закрыто и то и другое. Проверка с
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
      await muteServerKeys(s.id, on ? [key] : [], on ? [] : [key])
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
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
      {!own && (ex?.realip ?? []).length > 0 && (
        <RealIpBox server={s} items={ex?.realip ?? []} isViewer={isViewer} onChanged={onChanged} />
      )}
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
              sub={t('базы, очереди, Docker API, kubelet, экспортеры метрик')}
              tone={dangerous ? 'bad' : 'ok'}
            />
          )}
          {!own && (
            <NetTile
              label={t('Служебных')}
              value={String(service)}
              sub={t('Kubernetes, агенты мониторинга')}
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
              {t('Красным - опасное: базы, очереди, etcd, kubelet, Docker API, Prometheus и node_exporter. Желтым - служебное: порты Kubernetes и сети подов, агенты мониторинга. Снаружи должно быть закрыто и то и другое, а если порт открыт специально, отметьте "открыт намеренно". Остальное (SSH, веб, ваши сервисы) - для сведения.')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Где веб-сервер: бэкенд пишет "на хосте", "в поде ns/имя", "в контейнере имя"
function realIpWhere(w: string, t: TFn): string {
  if (w.startsWith('в поде ')) return t('в поде {n}', { n: w.slice(7) })
  if (w.startsWith('в контейнере ')) return t('в контейнере {n}', { n: w.slice(13) })
  return t('на хосте')
}

// Как ограничить доверие к заголовку у этого веб-сервера
function realIpFix(label: string, t: TFn): string {
  if (label === 'ingress-nginx') return t('в ConfigMap ingress-nginx задайте proxy-real-ip-cidr диапазонами Cloudflare')
  if (label === 'Caddy') return t('в trusted_proxies оставьте только диапазоны Cloudflare')
  if (label === 'Traefik') return t('уберите forwardedHeaders.insecure и оставьте в trustedIPs только диапазоны Cloudflare')
  return t('в set_real_ip_from оставьте только диапазоны Cloudflare')
}

// Веб-сервер верит CF-Connecting-IP или X-Forwarded-For от любого адреса, а 80/443 открыты в обход
// Cloudflare: прямой заход с поддельным заголовком проходит allow/deny (app/cf_realip.py). Решение
// "так задумано" - cfip:<веб-сервер>, как "открыт намеренно" у порта
function RealIpBox({ server: s, items, isViewer, onChanged }: { server: Server; items: RealIpTrust[]; isViewer: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const setDecided = async (key: string, on: boolean) => {
    if (on && !window.confirm(t('Так задумано: белые списки этого веб-сервера обходить можно? Панель перестанет алертить по нему.'))) return
    setBusy(true)
    try {
      const k = CFIP_MUTE + key
      await muteServerKeys(s.id, on ? [k] : [], on ? [] : [k])
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      {items.map((x) => (
        <div key={x.key} className={x.decided ? 'net-note exp-realip-done' : x.acl > 0 ? 'net-prob exp-realip' : 'net-note exp-realip'}>
          <div className="exp-realip-h">
            <b>{x.acl > 0 ? t('Белые списки обходятся подменой {h}', { h: x.hdr }) : t('IP клиента можно подделать заголовком {h}', { h: x.hdr })}</b>
            {x.decided ? (
              <span className="muted exp-act">
                {t('так задумано')}{' '}
                {!isViewer && (
                  <button className="ghost small row-act" disabled={busy} onClick={() => setDecided(x.key, false)}>
                    {t('вернуть алерт')}
                  </button>
                )}
              </span>
            ) : (
              !isViewer && (
                <button className="ghost small row-act exp-act" disabled={busy} onClick={() => setDecided(x.key, true)}>
                  🔕 {t('так задумано')}
                </button>
              )
            )}
          </div>
          <div>
            {t('{who} {where} верит заголовку от любого адреса:', { who: x.label, where: realIpWhere(x.where, t) })} <span className="mono">{x.trust}</span>.{' '}
            {(x.pods ?? 0) > 1 && <span className="muted">{t('подов: {n}', { n: x.pods ?? 0 })}. </span>}
            {x.acl > 0 ? (
              <>
                {t('Порты {p} открыты в обход Cloudflare, и прямой заход на адрес ноды с поддельным {h} проходит allow/deny у сайтов ({n}):', {
                  p: x.ports.join(', '),
                  h: x.hdr,
                  n: x.acl,
                })}{' '}
                <span className="mono">{x.names.join(', ')}</span>
              </>
            ) : (
              t('Порты {p} открыты в обход Cloudflare: при прямом заходе с поддельным {h} логи, баны fail2ban, лимиты входа и само приложение видят чужой адрес. Белых списков (allow/deny) тут нет.', {
                p: x.ports.join(', '),
                h: x.hdr,
              })
            )}
          </div>
          {!x.decided && (
            <div className="muted small">
              {t('Пустите на {p} только адреса Cloudflare или {fix}.', { p: x.ports.join(', '), fix: realIpFix(x.label, t) })}
            </div>
          )}
        </div>
      ))}
    </>
  )
}

// Точечный мут из карточки: добавить или снять ключ в alert_mutes сервера
function useMuteToggle(s: Server, onChanged: () => void) {
  const [busy, setBusy] = useState('')
  const toggle = (key: string, on: boolean) => {
    setBusy(key)
    muteServerKeys(s.id, on ? [key] : [], on ? [] : [key])
      .then(onChanged)
      .catch(toastError)
      .finally(() => setBusy(''))
  }
  return { busy, toggle }
}

// Шаг "как исправить" у SSH (выключить пароль) и "как отключить" у заброшенной учетки: команды под
// ноду собирает бэкенд (ssh_audit, access_audit), тексты здесь по id
function accessFixText(st: OsFixStep, t: (k: string, p?: Record<string, string | number>) => string): string {
  const p = { arg: st.arg ?? '', on: st.on ?? '' }
  switch (st.id) {
    case 'ssh_key_add': return t('Сначала добавить ключ для {arg}. Сейчас у нее только пароль, последний вход {on}: после выключения паролей она не войдет. Вместо <public-key> - свой публичный ключ (ssh-ed25519 ...), вход по ключу проверить в новом окне.', p)
    case 'ssh_key_check': return t('Проверить вход по ключу для {arg} в новом окне. Она входила по паролю {on}, хотя ключ есть: если ключ не тот, добавьте нужный.', p)
    case 'ssh_dropin': return t('Выключить вход по паролю отдельным файлом. Имя на 00- нужно, чтобы файл читался раньше 50-cloud-init.conf: в sshd побеждает первое значение.')
    case 'ssh_conf': return t('Выключить вход по паролю в sshd_config. У этой версии OpenSSH нет sshd_config.d, строка встает первой. Копия - sshd_config.bak-nopass.')
    case 'ssh_check': return t('Проверить, пока ничего не применено: ниже должно быть no. Если yes - значение задано раньше, найти где: sudo grep -rn PasswordAuthentication /etc/ssh/')
    case 'ssh_reload': return t('Применить, открытые сессии не рвутся. Это окно не закрывайте, пока в новом не войдете по ключу: если не пускает, отсюда же откат.')
    case 'ssh_undo': return t('если что-то не так')
    case 'ssh_match': return t('пароль только из своих сетей, из интернета - по ключу. Вместо <cidr> - ваши адреса или сети через запятую (192.0.2.10,198.51.100.0/24), иначе пароль выключится везде. Этот файл вместо файла выше, дальше та же проверка и reload.')
    case 'ssh_guard': return t('пароль нужен отовсюду: fail2ban от перебора. Банит адрес после нескольких неудачных входов, здесь порт {arg} (на RHEL пакет из EPEL). Пункт останется, уйдет только приписка про перебор.', p)
    case 'acc_alone': return t('Через другие учетки за 90 дней никто не входил. Если отключить {arg}, войти на сервер может стать некому: сначала проверьте вход другой учеткой с sudo или через консоль.', p)
    case 'acc_check': return t('Убедиться, что от {arg} ничего не работает. Процессы, crontab, lingering и сервисы с User={arg}: пусто и no crontab - ничего нет.', p)
    case 'acc_lock': return t('Отключить: пароль заблокирован, срок учетки в прошлом. SSH не пустит ни по паролю, ни по ключу, cron и su для нее тоже. Обратимо.')
    case 'acc_root_lock': return t('Заблокировать пароль {arg}: sudo и cron root продолжат работать. Срок учетки root не ставят, истекшая root ломает cron и su. Если в консоль входите root по паролю, после блокировки не войдете.', p)
    case 'acc_keys': return t('Убрать ключи: файл переименовывается, а не удаляется. Панель считает доступом любой ключ в authorized_keys.')
    case 'acc_undo': return t('если учетка понадобится')
    case 'acc_delete': return t('удалить совсем - необратимо. Домашний каталог останется без хозяина (userdel -r удалит и его). Лучше сначала поживите с отключенной.')
    default: return st.id
  }
}

// Доступы (helper changes-setup 0.5): кто может войти, чем, с sudo ли без пароля и когда входил.
// Последний вход - по журналу sshd, не только по lastlog: ansible и CI ходят без терминала.
// У заброшенной учетки рядом с "учетка нужна" - "как отключить": команды, после которых доступа
// панель не видит и пункт уходит сам
function AccessCard({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const { busy, toggle } = useMuteToggle(s, onChanged)
  const [open, setOpen] = useState('')
  const v = s.access
  // заброшенная учетка ушла из заброшенных, пока была на экране: что с ней теперь
  const fixed = useFixDone(v && s.online ? v.accounts.filter((a) => a.stale) : null, (a) => a.name, (a) => {
    const now = v?.accounts.find((x) => x.name === a.name)
    return !now
      ? t('Учетка {name} удалена, замечание закрыто', { name: a.name })
      : !now.access
        ? t('Учетка {name} отключена, войти по ней больше нельзя, замечание закрыто', { name: a.name })
        : t('Учетка {name} больше не в заброшенных: по ней входили, замечание закрыто', { name: a.name })
  }, { live: (a) => !a.muted })
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
  // шаги открытой учетки - под списком, в тексте шагов ее имя
  const openFix = v.accounts.find((a) => a.name === open && a.stale && !a.muted)?.fix ?? []
  return (
    <div className="chart-card net-card" id="mcard-access">
      <div className="net-head">
        <div className="chart-cap">{t('Доступы')}</div>
      </div>
      {fixed.bars}
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
          const fix = a.stale && !a.muted ? (a.fix ?? []) : []
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
              {fix.length > 0 && (
                <button className="ghost small row-act acc-act" onClick={() => setOpen(open === a.name ? '' : a.name)}>
                  {open === a.name ? t('скрыть') : t('как отключить')}
                </button>
              )}
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
      {openFix.length > 0 && (
        <FixSteps steps={openFix} text={(st) => accessFixText(st, t)}
          foot={t('Учетка уйдет из заброшенных в течение 10 минут.')} />
      )}
      <div className="net-foot">
        {t('Входы по журналу sshd видны с {d}.', {
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
  const units = s.noboot?.units ?? []
  const fixed = useFixDone(s.online ? s.noboot?.units : null, (u) => u.unit, (u) =>
    t('{unit} включен, поднимется после перезагрузки, замечание закрыто', { unit: u.unit }), { live: (u) => !u.muted })
  if (units.length === 0 && !fixed.done.length) return null
  const live = units.filter((u) => !u.muted)
  const cmd = `sudo systemctl enable ${live.map((u) => u.unit).join(' ')}`
  return (
    <div className="chart-card net-card" id="mcard-noboot">
      <div className="net-head">
        <div className="chart-cap">{t('Не запустятся после перезагрузки')}</div>
      </div>
      {fixed.bars}
      {units.length > 0 && <>
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
      </>}
      {live.length > 0 && <FixSteps steps={[{ id: 'enable', cmd }]} text={() => t('Включить автозапуск')} />}
    </div>
  )
}

// SSH: как пускает соединение из интернета (helper changes-setup 0.3). Судим только порт, открытый
// снаружи по внешней проверке: SSH за белым списком с паролем - не дыра. Совет виден и рядом с
// проблемой: у него "как исправить" (готовые команды, сначала - кому добавить ключ) и решение "вход
// по паролю нужен" - принятый совет уходит из "Требует действий", здесь приглушен с "вернуть"
function SshLogin({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const { busy, toggle } = useMuteToggle(s, onChanged)
  const [open, setOpen] = useState('')
  const v = s.ssh
  const fixed = useFixDone(v && s.online ? s.ssh_advice : null, (a) => a.kind, (a) =>
    a.kind === 'ssh_password' && v && !v.password
      ? t('Вход по паролю выключен, только ключи, замечание закрыто')
      : t('Исправлено: {text}', { text: a.text }), { live: (a) => !a.muted })
  if (!v) return null
  const probs = (s.problems ?? []).filter((p) => p.kind === 'ssh_risk')
  const adv = s.ssh_advice ?? []
  const openText = v.open === true ? t('открыт в интернет') : v.open === false ? t('закрыт снаружи') : t('снаружи не проверялся')
  const rootText = v.root === 'yes' ? (v.root_password ? t('по паролю') : t('по ключу')) : v.root === 'no' ? t('запрещен') : t('только по ключу')
  const top = v.fail_top ?? []
  return (
    <div className="chart-card net-card" id="mcard-ssh">
      <div className="net-head">
        <div className="chart-cap">SSH</div>
      </div>
      {fixed.bars}
      {probs.map((p) => (
        <div key={p.text} className={p.level >= 2 ? 'net-prob' : 'net-note'}>{p.text}</div>
      ))}
      {adv.map((a) => (
        <div key={a.kind}>
          <div className={`osupd-adv ${a.muted ? 'osupd-adv-off' : a.level >= 2 ? 'net-prob' : 'net-note'}`}>
            <span className="osupd-adv-text">{a.text}</span>
            {a.muted && <span className="osupd-adv-why">{t('вход по паролю нужен')}</span>}
            {(a.fix?.length ?? 0) > 0 && !a.muted && (
              <button className="ghost small row-act" onClick={() => setOpen(open === a.kind ? '' : a.kind)}>
                {open === a.kind ? t('скрыть') : t('как исправить')}
              </button>
            )}
            {!isViewer && a.key && (
              <button className="ghost small row-act" disabled={busy === a.key} onClick={() => toggle(a.key!, !a.muted)}>
                {a.muted ? t('вернуть') : t('вход по паролю нужен')}
              </button>
            )}
          </div>
          {open === a.kind && !a.muted && a.fix && (
            <FixSteps steps={a.fix} text={(st) => accessFixText(st, t)}
              foot={t('Пункт уйдет сам в течение 10 минут после выключения паролей.')} />
          )}
        </div>
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
        <NetTile label={t('Неудачных входов за 10 минут')} value={String(v.fail)} />
        {v.accepted_password > 0 && (
          <NetTile label={t('Вошли по паролю за 10 минут')} value={String(v.accepted_password)} tone="warn" />
        )}
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

function FailedUnits({ server: s, onChanged, onOom }: { server: Server; onChanged: () => void; onOom?: (ep: string) => void }) {
  const { t } = useI18n()
  const { isViewer, canLogs } = useAuth()
  const [st, setSt] = useState<Record<string, { busy?: boolean; ok?: boolean; msg?: string; log?: string[] }>>({})
  const [picking, setPicking] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [jr, setJr] = useState<string | null>(null)
  const r = s.last_report
  const ub = unitsBlock(r)
  if (!ub || !ub.units.length) return null
  // журнал учетке только для просмотра - с галкой "может читать логи", без нее кнопка приглушена
  const hasJournal = !journalBlocker(s)
  const canJournal = canLogs && hasJournal
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
  // у карточки с одной причиной на несколько юнитов (mdmonitor) - со всеми по очереди, до первой
  // ошибки; результат пишется на карточку (key)
  const run = async (key: string, units: string[], mode: 'restart' | 'reset') => {
    const unit = units.join(', ')
    const q =
      mode === 'restart'
        ? t('Перезапустить {u}?', { u: unit })
        : t('Сбросить у {u} отметку "упал"? Сам юнит не запустится, алерт по нему закроется.', { u: unit })
    if (!window.confirm(q)) return
    setSt((cur) => ({ ...cur, [key]: { busy: true } }))
    try {
      let ok = false
      for (const one of units) {
        const c = await backupCommand(s.id, { action: 'unit_fix', mode, unit: one })
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
        ok = last.status === 'done' && !!last.ok
        const msg = data ? unitStateText(data, t) : last.result || t('не удалось')
        setSt((cur) => ({ ...cur, [key]: { ok, msg: units.length > 1 ? `${one}: ${msg}` : msg, log: data?.log } }))
        if (!ok) break
      }
      if (ok) window.setTimeout(onChanged, 5000)
    } catch (e) {
      setSt((cur) => ({ ...cur, [key]: { ok: false, msg: e instanceof Error ? e.message : String(e) } }))
    }
  }
  const mute = async (kinds: string[], hours: number) => {
    setBusy(true)
    try {
      const mutes = s.alert_mutes ?? []
      if (hours < 0) {
        await muteServerKeys(s.id, kinds)
      } else if (hours === 0) {
        if (kinds.some((k) => mutes.includes(k)))
          await muteServerKeys(s.id, [], kinds)
        for (const k of kinds) if (s.alert_snoozes?.[k]) await snoozeServerAlert(s.id, k, 0)
      } else for (const k of kinds) await snoozeServerAlert(s.id, k, hours)
      setPicking(null)
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  // убит OOM (app/oom.py): причина и ссылка на эпизод, строка ядра в логе. Менеджер сессии
  // пользователя (user@N.service) - не служба: серым и в конце
  const oomOf = (u: FailedUnit) => s.oom?.units?.[u.unit]
  const muted = (u: FailedUnit) => ((s.alert_mutes ?? []).includes(UNIT_MUTE + u.unit) || oomOf(u)?.quiet ? 1 : 0)
  // юниты с одной причиной (hint.group) - одной карточкой: одно объяснение и одна команда
  const cards: { key: string; units: FailedUnit[]; hint: UnitHint | null }[] = []
  for (const u of ub.units) {
    const hint = unitHint(u, ub.units, t)
    const same = hint?.group ? cards.find((c) => c.hint?.group === hint.group) : undefined
    if (same) same.units.push(u)
    else cards.push({ key: u.unit, units: [u], hint })
  }
  const cardMuted = (c: (typeof cards)[number]) => Math.min(...c.units.map(muted))
  const cardLevel = (c: (typeof cards)[number]) => Math.max(...c.units.map(unitLevel))
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-units">
      <div className="net-head">
        <div className="chart-cap">{t('Упавшие юниты systemd')}</div>
      </div>
      {!agentOk && !isViewer && ub.fix && (
        <div className="muted small du-note">{t('Кнопки перезапуска появятся после обновления агента до 2.17.')}</div>
      )}
      {cards
        // заглушенные - в конец: они уже разобраны, внимание нужно остальным
        .sort((a, b) => cardMuted(a) - cardMuted(b) || cardLevel(b) - cardLevel(a))
        .map((card) => {
          const { key, units, hint } = card
          const u = units[0]
          const lvl = cardLevel(card)
          const names = units.map((x) => x.unit)
          const kinds = names.map((x) => UNIT_MUTE + x)
          const perm = kinds.every((k) => (s.alert_mutes ?? []).includes(k))
          const untils = kinds.map((k) => s.alert_snoozes?.[k])
          const until = untils.every(Boolean) ? [...(untils as string[])].sort()[0] : undefined
          const snoozed = !!until && new Date(until).getTime() > Date.now()
          const us = st[key]
          const ol = units.length === 1 ? oomOf(u) : undefined
          // одинаковые строки у юнитов карточки - один раз
          const ulog = [...new Set(units.flatMap((x) => x.log))]
          const log = ol?.line ? [`kernel: ${ol.line}`, ...ulog] : ulog
          // журнал - у того, по чьему логу видна причина; "назад" - по последнему падению
          const jrUnit = (units.find((x) => x.log.length) ?? u).unit
          const latest = units.reduce((a, b) => ((b.since ?? 0) > (a.since ?? 0) ? b : a))
          return (
            <div key={key} className={`unit-row${perm || snoozed || ol?.quiet ? ' unit-muted' : ''}`}>
              <div className="unit-head">
                <span className={`lk-dot lk-dot-${ol?.quiet ? 'idle' : lvl === 2 ? 'bad' : 'warn'}`} />
                {names.map((x, i) => (
                  <Fragment key={x}>
                    {i > 0 && <span className="muted">{i === names.length - 1 ? t('и') : ','}</span>}
                    <span className="mono unit-name">{x}</span>
                  </Fragment>
                ))}
                <span className={ol?.quiet ? 'muted' : lvl === 2 ? 't-down' : 't-degraded'}>
                  {ol ? t('убит OOM killer') : unitWhy(u, t)}
                </span>
                <span className="muted small">{ago(latest)}</span>
                {units.length > 1 && <span className="muted small">{t('одна причина')}</span>}
                {ol?.ep && onOom && (
                  <button className="oom-link small" onClick={() => onOom(ol.ep!)}>
                    {t('эпизод OOM')}
                  </button>
                )}
              </div>
              {ol?.quiet && (
                <div className="muted small unit-desc">
                  {t('Менеджер сессии пользователя, а не служба: поднимется сам при следующем входе, в упавшие не считается')}
                </div>
              )}
              {units.length === 1 && u.desc && u.desc !== u.unit && <div className="muted small unit-desc">{u.desc}</div>}
              {/* вывод юнита - мелко под заголовком: с подсказкой он только подтверждает причину */}
              {log.length > 0 && <pre className={`unit-log mono${hint ? ' unit-log-c' : ''}`}>{log.join('\n')}</pre>}
              {hint && (
                <div className="unit-hint">
                  <div className="muted small">{hint.what}</div>
                  <FixSteps steps={hint.steps} text={(x) => x.text} foot={hint.foot} />
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
                {canJournal ? (
                  <button className="ghost small row-act" onClick={() => setJr(jrUnit)}>
                    {t('Журнал')}
                  </button>
                ) : (
                  hasJournal && <LockedLogs className="ghost small row-act">{t('Журнал')}</LockedLogs>
                )}
                {canFix && !us?.busy && (
                  <>
                    {/* перезапуск, который заведомо не поможет, не предлагаем: почему - в подсказке */}
                    {!hint?.noRestart && (
                      <button className="ghost small row-act" onClick={() => run(key, names, 'restart')}>
                        {t('Перезапустить')}
                      </button>
                    )}
                    <button className="ghost small row-act" onClick={() => run(key, names, 'reset')}>
                      {t('Сбросить')}
                    </button>
                  </>
                )}
                {perm || snoozed ? (
                  <span className="snooze-active-chip snooze-perm">
                    🔇 {perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(until!) }) })}
                    {!isViewer && (
                      <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(kinds, 0)}>
                        ✕
                      </button>
                    )}
                  </span>
                ) : (
                  !isViewer &&
                  (picking === key ? (
                    <>
                      <span className="muted">🔕 {units.length > 1 ? t('не алертить по этим юнитам:') : t('не алертить по этому юниту:')}</span>
                      {[
                        { h: 24, l: t('1 день') },
                        { h: 24 * 7, l: t('1 неделя') },
                        { h: -1, l: t('постоянно') },
                      ].map(({ h, l }) => (
                        <button key={h} className="ghost small" disabled={busy} onClick={() => mute(kinds, h)}>
                          {l}
                        </button>
                      ))}
                      <button className="ghost small" disabled={busy} onClick={() => setPicking(null)}>
                        {t('Отмена')}
                      </button>
                    </>
                  ) : (
                    <button className="ghost small" onClick={() => setPicking(key)}>
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
  sub,
  title,
  active,
  expands,
  onClick,
}: {
  num: number
  label: string
  tone?: 'up' | 'down' | 'degraded' | 'muted'
  // строки под подписью: что именно и где (плитка "Заглушено")
  sub?: string[]
  title?: string
  active: boolean
  // плитка открывает панель, а не фильтрует список
  expands?: boolean
  onClick: () => void
}) {
  return (
    <button
      className={`stat-tile stat-tile-btn${sub ? ' stat-tile-wide' : ''}${active ? ' stat-tile-active' : ''}`}
      onClick={onClick}
      title={title || undefined}
      aria-pressed={expands ? undefined : active}
      aria-expanded={expands ? active : undefined}
      aria-haspopup={expands ? 'dialog' : undefined}
    >
      <div className={`stat-num${tone ? ` t-${tone}` : ''}`}>{num}</div>
      <div className="stat-lbl">{label}</div>
      {sub?.filter(Boolean).map((x) => (
        <div key={x} className="stat-sub">
          {x}
        </div>
      ))}
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

  // доступная (подписанная) версия агента для управляемого обновления
  const [avail, setAvail] = useState('')
  // обновление агента: та же строка, что на главной (FleetUpdate.tsx), здесь еще с canary
  const fleetRun = useFleetRollout(servers ?? [], avail, setServers, onUnauthorized, !isViewer)
  useEffect(() => {
    getAgentRelease()
      .then((r) => setAvail(r.version))
      .catch(() => setAvail(''))
  }, [])

  // серверы, которые онлайн, отчитались о версии и отстают от доступной
  const behind = agentBehind(servers ?? [], avail).filter((s) => s.online)
  // заглушенное (пороги и алерты серверов) - плиткой "Заглушено" рядом с остальными, весь
  // список в панели справа: мьют видно, не открывая карточку (иначе бессрочный теряется молча).
  // Мьюты репозиториев и покрытия бэкапа живут на своей странице "Бэкапы"
  const mutes = muteRows(servers ?? [], t, ['alert'])
  const mutesSub = mutesTileSub(mutes, t)
  const [mutesOpen, setMutesOpen] = useState(false)
  const online = servers?.filter((s) => s.online).length ?? 0
  const total = servers?.length ?? 0
  // серверы с бедой - по тем же строкам и цветам, что "Что сломано" на главной (serverUtils.tallyServers):
  // с проблемой (есть красная) и только с предупреждениями. Плитка фильтрует список по обоим, поэтому
  // число - сумма, а подпись говорит, что в нем
  const srvTally = tallyServers(servers ?? [], t)
  const problems = srvTally.down + srvTally.warn
  const problemsLabel = !srvTally.warn ? t('С проблемами') : !srvTally.down ? t('С предупреждениями')
    : t('С проблемами и предупреждениями')
  const detail = detailId != null ? servers?.find((s) => s.id === detailId) : undefined
  // ссылка на удаленный или чужой сервер: сказать об этом и убрать id из адреса, а не молча
  // показать список
  const notFound = !!servers && detailId != null && !detail
  useUrlCard('server', notFound ? null : detailId)
  const q = query.trim().toLowerCase()
  const visible = (servers ?? []).filter((s) => {
    if (filter === 'online' && !s.online) return false
    if (filter === 'offline' && s.online) return false
    if (filter === 'problem' && srvIssues(s, t).length === 0) return false
    if (!q) return true
    return [s.name, s.group_name, s.hostname, s.os]
      .some((v) => v?.toLowerCase().includes(q)) || srvIpHit(s, q)
  })
  // сырое (неокруглённое) значение метрики — чтобы близкие значения не «ничьились»
  const metric = (s: Server): number | null => {
    const rep = s.last_report
    if (!rep) return null
    if (sortBy === 'cpu') return rep.cpu_percent ?? null
    if (sortBy === 'ram')
      return rep.mem_total ? (rep.mem_used ?? 0) / rep.mem_total : null
    const d = (rep.disks ?? []).filter((x) => x.total)
    return d.length ? Math.max(...d.map((x) => x.used / x.total)) : null
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
    // отдельного bulk-эндпоинта у серверов нет — удаляем по одному. Уже удаленный (404) не ошибка,
    // а после отказа список перечитываем: удаленные до него не должны висеть выбранными
    const left = new Set(ids)
    try {
      for (const id of ids) {
        await deleteServer(id).catch((e) => {
          if (!(e instanceof ApiError && e.status === 404)) throw e
        })
        left.delete(id)
      }
      exitSelect()
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setSelected(left)
      load()
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
            tone={srvTally.down ? 'down' : 'degraded'}
            label={problemsLabel}
            title={t('серверов с проблемами: {d}, только с предупреждениями: {w}', { d: srvTally.down, w: srvTally.warn })}
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
          <SrvTile
            num={mutes.length}
            tone="muted"
            label={t('Заглушено')}
            sub={[mutesSub.where, mutesSub.when]}
            title={mutesSub.all}
            active={mutesOpen}
            expands
            onClick={() => setMutesOpen(true)}
          />
        </div>
      )}

      {!isViewer && (behind.length > 0 || fleetRun.agent) && (
        <div className="agent-update-banner upd-banner">
          <AgentUpdRow servers={servers ?? []} avail={avail} fleet={fleetRun} canUpd canary
            onOpenNode={(id) => setDetailId(id)} />
          {fleetRun.err && <div className="upd-err">{fleetRun.err}</div>}
        </div>
      )}
      {mutesOpen && (
        <MutesPanel
          rows={mutes}
          servers={servers ?? []}
          canEdit={!isViewer}
          onOpen={(id) => {
            // в карточку на раздел "Алерты": там те же заглушки и их настройка
            setMutesOpen(false)
            setDetailSec('alerts')
            setDetailId(id)
          }}
          onChanged={(u) => setServers((prev) => (prev ?? []).map((x) => (x.id === u.id ? u : x)))}
          onUnauthorized={onUnauthorized}
          onClose={() => setMutesOpen(false)}
        />
      )}

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

      {notFound && (
        <p className="form-error">
          {t('Сервер не найден или нет доступа')}{' '}
          <button type="button" className="linklike" onClick={() => setDetailId(null)}>{t('Закрыть')}</button>
        </p>
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
                  servers={servers ?? []}
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
          key={detail.id}
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
  servers,
  onOpen,
  onOpenSec,
  showGroup = true,
  selecting,
  checked,
  dragging,
  drag,
}: {
  server: Server
  servers: Server[] // все серверы: имена нод за ретранслятором
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
  // разделы с нулевым размером не считаем; если других нет - нет и процента (а не -Infinity%)
  const diskPs = (r.disks ?? []).filter((d) => d.total).map((d) => Math.round((d.used / d.total) * 100))
  const diskP = diskPs.length ? Math.max(...diskPs) : null
  // «горит» — сервер онлайн, но с активными превышениями (CPU/RAM/диск/темп/…)
  const issues = srvIssues(s, t)
  const onFire = s.online && issues.length > 0
  const known = knownItems(s, t)
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
          {/* известные проблемы: какие и что про них отмечено, клик - к списку в карточке, там же снять */}
          {known.length > 0 && (
            <span
              className="note-chip clickable"
              role="button"
              tabIndex={0}
              title={`${knownTitle(known, t)}\n${t('Нажмите: открыть список в карточке, там же снять отметку')}`}
              onClick={(e) => { e.stopPropagation(); onOpenSec('known') }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation(); e.preventDefault()
                  onOpenSec('known')
                }
              }}
            >
              📌
            </span>
          )}
          <MaintChip kind="server" id={s.id} />
          {showGroup && s.group_name && (
            <span className="type-chip group-chip">{s.group_name}</span>
          )}
          <RoleChips s={s} servers={servers} t={t} />
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
  useEscape(onClose)
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

// Команда установки по аргументам: переносится между ними, а не посреди токена. Токен
// приглушен до наведения (или тапа на телефоне), копируется команда целиком. Повторы ожидания
// (sh -c '...') тоже приглушены и текут как текст: глазу нужны адрес панели и токен (KERVAX_TOKEN в начале)
function InstallCmd({ cmd, token, add }: { cmd: string; token: string; add?: string }) {
  const parts = cmd.match(/'[^']*'|\S+/g) ?? []
  return (
    <pre className="install-cmd ic-cmd" tabIndex={0}>
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && ' '}
          <span className={p === token || p === `KERVAX_TOKEN=${token}` ? 'ic-arg ic-tok' : p.length > 80 && p.startsWith("'") ? 'ic-script' : 'ic-arg'}>
            {p}
          </span>
        </span>
      ))}
      {add && <span className="install-cmd-add"> {add}</span>}
    </pre>
  )
}

export function InstallModal({
  enroll,
  onClose,
  title,
  mode,
}: {
  enroll: ServerEnroll
  onClose: () => void
  title?: string
  mode?: InstallMode // с чего открыть выбор пути (подсказка "агент не достает до панели")
}) {
  const { t } = useI18n()
  useEscape(onClose)
  const [copied, setCopied] = useState('')
  // путь до панели (InstallPath): прокси ретранслятора, локации или свой - в команду
  const [path, setPath] = useState<InstallMode>(mode ?? 'direct')
  const [proxy, setProxy] = useState('')
  const base = localizeInstallCmd(enroll.install_cmd, t)
  const cmd = withProxy(base, proxy)
  // путь через прокси выбран, а прокси в команде еще нет: копировать рано
  const pending = path !== 'direct' && cmd === base
  // живая проверка: поллим сервер, пока агент не пришлёт первый отчёт
  const [connected, setConnected] = useState<Server | null>(null)
  // а пока его нет - что панель знает о ноде: открыт ли ей адрес, что она уже скачала
  const [st, setSt] = useState<InstallStatus | null>(null)
  useEffect(() => {
    if (connected) return
    const poll = async () => {
      try {
        const [list, now] = await Promise.all([listServers(), installStatus(enroll.server.id)])
        setSt(now)
        const s = list.find((x) => x.id === enroll.server.id)
        if (s?.last_seen) setConnected(s)
      } catch {
        /* транзиентные сбои поллинга не мешают ожиданию */
      }
    }
    void poll()
    const id = window.setInterval(poll, 4000)
    return () => window.clearInterval(id)
  }, [enroll.server.id, connected])
  const copy = (text: string, what: string) => {
    navigator.clipboard?.writeText(text)
    setCopied(what)
  }
  return (
    <div className="modal-backdrop">
      <div className="card modal ic-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{title ?? t('Сервер создан')}</h3>
          <span className="ic-name mono">{enroll.server.name}</span>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>

        <section className="ic-step">
          <div className="ic-step-head">
            <span className="ic-num">1</span>
            {t('Как агент пойдет в панель')}
          </div>
          <InstallPath
            server={enroll.server}
            initial={mode}
            onMode={setPath}
            onProxy={(p) => {
              setProxy(p)
              setCopied('')
            }}
          />
        </section>

        <section className="ic-step">
          <div className="ic-step-head">
            <span className="ic-num">2</span>
            {t('Выполните на сервере под root')}
            <span className="ic-once muted small">{t('токен показывается один раз')}</span>
          </div>
          <div className="ic-cmd-row">
            <InstallCmd cmd={cmd} token={enroll.token} />
            <button className={`ic-copy${connected ? ' ghost' : ''}`} disabled={pending} onClick={() => copy(cmd, 'cmd')}>
              {copied === 'cmd' ? t('Скопировано ✓') : t('Копировать')}
            </button>
          </div>
          {pending && (
            <p className="ic-hint t-degraded small">{t('Сначала заполните поля выше: прокси попадет в команду')}</p>
          )}
          <details className="ic-more">
            <summary className="muted small">{t('Агент сам настроит Docker, Kubernetes и доступ к бэкапам')}</summary>
            <p className="muted small">
              {t('Docker через прокси только для чтения (read-only proxy), Kubernetes через узкий сервисный аккаунт (SA), плюс доступ к бэкапам. Вручную доделывать ничего не нужно. Без авто-настройки: допишите в конец команды --no-auto.')}
            </p>
          </details>
          <details className="ic-more">
            <summary className="muted small">{t('На сервере уже стоит агент другой панели?')}</summary>
            <p className="muted small">
              {t('Допишите в конец команды любое имя этого инстанса, например main. Агенты будут работать рядом, каждый со своей панелью.')}
            </p>
            <div className="ic-cmd-row">
              <InstallCmd cmd={cmd} token={enroll.token} add="main" />
              <button className="ghost ic-copy" disabled={pending} onClick={() => copy(`${cmd} main`, 'main')}>
                {copied === 'main' ? t('Скопировано ✓') : t('Копировать')}
              </button>
            </div>
          </details>
        </section>

        <section className={`ic-status${connected ? ' ic-status-ok' : ''}`}>
          <span className="ic-num">3</span>
          {connected ? (
            <>
              <span className="ic-ok-dot">✓</span>
              <span>
                <b>{t('Агент на связи')}</b>
                <span className="muted small">
                  {' '}
                  {t('{host}, v{ver}, метрики уже идут', {
                    host: connected.hostname || connected.name,
                    ver: connected.agent_version || '?',
                  })}
                </span>
              </span>
              <button className="ic-done" onClick={onClose}>
                {t('Готово')}
              </button>
            </>
          ) : (
            <>
              <span className="ic-pulse" />
              <span>
                <b>{t('Ждем первый отчет агента')}</b>
                <InstallProgress st={st} path={path} serverId={enroll.server.id} />
              </span>
            </>
          )}
        </section>
      </div>
    </div>
  )
}

// Что панель знает о новой ноде, пока агент молчит. Самое продвинутое: скачал агента >
// скачал установщик > адрес открыт или открывается. Адрес в фаерволе панели важен только для пути
// напрямую: через прокси и ретранслятор нода приходит с их адреса
function InstallProgress({ st, path, serverId }: { st: InstallStatus | null; path: InstallMode; serverId: number }) {
  const { t } = useI18n()
  const [ip, setIp] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const at = (iso: string) => new Date(iso).toLocaleTimeString(currentLang() === 'en' ? 'en-GB' : 'ru-RU', { hour: '2-digit', minute: '2-digit' })
  const line = (text: string, cls = 'muted') => <span className={`ic-sub small ${cls}`}>{text}</span>
  if (!st) return line(t('статус сменится здесь сам'))
  if (st.agent_at) return line(t('сервер скачал агента в {t}, ждем запуска', { t: at(st.agent_at) }))
  if (st.script_at) return line(t('сервер скачал установщик в {t}', { t: at(st.script_at) }))
  if (st.relay === 'opening') return line(t('ретранслятор пустит этот сервер в течение минуты'))
  if (st.relay === 'open') return line(t('ретранслятор уже пускает этот сервер'))
  if (path !== 'direct' || !st.firewall) return line(t('статус сменится здесь сам'))
  if (st.firewall === 'open') return line(t('адрес {ip} открыт в фаерволе панели', { ip: st.ip }))
  if (st.firewall === 'opening') return line(t('открываем адрес {ip} в фаерволе панели, обычно до 2 минут, команда подождет', { ip: st.ip }))
  if (st.firewall === 'stale')
    return line(
      t('адреса в фаерволе панели не обновлялись {n} мин: на хосте панели не запускается ops/agent-firewall-sync.sh', {
        n: Math.round((st.fw_age ?? 0) / 60),
      }),
      't-degraded',
    )
  // фаервол есть, а IP сервера не указан: открыть нечего, агент не пройдет
  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await installVia(serverId, { mode: 'direct', ip: ip.trim() })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="ic-sub ic-noip small">
      <span className="t-degraded">{t('IP сервера не указан, фаервол панели его не пустит:')}</span>
      <input
        className="mono"
        value={ip}
        placeholder={t('IP сервера')}
        onChange={(e) => setIp(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && ip.trim() && void save()}
      />
      <button className="ghost" disabled={!ip.trim() || busy} onClick={() => void save()}>
        {t('Открыть')}
      </button>
      {err && <span className="form-error">{err}</span>}
    </span>
  )
}

const RELAY_DEFAULT_PORT = 8790

// Сервер заведен, а агент так и не вышел на связь за 10 минут: скорее всего не достает до панели
function agentLate(s: Server): boolean {
  if (s.last_seen || !s.created_at) return false
  return Date.now() - new Date(s.created_at).getTime() > 10 * 60_000
}

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
      <code className="upd-cmd">{cmd}</code>
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
  relay_enabled: boolean // ретранслятор для агентов (app/relay.py), сохраняется отдельно (setRelay)
  relay_port: number // 0 - по умолчанию
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
              🔇 {t(labelOf(k))} · {muteAutoNote(k, t) ?? t('постоянно')}
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
        🔧 {t('Нужно действие на сервере')}
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
                {t('{label} (helper {helper}): {a} -> {b}', {
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
      <FixSteps steps={[{ id: 'run', cmd }]} text={() => t('Выполнить от root, одной командой')}
        foot={[
          command && t('OTA обновляет только бинарь, не юнит, поэтому вручную.'),
          helpers.length > 0 && t('Переустановка helper\'а ничего не ломает.'),
        ].filter(Boolean).join(' ')} />
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
  relay,
  host,
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
  host: string // имя ноды для ansible -l (как в "Требует действий")
  onSubmit: () => void
  onCancel: () => void
  onDelete: () => void
}) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  // пустое поле - NaN: при сохранении оно не уходит (раньше уходил 0 и бэкенд отвечал 422)
  const num = (v: string) => (v === '' ? NaN : Number(v))
  const nv = (x: number) => (Number.isFinite(x) ? x : '')
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
            value={nv(form.cpu_alert_percent)}
            onChange={(e) => set({ cpu_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>RAM</span>
          <input
            type="number"
            min={0}
            max={100}
            value={nv(form.mem_alert_percent)}
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
            value={nv(form.disk_warn_percent)}
            onChange={(e) => set({ disk_warn_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🔴 {t('проблема')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={nv(form.disk_alert_percent)}
            onChange={(e) => set({ disk_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🚨 {t('критично')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={nv(form.disk_crit_percent)}
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
            value={nv(form.temp_alert_c)}
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
            value={nv(form.conntrack_alert_percent)}
            onChange={(e) => set({ conntrack_alert_percent: num(e.target.value) })}
          />
        </label>
        <label className="field">
          <span>🔌 {t('Алерт по коннектам СУБД, % (0 = выкл)')}</span>
          <input
            type="number"
            min={0}
            max={100}
            value={nv(form.db_conn_alert_percent)}
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
            value={nv(form.web_5xx_alert_percent)}
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
              value={nv(form.disk_temp_alert_c)}
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
          value={nv(form.offline_after_seconds)}
          onChange={(e) => set({ offline_after_seconds: num(e.target.value) })}
        />
      </label>
      <label className="field">
        <span>{t('Алертить, только если держится дольше, мин')}</span>
        <input
          type="number"
          min={0}
          max={1440}
          value={nv(form.alert_sustain_min)}
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
      {/* ретранслятор: прокси до панели для агентов, чей адрес режут на пути к ней */}
      <label className="checkbox" id="edit-relay">
        <input
          type="checkbox"
          checked={form.relay_enabled}
          onChange={(e) => set({ relay_enabled: e.target.checked })}
        />
        <span>📡 {t('Ретранслятор для агентов')}</span>
      </label>
      <p className="muted small">
        {t('Сервер станет прокси до панели для агентов, чей адрес режут на пути к ней. Пускает только до панели, только с адресов серверов, выбравших его в окне команды установки, и только с логином. Ставит helper relay-setup (появится в "Требует действий"), нужен python3. Включить можно, только если сервер сам ходит в панель напрямую.')}
      </p>
      {form.relay_enabled && (
        <label className="field">
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
  useEscape(onClose)
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
    let alive = true // ответ за прошлый период или зум не перетирает выбранный
    const run = () =>
      serverMetrics(server.id, hours, zoom ?? undefined).then((v) => { if (alive) setMetrics(v) }).catch(() => {})
    run()
    // на зуме не автообновляем — фиксированное окно
    const id = zoom ? 0 : window.setInterval(run, 15000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
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

// Эпизод OOM из ссылки алерта (?server=id&sec=oom&oom=<эпизод>): адрес снят при запуске, пока
// список серверов грузится, панель успевает переписать строку без него. Берется один раз:
// первая открытая карточка его забывает
let linkOom = new URLSearchParams(window.location.search).get('oom')
const takeLinkOom = (): string | null => linkOom ?? new URLSearchParams(window.location.search).get('oom')

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
  const [enrollMode, setEnrollMode] = useState<InstallMode | undefined>(undefined)
  // отказ действия в окне (403, 404, 422, сеть): тостом, а не молча вернувшейся кнопкой
  const actFail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onUnauthorized()
    toastError(e)
  }
  const reissue = async (mode?: InstallMode) => {
    setEnrollMode(mode)
    try {
      setEnroll(await rotateServerToken(s.id))
    } catch (e) {
      actFail(e)
    }
  }
  const doUpdate = async (fn: () => Promise<Server[]>) => {
    setUpdBusy(true)
    try {
      await fn()
      onChanged()
    } catch (e) {
      actFail(e)
    } finally {
      setUpdBusy(false)
    }
  }
  const [metrics, setMetrics] = useState<ServerMetric[] | null>(null)
  const [hours, setHours] = useState(6)
  const [zoomKey, setZoomKey] = useState<MetricKey | null>(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<ServerEditForm | null>(null)
  // Esc закрывает карточку, но не во время правки: там он стер бы набранное
  useEscape(onClose, !editing)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const mutes0 = useRef<string[]>([]) // мьюты на момент открытия формы
  const r = s.last_report ?? {}

  useEffect(() => {
    setMetrics(null)
    // поздний ответ за прошлый период не должен лечь на выбранный ("90д", потом сразу "1ч")
    let alive = true
    const run = () => serverMetrics(s.id, hours).then((v) => { if (alive) setMetrics(v) }).catch(() => {})
    run()
    const id = window.setInterval(run, 12000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [s.id, hours])

  // открытый раздел: сводка или тот, куда вела ссылка; в адресе он виден (?sec=), ссылкой
  // на раздел можно поделиться
  const [active, setActive] = useState<string>(() => secOfLink(initialSection) ?? 'overview')
  useUrlParam('sec', active === 'overview' ? null : active)
  const [oomEps, setOomEps] = useState<OomEpisode[] | null>(null)
  const oomMuteSig = JSON.stringify([
    (s.alert_mutes ?? []).filter((k) => k === 'oom' || k.startsWith(OOM_MUTE)),
    Object.entries(s.alert_snoozes ?? {}).filter(([k]) => k === 'oom' || k.startsWith(OOM_MUTE)),
  ])
  const [oomOpen, setOomOpen] = useState<string | null>(() => takeLinkOom())
  useEffect(() => {
    linkOom = null
  }, [])
  useUrlParam('oom', active === 'mem' ? oomOpen : null)
  useEffect(() => {
    if (active !== 'mem') return
    let alive = true
    const run = () =>
      serverOom(s.id)
        .then((v) => { if (alive) setOomEps(v.episodes) })
        .catch(() => {})
    run()
    const id = window.setInterval(run, 60000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
    // мьют OOM поставили или сняли: эпизоды заново, их пометка "не алертит" считается на бэкенде
  }, [s.id, active, oomMuteSig])
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
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setSaveErr(e instanceof Error ? e.message : t('Ошибка'))
    }
  }

  // бэйдж "ретранслятор" в шапке: настройки сразу на галочке ретранслятора и его состоянии
  function editRelay() {
    startEdit()
    window.setTimeout(() => document.getElementById('edit-relay')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
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
      relay_enabled: !!s.relay_enabled,
      relay_port: s.relay_port ?? 0,
    })
    mutes0.current = s.alert_mutes ?? []
    setSaveErr(null)
    setEditing(true)
  }

  async function submitEdit() {
    if (!form) return
    setSaving(true)
    setSaveErr(null)
    try {
      // пустое поле не отправляем (остается как было), дробь в целом пороге округляем
      const int = (x: number) => (Number.isFinite(x) ? Math.round(x) : undefined)
      // мьюты: поверх текущих, а не снимка на момент открытия формы - заглушенное за это время
      // в другом месте не должно пропасть
      const was = new Set(mutes0.current)
      const keep = new Set(form.alert_mutes)
      const mutes = [...new Set([...(s.alert_mutes ?? []).filter((k) => keep.has(k) || !was.has(k)), ...form.alert_mutes])]
      await updateServer(s.id, {
        name: form.name.trim(),
        group_name: form.group_name.trim(),
        agent_ip: form.agent_ip.trim(),
        cpu_alert_percent: int(form.cpu_alert_percent),
        mem_alert_percent: int(form.mem_alert_percent),
        disk_alert_percent: int(form.disk_alert_percent),
        disk_warn_percent: int(form.disk_warn_percent),
        disk_crit_percent: int(form.disk_crit_percent),
        disk_autofix: form.disk_autofix,
        temp_alert_c: int(form.temp_alert_c),
        conntrack_alert_percent: int(form.conntrack_alert_percent),
        db_conn_alert_percent: int(form.db_conn_alert_percent),
        web_5xx_alert_percent: Number.isFinite(form.web_5xx_alert_percent) ? form.web_5xx_alert_percent : undefined,
        kube_expiry_warn_days: form.kube_expiry_warn_days.split(/[^0-9]+/).filter(Boolean).map(Number),
        disk_temp_alert_c: int(form.disk_temp_alert_c),
        alert_mutes: mutes,
        offline_after_seconds: int(form.offline_after_seconds),
        alert_sustain_seconds: Number.isFinite(form.alert_sustain_min) ? Math.max(0, Math.round(form.alert_sustain_min * 60)) : undefined,
      })
      // ретранслятор отдельно: панель проверяет, что сервер сам достает до нее, и может отказать
      if (form.relay_enabled !== !!s.relay_enabled || (form.relay_enabled && form.relay_port !== (s.relay_port ?? 0)))
        try {
          await setRelay(s.id, { enabled: form.relay_enabled, port: form.relay_port || 0 })
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) return onUnauthorized()
          onChanged() // остальное уже сохранено
          setSaveErr(e instanceof Error ? t(e.message) : t('Ошибка'))
          return
        }
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
  const chartCard = (key: MetricKey, extra?: React.ReactNode, marks?: ChartMark[]) => {
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
            marks={marks}
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
        onInstall={isViewer ? undefined : () => reissue()} />
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
          {p.sub === 'resolver'
            ? <FixSteps steps={[{ id: 'rm' }]} text={() => t('Убрать неработающий резолвер из настроек сети (netplan, /etc/resolv.conf) или заменить рабочим')}
                foot={t('Остальные отвечают и имена резолвятся, но запасного нет.')} />
            : r.dns?.mode === 'local'
              ? <FixSteps steps={[{ id: 'local' }]} text={() => t('Проверить свой резолвер ноды (unbound, dnsmasq) и серверы, куда он пересылает запросы')} />
              : r.dns?.mode === 'direct'
                ? <FixSteps steps={[{ id: 'direct' }]} text={() => t('Поставить первыми в /etc/resolv.conf публичные: nameserver 1.1.1.1 и 8.8.8.8')}
                    foot={t('Пока тормозят резолверы провайдера: запросы идут к резолверам по порядку.')} />
                : <FixSteps steps={[{ id: 'resolved', cmd: DNS_PUBLIC }]} text={() => t('Добавить публичные резолверы')}
                    foot={t('Пока тормозят резолверы провайдера: systemd-resolved спросит их вместе с резолверами сети и возьмет первый ответ.')} />}
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
      <SrvTiles r={r} M={M} info={info} onGo={go} gpu={s.gpu_sum} oom={s.oom} />
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
            {/* роли как в списке; состояние ретранслятора живет в настройках сервера: клик туда */}
            {!editing && <RoleChips s={s} servers={servers} t={t} onRelay={isViewer ? undefined : editRelay} />}
            {!editing && s.gpu_sum && (
              <GpuChip sum={s.gpu_sum} bad={issues.some((i) => i.kind === 'gpu' || i.kind === 'gpu_heat')} t={t} />
            )}
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
              relay={s.relay}
              host={hostOf(s)}
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
            {!isViewer && !s.first_report_at && agentLate(s) && (
              <div className="install-late">
                <span>{t('Агент не достает до панели? Поставьте его через ретранслятор или локацию.')}</span>
                <button className="small-btn" onClick={() => reissue('relay')}>{t('Выбрать путь')}</button>
              </div>
            )}
            {!isViewer && !s.first_report_at && (
              <>
                <button className="small-btn" onClick={() => reissue()}>{t('Команда установки')}</button>
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
              (oomEps ?? []).map((e) => ({
                t: e.start * 1000,
                label: 'OOM',
                title: `${new Date(e.start * 1000).toLocaleString()} · ${oomKillsLine(e, t)}`,
                tone: e.level >= 2 ? 'bad' : 'warn',
              })),
            )}
            {(r.swap_total ?? 0) > 0 &&
              chartCard(
                'swap',
                <div className="loc-results chart-stats">
                  <StatRow color="#57a894" name={t('из swap')} value={fmtRate(r.swap_in)} />
                  <StatRow color="#cf9b52" name={t('в swap')} value={fmtRate(r.swap_out)} />
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
          </div>
          <div className="net-cards">
            <OomBlock server={s} eps={oomEps} open={oomOpen} onOpen={setOomOpen} onChanged={onChanged} />
            <MemLeaks server={s} />
            <MemLimitsCard server={s} isViewer={isViewer} onChanged={onChanged} />
            <MemContainers server={s} onChanged={onChanged} />
            <MemPods server={s} onChanged={onChanged} />
          </div>
          </div>
        )}

        {active === 'disk' && (
          <div className="srv-sec">
          <div className="net-cards">
            <DiskSpaceCard server={s} r={r} onChanged={onChanged} />
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
              <SshLogin server={s} onChanged={onChanged} />
              <AccessCard server={s} onChanged={onChanged} />
            </div>
          </div>
        )}

        {active === 'updates' && (
          <div className="srv-sec">
            <OsUpdates server={s} onChanged={onChanged} />
            <SoftwareCard server={s} onChanged={onChanged} />
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
              <FailedUnits server={s} onChanged={onChanged} onOom={(ep) => { setOomOpen(ep); go('mem') }} />
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
                    await muteServerKeys(s.id, [], [kind])
                    onChanged()
                  } catch (e) {
                    actFail(e)
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
                      await muteServerKeys(s.id, kind === null ? SRV_ALERT_KINDS.map((x) => x.k) : [kind])
                    } else if (kind === null) await snoozeServer(s.id, hours)
                    else await snoozeServerAlert(s.id, kind, hours)
                    onChanged()
                  } catch (e) {
                    actFail(e)
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
                server={s}
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
      <InstallModal enroll={enroll} title={t('Команда установки')} mode={enrollMode} onClose={() => { setEnroll(null); onChanged() }} />
    )}
    </>
  )
}
