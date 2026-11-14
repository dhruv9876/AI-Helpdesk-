"""Duplicate-ticket detection using the same embeddings as the knowledge base.

A new ticket is compared with every other *active* ticket (open or pending, not already merged).
The threshold (DUP_MIN_SCORE) was calibrated on labelled pairs: see scripts/eval_duplicates.py.
"""
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Ticket, TicketStatus
from app.services.embeddings import get_embedder

ACTIVE = (TicketStatus.open, TicketStatus.pending)


def ticket_text(ticket: Ticket) -> str:
    return f"{ticket.subject}\n{ticket.description}"


def embed_ticket(ticket: Ticket) -> None:
    embedder = get_embedder()
    ticket.embedding = embedder.embed([ticket_text(ticket)])[0].astype(np.float32).tobytes()
    ticket.embedder = embedder.name


def text_similarity(a: str, b: str) -> float:
    """Cosine similarity of two raw texts (used by the calibration script)."""
    va, vb = get_embedder().embed([a, b])
    return float(va @ vb)


def find_similar(
    db: Session, ticket: Ticket, threshold: float | None = None, limit: int = 5
) -> list[tuple[Ticket, float]]:
    """Active tickets whose similarity to `ticket` is at least `threshold`, best first."""
    threshold = settings.dup_min_score if threshold is None else threshold
    embedder = get_embedder()
    if ticket.embedding is None or ticket.embedder != embedder.name:
        return []
    others = db.scalars(
        select(Ticket).where(
            Ticket.id != ticket.id,
            Ticket.status.in_(ACTIVE),
            Ticket.merged_into_id.is_(None),
            Ticket.embedder == embedder.name,
            Ticket.embedding.is_not(None),
        )
    ).all()
    if not others:
        return []
    matrix = np.vstack([np.frombuffer(t.embedding, dtype=np.float32) for t in others])
    scores = matrix @ np.frombuffer(ticket.embedding, dtype=np.float32)
    ranked = sorted(zip(others, scores.tolist()), key=lambda ts: ts[1], reverse=True)
    return [(t, round(s, 4)) for t, s in ranked[:limit] if s >= threshold]
