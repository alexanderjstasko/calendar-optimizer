"""notifier: digest shape (options, series, one-offs), links, config gating, SMTP and Pushover (all mocked)."""

from __future__ import annotations

import datetime as dt
import html
import io
import logging
import re
import urllib.error
import urllib.parse
from unittest import mock

import pytest

import notifier
import review_model

NY = notifier.DISPLAY_TZ
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=NY)
TUESDAY = dt.datetime(2026, 10, 6, 9, 0, tzinfo=NY)
APP = "http://host:8000"
_ENV = ("NOTIFY_EMAIL_TO", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD", "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT",
        "NOTIFY_EMAIL_FROM", "NOTIFY_APP_URL", "NOTIFY_REVIEW_URL", "NOTIFY_CALENDAR_URL", "NOTIFY_PUSHOVER",
        "PUSHOVER_USER", "PUSHOVER_TOKEN", "VITE_API_URL")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Throwaway DB, none of the host's NOTIFY_* / PUSHOVER_* settings, and no real SMTP or HTTP."""
    # The digest expires stale proposals first; keep that off the real events.db.
    monkeypatch.setattr(notifier.db, "DB_PATH", tmp_path / "events.db")
    notifier.db.init_db()
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    # Tests that send patch these themselves; anything else reaching them fails the test
    # (notify_run swallows exceptions, hence the check after the test, not just the raise).
    guards = {name: mock.Mock(side_effect=AssertionError(f"unmocked {name}")) for name in ("urlopen", "SMTP", "SMTP_SSL")}
    monkeypatch.setattr(notifier.urllib.request, "urlopen", guards["urlopen"])
    monkeypatch.setattr(notifier.smtplib, "SMTP", guards["SMTP"])
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", guards["SMTP_SSL"])
    yield
    for name, guard in guards.items():
        assert not guard.called, f"a test reached the real {name}"


@pytest.fixture
def config(monkeypatch):
    for key, value in {
        "NOTIFY_EMAIL_TO": "me@example.com", "NOTIFY_SMTP_USER": "bot@example.com",
        "NOTIFY_SMTP_PASSWORD": "pw", "NOTIFY_SMTP_HOST": "smtp.example.com", "NOTIFY_SMTP_PORT": "587",
        "NOTIFY_APP_URL": APP,
    }.items():
        monkeypatch.setenv(key, value)
    return notifier.load_config()


def _p(pid, start, end, item_type="goal", **extra):
    row = {"id": pid, "run_id": "r1", "item_type": item_type, "item_id": "", "title": f"Item {pid}", "start": start,
           "end": end, "all_day": 0, "location": "", "cost": None, "url": None, "rationale": "",
           "status": "proposed", "ref": None}
    return {**row, **extra}


def _at(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=NY)


def _block(pid, start, minutes, item_type="goal", **extra):
    return _p(pid, start.isoformat(), (start + dt.timedelta(minutes=minutes)).isoformat(), item_type, **extra)


def live_shaped():
    """The dev DB's shape in miniature: a daily goal, a Mon & Wed goal with travel, one-offs, options."""
    rows = [_block(f"news{d}", _at(d, 8, 15), 30, item_id="g-news", title="Read the News") for d in range(12, 20)]
    for n, d in enumerate((12, 14, 19, 21), start=1):  # Mondays and Wednesdays; travel ends as the climb starts
        rows.append(_block(f"climb{d}", _at(d, 17, 30), 180, item_id="g-climb", title="Rockclimb", location="Gym"))
        rows.append(_block(f"travel{d}", _at(d, 17), 30, "travel", item_id=f"r{n}", title="Travel to Rockclimb"))
    return rows + [
        _block("laundry", _at(18, 11), 180, item_id="g-laundry", title="Laundry"),
        _block("soccer", _at(18, 16, 50), 40, "travel", item_id="google_x", title="Travel to Soccer"),
        _block("octopus", _at(13, 19), 90, "event", item_id="d1", title="Scientific Controversies: Octopus Umwelt",
               location="Pioneer Works", cost="Free", url="https://example.com/octopus?a=1&b=2",
               rationale="Octopus perception is exactly the lecture the Learning goal asks for. Fit: Tuesday, 35 min."),
        _block("queued1", _at(15, 19), 60, "event", item_id="d2", title="Queued Option", status="queued"),
    ]


def _parts(msg):
    return msg.get_body(("plain",)).get_content(), msg.get_body(("html",)).get_content()


# --- content ---------------------------------------------------------------------


def test_upcoming_drops_past_days():
    rows = [_p("old", "2026-10-04T20:00:00-04:00", "2026-10-04T21:00:00-04:00"),
            _p("today", "2026-10-05T08:00:00-04:00", "2026-10-05T08:30:00-04:00")]
    assert [p["id"] for p in notifier.upcoming(rows, now=NOW)] == ["today"]


def test_message_groups_by_local_day_and_lists_options_first(config):
    rows = [
        # 01:30 UTC on the 7th is 21:30 on the 6th in New York.
        _p("late", "2026-10-07T01:30:00+00:00", "2026-10-07T02:30:00+00:00", item_type="event",
           location="Sony Hall", cost="$20", url="https://example.com/e?a=1&b=2"),
        _p("early", "2026-10-06T08:00:00-04:00", "2026-10-06T08:30:00-04:00"),
    ]
    msg = notifier.build_message(rows, config=config, intro="hi", now=TUESDAY)
    text, body = _parts(msg)
    assert "Tue, Oct 6 · 9:30 – 10:30 PM" in text and "Wed" not in text
    assert (text.index("EVENT OPTIONS (1)") < text.index("Item late") < text.index("YOUR PLAN (1 BLOCK)")
            < text.index("By day:") < text.index("Item early"))
    assert "Sony Hall · $20" in text
    assert 'href="https://example.com/e?a=1&amp;b=2"' in body
    assert msg["Subject"] == "1 event option · 1 plan block to review"
    assert msg["From"] == "bot@example.com" and msg["To"] == "me@example.com"


def test_plan_reads_as_series_lines_with_travel_folded_in(config):
    msg = notifier.build_message(live_shaped(), config=config, intro="hi", now=TUESDAY)
    assert msg["Subject"] == "1 event option · 4 plan items (18 blocks) to review"
    text, body = _parts(msg)
    assert "  Read the News — daily 8:15 AM, 30 min — 8× Oct 12 – 19\n" in text
    assert "  Rockclimb — Mon & Wed 5:30 PM, 3 h — 4× Oct 12 – 21 + travel\n" in text
    assert "Travel to Rockclimb" not in text + body  # folded into its climb, not listed four more times
    one_offs = text[text.index("One-offs:"):]
    assert re.search(r"\n  Sun, Oct 18 +11 AM – 2 PM +Laundry\n", one_offs)
    assert re.search(r"\n +4:50 – 5:30 PM +Travel to Soccer\n", one_offs)  # same day: no second date
    assert (text.index("EVENT OPTIONS (1)") < text.index("YOUR PLAN (18 BLOCKS)") < text.index("Repeating:")
            < text.index("One-offs:") < text.index("Review plan:"))
    # The option card keeps the reason, not the scout's logistics.
    assert "Why: Octopus perception is exactly the lecture the Learning goal asks for.\n" in text
    assert "Fit:" not in text + body
    # HTML: one row per series with its count, the travel chip, and the same details.
    shown = re.sub(r"<[^>]+>", "", body)
    assert "daily 8:15 AM · 30 min · Oct 12 – 19" in shown and "8&times;" in body
    assert "Mon &amp; Wed 5:30 PM · 3 h · Oct 12 – 21" in shown and "+ travel</span>" in body
    assert '<span style="white-space:nowrap">Oct 12 – 19</span>' in body  # a phone wraps only at the dots


def test_queued_options_are_never_listed(config):
    text, body = _parts(notifier.build_message(live_shaped(), config=config, intro="hi", now=TUESDAY))
    assert "Queued Option" not in text + body


def test_links_only_navigate(config):
    text, body = _parts(notifier.build_message(live_shaped(), config=config, intro="hi", now=TUESDAY))
    hrefs = {html.unescape(h) for h in re.findall(r'href="([^"]*)"', body)}
    assert hrefs == {f"{APP}/#/events", f"{APP}/#/plan", f"{APP}/#/calendar?preview=1",
                     "https://example.com/octopus?a=1&b=2"}
    # Nothing a link prefetcher could use to decide: no link points at an API route.
    assert not [h for h in hrefs if re.search(r"approve|reject|revoke|optimizer|scout|notify", h)]
    for line in (f"Review events: {APP}/#/events", f"Review plan: {APP}/#/plan",
                 f"Preview on calendar: {APP}/#/calendar?preview=1"):
        assert line in text
    assert "nothing is approved or rejected from this email" in text


def test_html_is_a_table_layout_email_with_dark_mode(config):
    msg = notifier.build_message(live_shaped(), config=config, intro="hi", now=TUESDAY)
    assert msg.get_content_type() == "multipart/alternative"
    body = msg.get_body(("html",)).get_content()
    assert body.startswith("<!DOCTYPE html>")
    assert body.count("<table") == body.count('role="presentation"') > 3
    assert "max-width:600px" in body and 'name="color-scheme"' in body and "prefers-color-scheme: dark" in body
    assert "<script" not in body and "<link" not in body
    # The hidden preheader gives the inbox preview the names (the subject has the counts).
    assert re.search(r'<div style="display:none[^"]*">Scientific Controversies: Octopus Umwelt, Read the News', body)


@pytest.mark.parametrize("start,end,all_day,expected", [
    ("2026-10-12T08:15:00-04:00", "2026-10-12T08:45:00-04:00", 0, "8:15 – 8:45 AM"),
    ("2026-10-18T11:00:00-04:00", "2026-10-18T14:00:00-04:00", 0, "11 AM – 2 PM"),
    ("2026-10-07T01:30:00+00:00", "2026-10-07T02:30:00+00:00", 0, "9:30 – 10:30 PM"),
    ("2026-10-12", "2026-10-13", 1, "All day"),
])
def test_time_range(start, end, all_day, expected):
    assert notifier._time_range({"start": start, "end": end, "all_day": all_day}) == expected


def test_reason_and_title_cleanup():
    assert notifier._why("Great talk for the Learning goal. Fit: Tuesday, 35 min away.") == (
        "Great talk for the Learning goal.")
    long = notifier._why("word " * 60)
    assert long.endswith("…") and len(long) <= 161
    assert notifier._clean("How Art and Sci&shy;ence &amp; Ecologies") == "How Art and Science & Ecologies"


def test_subject_counts_options_and_plan_items():
    def subject(rows):
        return notifier.subject_line(notifier.build_digest(rows))

    start = _at(12, 9)
    assert subject([_block("a", start, 60, "event"), _block("b", start, 60, "event")]) == "2 event options to review"
    assert subject([_block(x, start, 30) for x in "abc"]) == "3 plan blocks to review"  # all one-offs
    assert subject(live_shaped()) == "1 event option · 4 plan items (18 blocks) to review"
    assert subject([]) == "Nothing to review"


@pytest.mark.parametrize("grouper,warns", [
    (mock.Mock(side_effect=RuntimeError("boom")), True),  # grouper broken: list the plan by day
    (mock.Mock(return_value=[]), False),                   # grouper lost rows: they are still listed
])
def test_plan_still_listed_when_grouping_fails(config, monkeypatch, caplog, grouper, warns):
    monkeypatch.setattr(review_model, "group_series", grouper)
    msg = notifier.build_message(live_shaped(), config=config, intro="hi", now=TUESDAY)
    text, _ = _parts(msg)
    assert "By day:" in text and "Repeating:" not in text
    assert text.count("Read the News") == 8 and text.count("Travel to Rockclimb") == 4
    assert msg["Subject"] == "1 event option · 18 plan blocks to review"
    assert ("grouping failed" in caplog.text) is warns


def test_calendar_link_only_on_mondays(config, monkeypatch):
    monkeypatch.setenv("NOTIFY_CALENDAR_URL", "http://host:8000/")
    cfg = notifier.load_config()
    rows = [_p("a", "2026-10-13T08:00:00-04:00", "2026-10-13T09:00:00-04:00")]
    monday = dt.datetime(2026, 10, 12, 7, 0, tzinfo=NY)
    tuesday = dt.datetime(2026, 10, 13, 7, 0, tzinfo=NY)

    text, body = _parts(notifier.build_message(rows, config=cfg, intro="hi", now=monday))
    assert "Your calendar: http://host:8000/" in text and f"Review plan: {APP}/#/plan" in text
    assert "Open calendar" in body and "Review plan" in body and "Calendar · Mon, Oct 12" in body

    text, body = _parts(notifier.build_message(rows, config=cfg, intro="hi", now=tuesday))
    assert "Your calendar" not in text and "Open calendar" not in body
    assert "Review plan" in body


def test_links_default_to_the_app_url_and_review_url_still_overrides(monkeypatch):
    monkeypatch.setenv("VITE_API_URL", "http://pi:8000/")
    cfg = notifier.load_config()
    assert (cfg["events_url"], cfg["plan_url"], cfg["preview_url"], cfg["calendar_url"]) == (
        "http://pi:8000/#/events", "http://pi:8000/#/plan", "http://pi:8000/#/calendar?preview=1", "http://pi:8000/")
    assert cfg["pushover"] is False
    monkeypatch.setenv("NOTIFY_APP_URL", "https://cal.example")
    monkeypatch.setenv("NOTIFY_REVIEW_URL", "https://old.example/review")
    cfg = notifier.load_config()
    assert cfg["events_url"] == "https://cal.example/#/events" and cfg["plan_url"] == "https://old.example/review"


# --- sending -----------------------------------------------------------------------


def test_config_problem(config, monkeypatch):
    assert notifier.config_problem(config) is None
    monkeypatch.setenv("NOTIFY_SMTP_PASSWORD", "")
    assert "PASSWORD" in notifier.config_problem(notifier.load_config())
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "")
    assert "NOTIFY_EMAIL_TO" in notifier.config_problem(notifier.load_config())


def test_digest_sends_via_starttls(config):
    rows = [_p("a", "2099-01-01T08:00:00-05:00", "2099-01-01T09:00:00-05:00")]
    with mock.patch.object(notifier.db, "get_proposals", return_value=rows), \
         mock.patch.object(notifier.smtplib, "SMTP") as smtp_cls:
        result = notifier.send_pending_digest()
    smtp = smtp_cls.return_value.__enter__.return_value
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("bot@example.com", "pw")
    smtp.send_message.assert_called_once()
    assert result["sent"] and result["count"] == 1 and result["subject"] == "1 plan block to review"


def test_digest_dry_run_and_unconfigured_never_connect(config, monkeypatch):
    rows = [_p("a", "2099-01-01T08:00:00-05:00", "2099-01-01T09:00:00-05:00")]
    with mock.patch.object(notifier.db, "get_proposals", return_value=rows), \
         mock.patch.object(notifier.smtplib, "SMTP") as smtp_cls:
        assert "Item a" in notifier.send_pending_digest(dry_run=True)["preview"]
        monkeypatch.setenv("NOTIFY_SMTP_PASSWORD", "")
        result = notifier.send_pending_digest()
    smtp_cls.assert_not_called()
    assert not result["sent"] and "PASSWORD" in result["skipped"]


def test_digest_smtp_failure_raises_notify_error(config):
    rows = [_p("a", "2099-01-01T08:00:00-05:00", "2099-01-01T09:00:00-05:00")]
    with mock.patch.object(notifier.db, "get_proposals", return_value=rows), \
         mock.patch.object(notifier.smtplib, "SMTP", side_effect=OSError("no route")):
        with pytest.raises(notifier.NotifyError):
            notifier.send_pending_digest()


def test_notify_run_emails_only_that_runs_proposals(config):
    new = [_p("n", "2099-01-01T18:00:00-05:00", "2099-01-01T19:00:00-05:00", "event")]
    everything = new + [_p("o", "2099-01-02T08:00:00-05:00", "2099-01-02T09:00:00-05:00")]

    def fake(run_id=None, status=None):
        return new if run_id == "r1" else everything

    with mock.patch.object(notifier.db, "get_proposals", side_effect=fake), \
         mock.patch.object(notifier.smtplib, "SMTP") as smtp_cls:
        notifier.notify_run({"run_id": "r1", "proposed": 1}, "scout")
    msg = smtp_cls.return_value.__enter__.return_value.send_message.call_args.args[0]
    assert msg["Subject"] == "1 event option to review"
    text = msg.get_body(("plain",)).get_content()
    assert text.startswith("The scout found 1 new event option (2 pending in total).")
    assert "Item n" in text and "Item o" not in text
    # An options-only email: its review link, then the preview, and no plan link.
    assert f"Review events: {APP}/#/events\nPreview on calendar: {APP}/#/calendar?preview=1\n" in text
    assert "Review plan" not in text


def test_notify_run_never_emails_queued_options(config):
    def option(title, status, hour):
        start = dt.datetime(2099, 1, 5, hour, tzinfo=NY)
        return {"item_type": "event", "item_id": title, "title": title, "description": "", "location": "",
                "start": start.isoformat(), "end": (start + dt.timedelta(hours=1)).isoformat(), "all_day": 0,
                "timezone": "America/New_York", "rationale": "", "status": status}

    notifier.db.insert_proposals("s1", [option("Shown option", "proposed", 18), option("Reserve option", "queued", 19)],
                                 created_at="2026-10-08T00:00:00+00:00")
    with mock.patch.object(notifier.smtplib, "SMTP") as smtp_cls:
        notifier.notify_run({"run_id": "s1"}, "scout")
    msg = smtp_cls.return_value.__enter__.return_value.send_message.call_args.args[0]
    text, body = _parts(msg)
    assert "Shown option" in text and "Reserve option" not in text + body
    assert msg["Subject"] == "1 event option to review"


def test_notify_run_skips_dry_runs_and_swallows_errors(config):
    with mock.patch.object(notifier.db, "get_proposals") as get:
        notifier.notify_run({"run_id": None, "dry_run": True}, "optimizer")
        get.assert_not_called()
    rows = [_p("a", "2099-01-01T08:00:00-05:00", "2099-01-01T09:00:00-05:00")]
    with mock.patch.object(notifier.db, "get_proposals", return_value=rows), \
         mock.patch.object(notifier.smtplib, "SMTP", side_effect=OSError("down")):
        notifier.notify_run({"run_id": "r1"}, "optimizer")  # must not raise
    with mock.patch.object(notifier.db, "get_proposals", side_effect=RuntimeError("db locked")):
        notifier.notify_run({"run_id": "r1"}, "optimizer")  # must not raise either


# --- Pushover (opt-in) ------------------------------------------------------------------


@pytest.fixture
def pushover(config, monkeypatch):
    monkeypatch.setenv("NOTIFY_PUSHOVER", "1")
    monkeypatch.setenv("PUSHOVER_USER", "u-key")
    monkeypatch.setenv("PUSHOVER_TOKEN", "a-token")
    reply = mock.MagicMock()
    reply.__enter__.return_value.read.return_value = b'{"status":1,"request":"abc"}'
    with mock.patch.object(notifier.urllib.request, "urlopen", return_value=reply) as urlopen:
        yield urlopen


def _run_rows(item_type):
    rows = [_p("n", "2099-01-05T18:00:00-05:00", "2099-01-05T19:00:00-05:00", item_type, title="Octopus talk")]
    return mock.patch.object(notifier.db, "get_proposals", return_value=rows)


def _form(urlopen):
    request = urlopen.call_args.args[0]
    assert request.full_url == notifier.PUSHOVER_API_URL and request.get_method() == "POST"
    assert urlopen.call_args.kwargs["timeout"] == 15
    return {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode()).items()}


def test_pushover_is_off_by_default(config):
    with _run_rows("event"), mock.patch.object(notifier.smtplib, "SMTP") as smtp_cls:
        notifier.notify_run({"run_id": "r1"}, "scout")
    smtp_cls.assert_called_once()  # the email went; the autouse guard proves nothing reached urlopen


def test_pushover_sends_one_post_with_the_deep_link(pushover):
    with _run_rows("event"), mock.patch.object(notifier.smtplib, "SMTP"):
        notifier.notify_run({"run_id": "r1"}, "scout")
    pushover.assert_called_once()
    form = _form(pushover)
    assert form["token"] == "a-token" and form["user"] == "u-key"
    assert form["url"] == f"{APP}/#/events" and form["url_title"] == "Review events"
    assert form["title"] == "1 event option to review" and "Octopus talk" in form["message"]


def test_pushover_links_the_plan_after_an_optimizer_run(pushover):
    with _run_rows("goal"), mock.patch.object(notifier.smtplib, "SMTP"):
        notifier.notify_run({"run_id": "r1"}, "optimizer")
    form = _form(pushover)
    assert form["url"] == f"{APP}/#/plan" and form["url_title"] == "Review plan"


def test_push_survives_email_failure_and_its_own_failures_never_raise(pushover, caplog):
    with _run_rows("event"), mock.patch.object(notifier.smtplib, "SMTP", side_effect=OSError("down")):
        notifier.notify_run({"run_id": "r1"}, "scout")
    pushover.assert_called_once()  # the email failed, the push still went

    cfg = notifier.load_config()
    pushover.side_effect = urllib.error.URLError("offline")
    with _run_rows("event"), mock.patch.object(notifier.smtplib, "SMTP"):
        notifier.notify_run({"run_id": "r1"}, "scout")  # must not raise
    assert notifier.send_push(cfg, title="t", message="m", url="u", url_title="x") is False
    pushover.side_effect = urllib.error.HTTPError(
        notifier.PUSHOVER_API_URL, 400, "Bad Request", {}, io.BytesIO(b'{"status":0,"errors":["user key is invalid"]}'))
    assert notifier.send_push(cfg, title="t", message="m", url="u", url_title="x") is False
    assert "user key is invalid" in caplog.text and "a-token" not in caplog.text
    pushover.side_effect = None
    for reply in (b'{"status":0,"errors":["bad"]}', b"[]", b"<html>captive portal</html>"):
        pushover.return_value.__enter__.return_value.read.return_value = reply
        assert notifier.send_push(cfg, title="t", message="m", url="u", url_title="x") is False


def test_pushover_needs_both_keys_and_only_runs_push(config, monkeypatch, caplog):
    monkeypatch.setenv("NOTIFY_PUSHOVER", "1")
    monkeypatch.setenv("PUSHOVER_USER", "u-key")  # no token: logged, nothing sent
    with _run_rows("event"), mock.patch.object(notifier.smtplib, "SMTP"):
        notifier.notify_run({"run_id": "r1"}, "scout")
    assert "PUSHOVER_TOKEN" in caplog.text
    monkeypatch.setenv("PUSHOVER_TOKEN", "a-token")
    with _run_rows("event"):
        notifier.send_pending_digest(dry_run=True)  # the on-demand digest only emails
        notifier.notify_run({"run_id": "r1", "dry_run": True}, "scout")
    # The autouse guard fails the test if any of this reached urlopen.


def test_push_text_fits_pushover_limit():
    rows = [_block(f"e{n}", _at(12, 18), 60, "event", item_id=f"d{n}", title="A long event title " * 4)
            for n in range(30)]
    text = notifier.push_text(notifier.build_digest(rows))
    assert len(text) <= 1024 and text.endswith("…")


# --- db helper the digest relies on -------------------------------------------------------


def test_expire_past_proposals_only_touches_undecided_rows_that_are_over():
    db = notifier.db

    def row(start, end, status="proposed", all_day=0):
        return {"item_type": "goal", "item_id": "g", "title": f"{start}-{status}", "description": "",
                "location": "", "start": start, "end": end, "all_day": all_day,
                "timezone": "America/New_York", "rationale": "", "status": status}

    ids = db.insert_proposals("r1", [
        row("2026-10-04T08:00:00-04:00", "2026-10-04T09:00:00-04:00"),             # yesterday -> expired
        row("2026-10-05T07:00:00-04:00", "2026-10-05T08:30:00-04:00"),             # ended this morning -> expired
        row("2026-10-05T08:30:00-04:00", "2026-10-05T09:30:00-04:00"),             # still running -> kept
        row("2026-10-06T08:00:00-04:00", "2026-10-06T09:00:00-04:00"),             # tomorrow -> kept
        row("2026-10-05", "2026-10-06", all_day=1),                                # all-day today -> kept
        row("2026-10-03", "2026-10-04", all_day=1),                                # all-day, over -> expired
        row("2026-10-04T08:00:00-04:00", "2026-10-04T09:00:00-04:00", "invalid"),  # not undecided -> kept
    ], created_at="2026-10-01T00:00:00+00:00")
    assert db.expire_past_proposals(NOW) == 3
    status = {p["id"]: p["status"] for p in db.get_proposals(run_id="r1")}
    assert [status[i] for i in ids] == [
        "expired", "expired", "proposed", "proposed", "proposed", "expired", "invalid"]
    assert db.count_decisions() == 0  # expiring is not a decision the learner should see
    assert db.expire_past_proposals(NOW) == 0  # idempotent
