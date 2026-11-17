# Notion: what each column does

The [template](../README.md#23-notion) has every column already. This page explains them.
Share both databases with your integration (`•••` → **Connections**), or the app gets a
"404 not found".

## Telos tree (`NOTION_GOALS_DB_ID`)

One database holds everything, as a tree: a row sits under another through its
**Parent item** (the template's **Sub-items** view shows the nesting). The **Level** of a row decides what the app does with it.

| Level | What it is | What the app does |
|---|---|---|
| **Telos** | Why you do things ("Health and Energy") | Context for the planner; never scheduled |
| **Goal**, **Project** | Something you are working toward | Context, and passes its priority down. With **Display** on and a **Session Time**, it is planned itself like a routine; with only a **Time to Complete**, like a task |
| **Routine** | Something you repeat | Placed every week (see **Type**) |
| **Task** | One piece of work | Placed before its **Target Date** |

| Column | Type | Used for |
|---|---|---|
| (title) | title | The name shown on the calendar |
| `Level` | select | Telos, Goal, Project, Routine, Task (above) |
| `Parent item` | relation | Where the row sits in the tree |
| `Status` | select (a status column also works) | `Not started`, `In-Progress`, `On Hold` (pauses the row and everything under it), `Done` and `Dropped` (both leave the plan), `Telos` |
| `Priority` | select | `Critical`, `High`, `Medium`, `Low`. A row without one inherits its parent's |
| `Type` | select | Routines only. `Routine`: placed by fixed rules, no AI. `Flexible`: placed by Claude around everything else. `Event Interest`: never placed; the events scout looks for matching events instead |
| `Times/Week` | number | How often: 7 = daily, 2 = twice a week, 0.5 = every other week, 0.25 = monthly |
| `Session Time (hr)` | number | How long one session is |
| `Time to Complete` | number | Hours a task (or project) needs in total |
| `Time Spent` | number | Hours done so far. **Written by the app** after each block |
| `Target Date` | date | A task's deadline, a goal's target |
| `Display` | checkbox | Off = the planner knows about it but never books it (e.g. "Work", with notes about office days) |
| `Day of Week` | multi-select | Allowed days; empty = any day |
| `Time of Day` | text | A window such as "Morning" or "after 6pm" |
| `Location` | text | Where it happens; the planner adds travel time when it is not at home |
| `Notes for LLM` | text | Free-text instructions Claude follows for this row |
| `Last Occurred` | date | **Written by the app**: the last time it happened |
| `Sessions` | number | **Written by the app**: how many sessions so far |

The app only writes the three columns marked above, and only after a block you
approved has passed.

## Website List (`NOTION_WEBSITES_DB_ID`, optional)

One row per events page the scout should read, for example your city's listings on
Eventbrite or a local events site. Leave `NOTION_WEBSITES_DB_ID` empty to turn the
scout off.

| Column | Type | Used for |
|---|---|---|
| `URL` | title | The page to read |
| `Upcoming Event` | text | **Written by the app** when you approve one of its events |
| `Events Attended` | number | **Written by the app** after you attended one |

How the scout picks events: [events-scout.md](events-scout.md).
