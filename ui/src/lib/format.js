// Number and time formatting, in Sentinel's conventions (en-IN grouping).
//
// Times are shown in IST, the plant's time, with the zone written out; the
// archive stores UTC and every time carries the UTC instant in its tooltip.

const IST = 'Asia/Kolkata'

export function num(v, digits) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const x = Number(v)
  // A whole number (a count, or a digital state archived as 0 or 1) shows no
  // decimals; anything else by magnitude.
  const d = digits ?? (Number.isInteger(x) ? 0
    : Math.abs(x) >= 1000 ? 0 : Math.abs(x) >= 100 ? 1 : Math.abs(x) >= 1 ? 2 : 3)
  return x.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export const pct = (v, digits = 1) => (v === null || v === undefined ? '—' : `${num(v, digits)}%`)

export function time(iso, { date = false, ms = false } = {}) {
  if (!iso) return '—'
  const d = new Date(iso)
  const t = d.toLocaleTimeString('en-GB', { timeZone: IST, hour12: false })
  const frac = ms ? `.${String(d.getUTCMilliseconds()).padStart(3, '0')}` : ''
  if (!date) return `${t}${frac} IST`
  const day = d.toLocaleDateString('en-GB', { timeZone: IST, day: '2-digit', month: 'short', year: 'numeric' })
  return `${day}, ${t}${frac} IST`
}

export const utc = (iso) => (iso ? new Date(iso).toISOString().replace('.000Z', 'Z') : '')

export function ago(iso, now = Date.now()) {
  if (!iso) return null
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  if (s < 90) return `${Math.round(s)} s ago`
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 172800) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 120) return `${num(seconds, 0)} s`
  if (seconds < 7200) return `${num(seconds / 60, 1)} min`
  return `${num(seconds / 3600, 1)} h`
}

export const PERIODS = [
  { id: '10', label: 'Last 10 minutes', minutes: 10 },
  { id: '60', label: 'Last hour', minutes: 60 },
  { id: '1440', label: 'Last 24 hours', minutes: 1440 },
]
export const periodOf = (id) => PERIODS.find((p) => p.id === id) ?? PERIODS[0]
