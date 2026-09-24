// The quality rules, as the screen renders them. Run with `npm test`.
//
// Would fail if: a Bad value rendered a number (stale or zero) instead of "—"
// and its reason; an Uncertain value lost its number, its amber, or its note;
// a StatusCode were decoded to the wrong name; or provenance were assigned
// against the rule (RIG_* real, simulator synthetic).
import assert from 'node:assert/strict'
import { renderToStaticMarkup } from 'react-dom/server'
import { Value, QualityChip, ProvenanceChip } from '../src/components/ui'
import { klassOf, provenanceOf, reasonFor, statusOf, kpiProvenance } from '../src/lib/quality'
import { num } from '../src/lib/format'

const html = (el) => renderToStaticMarkup(el)
const UNCERTAIN_CALIBRATION = 0x420A0000     // as the bridge publishes RIG_CURRENT
const BAD_NO_COMMUNICATION = 0x80310000
const BAD_DEVICE_FAILURE = 0x808B0000
let n = 0
const check = (name, fn) => { fn(); n += 1; console.log(`  ok  ${name}`) }

check('StatusCodes decode to their OPC UA names', () => {
  assert.equal(statusOf(UNCERTAIN_CALIBRATION).name, 'UncertainSensorCalibration')
  assert.equal(statusOf(BAD_NO_COMMUNICATION).name, 'BadNoCommunication')
  assert.equal(statusOf(BAD_DEVICE_FAILURE).name, 'BadDeviceFailure')
  assert.equal(statusOf(0).name, 'Good')
  assert.equal(klassOf(UNCERTAIN_CALIBRATION), 'Uncertain')
  assert.equal(klassOf(BAD_DEVICE_FAILURE), 'Bad')
  assert.equal(klassOf(null), null)
})

check('a Bad value shows "—" and its reason, never a number', () => {
  // Even when a (stale) number is present on the sample.
  const out = html(<Value sample={{ value: 29.3, quality: BAD_DEVICE_FAILURE }} unit="degC" />)
  assert.ok(out.includes('—'))
  assert.ok(!out.includes('29.3'), 'a Bad value must not show a number')
  assert.ok(!/>0</.test(out), 'a Bad value must not show zero')
  assert.ok(out.includes('BadDeviceFailure'))
  assert.ok(out.includes('reason-danger'))
})

check('an Uncertain value keeps its number, turns amber, and shows its note', () => {
  const note = 'uncalibrated - bench demo only'
  const out = html(<Value sample={{ value: 0.126, quality: UNCERTAIN_CALIBRATION }} unit="A" detail={note} />)
  assert.ok(out.includes('0.126'))
  assert.ok(out.includes('>A<'))
  assert.ok(out.includes('var(--warning)'))
  assert.ok(out.includes(note))
  assert.ok(out.includes('UncertainSensorCalibration'))
  const chip = html(<QualityChip code={UNCERTAIN_CALIBRATION} />)
  assert.ok(chip.includes('chip-warning') && chip.includes('Uncertain'))
})

check('a Good value shows number and unit, and no reason', () => {
  const out = html(<Value sample={{ value: 538.46, quality: 0 }} unit="degC" />)
  assert.ok(out.includes('538.5') && out.includes('degC'))
  assert.ok(!out.includes('reason'))
  assert.equal(reasonFor(0, null), null)
})

check('no sample is "—", not zero', () => {
  const out = html(<Value sample={null} unit="MW" />)
  assert.ok(out.includes('—') && out.includes('no sample'))
  assert.ok(html(<QualityChip klass={null} />).includes('No data'))
})

check('provenance: RIG_* real, simulator synthetic, KPIs inherit', () => {
  assert.equal(provenanceOf({ name: 'RIG_CURRENT', source_system: 'opcua' }).p, 'REAL')
  assert.equal(provenanceOf({ name: 'U1_MW', source_system: 'opcua' }).p, 'SYNTHETIC')
  assert.equal(provenanceOf({ name: 'LOADTEST_TAG_00', source_system: 'loadtest' }).p, 'SYNTHETIC')
  assert.equal(kpiProvenance(['RIG_HUB_TEMP', 'RIG_AMBIENT_TEMP']).p, 'REAL')
  assert.equal(kpiProvenance(['U1_COAL_FLOW', 'U1_MW']).p, 'SYNTHETIC')
  assert.ok(html(<ProvenanceChip p="REAL" />).includes('chip-success'))
})

check('numbers: en-IN grouping, whole numbers without decimals', () => {
  assert.equal(num(1), '1')
  assert.equal(num(23192), '23,192')
  assert.equal(num(null), '—')
})

console.log(`${n} checks passed`)
