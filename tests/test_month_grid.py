"""Month-grid bar packing (google_calendar.get_month_grid) via a mocked events feed.

August 2026 starts on a Saturday, so the Monday-start grid runs from Mon 27 Jul:
week 1 = 27 Jul-2 Aug, week 2 = 3-9 Aug, week 3 = 10-16 Aug, ...
"""

from datetime import date

import pytest

from app import google_calendar

EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"


def all_day(title, first, last_exclusive):
    return {"summary": title, "start": {"date": first}, "end": {"date": last_exclusive}}


def timed(title, day, hour):
    return {
        "summary": title,
        "start": {"dateTime": f"{day}T{hour:02d}:00:00+01:00"},
        "end": {"dateTime": f"{day}T{hour + 1:02d}:00:00+01:00"},
    }


@pytest.fixture
def events(google, connected):
    def serve(*items):
        google.get(url__regex=EVENTS_URL_PATTERN).respond(200, json={"items": list(items)})

    return serve


def _bars(week):
    return {b["event"]["title"]: (b["col_start"], b["col_end"], b["slot"]) for b in week["bars"]}


def _hidden(week):
    return [d["hidden_count"] for d in week["days"]]


async def test_bar_crossing_a_week_boundary_is_clipped_per_week(db, events):
    # Fri 7 - Tue 11 Aug inclusive (Google's all-day end is exclusive).
    events(all_day("Camping", "2026-08-07", "2026-08-12"))

    grid = await google_calendar.get_month_grid(db, 2026, 8)
    weeks = grid["weeks"]

    assert _bars(weeks[1]) == {"Camping": (5, 7, 0)}  # Fri..Sun
    assert _bars(weeks[2]) == {"Camping": (1, 2, 0)}  # Mon..Tue
    assert all(not w["bars"] for i, w in enumerate(weeks) if i not in (1, 2))


async def test_bar_spanning_a_whole_week_fills_every_column(db, events):
    # Sun 2 - Mon 17 Aug: starts in week 1, covers weeks 2 and 3, ends in week 4.
    events(all_day("Summer camp", "2026-08-02", "2026-08-18"))

    weeks = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"]

    assert [_bars(w).get("Summer camp") for w in weeks[:5]] == [
        (7, 7, 0),
        (1, 7, 0),
        (1, 7, 0),
        (1, 1, 0),
        None,
    ]


async def test_slot_is_reused_once_a_bar_ends(db, events):
    events(
        all_day("Grandma visits", "2026-08-10", "2026-08-12"),  # Mon-Tue
        timed("Dentist", "2026-08-11", 9),  # Tue: overlaps Grandma
        timed("Swimming", "2026-08-12", 16),  # Wed: Grandma has ended
    )

    week = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"][2]

    assert _bars(week) == {
        "Grandma visits": (1, 2, 0),
        "Dentist": (2, 2, 1),
        "Swimming": (3, 3, 0),  # back in the top row, not a third one
    }
    assert week["row_count"] == 2
    assert _hidden(week) == [0] * 7


async def test_overflow_beyond_the_visible_slots_becomes_plus_n_more(db, events, client):
    events(*[timed(f"Event {h}", "2026-08-12", h) for h in range(8, 13)])  # five on Wed

    week = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"][2]

    assert len(week["bars"]) == google_calendar.MAX_BAR_SLOTS
    assert sorted(_bars(week)) == ["Event 10", "Event 8", "Event 9"]  # earliest win the slots
    assert week["row_count"] == google_calendar.MAX_BAR_SLOTS
    assert _hidden(week) == [0, 0, 2, 0, 0, 0, 0]

    html = (await client.get("/widgets/calendar?year=2026&month=8")).text
    assert "+2 more" in html
    assert 'hx-get="/widgets/calendar/day/2026-08-12"' in html


async def test_overflowing_multi_day_event_counts_on_each_of_its_days_in_the_week(db, events):
    events(
        *[all_day(f"Busy {i}", "2026-08-10", "2026-08-13") for i in range(3)],  # Mon-Wed, fill all slots
        all_day("Trip", "2026-08-12", "2026-08-18"),  # Wed 12 - Mon 17: overflows in week 3 only
    )

    weeks = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"]

    assert "Trip" not in _bars(weeks[2])
    assert _hidden(weeks[2]) == [0, 0, 1, 1, 1, 1, 1]  # Wed..Sun, clipped to the week
    assert _bars(weeks[3])["Trip"] == (1, 1, 0)  # next week has room again
    assert _hidden(weeks[3]) == [0] * 7


async def test_longer_event_claims_a_slot_before_same_start_short_ones(db, events):
    events(
        *[all_day(f"Short {i}", "2026-08-10", "2026-08-11") for i in range(3)],
        all_day("Long", "2026-08-10", "2026-08-15"),  # Mon-Fri, same start
    )

    week = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"][2]

    assert _bars(week)["Long"] == (1, 5, 0)
    assert _hidden(week)[0] == 1  # one of the one-day events is the "+1 more"


async def test_all_day_event_outranks_timed_events_on_the_same_day(db, events):
    # The feed lists the timed events first; the all-day one still gets the top row.
    events(
        timed("Breakfast club", "2026-08-12", 7),
        timed("Piano", "2026-08-12", 15),
        timed("Football", "2026-08-12", 17),
        all_day("Inset day", "2026-08-12", "2026-08-13"),
    )

    week = (await google_calendar.get_month_grid(db, 2026, 8))["weeks"][2]
    day = await google_calendar.get_day_events(db, date(2026, 8, 12))

    assert _bars(week)["Inset day"][2] == 0
    assert "Football" not in _bars(week)  # the latest timed event is the one that overflows
    assert [e["title"] for e in day["events"]] == ["Inset day", "Breakfast club", "Piano", "Football"]
