import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { useCalendarApp, ScheduleXCalendar } from '@schedule-x/preact'
import {
  createViewDay,
  createViewWeek,
  createViewMonthGrid,
  createViewMonthAgenda,
} from '@schedule-x/calendar'
import { createEventsServicePlugin } from '@schedule-x/events-service'
import { createCalendarControlsPlugin } from '@schedule-x/calendar-controls'
import { createEventModalPlugin } from '@schedule-x/event-modal'
import { createCurrentTimePlugin } from '@schedule-x/current-time'
import 'temporal-polyfill/global'
import { CalendarNav } from './CalendarNav.jsx'
import { TabBar } from './TabBar.jsx'
import { CalendarIcon } from './icons.jsx'
import { useHashRoute } from './useHashRoute.js'
import { EventsTab, PlanTab } from './review/index.js'
import { makeEventDetails, isoOf } from './EventDetails.jsx'
import { OfflineBanner } from './OfflineBanner.jsx'
import { HoursTab } from './HoursTab.jsx'
import { SlotSheet } from './SlotSheet.jsx'
import { attachDragMove } from './dragMove.js'
import { layoutTimeGrid } from './gridLayout.js'
import { DEFAULT_HOURS, PROFILES, freeRange, profileFor, toMinutes } from './hours.js'
import { noteResponse, noteUnreachable, unreachableMessage } from './offline.js'
import '@schedule-x/theme-default/dist/index.css'
import './app.css'

// Where the FastAPI backend is running. A production build (`npm run build`) is
// served by FastAPI itself (backend/main.py mounts frontend/dist at /), so it
// calls its own origin. The Vite dev server reads VITE_API_URL from the
// project-root .env (see vite.config.js envDir), e.g. for Tailscale access.
const API_URL = import.meta.env.PROD
  ? ''
  : import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Breakpoints. Phones (<= 640px) get the bottom tab bar, a single-column day
// view and open on Day; tablets open on Week; desktops on Month.
const PHONE_MAX_WIDTH = 640
const TABLET_MAX_WIDTH = 1024
// Schedule-X only offers its month grid when the calendar is >= 700px wide
// (its own breakpoint, 43.75rem); narrower screens get the month agenda.
const MONTH_GRID_MIN_WIDTH = 760
const SX_SMALL_CALENDAR_PX = 700

const TIMEZONE = 'America/New_York'
const MONDAY = 1 // Schedule-X WeekDay enum: 1 = Monday ... 7 = Sunday

// Day/week views draw the awake window from the Hours tab: the earliest
// wake-up to the latest bedtime over both profiles, on whole hours (a bedtime
// after midnight runs the grid to 24:00). Events running outside it are drawn
// clipped to it, never hidden (see fitToAwakeWindow).
// From 641px up the day view is split into two side-by-side panes of (near)
// equal length, so the hour rows line up: morning left, afternoon/evening
// right. Phones show the whole window as one column instead.
const MIN_GRID_HOURS = 8
const hhmm = (h) => `${String(h).padStart(2, '0')}:00`
function gridWindowFor(hours = DEFAULT_HOURS) {
  let start = 24
  let end = 0
  for (const profile of PROFILES) {
    const sleep = hours?.[profile]?.sleep || DEFAULT_HOURS[profile].sleep
    const wake = toMinutes(sleep.end)
    const bed = toMinutes(sleep.start)
    start = Math.min(start, Math.floor(wake / 60))
    // Bed before wake on the clock = bed after midnight: draw to the day's end.
    end = Math.max(end, bed < wake ? 24 : Math.ceil(bed / 60))
  }
  if (end - start < MIN_GRID_HOURS) end = Math.min(24, start + MIN_GRID_HOURS)
  if (end - start < MIN_GRID_HOURS) start = end - MIN_GRID_HOURS
  const mid = start + Math.ceil((end - start) / 2)
  return {
    awake: { start: hhmm(start), end: hhmm(end) },
    morning: { start: hhmm(start), end: hhmm(mid) },
    afternoon: { start: hhmm(mid), end: hhmm(end) },
  }
}
const DEFAULT_GRID = gridWindowFor(DEFAULT_HOURS)
const hoursIn = (b) => parseInt(b.end, 10) - parseInt(b.start, 10)
// "7 AM", "3 PM", "midnight": the pane labels and the clip note.
function clockLabel(time) {
  const h = parseInt(time, 10)
  if (h === 0 || h === 24) return 'midnight'
  if (h === 12) return 'noon'
  return h < 12 ? `${h} AM` : `${h - 12} PM`
}
// A zoned instant on `day` at a grid clock time; '24:00' is the next midnight.
function atClock(day, time) {
  return time === '24:00' ? day.add({ days: 1 }).toZonedDateTime({ timeZone: TIMEZONE, plainTime: '00:00' }) : day.toZonedDateTime({ timeZone: TIMEZONE, plainTime: time })
}
// Pixels per hour are fitted to the space the grid actually has (measured),
// within these limits. Below the minimum the grid scrolls (short phones).
const HOUR_PX = { min: 32, phoneMin: 40, max: 72, splitMax: 96 }
const INITIAL_HOUR_PX = 40
// Month grid rows, from app.css: a day needs dayChromePx (padding, date,
// "+ N events" line) + eventPx per listed event (chip + gap); the first row
// also holds the weekday names. Rows share the leftover height (flex).
const MONTH_GRID = { dayNamesPx: 19, dayChromePx: 57, eventPx: 23, minEvents: 2 }

// A horizontal swipe of at least this many px (and clearly more horizontal than
// vertical) moves to the next/previous day, week or month.
const SWIPE_MIN_PX = 60
// An open calendar refetches events and badges this often (the backend's own sync cadence).
const REFETCH_WHILE_OPEN_MS = 5 * 60 * 1000

// Remembered per browser; wrapped in try/catch (private mode, blocked storage).
const PREVIEW_STORAGE_KEY = 'calendar.previewPlan'
function readStoredPreview() {
  try {
    return window.localStorage.getItem(PREVIEW_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
function storePreview(on) {
  try {
    window.localStorage.setItem(PREVIEW_STORAGE_KEY, on ? '1' : '0')
  } catch {
    /* storage unavailable: the toggle still works for this visit */
  }
}

// Colours live in app.css (tokens switch with prefers-color-scheme). Schedule-X
// writes these values onto <html> as --sx-color-<name>*; as var() references
// they resolve against the current theme, so light and dark are the same here.
const colors = (main, container, onContainer) => ({ main, container, onContainer })
const CALENDARS = {
  // Events the optimizer/scout placed after approval (calendarId 'optimizer',
  // see backend/db.py promote_proposal). Every other calendarId (the synced
  // sources) uses Schedule-X's default "primary" colour, mapped to --ev-fixed*.
  optimizer: {
    colorName: 'optimizer',
    lightColors: colors('var(--ev-plan)', 'var(--ev-plan-fill)', 'var(--ev-plan-ink)'),
    darkColors: colors('var(--ev-plan)', 'var(--ev-plan-fill)', 'var(--ev-plan-ink)'),
  },
  // Pending proposals shown by the preview (GET /events/preview).
  proposed: {
    colorName: 'proposed',
    lightColors: colors('var(--app-preview)', 'var(--app-preview-fill)', 'var(--app-preview-ink)'),
    darkColors: colors('var(--app-preview)', 'var(--app-preview-fill)', 'var(--app-preview-ink)'),
  },
  // A stack card of short blocks from different calendars (gridLayout.js).
  stack: {
    colorName: 'stack',
    lightColors: colors('var(--app-text-faint)', 'var(--app-surface-2)', 'var(--app-text)'),
    darkColors: colors('var(--app-text-faint)', 'var(--app-surface-2)', 'var(--app-text)'),
  },
}

const today = () => Temporal.Now.plainDateISO(TIMEZONE)
const isPhoneWidth = () => window.innerWidth <= PHONE_MAX_WIDTH
// Phones open on Day, tablets on Week, desktops on Month.
const viewBucketForWidth = (width) =>
  width <= PHONE_MAX_WIDTH ? 'day' : width <= TABLET_MAX_WIDTH ? 'week' : 'month'
// Schedule-X view name -> the nav bar's three buttons.
const navViewOf = (sxView) => (sxView?.startsWith('month') ? 'month' : sxView || 'week')

const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null

/**
 * Convert one event's start/end strings (as served by the FastAPI backend)
 * into the Temporal objects Schedule-X expects.
 *   - All-day events: plain 'YYYY-MM-DD' -> Temporal.PlainDate
 *   - Timed events:   '2026-07-15T10:00:00-04:00[America/New_York]' -> Temporal.ZonedDateTime
 */
function toTemporalValue(value) {
  if (!value) return value
  if (!value.includes('T')) {
    return Temporal.PlainDate.from(value)
  }
  return Temporal.ZonedDateTime.from(value)
}

/**
 * Fit a timed event into the drawn window of the day it starts (`awake`,
 * e.g. 07:00-23:00), keeping the real times in realStart/realEnd for the details
 * sheet (EventDetails.jsx). Schedule-X 4.9 silently drops a timed event that
 * ends after the view range's last day boundary (23:00) or starts before its
 * first (07:00) -- positionInTimeGrid -- which made an approved block moved to
 * 20:15-23:15 vanish from the day and week views; and it moves an event that
 * crosses midnight into the all-day row across both days. So: a late end is
 * drawn to 23:00, an early start from 07:00, and an event entirely outside the
 * window (a 5 AM run, a party from 11:30 PM) as a short block at the nearest
 * edge; the details sheet shows the real times plus clipNote. Events of a
 * day or more stay as they are (the all-day row is right for them).
 */
function fitToAwakeWindow(event, awake = DEFAULT_GRID.awake) {
  if (!(event.start instanceof Temporal.ZonedDateTime)) return event
  const start = event.start.withTimeZone(TIMEZONE)
  const end = event.end.withTimeZone(TIMEZONE)
  if (Temporal.ZonedDateTime.compare(end, start.add({ hours: 24 })) >= 0) return { ...event, start, end }
  const dayStart = atClock(start.toPlainDate(), awake.start)
  const dayEnd = atClock(start.toPlainDate(), awake.end)
  let shownStart = Temporal.ZonedDateTime.compare(start, dayStart) < 0 ? dayStart : start
  let shownEnd = Temporal.ZonedDateTime.compare(end, dayEnd) > 0 ? dayEnd : end
  if (shownStart === start && shownEnd === end) {
    // Ending exactly at midnight counts as crossing into the next day for
    // Schedule-X (all-day row): draw it to 23:59, keep the real end.
    if (end.hour === 0 && end.minute === 0 && Temporal.PlainDate.compare(end.toPlainDate(), start.toPlainDate()) > 0) {
      return { ...event, start, end: end.subtract({ minutes: 1 }), realStart: isoOf(start), realEnd: isoOf(end) }
    }
    return { ...event, start, end }
  }
  if (Temporal.ZonedDateTime.compare(shownEnd, shownStart) <= 0) {
    if (Temporal.ZonedDateTime.compare(end, dayStart) <= 0) {
      shownStart = dayStart
      shownEnd = dayStart.add({ minutes: 30 })
    } else {
      shownEnd = dayEnd
      shownStart = dayEnd.subtract({ minutes: 30 })
    }
  }
  const clock = (zdt) => zdt.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit' })
  const sameDay = Temporal.PlainDate.compare(start.toPlainDate(), end.toPlainDate()) === 0
  const notes = []
  if (shownStart !== start) notes.push(`Starts at ${clock(start)}.`)
  if (shownEnd !== end) {
    notes.push(
      `Runs until ${sameDay ? clock(end) : end.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' })}.`,
    )
  }
  return {
    ...event,
    start: shownStart,
    end: shownEnd,
    realStart: isoOf(start),
    realEnd: isoOf(end),
    clipNote: `${notes.join(' ')} The grid only draws ${clockLabel(awake.start)} – ${clockLabel(awake.end)}.`,
  }
}

// Background bands from the Hours tab (hours.js / backend/hours.py): the part
// of the sleep range that falls inside the drawn grid, the "Work" band, and
// free time, which is derived, not planned: every stretch of at least
// FREE_MIN_MINUTES inside the day's free window (after work until bed on a
// weekday, the whole awake day at the weekend: hours.js freeRange) with no
// timed event on it (fixed, approved, or a proposal while the preview is on).
// Tapping a Free band opens the slot sheet (SlotSheet.jsx). Drawn as
// Schedule-X background events behind the grid, so it can never overlap
// anything and needs no approval. Days covered: a week back to
// FREE_DAYS_AHEAD ahead; all-day entries do not block time (a trip or holiday
// is context, see the optimizer prompt).
// Background bands only take an inline style; these custom properties pick
// the look in app.css (.sx__time-grid-background-event).
const WORK_STYLE = { '--band-fill': 'var(--app-work-fill)', '--band-image': 'none', '--band-ink': 'var(--app-work-ink)' }
const SLEEP_STYLE = { '--band-fill': 'var(--app-sleep-fill)', '--band-image': 'none', '--band-ink': 'var(--app-sleep-ink)' }
const FREE_MIN_MINUTES = 30
const FREE_DAYS_AHEAD = 120
const compareZdt = Temporal.ZonedDateTime.compare
const at = (day, hhmm) => day.toZonedDateTime({ timeZone: TIMEZONE, plainTime: hhmm })
// The part of [start, end) inside [lo, hi), or null.
function clipRange(start, end, lo, hi) {
  const s = compareZdt(start, lo) > 0 ? start : lo
  const e = compareZdt(end, hi) < 0 ? end : hi
  return compareZdt(s, e) < 0 ? [s, e] : null
}
function freeTimeBackground(events, hours = DEFAULT_HOURS, awake = DEFAULT_GRID.awake) {
  const byDay = new Map()
  for (const ev of events) {
    if (!(ev.start instanceof Temporal.ZonedDateTime)) continue
    const key = ev.start.toPlainDate().toString()
    if (!byDay.has(key)) byDay.set(key, [])
    byDay.get(key).push([ev.start, ev.end])
  }
  const out = []
  let day = today().subtract({ days: 7 })
  const last = today().add({ days: FREE_DAYS_AHEAD })
  while (Temporal.PlainDate.compare(day, last) <= 0) {
    const p = hours[profileFor(day)] || DEFAULT_HOURS[profileFor(day)]
    const gridStart = atClock(day, awake.start)
    const gridEnd = atClock(day, awake.end)
    const busy = (byDay.get(day.toString()) || []).slice()
    // Sleep is one range that may cross midnight; only the parts inside the grid are drawn.
    const sleepParts =
      p.sleep.start < p.sleep.end
        ? [[at(day, p.sleep.start), at(day, p.sleep.end)]]
        : [
            [at(day, '00:00'), at(day, p.sleep.end)],
            [at(day, p.sleep.start), at(day.add({ days: 1 }), '00:00')],
          ]
    for (const [s, e] of sleepParts) {
      const part = clipRange(s, e, gridStart, gridEnd)
      if (part) {
        out.push({ start: part[0], end: part[1], title: 'Sleep', style: SLEEP_STYLE })
        busy.push(part)
      }
    }
    if (p.work) {
      const workStart = at(day, p.work.start)
      const workEnd = at(day, p.work.end)
      out.push({ start: workStart, end: workEnd, title: 'Work', style: WORK_STYLE })
      busy.push([workStart, workEnd])
    }
    busy.sort((a, b) => compareZdt(a[0], b[0]))
    const free = freeRange(p)
    let cursor = atClock(day, free.start)
    const freeEnd = atClock(day, free.end)
    const push = (from, to) => {
      if (to.since(from).total({ unit: 'minutes' }) >= FREE_MIN_MINUTES) out.push({ start: from, end: to, title: 'Free', style: {} })
    }
    for (const [start, end] of busy) {
      if (compareZdt(start, cursor) > 0) push(cursor, compareZdt(start, freeEnd) < 0 ? start : freeEnd)
      if (compareZdt(end, cursor) > 0) cursor = end
    }
    if (compareZdt(cursor, freeEnd) < 0) push(cursor, freeEnd)
    day = day.add({ days: 1 })
  }
  return out
}

// Fitting to the drawn window happens later, in pushEvents, so a change of
// hours refits the same events.
function withTemporalDates(events) {
  const out = []
  for (const { people, ...event } of events) {
    try {
      out.push({
        ...event,
        // The event modal renders `people.length && ...`, which prints a stray
        // "0" for an empty list -- only pass people when there are some.
        ...(people?.length ? { people } : {}),
        start: toTemporalValue(event.start),
        end: toTemporalValue(event.end),
      })
    } catch (err) {
      // One unreadable event must not keep the rest of the calendar from drawing.
      console.error('Skipped an event with unreadable times:', event.id, err)
    }
  }
  return out
}

// Approved planned blocks can be dragged in the Day and Week grids
// (dragMove.js); the class shows the grab cursor and the resize grip.
function asMovable(event) {
  if (event.calendarId !== 'optimizer' || !(event.start instanceof Temporal.ZonedDateTime)) return event
  return {
    ...event,
    _options: { ...event._options, additionalClasses: [...(event._options?.additionalClasses || []), 'is-movable'] },
  }
}

// Preview events get their own calendar colour plus the `is-proposed` class
// (Schedule-X puts _options.additionalClasses on the event element in every
// view), which app.css turns into a dashed outline.
function asProposed(event) {
  return {
    ...event,
    calendarId: 'proposed', // the details sheet captions it as proposed
    _options: {
      ...event._options,
      // Keep the backend's classes (sx-proposal, sx-proposal-<kind>) too.
      additionalClasses: [...(event._options?.additionalClasses || []), 'is-proposed'],
      disableDND: true,
      disableResize: true,
    },
  }
}

async function getJson(path) {
  let response
  try {
    response = await fetch(`${API_URL}${path}`)
  } catch {
    noteUnreachable()
    throw new Error(unreachableMessage())
  }
  if (noteResponse(response)) throw new Error(`${unreachableMessage()} Nothing is saved to show yet.`)
  if (!response.ok) throw new Error(`Backend returned ${response.status}`)
  return response.json()
}

async function postJson(path, body) {
  let response
  try {
    response = await fetch(`${API_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new Error(unreachableMessage({ writing: true }))
  }
  if (!response.ok) {
    let detail = `Backend returned ${response.status}`
    try {
      const data = await response.json()
      if (data?.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* no JSON body */
    }
    throw new Error(detail)
  }
  return response.json()
}

// Schedule-X decides "small calendar" (and switches away from the month grid)
// from its wrapper's width on every window resize. A calendar on a hidden tab
// measures 0px wide, so keep the last answer while hidden.
function isCalendarSmall($app) {
  const width = $app.elements.calendarWrapper?.clientWidth
  if (!width) return $app.calendarState.isCalendarSmall.value ?? window.innerWidth < MONTH_GRID_MIN_WIDTH
  return width < SX_SMALL_CALENDAR_PX
}

export function App() {
  const [route, navigate] = useHashRoute()
  const tab = route.tab
  const calendarVisible = tab === 'calendar'

  const [status, setStatus] = useState('loading') // loading | ready | error
  const [errorMessage, setErrorMessage] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [summary, setSummary] = useState(null) // GET /review/summary (badges, counts)
  const [hoursStatus, setHoursStatus] = useState({ hours: DEFAULT_HOURS, saved: false, saved_at: null }) // GET /hours
  const hoursRef = useRef(DEFAULT_HOURS) // the bands are built outside render
  const grid = useMemo(() => gridWindowFor(hoursStatus.hours), [hoursStatus.hours])
  const gridRef = useRef(DEFAULT_GRID)
  gridRef.current = grid
  const [previewOn, setPreviewOn] = useState(readStoredPreview)
  const [previewCount, setPreviewCount] = useState(null)
  const [previewError, setPreviewError] = useState('')
  const [todayDate, setTodayDate] = useState(today)
  const [isPhone, setIsPhone] = useState(isPhoneWidth)
  const [isDark, setIsDark] = useState(() => !!darkQuery?.matches)

  // Plugins are stateful objects -- create each one exactly once.
  const [eventsService] = useState(() => createEventsServicePlugin())
  const [calendarControls] = useState(() => createCalendarControlsPlugin())
  // Click an event to see its details (time, location, people, description).
  const [eventModal] = useState(() => createEventModalPlugin())
  // "Now" line across the whole week in the time-grid views.
  const [currentTime] = useState(() => createCurrentTimePlugin({ fullWeekWidth: true }))
  // The afternoon pane of the split day view is a second, day-only calendar
  // with its own plugins, kept on the same date and events as the main one.
  const [pmEvents] = useState(() => createEventsServicePlugin())
  const [pmControls] = useState(() => createCalendarControlsPlugin())
  const [pmModal] = useState(() => createEventModalPlugin())
  const [pmCurrentTime] = useState(() => createCurrentTimePlugin())
  // The event details sheet (EventDetails.jsx) replaces the plugins' default
  // modal content on both calendars. Created once: a new customComponents
  // object would make the Preact adapter re-render Schedule-X every time. It
  // reaches the app through a ref (move an approved block, close the sheet).
  const detailsCtx = useRef({ move: async () => {}, close: () => {} })
  const customComponents = useMemo(() => ({ eventModal: makeEventDetails(detailsCtx) }), [])
  // A tap on the time grid: when it lands on a Free band, open the slot sheet
  // for that band. The callbacks are created once, so they go through a ref.
  const [slot, setSlot] = useState(null) // {start, end} ISO, or null
  const freeBands = useRef([])
  const onGridClick = useRef(() => {})
  onGridClick.current = (dateTime) => {
    const band = freeBands.current.find(
      (b) => b.title === 'Free' && compareZdt(dateTime, b.start) >= 0 && compareZdt(dateTime, b.end) < 0,
    )
    setSlot(band ? { start: isoOf(band.start), end: isoOf(band.end) } : null)
  }

  // What the nav bar shows; updated from Schedule-X whenever the range changes.
  const [initialView] = useState(() => viewBucketForWidth(window.innerWidth))
  const initialHours = initialView === 'day' && !isPhoneWidth() ? DEFAULT_GRID.morning : DEFAULT_GRID.awake
  const [hourPx, setHourPx] = useState(INITIAL_HOUR_PX)
  const hourPxRef = useRef(INITIAL_HOUR_PX) // pushEvents lays the time grids out for it
  hourPxRef.current = hourPx
  const [nav, setNav] = useState(() => ({ view: initialView, date: today() }))
  // Calendar-controls calls only work once Schedule-X has rendered.
  const [rendered, setRendered] = useState(false)

  const panesRef = useRef(null)
  const mainPaneRef = useRef(null)
  const latestEvents = useRef([])
  const loadSeq = useRef(0) // the newest reloadCalendar() call
  // What the calendars were last drawn from (events, preview, hours, layout as
  // JSON): a refetch that brings nothing new skips the redraw.
  const dataText = useRef({ events: '', preview: '', hours: '' })
  const drawnFrom = useRef('')
  const drawnById = useRef(new Map()) // the events as drawn (clipped), for the drag layer
  const previewEvents = useRef([])
  const previewOnRef = useRef(previewOn)
  const navRef = useRef(nav)
  navRef.current = nav
  const lastLoadAt = useRef(0)

  // Views are also created once and reused across renders. Order matters to
  // Schedule-X's own small/wide switching: it picks the first compatible view,
  // so the month grid falls back to the month agenda (and back).
  const viewDay = useMemo(() => createViewDay(), [])
  // Schedule-X hides the week grid when the calendar is under 700px wide
  // (phones, narrow windows); keep it selectable there too.
  const viewWeek = useMemo(() => Object.assign(createViewWeek(), { hasSmallScreenCompat: true }), [])
  const viewMonthGrid = useMemo(() => createViewMonthGrid(), [])
  const viewMonthAgenda = useMemo(() => createViewMonthAgenda(), [])
  const viewDayPm = useMemo(() => createViewDay(), [])
  const sxViewName = (view) =>
    view === 'day'
      ? viewDay.name
      : view === 'week'
        ? viewWeek.name
        : window.innerWidth >= MONTH_GRID_MIN_WIDTH
          ? viewMonthGrid.name
          : viewMonthAgenda.name

  const calendar = useCalendarApp({
    views: [viewMonthAgenda, viewMonthGrid, viewDay, viewWeek],
    defaultView: sxViewName(initialView),
    timezone: TIMEZONE,
    firstDayOfWeek: MONDAY,
    dayBoundaries: initialHours,
    // eventOverlap off: events at the same time sit side by side, not cascaded.
    weekOptions: { gridHeight: hoursIn(initialHours) * INITIAL_HOUR_PX, eventOverlap: false },
    plugins: [eventsService, calendarControls, eventModal, currentTime],
    calendars: CALENDARS,
    isDark: !!darkQuery?.matches,
    callbacks: {
      // Fires on every view or date change (Schedule-X swallows the first,
      // initial range, so it does not mark the calendar as rendered).
      onRangeUpdate() {
        setNav({ view: navViewOf(calendarControls.getView()), date: calendarControls.getDate() })
      },
      // Runs after the plugins are initialised: controls calls are safe from
      // here on (earlier ones crash the current-time plugin).
      onRender() {
        setRendered(true)
      },
      onClickDateTime(dateTime) {
        onGridClick.current(dateTime)
      },
      isCalendarSmall,
    },
  })

  const afternoon = useCalendarApp({
    views: [viewDayPm],
    defaultView: viewDayPm.name,
    timezone: TIMEZONE,
    firstDayOfWeek: MONDAY,
    dayBoundaries: DEFAULT_GRID.afternoon,
    weekOptions: { gridHeight: hoursIn(DEFAULT_GRID.afternoon) * INITIAL_HOUR_PX, eventOverlap: false },
    plugins: [pmEvents, pmControls, pmModal, pmCurrentTime],
    calendars: CALENDARS,
    isDark: !!darkQuery?.matches,
    callbacks: {
      isCalendarSmall,
      onClickDateTime(dateTime) {
        onGridClick.current(dateTime)
      },
    },
  })

  const split = nav.view === 'day' && !isPhone
  const mainHours = split ? grid.morning : grid.awake
  const layoutRef = useRef({ split, isPhone, grid })
  layoutRef.current = { split, isPhone, grid }

  // --- Theme: follow the OS; Schedule-X needs setTheme for its own dark bits.
  useEffect(() => {
    if (!darkQuery) return undefined
    const onChange = (e) => setIsDark(e.matches)
    if (darkQuery.addEventListener) darkQuery.addEventListener('change', onChange)
    else darkQuery.addListener(onChange)
    return () => {
      if (darkQuery.removeEventListener) darkQuery.removeEventListener('change', onChange)
      else darkQuery.removeListener(onChange)
    }
  }, [])
  useEffect(() => {
    const theme = isDark ? 'dark' : 'light'
    calendar.setTheme(theme)
    afternoon.setTheme(theme)
  }, [isDark, calendar, afternoon])

  // --- Width buckets: phone layout flag, and the default view per bucket.
  useEffect(() => {
    const onResize = () => setIsPhone(isPhoneWidth())
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Re-applied only when the width crosses into a different bucket (e.g.
  // rotating a tablet) -- phones fire resize events while scrolling as the
  // address bar hides, which must not undo a view the user picked.
  useEffect(() => {
    if (!rendered) return undefined
    let bucket = viewBucketForWidth(window.innerWidth) // the initial view already matches
    function applyViewForWidth() {
      const next = viewBucketForWidth(window.innerWidth)
      if (next === bucket) return
      bucket = next
      goToView(next)
    }
    window.addEventListener('resize', applyViewForWidth)
    return () => window.removeEventListener('resize', applyViewForWidth)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rendered, calendarControls])

  // --- Fit the views to the space the calendar card really has (measured):
  // the hour height of the time grids, and how many events a month-grid day
  // lists before "+ N events", so the month's rows fit without scrolling.
  const renderedRef = useRef(false)
  renderedRef.current = rendered
  const monthEventsPerDay = useRef(4) // Schedule-X's default
  const fitLayout = useCallback(() => {
    const pane = mainPaneRef.current
    if (!pane || !pane.offsetHeight) return // hidden tab, or not laid out yet
    const container = pane.querySelector('.sx__view-container')
    if (!container) return
    const header = pane.querySelector('.sx__week-header')
    if (header) {
      const { split: isSplit, isPhone: phone, grid: g } = layoutRef.current
      const hours = hoursIn(isSplit ? g.morning : g.awake)
      const available = container.clientHeight - header.offsetHeight
      const min = phone ? HOUR_PX.phoneMin : HOUR_PX.min
      const max = isSplit ? HOUR_PX.splitMax : HOUR_PX.max
      const px = Math.max(min, Math.min(max, Math.floor(available / hours)))
      setHourPx((old) => (old === px ? old : px))
      return
    }
    const weeks = pane.querySelectorAll('.sx__month-grid-week').length
    if (!weeks || !renderedRef.current) return // month agenda, or too early
    const perWeek = (container.clientHeight - MONTH_GRID.dayNamesPx) / weeks
    const n = Math.max(MONTH_GRID.minEvents, Math.floor((perWeek - MONTH_GRID.dayChromePx) / MONTH_GRID.eventPx))
    if (n === monthEventsPerDay.current) return
    monthEventsPerDay.current = n
    calendarControls.setMonthGridOptions({ nEventsPerDay: n })
  }, [calendarControls])

  useEffect(() => {
    const el = panesRef.current
    if (!el || !window.ResizeObserver) return undefined
    const observer = new ResizeObserver(() => fitLayout())
    observer.observe(el)
    return () => observer.disconnect()
  }, [fitLayout, status])

  // The grid header (all-day row, day names) changes height with the view, the
  // split and the events on screen; refit after Schedule-X has re-rendered.
  const navDateKey = nav.date.toString()
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(fitLayout))
    return () => cancelAnimationFrame(id)
  }, [fitLayout, nav.view, navDateKey, split, rendered, calendarVisible, status])

  // Day view = morning + afternoon panes (or the whole awake window on phones);
  // week view = the whole awake window. Only touched when something changes
  // (the view, the scale, or the hours from the Hours tab). Schedule-X redraws
  // its hour axis from a signal effect; on the Pi that lands up to a second
  // after the call when a big event set was just pushed (a save on the Hours
  // tab), so a test must wait for the rows, not for a fixed delay (2026-10-19).
  const applied = useRef(`${initialHours.start}-${initialHours.end}@${INITIAL_HOUR_PX}`)
  const appliedPm = useRef(`${DEFAULT_GRID.afternoon.start}-${DEFAULT_GRID.afternoon.end}`)
  const appliedPmHeight = useRef(0)
  useEffect(() => {
    if (!rendered) return
    const key = `${mainHours.start}-${mainHours.end}@${hourPx}`
    if (applied.current !== key) {
      const boundsChanged = !applied.current.startsWith(`${mainHours.start}-${mainHours.end}@`)
      applied.current = key
      if (boundsChanged) calendarControls.setDayBoundaries(mainHours)
      calendarControls.setWeekOptions({ gridHeight: hoursIn(mainHours) * hourPx })
    }
    try {
      const pmKey = `${grid.afternoon.start}-${grid.afternoon.end}`
      if (appliedPm.current !== pmKey) {
        appliedPm.current = pmKey
        pmControls.setDayBoundaries(grid.afternoon)
      }
      const pmHeight = hoursIn(grid.afternoon) * hourPx
      if (appliedPmHeight.current !== pmHeight) {
        pmControls.setWeekOptions({ gridHeight: pmHeight })
        appliedPmHeight.current = pmHeight // only once it took (the pane may not be rendered yet)
      }
    } catch {
      /* afternoon pane not rendered yet */
    }
  }, [mainHours, grid, rendered, hourPx, calendarControls, pmControls])

  // Keep the afternoon pane on the same date as the main calendar.
  useEffect(() => {
    try {
      if (!pmControls.getDate().equals(nav.date)) pmControls.setDate(nav.date)
    } catch {
      /* not rendered yet; it picks the date up on the next change */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navDateKey, pmControls])

  // --- Phones: start the scrolled grid at "now" (today) or 08:00.
  const scrollKey = `${nav.view}|${navDateKey}|${hourPx}|${isPhone}`
  const lastScrollKey = useRef('')
  useEffect(() => {
    if (!rendered || !calendarVisible || nav.view === 'month') return undefined
    // Only when the day/view/scale changes, not on every re-render.
    const dayKey = scrollKey.split('|').slice(0, 2).join('|')
    if (lastScrollKey.current === dayKey) return undefined
    const id = requestAnimationFrame(() => {
      const container = mainPaneRef.current?.querySelector('.sx__view-container')
      if (!container || container.scrollHeight <= container.clientHeight + 1) return
      lastScrollKey.current = dayKey
      const start = parseInt(mainHours.start, 10)
      const end = parseInt(mainHours.end, 10)
      const now = Temporal.Now.zonedDateTimeISO(TIMEZONE)
      const todayPlain = now.toPlainDate()
      const monday = nav.date.subtract({ days: nav.date.dayOfWeek - 1 })
      const showsToday =
        nav.view === 'day'
          ? nav.date.equals(todayPlain)
          : Temporal.PlainDate.compare(todayPlain, monday) >= 0 &&
            Temporal.PlainDate.compare(todayPlain, monday.add({ days: 6 })) <= 0
      const nowHour = now.hour + now.minute / 60
      const focus = showsToday && nowHour >= start && nowHour < end ? Math.max(start, nowHour - 1) : Math.max(start, 8)
      container.style.scrollBehavior = 'auto'
      container.scrollTop = (focus - start) * hourPx
      container.style.scrollBehavior = ''
    })
    return () => cancelAnimationFrame(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollKey, rendered, calendarVisible])

  // --- Date/view changes, with a short slide in the direction of travel.
  function slide(direction) {
    const el = panesRef.current
    if (!el || !direction) return
    el.classList.remove('slide-next', 'slide-prev')
    void el.offsetWidth // restart the animation
    el.classList.add(direction > 0 ? 'slide-next' : 'slide-prev')
  }

  function goToDate(date) {
    slide(Temporal.PlainDate.compare(date, navRef.current.date))
    calendarControls.setDate(date)
  }

  function goToView(view) {
    calendarControls.setView(sxViewName(view))
  }

  function stepDate(direction) {
    const { view, date } = navRef.current
    const step = view === 'day' ? { days: 1 } : view === 'week' ? { weeks: 1 } : { months: 1 }
    goToDate(direction > 0 ? date.add(step) : date.subtract(step))
  }

  // Swipe left/right on the calendar to move a day (week, month) forward/back.
  useEffect(() => {
    const el = panesRef.current
    if (!el) return undefined
    let start = null
    const onStart = (e) => {
      if (e.touches.length !== 1) {
        start = null
        return
      }
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY, at: Date.now() }
    }
    const onEnd = (e) => {
      if (!start) return
      const touch = e.changedTouches[0]
      const dx = touch.clientX - start.x
      const dy = touch.clientY - start.y
      const quick = Date.now() - start.at < 800
      start = null
      if (!quick || Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) < Math.abs(dy) * 1.5) return
      stepDate(dx < 0 ? 1 : -1)
    }
    const onCancel = () => {
      start = null
    }
    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchend', onEnd, { passive: true })
    el.addEventListener('touchcancel', onCancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onCancel)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  // --- Drag a planned block to move it (Day and Week grids; dragMove.js).
  // The callbacks read refs, so the layer is attached once per mount.
  useEffect(() => {
    const el = panesRef.current
    if (!el) return undefined
    return attachDragMove(el, {
      timezone: TIMEZONE,
      getEvent: (id) => drawnById.current.get(String(id)),
      getBounds(pane) {
        const { split: isSplit, grid: g } = layoutRef.current
        if (pane.classList.contains('pane-afternoon')) return isSplit ? g.afternoon : null
        return isSplit ? g.morning : g.awake
      },
      onStart() {
        detailsCtx.current.close()
        setSlot(null)
      },
      onMove: (id, times) => detailsCtx.current.move(id, times),
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  // --- Data: calendar events, the preview overlay and the review summary.
  // Which layout the events were last pushed with (month list, or time grid at
  // an hour height): the stacks depend on it.
  const laidOut = useRef('')
  const layoutKey = () => (navRef.current.view === 'month' ? 'month' : `grid@${hourPxRef.current}`)
  const drawSignature = () => {
    const t = dataText.current
    return `${t.events}|${previewOnRef.current ? t.preview : ''}|${t.hours}|${layoutKey()}`
  }
  function pushEvents() {
    drawnFrom.current = drawSignature()
    const awake = gridRef.current.awake
    const raw = previewOnRef.current ? latestEvents.current.concat(previewEvents.current) : latestEvents.current
    const all = raw.map((e) => asMovable(fitToAwakeWindow(e, awake)))
    // Day and Week: short runs stacked, overlaps nested (gridLayout.js); the
    // month views list every event as it is.
    laidOut.current = layoutKey()
    const drawn = navRef.current.view === 'month' ? all : layoutTimeGrid(all, hourPxRef.current)
    drawnById.current = new Map(drawn.map((e) => [String(e.id), e]))
    const free = freeTimeBackground(all, hoursRef.current, awake)
    freeBands.current = free
    eventsService.set(drawn)
    eventsService.setBackgroundEvents(free)
    try {
      pmEvents.set(drawn)
      pmEvents.setBackgroundEvents(free)
    } catch {
      /* afternoon pane not rendered yet; the effect below fills it */
    }
    if (previewOnRef.current) revealPreview()
    requestAnimationFrame(fitLayout)
  }


  async function loadPreview() {
    const proposals = await getJson('/events/preview')
    dataText.current.preview = JSON.stringify(proposals)
    previewEvents.current = withTemporalDates(proposals).map(asProposed)
    setPreviewCount(proposals.length)
  }

  async function refreshSummary() {
    try {
      setSummary(await getJson('/review/summary'))
    } catch {
      /* badges are a convenience; everything else works without them */
    }
  }

  // The Hours tab's document; the bands behind the grid are drawn from it.
  function applyHours(status) {
    hoursRef.current = status.hours
    dataText.current.hours = JSON.stringify(status.hours)
    gridRef.current = gridWindowFor(status.hours) // before the next render: pushEvents may run first
    setHoursStatus(status)
  }

  async function loadHours() {
    try {
      applyHours(await getJson('/hours'))
    } catch {
      /* keep the defaults (or the last copy); the tab says so */
    }
  }

  // Events plus (when on) the preview; the preview failing never hides events.
  // Reloads overlap (the 5-min refetch, foreground return, a drop, a decision):
  // only the newest one draws, so an older /events answer never puts a moved
  // block back where it was. A success also clears an earlier load error.
  async function reloadCalendar() {
    const seq = ++loadSeq.current
    const [events] = await Promise.all([getJson('/events'), loadHours()])
    if (seq !== loadSeq.current) return
    lastLoadAt.current = Date.now()
    const eventsText = JSON.stringify(events)
    if (eventsText !== dataText.current.events) {
      dataText.current.events = eventsText
      latestEvents.current = withTemporalDates(events)
    }
    if (previewOnRef.current) {
      try {
        await loadPreview()
        setPreviewError('')
      } catch (err) {
        console.error('Preview failed:', err)
        setPreviewError("Couldn't load the preview.")
      }
    }
    if (seq !== loadSeq.current) return
    if (drawSignature() !== drawnFrom.current) pushEvents()
    setStatus('ready')
  }

  // Initial load on mount.
  useEffect(() => {
    let cancelled = false
    refreshSummary()
    reloadCalendar()
      .then(() => {
        if (!cancelled) setStatus('ready')
      })
      .catch((err) => {
        if (cancelled) return
        console.error('Failed to load events:', err)
        setErrorMessage(err.message)
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The afternoon pane renders after the first fetch may have finished.
  useEffect(() => {
    try {
      pmEvents.set(eventsService.getAll())
      pmEvents.setBackgroundEvents(freeTimeBackground(eventsService.getAll(), hoursRef.current, gridRef.current.awake))
    } catch {
      /* still not rendered */
    }
  }, [status, pmEvents, eventsService])

  // The stacks depend on the hour height, and the month views take the events
  // unstacked: lay them out again when either changes.
  const isMonth = nav.view === 'month'
  useEffect(() => {
    if (status === 'ready' && laidOut.current !== layoutKey()) pushEvents()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMonth, hourPx, status])

  async function handleSyncNow() {
    setSyncing(true)
    const started = Date.now()
    try {
      await Promise.all([reloadCalendar(), refreshSummary()])
      setStatus('ready')
    } catch (err) {
      console.error('Sync failed:', err)
      setErrorMessage(err.message)
      setStatus('error')
    } finally {
      // Keep the spinner visible long enough to register as feedback.
      setTimeout(() => setSyncing(false), Math.max(0, 500 - (Date.now() - started)))
    }
  }

  // After a decision the calendar and the badges change. The Events/Plan tabs
  // already report their fresh summary through onSummary, so they only need
  // the calendar reloaded; the slot sheet needs both.
  const handleTabChanged = useCallback(() => {
    reloadCalendar().catch((err) => console.error('Calendar refresh failed:', err))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const handleCalendarChanged = useCallback(() => {
    handleTabChanged()
    refreshSummary()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSummary = useCallback((next) => {
    if (next) setSummary(next)
  }, [])

  // "Move" in the details sheet: save the approved block's new times (the
  // backend refuses synced events), then redraw both calendars.
  detailsCtx.current = {
    async move(eventId, times) {
      await postJson(`/events/${encodeURIComponent(eventId)}/move`, times)
      await reloadCalendar()
    },
    // "Delete": the block leaves the calendar and its proposal is rejected (with its travel).
    async remove(eventId) {
      await postJson(`/events/${encodeURIComponent(eventId)}/delete`, {})
      await reloadCalendar()
    },
    close() {
      for (const modal of [eventModal, pmModal]) {
        try {
          modal.close()
        } catch {
          /* that calendar has no open sheet */
        }
      }
    },
  }

  async function setPreview(on) {
    previewOnRef.current = on
    setPreviewOn(on)
    storePreview(on)
    setPreviewError('')
    if (on) {
      try {
        await loadPreview()
      } catch (err) {
        console.error('Preview failed:', err)
        setPreviewError("Couldn't load the preview.")
      }
    }
    pushEvents()
  }

  // A deep link to the preview should land where there is something to see:
  // if the current day/week/month has no proposals, go to the first upcoming
  // date that has some (the view itself is kept).
  const revealPending = useRef(false)
  function revealPreview() {
    const dates = previewEvents.current
      .map((e) => (e.start instanceof Temporal.ZonedDateTime ? e.start.toPlainDate() : e.start))
      .sort(Temporal.PlainDate.compare)
    if (!revealPending.current || !dates.length) return
    revealPending.current = false
    const { view, date } = navRef.current
    const monday = date.subtract({ days: date.dayOfWeek - 1 })
    const [from, to] =
      view === 'day'
        ? [date, date]
        : view === 'week'
          ? [monday, monday.add({ days: 6 })]
          : [date.with({ day: 1 }), date.with({ day: date.daysInMonth })]
    const within = (d) => Temporal.PlainDate.compare(d, from) >= 0 && Temporal.PlainDate.compare(d, to) <= 0
    if (dates.some(within)) return
    goToDate(dates.find((d) => Temporal.PlainDate.compare(d, today()) >= 0) || dates[0])
  }

  // #/calendar?preview=1 (email, Plan tab) turns the preview on; ?preview=0 off.
  // The param is consumed so the URL stays #/calendar; the choice is remembered.
  useEffect(() => {
    if (tab !== 'calendar' || !('preview' in route.params)) return
    const { preview, ...rest } = route.params
    navigate('calendar', rest, { replace: true })
    const on = preview !== '0'
    revealPending.current = on
    if (on !== previewOnRef.current) setPreview(on)
    else if (on) revealPreview() // already on: reveal now, or once the load lands
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route])

  // The slot sheet belongs to the calendar tab.
  useEffect(() => {
    if (tab !== 'calendar') setSlot(null)
  }, [tab])

  // Fresh badges whenever the tab changes (decisions may have happened
  // elsewhere, e.g. on another device or by email).
  const firstTab = useRef(true)
  useEffect(() => {
    if (firstTab.current) {
      firstTab.current = false
      return
    }
    refreshSummary()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  // Coming back to the app (home-screen app resumed, tab refocused) after a
  // while: refresh quietly, and roll "today" over after midnight.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== 'visible') return
      setTodayDate((old) => (old.equals(today()) ? old : today()))
      if (Date.now() - lastLoadAt.current > 2 * 60 * 1000) {
        reloadCalendar().catch(() => {})
        refreshSummary()
      }
    }
    // The connection is back (the offline banner is up until a live read lands).
    function onOnline() {
      reloadCalendar().catch(() => {})
      refreshSummary()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)
    const timer = setInterval(() => setTodayDate((old) => (old.equals(today()) ? old : today())), 60 * 1000)
    // The backend syncs every source every 5 minutes; an open app follows
    // at the same pace instead of waiting for the next return to the foreground.
    const refetch = setInterval(() => {
      if (document.visibilityState !== 'visible') return
      reloadCalendar().catch(() => {})
      refreshSummary()
    }, REFETCH_WHILE_OPEN_MS)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onOnline)
      clearInterval(timer)
      clearInterval(refetch)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // --- Derived UI state.
  const pendingEvents = summary?.events?.pending ?? 0
  const pendingSeries =
    summary?.plan?.pending_series ?? summary?.plan?.series?.filter((s) => s.pending > 0).length ?? 0
  const badges = { events: pendingEvents, plan: pendingSeries }
  const pendingTotal = summary ? (summary.events?.pending ?? 0) + (summary.plan?.pending ?? 0) : previewCount
  const previewShown = previewOn ? (previewCount ?? pendingTotal) : pendingTotal
  const todayKey = todayDate.toString()
  const sectionTitle = tab === 'events' ? 'Events' : tab === 'plan' ? 'Plan' : tab === 'hours' ? 'Hours' : 'Calendar'

  return (
    <div class={`app-shell tab-${tab}`}>
      {/* Today's column/cell tint: Schedule-X marks only the date header, so
          target the day by its data attribute. Its current-time plugin also
          leaves the full-width "now" line behind after moving off today. */}
      <style>{`.calendar-area.view-week [data-time-grid-date="${todayKey}"],.calendar-area .sx__month-grid-day[data-date="${todayKey}"]{background-color:var(--app-today-tint)}.calendar-area .sx__week-grid:not(:has([data-time-grid-date="${todayKey}"])) .sx__current-time-indicator-full-week{display:none}`}</style>

      <header class="app-header">
        <a class="brand" href="#/calendar" aria-label="Calendar">
          <span class="brand-mark">
            <CalendarIcon size={17} />
          </span>
          <span class="brand-name">Calendar</span>
        </a>
        <h1 class="page-title">{sectionTitle}</h1>
        <TabBar variant="top" tab={tab} badges={badges} />
        <div class="header-end" />
      </header>

      <OfflineBanner onRetry={handleSyncNow} busy={syncing} />

      <main class="app-main">
        <section class="tab-pane pane-calendar" hidden={!calendarVisible} aria-label="Calendar">
          <div
            class={`calendar-area view-${nav.view}${split ? ' is-split' : ''}${isPhone ? ' is-phone' : ''}${
              previewOn ? ' has-preview' : ''
            }`}
          >
            <CalendarNav
              view={nav.view}
              date={nav.date}
              today={todayDate}
              onDate={goToDate}
              onView={goToView}
              compact={isPhone}
              syncing={syncing}
              onSync={handleSyncNow}
              preview={{ on: previewOn, count: previewShown, disabled: !previewOn && pendingTotal === 0 }}
              onPreview={setPreview}
            />
            {previewOn && (
              <p class={`preview-banner${previewError ? ' is-error' : ''}`} role="status">
                {previewError ? (
                  <span>{previewError}</span>
                ) : (
                  <span>
                    <span class="preview-swatch" aria-hidden="true" />
                    {previewShown ? `${previewShown} proposed ` : 'Proposed '}
                    <span class="preview-banner-long">blocks shown dashed: nothing is added until you approve it.</span>
                    <span class="preview-banner-short">· dashed until approved</span>
                  </span>
                )}
                <a href="#/plan">Review plan</a>
              </p>
            )}
            {status === 'error' && (
              <div class="error-card" role="alert">
                <div>
                  <strong>Couldn't load events.</strong> The backend at <code>{API_URL || window.location.origin}</code>{' '}
                  didn't answer ({errorMessage}).
                </div>
                <button type="button" class="btn" onClick={handleSyncNow} disabled={syncing}>
                  Try again
                </button>
              </div>
            )}
            <div class="calendar-panes" ref={panesRef}>
              {/* Each pane sets its own grid and hour heights: Schedule-X writes
                  them to page-wide CSS variables, which two calendars would fight over. */}
              <div
                class="calendar-pane pane-main"
                ref={mainPaneRef}
                style={{
                  '--sx-week-grid-height': `${hoursIn(mainHours) * hourPx}px`,
                  '--sx-week-grid-hour-height': `${hourPx}px`,
                }}
              >
                {split && (
                  <div class="pane-label">
                    Morning <span>{`${clockLabel(grid.morning.start)} – ${clockLabel(grid.morning.end)}`}</span>
                  </div>
                )}
                <ScheduleXCalendar calendarApp={calendar} customComponents={customComponents} />
              </div>
              {/* Always mounted (hidden outside the split day view) so it keeps its
                  events and date instead of re-initialising on every switch. */}
              <div
                class="calendar-pane pane-afternoon"
                hidden={!split}
                style={{
                  '--sx-week-grid-height': `${hoursIn(grid.afternoon) * hourPx}px`,
                  '--sx-week-grid-hour-height': `${hourPx}px`,
                }}
              >
                <div class="pane-label">
                  Afternoon <span>{`${clockLabel(grid.afternoon.start)} – ${clockLabel(grid.afternoon.end)}`}</span>
                </div>
                <ScheduleXCalendar calendarApp={afternoon} customComponents={customComponents} />
              </div>
            </div>
            {slot && (
              <SlotSheet
                apiUrl={API_URL}
                slot={slot}
                onClose={() => setSlot(null)}
                onPlaced={() => handleCalendarChanged()}
              />
            )}
          </div>
        </section>

        <section class="tab-pane pane-review pane-events" hidden={tab !== 'events'} aria-label="Events">
          <EventsTab
            apiUrl={API_URL}
            active={tab === 'events'}
            onCalendarChanged={handleTabChanged}
            onSummary={handleSummary}
            navigate={navigate}
          />
        </section>
        <section class="tab-pane pane-review pane-plan" hidden={tab !== 'plan'} aria-label="Plan">
          <PlanTab
            apiUrl={API_URL}
            active={tab === 'plan'}
            onCalendarChanged={handleTabChanged}
            onSummary={handleSummary}
            navigate={navigate}
          />
        </section>
        <section class="tab-pane pane-review pane-hours" hidden={tab !== 'hours'} aria-label="Hours">
          <HoursTab
            apiUrl={API_URL}
            hours={hoursStatus.hours}
            saved={hoursStatus.saved}
            savedAt={hoursStatus.saved_at}
            onSaved={(status) => {
              applyHours(status)
              pushEvents()
            }}
          />
        </section>
      </main>

      <TabBar variant="bottom" tab={tab} badges={badges} />
    </div>
  )
}
