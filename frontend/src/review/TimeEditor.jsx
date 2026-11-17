import { useState } from 'preact/hooks'
import { zonedParts } from './format.js'

const toMinutes = (hhmm) => {
  const [h, m] = hhmm.split(':').map(Number)
  return h * 60 + m
}
const fromMinutes = (total) => `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
// An end of 00:00 is midnight at the end of the day (a block may end exactly then).
const endMinutes = (hhmm) => (hhmm === '00:00' ? 24 * 60 : toMinutes(hhmm))
const nextDay = (ymd) => new Date(Date.parse(`${ymd}T00:00:00Z`) + 86400000).toISOString().slice(0, 10)

/**
 * The pencil's one-line editor: date, start, end, then Approve / Cancel.
 * Submits naive New York times ({start, end} as YYYY-MM-DDTHH:MM:00), which
 * POST .../approve reschedules and then promotes. Moving the start keeps the
 * block's length, as calendar apps do.
 */
export function TimeEditor({ p, busy, onSubmit, onCancel, submitLabel = 'Approve', onDelete, deleteLabel = 'Delete' }) {
  const [date, setDate] = useState(() => zonedParts(p.start).date)
  const [start, setStart] = useState(() => zonedParts(p.start).time)
  const [end, setEnd] = useState(() => zonedParts(p.end).time)
  const [error, setError] = useState('')

  function changeStart(value) {
    const length = endMinutes(end) - toMinutes(start)
    setStart(value)
    if (value && length > 0 && toMinutes(value) + length <= 24 * 60) setEnd(fromMinutes((toMinutes(value) + length) % (24 * 60)))
    setError('')
  }

  function submit(event) {
    event.preventDefault()
    if (!date || !start || !end) return setError('Pick a date, a start and an end.')
    if (endMinutes(end) - toMinutes(start) <= 0) return setError('The end has to be after the start.')
    onSubmit({ start: `${date}T${start}:00`, end: `${end === '00:00' ? nextDay(date) : date}T${end}:00` })
  }

  return (
    <form class="rv-editor" onSubmit={submit}>
      <div class="rv-editor-fields">
        <label class="rv-field rv-field-date">
          <span>Date</span>
          <input type="date" value={date} required onInput={(e) => (setDate(e.currentTarget.value), setError(''))} />
        </label>
        <label class="rv-field">
          <span>Start</span>
          <input type="time" step="300" value={start} required onInput={(e) => changeStart(e.currentTarget.value)} />
        </label>
        <label class="rv-field">
          <span>End</span>
          <input type="time" step="300" value={end} required onInput={(e) => (setEnd(e.currentTarget.value), setError(''))} />
        </label>
      </div>
      {error && (
        <p class="rv-editor-error" role="alert">
          {error}
        </p>
      )}
      <div class="rv-editor-actions">
        {onDelete && (
          <button type="button" class="rv-btn rv-btn-quiet rv-btn-danger rv-editor-delete" disabled={busy} onClick={onDelete}>
            {deleteLabel}
          </button>
        )}
        {/* Approve on the left, Cancel on the right, both the same size. */}
        <button type="submit" class="rv-btn rv-btn-primary rv-editor-btn" disabled={busy}>
          {submitLabel}
        </button>
        <button type="button" class="rv-btn rv-btn-outline rv-editor-btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}
