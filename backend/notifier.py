"""
Email digest of approval requests (pending proposals), plus an optional push.

After an optimizer or scout run proposes something, `notify_run()` emails that
run's pending proposals in the shape of the app's review tabs: event options
as compact cards; the plan as one line per repeating series
(`review_model.group_series`, the Plan tab's grouping, with each occurrence's
travel folded in as "+ travel") and the one-off blocks by day. Buttons
deep-link into the app: /#/events, /#/plan and /#/calendar?preview=1. Every
link only opens a page -- nothing in the email approves or rejects anything,
so a mail scanner that prefetches links can't make a decision. Only shown
options (status 'proposed') are listed; scout options waiting in the 'queued'
reserve never are. `send_pending_digest()` emails every upcoming pending
proposal on demand.

Transport is plain SMTP (stdlib smtplib, STARTTLS or SSL), so any provider
works; for Gmail use smtp.gmail.com:587 and an app password. Settings (.env):

    NOTIFY_EMAIL_TO        recipient; unset = email off
    NOTIFY_SMTP_HOST       default smtp.gmail.com
    NOTIFY_SMTP_PORT       default 587 (465 = implicit SSL)
    NOTIFY_SMTP_USER       login; also the default sender
    NOTIFY_SMTP_PASSWORD   app password
    NOTIFY_EMAIL_FROM      default NOTIFY_SMTP_USER
    NOTIFY_APP_URL         base of every link in the email; default VITE_API_URL
    NOTIFY_REVIEW_URL      overrides the "Review plan" link; default NOTIFY_APP_URL + /#/plan
    NOTIFY_CALENDAR_URL    calendar link, added on Mondays; default NOTIFY_APP_URL + /
    NOTIFY_PUSHOVER        1 = also send one Pushover notification per run (default off)
    PUSHOVER_USER          Pushover user key     } read only when NOTIFY_PUSHOVER=1
    PUSHOVER_TOKEN         Pushover API token    }

A failed send never fails the run that triggered it: it is logged and the
proposals stay in the app as usual.

CLI: python3 notifier.py [--dry-run] [--html PATH]   (full pending digest;
     --html writes the HTML version to PATH and sends nothing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import random

import db
import display_zone

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

logger = logging.getLogger("calendar_optimizer.notifier")

DISPLAY_TZ = display_zone.ZONE
# One of these ends every subject line, picked at random (the person's request).
SUBJECT_EMOJI = ("🦉", "🎲", "🌞", "🍀", "🚀", "🎯", "🧭", "🌈", "🐙", "🎈", "🦊", "🍉", "🛼", "🎷", "🪴", "⚡", "🧩", "🏔️", "🐢", "🎨")
# The weekly plan email: "10/26 - 11/1 Cal Review"; a run mid-week: "10/28 - 11/1 Cal Review".
PLAN_SUBJECT = "{span} Cal Review {emoji}"
CHANGES_SUBJECT = "Calendar Changes to Review!! {emoji}"
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
_TRUTHY = {"1", "true", "yes", "on"}


class NotifyError(Exception):
    pass


def _setting(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def load_config() -> dict:
    user = _setting("NOTIFY_SMTP_USER")
    app_url = _setting("NOTIFY_APP_URL", _setting("VITE_API_URL", "http://localhost:8000")).rstrip("/")
    return {
        "to": _setting("NOTIFY_EMAIL_TO"),
        "host": _setting("NOTIFY_SMTP_HOST", "smtp.gmail.com"),
        "port": int(_setting("NOTIFY_SMTP_PORT", "587")),
        "user": user,
        "password": _setting("NOTIFY_SMTP_PASSWORD"),
        "sender": _setting("NOTIFY_EMAIL_FROM", user),
        # Deep links into the app's tabs. They only navigate (the part after '#'
        # never reaches the server), so a scanner prefetching them changes nothing.
        "events_url": f"{app_url}/#/events",
        "plan_url": _setting("NOTIFY_REVIEW_URL", f"{app_url}/#/plan"),
        "preview_url": f"{app_url}/#/calendar?preview=1",
        "calendar_url": _setting("NOTIFY_CALENDAR_URL", f"{app_url}/"),
        "pushover": _setting("NOTIFY_PUSHOVER").lower() in _TRUTHY,
        "pushover_user": _setting("PUSHOVER_USER"),
        "pushover_token": _setting("PUSHOVER_TOKEN"),
    }


def config_problem(config: dict) -> str | None:
    """Why email can't be sent, or None when it can."""
    if not config["to"]:
        return "NOTIFY_EMAIL_TO is not set"
    if not config["user"] or not config["password"]:
        return "NOTIFY_SMTP_USER / NOTIFY_SMTP_PASSWORD are not set"
    if not config["sender"]:
        return "NOTIFY_EMAIL_FROM is not set"
    return None


# --- formatting helpers ----------------------------------------------------------


def _start(p: dict) -> dt.datetime:
    return db.parse_event_time(p["start"], p.get("all_day"))


def _day_key(p: dict) -> str:
    return p["start"][:10] if p.get("all_day") else _start(p).date().isoformat()


def _clock(t: dt.time | dt.datetime, meridiem: bool = True) -> str:
    """8:15 AM, 5 PM; without AM/PM when a range shares it (8:15 – 8:45 AM)."""
    hour = t.hour % 12 or 12
    text = f"{hour}:{t.minute:02d}" if t.minute else str(hour)
    return f"{text} {'AM' if t.hour < 12 else 'PM'}" if meridiem else text


def _time_range(p: dict) -> str:
    if p.get("all_day"):
        return "All day"
    start, end = _start(p), db.parse_event_time(p["end"], 0)
    shared = start.date() == end.date() and (start.hour < 12) == (end.hour < 12)
    return f"{_clock(start, meridiem=not shared)} – {_clock(end)}"


def _short_day(key: str) -> str:
    d = dt.date.fromisoformat(key)
    return f"{d:%a}, {d:%b} {d.day}"


def _date_span(first: str, last: str) -> str:
    a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    if a == b:
        return f"{a:%b} {a.day}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%b} {a.day} – {b.day}"
    return f"{a:%b} {a.day} – {b:%b} {b.day}"


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    if not rest:
        return f"{hours} h"
    return f"{hours}.5 h" if rest == 30 else f"{hours} h {rest} min"


def _clean(text: str | None) -> str:
    """Display text: decode entities some sources leave in titles ("Sci&shy;ence"), drop soft hyphens."""
    return " ".join(html.unescape(text or "").replace("\u00ad", "").split())


def _why(rationale: str | None, limit: int = 160) -> str:
    """The reason half of an option's rationale, about two lines (the scout's "Fit:" logistics stay in the app)."""
    text = re.split(r"\s*\bFit:\s", _clean(rationale), maxsplit=1)[0].strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—–-") + "…"
    return text


def _web_link(url: str | None) -> str | None:
    return url if url and url.lower().startswith(("https://", "http://")) else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def upcoming(proposals: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """Drop proposals whose day has already passed (they can't usefully be approved)."""
    today = (now or dt.datetime.now(DISPLAY_TZ)).astimezone(DISPLAY_TZ).date().isoformat()
    return [p for p in proposals if _day_key(p) >= today]


def _by_day(proposals: list[dict]) -> list[tuple[str, list[dict]]]:
    days: dict[str, list[dict]] = {}
    for p in sorted(proposals, key=_start):
        days.setdefault(_day_key(p), []).append(p)
    return sorted(days.items())


# --- content: what the email lists -------------------------------------------------


def build_digest(proposals: list[dict]) -> dict:
    """
    The email's content, split like the review tabs: event options, then the
    plan as repeating series (2+ occurrences, travel attached) plus one-off
    blocks. Only shown options count: 'queued' reserve options never appear.
    """
    shown = [p for p in proposals if (p.get("status") or "proposed") == "proposed"]
    events = sorted((p for p in shown if db.proposal_group(p["item_type"]) == "events"), key=_start)
    plan = [p for p in shown if db.proposal_group(p["item_type"]) != "events"]
    series, one_offs = _plan_series(plan)
    return {"events": events, "plan_blocks": len(plan), "series": series, "one_offs": one_offs}


def _size(digest: dict) -> int:
    return len(digest["events"]) + digest["plan_blocks"]


def _plan_series(plan: list[dict]) -> tuple[list[dict], list[dict]]:
    """(series, one_offs) from review_model.group_series, the grouping the Plan tab shows."""
    if not plan:
        return [], []
    try:
        import review_model  # imported here so a missing or broken grouper can't break the API's import

        groups = review_model.group_series(plan)
    except Exception as exc:  # noqa: BLE001 -- list the plan by day rather than lose the email
        logger.warning("plan series grouping failed, listing the plan by day: %s", exc)
        return [], sorted(({**p, "travel": None} for p in plan), key=_start)
    series, one_offs, listed = [], [], set()
    for group in groups:
        occurrences = sorted(group.get("occurrences") or [], key=_start)
        for o in occurrences:
            listed.update(x.get("id") for x in (o, o.get("travel")) if x)
        if len(occurrences) > 1:
            series.append(_series_view(group, occurrences))
        else:
            one_offs.extend(occurrences)
    # Never drop a block: anything the grouper didn't return is listed as a one-off.
    one_offs += [{**p, "travel": None} for p in plan if p.get("id") not in listed]
    series.sort(key=lambda s: (-s["count"], s["first"]))
    return series, sorted(one_offs, key=_start)


def _series_view(group: dict, occurrences: list[dict]) -> dict:
    """One series as listed: title, count, details ("daily 8:15 AM", "30 min"), date span, travel."""
    days = [_day_key(o) for o in occurrences]
    time = str(group.get("time") or "").strip()
    hhmm = re.fullmatch(r"(\d{1,2}):(\d{2})", time)
    clock = _clock(dt.time(int(hhmm[1]), int(hhmm[2]))) if hhmm else ""
    when = " ".join(x for x in (str(group.get("cadence") or "").strip(), clock) if x)
    details = [when] if when else []
    if time and not hhmm:
        details.append("times vary" if time == "varies" else time)
    if group.get("duration_min"):
        details.append(_duration(int(group["duration_min"])))
    if not details and group.get("summary"):
        details.append(str(group["summary"]))
    return {
        "title": _clean(group.get("title") or occurrences[0].get("title")),
        "count": len(occurrences),
        "details": details,
        "first": min(days),
        "span": _date_span(min(days), max(days)),
        "travel": bool(group.get("has_travel") or any(o.get("travel") for o in occurrences)),
    }


def pick_emoji(rng: random.Random | None = None) -> str:
    return (rng or random).choice(SUBJECT_EMOJI)


def _md(value: str) -> str:
    """'2026-10-26...' -> '10/26' (no leading zeros, the person's format)."""
    d = dt.date.fromisoformat(value[:10])
    return f"{d.month}/{d.day}"


def plan_subject(window_start: str | None, window_end: str | None, *, emoji: str | None = None) -> str:
    """The weekly plan email's subject: "10/26 - 11/1 Cal Review 🦉" for a Monday-to-Sunday window."""
    if not (window_start and window_end):
        return f"Cal Review {emoji or pick_emoji()}"
    first = db.parse_event_time(window_start, False).astimezone(DISPLAY_TZ)
    last = db.parse_event_time(window_end, False).astimezone(DISPLAY_TZ) - dt.timedelta(minutes=1)
    span = f"{first.month}/{first.day} - {last.month}/{last.day}"
    return PLAN_SUBJECT.format(span=span, emoji=emoji or pick_emoji())


def changes_subject(*, emoji: str | None = None) -> str:
    return CHANGES_SUBJECT.format(emoji=emoji or pick_emoji())


def subject_line(digest: dict) -> str:
    """
    The counts, used for the on-demand digest and the push title, e.g.
    "5 event options · 13 plan items (81 blocks) to review". A plan item is
    one decision in the Plan tab (a series or a one-off), the number its badge shows.
    """
    parts = []
    if digest["events"]:
        parts.append(_plural(len(digest["events"]), "event option"))
    blocks, items = digest["plan_blocks"], len(digest["series"]) + len(digest["one_offs"])
    if blocks:
        parts.append(_plural(blocks, "plan block") if items == blocks
                     else f"{_plural(items, 'plan item')} ({_plural(blocks, 'block')})")
    return " · ".join(parts) + " to review" if parts else "Nothing to review"


def _series_line(s: dict) -> str:
    """Read the News — daily 8:15 AM, 30 min — 24× Oct 12 – Nov 7"""
    parts = [s["title"], ", ".join(s["details"]), f"{s['count']}× {s['span']}" + (" + travel" if s["travel"] else "")]
    return " — ".join(x for x in parts if x)


def _preheader(digest: dict, limit: int = 120) -> str:
    """The inbox preview line: the names, since the subject already has the counts."""
    names = [_clean(p.get("title")) for p in digest["events"]] + [s["title"] for s in digest["series"]]
    names += [_clean(p.get("title")) for p in digest["one_offs"]]
    text = ", ".join(names)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",") + "…"


# --- plain text ------------------------------------------------------------------


def render_text(digest: dict, *, config: dict, intro: str, calendar_url: str | None = None) -> str:
    """The HTML's plain-text twin: same sections and links, one line per series."""
    sections: list[tuple[list[str], list[str]]] = []  # (body lines, link lines)
    if digest["events"]:
        body = [f"EVENT OPTIONS ({len(digest['events'])})", ""]
        for p in digest["events"]:
            meta = " · ".join(x for x in (_clean(p.get("location")), _clean(p.get("cost"))) if x)
            why, url = _why(p.get("rationale")), _web_link(p.get("url"))
            body += [f"{_short_day(_day_key(p))} · {_time_range(p)}", f"  {_clean(p.get('title'))}"]
            body += [f"  {x}" for x in (meta, why and f"Why: {why}", url) if x] + [""]
        sections.append((body, [f"Review events: {config['events_url']}"]))
    if digest["plan_blocks"]:
        body = [f"YOUR PLAN ({_plural(digest['plan_blocks'], 'block').upper()})", ""]
        if digest["series"]:
            body += ["Repeating:", *(f"  {_series_line(s)}" for s in digest["series"]), ""]
        if digest["one_offs"]:
            body.append("One-offs:" if digest["series"] else "By day:")
            for key, rows in _by_day(digest["one_offs"]):
                for n, p in enumerate(rows):
                    day = _short_day(key) if n == 0 else ""
                    travel = " + travel" if p.get("travel") else ""
                    body.append(f"  {day:<12}{_time_range(p):<18}{_clean(p.get('title'))}{travel}")
            body.append("")
        sections.append((body, [f"Review plan: {config['plan_url']}"]))
    if sections:  # the preview shows every pending proposal, so it closes the last section
        sections[-1][1].append(f"Preview on calendar: {config['preview_url']}")
    lines = [intro, ""]
    for body, links in sections:
        lines += body + links + [""]
    if calendar_url:
        lines += [f"Your calendar: {calendar_url}", ""]
    lines.append("These links only open the app; nothing is approved or rejected from this email.")
    return "\n".join(lines)


# --- HTML ------------------------------------------------------------------------
# Table layout with inline styles, so Gmail (which drops most <style> rules)
# renders it as designed; it is fluid up to 600px, so it fits a phone without
# zooming. Colours are the app's light tokens. The <style> block only re-colours
# for clients that honour prefers-color-scheme (Apple Mail); Gmail's apps darken
# light emails themselves, and these mid-contrast colours survive that.

_C = {
    "bg": "#f6f7fb", "card": "#ffffff", "ink": "#1b1d2a", "muted": "#626780", "line": "#e3e6ef",
    "accent": "#4f5fd1", "on_accent": "#ffffff", "travel": "#fbe9d0", "travel_ink": "#5a3b06",
}
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'
_CSS = (
    "a[x-apple-data-detectors]{color:inherit!important;text-decoration:none!important}"
    "@media (prefers-color-scheme: dark){"
    ".bg{background:#14161e!important}"
    ".card{background:#1c1f2a!important;border-color:#333748!important}"
    ".ink{color:#e8eaf2!important}.muted{color:#a3a8ba!important}.accent{color:#8d9cf0!important}"
    ".line{border-color:#333748!important}"
    ".btn{background:#8d9cf0!important;border-color:#8d9cf0!important;color:#0f1330!important}"
    ".btn2{border-color:#8d9cf0!important;color:#8d9cf0!important}"
    ".chip{background:#3e2b10!important;color:#f0c98a!important}}"
)


def _font(size: int, color: str, weight: int = 400, line: int | None = None) -> str:
    return (f"font-family:{_FONT};font-size:{size}px;line-height:{line or round(size * 1.4)}px;"
            f"font-weight:{weight};color:{color}")


def _eyebrow(text: str, cls: str = "muted", color: str = _C["muted"]) -> str:
    return (f'<div class="{cls}" style="{_font(12, color, 700, 16)};letter-spacing:.05em;'
            f'text-transform:uppercase">{html.escape(text)}</div>')


def _travel_chip() -> str:
    return (f' <span class="chip" style="display:inline-block;padding:0 7px;border-radius:9px;'
            f'background:{_C["travel"]};{_font(11, _C["travel_ink"], 600, 18)};vertical-align:middle;'
            f'white-space:nowrap">+ travel</span>')


def _button(label: str, href: str, primary: bool = True) -> str:
    """
    A bulletproof button: the cell carries the colour (also as bgcolor, for clients
    that ignore CSS backgrounds) and the padded link is the tap target. Buttons
    float left, so a row of them wraps on a narrow phone instead of squeezing.
    """
    if primary:
        cls, color, fill = "btn", _C["on_accent"], f"background:{_C['accent']};"
        cell = f'class="btn" bgcolor="{_C["accent"]}" style="border-radius:10px;{fill}"'
    else:
        cls, color, fill, cell = "btn2", _C["accent"], "", 'style="border-radius:10px"'
    style = (f"display:inline-block;padding:11px 18px;{_font(15, color, 600, 20)};{fill}"
             f"border:1px solid {_C['accent']};border-radius:10px;text-decoration:none")
    return (f'<table {_TABLE} align="left" style="margin:0 10px 10px 0"><tr><td {cell}>'
            f'<a class="{cls}" href="{html.escape(href)}" style="{style}">{html.escape(label)}</a>'
            "</td></tr></table>")


def _button_row(buttons: list[tuple[str, str, bool]]) -> str:
    return f'<tr><td style="padding:4px 0 8px">{"".join(_button(*b) for b in buttons)}</td></tr>'


def _heading(title: str, count: str, top: int) -> str:
    return (f'<tr><td style="padding:{top}px 2px 10px">'
            f'<span class="ink" style="{_font(18, _C["ink"], 700, 24)}">{html.escape(title)}</span>'
            f'<span class="muted" style="{_font(15, _C["muted"], 500, 24)}">&nbsp;&nbsp;{html.escape(count)}</span>'
            "</td></tr>")


def _card(inner: str, padding: str = "12px 14px") -> str:
    return (f'<table {_TABLE} width="100%" class="card" bgcolor="{_C["card"]}" style="background:{_C["card"]};'
            f'border:1px solid {_C["line"]};border-radius:12px"><tr><td style="padding:{padding}">{inner}'
            "</td></tr></table>")


def _event_card(p: dict) -> str:
    title = html.escape(_clean(p.get("title")))
    url = _web_link(p.get("url"))
    if url:
        title = (f'<a class="ink" href="{html.escape(url)}" style="{_font(16, _C["ink"], 600, 21)};'
                 f'text-decoration:none">{title}</a>')
    meta = " · ".join(x for x in (_clean(p.get("location")), _clean(p.get("cost"))) if x)
    why = _why(p.get("rationale"))
    inner = (
        _eyebrow(f"{_short_day(_day_key(p))} · {_time_range(p)}", "accent", _C["accent"])
        + f'<div class="ink" style="{_font(16, _C["ink"], 600, 21)};padding-top:4px">{title}</div>'
        + (f'<div class="muted" style="{_font(13, _C["muted"], 400, 18)};padding-top:3px">{html.escape(meta)}</div>'
           if meta else "")
        + (f'<div class="muted" style="{_font(13, _C["muted"], 400, 18)};padding-top:6px">{html.escape(why)}</div>'
           if why else "")
    )
    return f'<tr><td style="padding:0 0 8px">{_card(inner)}</td></tr>'


def _series_table(series: list[dict]) -> str:
    rows = []
    for n, s in enumerate(series):
        top = f"border-top:1px solid {_C['line']};" if n else ""
        # Each chunk stays on one line (a date span would otherwise break after its dash),
        # so a narrow phone wraps the details only at the dots.
        meta = " · ".join(f'<span style="white-space:nowrap">{html.escape(x)}</span>'
                          for x in s["details"] + [s["span"]])
        rows.append(
            f'<tr><td class="line" style="{top}padding:9px 0;vertical-align:top">'
            f'<div class="ink" style="{_font(15, _C["ink"], 600, 21)}">{html.escape(s["title"])}'
            f'{_travel_chip() if s["travel"] else ""}</div>'
            f'<div class="muted" style="{_font(13, _C["muted"], 400, 18)};padding-top:1px">{meta}</div></td>'
            f'<td class="line muted" align="right" style="{top}padding:9px 0 9px 12px;vertical-align:top;'
            f'white-space:nowrap;{_font(15, _C["muted"], 600, 21)}">{s["count"]}&times;</td></tr>'
        )
    return f'<table {_TABLE} width="100%">{"".join(rows)}</table>'


def _one_off_table(one_offs: list[dict]) -> str:
    rows = []
    for key, items in _by_day(one_offs):
        rows.append(f'<tr><td colspan="2" style="padding:10px 0 2px">'
                    f'{_eyebrow(_short_day(key))}</td></tr>')
        for p in items:
            rows.append(
                f'<tr><td class="muted" style="{_font(14, _C["muted"], 400, 20)};white-space:nowrap;'
                f'vertical-align:top;padding:2px 12px 2px 0">{html.escape(_time_range(p))}</td>'
                f'<td class="ink" style="{_font(14, _C["ink"], 600, 20)};vertical-align:top;padding:2px 0;width:100%">'
                f'{html.escape(_clean(p.get("title")))}{_travel_chip() if p.get("travel") else ""}</td></tr>'
            )
    return f'<table {_TABLE} width="100%">{"".join(rows)}</table>'


def _plan_card(digest: dict) -> str:
    parts = []
    if digest["series"]:
        parts.append(f'<div style="padding:2px 0 4px">{_eyebrow("Repeating")}</div>{_series_table(digest["series"])}')
    if digest["one_offs"]:
        divider = f"border-top:1px solid {_C['line']};margin-top:6px;padding-top:12px" if digest["series"] else ""
        label = "One-offs" if digest["series"] else "By day"
        parts.append(f'<div class="line" style="{divider}">{_eyebrow(label)}{_one_off_table(digest["one_offs"])}</div>')
    return f'<tr><td style="padding:0 0 8px">{_card("".join(parts), "12px 14px 10px")}</td></tr>'


def render_html(digest: dict, *, config: dict, intro: str, subject: str, now: dt.datetime,
                calendar_url: str | None = None) -> str:
    sections = []
    if digest["events"]:
        sections.append(("Event options", str(len(digest["events"])),
                         "".join(_event_card(p) for p in digest["events"]),
                         [("Review events", config["events_url"], True)]))
    if digest["plan_blocks"]:
        sections.append(("Your plan", _plural(digest["plan_blocks"], "block"), _plan_card(digest),
                         [("Review plan", config["plan_url"], True)]))
    if sections:  # the preview shows every pending proposal, so it closes the last section
        sections[-1][3].append(("Preview on calendar", config["preview_url"], False))

    rows = [
        f'<tr><td style="padding:0 2px 14px">{_eyebrow(f"Calendar · {now:%a}, {now:%b} {now.day}")}'
        f'<div class="ink" style="{_font(17, _C["ink"], 600, 24)};padding-top:6px">{html.escape(intro)}</div></td></tr>'
    ]
    for n, (title, count, body, buttons) in enumerate(sections):
        rows += [_heading(title, count, 6 if n == 0 else 22), body, _button_row(buttons)]
    if calendar_url:
        rows.append(_button_row([("Open calendar", calendar_url, False)]))
    rows.append(
        f'<tr><td class="line" style="padding:14px 2px 0;border-top:1px solid {_C["line"]}">'
        f'<div class="muted" style="{_font(12, _C["muted"], 400, 18)}">These links only open the app. '
        "Nothing is approved or rejected from this email.</div></td></tr>"
    )
    hidden = "display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;font-size:1px;line-height:1px"
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="x-apple-disable-message-reformatting">',
        '<meta name="format-detection" content="telephone=no, date=no, address=no, email=no, url=no">',
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">',
        f"<title>{html.escape(subject)}</title><style>{_CSS}</style></head>",
        f'<body class="bg" style="margin:0;padding:0;background:{_C["bg"]};-webkit-text-size-adjust:100%;'
        'text-size-adjust:100%">',
        f'<div style="{hidden}">{html.escape(_preheader(digest))}{"&#847;&zwnj;&nbsp;" * 30}</div>',
        f'<table {_TABLE} width="100%" class="bg" bgcolor="{_C["bg"]}" style="background:{_C["bg"]}">'
        '<tr><td align="center" style="padding:20px 12px 28px">',
        f'<table {_TABLE} width="100%" style="max-width:600px;width:100%">',
        *rows,
        "</table></td></tr></table></body></html>",
    ])


# --- the message -------------------------------------------------------------------


def build_message(proposals: list[dict], *, config: dict, intro: str, now: dt.datetime | None = None) -> EmailMessage:
    """Plain-text + HTML email listing the proposals the way the review tabs show them."""
    return _compose(build_digest(proposals), config=config, intro=intro, now=now)


def _compose(digest: dict, *, config: dict, intro: str, now: dt.datetime | None = None,
             subject: str | None = None, link_calendar: bool = False) -> EmailMessage:
    now = (now or dt.datetime.now(DISPLAY_TZ)).astimezone(DISPLAY_TZ)
    # Mondays start the week, and so does the Sunday plan email: link the calendar itself too.
    calendar_url = config.get("calendar_url") if (now.weekday() == 0 or link_calendar) else None
    subject = subject or subject_line(digest)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["sender"]
    msg["To"] = config["to"]
    msg.set_content(render_text(digest, config=config, intro=intro, calendar_url=calendar_url))
    msg.add_alternative(
        render_html(digest, config=config, intro=intro, subject=subject, now=now, calendar_url=calendar_url),
        subtype="html",
    )
    return msg


# --- sending -------------------------------------------------------------------


def send(msg: EmailMessage, config: dict) -> None:
    context = ssl.create_default_context()
    try:
        if config["port"] == 465:
            with smtplib.SMTP_SSL(config["host"], config["port"], context=context, timeout=30) as smtp:
                smtp.login(config["user"], config["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(config["host"], config["port"], timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(config["user"], config["password"])
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"{type(exc).__name__}: {exc}") from exc


def _deliver(digest: dict, *, intro: str, dry_run: bool, config: dict, subject: str | None = None,
             link_calendar: bool = False) -> dict:
    summary = {"count": _size(digest), "to": config["to"] or None, "subject": subject or subject_line(digest), "sent": False}
    if not summary["count"]:
        summary["skipped"] = "nothing pending"
        return summary
    msg = _compose(digest, config=config, intro=intro, subject=subject, link_calendar=link_calendar)
    if dry_run:
        summary["preview"] = msg.get_body(("plain",)).get_content()
        return summary
    problem = config_problem(config)
    if problem:
        summary["skipped"] = problem
        return summary
    send(msg, config)
    summary["sent"] = True
    return summary


def push_text(digest: dict, limit: int = 1024) -> str:
    """The Pushover body: one short line per option / series (Pushover allows 1024 characters)."""
    lines = [f"{_short_day(_day_key(p))} {_time_range(p)} · {_clean(p.get('title'))}" for p in digest["events"]]
    lines += [
        f"{s['title']} {s['count']}× · {s['details'][0] if s['details'] else s['span']}"
        + (" + travel" if s["travel"] else "")
        for s in digest["series"]
    ]
    if digest["one_offs"]:
        lines.append(f"+ {_plural(len(digest['one_offs']), 'one-off block')}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 2].rsplit("\n", 1)[0] + "\n…"


def send_push(config: dict, *, title: str, message: str, url: str, url_title: str) -> bool:
    """
    One Pushover notification (NOTIFY_PUSHOVER=1 with PUSHOVER_USER / PUSHOVER_TOKEN):
    a single stdlib POST. Never raises; returns whether Pushover accepted it.
    """
    if not config.get("pushover"):
        return False
    if not (config.get("pushover_user") and config.get("pushover_token")):
        logger.warning("NOTIFY_PUSHOVER is on but PUSHOVER_USER / PUSHOVER_TOKEN are not set")
        return False
    form = urllib.parse.urlencode({
        "token": config["pushover_token"], "user": config["pushover_user"],
        "title": title[:250], "message": message[:1024] or title[:250],
        "url": url[:512], "url_title": url_title[:100],
    }).encode()
    request = urllib.request.Request(PUSHOVER_API_URL, data=form, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            reply = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("errors")
        except Exception:  # noqa: BLE001
            detail = None
        logger.error("Pushover refused the notification: HTTP %s %s", exc.code, detail or "")
        return False
    except Exception as exc:  # noqa: BLE001 -- a notification must never break the run
        logger.error("Pushover notification failed: %s", exc)
        return False
    if not isinstance(reply, dict) or reply.get("status") != 1:
        logger.error("Pushover refused the notification: %s", reply.get("errors") if isinstance(reply, dict) else reply)
        return False
    return True


def _pending_digest() -> tuple[dict, str]:
    db.expire_past_proposals(dt.datetime.now(DISPLAY_TZ))
    digest = build_digest(upcoming(db.get_proposals(status="proposed")))
    return digest, f"{_plural(_size(digest), 'proposal')} waiting for your decision."


def send_pending_digest(dry_run: bool = False) -> dict:
    """Email every upcoming pending proposal. Raises NotifyError if SMTP fails."""
    digest, intro = _pending_digest()
    return _deliver(digest, intro=intro, dry_run=dry_run, config=load_config())


def _run_intro(kind: str, digest: dict, total: int) -> str:
    events, blocks = len(digest["events"]), digest["plan_blocks"]
    if events and not blocks:
        text = f"The {kind} found {_plural(events, 'new event option')}"
    elif blocks and not events:
        text = f"The {kind} planned {_plural(blocks, 'new block')}"
    else:
        text = f"The {kind} proposed {_plural(events + blocks, 'new item')}"
    return text + (f" ({total} pending in total)." if total > events + blocks else ".")


def notify_run(summary: dict, kind: str) -> None:
    """
    After a run: email its new proposals, and push them when Pushover is on.
    An optimizer run's email covers the whole plan now waiting (its own blocks
    plus the routine blocks proposed in the same pass) under the "Cal Review"
    subject with the week's dates. Never raises; logs instead.
    """
    run_id = summary.get("run_id")
    if not run_id or summary.get("dry_run"):
        return
    try:
        run_ids = [run_id]
        routine_run = (summary.get("routine") or {}).get("run_id") if kind == "optimizer" else None
        if routine_run:
            run_ids.append(routine_run)
        rows = [p for rid in run_ids for p in db.get_proposals(run_id=rid, status="proposed")]
        digest = build_digest(upcoming(rows))
        if not _size(digest):
            return
        total = len(upcoming(db.get_proposals(status="proposed")))
        intro, config = _run_intro(kind, digest, total), load_config()
        subject = plan_subject(summary.get("window_start"), summary.get("window_end")) if kind == "optimizer" else None
    except Exception as exc:  # noqa: BLE001 -- a notification must never break the run
        logger.error("approval notification failed: %s", exc)
        return
    try:
        result = _deliver(digest, intro=intro, dry_run=False, config=config, subject=subject, link_calendar=kind == "optimizer")
        if result["sent"]:
            logger.info("emailed %d %s proposals to %s", result["count"], kind, result["to"])
        else:
            logger.info("approval email not sent: %s", result.get("skipped"))
    except Exception as exc:  # noqa: BLE001
        logger.error("approval email failed: %s", exc)
    if config["pushover"]:
        only_events = digest["events"] and not digest["plan_blocks"]
        url, label = (config["events_url"], "Review events") if only_events else (config["plan_url"], "Review plan")
        try:
            if send_push(config, title=subject_line(digest), message=push_text(digest), url=url, url_title=label):
                logger.info("pushed %d %s proposals to Pushover", _size(digest), kind)
        except Exception as exc:  # noqa: BLE001
            logger.error("Pushover notification failed: %s", exc)


# --- the hourly change email (triggers.py) ---------------------------------------------


def change_lines(proposals: list[dict]) -> list[str]:
    """One line per pending change: when, what, and why ("Moved from Tue 5:30 PM: Dentist landed on it")."""
    lines = []
    for p in sorted(proposals, key=_start):
        if p["item_type"] == "travel":
            continue  # shown with its destination in the digest
        what = f"{_short_day(_day_key(p))} · {_time_range(p)} · {_clean(p.get('title'))}"
        why = _clean(p.get("rationale"))
        lines.append(f"{what} — {why}" if why else what)
    return lines


def notify_changes(*, dry_run: bool = False, now: dt.datetime | None = None) -> dict:
    """
    Email every trigger proposal (a new Notion item placed, a move after a
    clash) that no email has listed yet, under "Calendar Changes to Review!!",
    then mark them notified. Called once an hour by the Notion check, so the
    person gets at most one of these an hour. Never raises.
    """
    try:
        pending = upcoming(db.get_unnotified_trigger_proposals(), now)
        out: dict = {"count": len(pending), "sent": False}
        if not pending:
            out["skipped"] = "no new changes"
            return out
        digest = build_digest(pending)
        lines = change_lines(pending)
        intro = f"{_plural(len(lines), 'change')} to your plan since the last email. Nothing moves until you approve it."
        config = load_config()
        summary = _deliver(digest, intro=intro, dry_run=dry_run, config=config, subject=changes_subject())
        out.update(summary, lines=lines)
        if dry_run:
            return out
        if summary["sent"]:
            db.mark_proposals_notified([p["id"] for p in pending], (now or dt.datetime.now(dt.timezone.utc)).isoformat())
            logger.info("emailed %d plan changes to %s", len(pending), summary["to"])
        else:
            logger.info("change email not sent: %s", summary.get("skipped"))
        return out
    except Exception as exc:  # noqa: BLE001 -- a notification must never break the job
        logger.error("change email failed: %s", exc)
        return {"count": 0, "sent": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Email the pending approval requests.")
    parser.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    parser.add_argument("--html", metavar="PATH", help="write the email's HTML version to PATH; sends nothing")
    args = parser.parse_args()
    if args.html:
        pending_digest, intro_text = _pending_digest()
        message = _compose(pending_digest, config=load_config(), intro=intro_text)
        Path(args.html).write_text(message.get_body(("html",)).get_content(), encoding="utf-8")
        print(f"Subject: {message['Subject']}\nwrote {args.html}")
    else:
        result = send_pending_digest(dry_run=args.dry_run)
        if args.dry_run and "preview" in result:
            print(f"To: {result['to']}\nSubject: {result['subject']}\n\n{result['preview']}")
        else:
            print(result)
