import { useEffect } from 'react'

// Адрес в строке браузера следует за тем, что открыто на экране. Раньше он не менялся
// никогда: какую бы карточку человек ни открыл, в урле оставалась главная, и ссылку на
// монитор или сервер нельзя было никому дать. Параметры те же, по которым панель
// открывает карточку из алерта (?check=id, ?server=id), так что ссылка работает и в
// чужом браузере, и после F5.
//
// Пишем через replaceState: новых записей в истории не появляется и кнопка "назад"
// ведёт себя ровно как раньше. Писателей двое - раздел (App) и открытая карточка
// (страница раздела), поэтому оба кладут своё в общий объект, а строку собирает flush:
// каждый сам по себе затирал бы чужой параметр.
const cur: { section: string; cards: Record<string, number> } = { section: '', cards: {} }

function flush(): void {
  const open = Object.entries(cur.cards)
  const q = open.length
    ? open.map(([k, v]) => `${k}=${v}`).join('&')
    : cur.section && cur.section !== 'home'
      ? `section=${cur.section}`
      : ''
  const next = window.location.pathname + (q ? `?${q}` : '') + window.location.hash
  if (next !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState({}, '', next)
  }
}

/** Раздел панели в адресе: ?section=servers. Открытая карточка главнее - она и так
    говорит, в каком разделе искать. */
export function useUrlSection(section: string): void {
  useEffect(() => {
    cur.section = section
    flush()
  }, [section])
}

/** Открытая карточка в адресе: ?server=12. При закрытии карточки и при уходе из
    раздела параметр убирается. */
export function useUrlCard(param: string, id: number | null | undefined): void {
  useEffect(() => {
    if (id == null) delete cur.cards[param]
    else cur.cards[param] = id
    flush()
    return () => {
      delete cur.cards[param]
      flush()
    }
  }, [param, id])
}
