// Health: the collector and its buffer (with the live pipeline), gaps and
// losses, and each source's health.

import { useCallback, useState } from 'react'
import { Link } from '../lib/router'
import { useData } from '../lib/data'
import { klassOf, provenanceOf } from '../lib/quality'
import { ago, num } from '../lib/format'
import Pipeline from '../Pipeline'
import { Chip, Empty, Folio, Kpi, Ledger, Note, OpenLink, PageHeader, PrintBar, ProvenanceChip,
         QualityChip, Section, Time, Value, TagName } from '../components/ui'
import { sampleClass } from './Tags'

// The pipeline canvas has fixed coordinates so the particle overlay can share
// them exactly. This scales the whole thing to the available width instead.
function ScaleToFit({ designWidth, height, children }) {
  const [scale, setScale] = useState(1)
  const ref = useCallback((node) => {
    if (!node) return
    const fit = () => setScale(Math.min(1, node.clientWidth / designWidth))
    fit()
    new ResizeObserver(fit).observe(node)
  }, [designWidth])
  return (
    <div ref={ref} style={{ width: '100%', overflow: 'hidden' }}>
      <div style={{ transform: `scale(${scale})`, transformOrigin: 'top left', height: height * scale }}>
        {children}
      </div>
    </div>
  )
}

export function CollectorHealth() {
  const d = useData()
  const cs = d.status?.collectors ?? []
  const leader = cs.find((c) => c.is_leader) ?? cs[0]
  return (
    <>
      <PageHeader title="Collector & buffer"
        subtitle="The data path, drawn from the collector's own events: a dot moves only as far as the collector has reported it went. Stop the archive and the dots stop at the buffer, because the collector starts reporting value_buffered instead of value_forwarded; they drain in source-time order when it returns (§319, §382)."
        provenance="REAL" right={<PrintBar />} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Archive link" value={!leader ? '—' : leader.link_up ? 'Up' : 'Down'} tone={!leader || !leader.link_up ? 'danger' : 'success'}
             sub={leader ? `Reported by ${leader.instance}` : 'No collector reporting'} />
        <Kpi label="Buffer depth" value={leader ? num(leader.buffer_depth, 0) : '—'} unit="samples"
             tone={leader && leader.buffer_depth > 0 ? 'warning' : undefined} sub="Held on disk until the archive accepts them" />
        <Kpi label="Samples this run" value={leader ? num(leader.samples, 0) : '—'} sub="Acquired by the leader since it started" />
        <Kpi label="Live stream" value={d.link === 'connected' ? 'Connected' : 'Lost'} tone={d.link === 'connected' ? undefined : 'danger'}
             sub={`${num(d.streamCount, 0)} events since this page opened`} />
      </div>
      <Section title="Pipeline">
        <div className="panel p-3"><ScaleToFit designWidth={1360} height={300}>
          <Pipeline events={d.events} status={d.status} kpis={d.kpis} />
        </ScaleToFit></div>
      </Section>
      <Section title="Collectors" note="Redundant collectors report their own state; the leader is the one whose samples are archived (§455).">
        {cs.length === 0 ? <Empty>No collector has reported.</Empty> : (
          <Ledger>
            <thead><tr><th>Instance</th><th>Alive</th><th>Leader</th><th>Archive link</th><th className="n">Buffer</th><th className="n">Samples</th><th className="n">Last report</th></tr></thead>
            <tbody>
              {cs.map((c) => (
                <tr key={c.instance}>
                  <td className="font-semibold">{c.instance}</td>
                  <td><Chip tone={c.alive ? 'success' : 'danger'}>{c.alive ? 'alive' : 'silent'}</Chip></td>
                  <td>{c.is_leader ? <Chip tone="gold">leader</Chip> : <span className="text-[var(--muted)]">standby</span>}</td>
                  <td>{c.alive
                    ? <Chip tone={c.link_up ? 'success' : 'danger'}>{c.link_up ? 'up' : 'down'}</Chip>
                    : <span className="text-[12px] text-[var(--muted)]">{c.link_up ? 'up' : 'down'} at last report</span>}</td>
                  <td className="n">{num(c.buffer_depth, 0)}</td>
                  <td className="n">{num(c.samples, 0)}</td>
                  <td className="n text-[var(--muted)]">{ago(new Date(d.now - c.age_s * 1000).toISOString(), d.now)}</td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>
      <Folio sources="collector events (WebSocket), collector_leader view" cadence="events as emitted; status every 2.5 s" />
    </>
  )
}

const COUNTERS = [
  ['PUBLISH_MISSED', 'OPC UA notifications missed, from the server\'s own sequence numbers'],
  ['BUFFER_LOST', 'Samples discarded because the buffer overflowed'],
  ['DROP_NO_SOURCE_TS', 'Values refused: no SourceTimestamp (never stamped with arrival time)'],
  ['DROP_NO_STATUS', 'Values refused: no StatusCode'],
  ['DROP_UNSTORABLE', 'Values refused: not storable as a number'],
  ['NO_SERVER_TS', 'Samples stored with no ServerTimestamp (kept, with the field empty)'],
  ['DUPLICATE_TS', 'Samples absorbed: a source timestamp already taken for that tag'],
  ['TAGS_UNRESOLVED', 'Configured tags not found in the source\'s address space'],
]

export function Gaps() {
  const d = useData()
  const quiet = d.health.filter((h) => h.state === 'missing' || h.state === 'stale')
  return (
    <>
      <PageHeader title="Gaps & losses"
        subtitle="Everything the collector counts that is not a sample arriving normally, and every tag that has gone quiet. A loss that is counted can be accounted for; the one that matters is the loss nobody counted."
        provenance="REAL" right={<PrintBar />} />
      <Section title="Collector counters" note="The collector's own measurements, archived as tags like any other. Counts are per collector run.">
        <Ledger>
          <thead><tr><th>Counter</th><th className="n">Primary</th><th className="n">Secondary</th><th>Meaning</th></tr></thead>
          <tbody>
            {COUNTERS.map(([m, meaning]) => (
              <tr key={m}>
                <td className="font-semibold whitespace-nowrap">{m.replaceAll('_', ' ').toLowerCase()}</td>
                {['PRIMARY', 'SECONDARY'].map((inst) => {
                  const name = `COLLECTOR_${inst}_${m}`
                  const s = d.values[name]
                  const k = s ? (s.quality_class ?? klassOf(s.quality)) : null
                  return (
                    <td key={inst} className="n">
                      {!s ? <span className="text-[var(--faint)]">—</span>
                        : k === 'Good' && Number(s.value) > 0 ? <Link className="link" to={`/tags/${name}`}><Chip tone="danger">{num(s.value, 0)}</Chip></Link>
                        : <Link className="link" to={`/tags/${name}`}><Value sample={s} digits={0} /></Link>}
                    </td>
                  )
                })}
                <td className="text-[12px] text-[var(--muted)]">{meaning}</td>
              </tr>
            ))}
          </tbody>
        </Ledger>
        <p className="reason mt-2">A counter with no value is Uncertain (UncertainInitialValue) until the collector has measured it; "—" means no sample in the last 24 hours — for the secondary, usually because it is not running.</p>
      </Section>
      <Section title="Tags gone quiet" note="Acquired tags with no sample in the engine's window (missing) or none for longer than their stale limit.">
        {quiet.length === 0 ? <Empty>Every acquired tag is reporting.</Empty> : (
          <Ledger>
            <thead><tr><th>Tag</th><th>State</th><th>Last sample</th><th>Detail</th><th></th></tr></thead>
            <tbody>
              {quiet.map((h) => (
                <tr key={h.tag}>
                  <td className="font-semibold"><Link className="link" to={`/tags/${h.tag}`}><TagName name={h.tag} /></Link></td>
                  <td><Chip tone="danger">{h.state}</Chip></td>
                  <td>{h.last_source_ts ? <><Time iso={h.last_source_ts} date /> <span className="reason">{ago(h.last_source_ts, d.now)}</span></> : <span className="text-[var(--muted)]">never</span>}</td>
                  <td className="text-[12px] text-[var(--muted)]">{h.detail}</td>
                  <td className="n no-print"><OpenLink to={`/tags/${h.tag}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>
      <Note>The archive also keeps the collector's per-run sequence numbers and a ledger of counted losses
        (<code>sample_seq_gap</code>, <code>collector_loss</code>); the API does not serve them, so they are read with
        the outage test and SQL, not on this screen.</Note>
      <Folio sources="collector health tags, engine tag health" cadence="counters as archived; health every 2.5 s" />
    </>
  )
}

const SOURCES = [
  { key: 'sim', name: 'DCS simulator — Unit 1', match: (t) => t.startsWith('U1_') },
  { key: 'u2', name: 'Unit 2 — configured, no source', match: (t) => t.startsWith('U2_') },
  { key: 'rig', name: 'Bench rig (ESP32 over Modbus)', match: (t) => t.startsWith('RIG_') },
  { key: 'col', name: 'Collector self-measurement', match: (t) => t.startsWith('COLLECTOR_PRIMARY_') },
]

export function SourceHealth() {
  const d = useData()
  return (
    <>
      <PageHeader title="Source health"
        subtitle="Each source of data, and how its tags stand now: how many are Good, Uncertain, Bad or silent, and when it last delivered."
        provenance={['REAL', 'SYNTHETIC']} right={<PrintBar />} />
      <Ledger>
        <thead><tr><th>Source</th><th className="n">Tags</th><th className="n">Good</th><th className="n">Uncertain</th><th className="n">Bad</th><th className="n">No data</th><th>Newest sample</th><th>Provenance</th></tr></thead>
        <tbody>
          {SOURCES.map((src) => {
            const tags = d.tags.filter((t) => src.match(t.name))
            const c = { Good: 0, Uncertain: 0, Bad: 0, 'No data': 0 }
            let newest = null
            for (const t of tags) {
              const s = d.values[t.name]
              const k = sampleClass(s)
              c[k in c ? k : 'Bad'] += 1
              if (s?.source_ts && (!newest || s.source_ts > newest)) newest = s.source_ts
            }
            const prov = tags[0] ? provenanceOf(tags[0]) : { p: 'SYNTHETIC', source: '' }
            return (
              <tr key={src.key}>
                <td className="font-semibold">{src.name}</td>
                <td className="n">{num(tags.length, 0)}</td>
                <td className="n">{num(c.Good, 0)}</td>
                <td className="n">{c.Uncertain ? <Chip tone="warning">{c.Uncertain}</Chip> : 0}</td>
                <td className="n">{c.Bad ? <Chip tone="danger">{c.Bad}</Chip> : 0}</td>
                <td className="n">{c['No data'] ? <Chip tone="muted">{c['No data']}</Chip> : 0}</td>
                <td>{newest ? <><Time iso={newest} /> <span className="reason">{ago(newest, d.now)}</span></> : <span className="text-[var(--muted)]">none in 24 h</span>}</td>
                <td><ProvenanceChip p={prov.p} source={prov.source} /></td>
              </tr>
            )
          })}
        </tbody>
      </Ledger>
      <Section title="Rig tags, one by one" note="The rig is the one source whose failures are physical: an unplugged probe, a rig that does not answer.">
        <Ledger>
          <thead><tr><th>Tag</th><th className="n">Value</th><th>Quality</th><th>Source time</th><th></th></tr></thead>
          <tbody>
            {d.tags.filter((t) => t.name.startsWith('RIG_')).map((t) => {
              const s = d.values[t.name]
              return (
                <tr key={t.name}>
                  <td className="font-semibold"><Link className="link" to={`/tags/${t.name}`}><TagName name={t.name} /></Link></td>
                  <td className="n"><Value sample={s} unit={t.unit} detail={d.healthByTag[t.name]?.detail} /></td>
                  <td><QualityChip code={s?.quality} klass={s ? undefined : null} /></td>
                  <td><Time iso={s?.source_ts} /></td>
                  <td className="n no-print"><OpenLink to={`/tags/${t.name}`} /></td>
                </tr>
              )
            })}
          </tbody>
        </Ledger>
      </Section>
      <Folio sources="archive, collector live stream" cadence="as acquired; unchanged tags re-read every 30 s" />
    </>
  )
}
