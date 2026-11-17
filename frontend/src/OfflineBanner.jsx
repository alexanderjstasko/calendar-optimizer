import { useOffline } from './offline.js'
import { TZ as ZONE } from './review/format.js'

// Thin strip under the header while the app shows saved data (see offline.js).

const DAY = new Intl.DateTimeFormat('en-CA', { timeZone: ZONE })
const TIME = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZone: ZONE })
const DATE_TIME = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: ZONE })

function describe(cachedAt) {
  if (!cachedAt) return 'Nothing saved to show yet.'
  const today = DAY.format(new Date()) === DAY.format(cachedAt)
  return `Showing what was saved ${today ? `at ${TIME.format(cachedAt)}` : `on ${DATE_TIME.format(cachedAt)}`}.`
}

export function OfflineBanner({ onRetry, busy }) {
  const state = useOffline()
  if (!state) return null
  return (
    <p class="offline-banner" role="status">
      <span>
        <strong>Offline.</strong> {describe(state.cachedAt)}
      </span>
      <button type="button" class="offline-retry" onClick={onRetry} disabled={busy}>
        Retry
      </button>
    </p>
  )
}
