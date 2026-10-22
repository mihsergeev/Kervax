import type { Report, ReportNoise } from './api'
import { isBackup, noiseGroups, noiseWhen, times } from './reportNoise'

// Отчет одним HTML-файлом для передачи: все стили внутри, без внешних шрифтов и скриптов,
// светлая и темная тема по настройке системы, читается на телефоне и печатается.

type T = (k: string, p?: Record<string, string | number>) => string

// файл уходит людям: типографские знаки из названий и дат заменяем обычными
const PLAIN: [RegExp, string][] = [
  [/[–—]/g, '-'],
  [/[«»“”„]/g, '"'],
  [/[→⇒]/g, '->'],
  [/…/g, '...'],
  [/·/g, ','],
  [/×/g, 'x'],
  [/≈/g, '~'],
  [/≥/g, '>='],
  [/≤/g, '<='],
  [/[   ]/g, ' '],
  [/ё/g, 'е'],
  [/Ё/g, 'Е'],
]
export const plain = (s: string) => PLAIN.reduce((a, [re, to]) => a.replace(re, to), s)

export const esc = (s: string) =>
  plain(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

// длительность без длинного тире: файл уходит людям
function dur(s: number, t: T): string {
  if (!s) return '-'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h) return t('{h} ч {m} мин', { h, m })
  if (m) return t('{m} мин {s} с', { m, s: s % 60 })
  return t('{s} с', { s })
}

const when = (iso: string) =>
  new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
const short = (iso: string) =>
  new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
const day = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`

type Series = { label: string; cls: string; values: number[] }

function bars(labels: string[], series: Series[], fmt: (n: number) => string, t: T): string {
  const sums = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0))
  const max = Math.max(...sums, 0)
  const every = labels.length > 16 ? 3 : labels.length > 9 ? 2 : 1
  const cols = labels
    .map((d, i) => {
      const segs = series
        .filter((s) => s.values[i])
        .map((s) => `<span class="seg ${s.cls}" style="height:${((100 * s.values[i]) / (max || 1)).toFixed(1)}%"></span>`)
        .join('')
      const tip = series.map((s) => `${s.label} ${fmt(s.values[i] ?? 0)}`).join(', ')
      return `<div class="col" title="${esc(`${day(d)}: ${tip}`)}">${segs}</div>`
    })
    .join('')
  const xs = labels.map((d, i) => `<span>${i % every === 0 ? day(d) : ''}</span>`).join('')
  const legend = series
    .map((s) => `<span><i class="sw ${s.cls}"></i>${esc(s.label)}: ${esc(fmt(s.values.reduce((a, b) => a + b, 0)))}</span>`)
    .join('')
  return `<div class="chart"><span class="max">${max ? esc(fmt(max)) : ''}</span>${cols}</div>
<div class="xs">${xs}</div>
<div class="legend">${legend}${max === 0 ? `<span class="muted">${esc(t('за период пусто'))}</span>` : ''}</div>`
}

// широкие таблицы на телефоне раскладываются строками "подпись: значение"
function table(head: string[], rows: string[][], num: number[] = []): string {
  const th = head.map((h, i) => `<th${num.includes(i) ? ' class="num"' : ''}>${esc(h)}</th>`).join('')
  const td = (c: string, i: number) => {
    const cls = [num.includes(i) ? 'num' : '', c === '-' || c === '' ? 'nil' : ''].filter(Boolean).join(' ')
    return `<td${cls ? ` class="${cls}"` : ''} data-l="${esc(head[i])}">${c}</td>`
  }
  const tr = rows.map((r) => `<tr>${r.map(td).join('')}</tr>`).join('')
  return `<div class="tw"><table${head.length > 3 ? ' class="stack"' : ''}><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`
}

// что шумело: объект строкой-заголовком, под ним что именно, почему и когда; редкое у
// сервера после первых строк - одной строкой "и еще N", файл читают, а не листают
const PER_TARGET = 10

function noiseTable(items: ReportNoise[], t: T): string {
  const rows = noiseGroups(items)
    .map((g) => {
      const head = `<tr class="grp"><td colspan="2"><span class="mono">${esc(g[0].target)}</span></td></tr>`
      const lines = g
        .slice(0, PER_TARGET)
        .map(
          (x) =>
            `<tr><td><div>${esc(x.what)}</div>${x.note ? `<div class="muted small">${esc(x.note)}</div>` : ''}</td>` +
            `<td class="num small"><div>${esc(noiseWhen(x))}</div>${x.n > 1 ? `<div class="muted">${esc(times(x.n, t))}</div>` : ''}</td></tr>`,
        )
        .join('')
      const more = g.length > PER_TARGET ? `<tr><td colspan="2" class="muted small">${esc(t('и еще {n}', { n: g.length - PER_TARGET }))}</td></tr>` : ''
      return head + lines + more
    })
    .join('')
  return `<div class="tw"><table class="noise"><tbody>${rows}</tbody></table></div>`
}

function tile(label: string, value: string, sub: string, tone = ''): string {
  return `<div class="tile${tone ? ` ${tone}` : ''}"><div class="tl">${esc(label)}</div><div class="tv">${esc(value)}</div><div class="ts">${esc(sub)}</div></div>`
}

export function reportHtml(r: Report, t: T, brand: string): string {
  const title = t('Отчет {brand}', { brand })
  const period = `${when(r.since)} - ${when(r.until)}`
  const sites = r.sites
  const servers = r.servers
  const noise = r.noise
  const chart = r.day_labels.length >= 7
  const parts: string[] = []

  const tiles: string[] = []
  if (sites)
    tiles.push(
      tile(
        t('Сайты, средний аптайм'),
        sites.uptime != null ? `${sites.uptime.toFixed(2)}%` : '-',
        sites.incidents ? t('сбоев: {n}, лежали {d}', { n: sites.incidents, d: dur(sites.downtime_s, t) }) : t('сбоев не было'),
        sites.incidents ? 'warn' : 'ok',
      ),
    )
  if (servers)
    tiles.push(
      tile(
        t('Серверы, недоступность'),
        servers.offline_s ? dur(servers.offline_s, t) : '0',
        t('пропадали: {n} из {m}', { n: servers.with_gaps, m: servers.total }),
        servers.offline_s ? 'warn' : 'ok',
      ),
    )
  tiles.push(tile(t('Алертов пришло'), String(noise.fires), t('без отбоев')))
  parts.push(`<div class="tiles">${tiles.join('')}</div>`)
  if (servers && servers.panel_s > 0)
    parts.push(
      `<p class="note">${esc(t('Панель не принимала отчеты {d}, раз: {n}. В простой серверов это не входит.', { d: dur(servers.panel_s, t), n: servers.panel_gaps }))}</p>`,
    )

  if (chart && (sites?.downtime_s || servers?.offline_s)) {
    const ser: Series[] = []
    if (sites?.downtime_s) ser.push({ label: t('простой сайтов'), cls: 'c-sites', values: sites.per_day })
    if (servers?.offline_s) ser.push({ label: t('простой серверов'), cls: 'c-servers', values: servers.per_day })
    parts.push(`<h2>${esc(t('Простой по дням'))}</h2>${bars(r.day_labels, ser, (n) => (n ? dur(n, t) : '0'), t)}`)
  }
  if (chart)
    parts.push(
      `<h2>${esc(t('Алерты по дням'))}</h2>${bars(r.day_labels, [{ label: t('алертов'), cls: 'c-alerts', values: noise.per_day }], (n) => String(n), t)}`,
    )

  if (sites) {
    parts.push(`<h2>${esc(t('Сайты со сбоями'))}</h2>`)
    parts.push(
      sites.items.length === 0
        ? `<p class="muted">${esc(t('Все {n} мониторов отработали без сбоев', { n: sites.total }))}</p>`
        : table(
            [t('Монитор'), t('Аптайм'), t('Сбоев'), t('Лежал'), t('Самый долгий'), t('Последний')],
            sites.items.map((c) => [
              esc(c.name) + (c.open ? ` <span class="chip bad">${esc(t('лежит сейчас'))}</span>` : ''),
              c.uptime != null ? `${c.uptime.toFixed(2)}%` : '-',
              c.incidents ? String(c.incidents) : '-',
              esc(dur(c.downtime_s, t)),
              esc(dur(c.longest_s, t)),
              c.last_at ? esc(short(c.last_at)) : '-',
            ]),
            [1, 2, 3, 4, 5],
          ) +
            `<p class="muted">${esc(t('Остальные {n} без сбоев', { n: sites.total - sites.affected }))}${sites.more > 0 ? `, ${esc(t('и еще {n} со сбоями', { n: sites.more }))}` : ''}</p>`,
    )
  }
  if (servers) {
    // никто не пропадал - колонки недоступности пустые, не показываем их
    const anyOff = servers.items.some((s) => s.offline_s > 0)
    parts.push(`<h2>${esc(t('Серверы'))}</h2>`)
    parts.push(
      servers.items.length === 0
        ? `<p class="muted">${esc(t('Серверы не пропадали, алертов по ним не было'))}</p>`
        : table(
            anyOff ? [t('Сервер'), t('Недоступен'), t('Раз'), t('Самый долгий'), t('Что было')] : [t('Сервер'), t('Что было')],
            servers.items.map((s) => {
              const things = s.things
                .map((x) => `<div>${esc(x.what)}${x.n > 1 ? ` <span class="muted">${esc(times(x.n, t))}</span>` : ''}</div>`)
                .join('')
              const name = `<span class="mono">${esc(s.name)}</span>`
              return anyOff
                ? [name, esc(dur(s.offline_s, t)), s.gaps ? String(s.gaps) : '-', esc(dur(s.longest_s, t)), things]
                : [name, things]
            }),
            anyOff ? [1, 2, 3] : [],
          ),
    )
  }
  // бэкапы - своим разделом, в "что шумело" их не повторяем
  const other = noise.items.filter((x) => !isBackup(x))
  const backups = noise.items.filter(isBackup)
  if (other.length) {
    parts.push(`<h2>${esc(t('Что шумело'))}</h2>`)
    parts.push(noiseTable(other, t))
    if (noise.more > 0) parts.push(`<p class="muted">${esc(t('И еще {n} реже - в истории алертов.', { n: noise.more }))}</p>`)
  }
  if (backups.length) {
    parts.push(`<h2>${esc(t('Бэкапы: что ломалось'))}</h2>`)
    parts.push(noiseTable(backups, t))
  }
  if (r.maint.length) {
    parts.push(`<h2>${esc(t('Окна обслуживания'))}</h2>`)
    parts.push(
      table(
        [t('Окно'), t('Когда'), t('Затронет')],
        r.maint.map((m) => [
          esc(m.title),
          esc(
            m.count > 1
              ? t('{n} раз, последний {t}', { n: m.count, t: `${short(m.start)} - ${short(m.end)}` })
              : `${short(m.start)} - ${short(m.end)}`,
          ),
          esc(
            [m.servers ? t('серверов {n}', { n: m.servers }) : '', m.checks ? t('мониторов {n}', { n: m.checks }) : '']
              .filter(Boolean)
              .join(', '),
          ),
        ]),
      ),
    )
  }

  return `<!doctype html>
<html lang="${esc(document.documentElement.lang || 'ru')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}: ${esc(period)}</title>
<style>
:root { --bg: #f6f7f9; --card: #ffffff; --text: #1d2330; --muted: #677085; --border: #dfe3ea;
  --up: #1f9d55; --down: #d93a3a; --warn: #c98a12; --accent: #3d63dd; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #11141b; --card: #181c25; --text: #e4e8f0; --muted: #8d96a8; --border: #2a303d;
    --up: #3fbf74; --down: #f06060; --warn: #e2a93b; --accent: #6b8cff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.page { max-width: 980px; margin: 0 auto; padding: 24px 16px 40px; }
header { margin-bottom: 18px; }
h1 { margin: 0 0 4px; font-size: 22px; }
h2 { margin: 26px 0 10px; font-size: 16px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.noise .grp td { padding-top: 12px; font-weight: 600; background: color-mix(in srgb, var(--border) 35%, transparent); }
.noise td.num { width: 1%; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
.tile { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.tile.warn { border-color: color-mix(in srgb, var(--warn) 45%, var(--border)); }
.tl { color: var(--muted); font-size: 12px; }
.tv { font-size: 24px; font-weight: 700; margin: 2px 0; }
.ts { color: var(--muted); font-size: 12px; }
.note { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--card); }
.chart { position: relative; display: flex; align-items: flex-end; gap: 2px; height: 120px; padding-top: 14px;
  border-bottom: 1px solid var(--border); background: repeating-linear-gradient(to top, transparent 0,
  transparent calc(25% - 1px), color-mix(in srgb, var(--border) 70%, transparent) calc(25% - 1px),
  color-mix(in srgb, var(--border) 70%, transparent) 25%); }
.max { position: absolute; top: 0; left: 0; font-size: 11px; color: var(--muted); }
.col { flex: 1 1 0; min-width: 0; height: 100%; display: flex; flex-direction: column-reverse; }
.seg { display: block; width: 100%; min-height: 2px; }
.c-alerts { background: var(--accent); }
.c-sites { background: var(--down); }
.c-servers { background: var(--warn); }
.xs { display: flex; gap: 2px; margin-top: 3px; font-size: 11px; color: var(--muted); }
.xs span { flex: 1 1 0; min-width: 0; text-align: center; white-space: nowrap; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 6px; font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.tw { overflow-x: auto; background: var(--card); border: 1px solid var(--border); border-radius: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.chip { display: inline-block; padding: 0 6px; border-radius: 6px; font-size: 11px; background: color-mix(in srgb, var(--border) 70%, transparent); white-space: nowrap; }
.chip.bad { color: var(--down); background: color-mix(in srgb, var(--down) 12%, transparent); }
footer { margin-top: 30px; color: var(--muted); font-size: 12px; }
@media (max-width: 560px) {
  th, td { padding: 6px 7px; } .tv { font-size: 20px; }
  .mono { white-space: nowrap; } .stack .chip { white-space: normal; }
  .stack thead { display: none; }
  .stack tr { display: block; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  .stack tr:last-child { border-bottom: 0; }
  .stack td { display: block; padding: 2px 0; border: 0; text-align: right; white-space: normal; }
  .stack td::before { content: attr(data-l); float: left; margin-right: 12px; color: var(--muted); }
  .stack td:first-child { text-align: left; font-weight: 600; }
  .stack td:first-child::before, .stack td.nil { display: none; }
}
@media print {
  :root { --bg: #fff; --card: #fff; --text: #000; --muted: #555; --border: #ccc; color-scheme: light; }
  body { font-size: 12px; } .page { max-width: none; padding: 0; }
  .tw, .tile, .chart, table, tr { break-inside: avoid; }
  .tw { overflow: visible; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
</style>
</head>
<body>
<div class="page">
<header>
<h1>${esc(title)}</h1>
<div class="muted">${esc(t('За {n} дн.: {p}', { n: r.days, p: period }))}</div>
</header>
${parts.join('\n')}
<footer>${esc(t('Сформирован {d}', { d: when(new Date().toISOString()) }))}</footer>
</div>
</body>
</html>
`
}

export function downloadReportHtml(r: Report, t: T, brand: string): void {
  const html = reportHtml(r, t, brand)
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${brand.toLowerCase().replace(/[^a-z0-9\u0430-\u044f]+/gi, '-') || 'kervax'}-report-${r.since.slice(0, 10)}-${r.until.slice(0, 10)}.html`
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
