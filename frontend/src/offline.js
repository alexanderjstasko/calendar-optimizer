import { useEffect, useState } from 'preact/hooks'

// Is the app showing saved data? src/sw.js stamps every response it serves
// from its cache with X-SW-Cached-At, and answers 503 + X-SW-Offline when it
// has no copy. The fetch helpers (app.jsx getJson/postJson, review/api.js)
// report each response here; OfflineBanner.jsx shows the result via
// useOffline(). A plain store (no signal): one value, a set of listeners.
//   null                 live data
//   { cachedAt: Date }   the last read came from the worker's cache
//   { cachedAt: null }   the server is unreachable and nothing is saved
let state = null
const listeners = new Set()

function set(next) {
  const same = (next === null && state === null) || (next && state && +next.cachedAt === +state.cachedAt)
  if (same) return
  state = next
  listeners.forEach((fn) => fn(state))
}

export function offlineState() {
  return state
}

/** Record what a GET response was; returns true when it is the worker's "offline, no copy" answer. */
export function noteResponse(response) {
  if (response.headers.get('X-SW-Offline')) {
    set({ cachedAt: null })
    return true
  }
  const at = response.headers.get('X-SW-Cached-At')
  set(at ? { cachedAt: new Date(at) } : null)
  return false
}

/** A fetch that never got a response (no worker, or a POST while offline). */
export function noteUnreachable() {
  if (!state) set({ cachedAt: null })
}

export function unreachableMessage({ writing = false } = {}) {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    return writing ? "You're offline. Decisions need a connection; try again when you're back online." : "You're offline."
  }
  return "Couldn't reach the server. Check the connection and try again."
}

/** The current offline state, re-rendering the caller when it changes. */
export function useOffline() {
  const [value, setValue] = useState(state)
  useEffect(() => {
    listeners.add(setValue)
    setValue(state) // anything that changed between the first render and this effect
    return () => listeners.delete(setValue)
  }, [])
  return value
}

/** Production only: the Vite dev server has no sw.js (see vite.config.js). */
export function registerServiceWorker() {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => console.warn('Service worker not registered:', err))
  })
}
