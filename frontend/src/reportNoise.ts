import type { ReportNoise } from './api'

// Общее для "что шумело" в окне отчета и в скачанном HTML: группы по объекту, когда, сколько раз.

type T = (k: string, p?: Record<string, string | number>) => string

// "4 раза", "5 раз": по-русски форма зависит от числа
export function times(n: number, t: T): string {
  const few = n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
  return few ? t('{n} раза', { n }) : t('{n} раз', { n })
}

const dd = (iso: string) => new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit' })
const ddhm = (iso: string) =>
  new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

// когда: один раз - день и время, иначе дни "13.10-15.10" (или один день)
export function noiseWhen(x: ReportNoise): string {
  if (x.n === 1) return ddhm(x.last)
  const a = dd(x.first)
  const b = dd(x.last)
  return a === b ? a : `${a}-${b}`
}

// группы по объекту (сервер или монитор), самые шумные первыми
export function noiseGroups(items: ReportNoise[]): ReportNoise[][] {
  const by = new Map<string, ReportNoise[]>()
  for (const x of items) {
    const k = `${x.section === 'sites' ? 'c' : 's'}|${x.target}`
    by.set(k, [...(by.get(k) ?? []), x])
  }
  const sum = (g: ReportNoise[]) => g.reduce((a, x) => a + x.n, 0)
  return [...by.values()].sort((a, b) => sum(b) - sum(a))
}

export const isBackup = (x: ReportNoise) => x.kind.startsWith('backup')
