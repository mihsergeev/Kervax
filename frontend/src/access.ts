// Подписи ролей и разделов: окно "Пользователи" и "Моя учетка".

// Разделы верхнего меню. Ключи ДОЛЖНЫ совпадать с ALL_SECTIONS в backend/app/deps.py:
// по ним бэкенд решает, какие маршруты открыты, а фронт - какие вкладки рисовать.
export const SECTIONS: { key: string; label: string }[] = [
  { key: 'sites', label: 'Сайты' },
  { key: 'servers', label: 'Серверы' },
  { key: 'docker', label: 'Докер' },
  { key: 'kuber', label: 'Кубер' },
  { key: 'services', label: 'Сервисы' },
  { key: 'backups', label: 'Бэкапы' },
]
export const SECTION_LABEL: Record<string, string> = Object.fromEntries(SECTIONS.map((x) => [x.key, x.label]))

export const ROLE_LABEL: Record<string, string> = {
  admin: 'Админ',
  editor: 'Правка',
  viewer: 'Только просмотр',
}
export const ROLE_SHORT: Record<string, string> = {
  admin: 'админ',
  editor: 'правка',
  viewer: 'просмотр',
}
export const ROLE_WHAT: Record<string, string> = {
  admin: 'Может все, включая учетки и настройки панели.',
  editor: 'Правит в разрешенных разделах, учетки и настройки панели ему не видны.',
  viewer: 'Только смотрит, ничего не меняет.',
}

type T = (s: string, p?: Record<string, string | number>) => string
type Scoped = { role: string; sections?: string[] | null; server_groups?: string[] | null; site_groups?: string[] | null }

// Что видно учетке одной строкой: окно "Пользователи" и плашка входа под чужой учеткой
export const scopeText = (u: Scoped, t: T) =>
  u.role === 'admin'
    ? t('все разделы и группы')
    : [
        (u.sections?.length ?? 0) === 0
          ? t('все разделы')
          : (u.sections ?? []).map((x) => t(SECTION_LABEL[x] ?? x)).join(', '),
        (u.server_groups?.length ?? 0) === 0
          ? t('все серверы')
          : t('серверы: {g}', { g: (u.server_groups ?? []).join(', ') }),
        (u.site_groups?.length ?? 0) === 0 ? t('все сайты') : t('сайты: {g}', { g: (u.site_groups ?? []).join(', ') }),
      ].join(' · ')
