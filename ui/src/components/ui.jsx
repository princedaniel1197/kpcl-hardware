// Page furniture, ported from Sentinel's components/ui.tsx (sentinel-v2) with
// the same markup and classes, plus the three things CRPMS adds: a quality
// chip, a value cell that obeys the quality rules, and a time cell.

import { Link } from '../lib/router'
import { klassOf, reasonFor, statusOf, toneOf } from '../lib/quality'
import { num, time, utc } from '../lib/format'

// ── provenance ─────────────────────────────────────────────────────────────
const PROV = {
  REAL: { label: 'Real', cls: 'chip-success',
          title: 'Measured: by the bench rig\'s sensors, or by the collector measuring itself.' },
  CALIBRATED: { label: 'Calibrated', cls: 'chip-gold',
                title: 'A real published parameter shaping a synthetic instance.' },
  SYNTHETIC: { label: 'Synthetic', cls: 'chip-muted',
               title: 'Produced by the DCS simulator or a demonstration generator. No real plant is behind this value.' },
}

export function ProvenanceChip({ p, source, className = '' }) {
  const c = PROV[p] ?? PROV.SYNTHETIC
  return (
    <span className={`chip ${c.cls} ${className}`} title={source ? `${c.title} Source: ${source}.` : c.title}>
      {c.label}{source && <span style={{ fontWeight: 400, opacity: 0.85 }}>· {source}</span>}
    </span>
  )
}

export function Chip({ tone, children, title }) {
  return <span className={`chip chip-${tone}`} title={title}>{children}</span>
}

/** Good / Uncertain / Bad, from the numeric StatusCode, with the code's name. */
export function QualityChip({ code, klass: given }) {
  const klass = given ?? klassOf(code)
  if (!klass) return <Chip tone="muted">No data</Chip>
  const s = code === null || code === undefined ? null : statusOf(code)
  return (
    <Chip tone={toneOf(klass)} title={s ? `${s.name} (${s.hex}, ${code})${s.doc ? ` — ${s.doc}` : ''}` : klass}>
      {klass}
    </Chip>
  )
}

/** The value as the rules require: number + unit when Good; number + amber
 *  chip + reason when Uncertain; "—" + reason when Bad. Never a stale or zero
 *  number for a value that does not exist. */
export function Value({ sample, unit, digits, detail, big = false }) {
  if (!sample) {
    return <span className="no-value">— <span className="reason">no sample</span></span>
  }
  const klass = sample.quality_class ?? klassOf(sample.quality)
  const reason = reasonFor(sample.quality, detail)
  const numberCls = big ? 'hero-num text-[24px]' : 'tnum font-semibold'
  if (klass === 'Bad' || sample.value === null || sample.value === undefined) {
    return (
      <div>
        <span className={`${numberCls} no-value`}>—</span>
        {reason && <div className="reason reason-danger mt-0.5">{reason}</div>}
      </div>
    )
  }
  const shown = typeof sample.value === 'boolean'
    ? (sample.value ? 'true' : 'false')
    : num(sample.value, digits)
  return (
    <div>
      <span className={numberCls} style={klass === 'Uncertain' ? { color: 'var(--warning)' } : undefined}>
        {shown}
      </span>
      {unit && <span className="text-[12px] text-[var(--muted)] ml-1">{unit}</span>}
      {klass !== 'Good' && reason && (
        <div className={`reason ${klass === 'Uncertain' ? 'reason-warning' : ''} mt-0.5`}>{reason}</div>
      )}
      {klass === 'Good' && detail && <div className="reason mt-0.5">{detail}</div>}
    </div>
  )
}

export function Time({ iso, date = false, ms = false }) {
  if (!iso) return <span className="text-[var(--faint)]">—</span>
  return <span className="tnum text-[var(--muted)] whitespace-nowrap" title={`UTC ${utc(iso)}`}>{time(iso, { date, ms })}</span>
}

// ── page furniture ─────────────────────────────────────────────────────────
export function PageHeader({ title, subtitle, provenance, right, kicker, ident = false }) {
  return (
    <div className="rule-master-top pt-4 mb-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {kicker && <div className="text-[11px] uppercase tracking-[0.14em] text-[var(--faint)] mb-1">{kicker}</div>}
          <h1 className={`text-[26px] sm:text-[30px] leading-tight${ident ? ' ident' : ''}`}>{title}</h1>
          {subtitle && <p className="text-[13px] text-[var(--muted)] mt-1 max-w-[72ch]">{subtitle}</p>}
        </div>
        <div className="flex items-center gap-2 shrink-0 no-print">
          {provenance && (Array.isArray(provenance)
            ? provenance.map((p) => <ProvenanceChip key={p} p={p} />)
            : <ProvenanceChip p={provenance} />)}
          {right}
        </div>
      </div>
    </div>
  )
}

export function Section({ title, note, right, children, id }) {
  return (
    <section className="mt-8" id={id}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-2">
        <h2 className="text-[17px]">{title}</h2>
        {right}
      </div>
      {note && <p className="text-[12.5px] text-[var(--muted)] mb-3 max-w-[80ch]">{note}</p>}
      {children}
    </section>
  )
}

/** Cards are for KPI stat blocks only — never one card per ledger row. */
export function Kpi({ label, value, unit, sub, tone, href }) {
  const body = (
    <div className="panel px-3.5 py-3 h-full">
      <div className="text-[10.5px] uppercase tracking-[0.1em] text-[var(--muted)] leading-snug">{label}</div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span className="hero-num text-[24px]" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</span>
        {unit && <span className="text-[12px] text-[var(--muted)]">{unit}</span>}
      </div>
      {sub && <div className="text-[11.5px] text-[var(--muted)] mt-1 leading-snug">{sub}</div>}
    </div>
  )
  return href ? <Link to={href} className="block hover:opacity-90">{body}</Link> : body
}

export function Ledger({ children, className = '' }) {
  return <div className={`ledger-wrap ${className}`}><table className="ledger">{children}</table></div>
}

export function Empty({ children = 'Nothing in scope.' }) {
  return <div className="panel px-4 py-6 text-[13px] text-[var(--muted)] text-center">{children}</div>
}

export function Note({ children, tone = 'info' }) {
  const colour = tone === 'danger' ? 'var(--danger)' : tone === 'warning' ? 'var(--warning)' : 'var(--info)'
  return (
    <div className="panel px-3.5 py-3 text-[12.5px] leading-relaxed" style={{ borderLeft: `2px solid ${colour}` }}>
      {children}
    </div>
  )
}

/** The folio that closes every page: what the data is, and is not. */
export function Folio({ sources, cadence }) {
  return (
    <div className="folio">
      <div className="flex flex-wrap gap-x-6 gap-y-1 justify-between">
        <span>
          Demonstration data. Simulator tags are synthetic — no real DCS or plant is behind them. RIG_* tags are
          real measurements from bench hardware; RIG_CURRENT is uncalibrated (bench demo only).
        </span>
        <span className="shrink-0">CRPMS · monitoring overlay, read-only</span>
      </div>
      {(sources || cadence) && (
        <div className="mt-1.5">
          {sources && <>Sources: {sources}. </>}
          {cadence && <>Refresh: {cadence}.</>}
        </div>
      )}
    </div>
  )
}

/** A record that is not there: still a titled page with its folio. */
export function Missing({ title, children }) {
  return (
    <>
      <PageHeader title={title} />
      <Empty>{children}</Empty>
      <Folio />
    </>
  )
}

export function PrintBar({ label = 'Print this page' }) {
  return <button className="btn no-print" type="button" onClick={() => window.print()}>{label}</button>
}

/** A tag name that may break after its underscores on a narrow screen, as
 *  Sentinel's asset names break between words; unbroken where there is room. */
export function TagName({ name }) {
  const parts = name.split('_')
  return <>{parts.map((p, i) => <span key={i}>{p}{i < parts.length - 1 && <>_<wbr /></>}</span>)}</>
}

export function OpenLink({ to, children = 'Open →' }) {
  return <Link to={to} className="link whitespace-nowrap">{children}</Link>
}
