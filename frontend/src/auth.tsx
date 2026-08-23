import { createContext, useContext } from 'react'
import type { Role } from './api'

// Права текущего пользователя для гейтинга UI. Backend всё равно проверяет всё сам
// (роль, разделы, группы) — здесь только чтобы не показывать то, что даст 403.
//   isViewer — только чтение (роль viewer)
//   isAdmin  — учётки и настройки панели (роль admin; editor их не получает)
//   sections — разрешённые вкладки; пусто = все
export type Auth = {
  role: Role
  isViewer: boolean
  isAdmin: boolean
  sections: string[]
  username: string // чтобы отличать свою учётку от чужой (напр. запрет самоудаления)
}

const AuthContext = createContext<Auth>({
  role: 'admin',
  isViewer: false,
  isAdmin: true,
  sections: [],
  username: '',
})

export const AuthProvider = AuthContext.Provider

export function useAuth(): Auth {
  return useContext(AuthContext)
}
