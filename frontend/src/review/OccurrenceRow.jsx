import { useState } from 'preact/hooks'
import { formatDay, formatTimeRange, formatWeekdayDate, unescapeHtml } from './format.js'
import { TimeEditor } from './TimeEditor.jsx'
import { CloseIcon, KindBadge, PencilIcon, Switch } from './ui.jsx'

/**
 * One block of the plan: date · time · pencil · toggle · ×. Inside a series
 * card it shows only the date and time; on its own (`single`, a one-off
 * block) it also shows the title and kind. Travel to it is shown inline and
 * decided together with it. Tapping the date/time shows the model's reason.
 */
export function OccurrenceRow({ p, status, busy, single = false, onApprove, onRevoke, onReject, onApproveAt }) {
  const [editing, setEditing] = useState(false)
  const [showWhy, setShowWhy] = useState(false)
  const on = status === 'promoted'
  const travel = p.travel || null
  const title = unescapeHtml(p.title)
  const moved = Boolean(p.adjusted_at)
  const canMove = !on && p.item_type !== 'event' // public events happen at their listed time
  const why = unescapeHtml(p.rationale || '')
  const place = unescapeHtml(p.location || '')

  return (
    <div class={`rv-occ${single ? ' rv-card is-single' : ''}${on ? ' is-on' : ''}${busy ? ' is-busy' : ''}${moved ? ' is-moved' : ''}`}>
      <div class="rv-occ-main">
        <button
          type="button"
          class="rv-occ-when"
          aria-expanded={showWhy ? 'true' : 'false'}
          onClick={() => setShowWhy(!showWhy)}
        >
          {single && (
            <span class="rv-titlebar">
              <span class="rv-occ-title">{title}</span>
              <KindBadge type={p.item_type} />
            </span>
          )}
          <span class="rv-occ-line">
            <span class="rv-occ-day">{single ? formatDay(p.start) : formatWeekdayDate(p.start)}</span>
            {single && (
              <span class="rv-dot" aria-hidden="true">
                ·
              </span>
            )}
            <span class="rv-occ-time">{formatTimeRange(p)}</span>
            {moved && <span class="rv-occ-moved">your time</span>}
            {p.replaces_event_id && !on && <span class="rv-occ-tag rv-occ-tag-move">move</span>}
            {p.conflict_event_id && on && <span class="rv-occ-tag rv-occ-tag-conflict">conflict</span>}
          </span>
          {travel && <span class="rv-occ-travel">↳ travel {formatTimeRange(travel)}</span>}
        </button>
        {canMove ? (
          <button
            type="button"
            class={`rv-icon-btn${editing ? ' is-active' : ''}`}
            aria-label={`Pick another time for ${title}`}
            title="Approve at a time of my choosing"
            aria-expanded={editing ? 'true' : 'false'}
            disabled={busy}
            onClick={() => setEditing(!editing)}
          >
            <PencilIcon />
          </button>
        ) : (
          <span class="rv-icon-spacer" />
        )}
        <Switch
          checked={on}
          busy={busy}
          label={on ? `Approved: ${title}, ${formatDay(p.start)}. Switch off to take it back` : `Approve ${title}, ${formatDay(p.start)}`}
          onChange={(next) => (next ? onApprove() : onRevoke())}
        />
        {on ? (
          <span class="rv-icon-spacer" />
        ) : (
          <button
            type="button"
            class="rv-icon-btn rv-reject"
            aria-label={`Reject ${title}, ${formatDay(p.start)}`}
            title="Reject"
            disabled={busy}
            onClick={onReject}
          >
            <CloseIcon />
          </button>
        )}
      </div>
      {showWhy && (
        <div class="rv-occ-why">
          {single && place && <p class="rv-occ-place">{place}</p>}
          <p>{why || 'No reason given.'}</p>
        </div>
      )}
      {editing && canMove && (
        <TimeEditor
          p={p}
          busy={busy}
          onCancel={() => setEditing(false)}
          onSubmit={(times) => {
            setEditing(false)
            onApproveAt(times)
          }}
        />
      )}
    </div>
  )
}
