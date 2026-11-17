import { CalendarIcon, ClockIcon, PlanIcon, TicketIcon } from './icons.jsx'

// The app's four sections. Rendered twice by the shell: variant "top" is the
// segmented control in the header (wider screens), variant "bottom" the
// phone tab bar (<= 640px). CSS shows exactly one of them, so only one is
// ever in the accessibility tree. Plain links: the hash router does the rest,
// and back/forward move between tabs.

const ITEMS = [
  { tab: 'calendar', label: 'Calendar', Icon: CalendarIcon },
  { tab: 'events', label: 'Events', Icon: TicketIcon },
  { tab: 'plan', label: 'Plan', Icon: PlanIcon },
  { tab: 'hours', label: 'Hours', Icon: ClockIcon },
]

const BADGE_HINT = { events: 'event options to review', plan: 'plan series to review' }

/**
 * tab: the active tab; badges: {events?: number, plan?: number} (missing or 0
 * = no badge); variant: 'top' | 'bottom'.
 */
export function TabBar({ tab, badges = {}, variant }) {
  return (
    <nav class={`tabbar tabbar-${variant}`} aria-label="Sections">
      {ITEMS.map(({ tab: key, label, Icon }) => {
        const count = badges[key] || 0
        const active = tab === key
        const shown = count > 99 ? '99+' : String(count)
        return (
          <a
            key={key}
            href={`#/${key}`}
            class={`tabbar-item${active ? ' is-active' : ''}`}
            aria-current={active ? 'page' : undefined}
            title={count ? `${label}: ${count} ${BADGE_HINT[key]}` : undefined}
          >
            {variant === 'bottom' ? (
              <>
                <span class="tabbar-icon">
                  <Icon size={24} />
                  {count > 0 && (
                    <span class="tabbar-badge" aria-hidden="true">
                      {shown}
                    </span>
                  )}
                </span>
                <span class="tabbar-label">{label}</span>
              </>
            ) : (
              <>
                <span class="tabbar-label">{label}</span>
                {count > 0 && (
                  <span class="tabbar-count" aria-hidden="true">
                    {shown}
                  </span>
                )}
              </>
            )}
            {count > 0 && <span class="visually-hidden">{`, ${count} ${BADGE_HINT[key]}`}</span>}
          </a>
        )
      })}
    </nav>
  )
}
