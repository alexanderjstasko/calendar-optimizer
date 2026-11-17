import { formatDateRange, formatDuration, durationMinutes, formatHHMM, unescapeHtml } from './format.js'
import { OccurrenceRow } from './OccurrenceRow.jsx'
import { LISTED } from './useReview.js'
import { Chevron, ConfirmButton, KindBadge, Switch } from './ui.jsx'

/**
 * Where a series stands, from the rows it still lists (pending + upcoming
 * approved) with any in-flight decisions applied. Travel follows its
 * destination: it is approved, revoked and rejected together with it.
 */
export function seriesState(series, statusOf) {
  const occurrences = (series.occurrences || [])
    .filter((o) => LISTED.has(statusOf(o)))
    .sort((a, b) => a.start.localeCompare(b.start))
  const pending = []
  const approved = []
  const approveIds = []
  const revokeIds = []
  const rejectIds = []
  for (const o of occurrences) {
    const travel = o.travel && LISTED.has(statusOf(o.travel)) ? o.travel : null
    if (statusOf(o) === 'proposed') {
      pending.push(o)
      approveIds.push(o.id)
      rejectIds.push(o.id)
      if (travel && statusOf(travel) === 'proposed') rejectIds.push(travel.id)
    } else {
      approved.push(o)
      revokeIds.push(o.id)
    }
    if (travel) (statusOf(travel) === 'proposed' ? approveIds : revokeIds).push(travel.id)
  }
  return { occurrences, pending, approved, approveIds, revokeIds, rejectIds }
}

/** The ids one occurrence's decision covers: itself plus its travel in the matching state. */
export function occurrenceIds(o, statusOf, travelStatus) {
  const ids = [o.id]
  if (o.travel && statusOf(o.travel) === travelStatus) ids.push(o.travel.id)
  return ids
}

/**
 * "daily 8:15 AM · 30 min · 24×", "Mon & Wed 5:30 PM · 3 h · 8× · + travel",
 * "every 3 days · varies · 15 min · 8×". Built from the series fields (the
 * server's `summary` is the same line with 24-hour times; it is the fallback).
 * The time joins the cadence ("daily 8:15 AM") except after "every …" and
 * "4× a week", as the server writes it.
 */
export function describeSeries(series, occurrences) {
  const cadence = series.cadence || ''
  if (!cadence) return series.summary || `${occurrences.length}×`
  const raw = series.time || ''
  const fuzzy = raw === 'varies' || raw === 'all day'
  const time = fuzzy ? raw : formatHHMM(raw)
  const joined = time && !fuzzy && !cadence.startsWith('every') && !cadence.includes('×')
  const parts = joined ? [`${cadence} ${time}`] : [cadence, time]
  if (raw !== 'all day') parts.push(formatDuration(series.duration_min ?? durationMinutes(occurrences[0])))
  parts.push(`${occurrences.length}×`)
  if (series.has_travel || occurrences.some((o) => o.travel)) parts.push('+ travel')
  return parts.filter(Boolean).join(' · ')
}

/**
 * One series. `view` follows the filter chip: in "review" the toggle covers
 * the whole series (partly approved = tap to approve the rest); in "approved"
 * the card is about what is already on the calendar, so the toggle is on,
 * switching it off takes those back, and only they are listed.
 */
export function SeriesCard({ series, state, statusOf, isBusy, expanded, onExpand, actions, view = 'review' }) {
  const { occurrences, pending, approved, approveIds, revokeIds, rejectIds } = state
  const title = unescapeHtml(series.title)
  const managing = view === 'approved'
  const allOn = managing ? approved.length > 0 : pending.length === 0 && approved.length > 0
  const mixed = !managing && pending.length > 0 && approved.length > 0
  const rows = managing ? approved : occurrences
  const fullyApproved = approved.length > 0 && pending.length === 0
  const stateText = fullyApproved ? 'all approved' : approved.length ? `${approved.length} of ${occurrences.length} approved` : ''
  const busy = [...approveIds, ...revokeIds].some(isBusy)
  const first = occurrences[0]?.start
  const last = occurrences[occurrences.length - 1]?.start
  const key = series.key

  return (
    <article class={`rv-card rv-series${fullyApproved ? ' is-on' : ''}${expanded ? ' is-expanded' : ''}`}>
      <div class="rv-series-head">
        <button type="button" class="rv-series-expand" aria-expanded={expanded ? 'true' : 'false'} onClick={onExpand}>
          <span class="rv-titlebar">
            <span class="rv-series-title">{title}</span>
            <KindBadge type={series.item_type} />
          </span>
          <span class="rv-series-line">{describeSeries(series, occurrences)}</span>
          <span class="rv-series-meta">
            <span class="rv-series-tags">
              <span>{formatDateRange(first, last)}</span>
              {stateText && <span class="rv-series-state">{stateText}</span>}
            </span>
            <Chevron open={expanded} />
          </span>
        </button>
        <Switch
          checked={allOn}
          mixed={mixed}
          busy={busy}
          label={
            managing
              ? `${title}: ${approved.length} approved. Switch off to take them back`
              : allOn
              ? `${title}: all ${occurrences.length} approved. Switch off to take them all back`
              : mixed
                ? `${title}: approve the other ${pending.length}`
                : `Approve all ${occurrences.length} ${title}`
          }
          onChange={(next) => (next ? actions.approve(approveIds, key) : actions.revoke(revokeIds, key))}
        />
      </div>
      {expanded && (
        <div class="rv-series-body">
          <div class="rv-series-tools">
            <span class="rv-hint">
              {managing ? 'Tap a date for the reason; switch one off to take it back.' : 'Tap a date for the reason, ✎ to pick your own time.'}
            </span>
            {!managing && pending.length > 0 && (
              <ConfirmButton
                label="Reject series"
                confirmLabel={`Reject ${pending.length}`}
                disabled={busy}
                onConfirm={() => actions.reject(rejectIds, key)}
              />
            )}
          </div>
          <div class="rv-occ-list">
            {rows.map((o) => (
              <OccurrenceRow
                key={o.id}
                p={o}
                status={statusOf(o)}
                busy={isBusy(o.id) || Boolean(o.travel && isBusy(o.travel.id))}
                onApprove={() => actions.approve(occurrenceIds(o, statusOf, 'proposed'), key)}
                onRevoke={() => actions.revoke(occurrenceIds(o, statusOf, 'promoted'), key)}
                onReject={() => actions.reject(occurrenceIds(o, statusOf, 'proposed'), key)}
                onApproveAt={(times) => actions.approveAt(o, times, key)}
              />
            ))}
          </div>
        </div>
      )}
    </article>
  )
}
