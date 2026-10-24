import { useEffect, useRef } from 'react'

// Esc закрывает окно. Окна вкладываются (график поверх карточки сервера, шаги поверх окна бэкапа),
// поэтому закрывается только верхнее - открытое последним. close - та же функция, что у кнопки
// "Закрыть": если она спрашивает про несохраненное, спросит и здесь
const stack: number[] = []
let seq = 0

export function useEscape(close: () => void, on = true): void {
  const ref = useRef(close)
  useEffect(() => {
    ref.current = close
  })
  useEffect(() => {
    if (!on) return
    const id = ++seq
    stack.push(id)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || stack[stack.length - 1] !== id) return
      e.preventDefault()
      ref.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const i = stack.indexOf(id)
      if (i >= 0) stack.splice(i, 1)
    }
  }, [on])
}
