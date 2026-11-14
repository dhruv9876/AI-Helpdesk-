"""Small helpers shared by the ticket routers."""
import json

from fastapi import HTTPException, status

from app.core.deps import is_staff
from app.models import SuggestedReply, Ticket, User
from app.schemas import AIInfo, ReplySource, SuggestedReplyOut, TicketOut


def get_ticket_or_404(db, ticket_id: int, user: User) -> Ticket:
    """Fetch a ticket the user may see. Customers get 404 (not 403) for others' tickets so ids
    can't be probed."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or (not is_staff(user) and ticket.customer_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


def ticket_out(ticket: Ticket, user: User) -> TicketOut:
    """Serialise a ticket. AI triage fields (sentiment, flags, duplicates) are staff-only."""
    out = TicketOut.model_validate(ticket)
    if is_staff(user):
        out.ai = AIInfo(
            category=ticket.ai_category,
            priority=ticket.ai_priority,
            sentiment=ticket.sentiment,
            confidence=ticket.ai_confidence,
            source=ticket.ai_source,
            triaged_at=ticket.triaged_at,
            needs_attention=ticket.needs_attention,
            attention_reason=ticket.attention_reason,
            possible_duplicate_of_id=ticket.possible_duplicate_of_id,
            duplicate_score=ticket.duplicate_score,
        )
    return out


def reply_out(reply: SuggestedReply) -> SuggestedReplyOut:
    return SuggestedReplyOut(
        id=reply.id,
        ticket_id=reply.ticket_id,
        body=reply.body,
        sources=[ReplySource(**s) for s in json.loads(reply.sources_json)],
        confidence=reply.confidence,
        llm=reply.llm,
        status=reply.status,
        created_at=reply.created_at,
        resolved_at=reply.resolved_at,
        comment_id=reply.comment_id,
        reject_reason=reply.reject_reason,
    )
