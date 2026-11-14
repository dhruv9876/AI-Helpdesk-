from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select

from app.api.helpers import get_ticket_or_404, ticket_out
from app.core.config import settings
from app.core.deps import DB, AdminUser, CurrentUser, SessionFactory, is_staff
from app.models import Comment, Ticket, TicketPriority, TicketStatus, User, UserRole
from app.schemas import (
    CommentCreate,
    CommentOut,
    TicketCreate,
    TicketDetail,
    TicketOut,
    TicketPage,
    TicketUpdate,
)
from app.services.ai_pipeline import run_ai_pipeline

router = APIRouter(prefix="/tickets", tags=["tickets"])

CUSTOMER_EDITABLE = {"subject", "description", "status"}


def _validate_assignee(db, assignee_id: int) -> None:
    assignee = db.get(User, assignee_id)
    if assignee is None or not assignee.is_active or assignee.role == UserRole.customer:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "assignee_id must be an active agent or admin",
        )


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    user: CurrentUser,
    db: DB,
    background: BackgroundTasks,
    session_factory: SessionFactory,
):
    ticket = Ticket(subject=payload.subject, description=payload.description, customer_id=user.id)
    db.add(ticket)
    db.commit()
    if settings.ai_auto_triage:
        # Runs after the response is sent, so creating a ticket stays fast.
        background.add_task(run_ai_pipeline, ticket.id, session_factory)
    return ticket_out(ticket, user)


@router.get("", response_model=TicketPage)
def list_tickets(
    user: CurrentUser,
    db: DB,
    status_: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = None,
    category: str | None = None,
    assignee_id: int | None = None,
    unassigned: bool = False,
    needs_attention: bool | None = Query(default=None, description="Staff only: AI-flagged tickets"),
    q: str | None = Query(default=None, description="Search subject and description"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Ticket)
    if not is_staff(user):
        stmt = stmt.where(Ticket.customer_id == user.id)
    elif needs_attention is not None:  # ignored for customers so flags can't be probed
        stmt = stmt.where(Ticket.needs_attention == needs_attention)
    if status_:
        stmt = stmt.where(Ticket.status == status_)
    if priority:
        stmt = stmt.where(Ticket.priority == priority)
    if category:
        stmt = stmt.where(Ticket.category == category)
    if assignee_id is not None:
        stmt = stmt.where(Ticket.assignee_id == assignee_id)
    if unassigned:
        stmt = stmt.where(Ticket.assignee_id.is_(None))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Ticket.subject.ilike(like), Ticket.description.ilike(like)))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(
        stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).offset(offset).limit(limit)
    ).all()
    return TicketPage(items=[ticket_out(t, user) for t in items], total=total, limit=limit, offset=offset)


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: int, user: CurrentUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    visible = [CommentOut.model_validate(c) for c in ticket.comments if is_staff(user) or not c.is_internal]
    return TicketDetail(**ticket_out(ticket, user).model_dump(), comments=visible)


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(ticket_id: int, payload: TicketUpdate, user: CurrentUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return ticket_out(ticket, user)

    if is_staff(user):
        if changes.get("assignee_id") is not None:
            _validate_assignee(db, changes["assignee_id"])
    else:
        if set(changes) - CUSTOMER_EDITABLE:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Customers can only edit the subject, description, or close the ticket",
            )
        if "status" in changes and changes["status"] != TicketStatus.closed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Customers can only close a ticket")
        if {"subject", "description"} & set(changes) and ticket.status != TicketStatus.open:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only open tickets can be edited")

    for key, value in changes.items():
        setattr(ticket, key, value)

    # Track when a ticket was first resolved (used for dashboard metrics later).
    if ticket.status in (TicketStatus.resolved, TicketStatus.closed):
        ticket.resolved_at = ticket.resolved_at or datetime.now(timezone.utc)
    else:
        ticket.resolved_at = None

    db.commit()
    return ticket_out(ticket, user)


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(ticket_id: int, _: AdminUser, db: DB):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    # Detach anything that points at this ticket so deleting it can't leave dangling references.
    for other in db.scalars(
        select(Ticket).where(or_(Ticket.merged_into_id == ticket_id, Ticket.possible_duplicate_of_id == ticket_id))
    ):
        if other.merged_into_id == ticket_id:
            other.merged_into_id = None
        if other.possible_duplicate_of_id == ticket_id:
            other.possible_duplicate_of_id, other.duplicate_score = None, None
    db.delete(ticket)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{ticket_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED
)
def add_comment(ticket_id: int, payload: CommentCreate, user: CurrentUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    if payload.is_internal and not is_staff(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only staff can add internal notes")
    if ticket.status == TicketStatus.closed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is closed")
    comment = Comment(
        ticket_id=ticket.id,
        author_id=user.id,
        body=payload.body,
        is_internal=payload.is_internal,
    )
    db.add(comment)
    db.commit()
    return comment
