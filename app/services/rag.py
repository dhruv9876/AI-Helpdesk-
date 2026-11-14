"""Retrieval-augmented answering with an escalation path."""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import kb
from app.services.llm import NOT_FOUND, LLM, get_llm


@dataclass
class RagResult:
    answer: str | None
    escalate: bool
    confidence: float
    sources: list[kb.Hit]
    llm: str


def answer_question(db: Session, question: str, llm: LLM | None = None) -> RagResult:
    llm = llm or get_llm()
    hits = kb.search(db, question)
    confidence = hits[0].score if hits else 0.0

    # Guardrail 1: weak retrieval -> don't even call the LLM, hand over to a human.
    if confidence < settings.kb_min_score:
        return RagResult(None, True, confidence, hits, llm.name)

    answer = llm.answer(question, [h.text for h in hits])

    # Guardrail 2: the model itself says the passages don't answer the question.
    if not answer or answer.strip().startswith(NOT_FOUND):
        return RagResult(None, True, confidence, hits, llm.name)

    return RagResult(answer, False, confidence, hits, llm.name)


@dataclass
class ReplyDraft:
    body: str | None
    escalate: bool
    confidence: float
    sources: list[kb.Hit]
    llm: str
    reason: str | None


def draft_reply(db: Session, customer_name: str, subject: str, description: str, llm: LLM | None = None) -> ReplyDraft:
    """Draft a reply for an agent to review, grounded in the knowledge base.

    Same two guardrails as /kb/ask: weak retrieval, or the model saying NOT_FOUND, means no draft
    (the agent should write the reply themselves) rather than a made-up one.
    """
    llm = llm or get_llm()
    hits = kb.search(db, f"{subject}. {description}")
    confidence = hits[0].score if hits else 0.0
    if confidence < settings.kb_min_score:
        return ReplyDraft(None, True, confidence, hits, llm.name, "No knowledge-base article matches this ticket closely enough.")
    body = llm.draft_reply(customer_name, subject, description, [h.text for h in hits])
    if not body or body.strip().startswith(NOT_FOUND):
        return ReplyDraft(None, True, confidence, hits, llm.name, "The knowledge base does not answer this ticket.")
    return ReplyDraft(body.strip(), False, confidence, hits, llm.name, None)
