import { createContext, useContext } from 'react'
import type { Role } from './api'

// Права текущего пользователя для гейтинга UI. Backend всё равно проверяет всё сам
// (роль, разделы, группы) — здесь только чтобы не показывать то, что даст 403.
//   isViewer — только чтение (роль viewer)
//   canWrite — можно ли что-то менять: кнопки изменений (рестарт, мьют, настройка бэкапа...) рисуются
//              только при нем. Запрос на изменение от наблюдателя api.ts не отправит вовсе
//   canLogs  — можно ли читать логи контейнеров, подов и журнал systemd: всем, кроме наблюдателя;
//              ему - если админ поставил галку (в логах бывают пароли и токены)
//   isAdmin  — учётки и настройки панели (роль admin; editor их не получает)
//   sections — разрешённые вкладки; пусто = все
export type Auth = {
  role: Role
  isViewer: boolean
  canWrite: boolean
  canLogs: boolean
  isAdmin: boolean
  sections: string[]
  username: string // чтобы отличать свою учётку от чужой (напр. запрет самоудаления)
}

const AuthContext = createContext<Auth>({
  role: 'admin',
  isViewer: false,
  canWrite: true,
  canLogs: true,
  isAdmin: true,
  sections: [],
  username: '',
})

export const AuthProvider = AuthContext.Provider

export function useAuth(): Auth {
  return useContext(AuthContext)
}
