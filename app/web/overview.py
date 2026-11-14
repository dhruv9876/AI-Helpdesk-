from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api import ticket_ai
from app.core.deps import DB
from app.models import ChatStatus, Ticket
from app.services import chat as chat_service
from app.services import metrics
from app.web.deps import WebStaff, csrf_guard, page
from app.web.queries import PRIORITY_RANK, bar_chart, hbar_rows

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_guard)])


@router.get("/dashboard")
def overview(request: Request, user: WebStaff, db: DB):
    counts = metrics.ticket_counts(db)
    flagged = db.scalars(
        select(Ticket)
        .where(Ticket.needs_attention.is_(True), Ticket.status.in_(metrics.ACTIVE))
        .order_by(PRIORITY_RANK.desc(), Ticket.created_at)
        .limit(6)
    ).all()
    waiting = chat_service.list_chats(db, user.id, ChatStatus.waiting, limit=5)
    ai = ticket_ai.ai_stats(user, db)
    times = metrics.response_times(db)
    breakdown = metrics.active_breakdowns(db)
    days = metrics.volume_by_day(db)
    chart = bar_chart([
        {"label": str(d["date"].day), "count": d["count"],
         "title": f"{d['date'].strftime('%d %b')}: {d['count']} ticket{'s' if d['count'] != 1 else ''}"}
        for d in days
    ])
    decided = ai.replies_accepted + ai.replies_edited + ai.replies_rejected
    return page(request, "overview.html", {
        "counts": counts, "flagged": flagged, "waiting": waiting, "ai": ai, "times": times, "decided": decided,
        "chart": chart, "days_total": sum(d["count"] for d in days),
        "by_category": hbar_rows(breakdown["category"]), "by_priority": hbar_rows(breakdown["priority"]),
        "bot_finished": (ai.chats_total - ai.chats_handed_off) if ai.chats_total else 0,
        "today": datetime.now(timezone.utc),
    }, user, db)
