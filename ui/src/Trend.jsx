// A trend that shows the gap.
//
// The whole argument of this project shows up in one rendering decision: a
// sample that arrived Bad carries no value, and the line must BREAK there
// rather than joining its neighbours. Recharts draws a continuous line through
// nulls unless told otherwise — `connectNulls={false}` is the entire
// difference between a chart that tells the truth and one that quietly
// invents a straight line across a sensor failure.
//
// Bad points are also marked, so a break is distinguishable from "no data yet".

import { useEffect, useMemo, useState } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceDot, ResponsiveContainer,
         Tooltip, XAxis, YAxis } from 'recharts'
import { getTrend } from './api'
import { isa, mono, sans } from './theme'

export default function Trend({ tag, minutes = 10, height = 220 }) {
  const [points, setPoints] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const data = await getTrend(tag, minutes)
        if (live) { setPoints(data.points); setError(null) }
      } catch (e) { if (live) setError(String(e)) }
    }
    load()
    const t = setInterval(load, 2500)     // §528 asks for a 2-3 s refresh
    return () => { live = false; clearInterval(t) }
  }, [tag, minutes])

  const series = useMemo(() => points.map((p) => ({
    t: new Date(p.source_ts).getTime(),
    label: new Date(p.source_ts).toLocaleTimeString(),
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
          <XAxis dataKey="label" tick={{ fontSize: 9, fill: isa.textDim }}
                 minTickGap={60} stroke={isa.line} />
          <YAxis tick={{ fontSize: 9, fill: isa.textDim }} width={54}
                 stroke={isa.line} domain={['auto', 'auto']} />
          <Tooltip contentStyle={{ fontSize: 11, fontFamily: mono,
                                   background: isa.panel, border: `1px solid ${isa.line}` }}
                   formatter={(value, _n, p) => [
                     value === null ? 'no value' : value,
                     p.payload.quality]} />
          {/* connectNulls MUST stay false. See the note at the top of this file. */}
          <Line type="monotone" dataKey="value" stroke={isa.lineStrong}
                strokeWidth={1.4} dot={false} isAnimationActive={false}
                connectNulls={false} />
          {bad.map((p, i) => (
            <ReferenceDot key={`b${i}`} x={p.label} y={0} r={0}
                          ifOverflow="extendDomain"
                          label={{ value: '×', position: 'insideBottom',
                                   fill: isa.bad, fontSize: 14 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
      {bad.length > 0 && (
        <div style={{ fontSize: 10, color: isa.bad, fontFamily: sans }}>
          × marks a sample that arrived Bad. The line breaks there because the
          sample carries no value — it is not zero, and it is not interpolated.
        </div>
      )}
    </div>
  )
}
