// Минимальный .xlsx без библиотек: таблица с жирной закрепленной шапкой, автофильтром и
// ширинами колонок. Формулы, даты и объединения не нужны - только строки и числа. Zip без
// сжатия (stored): инвентаризация на сотню серверов - десятки килобайт, Excel и LibreOffice
// такой открывают

export type XlsxCell = string | number | null
export type XlsxSheet = {
  name: string
  head: string[]
  rows: XlsxCell[][]
  widths?: number[]
  totalRow?: boolean // последняя строка - итоги, жирным
}

const enc = new TextEncoder()

const CRC = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()

function crc32(b: Uint8Array): number {
  let c = 0xffffffff
  for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function zip(files: [string, string][]): Blob {
  const parts: Uint8Array[] = []
  const central: Uint8Array[] = []
  let off = 0
  for (const [name, text] of files) {
    const nb = enc.encode(name)
    const data = enc.encode(text)
    const crc = crc32(data)
    const h = new DataView(new ArrayBuffer(30))
    h.setUint32(0, 0x04034b50, true)
    h.setUint16(4, 20, true)
    h.setUint16(6, 0x0800, true) // имена в UTF-8
    h.setUint16(8, 0, true) // stored
    h.setUint16(12, 0x21, true) // 01.01.1980
    h.setUint32(14, crc, true)
    h.setUint32(18, data.length, true)
    h.setUint32(22, data.length, true)
    h.setUint16(26, nb.length, true)
    const c = new DataView(new ArrayBuffer(46))
    c.setUint32(0, 0x02014b50, true)
    c.setUint16(4, 20, true)
    c.setUint16(6, 20, true)
    c.setUint16(8, 0x0800, true)
    c.setUint16(14, 0x21, true)
    c.setUint32(16, crc, true)
    c.setUint32(20, data.length, true)
    c.setUint32(24, data.length, true)
    c.setUint16(28, nb.length, true)
    c.setUint32(42, off, true)
    parts.push(new Uint8Array(h.buffer), nb, data)
    central.push(new Uint8Array(c.buffer), nb)
    off += 30 + nb.length + data.length
  }
  const size = central.reduce((a, x) => a + x.length, 0)
  const e = new DataView(new ArrayBuffer(22))
  e.setUint32(0, 0x06054b50, true)
  e.setUint16(8, files.length, true)
  e.setUint16(10, files.length, true)
  e.setUint32(12, size, true)
  e.setUint32(16, off, true)
  return new Blob([...parts, ...central, new Uint8Array(e.buffer)] as BlobPart[], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
}

// управляющие символы в XML запрещены: модель диска из прошивки бывает любой
const x = (s: string) =>
  s
    // oxlint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

function col(i: number): string {
  let s = ''
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s
  return s
}

function cell(v: XlsxCell, ref: string, style: number): string {
  const s = style ? ` s="${style}"` : ''
  if (v == null || v === '') return style ? `<c r="${ref}"${s}/>` : ''
  if (typeof v === 'number' && Number.isFinite(v)) return `<c r="${ref}"${s}><v>${v}</v></c>`
  return `<c r="${ref}"${s} t="inlineStr"><is><t xml:space="preserve">${x(String(v))}</t></is></c>`
}

function sheetXml(sh: XlsxSheet): string {
  const rows = [sh.head as XlsxCell[], ...sh.rows]
  const w = Math.max(sh.head.length, ...sh.rows.map((r) => r.length))
  const data = rows
    .map((r, i) => {
      const style = i === 0 ? 1 : sh.totalRow && i === rows.length - 1 ? 2 : 0
      const cs = Array.from({ length: w }, (_, j) => cell(r[j] ?? null, `${col(j)}${i + 1}`, style)).join('')
      return `<row r="${i + 1}">${cs}</row>`
    })
    .join('')
  const cols = (sh.widths ?? [])
    .map((wd, i) => `<col min="${i + 1}" max="${i + 1}" width="${wd}" customWidth="1"/>`)
    .join('')
  // фильтр без строки итогов: иначе она сортируется вместе с серверами
  const last = rows.length - (sh.totalRow ? 1 : 0)
  return (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
    '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' +
    '<sheetFormatPr defaultRowHeight="15"/>' +
    (cols ? `<cols>${cols}</cols>` : '') +
    `<sheetData>${data}</sheetData>` +
    `<autoFilter ref="A1:${col(w - 1)}${Math.max(last, 1)}"/>` +
    '</worksheet>'
  )
}

// имя листа: до 31 символа и без []:*?/\
const sheetName = (s: string, i: number) =>
  s.replace(/[[\]:*?/\\]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 31) || `Sheet${i + 1}`

export function xlsxBlob(sheets: XlsxSheet[]): Blob {
  const names = sheets.map((s, i) => sheetName(s.name, i))
  const ns = 'http://schemas.openxmlformats.org/'
  const files: [string, string][] = [
    [
      '[Content_Types].xml',
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        `<Types xmlns="${ns}package/2006/content-types">` +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
        sheets
          .map((_, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`)
          .join('') +
        '</Types>',
    ],
    [
      '_rels/.rels',
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        `<Relationships xmlns="${ns}package/2006/relationships">` +
        `<Relationship Id="rId1" Type="${ns}officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>` +
        '</Relationships>',
    ],
    [
      'xl/workbook.xml',
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        `<workbook xmlns="${ns}spreadsheetml/2006/main" xmlns:r="${ns}officeDocument/2006/relationships"><sheets>` +
        names.map((n, i) => `<sheet name="${x(n)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join('') +
        '</sheets><definedNames>' +
        sheets
          .map((s, i) => {
            const w = Math.max(s.head.length, ...s.rows.map((r) => r.length))
            const last = 1 + s.rows.length - (s.totalRow ? 1 : 0)
            return `<definedName name="_xlnm._FilterDatabase" localSheetId="${i}" hidden="1">'${x(names[i].replace(/'/g, "''"))}'!$A$1:$${col(w - 1)}$${Math.max(last, 1)}</definedName>`
          })
          .join('') +
        '</definedNames></workbook>',
    ],
    [
      'xl/_rels/workbook.xml.rels',
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        `<Relationships xmlns="${ns}package/2006/relationships">` +
        sheets
          .map((_, i) => `<Relationship Id="rId${i + 1}" Type="${ns}officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`)
          .join('') +
        `<Relationship Id="rId${sheets.length + 1}" Type="${ns}officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
        '</Relationships>',
    ],
    [
      'xl/styles.xml',
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        `<styleSheet xmlns="${ns}spreadsheetml/2006/main">` +
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>' +
        '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>' +
        '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill>' +
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill></fills>' +
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
        '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>' +
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
        '</styleSheet>',
    ],
    ...sheets.map((s, i) => [`xl/worksheets/sheet${i + 1}.xml`, sheetXml(s)] as [string, string]),
  ]
  return zip(files)
}
