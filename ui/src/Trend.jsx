// A trend that shows the gap, drawn in Sentinel's chart style (components/
// charts.tsx: hairline grid, muted 11 px ticks, panel tooltip).
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
// zero, which is a picture of a zero reading. Uncertain points keep their
// value and are marked amber.
//
// The time axis is TIME, not a list of labels. With a categorical axis the
// points are spaced evenly whatever the gap between them, so a minute with no
// data at all -- the archive unreachable -- vanished from the picture. On a
// time axis it is blank space at the right-hand edge, and it fills in from the
// left as the buffer drains.

import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine,
         ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from 'recharts'
import { getTrend } from './api'
import { isa } from './theme'
import { num } from './lib/format'

const axis = { stroke: isa.line, tick: { fill: isa.textDim, fontSize: 11 }, tickLine: false }
const tip = {
  contentStyle: { background: isa.panel, border: `0.5px solid ${isa.line}`, borderRadius: 2,
                  fontSize: 12, fontFamily: 'var(--font-sans)' },
  labelStyle: { color: isa.text, fontWeight: 600 }, itemStyle: { color: isa.text },
}
const clock = (t) => new Date(t).toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false })

export default function Trend({ tag, unit, minutes = 10, height = 220, title }) {
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
    value: p.value,                       // null when Bad: left as null
    quality: p.quality_class,
    uncertain: p.quality_class === 'Uncertain' ? p.value : null,
  })), [points])

  const bad = series.filter((p) => p.quality === 'Bad')
  const uncertain = series.filter((p) => p.quality === 'Uncertain')

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
        <span className="text-[13px] font-semibold">{title ?? tag}</span>
        <span className="text-[11px] text-[var(--muted)] tnum">
          {num(series.length, 0)} points · {minutes >= 60 ? `${minutes / 60} h` : `${minutes} min`}
          {bad.length > 0 && <span style={{ color: isa.bad }}> · {bad.length} Bad</span>}
          {uncertain.length > 0 && <span style={{ color: isa.uncertain }}> · {uncertain.length} Uncertain</span>}
        </span>
      </div>
      {error && <div className="reason reason-danger">{error}</div>}
      <div style={{ width: '100%', height }}>
        <ResponsiveContainer>
          <ComposedChart data={series} margin={{ top: 8, right: 14, bottom: 4, left: 4 }}>
            <CartesianGrid stroke={isa.line} strokeOpacity={0.45} vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={[now - minutes * 60000, now]}
                   tickFormatter={clock} minTickGap={60} {...axis} />
            <YAxis {...axis} width={58} domain={['auto', 'auto']}
                   label={unit ? { value: unit, angle: -90, position: 'insideLeft', fill: isa.textDim, fontSize: 11 } : undefined} />
            <Tooltip {...tip} labelFormatter={(t) => `${clock(t)} IST`}
                     formatter={(value, name, p) => name === 'uncertain' ? [null, null]
                       : [value === null ? 'no value (Bad)' : `${num(value)}${unit ? ` ${unit}` : ''}`, p.payload.quality]} />
            {/* connectNulls MUST stay false. See the note at the top of this file. */}
            <Line type="monotone" dataKey="value" stroke={isa.text} strokeWidth={1.8}
                  dot={false} isAnimationActive={false} connectNulls={false} />
            {uncertain.length > 0 && (
              <Scatter dataKey="uncertain" fill={isa.uncertain} shape="circle" isAnimationActive={false} />
            )}
            {bad.map((p, i) => (
              <ReferenceLine key={`b${i}`} x={p.t} stroke={isa.bad} strokeDasharray="4 3" ifOverflow="discard" />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {bad.length > 0 && (
        <div className="reason reason-danger mt-1">
          A dashed red line marks a sample that arrived Bad. The trend breaks there because the sample carries no
          value — it is not zero, and it is not interpolated.
        </div>
      )}
    </div>
  )
}

/** Sentinel's horizontal bar block, for counts. */
export function CountBars({ data, h }) {
  return (
    <div style={{ width: '100%', height: h ?? 26 * data.length + 50 }}>
      <ResponsiveContainer>
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 14, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={isa.line} strokeOpacity={0.45} vertical horizontal={false} />
          <XAxis type="number" allowDecimals={false} {...axis} />
          <YAxis type="category" dataKey="label" {...axis} width={120} interval={0} />
          <Tooltip {...tip} cursor={{ fill: isa.panelDark }} />
          <Bar dataKey="count" name="Tags" isAnimationActive={false} radius={[0, 1, 1, 0]}>
            {data.map((d) => <Cell key={d.label} fill={d.colour} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
