// Display helpers for the review tabs. Everything is shown in the user's zone
// (America/New_York), whatever zone the phone or browser happens to be in.

export const TZ = 'America/New_York'

const inZone = (options) => new Intl.DateTimeFormat('en-US', { timeZone: TZ, ...options })
const weekdayDate = inZone({ weekday: 'short', month: 'short', day: 'numeric' }) // "Thu, Oct 9"
const monthDay = inZone({ month: 'short', day: 'numeric' }) // "Oct 9"
const clock = inZone({ hour: 'numeric', minute: '2-digit' }) // "8:15 AM"
const isoDate = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })
const isoDateTime = new Intl.DateTimeFormat('en-CA', {
  timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

const isPlainDate = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)

// A plain date as an instant that falls on that date in New York (16:00 UTC is
// 11:00 or 12:00 there), so formatting it in TZ never slips a day.
function plainDateInstant(value) {
  const [y, m, d] = value.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d, 16))
}

// Held-back rows can carry times the validator refused, so every formatter
// tolerates a value that isn't a date (Intl throws on an invalid Date).
function toDate(value) {
  if (!value) return null
  const date = isPlainDate(value) ? plainDateInstant(value) : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 'YYYY-MM-DD' of an ISO date or datetime, in New York ('' if it isn't one). */
export function dayKey(value) {
  if (isPlainDate(value)) return value
  const date = toDate(value)
  return date ? isoDate.format(date) : ''
}

export function todayKey(now = new Date()) {
  return isoDate.format(now)
}

function addDays(key, days) {
  const date = plainDateInstant(key)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/** "Today", "Tomorrow" or "Thu, Oct 9". */
export function formatDay(value) {
  const key = dayKey(value)
  if (!key) return ''
  const today = todayKey()
  if (key === today) return 'Today'
  if (key === addDays(today, 1)) return 'Tomorrow'
  return weekdayDate.format(plainDateInstant(key))
}

/** "Thu, Oct 9" always (no Today/Tomorrow), for compact lists. */
export function formatWeekdayDate(value) {
  const key = dayKey(value)
  return key ? weekdayDate.format(plainDateInstant(key)) : ''
}

function clockParts(date) {
  const parts = clock.formatToParts(date)
  const hour = parts.find((p) => p.type === 'hour')?.value ?? ''
  const minute = parts.find((p) => p.type === 'minute')?.value ?? '00'
  const period = parts.find((p) => p.type === 'dayPeriod')?.value ?? ''
  return { text: minute === '00' ? hour : `${hour}:${minute}`, period }
}

/** "8:15 AM", "5 PM". */
export function formatClock(value) {
  const date = toDate(value)
  if (!date) return ''
  const { text, period } = clockParts(date)
  return `${text} ${period}`.trim()
}

/** "8:15 – 8:45 AM", "11:30 AM – 12:30 PM", "All day". */
export function formatTimeRange(p) {
  if (!p) return ''
  if (p.all_day) return 'All day'
  const startDate = toDate(p.start)
  const endDate = toDate(p.end)
  if (!startDate || !endDate) return [p.start, p.end].filter(Boolean).join(' – ')
  const start = clockParts(startDate)
  const end = clockParts(endDate)
  if (start.period === end.period) return `${start.text} – ${end.text} ${end.period}`.trim()
  return `${start.text} ${start.period} – ${end.text} ${end.period}`
}

/** "17:30" -> "5:30 PM"; anything that isn't HH:MM (e.g. "varies") is returned as is. */
export function formatHHMM(value) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value || '')
  if (!match) return value || ''
  const hours = Number(match[1])
  const period = hours < 12 ? 'AM' : 'PM'
  const h12 = hours % 12 === 0 ? 12 : hours % 12
  return match[2] === '00' ? `${h12} ${period}` : `${h12}:${match[2]} ${period}`
}

/** 30 -> "30 min", 180 -> "3 h", 90 -> "1 h 30 min" (as the email writes it). */
export function formatDuration(minutes) {
  const total = Math.round(Number(minutes))
  if (!Number.isFinite(total) || total <= 0) return ''
  if (total < 60) return `${total} min`
  const hours = Math.floor(total / 60)
  const rest = total % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

export function durationMinutes(p) {
  const start = toDate(p?.start)
  const end = toDate(p?.end)
  if (!start || !end || p.all_day) return 0
  return Math.round((end - start) / 60000)
}

/** "Oct 9", "Oct 9 – 30", "Oct 9 – Nov 7". */
export function formatDateRange(first, last) {
  const a = dayKey(first)
  const b = dayKey(last)
  if (!a) return ''
  const start = plainDateInstant(a)
  if (!b || a === b) return monthDay.format(start)
  const end = plainDateInstant(b)
  if (a.slice(0, 7) === b.slice(0, 7)) return `${monthDay.format(start)} – ${b.slice(8).replace(/^0/, '')}`
  return `${monthDay.format(start)} – ${monthDay.format(end)}`
}

/** "Oct 8, 8:44 AM" for run timestamps. */
export function formatStamp(value) {
  const date = toDate(value)
  return date ? `${monthDay.format(date)}, ${formatClock(value)}` : ''
}

/** The date and HH:MM of an instant in New York, for the time editor's inputs. */
export function zonedParts(value) {
  if (isPlainDate(value)) return { date: value, time: '09:00' }
  const date = toDate(value)
  if (!date) return { date: '', time: '' }
  const parts = isoDateTime.formatToParts(date)
  const get = (type) => parts.find((p) => p.type === type)?.value ?? '00'
  return { date: `${get('year')}-${get('month')}-${get('day')}`, time: `${get('hour')}:${get('minute')}` }
}

/** "0:45", "6:02" for live counters. */
export function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/** "Free", "$25" or '' (unknown). */
export function formatCost(cost) {
  const text = (cost || '').trim()
  if (!text) return ''
  return /^free$/i.test(text) ? 'Free' : text
}

const entityCache = new Map()
const INVISIBLE = /[\u00ad\u200b\u200c\u200d\ufeff]/g // soft hyphen, zero-width characters
/**
 * Titles extracted from web pages can carry HTML entities ("Sci&shy;ence").
 * The backend cleans new ones (review_model.clean_text); this covers rows
 * stored before that fix, the same way: entities decoded, invisible
 * characters dropped. A <textarea> parses its content as text only, so
 * nothing is executed.
 */
export function unescapeHtml(value) {
  if (!value) return ''
  if (!/&[#a-z0-9]+;/i.test(value)) return value.replace(INVISIBLE, '')
  if (entityCache.has(value)) return entityCache.get(value)
  const box = document.createElement('textarea')
  box.innerHTML = value
  const text = box.value.replace(INVISIBLE, '')
  entityCache.set(value, text)
  return text
}

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
