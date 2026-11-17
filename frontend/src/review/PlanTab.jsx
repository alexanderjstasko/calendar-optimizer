import { useCallback, useMemo, useRef, useState } from 'preact/hooks'
import { revokeMany, runningFor } from './api.js'
import { formatDay, formatElapsed, formatStamp, formatTimeRange, plural, unescapeHtml, zonedParts } from './format.js'
import { OccurrenceRow } from './OccurrenceRow.jsx'
import { SeriesCard, occurrenceIds, seriesState } from './SeriesCard.jsx'
import { useDismiss, useNow, useReview } from './useReview.js'
import { Disclosure, FilterChips, KindBadge, LoadState, ReloadIcon, RunningBanner, Toast } from './ui.jsx'
import './review.css'

// The Plan tab: the optimizer's proposals as one card per series ("Read the
// News, daily 8:15 AM, 24x") instead of one row per occurrence. A card's
// toggle approves or takes back the whole series; its chevron opens the
// individual blocks (pencil, toggle, reject). One-off blocks are plain rows.

// Turn "approve at 18:30" for a destination into the same shift for its
// travel block, so travel still ends when the destination starts.
const wallClockMs = (iso) => {
  const { date, time } = zonedParts(iso)
  return Date.parse(`${date}T${time}:00Z`)
}
const naive = (ms) => new Date(ms).toISOString().slice(0, 19)
function shiftedTravel(travel, destination, times) {
  const delta = Date.parse(`${times.start}Z`) - wallClockMs(destination.start)
  if (!delta) return undefined // same start: approve the travel as proposed
  return { start: naive(wallClockMs(travel.start) + delta), end: naive(wallClockMs(travel.end) + delta) }
}

export function batchMessage(out, verb) {
  const done = out?.done?.length ?? 0
  const failed = out?.failed ?? []
  let text = verb === 'approve' ? `Approved ${plural(done, 'block')}` : `Rejected ${plural(done, 'block')}`
  if (verb === 'approve' && done) text += ', on your calendar'
  const overlaps = out?.superseded_overlaps?.length ?? 0
  if (overlaps) text += ` · ${plural(overlaps, 'overlapping option')} removed`
  if (failed.length) text += ` · ${failed.length} failed: ${failed[0].detail}`
  return text
}

export function revokeMessage(out) {
  const done = out?.done?.length ?? 0
  const failed = out?.failed ?? []
  let text = `Took back ${plural(done, 'block')}, off the calendar`
  if (failed.length) text += ` · ${failed.length} failed: ${failed[0].detail}`
  return text
}

const byCount = (a, b) =>
  b.state.occurrences.length - a.state.occurrences.length || a.series.title.localeCompare(b.series.title)
const byStart = (a, b) => a.state.occurrences[0].start.localeCompare(b.state.occurrences[0].start)

export function PlanTab({ apiUrl, active, onCalendarChanged, onSummary, navigate }) {
  const review = useReview({ apiUrl, active, onSummary, onCalendarChanged })
  const { api, summary, running, loadError, load, statusOf, isBusy, mutate, touched, toast, notify, dismissToast } = review
  const [filter, setFilter] = useState('review') // review | approved
  const [expanded, setExpanded] = useState(() => new Set())
  const [askReplan, setAskReplan] = useState(false)
  const [replanSince, setReplanSince] = useState(null) // set while this page waits for POST /optimizer/run
  const [askApproveAll, setAskApproveAll] = useState(false)
  const askRef = useRef(null)
  const barRef = useRef(null)
  const closeReplan = useCallback(() => setAskReplan(false), [])
  const closeApproveAll = useCallback(() => setAskApproveAll(false), [])
  useDismiss(askReplan, closeReplan, askRef)
  useDismiss(askApproveAll, closeApproveAll, barRef)

  const plan = summary?.plan
  const cards = useMemo(
    () =>
      (plan?.series || [])
        .map((series) => ({ series, key: series.key || `${series.item_type}:${series.title}`, state: seriesState(series, statusOf) }))
        .filter((c) => c.state.occurrences.length > 0),
    [plan, statusOf],
  )
  const pendingCards = cards.filter((c) => c.state.pending.length > 0)
  const approvedCards = cards.filter((c) => c.state.approved.length > 0)
  const shown = filter === 'review' ? cards.filter((c) => c.state.pending.length > 0 || touched.has(c.key)) : approvedCards
  const seriesCards = shown.filter((c) => c.state.occurrences.length > 1).sort(byCount)
  const oneOffs = shown.filter((c) => c.state.occurrences.length === 1).sort(byStart)
  const allPendingIds = pendingCards.flatMap((c) => c.state.approveIds)
  const heldBack = plan?.held_back || []

  const optimizerRun = summary?.runs?.optimizer // the run whose plan is under review
  const now = useNow(active && (replanSince !== null || running.optimizer !== null))
  const serverRunning = runningFor(running.optimizer, now) // a re-plan started elsewhere, or a dropped request
  const replanElapsed = replanSince !== null ? now - replanSince : serverRunning
  const replanning = replanElapsed !== null

  const go = (tab, params) => {
    if (navigate) navigate(tab, params)
    else window.location.hash = `#/${tab}${params?.preview ? '?preview=1' : ''}`
  }

  const actions = {
    approve: (ids, key) =>
      mutate({ ids, to: 'promoted', keys: [key], run: () => api.batch('approve', ids), describe: (out) => batchMessage(out, 'approve') }),
    reject: (ids, key) =>
      mutate({ ids, to: 'rejected', keys: [key], run: () => api.batch('reject', ids), describe: (out) => batchMessage(out, 'reject') }),
    revoke: (ids, key) =>
      mutate({ ids, to: 'proposed', keys: [key], run: () => revokeMany(api, ids), describe: revokeMessage }),
    approveAt: (o, times, key) => {
      const travel = o.travel && statusOf(o.travel) === 'proposed' ? o.travel : null
      return mutate({
        ids: [o.id, travel?.id],
        to: 'promoted',
        keys: [key],
        run: async () => {
          const out = await api.approve(o.id, times)
          if (travel) {
            try {
              await api.approve(travel.id, shiftedTravel(travel, o, times))
            } catch (err) {
              return { ...out, travelError: err.message }
            }
          }
          return out
        },
        describe: (out) =>
          out?.travelError
            ? `Approved at your time, but the travel block failed: ${out.travelError}`
            : `Approved at your time${travel ? ', travel moved with it' : ''}`,
      })
    },
  }

  function approvePlan() {
    setAskApproveAll(false)
    const ids = allPendingIds
    mutate({
      ids,
      to: 'promoted',
      keys: pendingCards.map((c) => c.key),
      run: () => api.batch('approve', ids),
      describe: (out) => batchMessage(out, 'approve'),
    })
  }

  async function startReplan() {
    setAskReplan(false)
    setReplanSince(Date.now())
    try {
      const out = await api.replan()
      notify(`New plan: ${plural(out?.proposed ?? 0, 'block')}${out?.invalid ? `, ${out.invalid} held back` : ''}`)
      setExpanded(new Set())
    } catch (err) {
      notify(
        err.status === 0
          ? 'Lost touch with the server. The re-plan carries on there; this page catches up when it is done.'
          : `Re-plan failed: ${err.message}`,
        'error',
      )
    } finally {
      setReplanSince(null)
      await load()
      onCalendarChanged?.()
    }
  }

  const toggleExpanded = (key) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const blocksToReview = allPendingIds.length

  return (
    <section class="rv-tab rv-plan" aria-label="Plan">
      <header class="rv-head">
        <div class="rv-head-text">
          <h2 class="rv-title">Your plan</h2>
          <p class="rv-sub">
            {!summary
              ? ' '
              : blocksToReview
                ? `${plural(blocksToReview, 'block')} to review`
                : 'Nothing waiting for you'}
          </p>
        </div>
        <button
          type="button"
          class="rv-btn rv-btn-outline"
          disabled={replanning || !summary}
          aria-expanded={askReplan ? 'true' : 'false'}
          onClick={() => setAskReplan(!askReplan)}
        >
          <ReloadIcon />
          Re-plan
        </button>
      </header>

      {askReplan && !replanning && (
        <div class="rv-card rv-ask" ref={askRef}>
          <p class="rv-ask-title">Ask the model for a fresh plan?</p>
          <p class="rv-hint">
            Takes about 6 minutes. It replaces the plan items you haven't decided on; anything you approved stays.
          </p>
          <div class="rv-ask-actions">
            <button type="button" class="rv-btn rv-btn-quiet" onClick={() => setAskReplan(false)}>
              Cancel
            </button>
            <button type="button" class="rv-btn rv-btn-primary" onClick={startReplan}>
              Re-plan now
            </button>
          </div>
        </div>
      )}

      {replanning && (
        <RunningBanner text="Re-planning (about 6 min). The list refreshes when it's done." elapsed={formatElapsed(replanElapsed)} />
      )}

      {summary && loadError && (
        <p class="rv-inline-error" role="alert">
          Couldn't refresh: {loadError}{' '}
          <button type="button" class="rv-link" onClick={load}>
            Retry
          </button>
        </p>
      )}

      {!summary ? (
        <LoadState error={loadError} onRetry={load} />
      ) : (
        <>
          <FilterChips
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'review', label: 'To review', count: pendingCards.length },
              { value: 'approved', label: 'Approved', count: approvedCards.length },
            ]}
          />

          {shown.length === 0 && (
            <div class="rv-empty">
              {filter === 'review' ? (
                <>
                  <p class="rv-empty-title">All caught up</p>
                  <p class="rv-hint">Nothing in the plan is waiting for you. Re-plan when you want a fresh one.</p>
                </>
              ) : (
                <p class="rv-hint">Nothing approved yet. Approved blocks show here until they've happened.</p>
              )}
            </div>
          )}

          {seriesCards.length > 0 && (
            <div class="rv-list">
              {seriesCards.map((c) => (
                <SeriesCard
                  key={c.key}
                  series={c.series}
                  state={c.state}
                  statusOf={statusOf}
                  isBusy={isBusy}
                  expanded={expanded.has(c.key)}
                  onExpand={() => toggleExpanded(c.key)}
                  actions={actions}
                  view={filter}
                />
              ))}
            </div>
          )}

          {oneOffs.length > 0 && (
            <>
              <h3 class="rv-section-title">One-off blocks</h3>
              <div class="rv-list">
                {oneOffs.map((c) => {
                  const o = c.state.occurrences[0]
                  return (
                    <OccurrenceRow
                      key={c.key}
                      p={o}
                      single
                      status={statusOf(o)}
                      busy={isBusy(o.id) || Boolean(o.travel && isBusy(o.travel.id))}
                      onApprove={() => actions.approve(occurrenceIds(o, statusOf, 'proposed'), c.key)}
                      onRevoke={() => actions.revoke(occurrenceIds(o, statusOf, 'promoted'), c.key)}
                      onReject={() => actions.reject(occurrenceIds(o, statusOf, 'proposed'), c.key)}
                      onApproveAt={(times) => actions.approveAt(o, times, c.key)}
                    />
                  )
                })}
              </div>
            </>
          )}

          <div class="rv-extras">
            {optimizerRun?.notes && (
              <Disclosure title="Why this plan">
                <p class="rv-notes">{unescapeHtml(optimizerRun.notes)}</p>
                <p class="rv-hint">
                  Planned {formatStamp(optimizerRun.finished_at || optimizerRun.started_at)}
                  {optimizerRun.proposed_count != null && ` · ${plural(optimizerRun.proposed_count, 'block')} proposed`}
                </p>
              </Disclosure>
            )}
            {heldBack.length > 0 && (
              <Disclosure title="Held back by validation" count={heldBack.length}>
                <ul class="rv-held">
                  {heldBack.map((p) => (
                    <li key={p.id}>
                      <div class="rv-held-top">
                        <KindBadge type={p.item_type} />
                        <span class="rv-held-title">{unescapeHtml(p.title)}</span>
                      </div>
                      <div class="rv-hint">
                        {formatDay(p.start)} · {formatTimeRange(p)}
                      </div>
                      {p.invalid_reason && <div class="rv-held-reason">{p.invalid_reason}</div>}
                    </li>
                  ))}
                </ul>
              </Disclosure>
            )}
          </div>
        </>
      )}

      <div class="rv-footer">
        <Toast toast={toast} onDismiss={dismissToast} />
        {summary && filter === 'review' && blocksToReview > 0 && (
          <div class="rv-bar" ref={barRef}>
            {askApproveAll ? (
              <>
                <button type="button" class="rv-btn rv-btn-secondary" onClick={() => setAskApproveAll(false)}>
                  Cancel
                </button>
                <button type="button" class="rv-btn rv-btn-primary rv-btn-grow" onClick={approvePlan}>
                  Yes, approve all {blocksToReview}
                </button>
              </>
            ) : (
              <>
                <button type="button" class="rv-btn rv-btn-secondary" onClick={() => go('calendar', { preview: 1 })}>
                  Preview
                </button>
                <button type="button" class="rv-btn rv-btn-primary rv-btn-grow" onClick={() => setAskApproveAll(true)}>
                  Approve plan ({blocksToReview})
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
