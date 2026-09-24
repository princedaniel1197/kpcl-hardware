// Quality and provenance, decoded for display.
//
// Quality is the numeric OPC UA StatusCode and stays the stored truth; this
// only puts a name and a meaning next to it. A value that is not Good is never
// shown as a number it does not have: Bad is "—" with its reason, Uncertain is
// the number with an amber chip and its reason.

import { STATUS_CODES } from './statusCodes'

export const klassOf = (code) => {
  if (code === null || code === undefined) return null
  return ['Good', 'Uncertain', 'Bad', 'Reserved'][(Number(code) >>> 30) & 3]
}

// The full code first; failing that, the code with its low 16 bits (the info
// bits) cleared, which is how the table is keyed.
export function statusOf(code) {
  if (code === null || code === undefined) return null
  const exact = STATUS_CODES[String(code)]
  const base = STATUS_CODES[String((Number(code) >>> 16) << 16 >>> 0)]
  const [name, doc] = exact ?? base ?? [`0x${(Number(code) >>> 0).toString(16).toUpperCase()}`, '']
  return { name, doc, hex: `0x${(Number(code) >>> 0).toString(16).toUpperCase().padStart(8, '0')}` }
}

export const toneOf = (klass) =>
  klass === 'Bad' ? 'danger' : klass === 'Uncertain' ? 'warning' : klass === 'Good' ? 'success' : 'muted'

/** The reason a value is not Good, in words. `detail` is the engine's health
 *  detail for the tag (a standing quality note, e.g. "uncalibrated - bench
 *  demo only", comes first in it). */
export function reasonFor(code, detail) {
  const klass = klassOf(code)
  if (klass === 'Good' || klass === null) return detail || null
  const s = statusOf(code)
  const parts = []
  if (detail) parts.push(detail)
  parts.push(s.doc ? `${s.name} — ${s.doc}` : s.name)
  return parts.join(' · ')
}

// ── provenance ─────────────────────────────────────────────────────────────
// Sentinel's honesty surface, with CRPMS's meanings:
//   REAL       measured by real hardware or software: the bench rig's sensors,
//              and the collector measuring itself.
//   SYNTHETIC  produced by the DCS simulator, the load-test generator or the
//              OMF demonstration. No real plant is behind it.
// CALIBRATED is Sentinel's third kind; nothing in CRPMS is a real parameter
// shaping synthetic data, so it is not used.
export function provenanceOf(tag) {
  const name = tag?.name ?? tag ?? ''
  const system = tag?.source_system
  if (name.startsWith('RIG_')) return { p: 'REAL', source: 'bench rig' }
  if (system === 'collector' || name.startsWith('COLLECTOR_')) return { p: 'REAL', source: 'collector' }
  if (system === 'loadtest') return { p: 'SYNTHETIC', source: 'load test' }
  if (system === 'omf-demo') return { p: 'SYNTHETIC', source: 'OMF demo' }
  return { p: 'SYNTHETIC', source: 'DCS simulator' }
}

/** A KPI inherits the provenance of its inputs: REAL only if every input tag is. */
export function kpiProvenance(inputTags) {
  if (!inputTags || inputTags.length === 0) return { p: 'SYNTHETIC', source: 'derived' }
  const real = inputTags.every((t) => provenanceOf(t).p === 'REAL')
  return real ? { p: 'REAL', source: 'from bench rig' } : { p: 'SYNTHETIC', source: 'from simulator' }
}
