import type { KeyboardEvent } from 'react'

// Rows contain their own action buttons: only a key on the row itself opens it.
export function activateHomeRow(e: KeyboardEvent<HTMLElement>, open: () => void) {
  if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
    e.preventDefault()
    open()
  }
}
