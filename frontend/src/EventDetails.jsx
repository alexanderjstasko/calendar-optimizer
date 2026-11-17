import { Component } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { TimeEditor } from './review/TimeEditor.jsx'
import { dayKey, formatTimeRange, formatWeekdayDate } from './review/format.js'

// A Temporal value as plain ISO 8601 (offset, no "[zone]" suffix): what the
// review formatters and POST /events/{id}/move both read.
export const isoOf = (value) =>
  value instanceof Temporal.PlainDate ? value.toString() : value.toString({ timeZoneName: 'never' })

// If the editor fails to render on some browser, say so in the sheet (and in
// the console) instead of leaving the Move button looking dead.
class EditorBoundary extends Component {
  state = { error: null }
  componentDidCatch(error) {
    console.error('Event editor failed:', error)
    this.setState({ error })
  }
  render() {
    if (this.state.error) {
      return (
        <p class="ev-sheet-error" role="alert">
          Couldn't open the editor: {String(this.state.error?.message || this.state.error)}
        </p>
      )
    }
    return this.props.children
  }
}

const icon = (d) => (
  <svg class="ev-sheet-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {d}
  </svg>
)
const ClockIcon = () => icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>)
const PinIcon = () => icon(<><path d="M12 21s-6-5.3-6-10a6 6 0 0 1 12 0c0 4.7-6 10-6 10z" /><circle cx="12" cy="11" r="2.2" /></>)
const NoteIcon = () => icon(<><path d="M5 7h14M5 12h14M5 17h9" /></>)
const CloseIcon = () => icon(<path d="M6 6l12 12M18 6L6 18" />)
const BackIcon = () => icon(<path d="M15 6l-6 6 6 6" />)
const ChevronIcon = () => icon(<path d="M9 6l6 6-6 6" />)

// A stack card's sheet: its blocks in time order; each opens its own details.
function StackList({ ev, ctx, rootRef, onPick }) {
  const start = isoOf(ev.start)
  const end = isoOf(ev.end)
  return (
    <div ref={rootRef} class="ev-sheet tone-fixed" onClick={(e) => e.stopPropagation()}>
      <div class="ev-sheet-handle" aria-hidden="true" />
      <div class="ev-sheet-head">
        <span class="ev-sheet-kind">{ev.stack.length} blocks</span>
        <button type="button" class="ev-sheet-close" aria-label="Close" onClick={() => ctx.current.close()}>
          <CloseIcon />
        </button>
      </div>
      <h2 class="ev-sheet-title">{formatTimeRange({ start, end })}</h2>
      <div class="ev-stack-date">{formatWeekdayDate(start)}</div>
      <ul class="ev-stack-list">
        {ev.stack.map((m) => (
          <li key={m.id}>
            <button type="button" class={`ev-stack-item tone-${kindOf(m).tone}`} onClick={() => onPick(m)}>
              <span class="ev-sheet-dot" aria-hidden="true" />
              <span class="ev-stack-time">{formatTimeRange({ start: m.realStart || isoOf(m.start), end: m.realEnd || isoOf(m.end) })}</span>
              <span class="ev-stack-title">{m.title}</span>
              <ChevronIcon />
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Where the event comes from, for the caption above the title.
function kindOf(ev) {
  if (ev.calendarId === 'optimizer') return { label: 'Planned block', tone: 'plan' }
  if (ev.calendarId === 'proposed') return { label: 'Proposed · not on your calendar yet', tone: 'proposed' }
  const source = String(ev.calendarId || '').split('_')[0]
  const names = { google: 'Google Calendar', apple: 'Apple Calendar', outlook: 'Outlook', partiful: 'Partiful' }
  return { label: names[source] || ev.calendarId || 'Calendar', tone: 'fixed' }
}

// Lines for the time row: the day, then the times (or the span for events
// that cross midnight or last several days).
function whenLines(ev, start, end) {
  if (ev.start instanceof Temporal.PlainDate) {
    const first = formatWeekdayDate(start)
    return [start === end ? first : `${first} – ${formatWeekdayDate(end)}`, 'All day']
  }
  if (dayKey(start) === dayKey(end)) return [formatWeekdayDate(start), formatTimeRange({ start, end })]
  return [`${formatWeekdayDate(start)} – ${formatWeekdayDate(end)}`, formatTimeRange({ start, end })]
}

/**
 * The event details sheet: Schedule-X's eventModal custom component (passed
 * from app.jsx), replacing the plugin's default content. It shows the real
 * times even when the grid draws a block clipped to the awake window
 * (realStart/realEnd/clipNote from fitToAwakeWindow in app.jsx), and lets
 * approved optimizer blocks be moved: "Move" opens the Plan tab's time editor
 * and saves through ctx.current.move (POST /events/{id}/move), then closes the
 * sheet. Synced events only show their details: the app never writes to
 * Google, Apple, Outlook or Partiful. ctx is a ref, so the component is
 * created once.
 */
export function makeEventDetails(ctx) {
  return function EventDetails({ calendarEvent }) {
    // A stack card (gridLayout.js) lists its blocks; picking one shows its details.
    const [picked, setPicked] = useState(null)
    const ev = picked || calendarEvent
    const [editing, setEditing] = useState(false)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState('')
    // Delete asks once: the first tap turns the button into "Really delete?" for a few seconds.
    const [confirming, setConfirming] = useState(false)
    const rootRef = useRef(null)
    useEffect(() => setPicked(null), [calendarEvent?.id])
    useEffect(() => {
      setEditing(false)
      setError('')
      setConfirming(false)
    }, [ev?.id])
    useEffect(() => {
      if (!confirming) return
      const t = setTimeout(() => setConfirming(false), 4000)
      return () => clearTimeout(t)
    }, [confirming])
    // Schedule-X places the popover (position: fixed, top from a CSS variable on
    // <html>) for the sheet's height at the moment it opened. Opening the editor
    // makes it taller, and for a block late in the day the Save button then sat
    // below the viewport: pull the popover up so the whole sheet stays on screen.
    useLayoutEffect(() => {
      const root = rootRef.current
      if (!root || window.innerWidth <= 640) return // phones: a bottom sheet with its own scroll
      const frame = requestAnimationFrame(() => {
        const rect = root.getBoundingClientRect()
        const margin = 12
        if (rect.bottom <= window.innerHeight - margin) return
        const top = Math.max(margin, window.innerHeight - margin - rect.height)
        document.documentElement.style.setProperty('--sx-event-modal-top', `${top}px`)
      })
      return () => cancelAnimationFrame(frame)
    }, [editing, error, confirming, ev?.id])
    if (!ev) return null
    if (ev.stack) return <StackList ev={ev} ctx={ctx} rootRef={rootRef} onPick={setPicked} />

    const start = ev.realStart || isoOf(ev.start)
    const end = ev.realEnd || isoOf(ev.end)
    const kind = kindOf(ev)
    const movable = ev.calendarId === 'optimizer'
    const [dayLine, timeLine] = whenLines(ev, start, end)

    async function save(times) {
      setBusy(true)
      setError('')
      try {
        await ctx.current.move(ev.id, times)
        ctx.current.close()
      } catch (err) {
        setError(err.message || "Couldn't move it.")
      } finally {
        setBusy(false)
      }
    }

    async function remove() {
      if (!confirming) {
        setConfirming(true)
        return
      }
      setBusy(true)
      setError('')
      try {
        await ctx.current.remove(ev.id)
        ctx.current.close()
      } catch (err) {
        setError(err.message || "Couldn't delete it.")
        setConfirming(false)
      } finally {
        setBusy(false)
      }
    }
    const deleteLabel = confirming ? 'Really delete?' : 'Delete'

    return (
      <div
        ref={rootRef}
        class={`ev-sheet tone-${kind.tone}${editing ? ' is-editing' : ''}`}
        // Schedule-X closes the modal on any document click whose target is not
        // inside it. In a real browser Preact re-renders between the button's
        // handler and that document listener (microtask checkpoint), so a
        // click that swaps the Move button for the editor arrives at the
        // document with a detached target and closed the sheet. Clicks inside
        // the sheet never need to reach the document.
        onClick={(e) => e.stopPropagation()}
      >
        <div class="ev-sheet-handle" aria-hidden="true" />
        <div class="ev-sheet-head">
          {picked ? (
            <button type="button" class="ev-sheet-back" onClick={() => setPicked(null)}>
              <BackIcon />
              {calendarEvent.stack.length} blocks
            </button>
          ) : (
            <span class="ev-sheet-kind">
              <span class="ev-sheet-dot" aria-hidden="true" />
              {kind.label}
            </span>
          )}
          <button type="button" class="ev-sheet-close" aria-label="Close" onClick={() => ctx.current.close()}>
            <CloseIcon />
          </button>
        </div>
        <h2 class="ev-sheet-title">{ev.title}</h2>

        <div class="ev-sheet-meta">
          <div class="ev-sheet-row">
            <ClockIcon />
            <div>
              <div class="ev-sheet-strong">{dayLine}</div>
              <div>{timeLine}</div>
              {ev.clipNote && <div class="ev-sheet-hint">{ev.clipNote}</div>}
              {ev.conflict && (
                <div class="ev-sheet-conflict" role="status">
                  Conflicts with {ev.conflict.title || 'a calendar event'}. A move is waiting for you in Plan.
                </div>
              )}
            </div>
          </div>
          {ev.location && (
            <div class="ev-sheet-row">
              <PinIcon />
              <div>{ev.location}</div>
            </div>
          )}
          {ev.description && (
            <div class="ev-sheet-row">
              <NoteIcon />
              <div class="ev-sheet-desc">{ev.description}</div>
            </div>
          )}
        </div>

        {movable && editing && (
          <div class="ev-sheet-editor">
            <div class="ev-sheet-editor-title">Move to</div>
            <EditorBoundary>
              <TimeEditor
                p={{ start, end }}
                busy={busy}
                submitLabel="Save"
                onSubmit={save}
                onCancel={() => setEditing(false)}
                onDelete={remove}
                deleteLabel={deleteLabel}
              />
            </EditorBoundary>
          </div>
        )}
        {movable && !editing && (
          <div class="ev-sheet-actions">
            <button type="button" class="rv-btn rv-btn-quiet rv-btn-danger ev-sheet-delete" disabled={busy} onClick={remove}>
              {deleteLabel}
            </button>
            <button type="button" class="rv-btn rv-btn-secondary ev-sheet-move" disabled={busy} onClick={() => setEditing(true)}>
              Move
            </button>
          </div>
        )}
        {error && (
          <p class="ev-sheet-error" role="alert">
            {error}
          </p>
        )}
      </div>
    )
  }
}
