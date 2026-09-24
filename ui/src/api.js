// Talking to the CRPMS API.
//
// One rule runs through all of it: a value that is not Good arrives as null
// with its StatusCode and its reason. Nothing here fills that in, defaults it,
// or drops the point so a line can close over it.
//
// Every request carries the principal's bearer token (§509). The token lives in
// localStorage, so it survives closing the tab: signing in on every new tab was
// a nuisance on a demonstration laptop. It lasts until Sign out, or until the
// API refuses it (revoked or expired), which clears it. The cost: anyone using
// this browser profile can open the dashboard. It is never put in a URL.

const TOKEN_KEY = 'crpms.token'
export const SUBPROTOCOL = 'crpms.bearer'

export const getToken = () => {
  try {
    // A token signed in before 23 Sep 2026 was kept in sessionStorage; move it
    // rather than ask for it again.
    const earlier = sessionStorage.getItem(TOKEN_KEY)
    if (earlier) {
      sessionStorage.removeItem(TOKEN_KEY)
      if (!localStorage.getItem(TOKEN_KEY)) localStorage.setItem(TOKEN_KEY, earlier)
    }
    return localStorage.getItem(TOKEN_KEY)
  } catch { return null }
}
export const setToken = (token) => {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* storage unavailable: the token lasts as long as the page */ }
}

export class AuthError extends Error {}

const json = async (path) => {
  const token = getToken()
  const r = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (r.status === 401) throw new AuthError(`${path}: not signed in`)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}

export const getWhoami = () => json('/api/whoami')

export const getStatus = () => json('/api/status')
export const getTags = () => json('/api/tags')
export const getTrend = (tag, minutes = 10) =>
  json(`/api/trend/${encodeURIComponent(tag)}?minutes=${minutes}`)
// The newest archived sample of a tag within the last 24 hours (the API's
// maximum window), or none. The trend endpoint returns the newest `limit`
// points, so limit=1 is the latest value, with its quality and timestamps.
export const getLatest = async (tag) =>
  (await json(`/api/trend/${encodeURIComponent(tag)}?minutes=1440&limit=1`)).points[0] ?? null
export const getRecent = (tag, minutes, limit = 25) =>
  json(`/api/trend/${encodeURIComponent(tag)}?minutes=${minutes}&limit=${limit}`)
export const getAttributes = (assetCode) =>
  json(`/api/assets/${encodeURIComponent(assetCode)}/attributes`)
export const getAssets = () => json('/api/assets')
export const getAllEvents = (limit = 200) => json(`/api/events?limit=${limit}`)
export const getKpis = (assetCode) =>
  json(`/api/kpis${assetCode ? `?asset_code=${encodeURIComponent(assetCode)}` : ''}`)
export const getEvents = (assetCode) =>
  json(`/api/events${assetCode ? `?asset_code=${encodeURIComponent(assetCode)}` : ''}`)
export const getTagHealth = () => json('/api/health/tags')
export const getReplay = (start, end, tags) =>
  json(`/api/replay?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}` +
       `&tags=${encodeURIComponent(tags.join(','))}`)

// The live event stream. Reconnects on its own: losing the socket is losing
// the picture, and the picture should come back by itself.
export function connectEvents(onEvent, onState) {
  let socket = null
  let closed = false
  let retry = null

  const open = () => {
    const token = getToken()
    // No token, no socket. An empty subprotocol is not merely refused by the
    // server: the browser throws on it, which took the whole dashboard down
    // when the token disappeared mid-session. The polls meet the same missing
    // token as a 401 and sign the viewer out.
    if (!token) { onState?.('disconnected'); return }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    // A browser cannot set an Authorization header on a WebSocket; the token
    // travels as the second offered subprotocol, never in the URL.
    socket = new WebSocket(`${proto}://${location.host}/ws/events`,
                           [SUBPROTOCOL, token])
    socket.onopen = () => onState?.('connected')
    socket.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)) } catch { /* ignore malformed */ }
    }
    socket.onclose = () => {
      onState?.('disconnected')
      if (!closed) retry = setTimeout(open, 1500)
    }
    socket.onerror = () => socket?.close()
  }
  open()
  return () => { closed = true; clearTimeout(retry); socket?.close() }
}

// OPC UA severity lives in the top two bits of the StatusCode.
export const qualityClass = (code) => {
  if (code === null || code === undefined) return 'unknown'
  const severity = (code >>> 30) & 3
  return ['Good', 'Uncertain', 'Bad', 'Reserved'][severity]
}
