import { useEffect, useState } from 'react'
import { serverMetrics, type GpuSum, type LlmSvc, type Server, type ServerMetric } from './api'
import { OvRows, type OvRow } from './cardLayout'
import { useI18n } from './i18n'
import { fmtInt } from './units'

// GPU NVIDIA (helper gpu-setup, app/gpu.py) и LLM-сервисы (агент 2.36). В карточке сервера раздел
// GPU: что не так, драйвер, таблица карт, процессы на них и ошибки Xid за сутки; графики раздела
// рисует сама карточка (buildMetric). В списке серверов плашка с загрузкой, в "Сервисах" блок LLM.

type TFn = (k: string, p?: Record<string, string | number>) => string

const gb = (mib: number | null | undefined) => (mib == null ? '—' : (mib / 1024).toFixed(1))
const pct = (a: number | null | undefined, b: number | null | undefined) => (a != null && b ? (a / b) * 100 : null)
const tone = (p: number | null, warn: number, bad: number) => (p == null ? '' : p >= bad ? ' bad' : p >= warn ? ' warn' : '')
// "1 840" вместо "1840": потоки токенов читаются взглядом
const num = (v: number) => fmtInt(v)

function upText(sec: number | null | undefined, t: TFn): string {
  if (!sec) return ''
  if (sec >= 86400) return t('{n} сут', { n: Math.floor(sec / 86400) })
  if (sec >= 3600) return t('{n} ч', { n: Math.floor(sec / 3600) })
  return t('{n} мин', { n: Math.max(1, Math.floor(sec / 60)) })
}

// время Xid: за сутки - часы и минуты, старше (ждет сброса GPU) - еще и дата
function xidTime(ts: number, old?: boolean): string {
  const d = new Date(ts * 1000)
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return old ? `${d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${hm}` : hm
}

// красная точка: аппаратный Xid уровня 2 (lv - как в app/gpu.py problems; одиночный Xid 62 желтый)
const xidBad = (x: { hw: boolean; lv?: number }) => (x.lv ?? (x.hw ? 2 : 0)) >= 2
const VM_RESET = (t: TFn) =>
  t('На ВМ с пробросом карту сбрасывает только перезагрузка хоста гипервизора: nvidia-smi -r или перезагрузка изнутри роняет ВМ')
// Xid 62 лечится перезагрузкой модуля nvidia без перезагрузки сервера: драйвер отпускает карту и поднимает
// заново. Хост гипервизора или сервер - только если карта после этого не поднялась
const MOD_RELOAD = (t: TFn, vm?: boolean) =>
  vm
    ? t('Останови процессы на GPU и перезагрузи модуль nvidia (rmmod/modprobe), без nvidia-smi -r и без перезагрузки ВМ. Перезагрузка хоста гипервизора - только если после этого RmInitAdapter failed или карта не видна')
    : t('Останови процессы на GPU и перезагрузи модуль nvidia (rmmod/modprobe), без nvidia-smi -r. Перезагрузка сервера - только если после этого RmInitAdapter failed или карта не видна')

const KIND: Record<string, string> = { container: 'контейнер', pod: 'под', unit: 'юнит', user: 'пользователь' }

// Плашка в списке серверов: средняя загрузка карт полосой, красная - если GPU не в порядке
export function GpuChip({ sum, bad, t }: { sum: GpuSum; bad: boolean; t: TFn }) {
  // smi=false - драйвер NVIDIA не ставили, карта не используется: связь не терялась
  // settling - nvidia-smi молчит первые минуты: модуль перезагружают
  const lost = sum.helper && !sum.ok && sum.smi !== false && !sum.settling
  const tip = [
    `${sum.n} x ${sum.models.join(', ') || 'NVIDIA'}`,
    sum.mem != null ? t('память {a} из {b} ГБ', { a: sum.mem, b: sum.memt ?? 0 }) : '',
    !sum.helper ? t('нет helper gpu-setup: загрузка не видна') : sum.smi === false ? t('драйвер NVIDIA не установлен')
      : sum.settling ? t('драйвер NVIDIA перезагружается') : !sum.ok ? t('драйвер NVIDIA не отвечает') : '',
    sum.blind ? t('Xid 62: упал микроконтроллер GPU, показания застыли до сброса карты') : sum.frozen ? t('показания nvidia-smi застыли') : '',
  ].filter(Boolean).join(', ')
  return (
    <span className={`gpu-chip${bad || lost ? ' gpu-chip-bad' : ''}`} title={tip}>
      GPU
      {sum.util != null ? (
        <>
          <span className="gpu-chip-bar"><span style={{ width: `${Math.min(100, sum.util)}%` }} /></span>
          {sum.util}%
        </>
      ) : lost ? (
        <span>{t('нет связи')}</span>
      ) : (
        sum.n > 1 && <span>x{sum.n}</span>
      )}
    </span>
  )
}

function Bar({ p, cls = '' }: { p: number | null; cls?: string }) {
  return (
    <span className="gpu-bar">
      <span className={cls} style={{ width: `${Math.max(0, Math.min(100, p ?? 0))}%` }} />
    </span>
  )
}

export function GpuCard({ server: s, rows }: { server: Server; rows: OvRow[] }) {
  const { t } = useI18n()
  const g = s.gpu
  if (!g) return null
  const pci = g.pci ?? []
  if (!g.helper)
    return (
      <div className="chart-card net-card net-card-wide" id="mcard-gpu">
        <div className="net-head"><div className="chart-cap">GPU</div></div>
        <div className="muted small">
          {t('На шине PCI карт NVIDIA: {n} ({list}). Загрузку, память, процессы и ошибки драйвера смотрит helper gpu-setup, на сервере его нет: он есть в "Требует действий".', {
            n: pci.length, list: pci.map((p) => p.bus).join(', '),
          })}
        </div>
      </div>
    )
  const gpus = g.gpus ?? []
  const procs = g.procs ?? []
  const xid = g.xid ?? []
  const seen = gpus.length
  const onBus = pci.filter((p) => p.drv !== 'vfio-pci').length
  const pm = [...new Set(gpus.map((x) => x.pm).filter(Boolean))]
  const pills: { text: string; lv?: 'bad' | 'warn'; tip?: string }[] = []
  const models = [...new Set(gpus.map((x) => x.name).filter(Boolean))]
  if (models.length) pills.push({ text: `${gpus.length} x ${models.join(', ')}` })
  if (g.driver) pills.push({ text: `${t('драйвер')} ${g.driver}` })
  if (g.cuda) pills.push({ text: `CUDA ${g.cuda}` })
  if (g.ok) pills.push({ text: t('видно GPU {a} из {b} на шине PCI', { a: seen, b: Math.max(onBus, seen) }), lv: onBus > seen ? 'bad' : undefined })
  const xidDay = xid.filter((x) => !x.old)
  const xidHeld = [...new Set(xid.filter((x) => x.held).map((x) => x.code))]
  pills.push({ text: t('Xid за сутки: {n}', { n: xidDay.length }), lv: xidDay.some(xidBad) ? 'bad' : xidDay.length ? 'warn' : undefined })
  // одиночный Xid 62 - мониторинг слеп, карта работает: плашка желтая, лечится перезагрузкой модуля
  // nvidia. Остальные на ВМ с пробросом: сброс из гостя роняет ВМ, поэтому сказано прямо, чем сбрасывать
  const only62 = xidHeld.length > 0 && xidHeld.every((c) => c === 62)
  if (xidHeld.length)
    pills.push({
      text: only62 ? t('ждет сброса GPU: Xid 62, перезагрузить модуль nvidia')
        : g.vm ? t('ждет сброса GPU: Xid {list}, из гостя не сбрасывать', { list: xidHeld.join(', ') }) : t('ждет сброса GPU: Xid {list}', { list: xidHeld.join(', ') }),
      lv: xid.some((x) => x.held && xidBad(x)) ? 'bad' : 'warn',
      tip: only62 ? MOD_RELOAD(t, g.vm) : g.vm ? VM_RESET(t) : undefined,
    })
  if (pm.length === 1) pills.push({ text: `persistence mode ${pm[0] === 'Enabled' ? t('вкл') : t('выкл')}` })
  const vm = pci.filter((p) => p.drv === 'vfio-pci')
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-gpu">
      <div className="net-head">
        <div className="chart-cap">GPU</div>
        <span className="muted small">
          {g.stale ? t('данные устарели: {n} назад', { n: upText(g.age, t) }) : t('helper gpu-setup, раз в минуту')}
        </span>
      </div>
      <OvRows rows={rows} />
      <div className="dh-sum">
        {pills.map((p, i) => (
          <span key={i} className={`dh-pill${p.lv ? ` dh-pill-${p.lv}` : ''}`} title={p.tip}>
            <span className={p.lv ? `lk-dot lk-dot-${p.lv}` : 'lk-dot lk-dot-ok'} />
            {p.text}
          </span>
        ))}
      </div>
      {g.smi === false && (
        <div className="muted small">
          {t('nvidia-smi нет: драйвер NVIDIA не установлен, карты на шине не используются ({list}).', {
            list: pci.map((p) => `${p.bus}${p.drv ? ` ${p.drv}` : ''}`).join(', '),
          })}
        </div>
      )}
      {g.settling && (
        <div className="muted small">{t('nvidia-smi не отвечает: похоже, перезагружают модуль nvidia. Если не ответит 5 минут, это авария.')}</div>
      )}
      {g.smi && !g.ok && g.err && <div className="muted small mono gpu-err">{g.err}</div>}
      {vm.length > 0 && (
        <div className="muted small">{t('Отданы в виртуальные машины (vfio-pci): {list}', { list: vm.map((p) => p.bus).join(', ') })}</div>
      )}
      {gpus.length > 0 && (
        <div className="gpu-tbl">
          <div className="gpu-tr gpu-th">
            <span>GPU</span>
            <span>{t('загрузка')}</span>
            <span>{t('память')}</span>
            <span className="num">{t('темп.')}</span>
            <span className="num gpu-m">{t('питание')}</span>
            <span className="num gpu-m2">{t('такты')}</span>
            <span className="num gpu-m2">{t('режим')}</span>
          </div>
          {gpus.map((x) => {
            const mp = pct(x.mem, x.memt)
            const hot = x.thermal || (x.temp != null && x.slow != null && x.temp >= x.slow)
            const warm = !hot && x.temp != null && x.slow != null && x.temp >= (x.maxop && x.maxop < x.slow ? x.maxop : x.slow - 5)
            return (
              <div key={x.i ?? x.bus} className={`gpu-tr${x.frozen || x.blind ? ' gpu-tr-frozen' : ''}`}
                title={x.blind ? t('Xid 62: упал микроконтроллер GPU, показания застыли до сброса карты') : x.frozen ? t('показания nvidia-smi застыли: им нельзя верить') : undefined}>
                <span title={x.name}>
                  <b>GPU {x.i}</b>
                </span>
                <span className="gpu-cell">
                  <Bar p={x.util} />
                  <span className="num">{x.util != null ? `${Math.round(x.util)}%` : '—'}</span>
                </span>
                <span className="gpu-cell">
                  <Bar p={mp} cls={tone(mp, 90, 97).trim()} />
                  <span className="num">{gb(x.mem)}/{gb(x.memt)} {t('ГБ')}</span>
                </span>
                <span className={`num${hot ? ' t-down' : warm ? ' t-degraded' : ''}`} title={x.slow ? t('замедление с {n}°C', { n: x.slow }) : undefined}>
                  {x.temp != null ? `${Math.round(x.temp)}°C` : '—'}
                </span>
                <span className="num gpu-m" title={x.pcap ? t('упирается в лимит питания') : undefined}>
                  {x.pw != null ? `${Math.round(x.pw)}${x.pwl ? `/${Math.round(x.pwl)}` : ''} ${t('Вт')}` : '—'}
                </span>
                <span className={`num gpu-m2${x.clk != null && x.clkm != null && x.clk < x.clkm && hot ? ' t-degraded' : ''}`}>
                  {x.clk != null ? `${x.clk} ${t('МГц')}` : '—'}
                </span>
                <span className="num gpu-m2">{x.ps || '—'}</span>
              </div>
            )
          })}
        </div>
      )}
      {procs.length > 0 && (
        <div className="gpu-ptbl">
          <div className="gpu-tr gpu-th">
            <span>GPU</span>
            <span>{t('процесс')}</span>
            <span className="num">{t('память')}</span>
            <span className="num">{t('доля')}</span>
          </div>
          {procs.map((p, n) => {
            const memt = gpus.find((x) => x.i === p.gpu)?.memt
            const share = pct(p.mem, memt)
            return (
              <div key={n} className="gpu-tr">
                <span className="mono">{p.gpu != null ? `GPU ${p.gpu}` : '—'}</span>
                <span className="gpu-proc">
                  {p.owner ? (
                    <>
                      <span className="mono">{p.owner}</span> <span className="muted small">{t(KIND[p.kind] ?? 'процесс')}, {p.name}</span>
                    </>
                  ) : (
                    <span className="mono">{p.name}</span>
                  )}
                  {p.pid != null && <span className="muted small"> pid {p.pid}</span>}
                </span>
                <span className="num mono">{p.mem != null ? `${gb(p.mem)} ${t('ГБ')}` : '—'}</span>
                <span className="num muted">{share != null ? `${Math.round(share)}%` : ''}</span>
              </div>
            )
          })}
        </div>
      )}
      {xid.length > 0 && (
        <div className="gpu-xid">
          <div className="muted small">
            {xidDay.length < xid.length ? t('Ошибки драйвера (Xid) за сутки и те, после которых GPU ждет сброса') : t('Ошибки драйвера (Xid) за сутки')}
          </div>
          {xid.map((x, n) => (
            <div key={n} className="gpu-xid-row">
              <span className={`lk-dot ${xidBad(x) ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
              <span className="mono">{xidTime(x.ts, x.old)}</span>
              <span className="mono">{x.gpu != null ? `GPU ${x.gpu}` : x.bus}</span>
              <span>
                Xid {x.code}
                {x.desc ? ` - ${x.desc}` : ''}
                {x.held && (
                  <span className="type-chip off"
                    title={x.code === 62 ? MOD_RELOAD(t, g.vm) : g.vm ? VM_RESET(t) : t('держится до перезагрузки сервера или драйвера NVIDIA')}>
                    {t('ждет сброса')}
                  </span>
                )}
                {x.fixed && (
                  <span className="type-chip" title={t('после ошибки драйвер NVIDIA перезагрузили или он принял карту заново')}>
                    {t('карта сброшена')}
                  </span>
                )}
                <span className="muted small"> {x.text}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// --- LLM ----------------------------------------------------------------------------------------

function sec(v: number | null | undefined, t: TFn): string {
  if (v == null) return '—'
  return v < 1 ? `${Math.round(v * 1000)} ${t('мс')}` : `${v.toFixed(1)} ${t('с')}`
}

// мини-график p95 за час: только линия, без осей - рядом с цифрой
function Spark({ vals, base }: { vals: (number | null)[]; base?: number | null }) {
  const pts = vals.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] != null)
  if (pts.length < 2) return <span className="llm-spark" />
  const W = 160
  const H = 22
  const top = Math.max(...pts.map((p) => p[1]), base ?? 0) * 1.1 || 1
  const x = (i: number) => (i / Math.max(1, vals.length - 1)) * W
  const y = (v: number) => H - 1 - (v / top) * (H - 2)
  return (
    <svg className="llm-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      {base != null && <line className="llm-spark-base" x1={0} x2={W} y1={y(base)} y2={y(base)} />}
      <polyline points={pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')} />
    </svg>
  )
}

// время всплеска: сегодня - часы и минуты, раньше - еще и дата
function hm(iso: string): string {
  const d = new Date(iso)
  const s = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toDateString() === new Date().toDateString() ? s : `${d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${s}`
}

// Всплески задержек за сутки (app/llm_lat.py): алерт уходит только при устойчивой деградации под
// нагрузкой, а здесь видно и разовые - когда, до скольких и сколько было запросов. Иначе по ссылке
// из алерта приходишь в пустое окно: агент считает за 5 минут, запросы уже кончились
function LlmSpikes({ x, t }: { x: LlmSvc; t: TFn }) {
  const evs = (x.lat_recent ?? []).slice(0, 3)
  if (!evs.length) return null
  return (
    <div className="llm-spikes small">
      <div className="muted">{t('Задержки выше обычного за сутки')}</div>
      {evs.map((e) => {
        const from = hm(e.from)
        const to = hm(e.to)
        const req = e.req ? (e.req[0] === e.req[1] ? String(e.req[0]) : `${e.req[0]}-${e.req[1]}`) : ''
        const facts = [
          req ? t('запросов за 5 мин {n}', { n: req }) : '',
          e.wait ? t('в очереди до {n}', { n: num(e.wait) }) : t('очереди не было'),
          e.load ? t('держалось под нагрузкой') : '',
        ].filter(Boolean).join(', ')
        return (
          <div key={`${e.m}${e.from}`} className={e.load ? 't-degraded' : undefined}>
            {from === to ? from : `${from}-${to}`}{' '}
            {t(e.m === 'ttft' ? 'p95 до первого токена до {v}, обычно {b}' : 'p95 на токен до {v}, обычно {b}',
              { v: sec(e.peak, t), b: sec(e.base, t) })}
            {facts && <span className="muted">{` · ${facts}`}</span>}
          </div>
        )
      })}
    </div>
  )
}

function LlmBlock({ x, hist, t }: { x: LlmSvc; hist: ServerMetric[]; t: TFn }) {
  const series = (m: 'ttft' | 'tpot' | 'e2e') => hist.map((h) => h.llm?.find((l) => l.k === x.label)?.[m] ?? null)
  const bad = !x.ok && !x.no_metrics && !x.locked
  const warn = (x.waiting ?? 0) >= 5 || (x.kv ?? 0) >= 95
  const lat: { k: 'ttft' | 'tpot' | 'e2e'; name: string; v?: number[] | null; base?: number | null }[] = [
    { k: 'ttft', name: t('TTFT, до первого токена'), v: x.ttft, base: x.base_ttft },
    { k: 'tpot', name: t('TPOT, на токен'), v: x.tpot, base: x.base_tpot },
    { k: 'e2e', name: t('E2E, весь запрос'), v: x.e2e },
  ]
  const latRows = lat.filter((l) => l.v?.length)
  // у TEI счетчика отказов нет, пока не было ни одного: тогда и строки нет, а не голое "за 5 мин"
  const rsFacts = [
    x.errors != null ? t('ошибок {n}', { n: num(x.errors) }) : '',
    x.aborted ? t('отменено {n}', { n: num(x.aborted) }) : '',
    x.preempt ? t('вытеснено {n}', { n: num(x.preempt) }) : '',
  ].filter(Boolean).join(', ')
  return (
    <div className="llm">
      <div className="llm-h">
        <span className={`lk-dot ${bad ? 'lk-dot-bad' : warn ? 'lk-dot-warn' : x.no_metrics ? '' : 'lk-dot-ok'}`} />
        <b>{x.engine_name}</b>
        {x.model && <span className="mono llm-model">{x.model}</span>}
        <span className="muted small">
          {t(KIND[x.kind] ?? 'процесс')} {x.name} ·{' '}
          {x.pub_port && x.pub_port !== x.port
            ? <span title={t('внутри контейнера :{p}', { p: x.port })}>:{x.pub_port}</span>
            : `:${x.port}`}
          {x.up ? ` · ${t('работает {d}', { d: upText(x.up, t) })}` : ''}
        </span>
      </div>
      {x.no_metrics && (
        <div className="muted small">{t('Метрики выключены: SGLang надо запускать с --enable-metrics, тогда здесь будут очередь, KV-кэш и задержки.')}</div>
      )}
      {bad && <div className="t-down small">{t('/metrics не отвечает: {e}', { e: x.err || t('ошибка') })}</div>}
      {x.locked && (
        <div className="muted small">
          {x.engine === 'litellm'
            ? t('/metrics закрыт ключом ({e}). Чтобы панель видела запросы и ошибки, в конфиге LiteLLM: require_auth_for_metrics_endpoint: false.', { e: x.err })
            : t('/metrics закрыт ключом ({e}): без ключа панель метрики не читает.', { e: x.err })}
        </div>
      )}
      {x.ok && (
        <div className="llm-kpis">
          {x.running != null ? (
            <div className="llm-kpi">
              <div className="llm-l">{t('Запросы')}</div>
              <div className="llm-v">{num(x.running)} <span className="muted small">{t('в работе')}</span></div>
              {x.waiting != null && (
                <div className={`llm-s${x.waiting >= 5 ? ' t-degraded' : ''}`}>{t('в очереди {n}', { n: num(x.waiting) })}</div>
              )}
            </div>
          ) : x.waiting != null ? (
            <div className="llm-kpi">
              <div className="llm-l">{t('Очередь')}</div>
              <div className={`llm-v${x.waiting >= 5 ? ' t-degraded' : ''}`}>{num(x.waiting)}</div>
            </div>
          ) : null}
          {x.kv != null && (
            <div className="llm-kpi">
              <div className="llm-l">{t('KV-кэш')}</div>
              <div className={`llm-v${x.kv >= 95 ? ' t-degraded' : ''}`}>{Math.round(x.kv)}%</div>
              <Bar p={x.kv} cls={x.kv >= 95 ? 'warn' : ''} />
            </div>
          )}
          {(x.gen_tps != null || x.prompt_tps != null) && (
            <div className="llm-kpi">
              <div className="llm-l">{x.gen_tps != null ? t('Генерация') : t('Токены')}</div>
              <div className="llm-v">
                {num(x.gen_tps ?? x.prompt_tps ?? 0)} <span className="muted small">{t('ток/с')}</span>
              </div>
              {x.gen_tps != null && x.prompt_tps != null && (
                <div className="llm-s">{t('промпт {n} ток/с', { n: num(x.prompt_tps) })}</div>
              )}
            </div>
          )}
          {x.req_s != null && (
            <div className="llm-kpi">
              <div className="llm-l">{t('Пропускная')}</div>
              <div className="llm-v">{x.req_s < 10 ? x.req_s.toFixed(2) : num(x.req_s)} <span className="muted small">{t('запр/с')}</span></div>
              {rsFacts && (
                <div className={`llm-s${(x.errors ?? 0) > 0 ? ' t-degraded' : ''}`}>
                  {rsFacts}{x.win ? ` ${t('за {n} мин', { n: Math.round(x.win / 60) })}` : ''}
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {x.ok && latRows.length > 0 && (
        <div className="llm-lat">
          <span className="llm-lh" />
          <span className="llm-lh num">p50</span>
          <span className="llm-lh num">p95</span>
          <span className="llm-lh num">p99</span>
          <span className="llm-lh llm-sp">{t('p95 за час')}</span>
          {latRows.map((l) => {
            const slow = l.base != null && l.v && l.v[1] >= l.base * (l.k === 'ttft' ? 3 : 2)
            return (
              <span key={l.k} className="llm-lr">
                <span>{l.name}</span>
                <span className="num">{sec(l.v![0], t)}</span>
                <span className={`num${slow ? ' t-degraded' : ''}`} title={l.base != null ? t('обычно {v}', { v: sec(l.base, t) }) : undefined}>
                  {sec(l.v![1], t)}
                </span>
                <span className="num">{sec(l.v![2], t)}</span>
                <span className="llm-sp"><Spark vals={series(l.k)} base={l.base} /></span>
              </span>
            )
          })}
        </div>
      )}
      {x.ok && !latRows.length && x.win != null && x.win > 0 && x.engine !== 'litellm' && (
        <div className="muted small">{t('Запросов за {n} мин не было: задержки не посчитать.', { n: Math.round(x.win / 60) })}</div>
      )}
      {x.ok && <LlmSpikes x={x} t={t} />}
    </div>
  )
}

// Раздел LLM в карточке сервисов ноды: история за час - для мини-графиков задержек
export function LlmSection({ server: s, rows }: { server: Server; rows: OvRow[] }) {
  const { t } = useI18n()
  const [hist, setHist] = useState<ServerMetric[]>([])
  useEffect(() => {
    let alive = true
    const load = () => serverMetrics(s.id, 1).then((m) => { if (alive) setHist(m) }).catch(() => {})
    load()
    const h = window.setInterval(load, 60_000)
    return () => {
      alive = false
      window.clearInterval(h)
    }
  }, [s.id])
  const list = s.llm ?? []
  if (!list.length) return null
  return (
    <div className="net-cards">
      <div className="chart-card net-card net-card-wide" id="mcard-llm">
        <div className="net-head">
          <div className="chart-cap">{t('LLM-сервисы')}</div>
          <span className="muted small">{t('за 5 минут, мини-графики за час')}</span>
        </div>
        <OvRows rows={rows} />
        {list.map((x) => (
          <LlmBlock key={x.key} x={x} hist={hist} t={t} />
        ))}
      </div>
    </div>
  )
}
