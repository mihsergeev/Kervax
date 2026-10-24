// Раздел "GitLab" в карточке сервисов (helper gitlab-setup, backend/app/gitlab.py): по инстансу -
// версия против выпусков и путь обновления, как поставлен, бэкап (архивы, SKIP, расписание, конфиг),
// фоновые миграции и gitlab-autoupgrade. 🔕 - не алертить об этом GitLab
import { useState } from 'react'
import { muteServerKeys, snoozeServerAlert, type GitlabInfo, type GitlabInst, type Server } from './api'
import { useI18n } from './i18n'
import { safeHref } from './shell'
import { toastError } from './toast'
import { useAuth } from './auth'
import { fmtRel } from './ServersPage'
import { GITLAB_MUTE } from './mutes'
import { fmtBytes } from './units'

const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-down': ' lk-dot-bad', 't-degraded': ' lk-dot-warn', 't-soon': '' }
const subOf = (...parts: (string | false | 0 | undefined | null)[]) => parts.filter(Boolean).join(' · ')
const when = (ts: number) =>
  new Date(ts * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
// компоненты, без которых GitLab из бэкапа не поднять целиком (registry и artifacts пропускают все)
const LOST = new Set(['db', 'repositories', 'uploads', 'lfs', 'packages', 'pages', 'terraform_state', 'ci_secure_files'])
const BBM_STATE: Record<number, string> = { 0: 'на паузе', 1: 'идет', 4: 'упала', 5: 'финализируется' }

export function gitlabProblems(g: GitlabInfo | null | undefined): number {
  return (g?.instances ?? []).filter((x) => x.level && !x.mute).length
}

// строка плитки: версии и что делать
export function gitlabLine(g: GitlabInfo, t: (k: string, p?: Record<string, string | number>) => string): string {
  const xs = g.instances
  const vers = [...new Set(xs.map((x) => x.version).filter(Boolean))].join(', ')
  const behind = xs.filter((x) => x.st && x.st.path.length > 0).length
  return subOf(vers, behind > 0 ? t('есть обновление: {n}', { n: behind }) : xs.some((x) => x.st) && t('последняя версия'))
}

export function GitlabSection({ server, g, onChanged }: { server: Server; g: GitlabInfo; onChanged: () => void }) {
  const { t } = useI18n()
  return (
    <div className="srv-sec">
      <div className="muted small">
        {t('Версия GitLab против выпусков на Docker Hub и обязательных остановок обновления, бэкап, фоновые миграции и автообновление. Алерт, когда пора вмешаться: патч своей минорной не стоит дольше {p} дн, версия ниже обязательной остановки, вышедшей больше {s} дн назад, автообновление заблокировано или сломано, упали фоновые миграции.', { p: g.patch_days, s: g.stop_days })}
      </div>
      {g.feed?.err ? (
        <div className="muted small q-stuck">{t('Выпуски GitLab не скачались: {e}', { e: g.feed.err })}</div>
      ) : !g.feed ? (
        <div className="muted small">{t('Выпуски GitLab еще не скачаны: актуальная версия появится после первой выборки')}</div>
      ) : null}
      <div className="chart-card net-card net-card-wide">
        <div className="loc-results docker-clist">
          {g.instances.map((x) => <GitlabRow key={x.key} server={server} x={x} onChanged={onChanged} />)}
        </div>
      </div>
      {g.feed?.ts && (
        <div className="muted small">
          {subOf(t('выпуски с Docker Hub: {t}', { t: fmtRel(g.feed.ts) }), g.feed.stops_src && t('остановки: {s}', { s: g.feed.stops_src }))}
        </div>
      )}
    </div>
  )
}

function GitlabRow({ server, x, onChanged }: { server: Server; x: GitlabInst; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)
  const kind = GITLAB_MUTE + x.key
  const mute = async (hours: number) => {
    setBusy(true)
    try {
      if (hours < 0) {
        await muteServerKeys(server.id, [kind])
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await muteServerKeys(server.id, [], [kind])
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(false)
      onChanged()
    } catch (e) {
      toastError(e) // кнопка остается, можно повторить
    } finally {
      setBusy(false)
    }
  }
  const st = x.st
  const hard = x.notes.some((n) => n.level >= 2)
  const tone = x.mute ? 't-up' : x.level ? (x.level >= 2 ? 't-down' : 't-degraded')
    : (st && st.path.length > 0) || x.notes.length > 0 || (x.bbm?.open ?? 0) > 0 ? 't-soon' : 't-up'
  const last = x.bk?.last
  const skipped = (last?.skipped ?? '').split(',').filter(Boolean)
  const au = x.au
  const behind = au?.behind_since ? Math.floor((Date.now() / 1000 - au.behind_since) / 86400) : 0
  return (
    <div className={`loc-res docker-row gl-row ${tone}${x.mute ? ' q-muted' : ''}`}>
      <span className={`lk-dot${x.mute ? '' : DOT[tone]}`} />
      <div className="docker-c-main">
        <div className="docker-c-name">
          {safeHref(x.url) ? <a href={safeHref(x.url)!} target="_blank" rel="noreferrer" className="mono">{x.label}</a> : <span className="mono">{x.label}</span>}{' '}
          {x.edition && <span className="type-chip">{x.edition.toUpperCase()}</span>}{' '}
          <span className="mono">{x.version || t('версия неизвестна')}</span>
          {x.state !== 'running' && <span className="q-stuck small"> · {t('контейнер {s}', { s: x.state })}</span>}
        </div>
        {st && (
          <div className="docker-c-img small">
            {st.path.length === 0
              ? <span className="muted">{subOf(t('последняя версия'), st.major && t('вышла {v}, переход на новую мажорную - руками', { v: st.major }))}</span>
              : <span>{subOf(
                  t('актуальная {v}', { v: st.latest }),
                  st.path.length === 1 ? t('обновить до {v}', { v: st.path[0] }) : t('путь: {p}', { p: st.path.join(' -> ') }),
                  st.patch && (st.patch.days != null
                    ? t('патч {v} вышел {n} дн назад', { v: st.patch.ver, n: st.patch.days })
                    : t('есть патч {v}', { v: st.patch.ver })),
                  st.stop && t('ниже обязательной остановки {v}', { v: st.stop.ver }),
                )}</span>}
          </div>
        )}
        {x.why && <div className={`docker-c-img small${x.mute ? ' muted' : (x.level ?? 0) >= 2 ? ' q-stuck' : ' t-degraded'}`}>{x.why}</div>}
        <div className="docker-c-img muted small">
          {x.mode === 'build'
            ? subOf(t('сборка: {f}', { f: x.dockerfile || x.dir }), x.from && `FROM ${x.from}`)
            : subOf(t('образ {i}', { i: x.image }), x.files && t('в {f}', { f: x.files }))}
        </div>
        <div className="docker-c-img muted small">
          {subOf(t('контейнер {c}', { c: x.ct }), x.data && t('данные {d}', { d: x.data }), x.config && t('конфиг {d}', { d: x.config }))}
        </div>
        {/* бэкап */}
        <div className={`docker-c-img small${hard ? '' : ' muted'}`}>
          <b>{t('Бэкап')}:</b>{' '}
          {last
            ? subOf(
                t('последний {w} ({r}), {s}', { w: when(last.ts), r: fmtRel(new Date(last.ts * 1000).toISOString()), s: fmtBytes(last.size) }),
                last.info ? (skipped.length ? t('SKIP={s}', { s: skipped.join(',') }) : t('без пропусков')) : '',
                t('архивов {n}, всего {s}', { n: x.bk!.n, s: fmtBytes(x.bk!.total) }),
              )
            : t('архивов gitlab-backup нет')}
          {x.bdir && <span className="muted"> · {x.bdir}</span>}
        </div>
        {x.sched.map((r) => (
          <div key={r.src} className="docker-c-img muted small">
            {subOf(
              r.how === 'timer' ? t('таймер {u} ({w})', { u: r.src, w: r.when }) : t('крон {w} ({f})', { w: r.when, f: r.src }),
              r.db ? (r.skip ? `SKIP=${r.skip}` : t('без пропусков')) : t('только конфиг'),
              r.etc ? t('и конфиг') : '',
              r.how === 'timer' && r.enabled && r.enabled !== 'enabled' && t('таймер {s}', { s: r.enabled }),
              r.last ? t('запуск {w}', { w: when(r.last) }) : '',
            )}
            {r.skip && r.skip.split(',').some((p) => LOST.has(p)) && <span className="q-stuck"> · {t('без части данных')}</span>}
          </div>
        ))}
        <div className="docker-c-img muted small">
          {subOf(
            x.secrets ? t('gitlab-secrets.json есть') : t('gitlab-secrets.json не найден'),
            x.cfg ? t('архив конфига {w}: {p}', { w: when(x.cfg.ts), p: x.cfg.path }) : t('архива конфига нет'),
            x.restic_cfg && t('конфиг в restic-бэкапе Kervax'),
            x.restic_data && t('данные в restic-бэкапе Kervax (копия работающей базы)'),
          )}
        </div>
        {x.notes.map((n) => (
          <div key={n.text} className={`docker-c-img small${n.level >= 2 ? ' q-stuck' : ' t-degraded'}`}>⚠ {n.text}</div>
        ))}
        {/* фоновые миграции */}
        {x.bbm && (
          <div className="docker-c-img small">
            <b>{t('Фоновые миграции')}:</b>{' '}
            {x.bbm.failed > 0 ? (
              <span className="q-stuck">{t('упали: {n}', { n: x.bbm.failed })}</span>
            ) : x.bbm.open > 0 ? (
              <span>{t('идут: {n}', { n: x.bbm.open })}</span>
            ) : (
              <span className="muted">{t('все завершены ({n})', { n: x.bbm.total })}</span>
            )}
            {x.bbm.list.map((m) => (
              <div key={`${m.job}|${m.table}|${m.col}`} className={`mono small${m.st === 4 ? ' q-stuck' : ' muted'}`}>
                {m.job} ({m.table}{m.col ? `.${m.col}` : ''}) - {t(BBM_STATE[m.st] ?? 'статус {n}', { n: m.st })}
              </div>
            ))}
          </div>
        )}
        {/* gitlab-autoupgrade */}
        {au ? (
          <div className="docker-c-img small">
            <b>{t('Автообновление')}:</b>{' '}
            <span className={au.timer === 'enabled' ? '' : 'muted'}>
              {subOf(
                au.timer === 'enabled'
                  ? (au.next ? t('таймер включен, запуск {w}', { w: when(au.next) }) : t('таймер включен'))
                  : t('таймер выключен, обновлять некому'),
                au.svc === 'failed' && t('последний прогон упал'),
                behind > 0 && t('обновление ждет {n} дн', { n: behind }),
                au.gitlab_backup === 'no' && t('бэкап перед шагом выключен'),
              )}
            </span>
            {au.broken && <div className="q-stuck small">{t('сломан: {r}', { r: au.broken })}</div>}
            {au.blocked && <div className="q-stuck small">{t('заблокировано: {r}', { r: au.blocked })}</div>}
            {au.last_result && <div className="muted small mono">{t('последний итог: {r}', { r: au.last_result })}</div>}
            {au.history.length > 0 && (
              <div className="muted small mono" title={au.history.join('\n')}>{au.history[au.history.length - 1]}</div>
            )}
          </div>
        ) : (
          <div className="docker-c-img muted small">{t('gitlab-autoupgrade не стоит')}</div>
        )}
        {x.mute && (
          <div className="small">
            <span className="snooze-active-chip snooze-perm">
              🔇 {x.mute.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: new Date(x.mute.until!).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) }) })}
              {!isViewer && (
                <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(0)}>✕</button>
              )}
            </span>
          </div>
        )}
        {!x.mute && picking && (
          <div className="small q-mute-pick">
            <span className="muted">🔕 {t('не алертить об этом GitLab:')}</span>
            {[
              { h: 24, l: t('1 день') },
              { h: 24 * 7, l: t('1 неделя') },
              { h: -1, l: t('постоянно') },
            ].map(({ h, l }) => (
              <button key={h} className="ghost small" disabled={busy} onClick={() => mute(h)}>{l}</button>
            ))}
            <button className="ghost small" disabled={busy} onClick={() => setPicking(false)}>{t('Отмена')}</button>
          </div>
        )}
      </div>
      {!isViewer && !x.mute ? (
        <button className="ghost small q-mute-btn" disabled={busy} title={t('Не алертить об этом GitLab: на день, неделю или постоянно')}
          onClick={() => setPicking(!picking)}>
          🔕
        </button>
      ) : (
        <span className="q-mute-btn" />
      )}
    </div>
  )
}
