// Event frames and the KPI register, with lineage.

import { Link } from '../lib/router'
import { useData, inStation } from '../lib/data'
import { useScope } from '../lib/scope'
import { kpiProvenance, provenanceOf, statusOf } from '../lib/quality'
import { duration, num } from '../lib/format'
import { subtreeTags } from './Assets'
import KPI_CONFIG from '../../../config/kpi_definitions.json'
import { Chip, Empty, Folio, Missing, Kpi, Ledger, Note, OpenLink, PageHeader, PrintBar, ProvenanceChip,
         QualityChip, Section, Time, Value } from '../components/ui'

const statusTone = (s) => (s === 'closed' ? 'success' : s === 'aborted' ? 'danger' : 'info')

export function EventFrames() {
  const d = useData()
  const { station, stationLabel, minutes, periodLabel } = useScope()
  const assetStation = Object.fromEntries(d.assets.map((a) => [a.asset_code, a.station]))
  const scoped = d.frames.filter((f) => inStation(station, { station: assetStation[f.asset_code] }))
  const since = d.now - minutes * 60000
  const inPeriod = scoped.filter((f) => new Date(f.start_ts).getTime() >= since || !f.end_ts)
  const complete = scoped.filter((f) => f.status === 'closed')
  const reference = scoped.find((f) => f.is_reference)

  return (
    <>
      <PageHeader kicker={`${stationLabel} · ${periodLabel}`} title="Event frames"
        subtitle="Start-ups captured automatically from the archive by template: a frame opens and closes on attribute triggers, and each milestone is stamped at the instant its condition became true (§472). One frame per row."
        provenance="SYNTHETIC" right={<PrintBar />} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Frames in the period" value={num(inPeriod.length, 0)} sub={`${num(scoped.length, 0)} in the latest ${num(d.frames.length, 0)} recorded`} />
        <Kpi label="Closed" value={num(complete.length, 0)} tone="success" sub="The end trigger was reached inside the template's limit" />
        <Kpi label="Aborted" value={num(scoped.filter((f) => f.status === 'aborted').length, 0)}
             tone={scoped.some((f) => f.status === 'aborted') ? 'warning' : undefined} sub="Overran the template's maximum duration" />
        <Kpi label="Reference start-up" value={reference ? duration(reference.duration_s) : '—'}
             sub={reference ? `Frame #${reference.id}, the baseline for milestone comparison` : 'None marked'} />
      </div>

      <Section title="Frames" note={inPeriod.length < scoped.length ? `${num(scoped.length - inPeriod.length, 0)} earlier frames are outside ${periodLabel.toLowerCase()}; widen the period in the header to list them.` : undefined}>
        {inPeriod.length === 0 ? <Empty>No frame started in {periodLabel.toLowerCase()}.</Empty> : (
          <Ledger>
            <thead><tr><th>Frame</th><th>Template</th><th>Element</th><th>Started</th><th className="n">Duration</th><th className="n">Milestones</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {inPeriod.map((f) => (
                <tr key={f.id}>
                  <td className="font-semibold"><Link className="link" to={`/events/${f.id}`}>#{f.id}</Link>{f.is_reference && <> <Chip tone="gold">reference</Chip></>}</td>
                  <td>{f.template}</td>
                  <td className="text-[12px]"><Link className="link" to={`/assets/${f.asset_code}`}>{f.asset_code}</Link></td>
                  <td><Time iso={f.start_ts} date /></td>
                  <td className="n">{f.end_ts ? duration(f.duration_s) : <Chip tone="info">open</Chip>}</td>
                  <td className="n">{num(f.milestones.length, 0)}</td>
                  <td><Chip tone={statusTone(f.status)}>{f.status}</Chip></td>
                  <td className="n no-print"><OpenLink to={`/events/${f.id}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>
      <Folio sources="event-frame detection over the archive (engine, every 30 s)" cadence="frames listed every 30 s" />
    </>
  )
}

export function EventDetail({ params }) {
  const d = useData()
  const f = d.frames.find((x) => String(x.id) === params.id)
  if (!f) return <Missing title={`Event frame #${params.id}`}>Frame #{params.id} is not among the latest {d.frames.length} recorded.</Missing>
  const ref = d.frames.find((x) => x.is_reference && x.template === f.template && x.id !== f.id)
  const refAt = Object.fromEntries((ref?.milestones ?? []).map((m) => [m.name, m.offset_s]))
  return (
    <>
      <PageHeader kicker={`${f.asset_code} · ${f.template}`} title={`Event frame #${f.id}`}
        subtitle={`Opened ${new Date(f.start_ts).toUTCString()}${f.end_ts ? `, closed after ${duration(f.duration_s)}` : ', still open'}.`}
        provenance="SYNTHETIC" right={<PrintBar label="Print frame folio" />} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Status" value={f.status} tone={f.status === 'aborted' ? 'danger' : f.status === 'closed' ? 'success' : undefined} />
        <Kpi label="Duration" value={f.end_ts ? duration(f.duration_s) : 'open'} />
        <Kpi label="Milestones" value={num(f.milestones.length, 0)} />
        <Kpi label="Against reference" value={ref && f.duration_s ? `${f.duration_s >= ref.duration_s ? '+' : '−'}${duration(Math.abs(f.duration_s - ref.duration_s))}` : '—'}
             tone={ref && f.duration_s > ref.duration_s ? 'warning' : undefined}
             sub={ref ? `Reference frame #${ref.id}, ${duration(ref.duration_s)}` : 'No reference frame for this template'} />
      </div>
      <Section title="Milestones" note="Each stamped at the instant its condition became true, not when the debounce expired. The value is what the trigger attribute read at that instant, with its quality.">
        <Ledger>
          <thead><tr><th>Milestone</th><th>Reached</th><th className="n">After start</th><th className="n">Reference</th><th className="n">Value</th><th>Quality</th></tr></thead>
          <tbody>
            {f.milestones.map((m) => {
              const r = refAt[m.name]
              return (
                <tr key={m.name}>
                  <td className="font-semibold">{m.name.replaceAll('_', ' ')}</td>
                  <td><Time iso={m.ts} ms /></td>
                  <td className="n tnum">+{duration(m.offset_s)}</td>
                  <td className="n tnum text-[var(--muted)]">{r === undefined ? '—' : `+${duration(r)}`}</td>
                  <td className="n"><Value sample={{ value: m.value, quality: m.quality }} /></td>
                  <td><QualityChip code={m.quality} /></td>
                </tr>
              )
            })}
          </tbody>
        </Ledger>
      </Section>
      <Folio sources="event frames and milestones" cadence="every 30 s" />
    </>
  )
}

const configOf = (name) => KPI_CONFIG.definitions.find((x) => x.name === name)

/** The tags a KPI's inputs resolve to on an element: its own attributes and
 *  those below it, as the engine resolves them. */
function kpiInputs(d, k) {
  const cfg = configOf(k.kpi)
  const tags = subtreeTags(d.assets, d.attributes, k.asset_code)
  return Object.entries(cfg?.inputs ?? {}).map(([variable, attribute]) => ({
    variable, attribute, tag: tags.find((t) => t.attribute === attribute)?.tag ?? null,
  }))
}

export function KpiRegister() {
  const d = useData()
  const { station, stationLabel } = useScope()
  const assetStation = Object.fromEntries(d.assets.map((a) => [a.asset_code, a.station]))
  const kpis = d.kpis.filter((k) => inStation(station, { station: assetStation[k.asset_code] }))
  const bad = kpis.filter((k) => k.quality_class === 'Bad')

  return (
    <>
      <PageHeader kicker={stationLabel} title="KPI register"
        subtitle="Every KPI on every element it applies to, with the version of the definition that produced it (§320, §379). A KPI with a Bad input is Bad, with a reason naming the input — never zero, never the last good value, never interpolated."
        provenance={['REAL', 'SYNTHETIC']} right={<PrintBar />} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="KPI values in scope" value={num(kpis.length, 0)} sub={`${num(new Set(kpis.map((k) => k.kpi)).size, 0)} definitions`} />
        <Kpi label="Good" value={num(kpis.filter((k) => k.quality_class === 'Good').length, 0)} tone="success" />
        <Kpi label="Bad" value={num(bad.length, 0)} tone={bad.length ? 'danger' : undefined} sub="Each names the input that made it Bad" />
        <Kpi label="Uncertain" value={num(kpis.filter((k) => k.quality_class === 'Uncertain').length, 0)}
             tone={kpis.some((k) => k.quality_class === 'Uncertain') ? 'warning' : undefined} />
      </div>
      <Section title="Register">
        {kpis.length === 0 ? <Empty /> : (
          <Ledger>
            <thead><tr><th>KPI</th><th>Element</th><th className="n">Value</th><th>Quality</th><th>Inputs</th><th>Computed</th><th>Provenance</th><th></th></tr></thead>
            <tbody>
              {kpis.map((k) => {
                const inputs = kpiInputs(d, k)
                const prov = kpiProvenance(inputs.map((i) => i.tag).filter(Boolean))
                return (
                  <tr key={`${k.kpi}-${k.asset_code}`}>
                    <td className="font-semibold whitespace-nowrap"><Link className="link" to={`/kpis/${k.kpi}/${k.asset_code}`}>{k.kpi}</Link> <span className="text-[11px] text-[var(--muted)]">v{k.version}</span></td>
                    <td className="text-[12px]"><Link className="link" to={`/assets/${k.asset_code}`}>{k.asset_code}</Link></td>
                    <td className="n"><Value sample={{ value: k.value, quality: k.quality, quality_class: k.quality_class }} unit={k.unit} detail={k.reason} /></td>
                    <td><QualityChip code={k.quality} /></td>
                    <td className="text-[12px] text-[var(--muted)]">{inputs.map((i) => i.tag ?? i.attribute).join(', ')}</td>
                    <td><Time iso={k.ts} /></td>
                    <td><ProvenanceChip p={prov.p} source={prov.source} /></td>
                    <td className="n no-print"><OpenLink to={`/kpis/${k.kpi}/${k.asset_code}`} /></td>
                  </tr>
                )
              })}
            </tbody>
          </Ledger>
        )}
      </Section>
      <Folio sources="KPI engine (kpi_value, kpi_definition), asset model" cadence="each KPI at its own calculation frequency; read every 2.5 s" />
    </>
  )
}

export function KpiDetail({ params }) {
  const d = useData()
  const k = d.kpis.find((x) => x.kpi === params.kpi && x.asset_code === params.asset)
  if (!k) return <Missing title={params.kpi}>No value of {params.kpi} on {params.asset}.</Missing>
  const cfg = configOf(k.kpi)
  const inputs = kpiInputs(d, k)
  const prov = kpiProvenance(inputs.map((i) => i.tag).filter(Boolean))
  const sample = { value: k.value, quality: k.quality, quality_class: k.quality_class }

  return (
    <>
      <PageHeader kicker={`${k.asset_code} · ${k.classification ?? 'KPI'} · version ${k.version}`} title={k.kpi}
        subtitle={cfg?.description} provenance={prov.p} right={<PrintBar label="Print KPI folio" />} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="panel px-3.5 py-3 h-full">
          <div className="text-[10.5px] uppercase tracking-[0.1em] text-[var(--muted)]">Latest value</div>
          <div className="mt-1.5"><Value sample={sample} unit={k.unit} detail={k.reason} big /></div>
        </div>
        <Kpi label="Quality" value={k.quality_class} tone={k.quality_class === 'Bad' ? 'danger' : k.quality_class === 'Uncertain' ? 'warning' : 'success'}
             sub={k.quality_class === 'Good' ? 'Every input Good and inside the validity window' : `${statusOf(k.quality)?.name} · ${statusOf(k.quality)?.hex}`} />
        <Kpi label="Reference" value={k.reference ?? '—'} unit={k.unit} sub={`Validity ${k.validity_low ?? '—'} to ${k.validity_high ?? '—'} ${k.unit ?? ''}`} />
        <Kpi label="Computed" value={new Date(k.ts).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false })} unit="IST"
             sub={`Definition version ${k.version}`} />
      </div>

      <Section title="Lineage" note="The value, the equation that produced it, the attributes it reads and the tags behind them, each with its own latest value, quality and provenance.">
        <Ledger>
          <tbody>
            <tr><td className="text-[var(--muted)] w-[220px]">Equation</td><td className="font-semibold">{cfg ? cfg.equation : '—'}</td></tr>
            <tr><td className="text-[var(--muted)]">Constants</td><td className="tnum">{cfg && Object.keys(cfg.constants ?? {}).length ? Object.entries(cfg.constants).map(([n, v]) => `${n} = ${v}`).join(', ') : 'none'}</td></tr>
            <tr><td className="text-[var(--muted)]">Computed with</td><td>version {k.version} · validity {k.validity_low ?? '—'} to {k.validity_high ?? '—'} {k.unit}</td></tr>
            <tr><td className="text-[var(--muted)]">Equation source</td><td>
              {cfg && cfg.version === k.version
                ? <>config/kpi_definitions.json, version {cfg.version} — the version that computed this value</>
                : <><Chip tone="warning">config v{cfg?.version ?? '—'}</Chip> <span className="reason">The configuration file holds version {cfg?.version ?? '—'}; version {k.version} was created in the database through the audited path, and the API does not serve definitions. The equation and inputs shown are version {cfg?.version ?? '—'}'s. (On 24 Sep 2026 every version of every KPI had the same equation and inputs; the versions differ in their validity window.)</span></>}
            </td></tr>
          </tbody>
        </Ledger>
      </Section>

      <Section title="Inputs">
        <Ledger>
          <thead><tr><th>Variable</th><th>Attribute</th><th>Tag</th><th className="n">Value</th><th>Quality</th><th>Source time</th><th>Provenance</th></tr></thead>
          <tbody>
            {inputs.map((i) => {
              const s = i.tag ? d.values[i.tag] : null
              const p = i.tag ? provenanceOf(d.tagByName[i.tag] ?? i.tag) : null
              return (
                <tr key={i.variable}>
                  <td className="font-semibold">{i.variable}</td>
                  <td>{i.attribute}</td>
                  <td className="text-[12px]">{i.tag ? <Link className="link" to={`/tags/${i.tag}`}>{i.tag}</Link> : <span className="reason reason-danger">not mapped on this element</span>}</td>
                  <td className="n">{i.tag ? <Value sample={s} unit={d.tagByName[i.tag]?.unit} detail={d.healthByTag[i.tag]?.detail} /> : '—'}</td>
                  <td>{i.tag ? <QualityChip code={s?.quality} klass={s ? undefined : null} /> : null}</td>
                  <td>{i.tag ? <Time iso={s?.source_ts} /> : null}</td>
                  <td>{p && <ProvenanceChip p={p.p} source={p.source} />}</td>
                </tr>
              )
            })}
          </tbody>
        </Ledger>
        {k.quality_class === 'Bad' && (
          <div className="mt-3"><Note tone="danger"><strong>Bad, and why.</strong> {k.reason}. The engine does not substitute
            zero, a default or the last good value for an input it does not have (§318).</Note></div>
        )}
      </Section>
      <Folio sources="KPI engine, config/kpi_definitions.json, asset model, archive" cadence={`every ${cfg ? num(cfg.calculation_freq_ms / 1000, 0) : '—'} s`} />
    </>
  )
}
