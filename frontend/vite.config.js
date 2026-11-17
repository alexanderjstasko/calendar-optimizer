import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import preact from '@preact/preset-vite'

const SW_SOURCE = fileURLToPath(new URL('./src/sw.js', import.meta.url))
// Copied verbatim from public/ (not in the bundle), so listed by hand.
const PUBLIC_FILES = ['/manifest.webmanifest', '/favicon.png', '/apple-touch-icon.png', '/icon-192.png', '/icon-512.png']

/**
 * Emits dist/sw.js from src/sw.js with the built file list and a version
 * (hash of the worker source plus that list) filled in, so a deploy that
 * changes anything installs a new worker. Build only: the dev server has no
 * bundle, and src/offline.js registers the worker only in production.
 */
function serviceWorker() {
  return {
    name: 'calendar-service-worker',
    apply: 'build',
    generateBundle(_options, bundle) {
      const built = Object.keys(bundle)
        .filter((file) => file !== 'index.html' && file !== 'sw.js')
        .map((file) => `/${file}`)
      const precache = ['/', ...built, ...PUBLIC_FILES]
      const source = readFileSync(SW_SOURCE, 'utf8')
      const version = createHash('sha256').update(source).update(precache.join('\n')).digest('hex').slice(0, 12)
      this.emitFile({
        type: 'asset',
        fileName: 'sw.js',
        source: source.replaceAll('__PRECACHE__', JSON.stringify(precache)).replaceAll("'__VERSION__'", JSON.stringify(version)),
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // TAILNET in the root .env (e.g. "example-tailnet.ts.net") lets the dev
  // server answer every MagicDNS name on the tailnet; empty = localhost only.
  const env = loadEnv(mode, '../', '')
  const tailnet = (env.TAILNET || '').trim().replace(/^\.+|\.+$/g, '')
  return {
    // TIMEZONE in the root .env (an IANA name), the same value the backend reads;
    // baked in at build time, so rebuild after changing it.
    define: { __APP_TIMEZONE__: JSON.stringify((env.TIMEZONE || '').trim() || 'America/New_York') },
    plugins: [preact(), serviceWorker()],
    envDir: '../', // .env lives in the project root, one level up from frontend/
    server: {
      allowedHosts: tailnet ? [`.${tailnet}`] : [],
    },
  }
})
