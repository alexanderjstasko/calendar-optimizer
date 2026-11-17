// Inline stroke icons (24px grid, currentColor) for the shell: tab bar and
// calendar nav. Decorative: callers put the accessible name on the button/link.

function Svg({ size = 20, children, ...rest }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  )
}

export const CalendarIcon = (p) => (
  <Svg {...p}>
    <rect x="3.5" y="5" width="17" height="15.5" rx="3" />
    <path d="M8 3v4M16 3v4M3.5 10h17" />
    <path d="M8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01" stroke-width="2.4" />
  </Svg>
)

// A ticket: the Events tab holds scouted event options.
export const TicketIcon = (p) => (
  <Svg {...p}>
    <path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h14a1.5 1.5 0 0 1 1.5 1.5V10a2 2 0 0 0 0 4v2.5A1.5 1.5 0 0 1 19 18H5a1.5 1.5 0 0 1-1.5-1.5V14a2 2 0 0 0 0-4z" />
    <path d="M14.5 7.5v1.25M14.5 11.4v1.2M14.5 15.25v1.25" />
  </Svg>
)

// A clock: the Hours tab holds the sleep, work and free-time ranges.
export const ClockIcon = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </Svg>
)

// A checklist: the Plan tab holds the optimizer's proposed series.
export const PlanIcon = (p) => (
  <Svg {...p}>
    <path d="m3.5 7 2 2 3.5-3.5M3.5 16.5l2 2L9 15M12.5 7.5h8M12.5 17h8" />
  </Svg>
)

export const ChevronLeft = (p) => (
  <Svg {...p}>
    <path d="m14.5 18-6-6 6-6" />
  </Svg>
)

export const ChevronRight = (p) => (
  <Svg {...p}>
    <path d="m9.5 18 6-6-6-6" />
  </Svg>
)

export const ChevronDown = (p) => (
  <Svg {...p}>
    <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />
  </Svg>
)

export const SyncIcon = (p) => (
  <Svg {...p}>
    <path d="M20 11.5A8 8 0 0 0 6.1 6.6L4 8.6" />
    <path d="M4 4v4.6h4.6" />
    <path d="M4 12.5a8 8 0 0 0 13.9 4.9l2.1-2" />
    <path d="M20 20v-4.6h-4.6" />
  </Svg>
)

