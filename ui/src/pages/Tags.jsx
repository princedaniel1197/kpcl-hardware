// Tag register and tag folio — Sentinel's register/detail pair (its Asset
// Register and asset folio), for tags.

import { useEffect, useState } from 'react'
import { Link, useRouter } from '../lib/router'
import { useData, inStation } from '../lib/data'
import { useScope } from '../lib/scope'
import { getRecent } from '../api'
import { klassOf, provenanceOf, statusOf } from '../lib/quality'
import { num, time } from '../lib/format'
import Trend from '../Trend'
import { Chip, Empty, Folio, Missing, Kpi, Ledger, Note, OpenLink, PageHeader, PrintBar, ProvenanceChip,
         QualityChip, Section, Time, Value, TagName } from '../components/ui'

const SOURCES = [
  ['all', 'All sources'], ['DCS simulator', 'DCS simulator'], ['bench rig', 'Bench rig'],
  ['collector', 'Collector'], ['load test', 'Load test'], ['OMF demo', 'OMF demo'],
]
const QUALITIES = ['all', 'Good', 'Uncertain', 'Bad', 'No data']

function FilterChips({ param, options, current }) {
  const { path, query, navigate } = useRouter()
  const go = (v) => {
    const p = new URLSearchParams(query.toString())
    if (v === 'all') p.delete(param); else p.set(param, v)
    const s = p.toString()
    navigate(path + (s ? `?${s}` : ''))
  }
  return (
    <div className="flex flex-wrap gap-2 no-print">
      {options.map(([v, label]) => (
        <button key={v} type="button" onClick={() => go(v)} className="px-2 py-0.5 text-[12px]"
          style={{ border: '0.5px solid var(--hairline)', borderRadius: 2,
                   background: current === v ? 'var(--gold)' : 'var(--panel)' }}>{label}</button>
      ))}
    </div>
  )
}

export function sampleClass(sample) {
  if (!sample) return 'No data'
  return sample.quality_class ?? klassOf(sample.quality) ?? 'No data'
}

export function TagRegister() {
  const d = useData()
  const { query } = useRouter()
  const { station, stationLabel } = useScope()
  const source = query.get('source') ?? 'all'
  const quality = query.get('quality') ?? 'all'

  // Acquired tags first, as an operator reads them; retired ones last.
  const order = ['DCS simulator', 'bench rig', 'collector', 'load test', 'OMF demo']
  const rank = (t) => order.indexOf(provenanceOf(t).source) * 2 + (t.description?.startsWith('RETIRED') ? 1 : 0)
  const inScope = d.tags.filter((t) => inStation(station, t))
    .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
  const rows = inScope.filter((t) => source === 'all' || provenanceOf(t).source === source)
    .filter((t) => quality === 'all' || sampleClass(d.values[t.name]) === quality)
  const count = (k) => inScope.filter((t) => sampleClass(d.values[t.name]) === k).length

  return (
    <>
      <PageHeader kicker={stationLabel} title="Tag register"
        subtitle="Every configured tag, with its latest archived value, the quality it carries, the time the value was measured at the source, and where it comes from. A value that is not Good shows its reason; a Bad value has no number."
        provenance={['REAL', 'SYNTHETIC']} right={<PrintBar />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Tags in scope" value={num(inScope.length, 0)}
             sub={`${num(inScope.filter((t) => provenanceOf(t).p === 'REAL').length, 0)} real · ${num(inScope.filter((t) => provenanceOf(t).p === 'SYNTHETIC').length, 0)} synthetic`} />
        <Kpi label="Good" value={num(count('Good'), 0)} tone="success" sub="Latest sample carries a Good StatusCode" />
        <Kpi label="Uncertain" value={num(count('Uncertain'), 0)} tone={count('Uncertain') ? 'warning' : undefined}
             sub="Kept with its value, and flagged" />
        <Kpi label="Bad or no data" value={num(count('Bad') + count('No data'), 0)} tone={count('Bad') + count('No data') ? 'danger' : undefined}
             sub={`${num(count('Bad'), 0)} Bad · ${num(count('No data'), 0)} with no sample in 24 h`} />
      </div>

      <Section title="Register"
        right={<div className="flex flex-col items-end gap-2">
          <FilterChips param="source" current={source} options={SOURCES} />
          <FilterChips param="quality" current={quality} options={QUALITIES.map((q) => [q, q === 'all' ? 'All quality' : q])} />
        </div>}>
        {rows.length === 0 ? <Empty>No tags match.</Empty> : (
          <Ledger>
            <thead>
              <tr><th>Tag</th><th className="hidden sm:table-cell">Description</th><th className="n">Value</th><th>Quality</th>
                <th>Source time</th><th>Provenance</th><th></th></tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const s = d.values[t.name]
                const prov = provenanceOf(t)
                const h = d.healthByTag[t.name]
                return (
                  <tr key={t.name}>
                    <td className="font-semibold"><Link className="link" to={`/tags/${t.name}`}><TagName name={t.name} /></Link></td>
                    <td className="hidden sm:table-cell text-[12px] text-[var(--muted)] max-w-[40ch]">{t.description}</td>
                    <td className="n"><Value sample={s} unit={t.unit} detail={h?.detail} /></td>
                    <td><QualityChip code={s?.quality} klass={s ? undefined : null} /></td>
                    <td><Time iso={s?.source_ts} /></td>
                    <td><ProvenanceChip p={prov.p} source={prov.source} /></td>
                    <td className="n no-print"><OpenLink to={`/tags/${t.name}`} /></td>
                  </tr>
                )
              })}
            </tbody>
          </Ledger>
        )}
      </Section>

      <Folio sources="tag table, archive (latest sample within 24 h), collector live stream, engine tag health"
             cadence="values as acquired; unchanged tags re-read every 30 s" />
    </>
  )
}

export function TagDetail({ params }) {
  const d = useData()
  const { minutes, periodLabel } = useScope()
  const t = d.tagByName[params.name]
  const s = d.values[params.name]
  const h = d.healthByTag[params.name]
  const [recent, setRecent] = useState([])

  useEffect(() => {
    let live = true
    const load = () => getRecent(params.name, minutes, 25)
      .then((r) => { if (live) setRecent([...r.points].reverse()) }).catch(() => {})
    load()
    const i = setInterval(load, 5000)
    return () => { live = false; clearInterval(i) }
  }, [params.name, minutes])

  if (!t) return <Missing title={params.name}>No tag called {params.name} is configured, or it is outside your station.</Missing>
  const prov = provenanceOf(t)
  const st = s ? statusOf(s.quality) : null
  const transit = s?.server_ts && s?.source_ts ? new Date(s.server_ts) - new Date(s.source_ts) : null

  return (
    <>
      <PageHeader kicker={[t.station ?? 'No station', t.asset_code ?? 'no asset', prov.source].join(' · ')}
        title={t.name} subtitle={t.description} ident
        provenance={prov.p} right={<PrintBar label="Print tag folio" />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="panel px-3.5 py-3 h-full">
          <div className="text-[10.5px] uppercase tracking-[0.1em] text-[var(--muted)]">Latest value</div>
          <div className="mt-1.5"><Value sample={s} unit={t.unit} detail={h?.detail} big /></div>
        </div>
        <Kpi label="Quality" value={s ? sampleClass(s) : 'No data'}
             tone={!s ? 'danger' : sampleClass(s) === 'Bad' ? 'danger' : sampleClass(s) === 'Uncertain' ? 'warning' : 'success'}
             sub={st ? `${st.name} · ${st.hex}` : 'No sample in the last 24 hours'} />
        <Kpi label="Measured at source" value={s?.source_ts ? time(s.source_ts) : '—'}
             sub={s?.source_ts ? `SourceTimestamp · ${new Date(s.source_ts).toISOString()}` : 'No SourceTimestamp'} />
        <Kpi label="Source to server" value={transit === null ? '—' : num(transit, 0)} unit={transit === null ? undefined : 'ms'}
             sub={transit === null ? 'No ServerTimestamp on this sample' : 'ServerTimestamp minus SourceTimestamp'} />
      </div>

      {t.name === 'RIG_CURRENT' && (
        <div className="mt-4"><Note tone="warning"><strong>Uncalibrated — bench demo only.</strong> A meter put the fans'
          current at 0.1–0.25 A while this read between 0.12 and 0.88 A. The bench's ADC is uncalibrated and its ground is
          poor, and the hardware is being left as it is, so this is published Uncertain and no KPI or alert uses it.</Note></div>
      )}

      <Section title="Trend" note={`${periodLabel}. The line breaks at a Bad sample; it is never drawn through a value that does not exist.`}>
        <div className="panel p-3"><Trend tag={t.name} unit={t.unit} minutes={minutes} height={260} title={t.name} /></div>
      </Section>

      <Section title="Nameplate">
        <Ledger>
          <tbody>
            <tr><td className="text-[var(--muted)] w-[220px]">Tag</td><td className="font-semibold">{t.name}</td></tr>
            <tr><td className="text-[var(--muted)]">Description</td><td>{t.description ?? '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Engineering unit</td><td>{t.unit ?? '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Range</td><td className="tnum">{t.range_low ?? '—'} to {t.range_high ?? '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Source</td><td>{t.source_system} · <ProvenanceChip p={prov.p} source={prov.source} /></td></tr>
            <tr><td className="text-[var(--muted)]">Asset</td><td>{t.asset_code ? <Link className="link" to={`/assets/${t.asset_code}`}>{t.asset_code}</Link> : '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Station</td><td>{t.station ?? '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Health</td><td>{h ? <><Chip tone={h.state === 'ok' ? 'success' : h.state === 'uncertain' ? 'warning' : 'danger'}>{h.state.replace('_', ' ')}</Chip> <span className="reason">{h.detail ?? ''}</span></> : <span className="text-[var(--muted)]">Not evaluated — the engine's rules run on acquired (OPC UA) tags</span>}</td></tr>
            {h && <tr><td className="text-[var(--muted)]">Rule verdict (computed)</td><td><QualityChip code={h.computed_quality} /> <span className="reason">computed apart from the source quality</span></td></tr>}
          </tbody>
        </Ledger>
      </Section>

      <Section title="Recent samples" note="The newest 25 in the period, as the archive holds them: source time, server time, value and StatusCode.">
        {recent.length === 0 ? <Empty>No samples in {periodLabel.toLowerCase()}.</Empty> : (
          <Ledger>
            <thead><tr><th>Source time</th><th>Server time</th><th className="n">Value</th><th>Quality</th><th>StatusCode</th></tr></thead>
            <tbody>
              {recent.map((p) => (
                <tr key={p.source_ts}>
                  <td><Time iso={p.source_ts} ms /></td>
                  <td><Time iso={p.server_ts} ms /></td>
                  <td className="n"><Value sample={p} unit={t.unit} /></td>
                  <td><QualityChip code={p.quality} /></td>
                  <td className="text-[12px] text-[var(--muted)]">{statusOf(p.quality)?.name} <span className="tnum">({p.quality})</span></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>

      <Folio sources="archive, collector live stream, engine tag health" cadence="as acquired; recent samples every 5 s" />
    </>
  )
}
