"""Ticket queue and ticket detail pages.

Each action calls the same function the JSON API uses (api/tickets.py, api/ticket_ai.py), so
permission rules and validation exist in exactly one place. These handlers only translate a
form post into those calls and render the part of the page that changed.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api import ticket_ai, tickets as tickets_api
from app.api.helpers import get_ticket_or_404
from app.core.deps import DB
from app.models import (
    Comment,
    ReplyStatus,
    SuggestedReply,
    Ticket,
    TicketPriority,
    TicketStatus,
    User,
    UserRole,
)
from app.schemas import AcceptReplyIn, CommentCreate, MergeIn, RejectReplyIn, TicketUpdate
from app.services import duplicates, metrics
from app.services.llm import get_llm
from app.services.triage import CATEGORIES, get_triager
from app.web.deps import WebStaff, csrf_guard, is_htmx, page, render, toast, toast_header, redirect
from app.web.queries import PRIORITY_RANK

router = APIRouter(prefix="/dashboard", include_in_schema=False, dependencies=[Depends(csrf_guard)])

PAGE_SIZE = 15
TABS = [("active", "Active"), ("open", "Open"), ("pending", "Pending"), ("resolved", "Resolved"), ("closed", "Closed"), ("all", "All")]


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return f"{'.'.join(str(x) for x in first['loc']) or 'This field'}: {first['msg']}"
    return str(exc)


# =======================================================================================
# Queue
# =======================================================================================
@router.get("/tickets")
def queue(
    request: Request, user: WebStaff, db: DB,
    status_: str = Query("active", alias="status"),
    priority: str = "", category: str = "", assignee: str = "", attention: str = "",
    q: str = "", sort: str = "queue", page_no: int = Query(1, alias="page", ge=1),
):
    stmt = select(Ticket).options(selectinload(Ticket.customer), selectinload(Ticket.assignee))
    if status_ == "active":
        stmt = stmt.where(Ticket.status.in_(metrics.ACTIVE))
    elif status_ in {s.value for s in TicketStatus}:
        stmt = stmt.where(Ticket.status == TicketStatus(status_))
    else:
        status_ = "all"
    if priority in {p.value for p in TicketPriority}:
        stmt = stmt.where(Ticket.priority == TicketPriority(priority))
    else:
        priority = ""
    effective_category = func.coalesce(Ticket.category, Ticket.ai_category)
    if category == "none":
        stmt = stmt.where(effective_category.is_(None))
    elif category:
        stmt = stmt.where(effective_category == category)
    if assignee == "me":
        stmt = stmt.where(Ticket.assignee_id == user.id)
    elif assignee == "none":
        stmt = stmt.where(Ticket.assignee_id.is_(None))
    elif assignee.isdigit():
        stmt = stmt.where(Ticket.assignee_id == int(assignee))
    else:
        assignee = ""
    if attention:
        stmt = stmt.where(Ticket.needs_attention.is_(True))
    q = q.strip()
    if q:
        like = f"%{q}%"
        conditions = [Ticket.subject.ilike(like), Ticket.description.ilike(like)]
        if q.lstrip("#").isdigit():
            conditions.append(Ticket.id == int(q.lstrip("#")))
        stmt = stmt.where(or_(*conditions))

    if sort == "newest":
        order = [Ticket.created_at.desc()]
    elif sort == "oldest":
        order = [Ticket.created_at.asc()]
    else:
        sort = "queue"  # flagged first, then by priority, then the longest-waiting
        order = [Ticket.needs_attention.desc(), PRIORITY_RANK.desc(), Ticket.created_at.asc()]
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page_no = min(page_no, pages)
    rows = db.scalars(stmt.order_by(*order, Ticket.id).offset((page_no - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()

    params = {"status": status_, "priority": priority, "category": category, "assignee": assignee,
              "attention": attention, "q": q, "sort": sort}
    kept = {k: v for k, v in params.items() if v and not (k == "status" and v == "active") and not (k == "sort" and v == "queue")}

    def url(**overrides) -> str:
        merged = {**kept, **overrides}
        merged = {k: v for k, v in merged.items() if v not in ("", None, 1)}
        return "/dashboard/tickets" + (f"?{urlencode(merged)}" if merged else "")

    counts = metrics.ticket_counts(db)
    counts["all"] = counts["open"] + counts["pending"] + counts["resolved"] + counts["closed"]
    categories = sorted({c for (c,) in db.execute(select(func.coalesce(Ticket.category, Ticket.ai_category)).distinct()) if c} | set(CATEGORIES))
    agents = db.scalars(select(User).where(User.role.in_([UserRole.agent, UserRole.admin]), User.is_active.is_(True)).order_by(User.full_name)).all()
    ctx = {
        "rows": rows, "total": total, "page_no": page_no, "pages": pages, "params": params, "counts": counts,
        "tabs": TABS, "url": url, "categories": categories, "agents": agents, "filtered": bool(kept),
        "prev_url": url(page=page_no - 1) if page_no > 1 else None,
        "next_url": url(page=page_no + 1) if page_no < pages else None,
        "first": (page_no - 1) * PAGE_SIZE + 1 if total else 0, "last": (page_no - 1) * PAGE_SIZE + len(rows),
    }
    if is_htmx(request):
        response = render(request, "partials/_queue.html", ctx, user=user)
        response.headers["Vary"] = "HX-Request"
        response.headers["HX-Push-Url"] = url(page=page_no)  # clean address: no empty filter parameters
        return response
    return page(request, "tickets.html", ctx, user, db)


# =======================================================================================
# Detail
# =======================================================================================
def _thread(db, ticket: Ticket) -> list[dict]:
    """The conversation: the customer's original message, then replies and internal notes."""
    items = [{"kind": "customer", "who": ticket.customer.full_name, "when": ticket.created_at,
              "body": ticket.description, "note": False, "first": True}]
    comments = db.scalars(select(Comment).where(Comment.ticket_id == ticket.id).options(selectinload(Comment.author)).order_by(Comment.id))
    for c in comments:
        kind = "customer" if c.author.role == UserRole.customer else "agent"
        items.append({"kind": "note" if c.is_internal else kind, "who": c.author.full_name, "when": c.created_at,
                      "body": c.body, "note": c.is_internal, "first": False})
    return items


def _context(db, user: User, ticket_id: int, *, suggestion_note: str | None = None) -> dict:
    ticket = get_ticket_or_404(db, ticket_id, user)
    if ticket.embedding is None:
        duplicates.embed_ticket(ticket)
        db.commit()
    similar = []
    for other, score in duplicates.find_similar(db, ticket):
        similar.append({"ticket": other, "customer_name": other.customer.full_name, "pct": round(score * 100),
                        "can_merge": other.customer_id == ticket.customer_id})
    pending = db.scalars(select(SuggestedReply).where(SuggestedReply.ticket_id == ticket.id,
                                                      SuggestedReply.status == ReplyStatus.pending).order_by(SuggestedReply.id.desc())).all()
    import json
    drafts = [{"id": r.id, "body": r.body, "confidence": round(r.confidence * 100), "llm": r.llm,
               "sources": json.loads(r.sources_json)} for r in pending]
    agents = db.scalars(select(User).where(User.role.in_([UserRole.agent, UserRole.admin]), User.is_active.is_(True)).order_by(User.full_name)).all()
    categories = list(CATEGORIES)
    for c in (ticket.category, ticket.ai_category):
        if c and c not in categories:
            categories.append(c)
    merged_into = db.get(Ticket, ticket.merged_into_id) if ticket.merged_into_id else None
    return {
        "t": ticket, "thread": _thread(db, ticket), "duplicates": similar, "drafts": drafts, "agents": agents,
        "categories": categories, "statuses": [s.value for s in TicketStatus], "priorities": [p.value for p in TicketPriority],
        "merged_into": merged_into, "suggestion_note": suggestion_note, "oob": False,
        "closed": ticket.status == TicketStatus.closed,
    }


@router.get("/tickets/{ticket_id}")
def detail(ticket_id: int, request: Request, user: WebStaff, db: DB):
    return page(request, "ticket.html", _context(db, user, ticket_id), user, db)


def _fragment(request, user, db, name: str, ticket_id: int, *, message: str | None = None, level: str = "success", **extra):
    ctx = {**_context(db, user, ticket_id, **{k: v for k, v in extra.items() if k == "suggestion_note"}), **extra}
    ctx["chips_oob"] = name.endswith("_t_sidebar.html")  # keep the header chips in step with the sidebar
    headers = toast_header(message, level) if message else None
    return render(request, name, ctx, user=user, headers=headers)


# ---------------------------------------------------------------- case fields
@router.post("/tickets/{ticket_id}/fields")
def update_fields(ticket_id: int, request: Request, user: WebStaff, db: DB,
                  status_: str | None = Form(None, alias="status"), priority: str | None = Form(None),
                  category: str | None = Form(None), assignee: str | None = Form(None)):
    """Fields that are not sent are left alone. To clear the category or assignee, send "none"."""
    ticket = get_ticket_or_404(db, ticket_id, user)
    changes = {}
    if status_ is not None and status_ != ticket.status.value:
        changes["status"] = status_
    if priority is not None and priority != ticket.priority.value:
        changes["priority"] = priority
    # "none" is the explicit way to clear a field. An empty string cannot be used for that:
    # FastAPI treats empty form values as missing, which would silently ignore "Unassigned".
    if category is not None:
        wanted_category = None if category == "none" else category
        if wanted_category != ticket.category:
            changes["category"] = wanted_category
    if assignee is not None:
        wanted = int(assignee) if assignee.isdigit() else None
        if wanted != ticket.assignee_id:
            changes["assignee_id"] = wanted
    if not changes:
        return _fragment(request, user, db, "partials/_t_sidebar.html", ticket_id)
    try:
        tickets_api.update_ticket(ticket_id, TicketUpdate(**changes), user, db)
    except (HTTPException, ValidationError) as exc:
        db.rollback()  # re-render the real state so the form snaps back to what is saved
        return _fragment(request, user, db, "partials/_t_sidebar.html", ticket_id, message=_error_text(exc), level="error")
    return _fragment(request, user, db, "partials/_t_sidebar.html", ticket_id, message="Ticket updated.")


# ---------------------------------------------------------------- conversation
@router.post("/tickets/{ticket_id}/comments")
def add_comment(ticket_id: int, request: Request, user: WebStaff, db: DB, body: str = Form(""), internal: str = Form("")):
    try:
        tickets_api.add_comment(ticket_id, CommentCreate(body=body.strip(), is_internal=bool(internal)), user, db)
    except (HTTPException, ValidationError) as exc:
        db.rollback()
        return toast(_error_text(exc), "error")
    return _fragment(request, user, db, "partials/_t_thread.html", ticket_id,
                     message="Note saved." if internal else "Reply sent to the customer.")


# ---------------------------------------------------------------- AI: suggested replies
@router.post("/tickets/{ticket_id}/suggest")
def suggest(ticket_id: int, request: Request, user: WebStaff, db: DB):
    note = None
    try:
        result = ticket_ai.suggest_reply(ticket_id, user, db, get_llm())
        if result.draft is None:
            note = result.reason or "The knowledge base does not answer this ticket."
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return _fragment(request, user, db, "partials/_t_suggestions.html", ticket_id, suggestion_note=note)


@router.post("/tickets/{ticket_id}/replies/{reply_id}/accept")
def accept_reply(ticket_id: int, reply_id: int, request: Request, user: WebStaff, db: DB, body: str = Form("")):
    try:
        ticket_ai.accept_reply(ticket_id, reply_id, AcceptReplyIn(body=body.strip() or None), user, db)
    except (HTTPException, ValidationError) as exc:
        db.rollback()
        return toast(_error_text(exc), "error")
    # The thread is the main target; the suggestions panel refreshes out-of-band.
    return _fragment(request, user, db, "partials/_t_after_reply.html", ticket_id, message="Reply sent to the customer.")


@router.post("/tickets/{ticket_id}/replies/{reply_id}/reject")
def reject_reply(ticket_id: int, reply_id: int, request: Request, user: WebStaff, db: DB):
    try:
        ticket_ai.reject_reply(ticket_id, reply_id, RejectReplyIn(), user, db)
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return _fragment(request, user, db, "partials/_t_suggestions.html", ticket_id, message="Draft discarded.", suggestion_note=None)


# ---------------------------------------------------------------- AI: triage, duplicates
@router.post("/tickets/{ticket_id}/triage")
def retriage(ticket_id: int, request: Request, user: WebStaff, db: DB, apply: str = ""):
    try:
        ticket_ai.retriage(ticket_id, user, db, get_triager(), apply=bool(apply))
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return _fragment(request, user, db, "partials/_t_sidebar.html", ticket_id,
                     message="Category and priority updated from the AI suggestion." if apply else "Triage refreshed.")


@router.post("/tickets/{ticket_id}/merge")
def merge(ticket_id: int, user: WebStaff, db: DB, target_id: int = Form(...)):
    try:
        ticket_ai.merge_ticket(ticket_id, MergeIn(target_id=target_id), user, db)
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return redirect(f"/dashboard/tickets/{target_id}")
