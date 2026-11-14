"""AI-assisted ticket workflow (staff only): triage, duplicates, suggested replies."""
import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.helpers import get_ticket_or_404, reply_out, ticket_out
from app.core.deps import DB, StaffUser
from app.models import (
    Comment,
    ReplyStatus,
    SuggestedReply,
    Ticket,
    TicketStatus,
)
from app.schemas import (
    AcceptReplyIn,
    AIStats,
    CommentOut,
    DuplicateOut,
    MergeIn,
    RejectReplyIn,
    SuggestedReplyOut,
    SuggestReplyOut,
    TicketOut,
)
from app.services import chat as chat_service
from app.services import duplicates, rag
from app.services.ai_pipeline import apply_triage, refresh_attention, refresh_duplicates, store_triage
from app.services.llm import LLM, get_llm
from app.services.triage import Triager, get_triager

router = APIRouter(tags=["ai"])

# Dependencies so tests (and you) can swap the LLM / triager.
LLMDep = Annotated[LLM, Depends(get_llm)]
TriagerDep = Annotated[Triager, Depends(get_triager)]


def _add_internal_note(db, ticket_id: int, author_id: int, body: str) -> None:
    db.add(Comment(ticket_id=ticket_id, author_id=author_id, body=body, is_internal=True))


# ---------- triage ----------
@router.post("/tickets/{ticket_id}/triage", response_model=TicketOut)
def retriage(ticket_id: int, user: StaffUser, db: DB, triager: TriagerDep, apply: bool = False):
    """Re-run AI triage. Updates the suggestion only; pass ?apply=true to also copy the
    suggested category and priority onto the ticket (overwriting what is there)."""
    ticket = get_ticket_or_404(db, ticket_id, user)
    store_triage(ticket, triager.triage(ticket.subject, ticket.description))
    if apply:
        apply_triage(ticket, force=True)
    refresh_attention(db, ticket)
    db.commit()
    return ticket_out(ticket, user)


# ---------- duplicates ----------
@router.get("/tickets/{ticket_id}/duplicates", response_model=list[DuplicateOut])
def list_duplicates(ticket_id: int, user: StaffUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    if ticket.embedding is None:  # tickets created before AI was on, or while it was off
        duplicates.embed_ticket(ticket)
        db.commit()
    return [
        DuplicateOut(
            ticket_id=other.id,
            subject=other.subject,
            status=other.status,
            customer_id=other.customer_id,
            score=score,
            same_customer=other.customer_id == ticket.customer_id,
            can_merge=other.customer_id == ticket.customer_id,
        )
        for other, score in duplicates.find_similar(db, ticket)
    ]


@router.post("/tickets/{ticket_id}/merge", response_model=TicketOut)
def merge_ticket(ticket_id: int, payload: MergeIn, user: StaffUser, db: DB):
    """Merge this ticket (the duplicate) into `target_id` (the original). The duplicate is
    closed and linked; both get an internal note. Only tickets from the same customer can be
    merged, so one customer's conversation never gets attached to another's."""
    source = get_ticket_or_404(db, ticket_id, user)
    if payload.target_id == source.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A ticket cannot be merged into itself")
    target = db.get(Ticket, payload.target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target ticket not found")
    if source.customer_id != target.customer_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only tickets from the same customer can be merged")
    if source.merged_into_id is not None or target.merged_into_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is already merged")
    if source.status == TicketStatus.closed or target.status == TicketStatus.closed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Closed tickets cannot be merged")

    now = datetime.now(timezone.utc)
    source.merged_into_id = target.id
    source.status = TicketStatus.closed
    source.resolved_at = source.resolved_at or now
    source.needs_attention, source.attention_reason = False, None
    source.possible_duplicate_of_id = source.duplicate_score = None
    _add_internal_note(db, source.id, user.id, f"Merged into ticket #{target.id} by {user.full_name}.")
    _add_internal_note(db, target.id, user.id, f"Ticket #{source.id} (\"{source.subject}\") was merged into this ticket by {user.full_name}.")
    db.commit()
    return ticket_out(source, user)


# ---------- suggested replies ----------
@router.post("/tickets/{ticket_id}/suggest-reply", response_model=SuggestReplyOut)
def suggest_reply(ticket_id: int, user: StaffUser, db: DB, llm: LLMDep):
    """Draft a reply from the knowledge base. Nothing is sent: an agent must accept it."""
    ticket = get_ticket_or_404(db, ticket_id, user)
    if ticket.status == TicketStatus.closed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is closed")
    result = rag.draft_reply(db, ticket.customer.full_name, ticket.subject, ticket.description, llm)
    if result.body is None:
        return SuggestReplyOut(draft=None, escalate=True, confidence=result.confidence, reason=result.reason)
    reply = SuggestedReply(
        ticket_id=ticket.id,
        body=result.body,
        sources_json=json.dumps(
            [{"article_id": h.article_id, "title": h.title, "score": h.score} for h in result.sources]
        ),
        confidence=result.confidence,
        llm=result.llm,
        requested_by_id=user.id,
    )
    db.add(reply)
    db.commit()
    return SuggestReplyOut(draft=reply_out(reply), escalate=False, confidence=result.confidence, reason=None)


@router.get("/tickets/{ticket_id}/suggested-replies", response_model=list[SuggestedReplyOut])
def list_suggested_replies(ticket_id: int, user: StaffUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    replies = db.scalars(
        select(SuggestedReply).where(SuggestedReply.ticket_id == ticket.id).order_by(SuggestedReply.id.desc())
    ).all()
    return [reply_out(r) for r in replies]


def _pending_reply(db, ticket: Ticket, reply_id: int) -> SuggestedReply:
    reply = db.get(SuggestedReply, reply_id)
    if reply is None or reply.ticket_id != ticket.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suggested reply not found")
    if reply.status != ReplyStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Reply was already {reply.status.value}")
    return reply


@router.post("/tickets/{ticket_id}/suggested-replies/{reply_id}/accept", response_model=CommentOut)
def accept_reply(ticket_id: int, reply_id: int, payload: AcceptReplyIn, user: StaffUser, db: DB):
    """The human 'send' step: posts the draft (optionally edited) as a public reply."""
    ticket = get_ticket_or_404(db, ticket_id, user)
    reply = _pending_reply(db, ticket, reply_id)
    if ticket.status == TicketStatus.closed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is closed")
    body = payload.body or reply.body
    comment = Comment(ticket_id=ticket.id, author_id=user.id, body=body, is_internal=False)
    db.add(comment)
    db.flush()
    reply.comment_id = comment.id
    reply.status = ReplyStatus.edited if body.strip() != reply.body.strip() else ReplyStatus.accepted
    reply.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return comment


@router.post("/tickets/{ticket_id}/suggested-replies/{reply_id}/reject", response_model=SuggestedReplyOut)
def reject_reply(ticket_id: int, reply_id: int, payload: RejectReplyIn, user: StaffUser, db: DB):
    ticket = get_ticket_or_404(db, ticket_id, user)
    reply = _pending_reply(db, ticket, reply_id)
    reply.status = ReplyStatus.rejected
    reply.reject_reason = payload.reason
    reply.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return reply_out(reply)


# ---------- stats ----------
@router.get("/ai/stats", response_model=AIStats)
def ai_stats(_: StaffUser, db: DB):
    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    by_status = {s: count(select(func.count()).select_from(SuggestedReply).where(SuggestedReply.status == s)) for s in ReplyStatus}
    decided = by_status[ReplyStatus.accepted] + by_status[ReplyStatus.edited] + by_status[ReplyStatus.rejected]
    positive = by_status[ReplyStatus.accepted] + by_status[ReplyStatus.edited]
    return AIStats(
        tickets_total=count(select(func.count()).select_from(Ticket)),
        tickets_triaged=count(select(func.count()).select_from(Ticket).where(Ticket.triaged_at.is_not(None))),
        needs_attention=count(select(func.count()).select_from(Ticket).where(Ticket.needs_attention.is_(True))),
        possible_duplicates=count(select(func.count()).select_from(Ticket).where(Ticket.possible_duplicate_of_id.is_not(None))),
        replies_suggested=sum(by_status.values()),
        replies_accepted=by_status[ReplyStatus.accepted],
        replies_edited=by_status[ReplyStatus.edited],
        replies_rejected=by_status[ReplyStatus.rejected],
        replies_pending=by_status[ReplyStatus.pending],
        acceptance_rate=round(positive / decided, 3) if decided else None,
        **chat_service.chat_stats(db),
    )
