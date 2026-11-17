// The review tabs' only door to the backend. Routes (see backend/main.py):
//   GET  /review/summary                       both tabs' data in one call
//   POST /optimizer/proposals/batch            {action: approve|reject|revoke, ids}
//   POST /optimizer/proposals/{id}/approve     optional body {start, end} = approve at that time
//   POST /optimizer/proposals/{id}/reject|revoke
//   POST /scout/reload                         next page of event options (queue or model)
//   POST /optimizer/run                        re-plan (a model call, minutes)
//   POST /scout/fetch, GET /scout/status       the Events tab's overflow menu
//   GET/PUT /hours                             the Hours tab (sleep, work, home, planning settings)
//   GET  /slot/options, POST /slot/place       a tapped free slot: what fits, book the pick

import { noteResponse, noteUnreachable, unreachableMessage } from '../offline.js'

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

// FastAPI errors are {detail: "text"} or, for validation, {detail: [{msg, loc}]}.
function describeDetail(detail) {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d?.msg || JSON.stringify(d)).join('; ')
  return JSON.stringify(detail)
}

export function createApi(apiUrl = '') {
  async function request(path, { method = 'GET', body } = {}) {
    const init = { method }
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(body)
    }
    let response
    try {
      response = await fetch(`${apiUrl}${path}`, init)
    } catch {
      if (method === 'GET') noteUnreachable()
      throw new ApiError(unreachableMessage({ writing: method !== 'GET' }), 0)
    }
    // Reads tell the offline banner where they came from (see offline.js).
    if (method === 'GET' && noteResponse(response)) throw new ApiError(`${unreachableMessage()} Nothing is saved to show yet.`, 503)
    if (!response.ok) {
      let detail = ''
      try {
        detail = describeDetail((await response.json()).detail)
      } catch {
        /* not JSON; fall back to the status */
      }
      throw new ApiError(detail || `Server returned ${response.status}`, response.status)
    }
    return response.status === 204 ? null : response.json()
  }

  const post = (path, body) => request(path, { method: 'POST', body })
  const one = (id) => `/optimizer/proposals/${encodeURIComponent(id)}`

  return {
    summary: () => request('/review/summary'),
    hours: () => request('/hours'),
    saveHours: (hours) => request('/hours', { method: 'PUT', body: hours }),
    recentRuns: (limit = 6) => request(`/optimizer/runs?limit=${limit}`),
    batch: (action, ids) => post('/optimizer/proposals/batch', { action, ids }),
    approve: (id, times) => post(`${one(id)}/approve`, times),
    reject: (id) => post(`${one(id)}/reject`),
    revoke: (id) => post(`${one(id)}/revoke`),
    reload: () => post('/scout/reload'),
    replan: () => post('/optimizer/run'),
    fetchSites: () => post('/scout/fetch'),
    scoutStatus: () => request('/scout/status'),
    // A tapped free slot on the calendar (slot.py).
    slotOptions: (start, end) => request(`/slot/options?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    slotPlace: (pick) => post('/slot/place', pick),
  }
}

/**
 * Run `task(id)` for every id, a few at a time (SQLite serialises writes
 * anyway; this only overlaps the network round trips). Never throws.
 */
export async function forEachLimited(ids, task, limit = 3) {
  const done = []
  const failed = []
  let next = 0
  async function worker() {
    while (next < ids.length) {
      const id = ids[next++]
      try {
        done.push({ id, result: await task(id) })
      } catch (err) {
        failed.push({ id, detail: err.message })
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, ids.length) }, worker))
  return { done, failed }
}

/**
 * Take approvals back. The batch route accepts "revoke" (series toggles off);
 * if a server ever refuses it (422), fall back to one revoke per id.
 */
export async function revokeMany(api, ids) {
  try {
    return await api.batch('revoke', ids)
  } catch (err) {
    if (err.status !== 422) throw err
    return forEachLimited(ids, (id) => api.revoke(id))
  }
}

// A run row says 'running' while its model call is in flight. A crashed
// process can leave one behind, so a run older than this counts as finished.
const STALE_RUN_MS = 20 * 60 * 1000

/**
 * The newest run of each kind that is still going, from GET /optimizer/runs
 * (newest first). The summary's `runs` can't tell: it reports the latest
 * *succeeded* run, the one whose proposals are under review.
 */
export function runningRuns(runs, now = Date.now()) {
  const latest = { optimizer: null, scout: null }
  for (const run of runs || []) {
    const kind = run.kind || 'optimizer'
    if (kind in latest && !latest[kind]) latest[kind] = run
  }
  return {
    optimizer: runningFor(latest.optimizer, now) !== null ? latest.optimizer : null,
    scout: runningFor(latest.scout, now) !== null ? latest.scout : null,
  }
}

/** Milliseconds since a run started if it is still running, else null. */
export function runningFor(run, now = Date.now()) {
  if (!run || run.status !== 'running' || !run.started_at) return null
  const elapsed = now - new Date(run.started_at).getTime()
  return elapsed >= 0 && elapsed < STALE_RUN_MS ? elapsed : null
}
