import type { InvServer } from './api'
import {
  cpuName,
  countBy,
  diskGroupText,
  diskGroups,
  disksText,
  fmtRam,
  fmtRamGb,
  fmtSize,
  ghz,
  gpuShort,
  invTotals,
  kernelShort,
  KINDS,
  kindLabel,
  osShort,
  raidText,
  serverKind,
  uptimeDays,
} from './inventoryData'
import { esc } from './reportHtml'

// Инвентаризация одним HTML-файлом для передачи, как отчет за период (reportHtml.ts): стили
// внутри, без внешних шрифтов и скриптов, светлая и темная тема по системе, печать.
// Краткий - строка на сервер; полный - модели процессоров и дисков, RAID, GPU, ядро, аптайм

type T = (k: string, p?: Record<string, string | number>) => string

const when = (d: Date) =>
  d.toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })

const noData = (t: T, cols: number) => `<td colspan="${cols}" class="muted">${esc(t('агент еще не присылал данные'))}</td>`

function shortRow(s: InvServer, t: T): string {
  if (!s.reported)
    return `<tr><td><b>${esc(s.name)}</b>${s.group ? ` <span class="muted">${esc(s.group)}</span>` : ''}</td><td class="mono">${esc(s.ip)}</td>${noData(t, 4)}</tr>`
  return `<tr><td><b>${esc(s.name)}</b>${s.group ? ` <span class="muted">${esc(s.group)}</span>` : ''}</td>
<td class="mono">${esc(s.ip)}</td><td class="num">${s.threads || ''}</td><td class="num">${s.mem ? esc(fmtRam(s.mem)) : ''}</td>
<td>${esc(disksText(s, t))}</td><td>${esc(osShort(s.os))}</td></tr>`
}

function fullRow(s: InvServer, t: T, raid: boolean): string {
  const gpus = Object.entries(countBy(s.gpus))
    .map(([g, n]) => `${n > 1 ? `${n}x ` : ''}${gpuShort(g)}`)
    .join(', ')
  const disks = s.disks
    ? diskGroups(s, true)
        .map((g) => `<div>${esc(diskGroupText(g, t))}${g.model ? ` <span class="muted">${esc(g.model)}</span>` : ''}</div>`)
        .join('')
    : `<span class="muted">${esc(disksText(s, t))}</span>`
  const cpu = s.cpu_model
    ? `${esc(cpuName(s.cpu_model))} <span class="muted">${esc(
        [s.virt ? `${s.threads} vCPU` : t('{n} пот.', { n: s.threads }), ghz(s.freq_mhz, t)].filter(Boolean).join(', '),
      )}</span>`
    : ''
  return `<tr><td><b>${esc(s.name)}</b>${s.group ? ` <span class="muted">${esc(s.group)}</span>` : ''}${
    s.reported ? ` <span class="chip">${esc(serverKind(s, t))}</span>` : ''
  }<div class="mono muted">${esc(s.ip)}${s.country ? ` ${esc(s.country)}` : ''}${s.local_ip ? `, ${esc(s.local_ip)}` : ''}</div></td>
${!s.reported ? noData(t, raid ? 6 : 5) : `<td>${cpu}${gpus ? `<div><span class="muted">GPU</span> ${esc(gpus)}</div>` : ''}</td>
<td class="num">${s.mem ? esc(fmtRam(s.mem)) : ''}</td><td class="nw">${disks}</td>
${raid ? `<td class="nw">${s.raid.map((r) => `<div${r.degraded ? ' class="bad"' : ''}>${esc(raidText(r))}</div>`).join('')}</td>` : ''}
<td class="nw">${esc(osShort(s.os))}${s.kernel ? ` <span class="muted mono">${esc(kernelShort(s.kernel))}</span>` : ''}</td>
<td class="num">${esc(t('{n} дн', { n: uptimeDays(s) }))}</td>`}</tr>`
}

export function inventoryHtml(list: InvServer[], full: boolean, scope: string, t: T, brand: string): string {
  const tot = invTotals(list)
  const title = t('Инвентаризация {brand}', { brand })
  const tile = (k: string, v: string, sub = '') =>
    `<div class="tile"><div class="tl">${esc(k)}</div><div class="tv">${esc(v)}</div>${sub ? `<div class="ts">${esc(sub)}</div>` : ''}</div>`
  const tiles = [
    tile(t('Серверов'), String(tot.n), `Baremetal ${tot.bare}, ${t('ВМ')} ${tot.vm}`),
    tile(t('CPU (потоков)'), String(tot.threads)),
    tile('RAM', fmtRamGb(tot.mem)),
    ...KINDS.filter((k) => tot.disks[k]).map((k) =>
      tile(kindLabel(k, t), fmtSize(tot.disks[k]!.size), t('дисков: {n}', { n: tot.disks[k]!.n })),
    ),
    ...(Object.keys(tot.gpus).length
      ? [tile('GPU', String(Object.values(tot.gpus).reduce((a, b) => a + b, 0)), Object.entries(tot.gpus).map(([g, n]) => `${n}x ${gpuShort(g)}`).join(', '))]
      : []),
  ]
  const raid = full && list.some((s) => s.raid.length)
  const head = full
    ? [t('Сервер / IP'), t('Процессор'), 'RAM', t('Диски'), ...(raid ? ['RAID'] : []), t('ОС / ядро'), t('Аптайм')]
    : [t('Сервер'), 'IP', 'CPU', 'RAM', t('Диски'), t('ОС')]
  const numCols = full ? [2, head.length - 1] : [2, 3]
  const th = head.map((h, i) => `<th${numCols.includes(i) ? ' class="num"' : ''}>${esc(h)}</th>`).join('')
  const body = list.map((s) => (full ? fullRow(s, t, raid) : shortRow(s, t))).join('\n')
  const foot = full
    ? ''
    : `<tfoot><tr><td colspan="2">${esc(t('Итого: {n}', { n: tot.n }))}</td><td class="num">${tot.threads}</td><td class="num">${esc(fmtRamGb(tot.mem))}</td><td>${esc(
        KINDS.filter((k) => tot.disks[k]).map((k) => `${tot.disks[k]!.n}x ${kindLabel(k, t)}, ${fmtSize(tot.disks[k]!.size)}`).join('; '),
      )}</td><td></td></tr></tfoot>`
  const note = tot.unknown
    ? `<p class="muted">${esc(t('Серверов без helper disk-health: {n}. У них объем дисков по разделам, тип и модели неизвестны.', { n: tot.unknown }))}</p>`
    : ''

  return `<!doctype html>
<html lang="${esc(document.documentElement.lang || 'ru')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>
:root { --bg: #f6f7f9; --card: #ffffff; --text: #1d2330; --muted: #677085; --border: #dfe3ea; --head: #f0f2f6;
  --down: #d93a3a; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #11141b; --card: #181c25; --text: #e4e8f0; --muted: #8d96a8; --border: #2a303d; --head: #1d222d; --down: #f06060; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.page { max-width: ${full ? 1280 : 1040}px; margin: 0 auto; padding: 24px 16px 40px; }
h1 { margin: 0 0 4px; font-size: 22px; }
header { margin-bottom: 16px; }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 14px; }
.tile { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; }
.tl { color: var(--muted); font-size: 12px; }
.tv { font-size: 20px; font-weight: 700; }
.ts { color: var(--muted); font-size: 12px; }
.tw { overflow-x: auto; background: var(--card); border: 1px solid var(--border); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 5px 9px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: var(--head); font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tfoot td { font-weight: 600; background: var(--head); border-top: 1px solid var(--border); border-bottom: 0; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.nw { white-space: nowrap; }
.bad { color: var(--down); }
.chip { display: inline-block; padding: 0 6px; border-radius: 5px; font-size: 11px; background: color-mix(in srgb, var(--border) 70%, transparent); white-space: nowrap; }
footer { margin-top: 24px; color: var(--muted); font-size: 12px; }
@media print {
  :root { --bg: #fff; --card: #fff; --text: #000; --muted: #555; --border: #ccc; --head: #eee; color-scheme: light; }
  body { font-size: 11px; } .page { max-width: none; padding: 0; }
  table { font-size: 11px; } th, td { padding: 3px 6px; }
  .tw { overflow: visible; border: 0; } tr, .tile { break-inside: avoid; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
</style>
</head>
<body>
<div class="page">
<header>
<h1>${esc(title)}</h1>
<div class="muted">${esc(full ? t('Полная') : t('Краткая'))}, ${esc(when(new Date()))}${scope ? `. ${esc(scope)}` : ''}</div>
</header>
<div class="tiles">${tiles.join('')}</div>
<div class="tw"><table><thead><tr>${th}</tr></thead><tbody>
${body}
</tbody>${foot}</table></div>
${note}
<footer>${esc(t('Сформирован {d}', { d: when(new Date()) }))}</footer>
</div>
</body>
</html>
`
}
