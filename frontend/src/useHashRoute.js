import { useCallback, useEffect, useState } from 'preact/hooks'

// Hash routes for the app's tabs: #/calendar (default), #/events, #/plan, #/hours,
// each with optional query params, e.g. #/calendar?preview=1 (deep link from
// the approval email). Hash routing keeps it a single static page: FastAPI
// serves frontend/dist as-is and needs no fallback route.

export const TABS = ['calendar', 'events', 'plan', 'hours']

export function parseHash(hash) {
  const raw = (hash || '').replace(/^#\/?/, '')
  const q = raw.indexOf('?')
  const path = (q === -1 ? raw : raw.slice(0, q)).replace(/\/+$/, '')
  const params = Object.fromEntries(new URLSearchParams(q === -1 ? '' : raw.slice(q + 1)))
  return { tab: TABS.includes(path) ? path : 'calendar', params }
}

export function buildHash(tab, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
  ).toString()
  return `#/${tab}${query ? `?${query}` : ''}`
}

/**
 * Returns [route, navigate]. route = {tab, params}; navigate(tab, params,
 * {replace}) pushes a history entry (back/forward move between tabs), or with
 * replace: true rewrites the current one (e.g. to drop a consumed param).
 */
export function useHashRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash))

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback((tab, params = {}, { replace = false } = {}) => {
    const hash = buildHash(TABS.includes(tab) ? tab : 'calendar', params)
    if (hash === window.location.hash) return
    if (replace) {
      // replaceState doesn't fire hashchange, so update the route ourselves.
      window.history.replaceState(window.history.state, '', hash)
      setRoute(parseHash(hash))
    } else {
      window.location.hash = hash
    }
  }, [])

  return [route, navigate]
}
