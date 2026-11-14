"""Numbers for the dashboard overview.

Everything is computed on request with plain queries: fine for a college-scale database, and it
keeps the numbers honest (no cached counters that can drift). Timestamps are normalised to naive
UTC first, because SQLite hands back naive datetimes while other databases return aware ones.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ChatSession,
    ChatStatus,
    Comment,
    Ticket,
    TicketPriority,
    TicketStatus,
    User,
    UserRole,
)

ACTIVE = (TicketStatus.open, TicketStatus.pending)
PRIORITY_ORDER = [TicketPriority.urgent, TicketPriority.high, TicketPriority.medium, TicketPriority.low]


def naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _median_or_none(values: list[float]) -> float | None:
    return round(median(values), 2) if values else None


def ticket_counts(db: Session) -> dict[str, int]:
    """Tickets per status plus the flagged count among active tickets."""
    by_status = dict(db.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all())
    counts = {s.value: by_status.get(s, 0) for s in TicketStatus}
    counts["active"] = counts["open"] + counts["pending"]
    counts["attention"] = db.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.needs_attention.is_(True), Ticket.status.in_(ACTIVE))
    ) or 0
    return counts


def waiting_chat_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.status == ChatStatus.waiting)) or 0


def volume_by_day(db: Session, days: int = 14, today: datetime | None = None) -> list[dict]:
    """Tickets created per day for the last `days` days (oldest first, zeros included)."""
    today = (today or now_utc()).date()
    start = today - timedelta(days=days - 1)
    created = [naive_utc(c).date() for c in db.scalars(select(Ticket.created_at)) if c is not None]
    per_day = Counter(d for d in created if start <= d <= today)
    return [{"date": start + timedelta(days=i), "count": per_day.get(start + timedelta(days=i), 0)} for i in range(days)]


def response_times(db: Session, window_days: int = 30, now: datetime | None = None) -> dict:
    """Median hours to first staff reply, and to resolution, for tickets created in the window."""
    now = now or now_utc()
    since = now - timedelta(days=window_days)
    tickets = [t for t in db.scalars(select(Ticket)) if naive_utc(t.created_at) >= since]
    ids = {t.id for t in tickets}

    first_reply: dict[int, datetime] = {}
    staff_comments = (
        select(Comment.ticket_id, func.min(Comment.created_at))
        .join(User, User.id == Comment.author_id)
        .where(Comment.is_internal.is_(False), User.role.in_([UserRole.agent, UserRole.admin]))
        .group_by(Comment.ticket_id)
    )
    for ticket_id, first in db.execute(staff_comments):
        if ticket_id in ids:
            first_reply[ticket_id] = naive_utc(first)

    first_hours = [_hours(first_reply[t.id] - naive_utc(t.created_at)) for t in tickets if t.id in first_reply]
    resolve_hours = [
        _hours(naive_utc(t.resolved_at) - naive_utc(t.created_at)) for t in tickets if t.resolved_at is not None
    ]
    return {
        "window_days": window_days,
        "first_response_hours": _median_or_none([h for h in first_hours if h >= 0]),
        "first_response_n": len(first_hours),
        "resolution_hours": _median_or_none([h for h in resolve_hours if h >= 0]),
        "resolution_n": len(resolve_hours),
    }


def active_breakdowns(db: Session) -> dict[str, list[tuple[str, int]]]:
    """Active tickets grouped by category and by priority."""
    active = list(db.scalars(select(Ticket).where(Ticket.status.in_(ACTIVE))))
    by_category = Counter((t.category or t.ai_category or "uncategorised") for t in active)
    by_priority = Counter(t.priority for t in active)
    return {
        "category": sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0])),
        "priority": [(p.value, by_priority.get(p, 0)) for p in PRIORITY_ORDER],
    }


def format_hours(hours: float | None) -> str:
    """Human wording for a duration: '45 min', '3.5 h', '2.1 days'. None -> 'no data yet'."""
    if hours is None:
        return "no data yet"
    if hours < 1:
        return f"{round(hours * 60)} min"
    if hours < 48:
        return f"{hours:.1f} h".replace(".0 h", " h")
    return f"{hours / 24:.1f} days"
