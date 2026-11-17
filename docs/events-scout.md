# The events scout (optional)

The scout suggests public events (talks, shows, meetups) that fit your free evenings
and weekends. Suggestions appear in the **Events** tab; switch one on to add it to
your calendar. Nothing is booked for you.

## Turning it on

1. Add pages to the **Website List** in Notion, one URL per row: the events pages of
   places you like, or your city's listings (for example an Eventbrite search page for
   your city).
2. Set `NOTION_WEBSITES_DB_ID` in `.env` and restart.

Without a Website List the scout is off, and nothing else changes.

## How it works

- **Weekly fetch** (`SCOUT_FETCH_DAYS`/`TIME`, default Monday 06:00): every site in the
  list is read for upcoming events. Eventbrite listing pages (any city) and a few New
  York sites (NYPL, DoNYC, Club Free Time) have built-in readers. **Any other page** is
  read by Claude from the page's text, which works for most listing pages and costs a
  few more tokens.
- **Daily picks** (`SCOUT_RECOMMEND_DAYS`/`TIME`, default every day 07:00): only events
  that start in your free time (from the Hours tab) and do not clash with your calendar
  are kept; Claude ranks them by your **Event Interest** routines, their notes, the key
  words in the Hours tab and what you approved or turned down before. The best 8 are
  shown; **Reload** shows the next ones.
- After an approved event has passed, the app counts it in the site's
  **Events Attended** and in the routine it served.

## Costs

The daily pick is the scout's main cost (around 35-40k tokens in, 10k out per day).
To lower it, run it less often, e.g. `SCOUT_RECOMMEND_DAYS=mon,thu`.

## Map and neighbourhoods

The Events tab looks up each venue on OpenStreetMap to show its neighbourhood. The
lookup is tuned for New York; elsewhere it still works for full street addresses. Set
`GEO_LOOKUPS=0` to turn it off.
