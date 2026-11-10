import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import type { ServerEnroll } from '../src/api'
import { I18nProvider } from '../src/i18n'
import { InstallModal } from '../src/ServersPage'
import '../src/App.css'

const enroll = (window as unknown as { installFixture: ServerEnroll }).installFixture
createRoot(document.getElementById('root')!).render(createElement(I18nProvider, {
  children: createElement(InstallModal, { enroll, onClose: () => {} }),
}))
