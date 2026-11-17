// The Hours tab's document (backend/hours.py is the source of truth, GET/PUT
// /hours): sleep and work hours for weekdays and weekends, home and office
// days, and the planning settings. Free time is *derived* (since 2026-10-26):
// after work until bed on a weekday (from waking when there is no work), the
// whole awake day at the weekend. Weekends never have work.

export const DEFAULT_HOURS = {
  home: '',
  weekdays: {
    sleep: { start: '23:00', end: '07:00' },
    work: { start: '09:00', end: '17:00', location: '', office_days: [] },
  },
  weekends: {
    sleep: { start: '23:00', end: '08:00' },
    work: null,
  },
  free_hours_per_week: 10,
  calendar_review_minutes: 20,
  goals_review_every_months: 1,
  goals_review_minutes: 30,
  event_keywords: [],
}

export const PROFILES = ['weekdays', 'weekends']
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
export const NEW_WORK = { start: '09:00', end: '17:00', location: '', office_days: [] }

export const toMinutes = (hhmm) => {
  const [h, m] = String(hhmm || '').split(':').map(Number)
  return Number.isFinite(h) && Number.isFinite(m) ? h * 60 + m : NaN
}

/** Minutes a range covers; a sleep range that crosses midnight wraps. */
export function rangeMinutes(range, { wraps = false } = {}) {
  if (!range) return 0
  const diff = toMinutes(range.end) - toMinutes(range.start)
  if (Number.isNaN(diff)) return 0
  return wraps ? (diff + 1440) % 1440 : diff
}

/** The profile that applies on a Temporal date (dayOfWeek 6 = Saturday, 7 = Sunday). */
export const profileFor = (date) => (date.dayOfWeek >= 6 ? 'weekends' : 'weekdays')

/** The clock time the awake day ends: bedtime, or '24:00' when bed is after midnight. */
export const bedtimeEnd = (sleep) => (toMinutes(sleep.start) > toMinutes(sleep.end) ? sleep.start : '24:00')

/** The derived free window of a profile (mirrors backend/hours.py free_range). */
export function freeRange(profile) {
  const start = profile.work ? profile.work.end : profile.sleep.end
  let end = bedtimeEnd(profile.sleep)
  if (toMinutes(end) <= toMinutes(start)) end = start
  return { start, end }
}

/**
 * Mirrors backend/hours.py validate(): returns an error message or ''. The
 * server checks again; this only saves a round trip for the common slips.
 */
export function validateHours(hours) {
  const isTime = (v) => /^([01]\d|2[0-3]):[0-5]\d$/.test(v || '')
  for (const profile of PROFILES) {
    const p = hours?.[profile]
    if (!p) return `${profile} is missing`
    const sleep = p.sleep
    if (!sleep || !isTime(sleep.start) || !isTime(sleep.end)) return `${label(profile)}: sleep needs a bedtime and a wake-up time`
    if (sleep.start === sleep.end) return `${label(profile)}: bed and wake-up are the same time`
    const work = profile === 'weekdays' ? p.work : null
    if (work) {
      if (!isTime(work.start) || !isTime(work.end)) return 'Work needs a start and an end time'
      if (work.start === work.end) return 'Work starts and ends at the same time'
      if (toMinutes(work.end) < toMinutes(work.start)) return 'Work ends before it starts'
    }
  }
  const free = Number(hours.free_hours_per_week)
  if (!Number.isFinite(free) || free < 0 || free > 100) return 'Free time must be between 0 and 100 hours a week'
  const review = Number(hours.calendar_review_minutes)
  if (!Number.isInteger(review) || review < 5 || review > 180) return 'The calendar review must be 5 to 180 whole minutes'
  const months = Number(hours.goals_review_every_months)
  if (!Number.isInteger(months) || months < 1 || months > 12) return 'The goals review cadence must be 1 to 12 whole months'
  const goalsMin = Number(hours.goals_review_minutes)
  if (!Number.isInteger(goalsMin) || goalsMin < 5 || goalsMin > 240) return 'The goals review must be 5 to 240 whole minutes'
  return ''
}

export const label = (profile) => (profile === 'weekends' ? 'Weekends' : 'Weekdays')

export const sameHours = (a, b) => JSON.stringify(a) === JSON.stringify(b)

/** '23:00' -> '11 PM', '08:15' -> '8:15 AM', '24:00' -> 'midnight'. */
export function clock12(hhmm) {
  if (hhmm === '24:00' || hhmm === '00:00') return 'midnight'
  const [h, m] = hhmm.split(':').map(Number)
  const hour = h % 12 || 12
  return `${hour}${m ? `:${String(m).padStart(2, '0')}` : ''} ${h < 12 ? 'AM' : 'PM'}`
}

/** '8 h', '7 h 30 min'. */
export function describeMinutes(minutes) {
  if (!minutes) return ''
  const h = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (!h) return `${rest} min`
  return rest ? `${h} h ${rest} min` : `${h} h`
}

/** 'jazz, AI talks' <-> ['jazz', 'AI talks'] (de-duplicated, trimmed). */
export function parseKeywords(text) {
  const out = []
  const seen = new Set()
  for (const raw of String(text || '').split(',')) {
    const word = raw.trim().replace(/\s+/g, ' ')
    if (!word || seen.has(word.toLowerCase())) continue
    seen.add(word.toLowerCase())
    out.push(word)
  }
  return out
}
