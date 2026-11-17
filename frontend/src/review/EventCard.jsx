import { useState } from 'preact/hooks'
import { formatCost, formatDay, formatTimeRange, unescapeHtml } from './format.js'
import { Chevron, CloseIcon, Switch } from './ui.jsx'

// The event's own description. The backend sends the discovered event's
// summary; older servers only have the proposal's description, which joins
// summary | cost | url, so the cost and link parts are dropped from it.
function descriptionOf(p) {
  if (p.summary !== undefined) return unescapeHtml(p.summary || '')
  const parts = (p.description || '').split(' | ')
  const drop = new Set([p.cost, p.url].filter(Boolean))
  return unescapeHtml(parts.filter((x) => x && !drop.has(x) && !/^https?:\/\//.test(x)).join(' | '))
}

// The scout's rationale reads "<why> Fit: <slot and travel>"; shown as two lines.
function reasonsOf(p) {
  const text = unescapeHtml(p.rationale || '')
  const at = text.lastIndexOf(' Fit:')
  if (at < 0) return { why: text.replace(/^Fit:\s*/, ''), fit: '' }
  return { why: text.slice(0, at).trim(), fit: text.slice(at + 5).trim() }
}

function mapLinks(place, venue) {
  if (place?.lat == null) {
    return { embed: null, open: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(venue)}` }
  }
  const { lat, lon } = place
  const d = 0.006
  const bbox = [lon - d, lat - d * 0.75, lon + d, lat + d * 0.75].map((n) => n.toFixed(5)).join(',')
  return {
    embed: `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${lat},${lon}`,
    open: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(venue || `${lat},${lon}`)}`,
  }
}

/**
 * One suggested public event: when, title (links to the event's page), the
 * neighbourhood (tap for a mini map with a pin) · cost, and the event's own
 * description. "Why" opens the scout's reasons and "Not interested" (a
 * rejection the scout learns from). The toggle approves it onto the
 * calendar. Events happen at their listed time, so there is no time editor.
 */
export function EventCard({ p, status, busy, onApprove, onRevoke, onReject }) {
  const [more, setMore] = useState(false)
  const [whyOpen, setWhyOpen] = useState(false)
  const [mapOpen, setMapOpen] = useState(false)
  const on = status === 'promoted'
  const title = unescapeHtml(p.title)
  const venue = unescapeHtml(p.location || '')
  const area = p.place?.neighborhood || ''
  const cost = formatCost(p.cost)
  const description = descriptionOf(p)
  const { why, fit } = reasonsOf(p)
  const map = venue || p.place ? mapLinks(p.place, venue) : null
  const hasWhy = why || fit || !on

  return (
    <article class={`rv-card rv-event${on ? ' is-on' : ''}${busy ? ' is-busy' : ''}`}>
      <div class="rv-event-top">
        <p class="rv-event-when">
          <span class="rv-event-day">{formatDay(p.start)}</span>
          <span class="rv-dot" aria-hidden="true">·</span>
          <span>{formatTimeRange(p)}</span>
        </p>
        <Switch
          checked={on}
          busy={busy}
          label={on ? `Going to ${title}. Switch off to take it off the calendar` : `Add ${title} to the calendar`}
          onChange={(next) => (next ? onApprove() : onRevoke())}
        />
      </div>
      <h3 class="rv-event-title">
        {p.url ? (
          <a href={p.url} target="_blank" rel="noreferrer">
            {title}
          </a>
        ) : (
          title
        )}
      </h3>
      {(map || cost) && (
        <p class="rv-event-meta">
          {map && (
            <button
              type="button"
              class={`rv-event-place${mapOpen ? ' is-open' : ''}`}
              aria-expanded={mapOpen ? 'true' : 'false'}
              title={venue}
              onClick={() => setMapOpen(!mapOpen)}
            >
              <PinIcon />
              {area || venue}
            </button>
          )}
          {map && cost && (
            <span class="rv-dot" aria-hidden="true">
              ·
            </span>
          )}
          {cost && <span class={cost === 'Free' ? 'rv-free' : ''}>{cost}</span>}
        </p>
      )}
      {mapOpen && map && (
        <div class="rv-event-map">
          {map.embed ? (
            <iframe src={map.embed} title={`Map of ${venue || area}`} loading="lazy" referrerpolicy="no-referrer" />
          ) : (
            <p class="rv-hint rv-event-map-none">No pin for this venue yet.</p>
          )}
          <div class="rv-event-map-foot">
            <span class="rv-event-map-venue">{venue}</span>
            <a class="rv-link" href={map.open} target="_blank" rel="noreferrer">
              Open in Maps ↗
            </a>
          </div>
        </div>
      )}
      {description && (
        <button
          type="button"
          class={`rv-event-desc${more ? ' is-open' : ''}`}
          aria-expanded={more ? 'true' : 'false'}
          onClick={() => setMore(!more)}
        >
          {description}
        </button>
      )}
      <div class="rv-event-actions">
        {p.url ? (
          <a class="rv-link" href={p.url} target="_blank" rel="noreferrer">
            Event page ↗
          </a>
        ) : (
          <span />
        )}
        {hasWhy && (
          <button
            type="button"
            class={`rv-btn rv-btn-quiet rv-event-why-toggle${whyOpen ? ' is-open' : ''}`}
            aria-expanded={whyOpen ? 'true' : 'false'}
            onClick={() => setWhyOpen(!whyOpen)}
          >
            Why
            <Chevron open={whyOpen} />
          </button>
        )}
      </div>
      {whyOpen && (
        <div class="rv-event-why">
          {why && <p>{why}</p>}
          {fit && <p class="rv-event-fit">{fit}</p>}
          {!why && !fit && <p class="rv-hint">The scout gave no reason for this one.</p>}
          {!on && (
            <button type="button" class="rv-btn rv-btn-quiet rv-btn-dismiss" disabled={busy} onClick={onReject}>
              <CloseIcon />
              Not interested
            </button>
          )}
        </div>
      )}
    </article>
  )
}

const PinIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6">
    <path d="M8 14.5s4.5-4.2 4.5-8a4.5 4.5 0 1 0-9 0c0 3.8 4.5 8 4.5 8z" />
    <circle cx="8" cy="6.5" r="1.6" />
  </svg>
)
