/**
 * Drag to move (and, with a mouse, drag the bottom edge to resize) approved
 * planned blocks in the Day and Week time grids. Schedule-X 4's own
 * drag-and-drop and resize are paid plugins, so this is a thin layer of our
 * own on top of its markup (4.9.1):
 *
 *   - every timed event is `.sx__time-grid-event[data-event-id]`;
 *   - every day column is `.sx__time-grid-day[data-time-grid-date="YYYY-MM-DD"]`,
 *     its height spanning the pane's dayBoundaries.
 *
 * Nothing in Schedule-X's DOM is changed while dragging: a fixed-position
 * ghost follows the pointer, so a refetch mid-drag cannot break it. Only
 * events whose calendarId is 'optimizer' move (the app never writes to the
 * synced calendars). The drop goes through opts.onMove, the same
 * POST /events/{id}/move the details sheet's Move button uses.
 *
 * Mouse: press and move 4 px to drag; press the bottom 7 px to resize. A plain
 * click still opens the details sheet. Escape cancels.
 * Touch: hold still for LONG_PRESS_MS, then drag. A swipe or scroll that
 * moves first is left alone (the day swipe and the grid scroll keep working).
 *
 * After a drag, the mouseup/touchend and the click are stopped at the window
 * (capture) so Schedule-X neither opens the details sheet nor treats the drop
 * as a tap on the grid, and the swipe handler never sees it.
 *
 * Times: the drawn block may be clipped to the awake window (fitToAwakeWindow
 * in app.jsx keeps the real times in realStart/realEnd). A move shifts the
 * real start and end by the pointer's travel, with the start snapped to
 * SNAP_MIN; a resize sets the real end and is refused for a block whose end
 * is clipped.
 */

const SNAP_MIN = 15
const MOUSE_SLOP_PX = 4
const TOUCH_SLOP_PX = 10
const LONG_PRESS_MS = 400
const RESIZE_EDGE_PX = 7
const AUTOSCROLL_EDGE_PX = 44
const AUTOSCROLL_STEP_PX = 10
const TOUCH_MOUSE_GAP_MS = 800 // compat mouse events after a touch are ignored

const toMin = (hhmm) => {
  const [h, m] = hhmm.split(':').map(Number)
  return h * 60 + m
}

// An ISO string with an offset (realStart) or a ZonedDateTime -> ZonedDateTime in tz.
function zoned(value, tz) {
  if (value instanceof Temporal.ZonedDateTime) return value.withTimeZone(tz)
  return Temporal.Instant.from(value).toZonedDateTimeISO(tz)
}

// What POST /events/{id}/move takes: naive New York wall time.
const naive = (zdt) => `${zdt.toPlainDateTime().toString({ smallestUnit: 'minute' })}:00`

// Rounds the wall-clock time, so a drop on a DST-change day lands where it was dropped.
const snap = (zdt) => zdt.round({ smallestUnit: 'minute', roundingIncrement: SNAP_MIN })

const clock = (zdt) => zdt.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit' })

// "1:15 – 4 PM" style, short enough for a narrow week column.
function rangeLabel(start, end) {
  const a = clock(start)
  const b = clock(end)
  const sameHalf = a.slice(-2) === b.slice(-2) && start.toPlainDate().equals(end.toPlainDate())
  return `${sameHalf ? a.slice(0, -3) : a} – ${b}`
}
const dayLabel = (zdt) => zdt.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })

/**
 * @param {HTMLElement} root  the element holding both calendar panes
 * @param {{
 *   timezone: string,
 *   getEvent: (id: string) => object | undefined,   // the event as drawn (after fitToAwakeWindow)
 *   getBounds: (pane: HTMLElement) => {start: string, end: string} | null,
 *   onStart?: () => void,                            // close sheets etc.
 *   onMove: (id: string, times: {start: string, end: string}) => Promise<void>,
 * }} opts
 * @returns {() => void} detach
 */
export function attachDragMove(root, opts) {
  const tz = opts.timezone
  let pending = null // a press on a movable block that has not become a drag yet
  let drag = null // the active drag
  let lastTouchAt = 0
  let suppressClickUntil = 0
  let toastTimer = null

  // --- Finding things -------------------------------------------------------

  function movableTarget(target) {
    const el = target instanceof Element ? target.closest('.sx__time-grid-event[data-event-id]') : null
    if (!el || !root.contains(el) || el.classList.contains('is-event-copy')) return null
    const ev = opts.getEvent(el.getAttribute('data-event-id'))
    if (!ev || ev.calendarId !== 'optimizer' || !(ev.start instanceof Temporal.ZonedDateTime)) return null
    const pane = el.closest('.calendar-pane')
    const column = el.closest('.sx__time-grid-day[data-time-grid-date]')
    const bounds = pane && opts.getBounds(pane)
    if (!column || !bounds) return null
    return { el, ev, pane, column, bounds }
  }

  const columnsOf = (pane) => Array.from(pane.querySelectorAll('.sx__time-grid-day[data-time-grid-date]'))

  // The column under x (clamped to the first/last), in the pane the drag started in.
  function columnAt(x) {
    const cols = columnsOf(drag.pane)
    if (!cols.length) return drag.column
    let best = cols[0]
    for (const col of cols) {
      const r = col.getBoundingClientRect()
      if (x >= r.left) best = col
    }
    return best
  }

  // Minutes from midnight at viewport y, measured on a column of the pane.
  function minuteAt(y, column) {
    const r = column.getBoundingClientRect()
    return drag.boundsStart + ((y - r.top) / r.height) * drag.span
  }

  // --- Ghost and toast ------------------------------------------------------

  function makeGhost(src) {
    const ghost = document.createElement('div')
    ghost.className = 'drag-ghost'
    const style = getComputedStyle(src)
    ghost.style.setProperty('--ghost-fill', style.backgroundColor)
    ghost.style.setProperty('--ghost-ink', style.color)
    ghost.style.setProperty('--ghost-bar', style.borderInlineStartColor || style.borderLeftColor)
    const title = document.createElement('div')
    title.className = 'drag-ghost-title'
    title.textContent = drag.ev.title || ''
    const time = document.createElement('div')
    time.className = 'drag-ghost-time'
    const day = document.createElement('div')
    day.className = 'drag-ghost-day'
    ghost.append(title, time, day)
    document.body.appendChild(ghost)
    return ghost
  }

  function toast(message) {
    let el = document.querySelector('.drag-toast')
    if (!el) {
      el = document.createElement('div')
      el.className = 'drag-toast'
      el.setAttribute('role', 'alert')
      document.body.appendChild(el)
    }
    el.textContent = message
    el.hidden = false
    clearTimeout(toastTimer)
    toastTimer = setTimeout(() => {
      el.hidden = true
    }, 5000)
  }

  // --- The drag -------------------------------------------------------------

  function begin(press, x, y) {
    const { el, ev, pane, column, bounds } = press
    const elRect = el.getBoundingClientRect()
    const boundsStart = toMin(bounds.start)
    const span = toMin(bounds.end) - boundsStart
    const realStart = zoned(ev.realStart || ev.start, tz)
    const realEnd = zoned(ev.realEnd || ev.end, tz)
    drag = {
      mode: press.mode,
      id: el.getAttribute('data-event-id'),
      ev,
      el,
      pane,
      column,
      boundsStart,
      span,
      realStart,
      realEnd,
      startDate: Temporal.PlainDate.from(column.getAttribute('data-time-grid-date')),
      x,
      y,
      newStart: realStart,
      newEnd: realEnd,
      targetColumn: column,
      scroller: pane.querySelector('.sx__view-container'),
      raf: 0,
      saving: false,
    }
    const colRect = column.getBoundingClientRect()
    // Where the drawn block sits in the column, in minutes from midnight.
    drag.drawnTop = boundsStart + ((elRect.top - colRect.top) / colRect.height) * span
    drag.drawnBottom = boundsStart + ((elRect.bottom - colRect.top) / colRect.height) * span
    drag.pressMinute = minuteAt(y, column)
    drag.ghost = makeGhost(el)
    // Auto-scroll steps must land at once, not animate (the grid scrolls smoothly).
    if (drag.scroller) drag.scroller.style.scrollBehavior = 'auto'
    el.classList.add('is-drag-source')
    document.documentElement.classList.add(drag.mode === 'resize' ? 'is-resizing-event' : 'is-dragging-event')
    opts.onStart?.()
    update()
    tick()
  }

  function update() {
    const d = drag
    const end = d.boundsStart + d.span
    const travel = minuteAt(d.y, d.column) - d.pressMinute
    if (d.mode === 'resize') {
      // New real end, snapped; at least one step long, inside the drawn window.
      const maxTravel = end - d.drawnBottom
      let newEnd = snap(d.realEnd.add({ minutes: Math.round(Math.min(travel, maxTravel)) }))
      const minEnd = d.realStart.add({ minutes: SNAP_MIN })
      if (Temporal.ZonedDateTime.compare(newEnd, minEnd) < 0) newEnd = minEnd
      while (d.drawnBottom + newEnd.since(d.realEnd).total('minutes') > end + 0.01) newEnd = newEnd.subtract({ minutes: SNAP_MIN })
      d.newStart = d.realStart
      d.newEnd = newEnd
      d.targetColumn = d.column
    } else {
      d.targetColumn = columnAt(d.x)
      const dayShift = Temporal.PlainDate.from(d.targetColumn.getAttribute('data-time-grid-date')).since(d.startDate).days
      // Keep the drawn block inside the column, then snap the real start.
      const lo = d.boundsStart - d.drawnTop
      const hi = end - d.drawnBottom
      const clamped = Math.max(lo, Math.min(hi, travel))
      let newStart = snap(d.realStart.add({ days: dayShift }).add({ minutes: Math.round(clamped) }))
      const shift = () => newStart.since(d.realStart.add({ days: dayShift })).total('minutes')
      while (shift() > hi + 0.01) newStart = newStart.subtract({ minutes: SNAP_MIN })
      while (shift() < lo - 0.01) newStart = newStart.add({ minutes: SNAP_MIN })
      d.newStart = newStart
      d.newEnd = d.realEnd.add({ days: dayShift }).add({ minutes: shift() })
      d.minuteShift = shift()
    }
    draw()
  }

  function draw() {
    const d = drag
    const col = d.targetColumn.getBoundingClientRect()
    const pxPerMin = col.height / d.span
    let top
    let bottom
    if (d.mode === 'resize') {
      top = d.drawnTop
      bottom = d.drawnBottom + d.newEnd.since(d.realEnd).total('minutes')
    } else {
      top = d.drawnTop + d.minuteShift
      bottom = d.drawnBottom + d.minuteShift
    }
    const g = d.ghost
    g.style.left = `${col.left + 2}px`
    g.style.width = `${Math.max(24, col.width - 4)}px`
    g.style.top = `${col.top + (top - d.boundsStart) * pxPerMin}px`
    g.style.height = `${Math.max(18, (bottom - top) * pxPerMin)}px`
    const otherDay = !d.newStart.toPlainDate().equals(d.realStart.toPlainDate())
    g.querySelector('.drag-ghost-time').textContent = d.saving ? 'Saving…' : rangeLabel(d.newStart, d.newEnd)
    // The day only when the block changes day (the week's column headers say the rest).
    g.querySelector('.drag-ghost-day').textContent = otherDay && !d.saving ? dayLabel(d.newStart) : ''
  }

  // Auto-scroll a scrolling grid (phones) when the pointer nears its edge.
  function tick() {
    if (!drag || drag.saving) return
    const s = drag.scroller
    if (s && s.scrollHeight > s.clientHeight + 1) {
      const r = s.getBoundingClientRect()
      let dy = 0
      if (drag.y < r.top + AUTOSCROLL_EDGE_PX) dy = -AUTOSCROLL_STEP_PX
      else if (drag.y > r.bottom - AUTOSCROLL_EDGE_PX) dy = AUTOSCROLL_STEP_PX
      if (dy) {
        const before = s.scrollTop
        s.scrollTop += dy
        if (s.scrollTop !== before) update()
      }
    }
    drag.raf = requestAnimationFrame(tick)
  }

  function cleanup() {
    if (!drag) return
    cancelAnimationFrame(drag.raf)
    drag.ghost.remove()
    if (drag.scroller) drag.scroller.style.scrollBehavior = ''
    drag.el.classList.remove('is-drag-source')
    document.documentElement.classList.remove('is-dragging-event', 'is-resizing-event')
    // The column's mouseDownOnChild flag is cleared on its mouseup, which we
    // stopped; without this its next tap (a Free band) would be ignored.
    try {
      drag.column.dispatchEvent(new MouseEvent('mouseleave'))
    } catch {
      /* old browser: the flag clears when the pointer leaves the column */
    }
    drag = null
  }

  async function finish() {
    const d = drag
    const unchanged =
      Temporal.ZonedDateTime.compare(d.newStart, d.realStart) === 0 &&
      Temporal.ZonedDateTime.compare(d.newEnd, d.realEnd) === 0
    if (unchanged) return cleanup()
    d.saving = true
    cancelAnimationFrame(d.raf)
    d.ghost.classList.add('is-saving')
    draw()
    try {
      await opts.onMove(d.id, { start: naive(d.newStart), end: naive(d.newEnd) })
    } catch (err) {
      toast(`Couldn't move ${d.ev.title || 'it'}: ${err?.message || err}`)
    } finally {
      if (drag === d) cleanup()
    }
  }

  // --- Mouse ----------------------------------------------------------------

  function onMouseDown(e) {
    if (e.button !== 0 || drag || Date.now() - lastTouchAt < TOUCH_MOUSE_GAP_MS) return
    const press = movableTarget(e.target)
    if (!press) return
    const r = press.el.getBoundingClientRect()
    const clippedEnd = !!press.ev.clipNote?.includes('Runs until') // the drawn end is not the real one
    press.mode = e.clientY >= r.bottom - RESIZE_EDGE_PX && !clippedEnd ? 'resize' : 'move'
    pending = { ...press, x: e.clientX, y: e.clientY, kind: 'mouse' }
    e.preventDefault() // no text selection while dragging; the click still fires
  }

  function onMouseMove(e) {
    if (drag && drag.saving) return
    if (drag) {
      drag.x = e.clientX
      drag.y = e.clientY
      update()
      return
    }
    if (!pending || pending.kind !== 'mouse') return
    if (Math.hypot(e.clientX - pending.x, e.clientY - pending.y) < MOUSE_SLOP_PX) return
    const press = pending
    pending = null
    begin(press, press.x, press.y)
    drag.x = e.clientX
    drag.y = e.clientY
    update()
  }

  function onMouseUp(e) {
    if (pending?.kind === 'mouse') pending = null // a plain click: Schedule-X opens the sheet
    if (!drag || drag.saving) return
    e.stopPropagation()
    e.preventDefault()
    suppressClickUntil = Date.now() + 400
    finish()
  }

  function onClick(e) {
    if (Date.now() < suppressClickUntil) {
      e.stopPropagation()
      e.preventDefault()
      suppressClickUntil = 0
    }
  }

  function onKeyDown(e) {
    if (e.key === 'Escape' && drag && !drag.saving) {
      suppressClickUntil = Date.now() + 400
      cleanup()
    }
  }

  // --- Touch: long press, then drag ----------------------------------------

  function onTouchStart(e) {
    lastTouchAt = Date.now()
    if (drag || e.touches.length !== 1) {
      cancelPending()
      return
    }
    const press = movableTarget(e.target)
    if (!press) return
    const t = e.touches[0]
    press.mode = 'move'
    pending = { ...press, x: t.clientX, y: t.clientY, kind: 'touch' }
    pending.timer = setTimeout(() => {
      if (!pending || pending.kind !== 'touch') return
      const p = pending
      pending = null
      begin(p, p.x, p.y)
      navigator.vibrate?.(12)
    }, LONG_PRESS_MS)
  }

  function cancelPending() {
    if (pending?.timer) clearTimeout(pending.timer)
    pending = null
  }

  function onTouchMove(e) {
    lastTouchAt = Date.now()
    const t = e.touches[0]
    if (drag) {
      if (e.cancelable) e.preventDefault() // hold the page still: this finger is dragging
      if (drag.saving || !t) return
      drag.x = t.clientX
      drag.y = t.clientY
      update()
      return
    }
    // Moved before the long press fired: it is a scroll or a swipe.
    if (pending?.kind === 'touch' && t && Math.hypot(t.clientX - pending.x, t.clientY - pending.y) > TOUCH_SLOP_PX) {
      cancelPending()
    }
  }

  function onTouchEnd(e) {
    lastTouchAt = Date.now()
    if (pending?.kind === 'touch') cancelPending() // a tap: Schedule-X opens the sheet
    if (!drag || drag.saving) return
    // Keep the drop away from Schedule-X (sheet), the grid (slot sheet) and
    // the swipe handler; no compat mouse events either.
    e.stopPropagation()
    if (e.cancelable) e.preventDefault()
    suppressClickUntil = Date.now() + 600
    if (e.type === 'touchcancel') cleanup()
    else finish()
  }

  function onContextMenu(e) {
    if (drag || pending?.kind === 'touch') e.preventDefault()
  }

  // Capture on the window so these run before Schedule-X's own handlers.
  const listeners = [
    ['mousedown', onMouseDown, { capture: true }],
    ['mousemove', onMouseMove, { capture: true }],
    ['mouseup', onMouseUp, { capture: true }],
    ['click', onClick, { capture: true }],
    ['keydown', onKeyDown, { capture: true }],
    ['touchstart', onTouchStart, { capture: true, passive: true }],
    ['touchmove', onTouchMove, { capture: true, passive: false }],
    ['touchend', onTouchEnd, { capture: true, passive: false }],
    ['touchcancel', onTouchEnd, { capture: true, passive: false }],
    ['contextmenu', onContextMenu, { capture: true }],
  ]
  for (const [type, fn, options] of listeners) window.addEventListener(type, fn, options)
  return () => {
    cancelPending()
    cleanup()
    clearTimeout(toastTimer)
    for (const [type, fn, options] of listeners) window.removeEventListener(type, fn, options)
  }
}
