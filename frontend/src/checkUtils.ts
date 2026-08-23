// Мелкие помощники разделa «Сайты», нужные сразу нескольким экранам.

type T = (s: string, v?: Record<string, string | number>) => string

// Подпись срока (TLS-сертификат / регистрация домена).
//
// Ноль дней — это «истекает сегодня», а НЕ «истёк»: до самой даты может оставаться
// почти сутки. Раньше здесь стояло `days <= 0 ? 'истёк'`, и монитор живого сайта
// в день истечения показывал пугающее «истёк». Та же логика на бэкенде —
// backend/app/collector.py, _expiry_text (тексты алертов).
export function expiryText(days: number, t: T): string {
  if (days < 0) return t('истёк')
  if (days === 0) return t('сегодня')
  return t('{n} дн.', { n: days })
}

// Двухуровневые TLD, где регистрируемое имя — три последних метки. Тот же список,
// что в backend/app/checks.py (_MULTI_TLD): расхождение означало бы, что панель
// группирует не так, как считает сроки.
const MULTI_TLD = new Set([
  'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'me.uk', 'com.au', 'net.au', 'org.au',
  'co.nz', 'com.cy', 'co.il', 'com.br', 'com.tr', 'co.jp', 'com.sg', 'com.hk',
  'com.ua', 'co.za', 'com.mx', 'com.ar',
])

// Имя, которое реально РЕГИСТРИРУЮТ и продлевают: у gitlab.example.com это
// example.com. Срок принадлежит ему одному — сколько бы мониторов ни стояло
// на его поддоменах.
export function registrableDomain(target: string): string {
  let host = target.trim()
  const i = host.indexOf('://')
  if (i >= 0) host = host.slice(i + 3)
  host = host.split('/')[0].split(':')[0].replace(/[.]+$/, '').toLowerCase()
  const labels = host.split('.').filter(Boolean)
  if (labels.length <= 2) return labels.join('.')
  const last2 = labels.slice(-2).join('.')
  return MULTI_TLD.has(last2) ? labels.slice(-3).join('.') : last2
}
