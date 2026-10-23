import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useI18n } from './i18n'

// Готовые шаги "как исправить": текст словами и команда с кнопкой "Копировать". Команды собирает
// бэкенд под ноду, тексты - у вызывающего по id шага (обновления ОС, версии ПО, SSH и доступы,
// старый образ прокси, pg_stat_statements). alt - другой путь вместо шагов выше (черта "или"),
// undo - откат, выполнять не по порядку, а только если что-то не так (черта "откат").
export type FixStep = { id: string; cmd?: string; alt?: boolean; undo?: boolean }

// Команды сами говорят, вышло или нет: последняя строка вроде echo "образ заменен" || echo "НЕ
// ЗАМЕНЕНО: ...", sed, не нашедший строку, иначе молчит (бэкенд: compose_cmd.verdict). Фразы
// пишет бэкенд по-русски, английская панель показывает и копирует их по-английски. Новую фразу
// бэкенда - сюда и в словарь, тест backend/tests/test_fix_verdicts.py сверяет список
const ECHO = [
  'образ заменен',
  'НЕ ЗАМЕНЕНО: строка image другая, поправьте ее руками',
  'контейнер на новом образе',
  'НЕ ОБНОВЛЕН: контейнер на прежнем образе, смотрите вывод выше',
  'контейнер на свежем образе',
  'ОБРАЗ ТОТ ЖЕ: автор образа его еще не пересобрал',
  'репозиторий переведен',
  'НЕ ЗАМЕНЕНО: репозитория NodeSource в sources.list.d нет, поправьте руками',
  'nodejs новой версии',
  'НЕ ОБНОВЛЕН: nodejs прежней версии',
  'файл изменен',
  'НЕ ИЗМЕНЕНО: нужной строки в блоке сервиса нет, поправьте руками',
  'privileged снят',
  'НЕ СНЯТ: контейнер все еще privileged',
  'docker-сокета у контейнера нет',
  'НЕ УБРАН: docker-сокет все еще у контейнера',
  'pg_stat_statements добавлен',
  'НЕ ДОБАВЛЕНО: строка shared_preload_libraries другая, поправьте руками',
].sort((a, b) => b.length - a.length)

function cmdText(cmd: string, lang: string, t: (s: string) => string): string {
  return lang === 'en' ? ECHO.reduce((c, s) => c.split(`${s}"`).join(`${t(s)}"`), cmd) : cmd
}

// Команда шага с кнопкой "Копировать": английская панель показывает и копирует фразы проверки
// по-английски (cmdText)
export function FixCmd({ cmd, onCopy, className = '' }: { cmd: string; onCopy?: () => void; className?: string }) {
  const { t, lang } = useI18n()
  const [copied, setCopied] = useState(false)
  const text = cmdText(cmd, lang, t)
  return (
    <div className={`agent-advice-cmd ${className}`.trim()}>
      <pre>{text}</pre>
      <button className="ghost" onClick={(e) => {
        e.stopPropagation()
        navigator.clipboard?.writeText(text)
        onCopy?.()
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      }}>
        {copied ? t('Скопировано') : t('Копировать')}
      </button>
    </div>
  )
}

// Длинный текст шага: первая фраза - что сделать (ярко), остальное - пояснение мелко под ней.
// Режем по точке перед заглавной: так и в русском, и в английском тексте
const SPLIT = /\.\s+(?=[A-ZА-ЯЁ])/
function splitStep(s: string): [string, string] {
  const m = SPLIT.exec(s)
  return m ? [s.slice(0, m.index + 1), s.slice(m.index + m[0].length)] : [s, '']
}

// Вид один на всю панель: номер шага кружком, текст яркий и короткий, команда на своем фоне - шаги
// не сливаются с текстом. Номер у каждого шага по порядку, у отката и "или" нет. note - пояснение к
// шагу мелко под текстом: все, что не нужно, чтобы просто выполнить шаг. foot - мелкая строка внизу
// (когда пункт уйдет сам, почему не страшно). tip - полный текст шага в подсказке при наведении
export function FixSteps<S extends FixStep>({ steps, text, note, tip, foot, onCopy }: {
  steps: S[]
  text: (st: S) => string
  note?: (st: S) => string | undefined
  tip?: (st: S) => string | undefined
  foot?: string
  // скопировали команду шага: вызывающий может ждать, что ее сейчас выполнят
  onCopy?: (st: S) => void
}) {
  const { t } = useI18n()
  let n = 0
  return (
    <div className="fix-b">
      {steps.map((st, i) => {
        const num = st.alt || st.undo ? 0 : ++n
        const [head, rest] = splitStep(text(st))
        const nt = [rest, note?.(st)].filter(Boolean).join(' ')
        return (
          <div key={st.id + i} className={`fix-b-step${num ? '' : ' fix-b-side'}`}>
            {num > 0 && <span className="fix-b-num">{num}</span>}
            <div className="fix-b-body">
              <div className="fix-b-text" title={tip?.(st)}>
                {!num && <span className="fix-b-tag">{t(st.alt ? 'или' : 'откат')}</span>}
                {head}
              </div>
              {nt && <div className="fix-b-note">{nt}</div>}
              {st.cmd && <FixCmd cmd={st.cmd} onCopy={() => onCopy?.(st)} />}
            </div>
          </div>
        )
      })}
      {foot && <div className="fix-b-foot">{foot}</div>}
    </div>
  )
}

// Замечание исправили, пока оно было на экране: со свежими данными агента оно пропадает, а вместе с
// ним шаги, бэкап и откат, и раздел пустеет без объяснений. Вместо этого на его месте зеленый бар
// "что изменилось" с кнопкой "закрыть"; пустое состояние раздела - только после нее. Помним только
// то, что видели здесь: состояние в компоненте раздела и уходит вместе с ним (другой раздел,
// закрыли окно). items - замечания раздела вместе с решенными "оставить как есть" (live = false):
// решение - не исправление, бар не нужен. null - данных нет (агент не прислал), это не исправление.
// text - что изменилось, по свежим данным вызывающего; on = false - раздел не на экране, забыть все
export type FixDone = { key: string; text: string }

export function useFixDone<T>(
  items: T[] | null | undefined,
  key: (x: T) => string,
  text: (gone: T) => string,
  { live, on = true }: { live?: (x: T) => boolean; on?: boolean } = {},
): { done: FixDone[]; bars: ReactNode } {
  const seen = useRef(new Map<string, T>())
  const [done, setDone] = useState<FixDone[]>([])
  useEffect(() => {
    if (!on) {
      seen.current.clear()
      if (done.length) setDone([])
      return
    }
    if (!items) return
    const all = new Set(items.map(key))
    const gone: FixDone[] = []
    for (const [k, x] of seen.current)
      if (!all.has(k)) {
        gone.push({ key: k, text: text(x) })
        seen.current.delete(k)
      }
    const back = new Set<string>()
    for (const x of items) {
      const k = key(x)
      if (live && !live(x)) seen.current.delete(k)
      else {
        seen.current.set(k, x)
        back.add(k)
      }
    }
    // вернулось - бар про него уже неправда
    if (gone.length || done.some((d) => back.has(d.key)))
      setDone([...done.filter((d) => !back.has(d.key)), ...gone])
  })
  const bars = done.length > 0 && (
    <FixDoneBars done={done} onClose={(k) => setDone(done.filter((d) => d.key !== k))} />
  )
  return { done, bars }
}

export function FixDoneBars({ done, onClose }: { done: FixDone[]; onClose: (key: string) => void }) {
  const { t } = useI18n()
  return (
    <div className="fix-done-list">
      {done.map((d) => (
        <div key={d.key} className="fix-done" role="status">
          <span className="lk-dot lk-dot-ok" />
          <span className="fix-done-text">{d.text}</span>
          <button className="ghost small" onClick={() => onClose(d.key)}>{t('закрыть')}</button>
        </div>
      ))}
    </div>
  )
}
