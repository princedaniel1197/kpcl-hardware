// The application shell, ported from Sentinel's components/Shell.tsx
// (sentinel-v2): the same 236 px ruled sidebar with grouped navigation, the
// same sticky header with a station and a period selector and entity search,
// the same drawer on narrow screens.
//
// Two deliberate differences. There is no English/ಕನ್ನಡ toggle: CRPMS's
// strings are not translated, and a toggle that changed nothing would be a
// false control. And where Sentinel's header ends with that toggle, CRPMS's
// ends with the state of the live event stream -- an operator must be able to
// see when the picture has stopped moving.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useRouter } from '../lib/router'
import { PERIODS } from '../lib/format'
import { useData } from '../lib/data'

export const APP_NAME = 'CRPMS'

const GROUPS = [
  { key: 'Overview', items: [{ href: '/', label: 'Overview' }] },
  { key: 'Assets', items: [
    { href: '/assets', label: 'Asset tree' },
    { href: '/tags', label: 'Tag register' },
  ] },
  { key: 'Live data', items: [
    { href: '/unit', label: 'Unit overview' },
    { href: '/trends', label: 'Trends' },
    { href: '/replay', label: 'Replay' },
    { href: '/rig', label: 'Bench rig' },
  ] },
  { key: 'Events & KPIs', items: [
    { href: '/events', label: 'Event frames' },
    { href: '/kpis', label: 'KPI register' },
  ] },
  { key: 'Health', items: [
    { href: '/health', label: 'Collector & buffer' },
    { href: '/health/gaps', label: 'Gaps & losses' },
    { href: '/health/sources', label: 'Source health' },
  ] },
  { key: 'Data sources', items: [{ href: '/sources', label: 'Data sources' }] },
  { key: 'Settings', items: [{ href: '/settings', label: 'Settings' }] },
]

const STATION_LABELS = { RTPS: 'RTPS — Raichur', BTPS: 'BTPS — Ballari', YTPS: 'YTPS — Yermarus' }

function Sidebar({ onNavigate }) {
  const { path } = useRouter()
  // Longest-prefix match, so /health/gaps highlights only "Gaps & losses".
  const all = GROUPS.flatMap((g) => g.items.map((i) => i.href))
  const active = all
    .filter((h) => path === h || (h !== '/' && path.startsWith(h + '/')))
    .sort((a, b) => b.length - a.length)[0]

  return (
    <nav className="h-full overflow-y-auto pb-10" style={{ background: 'var(--paper)' }}>
      <div className="px-4 pt-4 pb-3 rule-master">
        <Link to="/" onClick={onNavigate}>
          <div className="display text-[21px] leading-none">{APP_NAME}</div>
          <div className="text-[10.5px] uppercase tracking-[0.13em] text-[var(--muted)] mt-1.5">
            Karnataka Power Corporation
          </div>
        </Link>
      </div>
      <div className="px-2 pt-3">
        {GROUPS.map((g) => (
          <div key={g.key} className="mb-3.5">
            <div className="px-2 text-[10px] uppercase tracking-[0.13em] text-[var(--faint)] mb-1">{g.key}</div>
            {g.items.map((it) => {
              const on = it.href === active
              return (
                <Link key={it.href} to={it.href} onClick={onNavigate}
                  className="block px-2 py-[5px] text-[13px] rounded-[2px]"
                  style={on ? { background: 'var(--wash)', fontWeight: 600, boxShadow: 'inset 2px 0 0 var(--gold)' } : undefined}>
                  {it.label}
                </Link>
              )
            })}
          </div>
        ))}
      </div>
    </nav>
  )
}

function Search() {
  const { tags, assets } = useData()
  const { navigate, scope } = useRouter()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const box = useRef(null)

  useEffect(() => {
    const h = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const index = useMemo(() => [
    ...assets.map((a) => ({ id: a.asset_code, label: a.name, kind: a.level, href: `/assets/${a.asset_code}` })),
    ...tags.map((t) => ({ id: t.name, label: t.description ? `${t.name} — ${t.description}` : t.name, kind: 'tag', href: `/tags/${t.name}` })),
  ], [tags, assets])

  const results = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (s.length < 2) return []
    return index.filter((r) => r.label.toLowerCase().includes(s) || r.id.toLowerCase().includes(s)).slice(0, 12)
  }, [q, index])

  return (
    <div className="relative" ref={box}>
      <input className="input w-[180px] sm:w-[230px]" placeholder="Search tags and assets…" value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }} onFocus={() => setOpen(true)} />
      {open && results.length > 0 && (
        <div className="absolute right-0 mt-1 w-[320px] panel z-50 max-h-[60vh] overflow-y-auto">
          {results.map((r) => (
            <a key={r.id + r.href} href={r.href + scope}
              onClick={(e) => { e.preventDefault(); setOpen(false); setQ(''); navigate(r.href + scope) }}
              className="flex items-baseline justify-between gap-2 px-3 py-2 text-[12.5px] hover:bg-[var(--wash)]"
              style={{ borderBottom: '0.5px solid var(--hairline)' }}>
              <span className="truncate">{r.label}</span>
              <span className="text-[10px] uppercase tracking-wider text-[var(--faint)] shrink-0">{r.kind}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

function StreamState() {
  const { link, streamCount } = useData()
  const live = link === 'connected'
  return (
    <span className={`chip ${live ? 'chip-muted' : 'chip-danger'}`}
          title={live ? 'The collector\'s event stream is connected; values update as they are acquired.'
                      : 'The live event stream is not connected: values on screen are not updating from the collector.'}>
      {live ? `Live · ${streamCount.toLocaleString('en-IN')} events` : `Stream ${link}`}
    </span>
  )
}

function Header({ onMenu }) {
  const { query, setScope } = useRouter()
  const { stations } = useData()
  const station = query.get('station') ?? 'ALL'
  const period = query.get('period') ?? PERIODS[0].id

  return (
    <header className="rule-hair sticky top-0 z-40 no-print" style={{ background: 'var(--paper)' }}>
      <div className="flex items-center gap-2 sm:gap-3 px-3 sm:px-6 py-2.5 flex-wrap">
        <button className="btn lg:hidden" onClick={onMenu} aria-label="Menu">☰</button>
        <select className="select" value={station} onChange={(e) => setScope('station', e.target.value)} aria-label="Station">
          <option value="ALL">All stations</option>
          {stations.map((s) => <option key={s} value={s}>{STATION_LABELS[s] ?? s}</option>)}
        </select>
        <select className="select" value={period} onChange={(e) => setScope('period', e.target.value)} aria-label="Period">
          {PERIODS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>
        <div className="flex-1" />
        <Search />
        <StreamState />
      </div>
    </header>
  )
}

export default function Shell({ children }) {
  const [menu, setMenu] = useState(false)
  return (
    <div className="app-grid min-h-screen lg:grid" style={{ gridTemplateColumns: '236px 1fr' }}>
      <aside className="hidden lg:block h-screen sticky top-0 no-print" style={{ borderRight: '0.5px solid var(--hairline)' }}>
        <Sidebar />
      </aside>
      {menu && (
        <div className="fixed inset-0 z-50 lg:hidden no-print">
          <div className="absolute inset-0" style={{ background: 'rgba(42,36,24,0.35)' }} onClick={() => setMenu(false)} />
          <div className="absolute left-0 top-0 h-full w-[262px]" style={{ background: 'var(--paper)', borderRight: '0.5px solid var(--hairline)' }}>
            <Sidebar onNavigate={() => setMenu(false)} />
          </div>
        </div>
      )}
      <div className="min-w-0">
        <Header onMenu={() => setMenu(true)} />
        <main className="px-3 sm:px-6 py-5 max-w-[1500px] print-full">{children}</main>
      </div>
    </div>
  )
}

export { STATION_LABELS }
