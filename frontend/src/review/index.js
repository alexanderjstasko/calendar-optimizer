// Review tabs (WP-B). The shell (app.jsx) mounts these inside its tab panes.
//
// Both tabs take the same props:
//   apiUrl            base URL of the backend ('' in the production bundle)
//   active            true while this tab is the visible one (fetch on activation)
//   onCalendarChanged called after any decision that changes calendar events
//   onSummary(s)      called with every fresh GET /review/summary payload (badges)
//   navigate(tab, p)  switch tabs, e.g. navigate('calendar', {preview: 1})
export { EventsTab } from './EventsTab.jsx'
export { PlanTab } from './PlanTab.jsx'
