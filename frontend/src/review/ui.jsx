import { useEffect, useState } from 'preact/hooks'

// Small building blocks shared by both review tabs.

/**
 * A two-step button for decisions that can't be undone (rejections): the
 * first tap asks, the second does it. Asking times out after a few seconds.
 */
export function ConfirmButton({ label, confirmLabel, onConfirm, disabled = false }) {
  const [asking, setAsking] = useState(false)
  useEffect(() => {
    if (!asking) return undefined
    const timer = setTimeout(() => setAsking(false), 6000)
    return () => clearTimeout(timer)
  }, [asking])
  if (!asking) {
    return (
      <button type="button" class="rv-btn rv-btn-quiet rv-btn-danger" disabled={disabled} onClick={() => setAsking(true)}>
        {label}
      </button>
    )
  }
  return (
    <span class="rv-confirm-inline">
      <button type="button" class="rv-btn rv-btn-quiet" onClick={() => setAsking(false)}>
        Keep
      </button>
      <button
        type="button"
        class="rv-btn rv-btn-danger-solid"
        onClick={() => {
          setAsking(false)
          onConfirm()
        }}
      >
        {confirmLabel}
      </button>
    </span>
  )
}

/**
 * The approve toggle. `mixed` = some of a series approved; tapping it then
 * approves the rest. The whole 44x44 button is the tap target.
 */
export function Switch({ checked, mixed = false, busy = false, disabled = false, label, onChange }) {
  const state = mixed ? 'mixed' : checked ? 'on' : 'off'
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked && !mixed ? 'true' : 'false'}
      aria-label={label}
      title={label}
      class={`rv-switch is-${state}${busy ? ' is-busy' : ''}`}
      disabled={disabled || busy}
      onClick={(e) => {
        e.stopPropagation()
        onChange(mixed ? true : !checked)
      }}
    >
      <span class="rv-switch-track" aria-hidden="true">
        <span class="rv-switch-knob" />
      </span>
    </button>
  )
}

const KIND_LABEL = { goal: 'goal', task: 'task', travel: 'travel', free: 'free time', event: 'event' }

export function KindBadge({ type }) {
  return <span class={`rv-kind rv-kind-${type}`}>{KIND_LABEL[type] ?? type}</span>
}

/** "To review 7 | Approved 2". */
export function FilterChips({ value, options, onChange }) {
  return (
    <div class="rv-chips" role="group" aria-label="Show">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          class="rv-chip"
          aria-pressed={value === o.value ? 'true' : 'false'}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          <span class="rv-chip-count">{o.count}</span>
        </button>
      ))}
    </div>
  )
}

export function Toast({ toast, onDismiss }) {
  return (
    <div class="rv-toast-slot" role="status" aria-live="polite">
      {toast && (
        <button type="button" key={toast.id} class={`rv-toast is-${toast.tone}`} onClick={onDismiss}>
          {toast.message}
        </button>
      )}
    </div>
  )
}

/** A collapsed-by-default section ("Why this plan", "Held back"). */
export function Disclosure({ title, count, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section class={`rv-disclosure${open ? ' is-open' : ''}`}>
      <button type="button" class="rv-disclosure-head" aria-expanded={open ? 'true' : 'false'} onClick={() => setOpen(!open)}>
        <span>{title}</span>
        {count !== undefined && <span class="rv-disclosure-count">{count}</span>}
        <Chevron open={open} />
      </button>
      {open && <div class="rv-disclosure-body">{children}</div>}
    </section>
  )
}

export function Chevron({ open }) {
  return (
    <svg class={`rv-chevron${open ? ' is-open' : ''}`} viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
      <path d="M5.5 7.5 10 12l4.5-4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  )
}

export function PencilIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
      <path
        d="M13.6 3.6a1.9 1.9 0 0 1 2.7 2.7l-8.8 8.8-3.6.9.9-3.6 8.8-8.8Z"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linejoin="round"
      />
    </svg>
  )
}

export function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
      <path d="m5.5 5.5 9 9m0-9-9 9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </svg>
  )
}

export function ReloadIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
      <path
        d="M16 10a6 6 0 1 1-1.8-4.3M16 4v3.5h-3.5"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  )
}

/** First load: soft placeholder cards instead of a spinner. Error: a retry card. */
export function LoadState({ error, onRetry, rows = 4, what = 'the review' }) {
  if (error) {
    return (
      <div class="rv-card rv-error" role="alert">
        <p>Couldn't load {what}: {error}</p>
        <button type="button" class="rv-btn rv-btn-secondary" onClick={onRetry}>
          Try again
        </button>
      </div>
    )
  }
  return (
    <div class="rv-skeletons" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} class="rv-card rv-skeleton" />
      ))}
    </div>
  )
}

/** A thin banner with a live counter while a model call runs. */
export function RunningBanner({ text, elapsed }) {
  return (
    <div class="rv-banner" role="status">
      <span class="rv-pulse" aria-hidden="true" />
      <span>{text}</span>
      {elapsed && <span class="rv-banner-time">{elapsed}</span>}
    </div>
  )
}
