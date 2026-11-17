import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { createApi, runningRuns } from './api.js'

// Shared state for one review tab: the /review/summary payload, optimistic
// overrides while a decision is in flight, the toast, and which cards were
// acted on during this visit (they stay in the "To review" list until the
// tab is left, so a card never jumps away under the user's thumb).

/** Statuses a review row can show; anything else (rejected, superseded, expired) has left the list. */
export const LISTED = new Set(['proposed', 'promoted'])

const POLL_WHILE_RUNNING_MS = 10000
const REFRESH_ON_RETURN_MS = 30000

export function useReview({ apiUrl, active, onSummary, onCalendarChanged }) {
  const api = useMemo(() => createApi(apiUrl), [apiUrl])
  const [summary, setSummary] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [overrides, setOverrides] = useState(() => new Map()) // proposal id -> status shown meanwhile
  const [busy, setBusy] = useState(() => new Set()) // proposal ids with a request in flight
  const [touched, setTouched] = useState(() => new Set()) // card keys acted on during this visit
  const [toast, setToast] = useState(null) // {message, tone, id}
  const [running, setRunning] = useState({ optimizer: null, scout: null }) // model runs in flight

  const callbacks = useRef({})
  callbacks.current = { onSummary, onCalendarChanged }
  const started = useRef(0) // load sequence numbers: an older response never overwrites a newer one
  const applied = useRef(0)
  const lastLoaded = useRef(0)

  const load = useCallback(async () => {
    const mine = ++started.current
    try {
      // The run list is only for "is a re-plan / scout run going on?"; the
      // tab works without it.
      const [data, runs] = await Promise.all([api.summary(), api.recentRuns().catch(() => null)])
      if (mine < applied.current) return
      applied.current = mine
      lastLoaded.current = Date.now()
      setSummary(data)
      if (runs) setRunning(runningRuns(runs))
      setLoadError('')
      callbacks.current.onSummary?.(data)
    } catch (err) {
      if (mine >= applied.current) setLoadError(err.message)
    }
  }, [api])

  const notify = useCallback((message, tone = 'ok') => {
    if (message) setToast({ message, tone, id: Date.now() })
  }, [])

  useEffect(() => {
    if (!toast) return undefined
    const timer = setTimeout(() => setToast(null), toast.tone === 'error' ? 6000 : 3200)
    return () => clearTimeout(timer)
  }, [toast])

  // Fresh data every time the tab is opened; the visit's "touched" cards reset.
  useEffect(() => {
    if (!active) return
    setTouched(new Set())
    load()
  }, [active, load])

  // Back from another app (the phone was locked, the email was read...): refresh.
  useEffect(() => {
    if (!active) return undefined
    const onVisible = () => {
      if (document.visibilityState === 'visible' && Date.now() - lastLoaded.current > REFRESH_ON_RETURN_MS) load()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [active, load])

  // A model run (re-plan, scout reload) is in flight somewhere: poll until it ends.
  const somethingRunning = Boolean(running.optimizer || running.scout)
  useEffect(() => {
    if (!active || !somethingRunning) return undefined
    const timer = setInterval(load, POLL_WHILE_RUNNING_MS)
    return () => clearInterval(timer)
  }, [active, somethingRunning, load])

  const statusOf = useCallback((p) => (p ? (overrides.get(p.id) ?? p.status) : null), [overrides])
  const isBusy = useCallback((id) => busy.has(id), [busy])

  /**
   * One decision (or a batch): show `to` on `ids` at once, run the request,
   * reload the summary, then drop the optimistic state. `keys` are the cards
   * acted on (kept in view for this visit). `describe(result)` returns the
   * toast text; a thrown error becomes an error toast.
   */
  const mutate = useCallback(
    async ({ ids, to, keys = [], run, describe }) => {
      const list = ids.filter(Boolean)
      setBusy((prev) => new Set([...prev, ...list]))
      if (to) setOverrides((prev) => new Map([...prev, ...list.map((id) => [id, to])]))
      if (keys.length) setTouched((prev) => new Set([...prev, ...keys]))
      let message = ''
      let tone = 'ok'
      let result = null
      try {
        result = await run()
        message = describe ? describe(result) : ''
        if (result?.failed?.length) tone = 'error'
      } catch (err) {
        message = err.message
        tone = 'error'
      }
      await load()
      setBusy((prev) => {
        const next = new Set(prev)
        list.forEach((id) => next.delete(id))
        return next
      })
      if (to) {
        setOverrides((prev) => {
          const next = new Map(prev)
          list.forEach((id) => next.delete(id))
          return next
        })
      }
      notify(message, tone)
      try {
        callbacks.current.onCalendarChanged?.()
      } catch {
        /* the calendar refresh is the shell's business */
      }
      return result
    },
    [load, notify],
  )

  const dismissToast = useCallback(() => setToast(null), [])

  return { api, summary, running, loadError, load, statusOf, isBusy, mutate, touched, toast, notify, dismissToast }
}

/** A clock for live counters; ticks only while `enabled`. */
export function useNow(enabled, interval = 1000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return undefined
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), interval)
    return () => clearInterval(timer)
  }, [enabled, interval])
  return now
}

/** Closes a popover/confirm when the user taps elsewhere or presses Escape. */
export function useDismiss(open, close, ref) {
  useEffect(() => {
    if (!open) return undefined
    const onPointer = (e) => {
      if (ref.current && !ref.current.contains(e.target)) close()
    }
    const onKey = (e) => {
      if (e.key === 'Escape') close()
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, close, ref])
}
