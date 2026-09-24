// A small path router. Sentinel keeps the station and period in the query
// string and carries them across every navigation; so does this.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

const Ctx = createContext(null)
const SCOPE_KEYS = ['station', 'period']

function read() {
  return { path: window.location.pathname, query: new URLSearchParams(window.location.search) }
}

export function RouterProvider({ children }) {
  const [loc, setLoc] = useState(read)
  useEffect(() => {
    const on = () => setLoc(read())
    window.addEventListener('popstate', on)
    return () => window.removeEventListener('popstate', on)
  }, [])

  const navigate = useCallback((to) => {
    window.history.pushState(null, '', to)
    setLoc(read())
    window.scrollTo(0, 0)
  }, [])

  // The scope suffix every internal link carries.
  const scope = useMemo(() => {
    const p = new URLSearchParams()
    for (const k of SCOPE_KEYS) if (loc.query.get(k)) p.set(k, loc.query.get(k))
    const s = p.toString()
    return s ? `?${s}` : ''
  }, [loc])

  const setScope = useCallback((key, value) => {
    const p = new URLSearchParams(window.location.search)
    if (!value || value === 'ALL') p.delete(key); else p.set(key, value)
    const s = p.toString()
    navigate(window.location.pathname + (s ? `?${s}` : ''))
  }, [navigate])

  const value = useMemo(() => ({ ...loc, navigate, scope, setScope }), [loc, navigate, scope, setScope])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useRouter = () => useContext(Ctx)

/** Internal link that keeps the station/period scope, like Sentinel's. */
export function Link({ to, children, className = '', style, title, onClick }) {
  const { navigate, scope } = useRouter()
  const href = to.includes('?') ? to : to + scope
  return (
    <a href={href} className={className} style={style} title={title}
       onClick={(e) => {
         if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
         e.preventDefault()
         onClick?.()
         navigate(href)
       }}>
      {children}
    </a>
  )
}

/** Match `pattern` ("/tags/:name") against the current path. */
export function match(pattern, path) {
  const a = pattern.split('/').filter(Boolean)
  const b = path.split('/').filter(Boolean)
  if (a.length !== b.length) return null
  const params = {}
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith(':')) params[a[i].slice(1)] = decodeURIComponent(b[i])
    else if (a[i] !== b[i]) return null
  }
  return params
}
