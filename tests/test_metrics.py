from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Comment, Ticket, TicketPriority, TicketStatus
from app.services import metrics
from app.web.deps import ago, timeago
from app.web.queries import PRIORITY_RANK, bar_chart, hbar_rows

NOW = datetime(2026, 11, 8, 12, 0, 0)  # naive UTC, the shape SQLite returns


def mk(db, customer, created, **kw):
    t = Ticket(subject="s", description="d", customer_id=customer.id, created_at=created, **kw)
    db.add(t)
    db.commit()
    return t


def reply(db, ticket, author, when, internal=False):
    db.add(Comment(ticket_id=ticket.id, author_id=author.id, body="b", is_internal=internal, created_at=when))
    db.commit()


# ---------- time helpers
def test_naive_utc_normalises_both_shapes():
    aware = datetime(2026, 11, 8, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert metrics.naive_utc(aware) == datetime(2026, 11, 8, 12, 0)
    assert metrics.naive_utc(NOW) == NOW and metrics.naive_utc(None) is None


@pytest.mark.parametrize("hours, text", [(None, "no data yet"), (0.5, "30 min"), (0.0, "0 min"), (3.0, "3 h"), (3.55, "3.5 h"), (47.9, "47.9 h"), (48, "2.0 days"), (100, "4.2 days")])
def test_format_hours(hours, text):
    assert metrics.format_hours(hours) == text


def test_relative_time_filters(monkeypatch):
    monkeypatch.setattr(metrics, "now_utc", lambda: NOW)
    cases = [(NOW - timedelta(seconds=10), "just now", "just now"), (NOW - timedelta(minutes=5), "5 min", "5 min ago"),
             (NOW - timedelta(hours=3), "3 h", "3 h ago"), (NOW - timedelta(days=2), "2 d", "2 d ago"),
             (NOW + timedelta(minutes=3), "just now", "just now")]  # a clock a little ahead is not "-3 min"
    for when, short, long in cases:
        assert (timeago(when), ago(when)) == (short, long)
    assert (timeago(None), ago(None)) == ("never", "never")
    ist = timezone(timedelta(hours=5, minutes=30))  # an aware timestamp is converted to UTC before comparing
    assert ago(datetime(2026, 11, 8, 17, 25, tzinfo=ist)) == "5 min ago"  # 17:25 IST is 11:55 UTC, five minutes before NOW


# ---------- counts and breakdowns
def test_ticket_counts_and_attention_only_counts_active(db_session, users):
    alice = users["alice"]
    mk(db_session, alice, NOW, status=TicketStatus.open, needs_attention=True)
    mk(db_session, alice, NOW, status=TicketStatus.pending, needs_attention=True)
    mk(db_session, alice, NOW, status=TicketStatus.resolved, needs_attention=True)  # flagged but finished: not counted
    mk(db_session, alice, NOW, status=TicketStatus.closed)
    assert metrics.ticket_counts(db_session) == {"open": 1, "pending": 1, "resolved": 1, "closed": 1, "active": 2, "attention": 2}


def test_active_breakdowns_prefer_the_humans_category(db_session, users):
    alice = users["alice"]
    mk(db_session, alice, NOW, category="billing", ai_category="shipping")  # the agent's choice wins
    mk(db_session, alice, NOW, ai_category="shipping")
    mk(db_session, alice, NOW)
    mk(db_session, alice, NOW, category="billing", status=TicketStatus.closed)  # not active: ignored
    b = metrics.active_breakdowns(db_session)
    assert b["category"] == [("billing", 1), ("shipping", 1), ("uncategorised", 1)]
    assert b["priority"] == [("urgent", 0), ("high", 0), ("medium", 3), ("low", 0)]  # always all four, in urgency order


# ---------- volume
def test_volume_by_day_fills_empty_days_in_order(db_session, users):
    for days_ago, n in [(0, 2), (1, 1), (3, 4), (20, 9)]:  # the 20-day-old tickets fall outside the window
        for _ in range(n):
            mk(db_session, users["alice"], NOW - timedelta(days=days_ago, hours=1))
    vol = metrics.volume_by_day(db_session, days=5, today=NOW)
    assert [d["date"].day for d in vol] == [4, 5, 6, 7, 8]
    assert [d["count"] for d in vol] == [0, 4, 0, 1, 2]


def test_volume_counts_by_utc_day_not_local_day(db_session, users):
    mk(db_session, users["alice"], datetime(2026, 11, 7, 23, 59))
    mk(db_session, users["alice"], datetime(2026, 11, 8, 0, 1))
    assert [d["count"] for d in metrics.volume_by_day(db_session, days=2, today=NOW)] == [1, 1]


# ---------- response times
def test_median_first_reply_ignores_notes_customers_and_old_tickets(db_session, users):
    alice, agent = users["alice"], users["agent"]
    a = mk(db_session, alice, NOW - timedelta(hours=10))
    reply(db_session, a, agent, NOW - timedelta(hours=9))          # first public staff reply: 1 h
    reply(db_session, a, agent, NOW - timedelta(hours=2))          # later replies do not count
    b = mk(db_session, alice, NOW - timedelta(hours=10))
    reply(db_session, b, agent, NOW - timedelta(hours=9, minutes=30), internal=True)  # an internal note is not a reply
    reply(db_session, b, alice, NOW - timedelta(hours=9, minutes=45))                 # neither is the customer
    reply(db_session, b, agent, NOW - timedelta(hours=7))          # real reply after 3 h
    c = mk(db_session, alice, NOW - timedelta(hours=10))
    reply(db_session, c, agent, NOW - timedelta(hours=5))          # 5 h
    old = mk(db_session, alice, NOW - timedelta(days=60))
    reply(db_session, old, agent, NOW - timedelta(days=60) + timedelta(hours=100))  # outside the 30-day window
    mk(db_session, alice, NOW - timedelta(hours=1))                # never answered: not counted, not a zero
    r = metrics.response_times(db_session, now=NOW)
    assert r["first_response_n"] == 3 and r["first_response_hours"] == 3.0  # median of 1, 3, 5


def test_median_resolution_time(db_session, users):
    for hours in (2, 4, 100):
        t = mk(db_session, users["alice"], NOW - timedelta(days=5), status=TicketStatus.resolved)
        t.resolved_at = t.created_at + timedelta(hours=hours)
    mk(db_session, users["alice"], NOW - timedelta(days=5))  # still open: excluded
    r = metrics.response_times(db_session, now=NOW)
    assert r["resolution_n"] == 3 and r["resolution_hours"] == 4.0  # the median shrugs off the 100 h outlier


def test_no_data_gives_none_not_zero(db_session):
    r = metrics.response_times(db_session, now=NOW)
    assert r["first_response_hours"] is None and r["resolution_hours"] is None and r["first_response_n"] == 0


def test_impossible_negative_durations_are_discarded(db_session, users):
    t = mk(db_session, users["alice"], NOW - timedelta(hours=5), status=TicketStatus.resolved)
    t.resolved_at = NOW - timedelta(hours=9)  # resolved before it was created (bad data)
    db_session.commit()
    assert metrics.response_times(db_session, now=NOW)["resolution_hours"] is None


# ---------- charts
def test_bar_chart_geometry_stays_inside_the_plot():
    chart = bar_chart([{"label": str(i), "title": "t", "count": c} for i, c in enumerate([0, 3, 8, 1])])
    assert chart["peak"] == 8 and len(chart["bars"]) == 4
    for b in chart["bars"]:
        assert chart["left"] <= b["x"] and b["x"] + b["w"] <= chart["right"]
        assert b["y"] + b["h"] == pytest.approx(chart["base"], abs=0.2)  # every bar sits on the axis
    heights = [b["h"] for b in chart["bars"]]
    assert heights[0] == 0 and heights[2] == max(heights) and heights[1] == pytest.approx(heights[2] * 3 / 8, rel=0.02)
    assert [b["last"] for b in chart["bars"]] == [False, False, False, True]


def test_bar_chart_with_all_zeros_or_no_points_does_not_divide_by_zero():
    flat = bar_chart([{"label": "1", "title": "t", "count": 0}] * 3)
    assert flat["peak"] == 4 and all(b["h"] == 0 for b in flat["bars"])
    assert bar_chart([])["bars"] == []


def test_hbar_rows_scale_to_the_largest():
    rows = hbar_rows([("billing", 5), ("shipping", 2), ("none", 0)])
    assert [r["pct"] for r in rows] == [100.0, 40.0, 0.0]
    assert hbar_rows([("x", 0)])[0]["pct"] == 0.0 and hbar_rows([]) == []


def test_priority_rank_sorts_urgent_first(db_session, users):
    for p in (TicketPriority.low, TicketPriority.urgent, TicketPriority.medium, TicketPriority.high):
        mk(db_session, users["alice"], NOW, priority=p)
    ordered = db_session.scalars(select(Ticket).order_by(PRIORITY_RANK.desc())).all()
    assert [t.priority.value for t in ordered] == ["urgent", "high", "medium", "low"]
