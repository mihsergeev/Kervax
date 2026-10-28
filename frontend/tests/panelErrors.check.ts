// Записи журнала ошибок панели 24-26.10, проверка интерфейса: гоняет
// backend/tests/test_panel_errors_front.py (сборка rolldown + node). Печатает JSON с итогом.
// - stale: вкладка со старой сборкой узнает, что панель уже обновили (staleOf/bundleOf)
// - labels: подписи логов web-rate из ответа сервера учетке только для просмотра без галки
//   "может читать логи" (пути лога бэкенд вырезает), файл с ответом - первым аргументом
import { readFileSync } from 'node:fs'
import { bundleOf, staleOf } from '../src/errorLog'
import { webLogLabel } from '../src/serverUtils'

const page = (src: string) =>
  `<!doctype html><html><head><script type="module" crossorigin src="${src}"></script></head></html>`
const dev = '<!doctype html><script type="module" src="/src/main.tsx"></script>'

const stale = {
  bundle: bundleOf(page('/assets/index-AbC123_x.js')),
  dev_bundle: bundleOf(dev),
  same: staleOf('/assets/index-old1.js', page('/assets/index-old1.js')),
  same_abs: staleOf('https://kervax.example/assets/index-old1.js', page('/assets/index-old1.js')),
  updated: staleOf('/assets/index-old1.js', page('/assets/index-new2.js')),
  dev: staleOf(null, dev),
  broken_answer: staleOf('/assets/index-old1.js', '<html>502 Bad Gateway</html>'),
}

type RateLog = { log?: string; name?: string; sites?: string[] }
const srv = JSON.parse(readFileSync(process.argv[2], 'utf8')) as {
  last_report: { extras: { 'web-rate': { logs: RateLog[] }; units: { units: { log?: string[] }[] } } }
}
const logs = srv.last_report.extras['web-rate'].logs
const units = srv.last_report.extras.units.units

console.log(JSON.stringify({
  stale,
  has_log: logs.map((l) => 'log' in l),
  labels: logs.map((l) => webLogLabel(l)),
  unit_logs: units.map((u) => (u.log ?? []).length),
}))
