// Резервный сервер бэкапов: вторая копия выбранных restic-репозиториев основных серверов бэкапов
// (backend app/bsrv_mirror.py, helper agent/mirror-setup.sh). Резервный забирает копии сам учеткой
// только для чтения на основном; панель только называет, что копировать. Удалить копию, сменить
// адрес известного основного, разморозить копию - команды root на самом резервном, здесь они только
// показаны.
import { Fragment, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  backupMirrorPull, backupMirrorRole, backupMirrorSync, backupRepoMirror,
  type MirrorCopy, type MirrorReport, type Server,
} from './api'
import { CountryFlag } from './CountryFlag'
import { FixCmd } from './FixSteps'
import { NetTile } from './NetLinks'
import { OsIcon } from './osIcon'
import { fmtRel } from './ServersPage'
import { toastError } from './toast'
import { useI18n } from './i18n'
import { fmtBytes } from './units'
import type { RepoTone } from './bsrvRepos'
import {
  HELPER, ago, copyKey, copyShort, copyTone, entryDrift, errText, frozenText, isMirror, mirrorNotReady, mirrorReport,
  running, syncNotes, when,
} from './bsrvMirrorState'

/** "Копия на резервном" в карточке репозитория основного сервера бэкапов. */
export function RepoMirrorBlock({ server: s, repo, servers, canAct, onChanged }: {
  server: Server; repo: string; servers: Server[]; canAct: boolean; onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [pick, setPick] = useState('')
  const entry = s.backup_mirror?.[repo]
  const mirrors = servers.filter((x) => x.id !== s.id && isMirror(x)).sort((a, b) => a.name.localeCompare(b.name))
  if (!entry && (!mirrors.length || !canAct)) return null
  const run = async (f: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await f()
      onChanged()
    } catch (e) {
      toastError(e)
    } finally {
      setBusy(false)
    }
  }
  if (!entry) {
    const ready = mirrors.filter((m) => !mirrorNotReady(m, t))
    const cur = pick || String(ready[0]?.id ?? '')
    return (
      <div className="repo-mirror">
        <div className="repo-mirror-h">{t('Копия на резервном')}</div>
        <div className="muted small">
          {t('Резервный сам забирает копию раз в сутки учеткой только для чтения. Взломав этот сервер или клиента, удалить ее с резервного нельзя.')}
        </div>
        {ready.length ? (
          <div className="repo-acts">
            <select className="modal-sort" value={cur} onChange={(e) => setPick(e.target.value)}>
              {ready.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            <button className="ghost row-act" disabled={busy || !cur} onClick={() => run(() => backupRepoMirror(s.id, repo, Number(cur)))}>
              {busy ? '…' : t('Держать копию')}
            </button>
          </div>
        ) : (
          <div className="muted small">
            {mirrors.map((m) => `${m.name}: ${mirrorNotReady(m, t)}`).join('; ')}
          </div>
        )}
      </div>
    )
  }
  const m = servers.find((x) => x.id === entry.mirror)
  const rep = m ? mirrorReport(m) : null
  const c = rep?.copies.find((x) => x.src === entry.src && x.repo === repo)
  const drift = m ? entryDrift(s, repo, m, t) : ''
  const notes = syncNotes(s, entry.mirror, t)
  const tone: RepoTone = !m ? 'bad' : !rep ? 'warn' : c ? copyTone(c, rep) : drift ? 'warn' : 'mute'
  let line: string
  if (!m) line = t('резервного сервера больше нет в панели')
  else if (!rep) line = t('{m} не присылает отчет о копиях (helper mirror-setup)', { m: m.name })
  else if (!c) line = drift || t('ждет первой перекачки')
  else if (c.frozen) line = t('копия заморожена: {why}. Ничего не удаляется и не забирается, пока на резервном не разморозят', { why: frozenText(c, t) })
  else if (running(c, rep)) line = t('забирается прямо сейчас, в копии {sz}', { sz: fmtBytes(c.bytes, '0') })
  else if (!c.last_ok) line = t('первая перекачка еще не прошла') + (c.err_code ? `: ${errText(c, t)}` : '')
  else
    line = t('забрана {ago}: {sz}, снапшотов {n} из {src}', {
      ago: ago(c.last_ok, t), sz: fmtBytes(c.bytes, '0'), n: c.snaps, src: c.src_snaps,
    }) + (c.err_code ? `. ${t('Последняя попытка')}: ${errText(c, t)}` : '')
  return (
    <div className={`repo-mirror repo-fix repo-fix-${tone === 'ok' ? 'mute' : tone}`}>
      <div className="repo-mirror-h">
        {t('Копия на резервном')} <b className="mono">{m?.name ?? `#${entry.mirror}`}</b>
      </div>
      <div className={tone === 'bad' ? 't-down' : tone === 'warn' ? 't-degraded' : undefined}>{line}</div>
      {c && c.gone_files > 0 && (
        <div className="muted small">
          {t('Пропало на основном и держится в копии еще до {d} дн.: {n} файлов, {sz}', {
            d: rep?.keep_days ?? 0, n: c.gone_files, sz: fmtBytes(c.gone_bytes, '0'),
          })}
        </div>
      )}
      {drift && c && <div className="t-degraded small">{drift}</div>}
      {notes.map((x) => <div key={x} className="t-degraded small">{x}</div>)}
      {canAct && (
        <div className="repo-acts">
          {(drift || notes.length > 0) && (
            <button className="ghost row-act" disabled={busy} onClick={() => run(() => backupMirrorSync(s.id))}
              title={t('Отправить настройки копий основному и резервному заново')}>
              {t('Отправить заново')}
            </button>
          )}
          <button className="ghost row-act" disabled={busy}
            title={t('Резервный больше не забирает новое, копия на нем остается. Удалить ее можно только на нем самом.')}
            onClick={() => run(() => backupRepoMirror(s.id, repo, null))}>
            {busy ? '…' : t('Перестать копировать')}
          </button>
        </div>
      )}
    </div>
  )
}

const TONE_DOT: Record<RepoTone, string> = { bad: ' lk-dot-bad', warn: ' lk-dot-warn', ok: ' lk-dot-ok', mute: '' }

/** Окно резервного сервера: копии слева по группам, выбранная справа, внизу настройки и команды. */
export function MirrorModal({ server: s, servers, canAct, onChanged, onClose }: {
  server: Server; servers: Server[]; canAct: boolean; onChanged: () => void; onClose: () => void
}) {
  const { t } = useI18n()
  const [sel, setSel] = useState('')
  const [busy, setBusy] = useState(false)
  const rep = mirrorReport(s)
  const copies = (rep?.copies ?? []).slice().sort((a, b) => copyKey(a).localeCompare(copyKey(b)))
  const groups = ([
    ['bad', t('Требуют внимания')], ['warn', t('Обратить внимание')], ['ok', t('В порядке')], ['mute', t('На паузе')],
  ] as [RepoTone, string][])
    .map(([tone, cap]) => ({ tone, cap, list: rep ? copies.filter((c) => copyTone(c, rep) === tone) : [] }))
    .filter((g) => g.list.length > 0)
  const cur = groups.flatMap((g) => g.list).find((c) => copyKey(c) === sel) ?? groups[0]?.list[0]
  // какой основной под каким именем у резервного
  const srcServer = (src: string) => servers.find((p) => Object.values(p.backup_mirror ?? {}).some((e) => e.mirror === s.id && e.src === src))
  const total = copies.reduce((a, c) => a + (c.bytes || 0), 0)
  const pct = rep?.disk_total ? Math.round(100 - (rep.disk_free / rep.disk_total) * 100) : null
  const pull = async () => {
    setBusy(true)
    try {
      await backupMirrorPull(s.id)
      onChanged()
    } catch (e) {
      toastError(e)
    } finally {
      setBusy(false)
    }
  }
  const notReady = mirrorNotReady(s, t)

  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            <span className="type-chip">{t('резервный')}</span>
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>✕</button>
          </div>
        </div>
        {rep && (
          <div className="srv-meta srv-meta-line">
            <span className="srv-meta-item"><span className="srv-meta-k">{t('копии')}</span> <span className="mono">{rep.data_dir}</span></span>
            <span className="srv-meta-item"><span className="srv-meta-k">{t('удаленное на основном держится')}</span> {t('{n} дн.', { n: rep.keep_days })}</span>
            <span className="srv-meta-item"><span className="srv-meta-k">{t('забирает в')}</span> {rep.pull_at}</span>
            <span className="srv-meta-item"><span className="srv-meta-k">mirror-setup</span> {rep.helper}</span>
            <span className="srv-meta-item"><span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}</span>
          </div>
        )}
        <div className="srv-main mirror-main">
          {notReady ? (
            <div className="repo-fix repo-fix-warn">
              <div>{t('Сервер отмечен резервным, но копировать на него пока нельзя: {why}.', { why: notReady })}</div>
              <div>{t('Helper mirror-setup ставится плейбуком kervax_helpers.yml: сервер уже в "Требует действий".')}</div>
              <FixCmd cmd={`ansible-playbook playbooks/kervax_helpers.yml -l ${s.name}`} />
            </div>
          ) : (
            <>
              <div className="net-tiles srv-tiles">
                <NetTile label={t('Копии')} value={String(copies.filter((c) => c.enabled).length)}
                  sub={copies.some((c) => !c.enabled) ? t('на паузе: {n}', { n: copies.filter((c) => !c.enabled).length }) : undefined}
                  tone={groups.some((g) => g.tone === 'bad') ? 'bad' : copies.length ? 'ok' : undefined} />
                <NetTile label={t('Размер копий')} value={fmtBytes(total, '0')} />
                {pct != null && rep && (
                  <NetTile label={t('Место на диске')} value={`${pct}%`}
                    sub={t('свободно {f} из {tt}', { f: fmtBytes(rep.disk_free), tt: fmtBytes(rep.disk_total) })}
                    tone={pct >= 90 ? 'bad' : pct >= 80 ? 'warn' : 'ok'} />
                )}
                {rep && (
                  <NetTile label={t('Следующая перекачка')} value={rep.running ? t('идет') : rep.next_run ? when(rep.next_run) : '—'}
                    sub={rep.running && rep.current ? rep.current : undefined} />
                )}
              </div>
              {copies.length === 0 ? (
                <div className="srv-empty">
                  {t('Копий пока нет. Выберите репозиторий в окне основного сервера бэкапов (раздел "Репозитории") и нажмите "Держать копию".')}
                </div>
              ) : (
                <div className="bsrv-md">
                  <div className="bsrv-md-list">
                    {groups.map((g) => (
                      <Fragment key={g.tone}>
                        <div className={`bsrv-md-cap bsrv-md-cap-${g.tone}`}>{g.cap} <span>{g.list.length}</span></div>
                        {g.list.map((c) => (
                          <button key={copyKey(c)} className={`bsrv-md-it${cur && copyKey(c) === copyKey(cur) ? ' on' : ''}`} onClick={() => setSel(copyKey(c))}>
                            <span className={`lk-dot${TONE_DOT[g.tone]}`} />
                            <span className="bsrv-md-nm mono">{copyKey(c)}</span>
                            <span className={`bsrv-md-r${g.tone === 'bad' ? ' t-down' : g.tone === 'warn' ? ' t-degraded' : ''}`}>
                              {rep && copyShort(c, rep, t)}
                            </span>
                          </button>
                        ))}
                      </Fragment>
                    ))}
                  </div>
                  <div className="bsrv-md-body">
                    <select className="modal-sort bsrv-md-sel" value={cur ? copyKey(cur) : ''} onChange={(e) => setSel(e.target.value)}>
                      {groups.map((g) => (
                        <optgroup key={g.tone} label={g.cap}>
                          {g.list.map((c) => <option key={copyKey(c)} value={copyKey(c)}>{copyKey(c)}</option>)}
                        </optgroup>
                      ))}
                    </select>
                    {cur && rep && <CopyDetail c={cur} rep={rep} primary={srcServer(cur.src)} />}
                  </div>
                </div>
              )}
              {canAct && copies.some((c) => c.enabled) && (
                <div className="repo-acts">
                  <button className="ghost row-act" disabled={busy || rep?.running} onClick={pull}>
                    {busy ? '…' : rep?.running ? t('Перекачка уже идет') : t('Забрать сейчас')}
                  </button>
                </div>
              )}
              {rep && <MirrorHowTo rep={rep} />}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

function CopyDetail({ c, rep, primary }: { c: MirrorCopy; rep: MirrorReport; primary?: Server }) {
  const { t } = useI18n()
  const tone = copyTone(c, rep)
  const issues: { tone: RepoTone; head: string; why: string; cmd?: string }[] = []
  if (c.frozen)
    issues.push({
      tone: 'bad', head: t('Копия заморожена'),
      why: t('{why}. Так бывает, когда основной взломали или репозиторий стерли; бывает и после большой чистки. Пока копия заморожена, из нее ничего не удаляется и ничего нового не берется. Если потеря ожидаемая, разморозьте на резервном: то, что сейчас на основном, станет точкой отсчета, а пропавшее там продержится в копии еще срок хранения.', { why: frozenText(c, t) }),
      cmd: `${HELPER} unfreeze ${c.src} ${c.repo}`,
    })
  else if (c.enabled && tone === 'bad')
    issues.push({
      tone: 'bad', head: c.last_ok ? t('Копия отстала') : t('Первая копия не забрана'),
      why: (c.last_ok ? t('Последняя удачная перекачка {ago}.', { ago: ago(c.last_ok, t) }) : '') + (c.err_code ? ` ${errText(c, t)}.` : ''),
    })
  else if (c.err_code && c.enabled)
    issues.push({ tone: 'warn', head: t('Последняя попытка не удалась'), why: errText(c, t) })
  if (c.check_bad)
    issues.push({ tone: 'warn', head: t('Испорченные файлы в копии'), why: t('Проверка нашла {n} файлов, не совпавших со своим хешем: они отложены в сторону и заберутся с основного заново. Стоит проверить диск резервного.', { n: c.check_bad }) })
  if (c.bad_new)
    issues.push({ tone: 'warn', head: t('Основной отдал испорченные файлы'), why: t('{n} файлов с основного не совпали со своим хешем и в копию не взяты: на основном они испорчены или подменены.', { n: c.bad_new }) })
  if (!c.enabled)
    issues.push({ tone: 'mute', head: t('На паузе'), why: t('Копирование снято в панели: новое не забирается, копия хранится как есть. Удалить ее можно только на резервном.'), cmd: `${HELPER} drop ${c.src} ${c.repo} --yes` })
  const lag = c.src_newest && c.newest && c.src_newest > c.newest ? c.src_newest - c.newest : 0
  return (
    <>
      <div className="repo-head">
        <h5 className="mono">{c.repo}</h5>
        <span className={`repo-st repo-st-${tone}`}>
          <span className={`lk-dot${TONE_DOT[tone]}`} />
          {issues[0]?.head ?? (running(c, rep) ? t('Забирается') : t('В порядке'))}
        </span>
      </div>
      {issues.length > 0 && (
        <div className={`repo-fix repo-fix-${issues.some((i) => i.tone === 'bad') ? 'bad' : issues.some((i) => i.tone === 'warn') ? 'warn' : 'mute'}`}>
          {issues.map((i, k) => (
            <div key={k} className="repo-fix-item">
              {issues.length > 1 && <div className={`repo-fix-h repo-fix-h-${i.tone}`}>{i.head}</div>}
              <div>{i.why}</div>
              {i.cmd && <FixCmd cmd={i.cmd} />}
            </div>
          ))}
        </div>
      )}
      <div className="net-tiles repo-tiles">
        <NetTile label={t('Последняя копия')} value={c.last_ok ? ago(c.last_ok, t) : t('нет')}
          sub={c.last_ok ? when(c.last_ok) : c.added ? t('добавлена {d}', { d: when(c.added) }) : undefined}
          tone={c.frozen ? undefined : tone === 'bad' ? 'bad' : tone === 'ok' ? 'ok' : undefined} />
        <NetTile label={t('Размер копии')} value={fmtBytes(c.bytes, '0')}
          sub={t('на основном {sz}', { sz: fmtBytes(c.src_bytes, '0') })} />
        <NetTile label={t('Снапшоты')} value={String(c.snaps)}
          sub={lag ? t('свежий снапшот основного еще не здесь ({ago})', { ago: ago(c.src_newest, t) }) : t('на основном {n}', { n: c.src_snaps })} />
        <NetTile label={t('Удалено на основном')} value={String(c.gone_files)}
          sub={t('держится еще до {d} дн., {sz}', { d: rep.keep_days, sz: fmtBytes(c.gone_bytes, '0') })} />
        {rep.check_days > 0 && (
          <NetTile label={t('Проверка хешей')} value={c.check_ts ? ago(c.check_ts, t) : t('не было')}
            sub={t('каждый файл раз в {n} дн.', { n: rep.check_days })} tone={c.check_bad ? 'warn' : c.check_ts ? 'ok' : undefined} />
        )}
      </div>
      <dl className="repo-kv">
        <dt>{t('Основной')}</dt>
        <dd>{primary ? primary.name : c.src}{rep.sources[c.src] && <span className="muted mono"> {rep.sources[c.src].host}:{rep.sources[c.src].port}</span>}</dd>
        <dt>{t('Каталог')}</dt>
        <dd className="mono">{`${rep.data_dir}/${c.src}/${c.repo}`}</dd>
        {c.check_ts > 0 && (
          <>
            <dt>{t('Проверено за раз')}</dt>
            <dd>{t('{n} файлов', { n: c.check_files })}</dd>
          </>
        )}
      </dl>
    </>
  )
}

// Как устроено и команды на самом резервном: то, что панель сделать не может намеренно
function MirrorHowTo({ rep }: { rep: MirrorReport }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return (
    <div className="mirror-howto">
      <button className="ghost row-act" onClick={() => setOpen(!open)}>
        {open ? t('Скрыть') : t('Как устроено и команды на резервном')}
      </button>
      {open && (
        <div className="muted small mirror-howto-b">
          <p>{t('Резервный сам заходит на основной по SSH учеткой kervax-mread, которой можно только читать выбранные репозитории. У основного нет ни ключа, ни входа сюда. Файлы restic зашифрованы и названы по sha256 содержимого: копия берет файл, только если хеш совпал, и никогда не перезаписывает то, что уже есть.')}</p>
          <p>{t('Удаленное на основном держится в копии {d} дн. Если за одну перекачку пропало {p}% снапшотов или данных или сменился config, копия замораживается. Сроки и время меняются только на резервном, в /etc/kervax/mirror.conf.', { d: rep.keep_days, p: rep.freeze_pct })}</p>
          <p>{t('Восстановить из копии: restic -r <каталог копии> с паролем репозитория из сейфа. Саму копию не трогайте: если restic попросит починить индекс, делайте restic repair index на отдельной копии этого каталога.')}</p>
          <FixCmd cmd={`${HELPER} status`} />
        </div>
      )}
    </div>
  )
}

/** Карточка "Резервные серверы" на странице бэкапов: строка на резервный, кнопка сделать резервным. */
export function MirrorsCard({ servers, canAct, onOpen, onChanged }: {
  servers: Server[]; canAct: boolean; onOpen: (id: number) => void; onChanged: () => void
}) {
  const { t } = useI18n()
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const mirrors = servers.filter(isMirror).sort((a, b) => a.name.localeCompare(b.name))
  // кандидаты: сервер без rest-server с репозиториями (пустые бэкап-серверы как раз такие)
  const cands = servers
    .filter((s) => !isMirror(s) && !(s.last_report?.backup_server?.repos?.length))
    .sort((a, b) => a.name.localeCompare(b.name))
  if (!mirrors.length && !canAct) return null
  const make = async () => {
    if (!pick) return
    setBusy(true)
    try {
      await backupMirrorRole(Number(pick), true)
      setPick('')
      onChanged()
    } catch (e) {
      toastError(e)
    } finally {
      setBusy(false)
    }
  }
  const unmake = async (id: number) => {
    try {
      await backupMirrorRole(id, false)
      onChanged()
    } catch (e) {
      toastError(e)
    }
  }
  const rows: ReactNode[] = mirrors.map((m) => {
    const rep = mirrorReport(m)
    const cs = rep?.copies ?? []
    const bad = rep ? cs.filter((c) => copyTone(c, rep) === 'bad').length : 0
    const warn = rep ? cs.filter((c) => copyTone(c, rep) === 'warn').length : 0
    const nr = mirrorNotReady(m, t)
    return (
      <div key={m.id} className="loc-res docker-row repo-row mirror-row" onClick={() => onOpen(m.id)} role="button">
        <span className={`lk-dot${bad ? ' lk-dot-bad' : warn || nr ? ' lk-dot-warn' : cs.length ? ' lk-dot-ok' : ''}`} />
        <span className="mono">{m.name}</span>
        <span className={`muted small${bad ? ' t-down' : warn || nr ? ' t-degraded' : ''}`}>
          {nr || (cs.length
            ? t('копий: {n}, {sz}', { n: cs.filter((c) => c.enabled).length, sz: fmtBytes(cs.reduce((a, c) => a + c.bytes, 0), '0') })
              + (bad ? `, ${t('требуют внимания: {n}', { n: bad })}` : '')
            : t('копий пока нет'))}
        </span>
        {canAct && !cs.length && !Object.values(servers).some((p) => Object.values(p.backup_mirror ?? {}).some((e) => e.mirror === m.id)) && (
          <button className="ghost row-act" onClick={(e) => { e.stopPropagation(); unmake(m.id) }}>{t('Снять роль')}</button>
        )}
      </div>
    )
  })
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap net-cap">
          {t('Резервные серверы')}
          <span className="net-n">{mirrors.length}</span>
        </div>
      </div>
      <div className="loc-results">
        {rows}
        {canAct && (
          <div className="mirror-make">
            <span className="muted small">
              {t('Резервный держит вторую копию выбранных репозиториев с разных серверов бэкапов. Взломав основной сервер бэкапов или клиента, удалить ее нельзя.')}
            </span>
            <div className="repo-acts">
              <select className="modal-sort" value={pick} onChange={(e) => setPick(e.target.value)}>
                <option value="">{t('сервер…')}</option>
                {cands.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <button className="ghost row-act" disabled={busy || !pick} onClick={make}>
                {busy ? '…' : t('Сделать резервным')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
