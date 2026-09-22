// ANSI/ISA-101.01-2015 high-performance HMI palette.
//
// The governing idea: the base display is grey and low-saturation, and COLOUR
// IS RESERVED FOR ABNORMAL STATES. On a conventional colourful mimic an alarm
// competes with the decoration; on this one, anything coloured is the only
// thing coloured, and the eye goes straight to it.
//
// Four-level display hierarchy (§469):
//   1 Plant overview   2 Unit overview   3 Unit detail (TSI)   4 Diagnostic
export const isa = {
  // Level 1-2 base: greys only.
  background: '#f0f0ef',
  panel: '#e4e4e2',
  panelDark: '#d6d6d3',
  line: '#a8a8a4',
  lineStrong: '#6f6f6b',
  text: '#26262a',
  textDim: '#65656a',
  // Process values are dark grey, not coloured. They are normal.
  value: '#1c1c1f',
  // Colour, used sparingly and only for states that need action.
  bad: '#b3261e',        // Bad quality, alarm
  uncertain: '#9a6700',  // Uncertain quality, warning
  good: '#2f6f3e',       // used only where "confirmed good" must be asserted
  running: '#3b5f8a',    // equipment energised
  info: '#4a4a8a',
}

export const qualityColour = (klass) => ({
  Good: isa.value,
  Uncertain: isa.uncertain,
  Bad: isa.bad,
}[klass] ?? isa.textDim)

export const mono = "ui-monospace, SFMono-Regular, Menlo, monospace"
export const sans = "system-ui, -apple-system, 'Segoe UI', sans-serif"
