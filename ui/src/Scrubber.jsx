// Time scrubber: replay a window from the archive at any speed.
//
// It replays what the ARCHIVE holds, not a recording of the animation. That
// distinction matters: the outage and the recovery can be replayed because the
// samples are there with their original source timestamps and quality, which is
// the claim the whole system is making. If the scrubber replayed a captured
// animation it would prove nothing at all.

import { useEffect, useMemo, useRef, useState } from 'react'
import { getReplay } from './api'
import { isa, mono, sans, qualityColour } from './theme'

const SPEEDS = [0.5, 1, 2, 5, 20]

export default function Scrubber({ tags }) {
  const [window, setWindow] = useState(15)       // minutes back
  const [data, setData] = useState(null)
  const [cursor, setCursor] = useState(0)        // 0..1
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(5)
  const [loading, setLoading] = useState(false)
  const raf = useRef(null)

  const load = async () => {
    setLoading(true)
    try {
      const end = new Date()
      const start = new Date(end.getTime() - window * 60000)
      const replay = await getReplay(start.toISOString(), end.toISOString(), tags)
      setData(replay)
      // Start where the data starts, not at the window edge. "no data" at
      // cursor 0 is honest — nothing exists at that instant — but it is a poor
      // first impression of a replay that has 4,000 samples in it.
      const first = Object.values(replay.series)
        .flatMap((points) => (points.length ? [new Date(points[0].source_ts).getTime()] : []))
      if (first.length) {
        const span = new Date(replay.end).getTime() - new Date(replay.start).getTime()
        setCursor(Math.max(0, Math.min(1,
          (Math.min(...first) - new Date(replay.start).getTime()) / span)))
      } else {
        setCursor(0)
      }
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [window])

  useEffect(() => {
    if (!playing || !data) return
    let last = performance.now()
    const tick = (now) => {
      const dt = (now - last) / 1000
      last = now
      setCursor((c) => {
        const next = c + (dt * speed) / (window * 60)
        if (next >= 1) { setPlaying(false); return 1 }
        return next
      })
      raf.current = requestAnimationFrame(tick)
    }
    raf.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf.current)
  }, [playing, speed, window, data])

  const frame = useMemo(() => {
    if (!data) return {}
    const start = new Date(data.start).getTime()
    const end = new Date(data.end).getTime()
    const at = start + (end - start) * cursor
    const out = {}
    for (const [tag, points] of Object.entries(data.series)) {
      let chosen = null
      for (const p of points) {
        if (new Date(p.source_ts).getTime() <= at) chosen = p
        else break
      }
      // A tag with nothing at or before the cursor is shown as "no data",
      // not as its eventual first value.
      out[tag] = chosen
    }
    return { at: new Date(at), values: out }
  }, [data, cursor])

  return (
    <div style={{ border: `1px solid ${isa.line}`, background: isa.panel,
                  padding: 10, fontFamily: sans }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center',
                    flexWrap: 'wrap', marginBottom: 8 }}>
        <button onClick={() => setPlaying((p) => !p)}
                style={{ fontFamily: mono, minWidth: 64 }}>
          {playing ? '❚❚ pause' : '▶ play'}
        </button>
        <span style={{ fontSize: 11, color: isa.textDim }}>speed</span>
        {SPEEDS.map((s) => (
          <button key={s} onClick={() => setSpeed(s)}
                  style={{ fontFamily: mono, fontWeight: s === speed ? 700 : 400 }}>
            {s}×
          </button>
        ))}
        <span style={{ fontSize: 11, color: isa.textDim, marginLeft: 8 }}>window</span>
        {[5, 15, 60].map((m) => (
          <button key={m} onClick={() => setWindow(m)}
                  style={{ fontFamily: mono, fontWeight: m === window ? 700 : 400 }}>
            {m}m
          </button>
        ))}
        <button onClick={load} style={{ fontFamily: mono }}>reload</button>
        <span style={{ fontFamily: mono, fontSize: 12, marginLeft: 'auto' }}>
          {loading ? 'loading…' : frame.at?.toLocaleTimeString() ?? '—'}
        </span>
      </div>

      <input type="range" min="0" max="1" step="0.0005" value={cursor}
             onChange={(e) => { setPlaying(false); setCursor(Number(e.target.value)) }}
             style={{ width: '100%' }} />

      <div style={{ display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
                    gap: 8, marginTop: 8 }}>
        {tags.map((tag) => {
          const p = frame.values?.[tag]
          const klass = p?.quality_class ?? 'none'
          return (
            <div key={tag} style={{ border: `1px solid ${isa.line}`,
                                    padding: '4px 6px', background: isa.background }}>
              <div style={{ fontSize: 9, color: isa.textDim, fontFamily: mono }}>{tag}</div>
              <div style={{ fontFamily: mono, fontSize: 15,
                            color: klass === 'Good' ? isa.value : qualityColour(klass) }}>
                {p == null ? 'no data'
                  : p.value == null ? '- - -'
                  : Number(p.value).toFixed(2)}
              </div>
              <div style={{ fontSize: 9, color: qualityColour(klass) }}>
                {p?.quality_class ?? ''}
              </div>
            </div>
          )
        })}
      </div>
      {data && (
        <div style={{ fontSize: 10, color: isa.textDim, marginTop: 6 }}>
          Replaying the archive — {Object.values(data.counts).reduce((a, b) => a + b, 0).toLocaleString()} samples
          with their original source timestamps and quality. Not a recording of
          the animation.
        </div>
      )}
    </div>
  )
}
