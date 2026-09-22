// The data path, as nodes, with real values moving along it.
//
// DCS -> Collector -> Buffer -> Network -> Historian -> KPI Engine -> Dashboard
//
// EVERY PARTICLE IS AN EVENT. Nothing here is on a timer: a dot appears because
// the collector emitted `value_received`, and it reaches the historian because
// the collector emitted `value_forwarded`. When the archive is unreachable the
// events say `value_buffered` instead, so the dots stop at the buffer and the
// buffer fills — not because the animation was told the network is down, but
// because that is what the collector actually reported doing.
//
// A Bad value is drawn differently in transit and is SEEN to be rejected at the
// KPI node, with its reason, rather than passing through.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactFlow, { Background, Handle, Position, useEdgesState, useNodesState } from 'reactflow'
import 'reactflow/dist/style.css'
import { isa, mono, sans, qualityColour } from './theme'

const NODE_W = 150
const NODE_H = 76
const ROW_Y = 120

function Box({ data }) {
  const alarm = data.alarm
  return (
    <div style={{
      width: NODE_W, height: NODE_H, borderRadius: 3,
      border: `1px solid ${alarm ? isa.bad : isa.lineStrong}`,
      background: alarm ? '#f7e9e8' : isa.panel,
      color: isa.text, fontFamily: sans, padding: '6px 8px',
      boxSizing: 'border-box', display: 'flex', flexDirection: 'column',
      justifyContent: 'space-between',
    }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontSize: 11, letterSpacing: 0.3, color: isa.textDim,
                    textTransform: 'uppercase' }}>{data.label}</div>
      <div style={{ fontFamily: mono, fontSize: 17, color: alarm ? isa.bad : isa.value }}>
        {data.primary}
      </div>
      <div style={{ fontSize: 10, color: alarm ? isa.bad : isa.textDim,
                    whiteSpace: 'nowrap', overflow: 'hidden',
                    textOverflow: 'ellipsis' }} title={data.secondary}>
        {data.secondary}
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = { box: Box }

const LAYOUT = [
  ['dcs', 'DCS', 20],
  ['collector', 'Collector', 215],
  ['buffer', 'Buffer', 410],
  ['network', 'Network', 605],
  ['historian', 'Historian', 800],
  ['kpi', 'KPI Engine', 995],
  ['dashboard', 'Dashboard', 1190],
]
const CANVAS_W = LAYOUT[LAYOUT.length - 1][2] + NODE_W + 20
const CANVAS_H = 300

// Particles travel along segment i, from node i to node i+1.
const SEGMENTS = LAYOUT.length - 1
const TRANSIT_MS = 1100

export default function Pipeline({ events, status, kpis }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges] = useEdgesState([])
  const [particles, setParticles] = useState([])
  const [stats, setStats] = useState({
    received: 0, forwarded: 0, buffered: 0, drained: 0, bad: 0,
    bufferDepth: 0, linkUp: true, lastReason: null, lastDrain: null,
  })
  const nextId = useRef(0)

  // --- turn events into particles and counters -----------------------------
  useEffect(() => {
    if (!events.length) return
    const latest = events[events.length - 1]
    setStats((s) => {
      const n = { ...s }
      if (latest.kind === 'value_received') {
        n.received += 1
        if (((latest.quality >>> 30) & 3) === 2) n.bad += 1
      }
      if (latest.kind === 'value_forwarded') n.forwarded += latest.count ?? 1
      if (latest.kind === 'value_buffered') {
        n.buffered += latest.count ?? 1
        n.bufferDepth = latest.depth ?? n.bufferDepth
        n.linkUp = false
      }
      if (latest.kind === 'link_state') {
        n.linkUp = latest.up
        n.lastReason = latest.detail ?? null
      }
      if (latest.kind === 'buffer_drained') {
        n.drained += latest.replayed ?? 0
        n.bufferDepth = latest.remaining ?? 0
        n.lastDrain = latest
      }
      if (latest.kind === 'buffer_drain_started') n.bufferDepth = latest.depth ?? 0
      return n
    })

    if (latest.kind === 'value_received') {
      const severity = ((latest.quality >>> 30) & 3)
      const id = nextId.current++
      // A received value travels DCS -> Collector always. How much further it
      // gets is decided by what the collector reports next, not by this code.
      setParticles((p) => [...p.slice(-80), {
        id, born: performance.now(), tag: latest.tag,
        quality: latest.quality, severity,
        // Bad values are stopped and shown at the KPI node; everything else
        // runs the whole path.
        stop: severity === 2 ? 5 : SEGMENTS,
        buffered: false,
      }])
    }
    if (latest.kind === 'value_buffered') {
      // Anything still in flight past the buffer turns back: this is the
      // moment the picture shows the uplink gone.
      setParticles((p) => p.map((x) => ({ ...x, stop: Math.min(x.stop, 2), buffered: true })))
    }
  }, [events])

  // --- animation -----------------------------------------------------------
  const [, force] = useState(0)
  useEffect(() => {
    let raf
    const tick = () => { force((n) => n + 1); raf = requestAnimationFrame(tick) }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])

  useEffect(() => {
    const t = setInterval(() => {
      const now = performance.now()
      setParticles((p) => p.filter((x) => now - x.born < TRANSIT_MS * SEGMENTS + 2500))
    }, 2000)
    return () => clearInterval(t)
  }, [])

  // --- nodes ---------------------------------------------------------------
  const badKpis = useMemo(
    () => kpis.filter((k) => k.quality_class === 'Bad' || k.quality_class === 'Uncertain'),
    [kpis])

  useEffect(() => {
    const collector = status?.collectors?.find((c) => c.is_leader)
      ?? status?.collectors?.[0]
    const detail = {
      dcs: ['OPC UA', `${stats.received.toLocaleString()} values seen`],
      collector: [collector?.instance ?? '—',
                  `${(collector?.samples ?? 0).toLocaleString()} acquired`],
      buffer: [`${stats.bufferDepth.toLocaleString()}`,
               stats.bufferDepth > 0 ? 'holding — uplink down' : 'empty'],
      network: [stats.linkUp ? 'UP' : 'DOWN',
                stats.linkUp ? 'archive reachable' : (stats.lastReason ?? 'archive unreachable')],
      historian: [`${stats.forwarded.toLocaleString()}`, 'rows forwarded'],
      kpi: [badKpis.length ? `${badKpis.length} not Good` : 'all Good',
            badKpis[0]?.reason ?? 'inputs Good'],
      dashboard: [`${stats.bad.toLocaleString()}`, 'Bad values seen'],
    }
    setNodes(LAYOUT.map(([id, label, x]) => ({
      id, type: 'box', position: { x, y: ROW_Y },
      data: {
        label,
        primary: detail[id][0],
        secondary: detail[id][1],
        alarm: (id === 'network' && !stats.linkUp)
            || (id === 'buffer' && stats.bufferDepth > 0)
            || (id === 'kpi' && badKpis.length > 0),
      },
      draggable: false,
    })))
    setEdges(LAYOUT.slice(0, -1).map(([id], i) => ({
      id: `e${i}`, source: id, target: LAYOUT[i + 1][0],
      type: 'straight',
      style: {
        stroke: (i === 2 && !stats.linkUp) ? isa.bad : isa.line,
        strokeWidth: 1.5,
        strokeDasharray: (i === 2 && !stats.linkUp) ? '5 4' : undefined,
      },
      animated: false,
    })))
  }, [stats, status, badKpis, setNodes, setEdges])

  // --- particle overlay ----------------------------------------------------
  const now = performance.now()
  const dots = particles.map((p) => {
    const elapsed = now - p.born
    const progress = Math.min(elapsed / TRANSIT_MS, p.stop)
    const segment = Math.min(Math.floor(progress), SEGMENTS - 1)
    const within = progress - segment
    const x0 = LAYOUT[segment][2] + NODE_W
    const x1 = LAYOUT[segment + 1][2]
    const x = x0 + (x1 - x0) * Math.min(within, 1)
    const held = progress >= p.stop
    return { ...p, x, y: ROW_Y + NODE_H / 2, held }
  })

  return (
    <div style={{ position: 'relative', width: CANVAS_W, height: CANVAS_H,
                  background: isa.background, border: `1px solid ${isa.line}`,
                  transformOrigin: 'top left' }}>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
                 onNodesChange={onNodesChange}
                 defaultViewport={{ x: 0, y: 0, zoom: 1 }}
                 proOptions={{ hideAttribution: true }}
                 nodesDraggable={false} nodesConnectable={false}
                 zoomOnScroll={false} panOnDrag={false} zoomOnDoubleClick={false}
                 preventScrolling={false}>
        <Background color={isa.line} gap={22} size={1} />
      </ReactFlow>

      {/* The overlay shares React Flow's coordinate space exactly: the flow is
          pinned at zoom 1 with no fitView, so a node at x=410 is at 410px here
          too. Letting fitView transform the nodes while the overlay kept its
          own viewBox put every particle above the diagram instead of on the
          edges. */}
      <svg width={CANVAS_W} height={CANVAS_H}
           style={{ position: 'absolute', left: 0, top: 0, pointerEvents: 'none' }}>
        {dots.map((d) => (
          <g key={d.id}>
            {/* Bad values are square and red; Good ones are small grey dots.
                Shape as well as colour, so the difference survives a
                colour-blind viewer and a projector. */}
            {d.severity === 2 ? (
              <rect x={d.x - 4} y={d.y - 4} width={8} height={8}
                    fill={isa.bad} opacity={d.held ? 1 : 0.9}>
                {d.held && <animate attributeName="opacity" values="1;0.35;1"
                                    dur="0.9s" repeatCount="indefinite" />}
              </rect>
            ) : d.severity === 1 ? (
              <circle cx={d.x} cy={d.y} r={4} fill={isa.uncertain} />
            ) : (
              <circle cx={d.x} cy={d.y} r={3}
                      fill={d.buffered ? isa.uncertain : isa.lineStrong} />
            )}
          </g>
        ))}
      </svg>

      <div style={{ position: 'absolute', left: 10, bottom: 8, fontFamily: mono,
                    fontSize: 11, color: isa.textDim }}>
        <span style={{ color: isa.lineStrong }}>●</span> Good&nbsp;&nbsp;
        <span style={{ color: isa.uncertain }}>●</span> Uncertain&nbsp;&nbsp;
        <span style={{ color: isa.bad }}>■</span> Bad — stopped at the KPI engine
        {stats.lastDrain && (
          <span style={{ marginLeft: 16, color: isa.info }}>
            last drain: {stats.lastDrain.replayed} samples in {stats.lastDrain.seconds}s
          </span>
        )}
      </div>
    </div>
  )
}
