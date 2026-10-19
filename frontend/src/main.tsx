import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './App.css'
import App from './App.tsx'
import { I18nProvider } from './i18n'
import { installErrorHandlers } from './errorLog'

// ошибки интерфейса уходят в журнал ошибок панели (errorLog.ts)
installErrorHandlers()

// применяем тему до рендера, чтобы не мигало (по умолчанию тёмная)
if (localStorage.getItem('kervax_theme') === 'light') {
  document.documentElement.setAttribute('data-theme', 'light')
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </StrictMode>,
)
