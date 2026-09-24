// Asset tree and element folio — Sentinel's Asset Register (a hierarchy you
// navigate down, every level with its own health summary) and asset folio.

import { Link, useRouter } from '../lib/router'
import { useData, inStation } from '../lib/data'
import { useScope } from '../lib/scope'
import { provenanceOf } from '../lib/quality'
import { num, time, duration } from '../lib/format'
import { sampleClass } from './Tags'
import { Chip, Empty, Folio, Missing, Kpi, Ledger, Note, OpenLink, PageHeader, PrintBar, ProvenanceChip,
         QualityChip, Section, Time, Value } from '../components/ui'

/** Every tag attached to an element or anything below it. */
export function subtreeTags(assets, attributes, code) {
  const byParent = {}
  for (const a of assets) (byParent[a.parent_id] ??= []).push(a)
  const root = assets.find((a) => a.asset_code === code)
  if (!root) return []
  const out = []
  const walk = (a) => {
    for (const x of attributes[a.asset_code] ?? []) if (x.tag) out.push({ ...x, element: a.asset_code })
    for (const c of byParent[a.id] ?? []) walk(c)
  }
  walk(root)
  return out
}

function health(d, tags) {
  const c = { Good: 0, Uncertain: 0, Bad: 0, 'No data': 0 }
  for (const t of tags) { const k = sampleClass(d.values[t.tag]); c[k in c ? k : 'Bad'] += 1 }
  return c
}

export function AssetTree() {
  const d = useData()
  const { query } = useRouter()
  const { station, stationLabel } = useScope()
  const parentCode = query.get('parent')
  const all = d.assets.filter((a) => a.level === 'Enterprise' || inStation(station, a))
  const parent = parentCode ? all.find((a) => a.asset_code === parentCode) : undefined
  const listed = parent ? all.filter((a) => a.parent_id === parent.id)
                        : all.filter((a) => !a.parent_id || !all.some((p) => p.id === a.parent_id))

  const crumbs = []
  let cur = parent
  while (cur) { crumbs.unshift(cur); cur = all.find((a) => a.id === cur.parent_id) }

  const allTags = Object.values(d.attributes).flat().filter((x) => x.tag)
  const withBad = all.filter((a) => (d.attributes[a.asset_code] ?? []).some((x) => x.tag && sampleClass(d.values[x.tag]) === 'Bad'))
  const scopeQs = (extra) => {
    const p = new URLSearchParams()
    for (const k of ['station', 'period']) if (query.get(k)) p.set(k, query.get(k))
    for (const [k, v] of Object.entries(extra)) p.set(k, v)
    const s = p.toString()
    return s ? `?${s}` : ''
  }

  return (
    <>
      <PageHeader kicker={stationLabel} title="Asset tree"
        subtitle="The asset model as the engine holds it — enterprise, station, unit, system, equipment — each element created from a template, each attribute mapped to a tag. Adding a unit is a new element from an existing template, not code. Navigate down; every level carries its own health."
        provenance="SYNTHETIC" right={<PrintBar />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Elements in scope" value={num(all.length, 0)} sub={`${num(all.filter((a) => a.level === 'Unit').length, 0)} units`} />
        <Kpi label="Attributes mapped to tags" value={num(allTags.length, 0)} sub="Across every element in the model" />
        <Kpi label="Elements with a Bad tag" value={num(withBad.length, 0)} tone={withBad.length ? 'danger' : 'success'}
             href="/tags?quality=Bad" sub="A tag the source has disowned, or cannot reach" />
        <Kpi label="Templates in use" value={num(new Set(all.map((a) => a.template).filter(Boolean)).size, 0)}
             sub="Unit 2 was made from Unit 1's template" />
      </div>

      <Section title="Hierarchy">
        <div className="text-[12.5px] mb-2 no-print">
          <Link className="link" to="/assets">Top</Link>
          {crumbs.map((c) => (
            <span key={c.id}> / <Link className="link" to={`/assets${scopeQs({ parent: c.asset_code })}`}>{c.name}</Link></span>
          ))}
        </div>
        {listed.length === 0 ? <Empty>Nothing below this level.</Empty> : (
          <Ledger>
            <thead>
              <tr><th>Element</th><th>Code</th><th>Level</th><th>Template</th>
                <th className="n">Tags</th><th className="n">Good</th><th className="n">Uncertain</th><th className="n">Bad</th><th className="n">No data</th><th></th></tr>
            </thead>
            <tbody>
              {listed.map((a) => {
                const kids = all.filter((x) => x.parent_id === a.id).length
                const tags = subtreeTags(all, d.attributes, a.asset_code)
                const h = health(d, tags)
                return (
                  <tr key={a.id}>
                    <td className="font-semibold">
                      {kids > 0
                        ? <Link className="link" to={`/assets${scopeQs({ parent: a.asset_code })}`}>{a.name}</Link>
                        : <Link className="link" to={`/assets/${a.asset_code}`}>{a.name}</Link>}
                      {kids > 0 && <span className="text-[11px] text-[var(--muted)]"> · {kids} below</span>}
                    </td>
                    <td className="text-[11.5px] text-[var(--muted)]">{a.asset_code}</td>
                    <td className="text-[12px]">{a.level}</td>
                    <td className="text-[12px]">{a.template ?? '—'}</td>
                    <td className="n">{num(tags.length, 0)}</td>
                    <td className="n">{num(h.Good, 0)}</td>
                    <td className="n">{h.Uncertain ? <Chip tone="warning">{h.Uncertain}</Chip> : 0}</td>
                    <td className="n">{h.Bad ? <Chip tone="danger">{h.Bad}</Chip> : 0}</td>
                    <td className="n">{h['No data'] ? <Chip tone="muted">{h['No data']}</Chip> : 0}</td>
                    <td className="n no-print"><OpenLink to={`/assets/${a.asset_code}`} /></td>
                  </tr>
                )
              })}
            </tbody>
          </Ledger>
        )}
      </Section>

      <Folio sources="asset model (element, template, attribute), archive, collector live stream" cadence="model every 30 s; values as acquired" />
    </>
  )
}

export function AssetDetail({ params }) {
  const d = useData()
  const a = d.assets.find((x) => x.asset_code === params.code)
  if (!a) return <Missing title={params.code}>No element {params.code}, or it is outside your station.</Missing>
  const parent = d.assets.find((x) => x.id === a.parent_id)
  const children = d.assets.filter((x) => x.parent_id === a.id)
  const own = (d.attributes[a.asset_code] ?? [])
  const below = subtreeTags(d.assets, d.attributes, a.asset_code)
  const h = health(d, below)
  const kpis = d.kpis.filter((k) => k.asset_code === a.asset_code)
  const frames = d.frames.filter((f) => f.asset_code === a.asset_code).slice(0, 10)
  const provs = [...new Set(below.map((x) => provenanceOf(x.tag).p))]

  return (
    <>
      <PageHeader kicker={[a.station ?? 'No station', a.level, a.template ?? 'no template'].join(' · ')}
        title={a.name} subtitle={`Element ${a.asset_code}${parent ? ` · below ${parent.name}` : ''}`}
        provenance={provs.length ? provs : 'SYNTHETIC'} right={<PrintBar label="Print element folio" />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Tags at and below" value={num(below.length, 0)} sub={`${num(own.filter((x) => x.tag).length, 0)} on this element itself`} />
        <Kpi label="Good" value={num(h.Good, 0)} tone={below.length && h.Good === below.length ? 'success' : undefined} sub="Latest sample Good" />
        <Kpi label="Uncertain" value={num(h.Uncertain, 0)} tone={h.Uncertain ? 'warning' : undefined} sub="Kept with its value, flagged" />
        <Kpi label="Bad or no data" value={num(h.Bad + h['No data'], 0)} tone={h.Bad + h['No data'] ? 'danger' : undefined}
             sub={`${num(h.Bad, 0)} Bad · ${num(h['No data'], 0)} no sample in 24 h`} />
      </div>

      {a.asset_code.endsWith('-U2') && (
        <div className="mt-4"><Note tone="warning"><strong>Configured, not acquired.</strong> Unit 2 was created from Unit 1's
          template to show that adding a unit is configuration. The simulator serves no Unit 2, so its tags have no data and
          its KPIs are Bad, naming inputs with no value. That is the truth about it.</Note></div>
      )}

      <Section title="Attributes" note="Each attribute of this element and the tag it reads, with the latest value, its quality, the time it was measured and where it comes from.">
        {own.length === 0 ? <Empty>This element has no attributes of its own{children.length ? '; see the elements below it' : ''}.</Empty> : (
          <Ledger>
            <thead><tr><th>Attribute</th><th>Tag</th><th className="n">Value</th><th>Quality</th><th>Source time</th><th>Provenance</th><th></th></tr></thead>
            <tbody>
              {own.map((x) => {
                const s = x.tag ? d.values[x.tag] : null
                const prov = x.tag ? provenanceOf(d.tagByName[x.tag] ?? x.tag) : null
                return (
                  <tr key={x.attribute}>
                    <td className="font-semibold">{x.attribute}</td>
                    <td className="text-[12px]">{x.tag ? <Link className="link" to={`/tags/${x.tag}`}>{x.tag}</Link> : <span className="text-[var(--muted)]">static</span>}</td>
                    <td className="n">{x.tag ? <Value sample={s} unit={x.unit} detail={d.healthByTag[x.tag]?.detail} />
                                             : <span className="tnum">{x.static_value ?? '—'}</span>}</td>
                    <td>{x.tag ? <QualityChip code={s?.quality} klass={s ? undefined : null} /> : null}</td>
                    <td>{x.tag ? <Time iso={s?.source_ts} /> : null}</td>
                    <td>{prov && <ProvenanceChip p={prov.p} source={prov.source} />}</td>
                    <td className="n no-print">{x.tag && <OpenLink to={`/tags/${x.tag}`} />}</td>
                  </tr>
                )
              })}
            </tbody>
          </Ledger>
        )}
      </Section>

      {children.length > 0 && (
        <Section title="Below this element">
          <Ledger>
            <thead><tr><th>Element</th><th>Level</th><th>Template</th><th className="n">Tags</th><th></th></tr></thead>
            <tbody>
              {children.map((c) => (
                <tr key={c.id}>
                  <td className="font-semibold"><Link className="link" to={`/assets/${c.asset_code}`}>{c.name}</Link></td>
                  <td className="text-[12px]">{c.level}</td>
                  <td className="text-[12px]">{c.template ?? '—'}</td>
                  <td className="n">{num(subtreeTags(d.assets, d.attributes, c.asset_code).length, 0)}</td>
                  <td className="n no-print"><OpenLink to={`/assets/${c.asset_code}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        </Section>
      )}

      {kpis.length > 0 && (
        <Section title="KPIs computed on this element">
          <Ledger>
            <thead><tr><th>KPI</th><th className="n">Value</th><th>Quality</th><th>Computed</th><th></th></tr></thead>
            <tbody>
              {kpis.map((k) => (
                <tr key={k.kpi}>
                  <td className="font-semibold">{k.kpi} <span className="text-[11px] text-[var(--muted)]">v{k.version}</span></td>
                  <td className="n"><Value sample={{ value: k.value, quality: k.quality, quality_class: k.quality_class }} unit={k.unit} detail={k.reason} /></td>
                  <td><QualityChip code={k.quality} /></td>
                  <td><Time iso={k.ts} /></td>
                  <td className="n no-print"><OpenLink to={`/kpis/${k.kpi}/${k.asset_code}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        </Section>
      )}

      {frames.length > 0 && (
        <Section title="Event frames">
          <Ledger>
            <thead><tr><th>Frame</th><th>Template</th><th>Started</th><th className="n">Duration</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {frames.map((f) => (
                <tr key={f.id}>
                  <td className="font-semibold">#{f.id}</td>
                  <td>{f.template}</td>
                  <td><Time iso={f.start_ts} date /></td>
                  <td className="n">{duration(f.duration_s)}</td>
                  <td><Chip tone={f.status === 'closed' ? 'success' : f.status === 'aborted' ? 'danger' : 'info'}>{f.status}</Chip></td>
                  <td className="n no-print"><OpenLink to={`/events/${f.id}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        </Section>
      )}

      <Folio sources="asset model, archive, KPI engine, event frames" cadence={`values as acquired · as at ${time(new Date(d.now).toISOString())}`} />
    </>
  )
}
