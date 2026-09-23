// A trend that shows the gap.
//
// The whole argument of this project shows up in one rendering decision: a
// sample that arrived Bad carries no value, and the line must BREAK there
// rather than joining its neighbours. Recharts draws a continuous line through
// nulls unless told otherwise — `connectNulls={false}` is the entire
// difference between a chart that tells the truth and one that quietly
// invents a straight line across a sensor failure.
//
// Bad points are also marked, so a break is distinguishable from "no data yet"
// -- with a vertical line at the instant, never with a mark at some y value:
// an earlier version put the mark at y = 0 and stretched the axis to include
// zero, which is a picture of a zero reading.
//
// The time axis is TIME, not a list of labels. With a categorical axis the
// points are spaced evenly whatever the gap between them, so a minute with no
// data at all -- the archive unreachable -- vanished from the picture. On a
// time axis it is blank space at the right-hand edge, and it fills in from the
// left as the buffer drains.

import { useEffect, useMemo, useState } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer,
         Tooltip, XAxis, YAxis } from 'recharts'
import { getTrend } from './api'
import { isa, mono, sans } from './theme'

export default function Trend({ tag, minutes = 10, height = 220 }) {
  const [points, setPoints] = useState([])
  const [error, setError] = useState(null)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const data = await getTrend(tag, minutes)
        if (live) { setPoints(data.points); setError(null); setNow(Date.now()) }
      } catch (e) { if (live) setError(String(e)) }
    }
    load()
    const t = setInterval(load, 2500)     // §528 asks for a 2-3 s refresh
    return () => { live = false; clearInterval(t) }
  }, [tag, minutes])

  const series = useMemo(() => points.map((p) => ({
    t: new Date(p.source_ts).getTime(),
    value: p.value,                       // null when not Good: left as null
    quality: p.quality_class,
  })), [points])

  const bad = series.filter((p) => p.quality === 'Bad')
  const uncertain = series.filter((p) => p.quality === 'Uncertain')

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                    alignItems: 'baseline', marginBottom: 4 }}>
        <span style={{ fontFamily: mono, fontSize: 13, color: isa.text }}>{tag}</span>
        <span style={{ fontFamily: sans, fontSize: 10, color: isa.textDim }}>
          {series.length} points · last {minutes} min
          {bad.length > 0 && <span style={{ color: isa.bad }}> · {bad.length} Bad</span>}
          {uncertain.length > 0 && <span style={{ color: isa.uncertain }}> · {uncertain.length} Uncertain</span>}
        </span>
      </div>
      {error && <div style={{ color: isa.bad, fontSize: 11 }}>{error}</div>}
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={series} margin={{ top: 6, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={isa.line} strokeDasharray="2 3" />
          <XAxis dataKey="t" type="number" scale="time"
                 domain={[now - minutes * 60000, now]}
                 tickFormatter={(t) => new Date(t).toLocaleTimeString()}
                 tick={{ fontSize: 9, fill: isa.textDim }}
                 minTickGap={60} stroke={isa.line} />
          <YAxis tick={{ fontSize: 9, fill: isa.textDim }} width={54}
                 stroke={isa.line} domain={['auto', 'auto']} />
          <Tooltip contentStyle={{ fontSize: 11, fontFamily: mono,
                                   background: isa.panel, border: `1px solid ${isa.line}` }}
                   labelFormatter={(t) => new Date(t).toLocaleTimeString()}
                   formatter={(value, _n, p) => [
                     value === null ? 'no value' : value,
                     p.payload.quality]} />
          {/* connectNulls MUST stay false. See the note at the top of this file. */}
          <Line type="monotone" dataKey="value" stroke={isa.lineStrong}
                strokeWidth={1.4} dot={false} isAnimationActive={false}
                connectNulls={false} />
          {bad.map((p, i) => (
            <ReferenceLine key={`b${i}`} x={p.t} stroke={isa.bad}
                           strokeDasharray="2 2" ifOverflow="discard" />
          ))}
        </LineChart>
      </ResponsiveContainer>
      {bad.length > 0 && (
        <div style={{ fontSize: 10, color: isa.bad, fontFamily: sans }}>
          A red dashed line marks a sample that arrived Bad. The trend breaks
          there because the sample carries no value — it is not zero, and it is
          not interpolated.
        </div>
      )}
    </div>
  )
}
