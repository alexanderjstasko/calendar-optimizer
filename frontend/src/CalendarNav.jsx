import { ChevronDown, ChevronLeft, ChevronRight, SyncIcon } from './icons.jsx'

// One slim navigation bar for the calendar, replacing Schedule-X's own header.
// The split day view shows two Schedule-X calendars side by side, and each
// would otherwise draw its own header; this bar drives both.
//
// The children are deliberately flat: CSS `order` lays them out as one row on
// wider screens ([Today][<][>] date ... [Preview][Day|Week|Month][sync]) and as
// two rows on phones ([<] date [>] ... [Today][sync] / [Day|Week|Month] ... [Preview]).

const VIEW_BUTTONS = [
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
]

const STEP_NAMES = { day: 'day', week: 'week', month: 'month' }

function label(view, date, today, compact) {
  const sameYear = date.year === today.year
  if (view === 'day') {
    return date.toLocaleString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      ...(compact && sameYear ? {} : { year: 'numeric' }),
    })
  }
  if (view === 'week') {
    const monday = date.subtract({ days: date.dayOfWeek - 1 })
    const sunday = monday.add({ days: 6 })
    const from = monday.toLocaleString('en-US', { month: 'short', day: 'numeric' })
    const to = sunday.toLocaleString('en-US', monday.month === sunday.month ? { day: 'numeric' } : { month: 'short', day: 'numeric' })
    return compact && sunday.year === today.year ? `${from} – ${to}` : `${from} – ${to}, ${sunday.year}`
  }
  return date.toLocaleString('en-US', { month: 'long', year: 'numeric' })
}

/**
 * view: 'day' | 'week' | 'month'; date/today: Temporal.PlainDate.
 * onDate(PlainDate) moves the calendar; onView(view) switches view.
 * compact: phone layout (shorter date label).
 * syncing/onSync: the refresh button. preview: {on, count, disabled}; onPreview(on).
 */
export function CalendarNav({ view, date, today, onDate, onView, compact, syncing, onSync, preview, onPreview }) {
  const step = view === 'day' ? { days: 1 } : view === 'week' ? { weeks: 1 } : { months: 1 }
  const unit = STEP_NAMES[view] || 'day'
  const count = preview?.count
  return (
    <nav class="calendar-nav" aria-label="Calendar navigation">
      <button type="button" class="btn cnav-today" onClick={() => onDate(today)} disabled={date.equals(today)}>
        Today
      </button>
      <button type="button" class="icon-btn cnav-prev" aria-label={`Previous ${unit}`} onClick={() => onDate(date.subtract(step))}>
        <ChevronLeft />
      </button>
      <button type="button" class="icon-btn cnav-next" aria-label={`Next ${unit}`} onClick={() => onDate(date.add(step))}>
        <ChevronRight />
      </button>
      {/* The date text doubles as the date picker: the native input sits invisibly
          on top of it, so tapping the date opens the picker on phones and desktops. */}
      <label class="cnav-date">
        <span class="cnav-date-text">{label(view, date, today, compact)}</span>
        <ChevronDown size={16} />
        <input
          type="date"
          aria-label="Go to date"
          value={date.toString()}
          onChange={(e) => e.currentTarget.value && onDate(Temporal.PlainDate.from(e.currentTarget.value))}
        />
      </label>
      <span class="cnav-break" aria-hidden="true" />
      {preview && (
        <button
          type="button"
          class={`preview-toggle${preview.on ? ' is-on' : ''}`}
          aria-pressed={preview.on}
          disabled={preview.disabled}
          onClick={() => onPreview(!preview.on)}
          title="Show every pending proposal on the calendar, as if you approved them all"
        >
          <span class="preview-label">
            Preview<span class="preview-label-long"> plan</span>
          </span>
          {count > 0 && <span class="preview-count">{count}</span>}
        </button>
      )}
      <div class="segmented cnav-views" role="group" aria-label="View">
        {VIEW_BUTTONS.map((b) => (
          <button
            type="button"
            key={b.key}
            class={view === b.key ? 'is-active' : ''}
            aria-pressed={view === b.key}
            onClick={() => onView(b.key)}
          >
            {b.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        class={`icon-btn cnav-sync${syncing ? ' is-spinning' : ''}`}
        aria-label={syncing ? 'Syncing…' : 'Sync now'}
        title="Sync now"
        onClick={onSync}
        disabled={syncing}
      >
        <SyncIcon size={19} />
      </button>
    </nav>
  )
}
