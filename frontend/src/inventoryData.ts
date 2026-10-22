import type { InvServer } from './api'
import { byteUnits } from './units'
import { xlsxBlob, type XlsxCell, type XlsxSheet } from './xlsx'

// Инвентаризация железа (backend app/inventory.py): форматы и итоги, общие для раздела в окне
// "Отчет", HTML-файла и Excel. Диски - в десятичных ТБ, как их продают (1.92 ТБ), память - в
// планках: MemTotal меньше установленной на резерв ядра, 251 ГБ - это 256

type T = (k: string, p?: Record<string, string | number>) => string

export type DiskKind = 'nvme' | 'ssd' | 'hdd' | 'virt' | ''
export const KINDS: DiskKind[] = ['nvme', 'ssd', 'hdd', 'virt', '']

export function kindLabel(k: DiskKind, t: T): string {
  return k === 'nvme' ? 'NVMe' : k === 'ssd' ? 'SSD' : k === 'hdd' ? 'HDD' : k === 'virt' ? t('вирт.') : t('диск')
}

// десятичные, три значащих цифры, как у производителя: 1920383410176 -> "1.92 ТБ",
// 16000900661248 -> "16 ТБ", 480103981056 -> "480 ГБ"
export function fmtSize(b: number): string {
  const u = byteUnits()
  if (b >= 1e12) return `${parseFloat((b / 1e12).toPrecision(3))} ${u[4]}`
  return `${Math.round(b / 1e9)} ${u[3]}`
}

// установленные объемы памяти: MemTotal округляем вверх до ближайшего, если он близко
const RAM_SIZES = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 160, 192, 256, 320, 384, 512, 640, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192]

export function ramGb(b: number): number {
  const g = b / 2 ** 30
  if (!g) return 0
  const up = RAM_SIZES.find((x) => x >= g)
  return up && g >= up * 0.88 ? up : Math.round(g)
}

export function fmtRam(b: number): string {
  const u = byteUnits()
  const g = ramGb(b)
  return g >= 1024 ? `${Math.round(g / 102.4) / 10} ${u[4]}` : `${g} ${u[3]}`
}

// "Intel(R) Xeon(R) Gold 6226R CPU @ 2.90GHz" -> "Intel Xeon Gold 6226R",
// "AMD EPYC 7502P 32-Core Processor" -> "AMD EPYC 7502P"
export function cpuName(m: string): string {
  return m
    .replace(/\((R|TM|tm|r)\)/g, '')
    .replace(/\s+CPU\s+@.*$/, '')
    .replace(/\s+@\s*[\d.]+\s*GHz$/i, '')
    .replace(/\s+\d+-Core Processor$/i, '')
    .replace(/\s+Processor(?=\s*\(|$)/i, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

// "Debian GNU/Linux 12 (bookworm)" -> "Debian 12", "Ubuntu 24.04.1 LTS" -> "Ubuntu 24.04.1"
export const osShort = (os: string) =>
  os.replace(/\s*GNU\/Linux/, '').replace(/\s+LTS\b/, '').replace(/\s*\([a-z ]+\)$/i, '').trim()

// "6.8.0-47-generic" -> "6.8.0-47": хвост сборки дистрибутива в инвентаризации не нужен
export const kernelShort = (k: string) => k.replace(/-(generic|amd64|arm64|cloud-amd64|default)$/, '')

export const ghz = (mhz: number | null, t: T) => (mhz ? t('{v} ГГц', { v: (mhz / 1000).toFixed(1) }) : '')

export function serverKind(s: InvServer, t: T): string {
  if (!s.virt) return 'Baremetal'
  return s.virt === 'VM' ? t('ВМ') : `${t('ВМ')} ${s.virt}`
}

export const uptimeDays = (s: InvServer) => Math.floor(s.uptime / 86400)

export type DiskGroup = { n: number; size: number; kind: DiskKind; model: string }

// одинаковые диски одной строкой: "2x 1.9 ТБ NVMe"; byModel - отдельно по моделям (полный отчет)
export function diskGroups(s: InvServer, byModel = false): DiskGroup[] {
  const m = new Map<string, DiskGroup>()
  for (const d of s.disks ?? []) {
    const k = `${d.kind}|${d.size}|${byModel ? d.model : ''}`
    const g = m.get(k)
    if (g) g.n++
    else m.set(k, { n: 1, size: d.size, kind: d.kind, model: byModel ? d.model : '' })
  }
  return [...m.values()].sort((a, b) => KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind) || b.size - a.size)
}

export const diskGroupText = (g: DiskGroup, t: T) =>
  `${g.n > 1 ? `${g.n}x ` : ''}${g.size ? fmtSize(g.size) : '?'} ${kindLabel(g.kind, t)}`

export function disksText(s: InvServer, t: T): string {
  if (!s.disks) return s.fs_total ? `${fmtSize(s.fs_total)} ${t('по разделам')}` : ''
  return diskGroups(s)
    .map((g) => diskGroupText(g, t))
    .join(', ')
}

export const raidText = (r: InvServer['raid'][number]) =>
  [r.level, r.map, r.sync].filter(Boolean).join(' ') || r.dev

// метки RAID рядом с дисками: одинаковые массивы одной меткой "raid1 [UU] x2", массив без диска - bad
export function raidLabels(raid: InvServer['raid']): { text: string; bad: boolean }[] {
  const m = new Map<string, { n: number; bad: boolean }>()
  for (const r of raid) {
    const k = raidText(r)
    const x = m.get(k)
    if (x) x.n++
    else m.set(k, { n: 1, bad: r.degraded })
  }
  return [...m.entries()].map(([k, x]) => ({ text: x.n > 1 ? `${k} x${x.n}` : k, bad: x.bad }))
}

// серверы по группам в порядке списка (бэкенд уже отсортировал по группе и имени), серверы
// без группы - последними: ключ пустой или ниже любого символа
export function byGroup(list: InvServer[], key: (s: InvServer) => string = (s) => s.group): [string, InvServer[]][] {
  const m = new Map<string, InvServer[]>()
  for (const s of list) {
    const k = key(s)
    const xs = m.get(k)
    if (xs) xs.push(s)
    else m.set(k, [s])
  }
  const noGroup = (k: string) => !k || k === '\u0000'
  return [...m.entries()].sort((a, b) => Number(noGroup(a[0])) - Number(noGroup(b[0])))
}

export type InvTotals = {
  n: number
  bare: number
  vm: number
  threads: number
  mem: number // ГБ, по планкам
  disks: Partial<Record<DiskKind, { n: number; size: number }>>
  unknown: number // серверов без helper'а disk-health
  gpus: Record<string, number>
}

export function invTotals(list: InvServer[]): InvTotals {
  const r: InvTotals = { n: list.length, bare: 0, vm: 0, threads: 0, mem: 0, disks: {}, unknown: 0, gpus: {} }
  for (const s of list) {
    if (s.reported) {
      if (s.virt) r.vm++
      else r.bare++
    }
    r.threads += s.threads
    r.mem += ramGb(s.mem)
    if (!s.disks) {
      if (s.reported) r.unknown++
    } else
      for (const d of s.disks) {
        const x = (r.disks[d.kind] ??= { n: 0, size: 0 })
        x.n++
        x.size += d.size
      }
    for (const g of s.gpus) r.gpus[g] = (r.gpus[g] ?? 0) + 1
  }
  return r
}

export const fmtRamGb = (g: number) => {
  const u = byteUnits()
  return g >= 1024 ? `${Math.round(g / 102.4) / 10} ${u[4]}` : `${g} ${u[3]}`
}

export const gpuShort = (name: string) => name.replace(/^NVIDIA\s+/, '').replace(/^(GeForce|Tesla)\s+/, '')

// --- Excel ---

const tb = (b: number) => Math.round(b / 1e10) / 100

export function inventoryXlsx(list: InvServer[], full: boolean, t: T): Blob {
  const tot = invTotals(list)
  const sheets: XlsxSheet[] = []
  if (!full) {
    const rows: XlsxCell[][] = list.map((s) => [
      s.name, s.group, s.ip, s.threads || null, ramGb(s.mem) || null, disksText(s, t), osShort(s.os),
    ])
    rows.push([
      t('Итого: {n}', { n: tot.n }), '', '', tot.threads, tot.mem,
      KINDS.filter((k) => tot.disks[k]).map((k) => `${tot.disks[k]!.n}x ${kindLabel(k, t)}`).join(', '), '',
    ])
    sheets.push({
      name: t('Кратко'),
      head: [t('Сервер'), t('Группа'), 'IP', 'CPU', t('RAM, ГБ'), t('Диски'), t('ОС')],
      rows,
      widths: [22, 14, 16, 7, 10, 34, 24],
      totalRow: true,
    })
  } else {
    sheets.push({
      name: t('Полно'),
      head: [
        t('Сервер'), t('Группа'), 'IP', t('Локальный IP'), t('Страна'), t('Тип'), t('Процессор'), t('Потоков'),
        t('Частота, ГГц'), t('RAM, ГБ'), t('Дисков'), t('Диски, ТБ'), t('Диски'), 'RAID', 'GPU', t('ОС'), t('Ядро'),
        t('Аптайм, дн'),
      ],
      rows: list.map((s) => [
        s.name, s.group, s.ip, s.local_ip, s.country, s.reported ? serverKind(s, t) : '', s.cpu_model,
        s.threads || null, s.freq_mhz ? Math.round(s.freq_mhz / 100) / 10 : null, ramGb(s.mem) || null,
        s.disks ? s.disks.length : null, s.disks ? tb(s.disks.reduce((a, d) => a + d.size, 0)) : null,
        disksText(s, t), s.raid.map((r) => `${r.dev} ${raidText(r)}`).join(', '),
        Object.entries(countBy(s.gpus)).map(([g, n]) => `${n}x ${g}`).join(', '), s.os, s.kernel,
        s.reported ? uptimeDays(s) : null,
      ]),
      widths: [22, 14, 16, 14, 8, 14, 40, 9, 12, 9, 8, 10, 34, 24, 28, 24, 22, 11],
    })
    sheets.push({
      name: t('Диски'),
      head: [t('Сервер'), t('Устройство'), t('Модель'), t('Серийный номер'), t('Тип'), t('Размер, ТБ')],
      rows: list.flatMap((s) =>
        (s.disks ?? []).map((d) => [s.name, d.dev, d.model, d.serial, kindLabel(d.kind, t), d.size ? tb(d.size) : null]),
      ),
      widths: [22, 12, 32, 22, 8, 11],
    })
  }
  const sum: XlsxCell[][] = [
    [t('Серверов'), tot.n],
    ['Baremetal', tot.bare],
    [t('ВМ'), tot.vm],
    [t('CPU (потоков)'), tot.threads],
    [t('RAM, ГБ'), tot.mem],
  ]
  for (const k of KINDS) {
    const d = tot.disks[k]
    if (d) sum.push([t('{kind}: дисков', { kind: kindLabel(k, t) }), d.n], [t('{kind}: ТБ', { kind: kindLabel(k, t) }), tb(d.size)])
  }
  for (const [g, n] of Object.entries(tot.gpus)) sum.push([`GPU ${g}`, n])
  if (tot.unknown) sum.push([t('Без helper disk-health, диски неизвестны'), tot.unknown])
  sheets.push({ name: t('Итоги'), head: [t('Показатель'), t('Значение')], rows: sum, widths: [40, 14] })
  // подытоги групп: сколько железа у каждой
  const kinds = KINDS.filter((k) => tot.disks[k])
  const grow = (name: string, x: InvTotals): XlsxCell[] => [
    name, x.n, x.threads, x.mem, ...kinds.map((k) => (x.disks[k] ? tb(x.disks[k]!.size) : null)),
    Object.values(x.gpus).reduce((a, b) => a + b, 0) || null,
  ]
  sheets.push({
    name: t('По группам'),
    head: [t('Группа'), t('Серверов'), 'CPU', t('RAM, ГБ'), ...kinds.map((k) => t('{kind}: ТБ', { kind: kindLabel(k, t) })), 'GPU'],
    rows: [...byGroup(list).map(([g, xs]) => grow(g || t('без группы'), invTotals(xs))), grow(t('Итого'), tot)],
    widths: [22, 10, 8, 10, ...kinds.map(() => 11), 7],
    totalRow: true,
  })
  return xlsxBlob(sheets)
}

export function countBy(xs: string[]): Record<string, number> {
  const r: Record<string, number> = {}
  for (const x of xs) r[x] = (r[x] ?? 0) + 1
  return r
}

export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
