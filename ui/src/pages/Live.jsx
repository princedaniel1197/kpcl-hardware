// Live data: the unit mimic and TSI, trends, replay, and the bench rig.

import { useRouter } from '../lib/router'
import { useData } from '../lib/data'
import { useScope } from '../lib/scope'
import { provenanceOf } from '../lib/quality'
import { duration, num } from '../lib/format'
import Trend from '../Trend'
import Mimic from '../Mimic'
import Scrubber from '../Scrubber'
import { Empty, Folio, Kpi, Ledger, Note, OpenLink, PageHeader, PrintBar, ProvenanceChip,
         QualityChip, Section, Time, Value, TagName } from '../components/ui'
import { Link } from '../lib/router'

function ValueRows({ names }) {
  const d = useData()
  return (
    <Ledger>
      <thead><tr><th>Tag</th><th className="hidden sm:table-cell">Description</th><th className="n">Value</th><th>Quality</th><th>Source time</th><th>Provenance</th><th></th></tr></thead>
      <tbody>
        {names.map((n) => {
          const t = d.tagByName[n] ?? { name: n }
          const s = d.values[n]
          const prov = provenanceOf(t)
          return (
            <tr key={n}>
              <td className="font-semibold"><Link className="link" to={`/tags/${n}`}><TagName name={n} /></Link></td>
              <td className="hidden sm:table-cell text-[12px] text-[var(--muted)] max-w-[36ch]">{t.description}</td>
              <td className="n"><Value sample={s} unit={t.unit} detail={d.healthByTag[n]?.detail} /></td>
              <td><QualityChip code={s?.quality} klass={s ? undefined : null} /></td>
              <td><Time iso={s?.source_ts} /></td>
              <td><ProvenanceChip p={prov.p} source={prov.source} /></td>
              <td className="n no-print"><OpenLink to={`/tags/${n}`} /></td>
            </tr>
          )
        })}
      </tbody>
    </Ledger>
  )
}

const TSI = ['U1_TURB_SPEED', 'U1_BEARING_VIB', 'U1_GEN_STATOR_TEMP', 'U1_CONDENSER_VAC']

export function UnitOverview() {
  const d = useData()
  const kpis = d.kpis.filter((k) => k.asset_code === 'KPCL-RTPS-U1')
  const latest = d.frames.find((f) => f.asset_code === 'KPCL-RTPS-U1')
  return (
    <>
      <PageHeader kicker="RTPS · Unit 1 · KPCL-RTPS-U1" title="Unit overview"
        subtitle="The unit as an operator sees it: one mimic, drawn identically for every unit of this template (§470). Colour signals state only — a value that is not Good is drawn as what it is, never as a zero or an invented state."
        provenance="SYNTHETIC" right={<PrintBar />} />

      <div className="panel p-3 overflow-x-auto"><Mimic values={d.values} /></div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-5">
        {kpis.map((k) => (
          <Kpi key={k.kpi} label={`${k.kpi} v${k.version}`}
               value={k.quality_class === 'Bad' || k.value === null ? '—' : num(k.value)} unit={k.unit}
               tone={k.quality_class === 'Bad' ? 'danger' : k.quality_class === 'Uncertain' ? 'warning' : undefined}
               sub={k.reason ?? `Reference ${k.reference ?? '—'} ${k.unit ?? ''}`} href={`/kpis/${k.kpi}/${k.asset_code}`} />
        ))}
      </div>

      <Section title="Turbine supervisory (TSI)" note="Level 3 of the display hierarchy (§469): the unit's machine-protection quantities.">
        <ValueRows names={TSI} />
      </Section>

      <Section title="Latest event frame">
        {!latest ? <Empty>No event frame captured.</Empty> : (
          <Ledger>
            <thead><tr><th>Milestone</th><th>Reached</th><th className="n">After start</th><th></th></tr></thead>
            <tbody>
              {latest.milestones.map((m) => (
                <tr key={m.name}>
                  <td className="font-semibold">{m.name.replaceAll('_', ' ')}</td>
                  <td><Time iso={m.ts} /></td>
                  <td className="n tnum">+{duration(m.offset_s)}</td>
                  <td className="n no-print"><OpenLink to={`/events/${latest.id}`} /></td>
                </tr>
              ))}
            </tbody>
          </Ledger>
        )}
      </Section>

      <Folio sources="OPC UA DCS simulator via the collector's live stream, KPI engine, event frames" cadence="as acquired" />
    </>
  )
}

const DEFAULT_TRENDS = ['U1_MW', 'U1_MS_TEMP', 'U1_COAL_FLOW', 'U1_BEARING_VIB']

export function Trends() {
  const d = useData()
  const { query, path, navigate } = useRouter()
  const { minutes, periodLabel } = useScope()
  const chosen = (query.get('tags') ?? DEFAULT_TRENDS.join(',')).split(',').filter(Boolean)
  const setTags = (list) => {
    const p = new URLSearchParams(query.toString())
    p.set('tags', list.join(','))
    navigate(`${path}?${p}`)
  }
  const acquirable = d.tags.filter((t) => !t.name.startsWith('LOADTEST_'))
  return (
    <>
      <PageHeader kicker={periodLabel} title="Trends"
        subtitle="Archived samples on a time axis, with the quality each one carries. A line breaks at a Bad sample; Uncertain points are marked. Choose the period in the header."
        provenance={[...new Set(chosen.map((n) => provenanceOf(d.tagByName[n] ?? n).p))]}
        right={<PrintBar />} />
      <div className="flex flex-wrap items-center gap-2 mb-3 no-print">
        <select className="select" value="" onChange={(e) => e.target.value && setTags([...chosen, e.target.value])} aria-label="Add a tag">
          <option value="">Add a tag…</option>
          {acquirable.filter((t) => !chosen.includes(t.name)).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
        </select>
        {chosen.map((n) => (
          <button key={n} type="button" className="px-2 py-0.5 text-[12px]" title="Remove"
                  style={{ border: '0.5px solid var(--hairline)', borderRadius: 2, background: 'var(--panel)' }}
                  onClick={() => setTags(chosen.filter((x) => x !== n))}>{n} ×</button>
        ))}
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        {chosen.map((n) => {
          const t = d.tagByName[n]
          const prov = provenanceOf(t ?? n)
          return (
            <div key={n} className="panel p-3">
              <Trend tag={n} unit={t?.unit} minutes={minutes} />
              <div className="mt-1 flex flex-wrap gap-2 items-center">
                <ProvenanceChip p={prov.p} source={prov.source} />
                <span className="text-[11.5px] text-[var(--muted)]">{t?.description}</span>
              </div>
            </div>
          )
        })}
      </div>
      <Folio sources="archive (samples with StatusCode and both timestamps)" cadence="every 2.5 s" />
    </>
  )
}

export function ReplayPage() {
  return (
    <>
      <PageHeader title="Replay"
        subtitle="Replays what the archive holds for a window — every sample with its quality, and the collector's buffer and link state alongside — which is what actually happened, not a recording of the animation."
        provenance={['REAL', 'SYNTHETIC']} right={<PrintBar />} />
      <div className="panel p-3">
        <Scrubber tags={['U1_MW', 'U1_MS_TEMP', 'U1_COAL_FLOW', 'COLLECTOR_PRIMARY_BUFFER_DEPTH', 'COLLECTOR_PRIMARY_LINK_STATE']} />
      </div>
      <Folio sources="archive" cadence="on request" />
    </>
  )
}

const RIG = ['RIG_HUB_TEMP', 'RIG_AMBIENT_TEMP', 'RIG_CURRENT', 'RIG_VIBRATION', 'RIG_SUPPLY_V', 'RIG_RUNNING']

export function BenchRig() {
  const d = useData()
  const { minutes } = useScope()
  const rise = d.kpis.find((k) => k.asset_code === 'KPCL-RTPS-RIG')
  const rig = RIG.map((n) => d.healthByTag[n]).filter(Boolean)
  const unreachable = rig.length > 0 && rig.every((h) => h.source_quality === 2150694912)
  return (
    <>
      <PageHeader kicker="RTPS · KPCL-RTPS-RIG · ESP32 over Modbus TCP" title="Bench rig"
        subtitle="Real sensors on a bench — two DS18B20 probes, an MPU-6500, an ACS712 — read by an ESP32 and published into the OPC UA address space by the Modbus bridge. The tags say what the rig is: amperes and degrees, not a 210 MW unit."
        provenance="REAL" right={<PrintBar />} />

      {unreachable && (
        <div className="mb-4"><Note tone="danger"><strong>The rig is not answering.</strong> Every rig tag is
          BadNoCommunication: the bridge cannot reach the ESP32 over Modbus. The last good values are not shown as current;
          each tag shows "—" with its reason until the rig reports again.</Note></div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="panel px-3.5 py-3 h-full">
          <div className="text-[10.5px] uppercase tracking-[0.1em] text-[var(--muted)]">Motor thermal rise</div>
          <div className="mt-1.5">{rise ? <Value sample={{ value: rise.value, quality: rise.quality, quality_class: rise.quality_class }} unit={rise.unit} detail={rise.reason} big /> : '—'}</div>
        </div>
        <Kpi label="Hub probe" value={d.values.RIG_HUB_TEMP?.quality_class === 'Good' ? num(d.values.RIG_HUB_TEMP.value, 1) : '—'} unit="°C"
             tone={d.values.RIG_HUB_TEMP?.quality_class === 'Good' ? undefined : 'danger'} sub="DS18B20, identified by ROM address" />
        <Kpi label="Ambient probe" value={d.values.RIG_AMBIENT_TEMP?.quality_class === 'Good' ? num(d.values.RIG_AMBIENT_TEMP.value, 1) : '—'} unit="°C"
             tone={d.values.RIG_AMBIENT_TEMP?.quality_class === 'Good' ? undefined : 'danger'} sub="DS18B20 on the same wire" />
        <Kpi label="Load current" value={d.values.RIG_CURRENT && d.values.RIG_CURRENT.quality_class !== 'Bad' ? num(d.values.RIG_CURRENT.value, 3) : '—'} unit="A"
             tone={d.values.RIG_CURRENT?.quality_class === 'Bad' || !d.values.RIG_CURRENT ? 'danger' : 'warning'}
             sub="Uncalibrated — bench demo only; never Good" />
      </div>

      <Section title="Rig tags">
        <ValueRows names={RIG} />
      </Section>

      <Section title="Trends">
        <div className="grid lg:grid-cols-2 gap-4">
          {['RIG_HUB_TEMP', 'RIG_AMBIENT_TEMP', 'RIG_CURRENT', 'RIG_VIBRATION'].map((n) => (
            <div key={n} className="panel p-3"><Trend tag={n} unit={d.tagByName[n]?.unit} minutes={minutes} /></div>
          ))}
        </div>
      </Section>

      <Section title="What the rig is, and is not">
        <div className="grid md:grid-cols-2 gap-3">
          <Note><strong>Real.</strong> The temperatures are two DS18B20 probes read by ROM address, so an unplugged probe
            is Bad with no value rather than the other probe standing in for it — the Stage 10 test, passed on 24 September
            2026. Vibration is an approximate velocity from an MPU-6500.</Note>
          <Note tone="warning"><strong>Uncalibrated current.</strong> A meter put the fans at 0.1–0.25 A while the rig read
            0.12–0.88 A: the ADC is uncalibrated and the bench ground is poor. RIG_CURRENT is published Uncertain and no KPI or
            alert uses it. The relays and the run switch are not fitted, so their inputs are Bad (BadNotConnected).</Note>
        </div>
      </Section>

      <Folio sources="ESP32 over Modbus TCP, Modbus-to-OPC UA bridge, collector" cadence="bridge polls every second; values as acquired" />
    </>
  )
}
