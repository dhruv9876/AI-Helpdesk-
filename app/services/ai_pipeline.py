"""What happens to a ticket after it is created: triage, duplicate check, attention flags.

Runs as a FastAPI background task so the customer's request returns immediately. It never raises:
if the AI step fails the ticket simply stays untriaged and an agent can re-run triage manually.

Suggest-only policy: triage results are always stored in the ai_* fields. They are copied onto the
ticket's real `category` / `priority` only once, on first triage, and only if nobody has set them
yet (category empty, priority still the default). After that, humans own those fields.
"""
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Sentiment, Ticket, TicketPriority
from app.services import duplicates
from app.services.triage import Triager, TriageResult, get_triager

log = logging.getLogger(__name__)


def store_triage(ticket: Ticket, result: TriageResult) -> None:
    ticket.ai_category = result.category
    ticket.ai_priority = result.priority
    ticket.sentiment = result.sentiment
    ticket.ai_confidence = result.confidence
    ticket.ai_source = result.source
    ticket.triaged_at = datetime.now(timezone.utc)


def apply_triage(ticket: Ticket, force: bool = False) -> None:
    """Copy the stored suggestion onto the real fields.

    force=False (automatic, first run only): fill only fields no human has set.
    force=True (agent clicked "apply"): overwrite.
    """
    if ticket.ai_category is None:
        return
    if force or (not ticket.triage_applied and ticket.category is None):
        ticket.category = ticket.ai_category
    if force or (not ticket.triage_applied and ticket.priority in (None, TicketPriority.medium)):
        ticket.priority = ticket.ai_priority or ticket.priority
    ticket.triage_applied = True


def refresh_attention(db: Session, ticket: Ticket, matches: list[tuple[Ticket, float]] | None = None) -> None:
    """Flag tickets an agent should look at first, and say why.

    `matches` are the similar active tickets (looked up if not supplied). Repeat contact means
    ANY of them belongs to the same customer, not just the single best match, which could be
    another customer's ticket.
    """
    if matches is None:
        matches = duplicates.find_similar(db, ticket)
    reasons = []
    if TicketPriority.urgent in (ticket.ai_priority, ticket.priority):
        reasons.append("urgent priority")
    if ticket.sentiment == Sentiment.angry:
        reasons.append("angry customer")
    if any(other.customer_id == ticket.customer_id for other, _ in matches):
        reasons.append("repeat contact: customer has a similar open ticket")
    ticket.needs_attention = bool(reasons)
    ticket.attention_reason = "; ".join(reasons) or None


def refresh_duplicates(db: Session, ticket: Ticket) -> list[tuple[Ticket, float]]:
    duplicates.embed_ticket(ticket)
    matches = duplicates.find_similar(db, ticket)
    if matches:
        best, score = matches[0]
        ticket.possible_duplicate_of_id, ticket.duplicate_score = best.id, score
    else:
        ticket.possible_duplicate_of_id, ticket.duplicate_score = None, None
    return matches


def run_ai_pipeline(
    ticket_id: int, session_factory: Callable[[], Session], triager: Triager | None = None
) -> None:
    try:
        with session_factory() as db:
            ticket = db.get(Ticket, ticket_id)
            if ticket is None:
                return
            triager = triager or get_triager()
            store_triage(ticket, triager.triage(ticket.subject, ticket.description))
            apply_triage(ticket)
            matches = refresh_duplicates(db, ticket)
            refresh_attention(db, ticket, matches)
            db.commit()
    except Exception:  # background task: never crash the worker, just record it
        log.exception("AI pipeline failed for ticket %s", ticket_id)
