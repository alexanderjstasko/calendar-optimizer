import { useEffect, useState } from 'preact/hooks'
import { createApi } from './review/api.js'
import { Toast } from './review/ui.jsx'
import {
  DEFAULT_HOURS,
  NEW_WORK,
  WEEKDAYS,
  clock12,
  describeMinutes,
  freeRange,
  parseKeywords,
  rangeMinutes,
  validateHours,
} from './hours.js'
import { formatStamp } from './review/format.js'
import './review/review.css'

// The Hours tab (compact since 2026-10-26): one line per setting with an Edit
// control -- Home, sleep and work per profile (weekends never work), and the
// planning settings (the free-time floor, the Calendar Review length, the
// scout's key words). Editing a row opens its fields in place; Save PUTs the
// whole document and tells the shell (onSaved) so the calendar's bands redraw.

const DEFAULT_ROW = { office_days: [] }

function describeWork(work) {
  if (!work) return 'No work'
  const parts = [`${clock12(work.start)} – ${clock12(work.end)}`]
  const days = work.office_days || []
  parts.push(days.length ? `Office ${days.join(', ')}` : 'Always from home')
  if (work.location) parts.push(work.location)
  return parts.join(' · ')
}

function describeSleep(sleep) {
  return `${clock12(sleep.start)} – ${clock12(sleep.end)} · ${describeMinutes(rangeMinutes(sleep, { wraps: true }))}`
}

function describeFree(profile) {
  const free = freeRange(profile)
  const minutes = rangeMinutes(free)
  return minutes > 0 ? `${clock12(free.start)} – ${clock12(free.end)} · ${describeMinutes(minutes)}` : 'none'
}

/** One line: name | value | Edit, or the editor with Cancel / Save while editing. */
function Row({ name, hint, value, editing, onEdit, onCancel, onSave, saving, children }) {
  return (
    <div class={`rv-hours-row${editing ? ' is-editing' : ''}`}>
      <div class="rv-hours-line">
        <span class="rv-hours-name">{name}</span>
        {!editing && <span class="rv-hours-value">{value}</span>}
        {!editing && hint && <span class="rv-hours-hint">{hint}</span>}
        {!editing && (
          <button type="button" class="rv-btn rv-btn-quiet rv-hours-edit" onClick={onEdit} aria-label={`Edit ${name}`}>
            Edit
          </button>
        )}
      </div>
      {editing && (
        <form
          class="rv-hours-editor"
          onSubmit={(e) => {
            e.preventDefault()
            onSave()
          }}
        >
          {children}
          <div class="rv-hours-editor-actions">
            <button type="button" class="rv-btn rv-btn-quiet" disabled={saving} onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" class="rv-btn rv-btn-primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

const TimeField = ({ label, value, onInput }) => (
  <label class="rv-field">
    <span>{label}</span>
    <input type="time" required value={value || ''} onInput={(e) => onInput(e.currentTarget.value)} />
  </label>
)

/**
 * hours: the saved document from the shell (or the defaults while loading);
 * savedAt: ISO stamp or null; onSaved(status): the PUT's answer.
 */
export function HoursTab({ apiUrl, hours, saved, savedAt, onSaved }) {
  const [api] = useState(() => createApi(apiUrl))
  const base = hours || DEFAULT_HOURS
  const [editing, setEditing] = useState(null) // which row is open: 'home' | 'weekdays.sleep' | ...
  const [draft, setDraft] = useState(base) // the whole document while a row is open
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)
  const [keywordText, setKeywordText] = useState('')

  // A fresh copy from the server (a save on another device) is shown as long
  // as no row is being edited here.
  useEffect(() => {
    if (editing === null) setDraft(base)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hours])

  useEffect(() => {
    if (!toast) return undefined
    const timer = setTimeout(() => setToast(null), toast.tone === 'error' ? 6000 : 3200)
    return () => clearTimeout(timer)
  }, [toast])

  function open(key) {
    setDraft(base)
    if (key === 'keywords') setKeywordText((base.event_keywords || []).join(', '))
    setEditing(key)
  }

  function cancel() {
    setDraft(base)
    setEditing(null)
  }

  const patchProfile = (profile, patch) => setDraft((d) => ({ ...d, [profile]: { ...d[profile], ...patch } }))
  const patchWork = (patch) =>
    setDraft((d) => ({ ...d, weekdays: { ...d.weekdays, work: { ...DEFAULT_ROW, ...(d.weekdays.work || NEW_WORK), ...patch } } }))

  async function save() {
    const next = editing === 'keywords' ? { ...draft, event_keywords: parseKeywords(keywordText) } : draft
    const problem = validateHours(next)
    if (problem) return setToast({ id: Date.now(), tone: 'error', message: problem })
    setSaving(true)
    try {
      const status = await api.saveHours(next)
      onSaved(status)
      setEditing(null)
      setToast({ id: Date.now(), tone: 'ok', message: 'Saved. The calendar and the next planning run use these hours.' })
    } catch (err) {
      setToast({ id: Date.now(), tone: 'error', message: err.message })
    } finally {
      setSaving(false)
    }
  }

  const row = (key, props, editor) => (
    <Row
      {...props}
      editing={editing === key}
      onEdit={() => open(key)}
      onCancel={cancel}
      onSave={save}
      saving={saving}
    >
      {editor}
    </Row>
  )

  const shown = editing === null ? base : draft
  const work = shown.weekdays.work
  const keywords = shown.event_keywords || []

  return (
    <section class="rv-tab rv-hours" aria-label="Hours">
      <header class="rv-head">
        <div class="rv-head-text">
          <h2 class="rv-title">Hours</h2>
        </div>
      </header>

      <section class="rv-card rv-hours-card" aria-label="Home">
        {row(
          'home',
          { name: 'Home', value: shown.home || 'Not set', hint: shown.home ? '' : 'days start and end here' },
          <label class="rv-field rv-field-wide">
            <span>Street address</span>
            <input
              type="text"
              maxLength={200}
              placeholder="e.g. 1 Home Street, Springfield"
              value={draft.home || ''}
              onInput={(e) => setDraft({ ...draft, home: e.currentTarget.value })}
            />
          </label>,
        )}
      </section>

      <section class="rv-card rv-hours-card" aria-label="Weekdays">
        <h3 class="rv-hours-card-title">Weekdays</h3>
        {row(
          'weekdays.sleep',
          { name: 'Sleep', value: describeSleep(shown.weekdays.sleep) },
          <div class="rv-hours-fields">
            <TimeField label="Bed" value={draft.weekdays.sleep.start} onInput={(v) => patchProfile('weekdays', { sleep: { ...draft.weekdays.sleep, start: v } })} />
            <TimeField label="Wake" value={draft.weekdays.sleep.end} onInput={(v) => patchProfile('weekdays', { sleep: { ...draft.weekdays.sleep, end: v } })} />
          </div>,
        )}
        {row(
          'weekdays.work',
          { name: 'Work', value: describeWork(work) },
          <div class="rv-hours-fields">
            <label class="rv-hours-check rv-field-wide">
              <input
                type="checkbox"
                checked={!!draft.weekdays.work}
                onChange={(e) => patchProfile('weekdays', { work: e.currentTarget.checked ? { ...NEW_WORK } : null })}
              />
              <span>I work on weekdays</span>
            </label>
            {draft.weekdays.work && (
              <>
                <TimeField label="Start" value={draft.weekdays.work.start} onInput={(v) => patchWork({ start: v })} />
                <TimeField label="End" value={draft.weekdays.work.end} onInput={(v) => patchWork({ end: v })} />
                <label class="rv-field rv-field-wide">
                  <span>Office address</span>
                  <input
                    type="text"
                    maxLength={200}
                    placeholder="e.g. 100 Office Plaza"
                    value={draft.weekdays.work.location || ''}
                    onInput={(e) => patchWork({ location: e.currentTarget.value })}
                  />
                </label>
                <fieldset class="rv-hours-days rv-field-wide">
                  <legend>Days in the office (the others are from home)</legend>
                  {WEEKDAYS.map((day) => {
                    const on = (draft.weekdays.work.office_days || []).includes(day)
                    return (
                      <label key={day} class={`rv-hours-day${on ? ' is-on' : ''}`}>
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={(e) => {
                            const set = new Set(draft.weekdays.work.office_days || [])
                            if (e.currentTarget.checked) set.add(day)
                            else set.delete(day)
                            patchWork({ office_days: WEEKDAYS.filter((d) => set.has(d)) })
                          }}
                        />
                        <span>{day}</span>
                      </label>
                    )
                  })}
                </fieldset>
              </>
            )}
          </div>,
        )}
        <div class="rv-hours-row is-derived">
          <div class="rv-hours-line">
            <span class="rv-hours-name">Free</span>
            <span class="rv-hours-value">{describeFree(shown.weekdays)}</span>
            <span class="rv-hours-hint">after work until bed</span>
          </div>
        </div>
      </section>

      <section class="rv-card rv-hours-card" aria-label="Weekends">
        <h3 class="rv-hours-card-title">Weekends</h3>
        {row(
          'weekends.sleep',
          { name: 'Sleep', value: describeSleep(shown.weekends.sleep) },
          <div class="rv-hours-fields">
            <TimeField label="Bed" value={draft.weekends.sleep.start} onInput={(v) => patchProfile('weekends', { sleep: { ...draft.weekends.sleep, start: v } })} />
            <TimeField label="Wake" value={draft.weekends.sleep.end} onInput={(v) => patchProfile('weekends', { sleep: { ...draft.weekends.sleep, end: v } })} />
          </div>,
        )}
        <div class="rv-hours-row is-derived">
          <div class="rv-hours-line">
            <span class="rv-hours-name">Free</span>
            <span class="rv-hours-value">{describeFree(shown.weekends)}</span>
            <span class="rv-hours-hint">the whole day</span>
          </div>
        </div>
      </section>

      <section class="rv-card rv-hours-card" aria-label="Planning">
        <h3 class="rv-hours-card-title">Planning</h3>
        {row(
          'free',
          { name: 'Free time', value: `At least ${shown.free_hours_per_week} h a week unscheduled`, hint: 'the planner leaves this much open' },
          <label class="rv-field">
            <span>Hours a week</span>
            <input
              type="number"
              min="0"
              max="100"
              step="0.5"
              required
              value={draft.free_hours_per_week}
              onInput={(e) => setDraft({ ...draft, free_hours_per_week: Number(e.currentTarget.value) })}
            />
          </label>,
        )}
        {row(
          'review',
          { name: 'Calendar review', value: `${shown.calendar_review_minutes} min on Sundays`, hint: 'the planner picks the slot' },
          <label class="rv-field">
            <span>Minutes</span>
            <input
              type="number"
              min="5"
              max="180"
              step="5"
              required
              value={draft.calendar_review_minutes}
              onInput={(e) => setDraft({ ...draft, calendar_review_minutes: Number(e.currentTarget.value) })}
            />
          </label>,
        )}
        {row(
          'goals-review',
          {
            name: 'Goals review',
            value: `${shown.goals_review_minutes} min every ${shown.goals_review_every_months === 1 ? 'month' : `${shown.goals_review_every_months} months`}`,
            hint: 'a look over the Telos tree; the planner picks the slot',
          },
          <div class="rv-hours-fields">
            <label class="rv-field">
              <span>Every (months)</span>
              <input
                type="number"
                min="1"
                max="12"
                step="1"
                required
                value={draft.goals_review_every_months}
                onInput={(e) => setDraft({ ...draft, goals_review_every_months: Number(e.currentTarget.value) })}
              />
            </label>
            <label class="rv-field">
              <span>Minutes</span>
              <input
                type="number"
                min="5"
                max="240"
                step="5"
                required
                value={draft.goals_review_minutes}
                onInput={(e) => setDraft({ ...draft, goals_review_minutes: Number(e.currentTarget.value) })}
              />
            </label>
          </div>,
        )}
        {row(
          'keywords',
          {
            name: 'Event key words',
            value: keywords.length ? (
              <span class="rv-hours-chips">
                {keywords.map((k) => (
                  <span key={k} class="rv-hours-chip">
                    {k}
                  </span>
                ))}
              </span>
            ) : (
              'None'
            ),
            hint: 'what the events scout looks out for',
          },
          <label class="rv-field rv-field-wide">
            <span>Comma-separated</span>
            <input
              type="text"
              placeholder="e.g. jazz, AI talks, book launch"
              value={keywordText}
              onInput={(e) => setKeywordText(e.currentTarget.value)}
            />
          </label>,
        )}
      </section>

      <p class="rv-hint rv-hours-stamp">{saved ? `Saved ${formatStamp(savedAt)}.` : 'Not saved yet: these are the defaults.'}</p>
      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </section>
  )
}
