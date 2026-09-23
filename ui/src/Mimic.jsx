// Unit mimic, ANSI/ISA-101.01-2015 high-performance HMI conventions.
//
// The governing idea, and the reason this looks austere next to a conventional
// mimic: the display is GREY, and colour is reserved for abnormal states. On a
// colourful schematic an alarm competes with the decoration. Here, anything
// coloured is the only thing coloured.
//
// The layout is identical for every unit (§470): the component takes the unit's
// tag prefix and asset code, and nothing else about a unit, so every unit of
// this template is drawn by exactly the same code in exactly the same places.
// An operator who has learned where to look should not have to learn again per
// unit.
//
// UNKNOWN IS DRAWN AS UNKNOWN. A tag that is Bad has no value, and the graphics
// must not quietly treat that as zero: an earlier version showed a Bad shaft
// speed as "0 rpm", drew a Bad breaker state as OPEN, and drew an empty load
// bar for a Bad generation reading -- each a zero or a state invented for a
// value that does not exist. Now a missing value shows "- - -", a digital with
// no known state shows "?", and a bar or rotor with no value is drawn hatched
// or still and says so.

import { isa, mono, sans, qualityColour } from './theme'

const W = 860, H = 430

// A process value: dark grey when normal, coloured only when it is not.
function PV({ x, y, label, value, unit, quality, digits = 1, warn }) {
  const klass = quality
  const abnormal = klass === 'Bad' || klass === 'Uncertain' || warn
  const colour = abnormal ? qualityColour(klass === 'Good' ? 'Uncertain' : klass) : isa.value
  const shown = value === null || value === undefined
    ? '- - -'
    : Number(value).toFixed(digits)
  return (
    <g>
      <text x={x} y={y} fontSize="10" fill={isa.textDim} fontFamily={sans}>{label}</text>
      <text x={x} y={y + 17} fontSize="16" fill={colour} fontFamily={mono}>
        {shown}
        <tspan fontSize="10" fill={isa.textDim} dx="4">{unit}</tspan>
      </text>
      {klass === 'Bad' && (
        <text x={x} y={y + 29} fontSize="9" fill={isa.bad} fontFamily={sans}>
          BAD — {` `}no value
        </text>
      )}
      {klass === 'Uncertain' && (
        <text x={x} y={y + 29} fontSize="9" fill={isa.uncertain} fontFamily={sans}>
          UNCERTAIN
        </text>
      )}
    </g>
  )
}

// A digital's state: true, false, or null when it is not known.
const state = (d) => (d.value === null || d.value === undefined ? null : d.value >= 1)

export default function Mimic({ values, unit = 'U1', assetCode = 'KPCL-RTPS-U1',
                                ratedMw = 210 }) {
  const v = (name) => values[`${unit}_${name}`] ?? { value: null, quality_class: 'unknown' }
  const speed = v('TURB_SPEED').value ?? null
  const mw = v('MW').value ?? null
  const breaker = state(v('BREAKER_CLOSED'))
  const lightup = state(v('BOILER_LIGHTUP'))
  const drumPress = v('DRUM_PRESS').value ?? null
  // Rotor rotation is driven by the ACTUAL speed tag: at 3000 rpm the mark
  // turns once per 20 ms of animation time, at standstill it does not turn,
  // and with no speed value it does not turn and is drawn dashed.
  const revPerSec = speed === null ? 0 : speed / 60
  const spinDuration = revPerSec > 0.05 ? (1 / revPerSec) * 8 : 0
  const loadFraction = mw === null ? null : Math.max(0, Math.min(mw / ratedMw, 1))
  // Drum level is not measured by this simulator, so the vessel shows PRESSURE
  // as a fill fraction and says so. Drawing a level we do not have would be
  // inventing an instrument.
  const pressFraction = drumPress === null ? null
    : Math.max(0, Math.min(drumPress / 160, 1))

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', background: isa.background,
                                            border: `1px solid ${isa.line}` }}>
      <defs>
        <pattern id="unknown" width="6" height="6" patternUnits="userSpaceOnUse"
                 patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke={isa.uncertain} strokeWidth="2" />
        </pattern>
      </defs>
      <text x="14" y="22" fontSize="12" fill={isa.text} fontFamily={sans}
            letterSpacing="0.5">UNIT OVERVIEW — {assetCode}</text>
      <line x1="14" y1="30" x2={W - 14} y2="30" stroke={isa.line} />

      {/* ---------- boiler ---------- */}
      <rect x="40" y="70" width="120" height="180" fill={isa.panel}
            stroke={isa.lineStrong} />
      <text x="100" y="64" fontSize="10" fill={isa.textDim} fontFamily={sans}
            textAnchor="middle">BOILER</text>
      {/* drum */}
      <rect x="60" y="88" width="80" height="34" rx="17" fill={isa.panelDark}
            stroke={isa.lineStrong} />
      {pressFraction === null ? (
        <rect x="60" y="88" width="80" height="34" rx="17" fill="url(#unknown)" />
      ) : (
        <rect x="60" y={88 + 34 * (1 - pressFraction)} width="80"
              height={34 * pressFraction} rx="17" fill={isa.line} opacity="0.75" />
      )}
      <text x="100" y="110" fontSize="9" fill={isa.text} fontFamily={mono}
            textAnchor="middle">DRUM</text>
      {/* furnace: colour ONLY when fired, because that is a state worth seeing */}
      <rect x="62" y="160" width="76" height="70"
            fill={lightup === null ? 'url(#unknown)' : lightup ? '#c9622a' : isa.panelDark}
            stroke={isa.lineStrong} opacity={lightup ? 0.85 : 1} />
      <text x="100" y="200" fontSize="9" textAnchor="middle" fontFamily={sans}
            fill={lightup ? '#fff' : lightup === null ? isa.uncertain : isa.textDim}>
        {lightup === null ? '?' : lightup ? 'FIRING' : 'OFF'}
      </text>

      {/* ---------- main steam line ---------- */}
      <line x1="160" y1="105" x2="300" y2="105" stroke={isa.lineStrong} strokeWidth="3" />
      <PV x={190} y={62} label="MAIN STEAM" value={v('MS_TEMP').value}
          unit="°C" quality={v('MS_TEMP').quality_class} />
      <PV x={190} y={122} label="PRESSURE" value={v('MS_PRESS').value}
          unit="kg/cm²" quality={v('MS_PRESS').quality_class} />

      {/* ---------- turbine ---------- */}
      <rect x="300" y="78" width="150" height="56" fill={isa.panel}
            stroke={isa.lineStrong} />
      <text x="375" y="72" fontSize="10" fill={isa.textDim} fontFamily={sans}
            textAnchor="middle">TURBINE</text>
      <circle cx="375" cy="106" r="20" fill={isa.panelDark} stroke={isa.lineStrong} />
      <g>
        {/* the rotor mark turns at the actual shaft speed */}
        <line x1="375" y1="106" x2="375" y2="90" stroke={isa.text} strokeWidth="2"
              strokeDasharray={speed === null ? '2 2' : undefined}>
          {spinDuration > 0 && (
            <animateTransform attributeName="transform" type="rotate"
                              from="0 375 106" to="360 375 106"
                              dur={`${spinDuration}s`} repeatCount="indefinite" />
          )}
        </line>
      </g>
      <PV x={300} y={150} label="SPEED" value={speed} unit="rpm" digits={0}
          quality={v('TURB_SPEED').quality_class} />
      {/* The warning level is the alert rule's own threshold
          (config/alert_rules.json, 7.1 mm/s), not a number chosen here. */}
      <PV x={380} y={150} label="VIBRATION" value={v('BEARING_VIB').value}
          unit="mm/s" digits={2} quality={v('BEARING_VIB').quality_class}
          warn={v('BEARING_VIB').value !== null && v('BEARING_VIB').value > 7.1} />

      {/* ---------- generator and breaker ---------- */}
      <line x1="450" y1="106" x2="500" y2="106" stroke={isa.lineStrong} strokeWidth="3" />
      <circle cx="530" cy="106" r="30" fill={isa.panel} stroke={isa.lineStrong} />
      <text x="530" y="111" fontSize="14" textAnchor="middle" fontFamily={mono}
            fill={isa.text}>G</text>
      <line x1="560" y1="106" x2="620" y2="106" stroke={isa.lineStrong} strokeWidth="3" />
      {/* breaker: closed is a plain bar, open is a visible break — the
          abnormal state is the one that stands out */}
      {breaker === null ? (
        <>
          <rect x="620" y="94" width="26" height="24" fill="url(#unknown)"
                stroke={isa.uncertain} strokeWidth="2" />
          <text x="633" y="134" fontSize="9" textAnchor="middle" fontFamily={sans}
                fill={isa.uncertain}>? STATE UNKNOWN</text>
        </>
      ) : (
        <>
          <rect x="620" y="94" width="26" height="24"
                fill={breaker ? isa.panelDark : isa.background}
                stroke={breaker ? isa.lineStrong : isa.bad} strokeWidth={breaker ? 1 : 2} />
          {!breaker && <line x1="624" y1="118" x2="642" y2="96" stroke={isa.bad}
                             strokeWidth="2" />}
          <text x="633" y="134" fontSize="9" textAnchor="middle" fontFamily={sans}
                fill={breaker ? isa.textDim : isa.bad}>
            {breaker ? 'CLOSED' : 'OPEN'}
          </text>
        </>
      )}
      <line x1="646" y1="106" x2="710" y2="106" stroke={isa.lineStrong} strokeWidth="3" />
      <text x="716" y="110" fontSize="10" fill={isa.textDim} fontFamily={sans}>GRID</text>

      {/* ---------- load bar ---------- */}
      <text x="300" y="215" fontSize="10" fill={isa.textDim} fontFamily={sans}>
        GROSS GENERATION
      </text>
      <rect x="300" y="222" width="410" height="22" fill={isa.panelDark}
            stroke={isa.lineStrong} />
      {loadFraction === null ? (
        <rect x="300" y="222" width="410" height="22" fill="url(#unknown)" />
      ) : (
        <rect x="300" y="222" width={410 * loadFraction} height="22"
              fill={isa.lineStrong} />
      )}
      <text x={306} y={238} fontSize="13" fontFamily={mono}
            fill={loadFraction !== null && loadFraction > 0.35 ? '#fff'
                  : mw === null ? isa.uncertain : isa.value}>
        {mw === null ? '- - - no value' : mw.toFixed(1)} MW
      </text>
      <text x={700} y={238} fontSize="10" fontFamily={mono} fill={isa.textDim}
            textAnchor="end">{ratedMw}</text>

      {/* ---------- secondary values ---------- */}
      <line x1="14" y1="268" x2={W - 14} y2="268" stroke={isa.line} />
      <PV x={40} y={286} label="FEEDWATER" value={v('FEEDWATER_FLOW').value}
          unit="t/h" quality={v('FEEDWATER_FLOW').quality_class} />
      <PV x={170} y={286} label="COAL FLOW" value={v('COAL_FLOW').value}
          unit="t/h" quality={v('COAL_FLOW').quality_class} />
      <PV x={300} y={286} label="AUX POWER" value={v('AUX_POWER').value}
          unit="MW" quality={v('AUX_POWER').quality_class} />
      <PV x={430} y={286} label="CONDENSER" value={v('CONDENSER_VAC').value}
          unit="mmHg" digits={0} quality={v('CONDENSER_VAC').quality_class} />
      <PV x={560} y={286} label="STATOR TEMP" value={v('GEN_STATOR_TEMP').value}
          unit="°C" quality={v('GEN_STATOR_TEMP').quality_class} />
      <PV x={690} y={286} label="DRUM PRESS" value={drumPress}
          unit="kg/cm²" quality={v('DRUM_PRESS').quality_class} />

      <text x="40" y="350" fontSize="9" fill={isa.textDim} fontFamily={sans}>
        Colour is reserved for abnormal states. A value shown as “- - -”, or a
        hatched shape, is not zero: it arrived Bad and carries no value.
      </text>
      <text x="40" y="364" fontSize="9" fill={isa.textDim} fontFamily={sans}>
        The vessel fill shows drum PRESSURE, not level — this simulator has no
        drum level instrument, and drawing one would be inventing it.
      </text>
    </svg>
  )
}
