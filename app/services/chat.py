"""Live chat logic: bot-first answering, handoff to humans, claiming, closing, ticket conversion.

Pure synchronous service code with no WebSocket knowledge. Each function does its database work
and returns an `Outcome`: plain, JSON-ready events for the transport layer (api/chats.py and
chat_hub.py) to deliver. That keeps all the rules testable without opening a socket.

Chat life cycle:

    bot ──(customer asks for a person / sounds upset / bot unsure)──> waiting ──(agent claims)──> active
     │                                                                    │                          │
     └──────────────(agent takes over a bot chat)─────────────────────────┴──────────────────────────┤
                                                                                                     ▼
                                                                                                   closed
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    ChatMessage,
    ChatSession,
    ChatStatus,
    Sentiment,
    SenderType,
    Ticket,
    User,
    UserRole,
)
from app.schemas import ChatMessageOut, ChatOut, ReplySource
from app.services import rag
from app.services.llm import LLM
from app.services.triage import detect_sentiment

BOT_NAME = "Support Assistant"
BOT_FOOTER = '\n\nIf this didn\'t help, type "agent" to talk to a person.'
HANDOFF_TEXT = "I'm connecting you with a human agent. Someone will join this chat shortly."
NO_AGENTS_TEXT = (
    " No agents appear to be online right now. If you'd rather not wait, you can turn this chat "
    "into a support ticket and we'll reply there."
)

# ---------------------------------------------------------------------------------------
# "Let me talk to a person" detection
# ---------------------------------------------------------------------------------------
_PEOPLE = r"(human|person|people|agent|representative|rep|manager|supervisor|operator|someone|somebody|staff|support team)"
_HUMAN_PATTERNS = [
    re.compile(rf"\b(real|live|actual) (person|human|agent|representative)\b"),
    re.compile(rf"\b(speak|talk|chat|connect|transfer)\b[^.!?]{{0,25}}\b{_PEOPLE}\b"),
    re.compile(rf"\b(need|want|get|give|put) (me )?(a |an |the )?{_PEOPLE}\b"),
    re.compile(r"\bcustomer (service|support) (rep|representative|agent)\b"),
    re.compile(r"^\W*(human|agent|representative|operator|person|someone)\W*(please\W*)?$"),
]


def wants_human(text: str) -> bool:
    lowered = text.lower().strip()
    return any(p.search(lowered) for p in _HUMAN_PATTERNS)


# ---------------------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------------------
class ChatError(Exception):
    """A rule violation. `status_code` mirrors the HTTP status the REST layer will return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Outcome:
    events: list[dict] = field(default_factory=list)  # delivered to everyone in the chat
    queue: list[dict] = field(default_factory=list)  # delivered to staff watching the queue
    messages: list[ChatMessageOut] = field(default_factory=list)  # what this call created


# ---------------------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_staff(user: User) -> bool:
    return user.role in (UserRole.agent, UserRole.admin)


def message_out(m: ChatMessage) -> ChatMessageOut:
    names = {
        SenderType.bot: BOT_NAME,
        SenderType.system: "System",
    }
    name = names.get(m.sender_type) or (m.sender.full_name if m.sender else m.sender_type.value.title())
    return ChatMessageOut(
        id=m.id,
        chat_id=m.chat_id,
        sender_type=m.sender_type,
        sender_id=m.sender_id,
        sender_name=name,
        body=m.body,
        sources=[ReplySource(**s) for s in json.loads(m.sources_json)],
        created_at=m.created_at,
    )


def chat_out(chat: ChatSession, include_reason: bool, last_message: str | None = None) -> ChatOut:
    return ChatOut(
        id=chat.id,
        status=chat.status,
        customer_id=chat.customer_id,
        customer_name=chat.customer.full_name,
        agent_id=chat.agent_id,
        agent_name=chat.agent.full_name if chat.agent else None,
        handoff_reason=chat.handoff_reason if include_reason else None,  # customers never see this
        waiting_since=chat.escalated_at if include_reason else None,
        ticket_id=chat.ticket_id,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        closed_at=chat.closed_at,
        last_message=last_message,
    )


def _messages(db: Session, chat_id: int) -> list[ChatMessage]:
    """Always read history with an explicit query. `chat.messages` is a cached relationship that
    goes stale if messages were added through the same session in the meantime."""
    return list(db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)))


def _add_message(db: Session, chat: ChatSession, sender: SenderType, sender_id: int | None, body: str,
                 sources: list[dict] | None = None) -> ChatMessage:
    m = ChatMessage(chat_id=chat.id, sender_type=sender, sender_id=sender_id, body=body,
                    sources_json=json.dumps(sources or []))
    db.add(m)
    db.flush()
    chat.updated_at = _now()
    return m


def _emit_message(outcome: Outcome, m: ChatMessage) -> None:
    out = message_out(m)
    outcome.messages.append(out)
    outcome.events.append({"type": "message", "message": out.model_dump(mode="json")})


def _emit_status(outcome: Outcome, chat: ChatSession) -> None:
    agent = {"id": chat.agent_id, "name": chat.agent.full_name} if chat.agent else None
    outcome.events.append({"type": "status", "chat_id": chat.id, "status": chat.status.value, "agent": agent})


def _queue_event(kind: str, chat: ChatSession) -> dict:
    return {"type": kind, "chat": chat_out(chat, include_reason=True).model_dump(mode="json")}


# ---------------------------------------------------------------------------------------
# Loading with access control
# ---------------------------------------------------------------------------------------
def _user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise ChatError(403, "Account is disabled")
    return user


def _chat_for(db: Session, chat_id: int, user: User) -> ChatSession:
    """Customers get 404 (not 403) for other people's chats so ids can't be probed."""
    chat = db.get(ChatSession, chat_id)
    if chat is None or (not is_staff(user) and chat.customer_id != user.id):
        raise ChatError(404, "Chat not found")
    return chat


# ---------------------------------------------------------------------------------------
# Opening, reading, listing
# ---------------------------------------------------------------------------------------
def open_chat(db: Session, user_id: int) -> tuple[ChatSession, bool, Outcome]:
    """Start a chat, or return the customer's existing open one (one open chat per customer)."""
    user = _user(db, user_id)
    if is_staff(user):
        raise ChatError(403, "Only customers can start a chat")
    existing = db.scalar(
        select(ChatSession)
        .where(ChatSession.customer_id == user.id, ChatSession.status != ChatStatus.closed)
        .order_by(ChatSession.id.desc())
    )
    if existing:
        return existing, False, Outcome()
    chat = ChatSession(customer_id=user.id)
    db.add(chat)
    db.flush()
    outcome = Outcome()
    first = (user.full_name.split() or ["there"])[0]
    greeting = _add_message(
        db, chat, SenderType.bot, None,
        f'Hi {first}! I\'m the support assistant. Ask me a question, or type "agent" to talk to a person.',
    )
    _emit_message(outcome, greeting)
    db.commit()
    return chat, True, outcome


def snapshot(db: Session, chat_id: int, user_id: int) -> dict:
    """Everything a client needs when it connects: the chat and its history."""
    user = _user(db, user_id)
    chat = _chat_for(db, chat_id, user)
    return {
        "chat": chat_out(chat, include_reason=is_staff(user)).model_dump(mode="json"),
        "messages": [message_out(m).model_dump(mode="json") for m in _messages(db, chat.id)],
    }


def history(db: Session, chat_id: int, user_id: int) -> tuple[ChatOut, list[ChatMessageOut]]:
    """The chat and its messages as objects (the dashboard renders them server-side first)."""
    user = _user(db, user_id)
    chat = _chat_for(db, chat_id, user)
    return chat_out(chat, include_reason=is_staff(user)), [message_out(m) for m in _messages(db, chat.id)]


def list_chats(db: Session, user_id: int, status: ChatStatus | None = None, limit: int = 50) -> list[ChatOut]:
    user = _user(db, user_id)
    stmt = select(ChatSession)
    if not is_staff(user):
        stmt = stmt.where(ChatSession.customer_id == user.id)
    if status:
        stmt = stmt.where(ChatSession.status == status)
    # Waiting chats oldest-first (fair queue); everything else newest-first.
    order = ChatSession.escalated_at.asc() if status == ChatStatus.waiting else ChatSession.updated_at.desc()
    result = []
    for chat in db.scalars(stmt.order_by(order, ChatSession.id.desc()).limit(limit)):
        # The preview is what the customer last SAID: that is what an agent needs to pick a chat.
        # (System notices like "connecting you with an agent" say nothing about the problem.)
        last_said = select(ChatMessage.body).where(ChatMessage.chat_id == chat.id, ChatMessage.sender_type == SenderType.customer)
        last = db.scalar(last_said.order_by(ChatMessage.id.desc()).limit(1)) or db.scalar(
            select(ChatMessage.body).where(ChatMessage.chat_id == chat.id).order_by(ChatMessage.id.desc()).limit(1))
        result.append(chat_out(chat, include_reason=is_staff(user), last_message=(last or "")[:120] or None))
    return result


def waiting_queue(db: Session) -> list[dict]:
    stmt = select(ChatSession).where(ChatSession.status == ChatStatus.waiting).order_by(ChatSession.escalated_at)
    return [chat_out(c, include_reason=True).model_dump(mode="json") for c in db.scalars(stmt)]


# ---------------------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------------------
def _check_rate(db: Session, chat: ChatSession, user_id: int) -> None:
    since = _now() - timedelta(seconds=60)
    recent = db.scalar(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.chat_id == chat.id,
            ChatMessage.sender_id == user_id,
            ChatMessage.sender_type.in_([SenderType.customer, SenderType.agent]),
            ChatMessage.created_at >= since,
        )
    )
    if (recent or 0) >= settings.chat_messages_per_minute:
        raise ChatError(429, "You're sending messages too quickly. Please wait a moment.")


def _unique_sources(hits) -> list[dict]:
    best: dict[int, dict] = {}
    for h in hits:
        if h.article_id not in best or h.score > best[h.article_id]["score"]:
            best[h.article_id] = {"article_id": h.article_id, "title": h.title, "score": h.score}
    ranked = sorted(best.values(), key=lambda s: s["score"], reverse=True)
    # Cite what actually backs the answer: the best article, plus any that match nearly as well.
    # Weakly related articles in the top 3 would make the answer look less trustworthy.
    return [s for s in ranked if s["score"] >= 0.6 * ranked[0]["score"]][:3]


def _hand_off(db: Session, chat: ChatSession, reason: str, agents_online: bool, outcome: Outcome) -> None:
    chat.status = ChatStatus.waiting
    chat.handoff_reason = reason
    chat.escalated_at = _now()
    text = HANDOFF_TEXT + ("" if agents_online else NO_AGENTS_TEXT)
    _emit_message(outcome, _add_message(db, chat, SenderType.system, None, text))
    _emit_status(outcome, chat)
    outcome.queue.append(_queue_event("chat_waiting", chat))


def send_message(db: Session, chat_id: int, user_id: int, body: str, llm: LLM | None = None,
                 agents_online: bool = False) -> Outcome:
    """A customer or the assigned agent sends a message."""
    user = _user(db, user_id)
    chat = _chat_for(db, chat_id, user)
    body = (body or "").strip()
    if not body:
        raise ChatError(422, "Message cannot be empty")
    if len(body) > settings.chat_max_message_chars:
        raise ChatError(422, f"Message is longer than {settings.chat_max_message_chars} characters")
    if chat.status == ChatStatus.closed:
        raise ChatError(409, "This chat is closed")

    outcome = Outcome()
    if is_staff(user):
        # Only the agent who claimed the chat may speak in it; other staff can just watch.
        if chat.agent_id != user.id or chat.status != ChatStatus.active:
            raise ChatError(403, "Claim this chat before replying")
        _check_rate(db, chat, user.id)
        _emit_message(outcome, _add_message(db, chat, SenderType.agent, user.id, body))
        db.commit()
        return outcome

    _check_rate(db, chat, user.id)
    _emit_message(outcome, _add_message(db, chat, SenderType.customer, user.id, body))
    if chat.status != ChatStatus.bot:  # waiting or with an agent: humans handle it from here
        db.commit()
        return outcome

    # --- the bot's turn: decide between answering and handing off ---
    prior_answers = db.scalar(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.chat_id == chat.id,
            ChatMessage.sender_type == SenderType.bot,
            ChatMessage.sources_json != "[]",
        )
    ) or 0
    mood = detect_sentiment(body)
    reason = None
    if wants_human(body):
        reason = "Customer asked for a human"
    elif mood == Sentiment.angry:
        reason = "Customer seems upset"
    elif mood == Sentiment.frustrated and prior_answers > 0:
        reason = "Customer still frustrated after a bot answer"
    else:
        result = rag.answer_question(db, body, llm)
        if result.escalate:
            reason = "Bot could not find a confident answer"
        else:
            _emit_message(outcome, _add_message(
                db, chat, SenderType.bot, None, result.answer + BOT_FOOTER, _unique_sources(result.sources)))
    if reason:
        _hand_off(db, chat, reason, agents_online, outcome)
    db.commit()
    return outcome


# ---------------------------------------------------------------------------------------
# Agent actions
# ---------------------------------------------------------------------------------------
def claim_chat(db: Session, chat_id: int, user_id: int) -> Outcome:
    user = _user(db, user_id)
    if not is_staff(user):
        raise ChatError(403, "Only staff can claim chats")
    chat = db.get(ChatSession, chat_id)
    if chat is None:
        raise ChatError(404, "Chat not found")
    took_over_bot = chat.status == ChatStatus.bot
    # Atomic: two agents clicking "claim" at once cannot both win.
    claimed = db.execute(
        update(ChatSession)
        .where(ChatSession.id == chat_id, ChatSession.status.in_([ChatStatus.bot, ChatStatus.waiting]))
        .values(status=ChatStatus.active, agent_id=user.id, updated_at=_now())
        .execution_options(synchronize_session=False)
    ).rowcount
    if not claimed:
        db.rollback()
        raise ChatError(409, "This chat has already been taken or is closed")
    db.commit()
    db.refresh(chat)
    outcome = Outcome()
    if took_over_bot and not chat.handoff_reason:
        chat.handoff_reason = "Agent took over from the bot"  # so the hand-off rate counts it
    _emit_message(outcome, _add_message(db, chat, SenderType.system, None, f"{user.full_name} joined the chat."))
    _emit_status(outcome, chat)
    outcome.queue.append({"type": "chat_claimed", "chat_id": chat.id, "agent_id": user.id, "agent_name": user.full_name})
    db.commit()
    return outcome


def close_chat(db: Session, chat_id: int, user_id: int) -> Outcome:
    user = _user(db, user_id)
    chat = _chat_for(db, chat_id, user)
    is_owner = chat.customer_id == user.id
    is_assigned = chat.agent_id == user.id
    if not (is_owner or is_assigned or user.role == UserRole.admin):
        raise ChatError(403, "Only the customer, the assigned agent or an admin can close this chat")
    if chat.status == ChatStatus.closed:
        raise ChatError(409, "This chat is already closed")
    return _close(db, chat, "Chat closed.")


def _close(db: Session, chat: ChatSession, text: str) -> Outcome:
    was_waiting = chat.status == ChatStatus.waiting
    chat.status = ChatStatus.closed
    chat.closed_at = _now()
    outcome = Outcome()
    _emit_message(outcome, _add_message(db, chat, SenderType.system, None, text))
    _emit_status(outcome, chat)
    if was_waiting or chat.agent_id is None:
        outcome.queue.append({"type": "chat_closed", "chat_id": chat.id})  # leaves the waiting queue
    db.commit()
    return outcome


# ---------------------------------------------------------------------------------------
# Chat -> ticket
# ---------------------------------------------------------------------------------------
def _transcript(db: Session, chat: ChatSession) -> str:
    lines = []
    for m in _messages(db, chat.id):
        if m.sender_type == SenderType.system:
            continue
        who = {SenderType.customer: "Customer", SenderType.bot: "Support bot"}.get(m.sender_type)
        who = who or f"Agent ({m.sender.full_name if m.sender else 'support'})"
        lines.append(f"{who}: {m.body}")
    text = "Chat transcript:\n" + "\n".join(lines)
    return text if len(text) <= 5000 else text[:4985] + "\n...(truncated)"


def convert_to_ticket(db: Session, chat_id: int, user_id: int) -> tuple[Ticket, Outcome]:
    """Turn a chat into a ticket (e.g. no agent is online). The customer's first message becomes
    the subject and the full conversation becomes the description. The chat is then closed."""
    user = _user(db, user_id)
    chat = _chat_for(db, chat_id, user)
    if chat.ticket_id is not None:
        raise ChatError(409, f"This chat was already converted into ticket #{chat.ticket_id}")
    first = next((m for m in _messages(db, chat.id) if m.sender_type == SenderType.customer), None)
    if first is None:
        raise ChatError(422, "There is nothing to convert yet: the customer has not written anything")
    one_line = " ".join(first.body.split())
    subject = "Chat: " + (one_line if len(one_line) <= 80 else one_line[:77] + "...")
    ticket = Ticket(subject=subject, description=_transcript(db, chat), customer_id=chat.customer_id)
    db.add(ticket)
    db.flush()
    chat.ticket_id = ticket.id
    if chat.status == ChatStatus.closed:
        db.commit()
        return ticket, Outcome()
    return ticket, _close(db, chat, f"This chat was converted into ticket #{ticket.id}.")


# ---------------------------------------------------------------------------------------
# Stats (used by /ai/stats)
# ---------------------------------------------------------------------------------------
def chat_stats(db: Session) -> dict:
    total = db.scalar(select(func.count()).select_from(ChatSession)) or 0
    handed = db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.handoff_reason.is_not(None))) or 0
    return {
        "chats_total": total,
        "chats_handed_off": handed,
        "handoff_rate": round(handed / total, 3) if total else None,
    }
