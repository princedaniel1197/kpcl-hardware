// CRPMS — the visualisation, as a module of the Sentinel product: the same
// ivory ledger, shell and components (see src/styles/sentinel.css and
// src/components/), with CRPMS's own rules on top: every value carries its
// quality, its source timestamp and its provenance, and a value that is not
// Good is never shown as a number it does not have.
//
// This file holds the routes and the DataProvider: the
// dashboard's one poll (§528, 2-3 s) and the collector's live event stream.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { connectEvents, getAllEvents, getAssets, getAttributes, getKpis,
         getLatest, getStatus, getTagHealth, getTags } from './api'
import { DataContext, useData } from './lib/data'
import { RouterProvider, match, useRouter } from './lib/router'
import { klassOf } from './lib/quality'
import Shell from './components/Shell'
import { Folio, Note, PageHeader } from './components/ui'
import Overview from './pages/Overview'
import { AssetTree, AssetDetail } from './pages/Assets'
import { TagRegister, TagDetail } from './pages/Tags'
import { UnitOverview, Trends, ReplayPage, BenchRig } from './pages/Live'
import { EventFrames, EventDetail, KpiRegister, KpiDetail } from './pages/EventsKpis'
import { CollectorHealth, Gaps, SourceHealth } from './pages/Health'
import { DataSources, Settings, NotFound } from './pages/Other'

// There is no sign-in: token access (§509) was removed from the API and this
// interface by decision on 24 Sep 2026. The dashboard opens straight away.
export default function App() {
  return (
    <RouterProvider>
      <DataProvider>
        <Shell><Routes /></Shell>
      </DataProvider>
    </RouterProvider>
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
  const { ready, unreachable } = useData()
  if (!ready && unreachable) return <Unreachable />
  // Until the first read lands, a detail page would say its record does not
  // exist; say what is true instead.
  if (!ready) return <div className="py-10 text-[13px] text-[var(--muted)]">Loading ledger…</div>
  for (const [pattern, Page] of ROUTES) {
    const params = match(pattern, path)
    if (params) return <Page params={params} key={path} />
  }
  return <NotFound />
}

// The API did not answer: say so, rather than wait on an empty ledger. This is
// what a copy of the interface hosted away from the station laptop shows.
function Unreachable() {
  return (
    <>
      <PageHeader title="The CRPMS API is not reachable"
        subtitle="This screen reads the API, which runs on the station laptop with the collector, the engine and the archive." />
      <Note tone="danger">
        No answer from <code>/api</code> at this address. On the station laptop, <code>make start</code> runs the
        whole system and serves this interface with its data. A copy hosted anywhere else has no data behind it.
        This page retries every few seconds.
      </Note>
      <Folio />
    </>
  )
}

const STREAM_TAGS_REFRESH_MS = 30000

function DataProvider({ children }) {
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
  const [unreachable, setUnreachable] = useState(false)
  const lastStreamed = useRef({})

  // The API being briefly away once the ledger has loaded is not worth a red
  // screen; the stream chip and the values' own times show it. Before the
  // first read lands, it is all there is to say.
  const guard = useCallback(() => setUnreachable(true), [])

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
        setUnreachable(false)
      } catch { guard() }
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
      } catch { guard() }
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
    return { unreachable, status, health, healthByTag, kpis, frames, tags, tagByName,
             assets, attributes, values, events, streamCount, link, stations, now,
             ready: configReady && pollReady }
  }, [unreachable, status, health, kpis, frames, tags, assets, attributes, values, events,
      streamCount, link, now, configReady, pollReady])

  return <DataContext.Provider value={value}>{children}</DataContext.Provider>
}
