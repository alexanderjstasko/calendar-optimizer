import { useEffect, useState } from 'preact/hooks'
import { createApi } from './review/api.js'
import { formatClock, formatCost, formatDay, formatTimeRange, plural, unescapeHtml } from './review/format.js'
import { KindBadge, LoadState } from './review/ui.jsx'

// A tapped free band on the calendar (app.jsx onClickDateTime): what could
// go into that slot. Scouted public events that fit it come first
// (recommended ones, then key-word matches), then open tasks and flexible
// goals that fit its length and rules (the plan's own picks first, then by
// deadline and priority). No model call. Tapping Add books it at once
// (POST /slot/place): the person chose it, so it is approved. Two filters
// narrow the list: Location (Any / Home / Out, from each option's `where`)
// and Type (All / Events / Goals / Tasks); both are remembered per device.

const hoursText = (h) => (h >= 1 ? `${Number.isInteger(h) ? h : h.toFixed(1)} h` : `${Math.round(h * 60)} min`)

// A naive New York wall-clock string, which the API reads as display-zone time.
const wallClock = (iso) => iso.slice(0, 19)

function addHours(isoStart, hours) {
  const ms = Date.parse(`${wallClock(isoStart)}Z`) + Math.round(hours * 60) * 60 * 1000
  return new Date(ms).toISOString().slice(0, 19)
}

const FILTER_KEY = 'slotSheet.filters'
function remembered(name, allowed, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}')[name]
    return allowed.includes(v) ? v : fallback
  } catch {
    return fallback
  }
}
function remember(name, value) {
  try {
    const all = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}')
    localStorage.setItem(FILTER_KEY, JSON.stringify({ ...all, [name]: value }))
  } catch {
    // private mode or blocked storage: the filter just isn't remembered
  }
}

function Segmented({ label, value, options, onChange }) {
  return (
    <div class="slot-filter">
      <span class="slot-filter-label">{label}</span>
      <div class="segmented slot-filter-seg" role="group" aria-label={label}>
        {options.map((o) => (
          <button
            type="button"
            key={o.value}
            class={value === o.value ? 'is-active' : ''}
            aria-pressed={value === o.value}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  )
}

export function SlotSheet({ apiUrl, slot, onClose, onPlaced }) {
  const [api] = useState(() => createApi(apiUrl))
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(null) // the id being booked
  const [note, setNote] = useState('')
  const [attempt, setAttempt] = useState(0) // bumped by Try again to refetch
  // Filters, kept between taps: where it happens and what kind of thing it is.
  const [where, setWhere] = useState(() => remembered('where', ['any', 'home', 'out'], 'any'))
  const [kind, setKind] = useState(() => remembered('kind', ['all', 'event', 'goal', 'task'], 'all'))
  const pickWhere = (v) => (setWhere(v), remember('where', v))
  const pickKind = (v) => (setKind(v), remember('kind', v))

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError('')
    setNote('')
    api
      .slotOptions(slot.start, slot.end)
      .then((d) => !cancelled && setData(d))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [api, slot.start, slot.end, attempt])

  async function book(pick, start, end) {
    setBusy(pick.id)
    setNote('')
    try {
      const out = await api.slotPlace({ kind: pick.kind, id: pick.id, start: wallClock(start), end: wallClock(end) })
      onPlaced?.(out)
      onClose()
    } catch (err) {
      setNote(err.message || "Couldn't add it.")
    } finally {
      setBusy(null)
    }
  }

  const allEvents = data?.events || []
  const allItems = data?.items || []
  const fits = (x) => where === 'any' || (x.where || 'out') === where
  const events = kind === 'all' || kind === 'event' ? allEvents.filter(fits) : []
  const items = allItems.filter((it) => (kind === 'all' || it.kind === kind) && fits(it))
  const nothing = data && !allEvents.length && !allItems.length
  const filteredOut = data && !nothing && !events.length && !items.length
  const kinds = [
    { value: 'all', label: 'All' },
    ...[
      { value: 'event', label: 'Events', n: allEvents.length },
      { value: 'goal', label: 'Goals', n: allItems.filter((it) => it.kind === 'goal').length },
      { value: 'task', label: 'Tasks', n: allItems.filter((it) => it.kind === 'task').length },
    ].filter((k) => k.n > 0),
  ]

  return (
    <div class="slot-sheet" role="dialog" aria-label="Free slot" onClick={(e) => e.stopPropagation()}>
      <div class="ev-sheet-handle" aria-hidden="true" />
      <div class="slot-sheet-head">
        <div>
          <div class="slot-sheet-kind">Free slot</div>
          <h2 class="slot-sheet-title">
            {formatDay(slot.start)} · {formatTimeRange({ start: slot.start, end: slot.end })}
          </h2>
        </div>
        <button type="button" class="ev-sheet-close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>

      <div class="slot-sheet-body">
        {data && !nothing && (
          <div class="slot-filters">
            <Segmented
              label="Location"
              value={where}
              onChange={pickWhere}
              options={[
                { value: 'any', label: 'Any' },
                { value: 'home', label: 'Home' },
                { value: 'out', label: 'Out' },
              ]}
            />
            {kinds.length > 2 && <Segmented label="Type" value={kind} onChange={pickKind} options={kinds} />}
          </div>
        )}
        {!data && <LoadState error={error} what="the options" onRetry={() => setAttempt((n) => n + 1)} rows={3} />}
        {nothing && <p class="rv-hint slot-sheet-empty">Nothing in the pool fits this slot. Enjoy the free time.</p>}
        {filteredOut && <p class="rv-hint slot-sheet-empty">Nothing matches these filters.</p>}

        {events.length > 0 && (
          <section class="slot-sheet-section" aria-label="Events">
            <h3 class="rv-section-title">{plural(events.length, 'event')}</h3>
            <ul class="slot-list">
              {events.map((e) => {
                const cost = formatCost(e.cost)
                return (
                  <li key={e.id} class="slot-row">
                    <div class="slot-row-main">
                      <div class="slot-row-top">
                        <span class="slot-row-title">
                          {e.url ? (
                            <a href={e.url} target="_blank" rel="noreferrer">
                              {unescapeHtml(e.title)}
                            </a>
                          ) : (
                            unescapeHtml(e.title)
                          )}
                        </span>
                        {e.recommended && <span class="slot-tag slot-tag-rec">picked for you</span>}
                        {e.keyword_match && <span class="slot-tag slot-tag-key">key word</span>}
                      </div>
                      <div class="slot-row-meta">
                        {formatTimeRange({ start: e.start, end: e.end })}
                        {e.location && ` · ${unescapeHtml(e.location)}`}
                        {cost && ` · ${cost}`}
                      </div>
                    </div>
                    <button
                      type="button"
                      class="rv-btn rv-btn-primary slot-add"
                      disabled={busy !== null}
                      onClick={() => book(e, e.start, e.end)}
                    >
                      {busy === e.id ? '…' : 'Add'}
                    </button>
                  </li>
                )
              })}
            </ul>
          </section>
        )}

        {items.length > 0 && (
          <section class="slot-sheet-section" aria-label="Tasks and goals">
            <h3 class="rv-section-title">Tasks & goals</h3>
            <ul class="slot-list">
              {items.map((it) => {
                const end = addHours(slot.start, it.duration_hr)
                return (
                  <li key={`${it.kind}:${it.id}`} class="slot-row">
                    <div class="slot-row-main">
                      <div class="slot-row-top">
                        <span class="slot-row-title">{unescapeHtml(it.title)}</span>
                        <KindBadge type={it.kind} />
                        {it.recommended && <span class="slot-tag slot-tag-rec">in the plan</span>}
                      </div>
                      <div class="slot-row-meta">
                        {hoursText(it.duration_hr)} from {formatClock(slot.start)}
                        {it.deadline && ` · due ${formatDay(it.deadline)}`}
                        {it.priority && ` · ${it.priority}`}
                        {it.frequency && ` · ${it.frequency}`}
                      </div>
                    </div>
                    <button
                      type="button"
                      class="rv-btn rv-btn-primary slot-add"
                      disabled={busy !== null}
                      onClick={() => book(it, slot.start, end)}
                    >
                      {busy === it.id ? '…' : 'Add'}
                    </button>
                  </li>
                )
              })}
            </ul>
          </section>
        )}
        {note && (
          <p class="ev-sheet-error" role="alert">
            {note}
          </p>
        )}
      </div>
    </div>
  )
}
