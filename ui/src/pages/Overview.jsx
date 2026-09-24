// Overview — CRPMS's Command Centre. Sentinel's layout: a headline figure,
// eight stat blocks, and one alert ledger across every module.

import { Link, useRouter } from '../lib/router'
import { useData, inStation } from '../lib/data'
import { useScope } from '../lib/scope'
import { ago, num, time, duration } from '../lib/format'
import { klassOf, reasonFor, toneOf } from '../lib/quality'
import { isa } from '../theme'
import { CountBars } from '../Trend'
import { Chip, Empty, Folio, Kpi, Ledger, Note, OpenLink, PageHeader, Section, Value } from '../components/ui'

export function qualityCounts(health, tagByName, station) {
  const rows = health.filter((h) => inStation(station, tagByName[h.tag]))
  const c = { Good: 0, Uncertain: 0, Bad: 0, none: 0 }
  for (const h of rows) {
    const k = h.state === 'missing' ? null : klassOf(h.source_quality)
    if (k === 'Good' || k === 'Uncertain' || k === 'Bad') c[k] += 1; else c.none += 1
  }
  const reporting = rows.filter((h) => h.state !== 'missing' && h.state !== 'stale').length
  return { rows, counts: c, total: rows.length, reporting,
           completeness: rows.length ? (100 * reporting) / rows.length : null }
}

export default function Overview() {
  const d = useData()
  const { scope } = useRouter()
  const { station, stationLabel, periodLabel } = useScope()
  const q = qualityCounts(d.health, d.tagByName, station)
  const assetStation = Object.fromEntries(d.assets.map((a) => [a.asset_code, a.station]))
  const kpis = d.kpis.filter((k) => inStation(station, { station: assetStation[k.asset_code] }))
  const leader = d.status?.collectors?.find((c) => c.is_leader) ?? d.status?.collectors?.[0]
  const rigTags = d.health.filter((h) => h.tag.startsWith('RIG_'))
  const rigUnreachable = rigTags.length > 0 && rigTags.every((h) => h.source_quality === 2150694912)
  const newest = d.health.reduce((m, h) => (h.last_source_ts && h.last_source_ts > (m ?? '') ? h.last_source_ts : m), null)
  const newestAge = newest ? (d.now - new Date(newest).getTime()) / 1000 : null
  const flagged = q.rows.filter((h) => h.computed_class && h.computed_class !== 'Good').length
  const startup = d.frames.find((f) => f.template === 'ThermalStartup' && f.status === 'closed')
  const kpiBad = kpis.filter((k) => k.quality_class !== 'Good')

  const bars = [
    { label: 'Good', count: q.counts.Good, colour: isa.good },
    { label: 'Uncertain', count: q.counts.Uncertain, colour: isa.uncertain },
    { label: 'Bad', count: q.counts.Bad, colour: isa.bad },
    { label: 'No data', count: q.counts.none, colour: isa.faint },
  ]

  // One row per finding, across every module, Bad before Uncertain.
  const alerts = []
  if (leader && !leader.link_up) {
    alerts.push({ sev: 'danger', module: 'Collector', title: `Archive link down on ${leader.instance}`,
                  detail: `Acquisition continues into the local buffer (${num(leader.buffer_depth, 0)} samples held). They are replayed in source-time order when the link returns.`,
                  value: `${num(leader.buffer_depth, 0)} buffered`, href: '/health' })
  } else if (leader && leader.buffer_depth > 0) {
    alerts.push({ sev: 'warning', module: 'Collector', title: `Buffer draining on ${leader.instance}`,
                  detail: 'Samples held during an outage are being replayed to the archive.',
                  value: `${num(leader.buffer_depth, 0)} buffered`, href: '/health' })
  }
  if (d.status && (!leader || !leader.alive)) {
    alerts.push({ sev: 'danger', module: 'Collector', title: 'No live collector is reporting',
                  detail: 'Nothing is acquiring. Values on every screen are the last archived.', href: '/health' })
  }
  for (const h of q.rows) {
    if (h.state === 'ok') continue
    const tag = d.tagByName[h.tag]
    const k = h.state === 'missing' ? null : klassOf(h.source_quality)
    alerts.push({
      sev: k === 'Bad' || ['missing', 'comm_failed', 'bad', 'stale'].includes(h.state) ? 'danger' : 'warning',
      module: 'Tag health', title: `${h.tag} — ${h.state.replace('_', ' ')}`,
      detail: h.state === 'missing' ? (h.detail ?? 'no samples') : reasonFor(h.source_quality, h.detail),
      sample: h.state === 'missing' ? null : d.values[h.tag], unit: tag?.unit, href: `/tags/${h.tag}` })
  }
  for (const k of kpiBad) {
    alerts.push({ sev: k.quality_class === 'Bad' ? 'danger' : 'warning', module: 'KPIs',
                  title: `${k.kpi} v${k.version} on ${k.asset_code}`, detail: k.reason,
                  sample: { value: k.value, quality: k.quality, quality_class: k.quality_class }, unit: k.unit,
                  href: `/kpis/${k.kpi}/${k.asset_code}` })
  }
  alerts.sort((a, b) => (a.sev === b.sev ? 0 : a.sev === 'danger' ? -1 : 1))

  return (
    <>
      <PageHeader
        kicker={`${stationLabel} · ${periodLabel} · as at ${time(new Date(d.now).toISOString())}`}
        title="Overview"
        subtitle="Every value on these screens is read from the archive or from the collector's live stream, with its quality and its source timestamp. CRPMS is a read-only overlay: it acquires, archives and computes; it never writes to the control system."
        provenance={['REAL', 'SYNTHETIC']}
      />

      {/* hero */}
      <div className="panel px-5 py-5 sm:px-7 sm:py-6">
        <div className="flex flex-wrap items-end gap-x-10 gap-y-4 justify-between">
          <div>
            <div className="text-[11px] uppercase tracking-[0.13em] text-[var(--muted)]">
              Data completeness across acquired tags
            </div>
            <div className="flex items-baseline gap-2 mt-1">
              <span className="hero-num text-[54px] sm:text-[68px]">{q.completeness === null ? '—' : num(q.completeness, 1)}</span>
              <span className="display text-[22px] text-[var(--muted)]">per cent</span>
            </div>
            <div className="text-[12px] text-[var(--muted)] mt-1 max-w-[56ch]">
              {num(q.reporting, 0)} of {num(q.total, 0)} acquired tags delivered a sample in the last five minutes.
              Counted from the engine's tag-health verdicts on each read — none of it is stored. Unit 2's tags are
              configured but have no source, and count against it.
            </div>
          </div>
          <div className="min-w-[280px] flex-1 max-w-[560px]">
            <CountBars data={bars} />
          </div>
        </div>
        <div className="mt-4 pt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-[12px]" style={{ borderTop: '0.5px solid var(--hairline)' }}>
          {[['Good', 'Good'], ['Uncertain', 'Uncertain'], ['Bad', 'Bad'], ['No data', 'none']].map(([label, key]) => (
            <Link key={key} to={`/tags${scope ? `${scope}&` : '?'}quality=${encodeURIComponent(label)}`} className="link">
              {label} <span className="tnum font-semibold">{num(q.counts[key], 0)}</span>
            </Link>
          ))}
        </div>
      </div>

      {/* stat blocks */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-5">
        <Kpi label="Archive link" value={!leader ? '—' : leader.link_up ? 'Up' : 'Down'} href="/health"
             tone={!leader || !leader.link_up ? 'danger' : 'success'}
             sub={leader ? `${leader.instance} collector · ${num(leader.samples, 0)} samples acquired this run` : 'No collector reporting'} />
        <Kpi label="Buffered, not yet archived" value={leader ? num(leader.buffer_depth, 0) : '—'} unit="samples" href="/health"
             tone={leader && leader.buffer_depth > 0 ? 'warning' : undefined}
             sub="Held on the collector's disk while the archive is unreachable" />
        <Kpi label="Newest sample" value={newestAge === null ? '—' : ago(newest, d.now)} href="/tags"
             tone={newestAge !== null && newestAge > 10 ? 'danger' : undefined}
             sub={newest ? `Source time ${time(newest)}` : 'Nothing archived'} />
        <Kpi label="Rule verdicts not Good" value={num(flagged, 0)} href="/tags"
             tone={flagged > 0 ? 'warning' : undefined}
             sub="Range, rate, cross-tag, frozen and stale rules, computed apart from source quality" />
        <Kpi label="KPIs not Good" value={`${num(kpiBad.length, 0)} of ${num(kpis.length, 0)}`} href="/kpis"
             tone={kpiBad.length > 0 ? 'danger' : 'success'}
             sub="A KPI with a Bad input is Bad, naming the input — never zero" />
        <Kpi label="Latest start-up" value={startup ? duration(startup.duration_s) : '—'} href="/events"
             sub={startup ? `${startup.milestones.length} milestones · began ${time(startup.start_ts)}` : 'No complete start-up in the record'} />
        <Kpi label="Bench rig" value={rigTags.length === 0 ? '—' : rigUnreachable ? 'Unreachable' : 'Reporting'} href="/rig"
             tone={rigUnreachable ? 'danger' : undefined}
             sub={rigUnreachable ? 'Every rig tag is BadNoCommunication: the ESP32 does not answer Modbus' : 'Real sensors over Modbus TCP'} />
        <Kpi label="Live stream" value={d.link === 'connected' ? 'Connected' : 'Lost'} href="/health"
             tone={d.link === 'connected' ? undefined : 'danger'}
             sub={`${num(d.streamCount, 0)} collector events since this page opened`} />
      </div>

      <Section title="Alert ledger"
        note="One row per finding, across every module. Each links to the screen that holds the evidence. Bad before Uncertain; a value that is not Good shows its reason, never a number it does not have.">
        {alerts.length === 0 ? <Empty>No findings in the current scope.</Empty> : (
          <Ledger>
            <thead>
              <tr><th>Module</th><th>Finding</th><th className="n">Value</th><th></th></tr>
            </thead>
            <tbody>
              {alerts.map((a, i) => (
                <tr key={i}>
                  <td className="whitespace-nowrap align-top"><Chip tone={a.sev}>{a.module}</Chip></td>
                  <td>
                    <div className="font-semibold text-[13px]">{a.title}</div>
                    {a.detail && <div className="text-[12px] text-[var(--muted)] mt-0.5 max-w-[86ch]">{a.detail}</div>}
                  </td>
                  <td className="n align-top">
                    {a.sample !== undefined
                      ? <ValueShort sample={a.sample} unit={a.unit} />
                      : <span className="font-semibold">{a.value ?? '—'}</span>}
                  </td>
                  <td className="n align-top no-print"><OpenLink to={a.href} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>

      <Section title="What this screen is, and is not">
        <div className="grid md:grid-cols-2 gap-3">
          <Note>
            <strong>Refresh cadence.</strong> Collector status, tag health and KPIs are read every 2.5 seconds; values
            arrive on the collector's live stream as they are acquired. Times are IST; the archive stores UTC, shown on
            hover.
          </Note>
          <Note tone="warning">
            <strong>Scope.</strong> The unit is a simulator, not a real DCS; its tags are synthetic. The bench rig's
            tags are real sensors on a bench, and its current reading is uncalibrated. CRPMS does not control plant: the
            acquisition path has no write method at all. See <Link className="link" to="/sources">Data sources</Link>.
          </Note>
        </div>
      </Section>

      <Folio sources="OPC UA DCS simulator, bench rig over Modbus TCP, collector self-measurement, KPI engine"
             cadence="live stream as acquired; status and health every 2.5 s" />
    </>
  )
}

/** A value in a right-aligned ledger cell: the number, or "—" when Bad. */
export function ValueShort({ sample, unit, digits }) {
  if (!sample) return <span className="no-value">—</span>
  const k = sample.quality_class ?? klassOf(sample.quality)
  if (k === 'Bad' || sample.value === null || sample.value === undefined) return <span className="no-value">—</span>
  const v = typeof sample.value === 'boolean' ? String(sample.value) : num(sample.value, digits)
  return (
    <span className="tnum font-semibold" style={k === 'Uncertain' ? { color: 'var(--warning)' } : undefined}>
      {v}{unit && <span className="text-[12px] font-normal text-[var(--muted)] ml-1">{unit}</span>}
    </span>
  )
}

export const toneFor = toneOf
