// Our own layout pass over the timed events before they reach Schedule-X's
// Day and Week grids (never the month views), run from pushEvents in app.jsx
// with the current hour height. Two problems it solves (2026-11-16):
//
// 1. Short blocks back to back. A block is drawn at least MIN_BLOCK_PX tall
//    (app.css), so at 32 px an hour a 15- or 30-minute block spills over the
//    one after it. A run of such tight blocks that would collide becomes one
//    stack card (calendarId of its members, or `stack` when they are mixed)
//    listing them one per line; the details sheet lists the members and opens
//    each one (EventDetails.jsx). A tight block that collides with a block it
//    cannot stack with is drawn at its true height instead (`is-squeezed`).
//    Proposals (preview) never stack.
//
// 2. Overlaps. Schedule-X gives every event of an overlapping cluster the
//    same width for its whole length. When the cluster's longest event
//    overlaps all the others, it keeps the full width (`ov-base`) and the
//    others sit side by side on top of its right side (`ov-nest ov-c-<col>
//    ov-n-<cols>`); app.css turns the classes into left/width. Other clusters
//    keep Schedule-X's side-by-side columns.

export const MIN_BLOCK_PX = 19 // .sx__time-grid-event min-height in app.css
export const STACK_LINE_PX = 12 // line-height of a stack card's lines (app.css .is-stack)
const STACK_PAD_PX = 4
const MAX_NEST_COLUMNS = 4

const cmp = (a, b) => Temporal.ZonedDateTime.compare(a, b)
const minutes = (from, to) => to.since(from).total({ unit: 'minutes' })
const isTimed = (e) => e.start instanceof Temporal.ZonedDateTime && minutes(e.start, e.end) < 24 * 60
const isProposed = (e) => e.calendarId === 'proposed'
const overlaps = (a, b) => cmp(a.start, b.end) < 0 && cmp(b.start, a.end) < 0
const withClasses = (e, classes) => ({
  ...e,
  _options: { ...e._options, additionalClasses: [...(e._options?.additionalClasses || []), ...classes] },
})

/** Events for the Day/Week grids at `hourPx` pixels an hour. Pure: returns a new list. */
export function layoutTimeGrid(events, hourPx) {
  const px = (from, to) => (minutes(from, to) * hourPx) / 60
  const tight = (e) => px(e.start, e.end) < MIN_BLOCK_PX
  // Would e, drawn at least MIN_BLOCK_PX tall, run into `next`?
  const collides = (e, next) => px(e.start, next.start) < MIN_BLOCK_PX

  const byDay = new Map()
  const out = []
  for (const e of events) {
    if (!isTimed(e)) {
      out.push(e)
      continue
    }
    const key = e.start.toPlainDate().toString()
    if (!byDay.has(key)) byDay.set(key, [])
    byDay.get(key).push(e)
  }

  for (const day of byDay.values()) {
    day.sort((a, b) => cmp(a.start, b.start) || cmp(b.end, a.end))
    const classes = new Map(day.map((e) => [e, []]))
    const clustered = new Set()

    // --- Overlap clusters (connected by real overlap).
    let cluster = []
    let clusterEnd = null
    const closeCluster = () => {
      if (cluster.length > 1) {
        cluster.forEach((e) => clustered.add(e))
        nest(cluster, classes)
      }
      cluster = []
      clusterEnd = null
    }
    for (const e of day) {
      if (cluster.length && cmp(e.start, clusterEnd) >= 0) closeCluster()
      cluster.push(e)
      if (!clusterEnd || cmp(e.end, clusterEnd) > 0) clusterEnd = e.end
    }
    closeCluster()

    // --- Runs of tight blocks.
    const stackable = (e) => !clustered.has(e) && !isProposed(e) && tight(e)
    const stacked = new Set()
    const stacks = []
    for (let i = 0; i < day.length; i++) {
      const e = day[i]
      if (!stackable(e)) continue
      const run = [e]
      while (i + 1 < day.length && stackable(day[i + 1]) && collides(run[run.length - 1], day[i + 1])) run.push(day[++i])
      const next = day[i + 1]
      if (run.length === 1) {
        if (next && collides(e, next)) classes.get(e).push('is-squeezed')
        continue
      }
      run.forEach((m) => stacked.add(m))
      // A run can still be shorter than one line (two 6-minute blocks).
      const squeezed = px(run[0].start, run[run.length - 1].end) < MIN_BLOCK_PX && next && collides(run[0], next)
      stacks.push(stackEvent(run, hourPx, squeezed))
    }

    for (const e of day) {
      if (stacked.has(e)) continue
      const extra = classes.get(e)
      out.push(extra.length ? withClasses(e, extra) : e)
    }
    out.push(...stacks)
  }
  return out
}

// The longest event keeps the full width when it overlaps every other member;
// the rest get greedy columns over its right side.
function nest(cluster, classes) {
  const base = cluster.reduce((best, e) => (minutes(e.start, e.end) > minutes(best.start, best.end) ? e : best))
  const others = cluster.filter((e) => e !== base)
  if (!others.every((e) => overlaps(e, base))) return
  const columns = [] // per column: the events placed in it
  const columnOf = new Map()
  for (const e of others) {
    let c = columns.findIndex((col) => col.every((x) => !overlaps(x, e)))
    if (c < 0) {
      c = columns.length
      columns.push([])
    }
    columns[c].push(e)
    columnOf.set(e, c)
  }
  if (columns.length > MAX_NEST_COLUMNS) return
  classes.get(base).push('ov-base')
  for (const e of others) classes.get(e).push('ov-nest', `ov-c-${columnOf.get(e)}`, `ov-n-${columns.length}`)
}

// One card for a run of tight blocks: its members one per line, as many as
// fit, the rest as "+N more".
function stackEvent(run, hourPx, squeezed) {
  const first = run[0]
  const last = run[run.length - 1]
  const heightPx = Math.max(MIN_BLOCK_PX, (minutes(first.start, last.end) * hourPx) / 60)
  const lines = Math.max(1, Math.floor((heightPx - STACK_PAD_PX) / STACK_LINE_PX))
  const titles = run.map((m) => m.title || '(untitled)')
  const shown =
    titles.length <= lines
      ? titles
      : lines === 1
        ? [`${titles[0]} +${titles.length - 1}`]
        : [...titles.slice(0, lines - 1), `+${titles.length - lines + 1} more`]
  const calendarIds = new Set(run.map((m) => m.calendarId))
  const classes = ['is-stack']
  if (squeezed) classes.push('is-squeezed')
  if (run.some((m) => m.conflict)) classes.push('has-conflict')
  return {
    id: `stack-${first.id}`,
    calendarId: calendarIds.size === 1 ? first.calendarId : 'stack',
    title: shown.join('\n'),
    start: first.start,
    end: last.end,
    stack: run,
    _options: { additionalClasses: classes, disableDND: true, disableResize: true },
  }
}
