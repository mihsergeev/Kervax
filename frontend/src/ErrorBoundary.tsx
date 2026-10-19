import { Component, type ErrorInfo, type ReactNode } from 'react'
import { errorFields, reportFront } from './errorLog'
import { useI18n } from './i18n'

// Экран, который упал при отрисовке, раньше превращался в белую страницу без единого слова.
// Теперь ошибка уходит в журнал ошибок панели, а человек видит, что случилось, и может
// сообщить о проблеме или перезагрузить страницу. resetKey - смена раздела снимает поломку.
type Props = { children: ReactNode; resetKey: string; onReport: () => void }
type State = { error: Error | null; key: string }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (state.error && props.resetKey !== state.key) return { error: null, key: props.resetKey }
    if (!state.error && props.resetKey !== state.key) return { key: props.resetKey }
    return null
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const { message, stack } = errorFields(error)
    reportFront('render', message, stack, info.componentStack || '')
  }

  render(): ReactNode {
    if (this.state.error) return <Crashed error={this.state.error} onReport={this.props.onReport} />
    return this.props.children
  }
}

function Crashed({ error, onReport }: { error: Error; onReport: () => void }) {
  const { t } = useI18n()
  return (
    <div className="card pe-crash">
      <h3>{t('Этот экран сломался')}</h3>
      <p className="muted small">
        {t('Ошибка уже записана в журнал ошибок панели. Можно перейти в другой раздел, перезагрузить страницу или добавить пару слов о том, что делали.')}
      </p>
      <pre className="pe-pre">{`${error.name}: ${error.message}`}</pre>
      <div className="pe-acts">
        <button onClick={onReport}>{t('Сообщить о проблеме')}</button>
        <button className="ghost" onClick={() => window.location.reload()}>
          {t('Перезагрузить страницу')}
        </button>
      </div>
    </div>
  )
}
