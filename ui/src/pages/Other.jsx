// Data sources, settings, and the page for a path that is not one.

import { Link } from '../lib/router'
import { useData } from '../lib/data'
import { num } from '../lib/format'
import { Empty, Folio, Ledger, Note, PageHeader, PrintBar, ProvenanceChip, Section } from '../components/ui'

const SOURCES = [
  { name: 'DCS simulator', what: 'An OPC UA server standing in for a 210 MW unit\'s DCS, with a scripted cold start-up. Not a real BHEL, Yokogawa, ABB or Andritz system.',
    protocol: 'OPC UA subscription (SourceTimestamp + ServerTimestamp + StatusCode on every value)', prefix: 'U1_', p: 'SYNTHETIC', cadence: '250–1000 ms per tag' },
  { name: 'Unit 2', what: 'Created from Unit 1\'s template to show that adding a unit is configuration. The simulator serves no Unit 2: its tags have no data, by design.',
    protocol: 'none', prefix: 'U2_', p: 'SYNTHETIC', cadence: '—' },
  { name: 'Bench rig', what: 'An ESP32 on a bench reading two DS18B20 probes, an MPU-6500 and an ACS712, bridged into OPC UA. Real sensors. The current reading is uncalibrated (bench demo only); relays and run switch are not fitted.',
    protocol: 'Modbus TCP over WiFi → Modbus-to-OPC UA bridge → OPC UA', prefix: 'RIG_', p: 'REAL', cadence: '1 s bridge poll' },
  { name: 'Collector self-measurement', what: 'The collector\'s own counters and state — link, buffer, losses, refusals — archived as tags.',
    protocol: 'written by the collector', prefix: 'COLLECTOR_', p: 'REAL', cadence: '5 s' },
  { name: 'Load test', what: 'Ten million generated rows — the Stage 2 load test — that prove the 24-hour trend query returns in under five seconds on an archive of that size.',
    protocol: 'generated', prefix: 'LOADTEST_', p: 'SYNTHETIC', cadence: 'historical only' },
  { name: 'OMF demonstration', what: 'One tag written through the PI-compatible OMF path, received by this project\'s own OMF receiver. Whether a real PI Web API accepts it has not been tested.',
    protocol: 'OMF over HTTP', prefix: 'OMF_', p: 'SYNTHETIC', cadence: 'on demonstration' },
]

export function DataSources() {
  const d = useData()
  return (
    <>
      <PageHeader title="Data sources"
        subtitle="Where every value on these screens comes from, how it arrives, and whether a real instrument is behind it. The provenance chip on every value is decided from this list."
        provenance={['REAL', 'SYNTHETIC']} right={<PrintBar />} />
      <Ledger>
        <thead><tr><th>Source</th><th>What it is</th><th>Arrives by</th><th className="n">Tags</th><th>Cadence</th><th>Provenance</th></tr></thead>
        <tbody>
          {SOURCES.map((s) => (
            <tr key={s.name}>
              <td className="font-semibold whitespace-nowrap">{s.name}</td>
              <td className="text-[12.5px] max-w-[52ch]">{s.what}</td>
              <td className="text-[12px] text-[var(--muted)] max-w-[30ch]">{s.protocol}</td>
              <td className="n"><Link className="link" to="/tags">{num(d.tags.filter((t) => t.name.startsWith(s.prefix)).length, 0)}</Link></td>
              <td className="text-[12px] whitespace-nowrap">{s.cadence}</td>
              <td><ProvenanceChip p={s.p} /></td>
            </tr>
          ))}
        </tbody>
      </Ledger>
      <Section title="Not on these screens">
        <div className="grid md:grid-cols-2 gap-3">
          <Note><strong>Karnataka SLDC generation.</strong> A separate recorder archives the state load despatch centre's
            published station and unit generation every five minutes. It is real public data, but the API does not serve
            it, so it is not shown here.</Note>
          <Note tone="warning"><strong>What CRPMS does not demonstrate.</strong> Any real DCS; a licensed AVEVA PI
            installation; 34,700 I/O across 13 sites; wide-area network behaviour; cross-site time synchronisation; boiler
            and turbine physics. The first list is believable because this one is written down.</Note>
        </div>
      </Section>
      <Folio />
    </>
  )
}

export function Settings() {
  return (
    <>
      <PageHeader title="Settings" subtitle="How this screen reads the system. There is nothing here to configure the plant with: CRPMS is read-only." />
      <Section title="Access">
        <Note tone="warning">There is no sign-in and no role-based access. Token access (§509) was removed from the API
          and this interface by decision on 24 Sep 2026: anyone who can reach the API reads everything it serves.</Note>
      </Section>
      <Section title="How this screen reads the system">
        <Ledger>
          <tbody>
            <tr><td className="text-[var(--muted)] w-[220px]">Status, health, KPIs</td><td>every 2.5 s (§528 asks for 2–3 s)</td></tr>
            <tr><td className="text-[var(--muted)]">Values</td><td>the collector's live stream, as acquired; tags the stream has not updated in 30 s are re-read from the archive</td></tr>
            <tr><td className="text-[var(--muted)]">Tags, asset model, event frames</td><td>every 30 s</td></tr>
            <tr><td className="text-[var(--muted)]">Times</td><td>shown in IST; the archive stores UTC, shown on hover</td></tr>
            <tr><td className="text-[var(--muted)]">Language</td><td>English only — the strings are not translated, so there is no language toggle</td></tr>
          </tbody>
        </Ledger>
      </Section>
      <Folio />
    </>
  )
}

export function NotFound() {
  return (
    <>
      <PageHeader title="Not a page" subtitle="Nothing lives at this address." />
      <Empty><Link className="link" to="/">Back to the overview</Link></Empty>
      <Folio />
    </>
  )
}
