import type { InvServer } from './api'
import {
  byGroup,
  countBy,
  cpuName,
  diskGroupText,
  diskGroups,
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
  raidLabels,
  serverKind,
  uptimeDays,
  type DiskKind,
  type InvTotals,
} from './inventoryData'
import { esc } from './reportHtml'

// Инвентаризация одним HTML-файлом для передачи, как отчет за период (reportHtml.ts): стили
// внутри, без внешних шрифтов и скриптов, светлая и темная тема по системе, печать в альбомной
// A4 с шапкой таблицы на каждой странице. Сводка сверху, дальше одна таблица: группы -
// строками-заголовками с подытогами, зебра, цветные метки типов дисков как в панели.
// Краткий - строка на сервер; полный - модели процессоров и дисков, RAID рядом с дисками, ядро

type T = (k: string, p?: Record<string, string | number>) => string

const when = (d: Date) =>
  d.toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })

const dk = (k: DiskKind, text: string) => `<span class="dk k-${k || 'unk'}">${esc(text)}</span>`

const noData = (t: T, cols: number) => `<td colspan="${cols}" class="muted">${esc(t('агент еще не присылал данные'))}</td>`

function unk(s: InvServer, t: T): string {
  return `${dk('', fmtSize(s.fs_total))} <span class="muted">${esc(t('по разделам'))}</span>`
}

function shortRow(s: InvServer, t: T): string {
  const head = `<td class="nw"><span class="nm">${esc(s.name)}</span></td><td class="nw ip">${esc(s.ip)}</td>`
  if (!s.reported) return `<tr class="r">${head}${noData(t, 4)}</tr>`
  const disks = s.disks ? diskGroups(s).map((g) => dk(g.kind, diskGroupText(g, t))).join('') : unk(s, t)
  return `<tr class="r">${head}<td class="n">${s.threads || ''}</td><td class="n">${s.mem ? esc(fmtRam(s.mem)) : ''}</td>
<td>${disks}</td><td class="nw">${esc(osShort(s.os))}</td></tr>`
}

function fullRow(s: InvServer, t: T): string {
  const name = `<td class="nw"><span class="nm">${esc(s.name)}</span>${
    s.reported ? `<span class="kd">${esc(s.virt ? serverKind(s, t) : 'BM')}</span>` : ''
  }<span class="sub ip">${esc(s.ip)}${s.country ? ` · ${esc(s.country)}` : ''}${s.local_ip ? `, ${esc(s.local_ip)}` : ''}</span></td>`
  if (!s.reported) return `<tr class="r">${name}${noData(t, 6)}</tr>`
  const gpus = Object.entries(countBy(s.gpus))
    .map(([g, n]) => `${n > 1 ? `${n}x ` : ''}${gpuShort(g)}`)
    .join(', ')
  const raid = raidLabels(s.raid)
    .map((r) => `<span class="raid${r.bad ? ' bad' : ''}">${esc(r.text)}</span>`)
    .join('')
  const disks = s.disks
    ? diskGroups(s, true)
        .map((g, i) => `<div>${dk(g.kind, diskGroupText(g, t))}${i === 0 ? raid : ''}${g.model ? `<span class="dmod">${esc(g.model)}</span>` : ''}</div>`)
        .join('')
    : `${unk(s, t)}${raid}`
  return `<tr class="r">${name}
<td class="n">${s.threads || ''}<span class="sub">${s.virt ? 'vCPU' : esc(t('пот.'))}</span></td>
<td class="nw">${esc(cpuName(s.cpu_model))}<span class="sub">${esc(ghz(s.freq_mhz, t))}${gpus ? ` <span class="gpu">GPU ${esc(gpus)}</span>` : ''}</span></td>
<td class="n">${s.mem ? esc(fmtRam(s.mem)) : ''}</td><td class="nw">${disks}</td>
<td class="nw">${esc(osShort(s.os))}${s.kernel ? `<span class="sub ip">${esc(kernelShort(s.kernel))}</span>` : ''}</td>
<td class="n">${esc(t('{n} дн', { n: uptimeDays(s) }))}</td></tr>`
}

// подытог группы и итог строкой: серверы, CPU, RAM, объем по типам дисков
function subtotal(x: InvTotals, t: T): string {
  return [
    t('{n} серв.', { n: x.n }),
    `<b>${x.threads}</b> CPU`,
    `<b>${esc(fmtRamGb(x.mem))}</b> RAM`,
    ...KINDS.filter((k) => x.disks[k]).map((k) => `<b>${esc(fmtSize(x.disks[k]!.size))}</b> ${esc(kindLabel(k, t))}`),
  ].join(' · ')
}

export function inventoryHtml(list: InvServer[], full: boolean, scope: string, t: T, brand: string): string {
  const tot = invTotals(list)
  const title = t('Инвентаризация {brand}', { brand })
  const all = KINDS.reduce((a, k) => a + (tot.disks[k]?.size ?? 0), 0) || 1
  const cell = (k: string, v: string, sub: string, kind = '') =>
    `<div${kind ? ` class="d k-${kind}"` : ''}><span>${esc(k)}</span><b>${esc(v)}</b><small>${esc(sub)}</small></div>`
  const gpuTot = Object.entries(tot.gpus)
  const sum = [
    cell(t('Серверы'), String(tot.n), `Baremetal ${tot.bare}, ${t('ВМ')} ${tot.vm}`),
    cell('CPU', String(tot.threads), t('потоков всего')),
    cell('RAM', fmtRamGb(tot.mem), t('всего')),
    ...KINDS.filter((k) => tot.disks[k]).map((k) =>
      cell(kindLabel(k, t), fmtSize(tot.disks[k]!.size), t('дисков: {n}', { n: tot.disks[k]!.n }), k || 'unk'),
    ),
    ...(gpuTot.length
      ? [cell('GPU', String(gpuTot.reduce((a, [, n]) => a + n, 0)), gpuTot.map(([g, n]) => `${n}x ${gpuShort(g)}`).join(', '), 'gpu')]
      : []),
  ]
  const share = KINDS.filter((k) => tot.disks[k])
    .map((k) => `<i class="k-${k || 'unk'}" style="width:${((100 * tot.disks[k]!.size) / all).toFixed(2)}%"></i>`)
    .join('')
  const head = full
    ? [t('Сервер / IP'), 'CPU', t('Процессор'), 'RAM', t('Диски и RAID'), t('ОС / ядро'), t('Аптайм')]
    : [t('Сервер'), 'IP', 'CPU', 'RAM', t('Диски'), t('ОС')]
  const num = full ? [1, 3, 6] : [2, 3]
  const th = head.map((h, i) => `<th${num.includes(i) ? ' class="n"' : ''}>${esc(h)}</th>`).join('')
  const groups = byGroup(list)
  const body = groups
    .map(([g, items]) => {
      const rows = items.map((s) => (full ? fullRow(s, t) : shortRow(s, t))).join('\n')
      // одна группа без имени - заголовок не нужен
      if (groups.length === 1 && !g) return rows
      return `<tr class="gh"><td colspan="${head.length}">${esc(g || t('без группы'))} <small>${subtotal(invTotals(items), t)}</small></td></tr>\n${rows}`
    })
    .join('\n')
  const note = tot.unknown
    ? `<p class="muted">${esc(t('Серверов без helper disk-health: {n}. У них объем дисков по разделам, тип и модели неизвестны.', { n: tot.unknown }))}</p>`
    : ''
  const now = when(new Date())

  return `<!doctype html>
<html lang="${esc(document.documentElement.lang || 'ru')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>
:root { --bg: #f4f6f9; --card: #fff; --text: #1b2230; --muted: #667085; --border: #e1e6ee; --head: #eef1f6;
  --zebra: #f8f9fb; --gh: #e8edf6; --bad: #d92d20; --ok: #16a34a;
  --nvme: #7c3aed; --ssd: #2563eb; --hdd: #d97706; --virt: #64748b; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1218; --card: #171b24; --text: #e5e9f1; --muted: #8e97ab; --border: #272d3a; --head: #1c2130;
    --zebra: #1a1e28; --gh: #202842; --bad: #f87171; --ok: #4ade80;
    --nvme: #a78bfa; --ssd: #60a5fa; --hdd: #fbbf24; --virt: #94a3b8; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.page { max-width: ${full ? 1240 : 1080}px; margin: 0 auto; padding: 22px 16px 36px; }
header { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; padding-bottom: 12px; border-bottom: 2px solid var(--text); }
h1 { margin: 0; font-size: 21px; }
.meta { color: var(--muted); font-size: 12px; margin-left: auto; text-align: right; }
.muted { color: var(--muted); }
.k-nvme { --c: var(--nvme); } .k-ssd { --c: var(--ssd); } .k-hdd { --c: var(--hdd); } .k-virt { --c: var(--virt); }
.k-unk { --c: transparent; } .k-gpu { --c: var(--ok); }
.sum { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); border: 1px solid var(--border); border-radius: 8px; background: var(--card); overflow: hidden; margin-bottom: 8px; }
.sum div { padding: 8px 12px; border-right: 1px solid var(--border); }
.sum div:last-child { border-right: 0; }
.sum span { display: block; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
.sum b { display: block; font-size: 19px; font-variant-numeric: tabular-nums; }
.sum small { color: var(--muted); font-size: 11px; }
.sum .d { box-shadow: inset 3px 0 0 var(--c); }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin: 0 0 16px; background: var(--border); }
.bar i { display: block; height: 100%; background: var(--c); }
.tw { background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
thead { display: table-header-group; }
th { background: var(--head); text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 700; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr.r:nth-child(even of .r) td { background: var(--zebra); }
tr.gh td { background: var(--gh); font-weight: 700; padding: 6px 8px; }
tr.gh small { font-weight: 400; color: var(--muted); margin-left: 6px; }
tr.gh small b { color: var(--text); font-weight: 600; }
tfoot td { background: var(--head); font-weight: 700; border-top: 2px solid var(--text); border-bottom: 0; }
tfoot td small { font-weight: 400; color: var(--muted); }
.n { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.nw { white-space: nowrap; }
.nm { font-weight: 600; }
.ip { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }
.sub { display: block; font-size: 11px; color: var(--muted); line-height: 1.3; }
.sub.ip { font-size: 11px; }
.dk { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; margin-right: 10px; }
.dk::before { content: ''; width: 8px; height: 8px; border-radius: 2px; background: var(--c); }
.dk.k-unk::before { border: 1px dashed var(--muted); width: 7px; height: 7px; }
.dmod { display: block; font-size: 10.5px; color: var(--muted); padding-left: 13px; font-family: ui-monospace, Consolas, monospace; }
.raid { font-size: 10.5px; font-family: ui-monospace, Consolas, monospace; border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; color: var(--muted); margin-right: 4px; }
.raid.bad { color: var(--bad); border-color: var(--bad); }
.kd { font-size: 10px; font-weight: 700; border-radius: 3px; padding: 0 4px; margin-left: 5px; color: var(--muted); border: 1px solid var(--border); }
.gpu { font-size: 10.5px; font-weight: 700; color: var(--ok); }
footer { margin-top: 18px; color: var(--muted); font-size: 11px; display: flex; justify-content: space-between; gap: 12px; }
@media print {
  :root { --bg: #fff; --card: #fff; --text: #000; --muted: #555; --border: #bbb; --head: #eee; --zebra: #f6f6f6; --gh: #e4e8f0; color-scheme: light; }
  @page { size: A4 landscape; margin: 10mm; }
  body { font-size: 10.5px; } .page { max-width: none; padding: 0; }
  .tw { border: 0; border-radius: 0; overflow: visible; } th { font-size: 9px; } td { padding: 2px 6px; }
  tr { break-inside: avoid; } tr.gh { break-after: avoid; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
</style>
</head>
<body>
<div class="page">
<header>
<h1>${esc(title)}</h1>
<div class="meta">${esc(full ? t('Полная') : t('Краткая'))} · ${esc(now)}<br>${esc(scope || t('Все группы'))}</div>
</header>
<div class="sum">${sum.join('')}</div>
<div class="bar">${share}</div>
<div class="tw"><table><thead><tr>${th}</tr></thead><tbody>
${body}
</tbody><tfoot><tr><td colspan="${head.length}">${esc(t('Итого'))} <small>${subtotal(tot, t)}</small></td></tr></tfoot></table></div>
${note}
<footer><span>${esc(brand)}</span><span>${esc(t('Сформирован {d}', { d: now }))}</span></footer>
</div>
</body>
</html>
`
}
