import { useState, type ReactNode, type Ref } from 'react'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { useEscape } from './useEscape'

// Окно правки монитора: меню слева по разделам, сохранение одно, в подвале, как у окна правки
// сервера (ServerEdit.tsx). Разделы и их содержимое дает форма монитора (AddMonitor в editing),
// у старого монитора сертификата - карточка (CertEdit)

export function CheckEditWindow({
  title,
  secs,
  sec,
  side,
  dirty,
  busy,
  err,
  canSave,
  mainRef,
  onGo,
  onSave,
  onClose,
  children,
}: {
  title: string
  secs: SecDef[]
  sec: string
  side?: Record<string, string>
  dirty: boolean
  busy: boolean
  err: string | null
  canSave: boolean
  mainRef?: Ref<HTMLDivElement>
  onGo: (id: string) => void
  // нет - сохранять нечего, в подвале только "Закрыть"
  onSave?: () => void
  onClose: () => void
  children: ReactNode
}) {
  const { t } = useI18n()
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }
  useEscape(close)
  const cur = secs.find((x) => x.id === sec) ?? secs[0]
  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{title}</h3>
          <span className="muted small">{t('настройки')}</span>
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="al-body">
          <CardNav secs={secs} active={cur.id} info={{}} side={side} onGo={onGo} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>
            <div className="srv-sec">{children}</div>
          </div>
        </div>
        <div className="al-foot">
          {err ? (
            <span className="form-error small">{err}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')}
            </span>
          ) : null}
          <span className="al-foot-btns">
            {onSave ? (
              <>
                <button className="ghost" onClick={close} disabled={busy}>
                  {t('Отмена')}
                </button>
                <button onClick={onSave} disabled={busy || !dirty || !canSave}>
                  {busy ? t('…') : t('Сохранить')}
                </button>
              </>
            ) : (
              <button className="ghost" onClick={close}>
                {t('Закрыть')}
              </button>
            )}
          </span>
        </div>
      </div>
    </div>
  )
}

// раздел "Удаление": отдельно внизу меню, подальше от "Сохранить"
// canOff - монитор можно выключить в этом же окне (у старого монитора сертификата нельзя)
export function CheckDeleteSec({ busy, canOff, onDelete }: { busy: boolean; canOff: boolean; onDelete: () => void }) {
  const { t } = useI18n()
  return (
    <div className="se-danger">
      <span>
        <b>{t('Удалить монитор')}</b>
        <span className="muted small">
          {canOff
            ? t('Вместе с ним удалятся история проверок и инциденты. Если монитор не нужен на время, лучше его выключить.')
            : t('Вместе с ним удалятся история проверок и инциденты.')}
        </span>
      </span>
      <button className="ghost danger-btn" onClick={onDelete} disabled={busy}>
        🗑 {t('Удалить монитор')}
      </button>
    </div>
  )
}

// старый монитор сертификата формы не имеет: его заводят и меняют только через API
const CERT_SECS: SecDef[] = [
  { id: 'cert', label: 'Монитор', group: '' },
  { id: 'note', label: 'Заметка', group: '' },
  { id: 'del', label: 'Удаление', group: 'Другое' },
]

export function CertEdit({
  title,
  note,
  initialSec,
  err,
  onDelete,
  onClose,
}: {
  title: string
  note: ReactNode
  initialSec?: string
  err: string | null
  onDelete?: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const secs = onDelete ? CERT_SECS : CERT_SECS.filter((x) => x.id !== 'del')
  const [sec, setSec] = useState(() => (secs.some((x) => x.id === initialSec) ? initialSec! : 'cert'))
  return (
    <CheckEditWindow title={title} secs={secs} sec={sec} dirty={false} busy={false} err={err} canSave={false} onGo={setSec} onClose={onClose}>
      {sec === 'note' ? (
        note
      ) : sec === 'del' && onDelete ? (
        <CheckDeleteSec busy={false} canOff={false} onDelete={onDelete} />
      ) : (
        <p className="muted small">
          {t('Это монитор сертификата старого типа: его настройки меняются только через API. Удалить его можно в разделе "Удаление", а за сертификатом следит и обычный монитор сайта.')}
        </p>
      )}
    </CheckEditWindow>
  )
}
