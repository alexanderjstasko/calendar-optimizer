/* eslint-env serviceworker */
// Service worker: lets the home-screen app open offline and show the last
// data it saw. Emitted as dist/sw.js by the serviceWorker() plugin in
// vite.config.js, which fills in the PRECACHE list (the built app's files)
// and VERSION (a hash of this source plus that list) below, so every deploy
// installs a new worker and drops the old static cache.
//
//   Static files (/, /assets/*, icons, manifest): precached at install.
//     Hashed assets are served cache-first (they never change); a navigation
//     goes network-first so a deploy shows on the next open, with the saved
//     page as the offline fallback.
//   API reads the app makes (API_PATHS): network-first; every good response
//     is saved (stamped X-SW-Cached-At). Offline, or when the network takes
//     longer than NETWORK_TIMEOUT_MS and a copy exists, the copy is served
//     with that stamp, which src/offline.js turns into the "Offline" banner.
//     With no copy, a 503 marked X-SW-Offline comes back instead of hanging.
//   POST requests are never touched: a decision either reaches the server
//   or fails visibly in the app; nothing is queued.

const VERSION = '__VERSION__'
const PRECACHE = __PRECACHE__ // eslint-disable-line no-undef
const STATIC_CACHE = `static-${VERSION}`
const DATA_CACHE = 'data-v1'
const API_PATHS = ['/events', '/events/preview', '/review/summary', '/optimizer/runs', '/scout/status', '/schedule', '/preferences', '/hours', '/health']
const NETWORK_TIMEOUT_MS = 8000
const CACHED_AT = 'X-SW-Cached-At'
const OFFLINE = 'X-SW-Offline'

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE.map((url) => new Request(url, { cache: 'reload' }))))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== STATIC_CACHE && k !== DATA_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (request.mode === 'navigate') {
    event.respondWith(networkFirstPage(request))
  } else if (PRECACHE.includes(url.pathname)) {
    event.respondWith(cacheFirst(request))
  } else if (API_PATHS.includes(url.pathname)) {
    event.respondWith(networkFirstData(request))
  }
})

async function cacheFirst(request) {
  const cached = await caches.match(request, { cacheName: STATIC_CACHE })
  if (cached) return cached
  const response = await fetch(request)
  if (response.ok) (await caches.open(STATIC_CACHE)).put(request, response.clone())
  return response
}

// Every navigation is the one page (routes live in the hash), so the saved
// copy of "/" answers any of them offline.
async function networkFirstPage(request) {
  try {
    const response = await fetch(new Request(request, { cache: 'no-cache' }))
    if (response.ok) (await caches.open(STATIC_CACHE)).put('/', response.clone())
    return response
  } catch (err) {
    const cached = await caches.match('/', { cacheName: STATIC_CACHE })
    if (cached) return cached
    throw err
  }
}

async function networkFirstData(request) {
  const cache = await caches.open(DATA_CACHE)
  const cached = await cache.match(request)
  try {
    const response = await (cached ? withTimeout(fetch(request), NETWORK_TIMEOUT_MS) : fetch(request))
    if (response.ok) {
      const headers = new Headers(response.headers)
      headers.set(CACHED_AT, new Date().toISOString())
      const body = await response.clone().arrayBuffer()
      await cache.put(request, new Response(body, { status: response.status, statusText: response.statusText, headers }))
    }
    return response
  } catch {
    if (cached) return cached
    return new Response(JSON.stringify({ detail: 'offline, and nothing is saved for this yet' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json', [OFFLINE]: '1' },
    })
  }
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('network timeout')), ms)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (err) => {
        clearTimeout(timer)
        reject(err)
      },
    )
  })
}
