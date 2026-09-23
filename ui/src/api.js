// Talking to the CRPMS API.
//
// One rule runs through all of it: a value that is not Good arrives as null
// with its StatusCode and its reason. Nothing here fills that in, defaults it,
// or drops the point so a line can close over it.
//
// Every request carries the principal's bearer token (§509). The token lives in
// sessionStorage: it is gone when the tab closes, and it is never put in a URL.

const TOKEN_KEY = 'crpms.token'
export const SUBPROTOCOL = 'crpms.bearer'

export const getToken = () => {
  try { return sessionStorage.getItem(TOKEN_KEY) } catch { return null }
}
export const setToken = (token) => {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
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
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    // A browser cannot set an Authorization header on a WebSocket; the token
    // travels as the second offered subprotocol, never in the URL.
    socket = new WebSocket(`${proto}://${location.host}/ws/events`,
                           [SUBPROTOCOL, getToken() ?? ''])
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
