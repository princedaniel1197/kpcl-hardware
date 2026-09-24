import { useCallback, useEffect, useMemo, useState } from 'react'
import Pipeline from './Pipeline'
import Mimic from './Mimic'
import Trend from './Trend'
import Scrubber from './Scrubber'
import { AuthError, connectEvents, getEvents, getKpis, getStatus, getTagHealth,
         getToken, getWhoami, setToken } from './api'
import { isa, mono, sans, qualityColour } from './theme'

const MIMIC_TAGS = [
  'U1_MW', 'U1_TURB_SPEED', 'U1_DRUM_PRESS', 'U1_MS_TEMP', 'U1_MS_PRESS',
  'U1_FEEDWATER_FLOW', 'U1_COAL_FLOW', 'U1_AUX_POWER', 'U1_CONDENSER_VAC',
  'U1_GEN_STATOR_TEMP', 'U1_BEARING_VIB', 'U1_BOILER_LIGHTUP',
  'U1_TURB_ROLLING', 'U1_BREAKER_CLOSED',
]

const TABS = ['Pipeline', 'Unit overview', 'Unit TSI', 'Trends', 'Replay', 'Health']

// Every API call is authenticated (§509). Without a valid token there is
// nothing to show, so the first thing on screen is the sign-in.
export default function App() {
  const [who, setWho] = useState(null)
  const [checked, setChecked] = useState(false)

  const check = useCallback(async () => {
    if (!getToken()) { setWho(null); setChecked(true); return }
    try {
      setWho(await getWhoami())
    } catch (e) {
      if (e instanceof AuthError) setToken(null)
      setWho(null)
    }
    setChecked(true)
  }, [])

  useEffect(() => { check() }, [check])
  const signOut = useCallback(() => { setToken(null); setWho(null) }, [])

  if (!checked) return null
  if (!who) return <SignIn onToken={(t) => { setToken(t); check() }} />
  return <Dashboard who={who} onSignOut={signOut} />
}

function SignIn({ onToken }) {
  const [value, setValue] = useState('')
  return (
    <div style={{ fontFamily: sans, background: isa.background, minHeight: '100vh',
                  color: isa.text, display: 'flex', alignItems: 'center',
                  justifyContent: 'center', padding: 16 }}>
      <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) onToken(value.trim()) }}
            style={{ background: isa.panel, border: `1px solid ${isa.line}`,
                     padding: 20, width: 'min(420px, 100%)' }}>
        <strong style={{ letterSpacing: 0.5 }}>CRPMS</strong>
        <p style={{ fontSize: 12, color: isa.textDim }}>
          Paste an access token. Tokens are issued with
          <code style={{ fontFamily: mono }}> python -m ops.access create</code> and
          shown once; the server keeps only their SHA-256.
        </p>
        <input type="password" autoFocus value={value}
               onChange={(e) => setValue(e.target.value)}
               aria-label="access token"
               style={{ width: '100%', boxSizing: 'border-box', fontFamily: mono,
                        fontSize: 12, padding: 6, border: `1px solid ${isa.lineStrong}` }} />
        <button type="submit" style={{ marginTop: 10, fontFamily: sans, fontSize: 12,
                                       padding: '4px 12px', cursor: 'pointer' }}>
          Sign in
        </button>
      </form>
    </div>
  )
}

function Dashboard({ who, onSignOut }) {
  const [tab, setTab] = useState('Pipeline')
  const [events, setEvents] = useState([])
  const [link, setLink] = useState('connecting')
  const [status, setStatus] = useState(null)
  const [kpis, setKpis] = useState([])
  const [health, setHealth] = useState([])
  const [frames, setFrames] = useState([])
  const [values, setValues] = useState({})

  // Live events. Kept bounded: the picture needs the recent past, not all of it.
  const onEvent = useCallback((e) => {
    setEvents((prev) => [...prev.slice(-400), e])
    if (e.kind === 'value_received' && e.tag) {
      setValues((v) => ({
        ...v,
        [e.tag]: {
          value: e.value,
          quality: e.quality,
          quality_class: ['Good', 'Uncertain', 'Bad', 'Reserved'][(e.quality >>> 30) & 3],
          source_ts: e.source_ts,
        },
      }))
    }
  }, [])

  useEffect(() => connectEvents(onEvent, setLink), [onEvent])

  useEffect(() => {
    const poll = async () => {
      try {
        const [s, k, h, f] = await Promise.all([
          getStatus(), getKpis(), getTagHealth(), getEvents('KPCL-RTPS-U1')])
        setStatus(s); setKpis(k); setHealth(h); setFrames(f)
      } catch (e) {
        // A revoked or expired token signs the viewer out; the API being
        // briefly away is not worth a red screen.
        if (e instanceof AuthError) onSignOut()
      }
    }
    poll()
    const t = setInterval(poll, 2500)          // §528: 2-3 s dashboard refresh
    return () => clearInterval(t)
  }, [onSignOut])

  const badTags = health.filter((h) => h.state !== 'ok')

  return (
    <div style={{ fontFamily: sans, background: isa.background, minHeight: '100vh',
                  color: isa.text }}>
      <header style={{ display: 'flex', alignItems: 'baseline', gap: 16,
                       padding: '10px 16px', borderBottom: `1px solid ${isa.line}`,
                       background: isa.panel }}>
        <strong style={{ letterSpacing: 0.5 }}>CRPMS</strong>
        <span style={{ fontSize: 11, color: isa.textDim }}>
          Orianode demonstrator — KPCL RTPS
        </span>
        <nav style={{ display: 'flex', gap: 4, marginLeft: 12 }}>
          {TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)}
                    style={{
                      fontFamily: sans, fontSize: 12, padding: '3px 10px',
                      border: `1px solid ${t === tab ? isa.lineStrong : 'transparent'}`,
                      background: t === tab ? isa.background : 'transparent',
                      cursor: 'pointer',
                    }}>{t}</button>
          ))}
        </nav>
        <span style={{ marginLeft: 'auto', fontFamily: mono, fontSize: 11,
                       color: link === 'connected' ? isa.textDim : isa.bad }}>
          {link === 'connected' ? `live · ${events.length} events` : `stream ${link}`}
        </span>
        <span style={{ fontSize: 11, color: isa.textDim }}>
          {who.username} · {who.role}{who.station ? ` (${who.station})` : ''}
        </span>
        <button onClick={onSignOut}
                style={{ fontFamily: sans, fontSize: 11, padding: '2px 8px',
                         cursor: 'pointer' }}>
          Sign out
        </button>
      </header>

      <main style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
        {tab === 'Pipeline' && (
          <>
            <ScaleToFit designWidth={1360}>
              <Pipeline events={events} status={status} kpis={kpis} />
            </ScaleToFit>
            <KpiStrip kpis={kpis} />
            <p style={{ fontSize: 11, color: isa.textDim, maxWidth: 780, margin: 0 }}>
              Every dot is one acquired value, drawn because the collector
              emitted an event — not on a timer. Stop the database and the dots
              stop at the buffer, because the collector starts reporting
              <code> value_buffered</code> instead of <code> value_forwarded</code>.
            </p>
          </>
        )}

        {tab === 'Unit overview' && (
          <>
            <Mimic values={values} />
            <KpiStrip kpis={kpis} />
          </>
        )}

        {tab === 'Unit TSI' && <TsiOverview values={values} frames={frames} />}

        {tab === 'Trends' && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {['U1_MW', 'U1_MS_TEMP', 'U1_COAL_FLOW', 'U1_BEARING_VIB'].map((t) => (
              <Trend key={t} tag={t} />
            ))}
          </div>
        )}

        {tab === 'Replay' && (
          <Scrubber tags={['U1_MW', 'U1_MS_TEMP', 'U1_COAL_FLOW',
                           'COLLECTOR_PRIMARY_BUFFER_DEPTH',
                           'COLLECTOR_PRIMARY_LINK_STATE']} />
        )}

        {tab === 'Health' && (
          <div>
            <h3 style={{ fontSize: 13, margin: '0 0 8px' }}>
              Tag health — {badTags.length} not OK of {health.length}
            </h3>
            <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: isa.textDim }}>
                  <th style={th}>Tag</th><th style={th}>State</th>
                  <th style={th}>Source quality</th><th style={th}>Computed</th>
                  <th style={th}>Detail</th>
                </tr>
              </thead>
              <tbody>
                {health.map((h) => (
                  <tr key={h.tag} style={{ borderTop: `1px solid ${isa.line}` }}>
                    <td style={{ ...td, fontFamily: mono }}>{h.tag}</td>
                    <td style={{ ...td, color: h.state === 'ok' ? isa.textDim
                                               : h.state === 'uncertain' ? isa.uncertain
                                               : isa.bad }}>
                      {h.state}
                    </td>
                    <td style={{ ...td, color: qualityColour(h.source_class) }}>
                      {h.source_quality === null ? '—' : `${h.source_class} (${h.source_quality})`}
                    </td>
                    <td style={{ ...td, color: qualityColour(h.computed_class) }}>
                      {h.computed_class}
                    </td>
                    <td style={{ ...td, color: isa.textDim }}>{h.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p style={{ fontSize: 11, color: isa.textDim, maxWidth: 780 }}>
              Source quality and computed quality are separate columns on purpose.
              A value that arrived Good and failed a range check is not the same
              thing as a value the instrument disowned.
            </p>
          </div>
        )}
      </main>
    </div>
  )
}

// The pipeline canvas has fixed coordinates so the particle overlay can share
// them exactly. This scales the whole thing to the available width instead.
function ScaleToFit({ designWidth, children }) {
  const [scale, setScale] = useState(1)
  const ref = useCallback((node) => {
    if (!node) return
    const fit = () => setScale(Math.min(1, node.clientWidth / designWidth))
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(node)
  }, [designWidth])
  return (
    <div ref={ref} style={{ width: '100%', overflow: 'hidden' }}>
      <div style={{ transform: `scale(${scale})`, transformOrigin: 'top left',
                    height: 300 * scale }}>
        {children}
      </div>
    </div>
  )
}

const th = { padding: '4px 8px', fontWeight: 500 }
const td = { padding: '3px 8px' }

function KpiStrip({ kpis }) {
  return (
    <div style={{ display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))',
                  gap: 10 }}>
      {kpis.map((k) => {
        const abnormal = k.quality_class !== 'Good'
        return (
          <div key={`${k.kpi}-${k.asset_code}`}
               style={{ border: `1px solid ${abnormal ? isa.bad : isa.line}`,
                        background: abnormal ? '#f7e9e8' : isa.panel,
                        padding: '6px 9px' }}>
            <div style={{ fontSize: 10, color: isa.textDim }}>
              {k.kpi} <span style={{ fontFamily: mono }}>v{k.version}</span>
            </div>
            <div style={{ fontFamily: mono, fontSize: 18,
                          color: abnormal ? isa.bad : isa.value }}>
              {k.value === null ? '- - -' : Number(k.value).toFixed(2)}
              <span style={{ fontSize: 10, color: isa.textDim, marginLeft: 4 }}>
                {k.unit}
              </span>
            </div>
            <div style={{ fontSize: 9, color: abnormal ? isa.bad : isa.textDim }}>
              {k.reason ?? `${k.asset_code} · ref ${k.reference ?? '—'}`}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// §469 asks for a Unit TSI (turbine supervisory) overview alongside the unit
// overview. Same layout conventions, different depth — level 3 of the display
// hierarchy.
function TsiOverview({ values, frames }) {
  const v = (t) => values[t] ?? { value: null, quality_class: 'unknown' }
  const latest = frames[0]
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div style={{ border: `1px solid ${isa.line}`, background: isa.panel,
                    padding: 12 }}>
        <div style={{ fontSize: 12, marginBottom: 8 }}>
          UNIT TSI OVERVIEW — KPCL-RTPS-U1
        </div>
        {[['Shaft speed', 'U1_TURB_SPEED', 'rpm', 0],
          ['Bearing vibration', 'U1_BEARING_VIB', 'mm/s', 2],
          ['Stator temperature', 'U1_GEN_STATOR_TEMP', '°C', 1],
          ['Condenser vacuum', 'U1_CONDENSER_VAC', 'mmHg', 0]].map(
          ([label, tag, unit, digits]) => {
            const d = v(tag)
            const abnormal = d.quality_class !== 'Good'
            return (
              <div key={tag} style={{ display: 'flex', justifyContent: 'space-between',
                                      borderTop: `1px solid ${isa.line}`,
                                      padding: '6px 0' }}>
                <span style={{ fontSize: 11, color: isa.textDim }}>{label}</span>
                <span style={{ fontFamily: mono, fontSize: 15,
                               color: abnormal ? qualityColour(d.quality_class) : isa.value }}>
                  {d.value === null ? '- - -' : Number(d.value).toFixed(digits)} {unit}
                </span>
              </div>
            )
          })}
      </div>
      <div style={{ border: `1px solid ${isa.line}`, background: isa.panel,
                    padding: 12 }}>
        <div style={{ fontSize: 12, marginBottom: 8 }}>LATEST EVENT FRAME</div>
        {!latest && <div style={{ fontSize: 11, color: isa.textDim }}>none captured</div>}
        {latest && (
          <>
            <div style={{ fontFamily: mono, fontSize: 11, color: isa.textDim }}>
              {latest.template} · {latest.status}
              {latest.duration_s != null && ` · ${(latest.duration_s / 60).toFixed(1)} min`}
            </div>
            <table style={{ fontSize: 11, marginTop: 8, width: '100%' }}>
              <tbody>
                {latest.milestones.map((m) => (
                  <tr key={m.name}>
                    <td style={{ ...td, color: isa.textDim }}>{m.name}</td>
                    <td style={{ ...td, fontFamily: mono, textAlign: 'right' }}>
                      +{Math.round(m.offset_s)}s
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}
