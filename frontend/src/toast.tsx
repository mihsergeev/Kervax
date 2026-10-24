import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError } from './api'
import { tr } from './i18n'

// Отказ мелкого действия (заглушить, оставить, удалить, перетащить): раньше catch его глотал, и
// кнопка молча возвращалась. Теперь причина всплывает снизу на несколько секунд, рядом с любым окном.
// Формы со своим полем ошибки показывают ее у себя, сюда - то, где места под текст нет
type Msg = { id: number; text: string }
let push: ((m: Msg) => void) | null = null
let seq = 0

// eslint-disable-next-line react-refresh/only-export-components
export function toastError(e: unknown): void {
  // протухший вход App лечит сам: показывает страницу входа
  if (e instanceof ApiError && e.status === 401) return
  const text = e instanceof Error ? e.message : typeof e === 'string' ? e : tr('Ошибка')
  push?.({ id: ++seq, text: text || tr('Ошибка') })
}

export function Toasts() {
  const [items, setItems] = useState<Msg[]>([])
  useEffect(() => {
    push = (m) => {
      setItems((x) => [...x.filter((y) => y.text !== m.text).slice(-2), m])
      window.setTimeout(() => setItems((x) => x.filter((y) => y.id !== m.id)), 7000)
    }
    return () => {
      push = null
    }
  }, [])
  if (!items.length) return null
  return createPortal(
    <div className="toasts" role="alert">
      {items.map((m) => (
        <button key={m.id} type="button" className="toast toast-err" title={tr('Закрыть')}
          onClick={() => setItems((x) => x.filter((y) => y.id !== m.id))}>
          {m.text}
        </button>
      ))}
    </div>,
    document.body,
  )
}
