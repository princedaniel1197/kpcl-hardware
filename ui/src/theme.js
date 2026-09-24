// Colours for the components drawn in JavaScript -- the pipeline canvas, the
// unit mimic, the trends and the replay scrubber -- in Sentinel's ivory-ledger
// inks (src/styles/sentinel.css holds the same values as CSS variables).
//
// The rule is the one ANSI/ISA-101 high-performance HMI practice and the
// ledger share: the base is quiet, and COLOUR IS RESERVED FOR STATE. Red is
// Bad, amber is Uncertain, green is asserted-good; nothing is coloured for
// decoration, so anything coloured is the thing to look at.
//
// Four-level display hierarchy (§469):
//   1 Plant overview   2 Unit overview   3 Unit detail (TSI)   4 Diagnostic
export const isa = {
  background: '#F5F1E8',   // paper
  panel: '#FBF9F3',        // panel
  panelDark: '#EFE9DA',    // wash
  line: '#CBB97F',         // hairline
  lineStrong: '#7A7260',   // muted
  text: '#2A2418',         // ink
  textDim: '#7A7260',      // muted
  faint: '#A39B87',
  gold: '#C9A84C',
  // Process values are ink, not coloured. They are normal.
  value: '#2A2418',
  bad: '#8C3B2E',          // danger
  uncertain: '#A9762B',    // warning
  good: '#5B6E3A',         // success
  running: '#5C6B7A',      // info: equipment energised
  info: '#5C6B7A',
  badWash: 'rgba(140, 59, 46, 0.12)',
  uncertainWash: 'rgba(169, 118, 43, 0.12)',
}

export const qualityColour = (klass) => ({
  Good: isa.value,
  Uncertain: isa.uncertain,
  Bad: isa.bad,
}[klass] ?? isa.textDim)

// Sentinel has no monospace face. Numbers, tags and timestamps are DM Sans at
// tabular figures; the name is kept so existing components need no change.
export const mono = "'DM Sans', system-ui, sans-serif"
export const sans = "'DM Sans', system-ui, sans-serif"
export const display = "'Cormorant Garamond', Georgia, serif"
