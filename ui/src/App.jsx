// CRPMS — the visualisation, as a module of the Sentinel product: the same
// ivory ledger, shell and components (see src/styles/sentinel.css and
// src/components/), with CRPMS's own rules on top: every value carries its
// quality, its source timestamp and its provenance, and a value that is not
// Good is never shown as a number it does not have.
//
// This file holds the sign-in, the routes, and the DataProvider: the
// dashboard's one poll (§528, 2-3 s) and the collector's live event stream.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AuthError, connectEvents, getAllEvents, getAssets, getAttributes, getKpis,
         getLatest, getStatus, getTagHealth, getTags, getToken, getWhoami, setToken } from './api'
import { DataContext, useData } from './lib/data'
import { RouterProvider, match, useRouter } from './lib/router'
import { klassOf } from './lib/quality'
import Shell, { APP_NAME } from './components/Shell'
import Overview from './pages/Overview'
import { AssetTree, AssetDetail } from './pages/Assets'
import { TagRegister, TagDetail } from './pages/Tags'
import { UnitOverview, Trends, ReplayPage, BenchRig } from './pages/Live'
import { EventFrames, EventDetail, KpiRegister, KpiDetail } from './pages/EventsKpis'
import { CollectorHealth, Gaps, SourceHealth } from './pages/Health'
import { DataSources, Settings, NotFound } from './pages/Other'

// Every API call is authenticated (§509). Without a valid token there is
// nothing to show, so the first thing on screen is the sign-in.
export default function App() {
  const [who, setWho] = useState(null)
  const [checked, setChecked] = useState(false)
  const [unreachable, setUnreachable] = useState(false)

  const check = useCallback(async () => {
    if (!getToken()) { setWho(null); setChecked(true); return }
    try {
      setWho(await getWhoami())
      setUnreachable(false)
    } catch (e) {
      // A refused token is cleared; an API that did not answer is said so,
      // rather than dropping the viewer back at the sign-in without a word.
      if (e instanceof AuthError) { setToken(null); setUnreachable(false) } else setUnreachable(true)
      setWho(null)
    }
    setChecked(true)
  }, [])

  useEffect(() => { check() }, [check])
  const signOut = useCallback(() => { setToken(null); setWho(null) }, [])

  if (!checked) return <div className="p-6 text-[13px] text-[var(--muted)]">Loading ledger…</div>
  if (!who) return <SignIn unreachable={unreachable} onToken={(t) => { setToken(t); check() }} />
  return (
    <RouterProvider>
      <DataProvider who={who} onSignOut={signOut}>
        <Shell><Routes /></Shell>
      </DataProvider>
    </RouterProvider>
  )
}

function SignIn({ onToken, unreachable }) {
  const [value, setValue] = useState('')
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <form className="panel w-full max-w-[440px]"
            onSubmit={(e) => { e.preventDefault(); if (value.trim()) onToken(value.trim()) }}>
        <div className="px-5 pt-5 pb-3 rule-master">
          <div className="display text-[21px] leading-none">{APP_NAME}</div>
          <div className="text-[10.5px] uppercase tracking-[0.13em] text-[var(--muted)] mt-1.5">
            Karnataka Power Corporation
          </div>
        </div>
        <div className="px-5 py-4">
          <h1 className="text-[22px] leading-tight">Sign in</h1>
          <p className="text-[12.5px] text-[var(--muted)] mt-1">
            Paste an access token. Tokens are issued with <code>make token</code> (or
            <code> python -m ops.access create</code>) and shown once; the server keeps only their SHA-256.
          </p>
          <input type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)}
                 aria-label="Access token" className="input w-full mt-3" placeholder="Access token" />
          <button type="submit" className="btn btn-primary mt-3">Sign in</button>
          {unreachable && (
            <p className="reason reason-danger mt-3" role="alert">
              The CRPMS API did not answer at this address. It runs on the station laptop with the collector and
              the archive (<code>make start</code>); a copy of this interface hosted elsewhere has no data behind it.
            </p>
          )}
        </div>
        <div className="folio px-5 pb-4 mt-0">CRPMS · monitoring overlay, read-only</div>
      </form>
    </div>
  )
}

const ROUTES = [
  ['/', Overview],
  ['/assets', AssetTree], ['/assets/:code', AssetDetail],
  ['/tags', TagRegister], ['/tags/:name', TagDetail],
  ['/unit', UnitOverview], ['/trends', Trends], ['/replay', ReplayPage], ['/rig', BenchRig],
  ['/events', EventFrames], ['/events/:id', EventDetail],
  ['/kpis', KpiRegister], ['/kpis/:kpi/:asset', KpiDetail],
  ['/health', CollectorHealth], ['/health/gaps', Gaps], ['/health/sources', SourceHealth],
  ['/sources', DataSources], ['/settings', Settings],
]

function Routes() {
  const { path } = useRouter()
  const { ready } = useData()
  // Until the first read lands, a detail page would say its record does not
  // exist; say what is true instead.
  if (!ready) return <div className="py-10 text-[13px] text-[var(--muted)]">Loading ledger…</div>
  for (const [pattern, Page] of ROUTES) {
    const params = match(pattern, path)
    if (params) return <Page params={params} key={path} />
  }
  return <NotFound />
}

const STREAM_TAGS_REFRESH_MS = 30000

function DataProvider({ who, onSignOut, children }) {
  const [status, setStatus] = useState(null)
  const [health, setHealth] = useState([])
  const [kpis, setKpis] = useState([])
  const [frames, setFrames] = useState([])
  const [tags, setTags] = useState([])
  const [assets, setAssets] = useState([])
  const [attributes, setAttributes] = useState({})
  const [values, setValues] = useState({})
  const [events, setEvents] = useState([])
  const [streamCount, setStreamCount] = useState(0)
  const [link, setLink] = useState('connecting')
  const [now, setNow] = useState(Date.now())
  const [configReady, setConfigReady] = useState(false)
  const [pollReady, setPollReady] = useState(false)
  const lastStreamed = useRef({})

  const guard = useCallback((e) => {
    // A revoked or expired token signs the viewer out; the API being briefly
    // away is not worth a red screen.
    if (e instanceof AuthError) onSignOut()
  }, [onSignOut])

  // Live values: every acquired value the collector emits.
  const onEvent = useCallback((e) => {
    setEvents((prev) => [...prev.slice(-400), e])
    setStreamCount((n) => n + 1)
    if (e.kind === 'value_received' && e.tag) {
      lastStreamed.current[e.tag] = Date.now()
      setValues((v) => ({ ...v, [e.tag]: {
        value: e.value, quality: e.quality, quality_class: klassOf(e.quality),
        source_ts: e.source_ts, server_ts: e.server_ts ?? null, via: 'stream' } }))
    }
  }, [])
  useEffect(() => connectEvents(onEvent, setLink), [onEvent])

  // The dashboard poll.
  useEffect(() => {
    const poll = async () => {
      try {
        const [s, h, k] = await Promise.all([getStatus(), getTagHealth(), getKpis()])
        setStatus(s); setHealth(h); setKpis(k); setNow(Date.now()); setPollReady(true)
      } catch (e) { guard(e) }
    }
    poll()
    const t = setInterval(poll, 2500)          // §528: 2-3 s dashboard refresh
    return () => clearInterval(t)
  }, [guard])

  // Slower-moving things: configuration, event frames.
  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const [t, a, f] = await Promise.all([getTags(), getAssets(), getAllEvents(100)])
        if (!live) return
        setTags(t); setAssets(a); setFrames(f); setConfigReady(true)
        const attrs = {}
        await Promise.all(a.map(async (x) => {
          try { attrs[x.asset_code] = await getAttributes(x.asset_code) } catch { attrs[x.asset_code] = [] }
        }))
        if (live) setAttributes(attrs)
      } catch (e) { guard(e) }
    }
    load()
    const t = setInterval(load, 30000)
    return () => { live = false; clearInterval(t) }
  }, [guard])

  // The latest archived sample of each tag the stream has not updated
  // recently: tags that have not changed (the archive keeps changes), the
  // collector's own health, and anything acquired before this page opened.
  useEffect(() => {
    if (tags.length === 0) return
    let live = true
    const refresh = async () => {
      const stale = tags.filter((t) => Date.now() - (lastStreamed.current[t.name] ?? 0) > STREAM_TAGS_REFRESH_MS)
      for (let i = 0; i < stale.length && live; i += 6) {
        const batch = stale.slice(i, i + 6)
        const got = await Promise.all(batch.map((t) => getLatest(t.name).catch(() => undefined)))
        if (!live) return
        setValues((v) => {
          const next = { ...v }
          batch.forEach((t, j) => {
            if (got[j] === undefined) return
            if (lastStreamed.current[t.name] && Date.now() - lastStreamed.current[t.name] < STREAM_TAGS_REFRESH_MS) return
            next[t.name] = got[j] ? { ...got[j], via: 'archive' } : null
          })
          return next
        })
      }
    }
    refresh()
    const t = setInterval(refresh, STREAM_TAGS_REFRESH_MS)
    return () => { live = false; clearInterval(t) }
  }, [tags])

  const value = useMemo(() => {
    const tagByName = Object.fromEntries(tags.map((t) => [t.name, t]))
    const healthByTag = Object.fromEntries(health.map((h) => [h.tag, h]))
    const stations = [...new Set(assets.map((a) => a.station).filter(Boolean))].sort()
    return { who, signOut: onSignOut, status, health, healthByTag, kpis, frames, tags, tagByName,
             assets, attributes, values, events, streamCount, link, stations, now,
             ready: configReady && pollReady }
  }, [who, onSignOut, status, health, kpis, frames, tags, assets, attributes, values, events,
      streamCount, link, now, configReady, pollReady])

  return <DataContext.Provider value={value}>{children}</DataContext.Provider>
}
