export const TOKEN_KEY = 'kervax_token'

// Запросы уже открытого экрана не должны незаметно получить права другой учетки
// между изменением localStorage в соседней вкладке и обработкой события storage.
let activeToken = localStorage.getItem(TOKEN_KEY)

export function getToken(): string | null {
  return activeToken
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
  activeToken = token
}

export function isCurrentSession(token: string | null): boolean {
  return token === activeToken && token === localStorage.getItem(TOKEN_KEY)
}
