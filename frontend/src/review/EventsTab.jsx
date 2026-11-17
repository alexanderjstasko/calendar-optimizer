import { useCallback, useMemo, useRef, useState } from 'preact/hooks'
import { runningFor } from './api.js'
import { EventCard } from './EventCard.jsx'
import { formatElapsed, formatStamp, plural, unescapeHtml } from './format.js'
import { LISTED, useDismiss, useNow, useReview } from './useReview.js'
import { Disclosure, FilterChips, LoadState, ReloadIcon, RunningBanner, Toast } from './ui.jsx'
import './review.css'

// The Events tab: the scout's menu of public events (8 at a time). Toggle =
// go (approved onto the calendar); "Not interested" = a rejection the scout
// learns from. Reload swaps the page: instantly from the queue while options
// are waiting there (summary events.reload = "queue"), otherwise by asking the
// model again (~2 min).

const PAGE_SIZE = 8 // LLM_SCOUT_PAGE_SIZE's default; the summary's events.page_size wins when present

const byStart = (a, b) => a.start.localeCompare(b.start)

function overlapNote(out) {
  const n = out?.superseded_overlaps?.length ?? 0
  return n ? ` · ${plural(n, 'overlapping option')} removed` : ''
}

export function EventsTab({ apiUrl, active, onCalendarChanged, onSummary }) {
  const review = useReview({ apiUrl, active, onSummary, onCalendarChanged })
  const { api, summary, running, loadError, load, statusOf, isBusy, mutate, touched, toast, notify, dismissToast } = review
  const [filter, setFilter] = useState('review')
  const [reloadSince, setReloadSince] = useState(null) // waiting for POST /scout/reload
  const [reloadAsksModel, setReloadAsksModel] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [askFetch, setAskFetch] = useState(false)
  const [fetchSince, setFetchSince] = useState(null) // waiting for POST /scout/fetch
  const [scoutStatus, setScoutStatus] = useState(null)
  const menuRef = useRef(null)
  const rootRef = useRef(null)
  const closeMenu = useCallback(() => {
    setMenuOpen(false)
    setAskFetch(false)
  }, [])
  useDismiss(menuOpen, closeMenu, menuRef)

  const events = summary?.events
  const items = useMemo(() => (events?.items || []).filter((p) => LISTED.has(statusOf(p))).sort(byStart), [events, statusOf])
  const pending = items.filter((p) => statusOf(p) === 'proposed')
  const approved = items.filter((p) => statusOf(p) === 'promoted')
  const shown = filter === 'review' ? items.filter((p) => statusOf(p) === 'proposed' || touched.has(p.id)) : approved

  const queued = events?.queued ?? 0
  const pageSize = events?.page_size ?? PAGE_SIZE
  // The summary says what POST /scout/reload would do right now; older
  // servers don't, so fall back to "a full page is waiting in the queue".
  const instant = events?.reload ? events.reload === 'queue' : queued >= pageSize

  const scoutRun = summary?.runs?.scout // the run whose options are shown
  const now = useNow(active && (reloadSince !== null || fetchSince !== null || running.scout !== null))
  const serverRunning = runningFor(running.scout, now) // e.g. the morning scout run, or a reload elsewhere
  const reloading = reloadSince !== null
  const working = reloading || fetchSince !== null || serverRunning !== null

  const actions = {
    approve: (p) =>
      mutate({
        ids: [p.id],
        to: 'promoted',
        keys: [p.id],
        run: () => api.approve(p.id),
        describe: (out) => `Added to your calendar${overlapNote(out)}`,
      }),
    revoke: (p) =>
      mutate({ ids: [p.id], to: 'proposed', keys: [p.id], run: () => api.revoke(p.id), describe: () => 'Taken off your calendar' }),
    reject: (p) =>
      mutate({ ids: [p.id], to: 'rejected', run: () => api.reject(p.id), describe: () => "Noted: not interested. The scout learns from this." }),
  }

  async function reload() {
    const asksModel = !instant
    setReloadAsksModel(asksModel)
    setReloadSince(Date.now())
    try {
      const out = await api.reload()
      const n = out?.shown?.length ?? 0
      // The button sits under the list: bring the new options into view.
      rootRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
      notify(
        out?.source === 'model'
          ? `${plural(n, 'fresh option')} from the model`
          : `Showing ${plural(n, 'new option')}${out?.queued ? ` · ${out.queued} more ready` : ''}`,
      )
    } catch (err) {
      notify(
        err.status === 0 && asksModel
          ? 'Lost touch with the server. The scout carries on there; this page catches up when it is done.'
          : `Reload failed: ${err.message}`,
        'error',
      )
    } finally {
      setReloadSince(null)
      await load()
      onCalendarChanged?.() // the calendar's preview layer shows pending options
    }
  }

  async function openMenu() {
    if (menuOpen) return closeMenu()
    setMenuOpen(true)
    try {
      setScoutStatus(await api.scoutStatus())
    } catch {
      setScoutStatus(null)
    }
  }

  async function fetchSites() {
    closeMenu()
    setFetchSince(Date.now())
    try {
      const out = await api.fetchSites()
      notify(`Sites read: ${out?.new ?? 0} new events, ${out?.updated ?? 0} updated${out?.errors ? `, ${out.errors} sites failed` : ''}`)
    } catch (err) {
      notify(`Fetching sites failed: ${err.message}`, 'error')
    } finally {
      setFetchSince(null)
      await load()
    }
  }

  const unreadable = (scoutStatus?.websites || []).filter((w) => w.last_fetch_method === 'unreadable' || w.last_fetch_error)

  let banner = null
  if (reloading && reloadAsksModel) banner = { text: 'Asking the model for fresh options (about 2 min).', since: now - reloadSince }
  else if (fetchSince !== null) banner = { text: 'Reading the events sites (a few minutes).', since: now - fetchSince }
  else if (serverRunning !== null && !reloading) banner = { text: 'The scout is picking events. This list refreshes when it is done.', since: serverRunning }

  const reloadLabel = reloading
    ? reloadAsksModel
      ? `Asking the model… ${formatElapsed(now - reloadSince)}`
      : 'Reloading…'
    : instant
      ? 'Reload (instant)'
      : 'Reload (asks the model, ~2 min)'

  return (
    <section class="rv-tab rv-events" aria-label="Events" ref={rootRef}>
      <header class="rv-head rv-head-events">
        <div class="rv-head-text">
          <h2 class="rv-title rv-title-keep">Events</h2>
        </div>
        {/* A second Reload beside the title so it's found without scrolling past every card.
            A model reload costs a call and ~2 min, so that one asks first. */}
        {filter === 'review' && summary && (
          <button
            type="button"
            class="rv-btn rv-btn-secondary rv-head-reload"
            disabled={working}
            title={reloadLabel}
            onClick={() => {
              if (instant || window.confirm('Ask the model for fresh options? It takes about 2 minutes.')) reload()
            }}
          >
            <ReloadIcon />
            {reloading ? (reloadAsksModel ? formatElapsed(now - reloadSince) : 'Reloading…') : 'Reload'}
          </button>
        )}
        <div class="rv-menu" ref={menuRef}>
          <button
            type="button"
            class={`rv-icon-btn${menuOpen ? ' is-active' : ''}`}
            aria-label="More"
            aria-haspopup="true"
            aria-expanded={menuOpen ? 'true' : 'false'}
            onClick={openMenu}
          >
            <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
              <circle cx="4.5" cy="10" r="1.6" fill="currentColor" />
              <circle cx="10" cy="10" r="1.6" fill="currentColor" />
              <circle cx="15.5" cy="10" r="1.6" fill="currentColor" />
            </svg>
          </button>
          {menuOpen && (
            <div class="rv-menu-panel">
              {scoutStatus && (
                <p class="rv-hint">
                  {plural(scoutStatus.websites?.length ?? 0, 'site')} · {scoutStatus.undecided_in_window ?? 0} candidate events
                  {scoutStatus.last_run?.started_at ? ` · last picks ${formatStamp(scoutStatus.last_run.started_at)}` : ''}
                  {unreadable.length ? ` · ${plural(unreadable.length, 'site')} unreadable` : ''}
                </p>
              )}
              {askFetch ? (
                <>
                  <p class="rv-hint">
                    Re-reads every events site now. Takes a few minutes and uses the model for sites that only have text.
                  </p>
                  <div class="rv-menu-actions">
                    <button type="button" class="rv-btn rv-btn-quiet" onClick={() => setAskFetch(false)}>
                      Cancel
                    </button>
                    <button type="button" class="rv-btn rv-btn-primary rv-btn-grow" onClick={fetchSites}>
                      Fetch sites now
                    </button>
                  </div>
                </>
              ) : (
                <button type="button" class="rv-btn rv-btn-secondary" disabled={working} onClick={() => setAskFetch(true)}>
                  Fetch sites again…
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      {banner && <RunningBanner text={banner.text} elapsed={formatElapsed(banner.since)} />}

      {summary && loadError && (
        <p class="rv-inline-error" role="alert">
          Couldn't refresh: {loadError}{' '}
          <button type="button" class="rv-link" onClick={load}>
            Retry
          </button>
        </p>
      )}

      {!summary ? (
        <LoadState error={loadError} onRetry={load} rows={3} />
      ) : (
        <>
          <FilterChips
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'review', label: 'To review', count: pending.length },
              { value: 'approved', label: 'Approved', count: approved.length },
            ]}
          />

          {shown.length === 0 && (
            <div class="rv-empty">
              {filter === 'review' ? (
                <>
                  <p class="rv-empty-title">No options right now</p>
                  <p class="rv-hint">Reload for more, or check back after the morning scout run.</p>
                </>
              ) : (
                <p class="rv-hint">Nothing approved yet. Events you say yes to show here until they've happened.</p>
              )}
            </div>
          )}

          <div class="rv-list">
            {shown.map((p) => (
              <EventCard
                key={p.id}
                p={p}
                status={statusOf(p)}
                busy={isBusy(p.id) || reloading}
                onApprove={() => actions.approve(p)}
                onRevoke={() => actions.revoke(p)}
                onReject={() => actions.reject(p)}
              />
            ))}
          </div>

          {filter === 'review' && (
            <div class="rv-reload">
              <button type="button" class="rv-btn rv-btn-secondary rv-btn-block" disabled={working} onClick={reload}>
                <ReloadIcon />
                {reloadLabel}
              </button>
              <p class="rv-hint">
                {instant
                  ? `Shows the next ${pageSize}; the undecided ones here go back in the queue.`
                  : 'Replaces the undecided ones with a fresh list from the model.'}
              </p>
            </div>
          )}

          {scoutRun?.notes && (
            <div class="rv-extras">
              <Disclosure title="Why these picks">
                <p class="rv-notes">{unescapeHtml(scoutRun.notes)}</p>
                <p class="rv-hint">Picked {formatStamp(scoutRun.finished_at || scoutRun.started_at)}</p>
              </Disclosure>
            </div>
          )}
        </>
      )}

      <div class="rv-footer">
        <Toast toast={toast} onDismiss={dismissToast} />
      </div>
    </section>
  )
}
