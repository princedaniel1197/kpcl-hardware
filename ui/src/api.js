// Talking to the CRPMS API.
//
// There is no sign-in: token access (§509) was removed from the API and this
// interface by decision on 24 Sep 2026.
//
// One rule runs through all of it: a value that is not Good arrives as null
// with its StatusCode and its reason. Nothing here fills that in, defaults it,
// or drops the point so a line can close over it.

// A token kept by the sign-in this interface used to have is of no further use;
// it is removed rather than left in the browser.
try {
  localStorage.removeItem('crpms.token')
  sessionStorage.removeItem('crpms.token')
} catch { /* storage unavailable: nothing is kept there either */ }

const json = async (path) => {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}

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
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${proto}://${location.host}/ws/events`)
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
