import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(e: type[enum.Enum]) -> Enum:
    # Store the lowercase values ("open") instead of member names ("OPEN").
    return Enum(e, values_callable=lambda x: [m.value for m in x], native_enum=False, length=20)


class UserRole(str, enum.Enum):
    customer = "customer"
    agent = "agent"
    admin = "admin"


class TicketStatus(str, enum.Enum):
    open = "open"
    pending = "pending"
    resolved = "resolved"
    closed = "closed"


class TicketPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class Sentiment(str, enum.Enum):
    positive = "positive"
    neutral = "neutral"
    frustrated = "frustrated"
    angry = "angry"


class ReplyStatus(str, enum.Enum):
    pending = "pending"    # drafted, waiting for an agent
    accepted = "accepted"  # agent posted it unchanged
    edited = "edited"      # agent posted it after editing
    rejected = "rejected"  # agent discarded it


class ChatStatus(str, enum.Enum):
    bot = "bot"          # the assistant is answering
    waiting = "waiting"  # handed off, waiting in the agent queue
    active = "active"    # a human agent has joined
    closed = "closed"


class SenderType(str, enum.Enum):
    customer = "customer"
    bot = "bot"
    agent = "agent"
    system = "system"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.customer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus), default=TicketStatus.open, index=True
    )
    priority: Mapped[TicketPriority] = mapped_column(
        _enum(TicketPriority), default=TicketPriority.medium, index=True
    )
    # Filled in by staff now, and by AI triage in phase 3.
    category: Mapped[str | None] = mapped_column(String(50), default=None, index=True)

    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # --- AI triage (suggestions; see services/ai_pipeline.py for what is auto-applied) ---
    ai_category: Mapped[str | None] = mapped_column(String(50), default=None)
    ai_priority: Mapped[TicketPriority | None] = mapped_column(_enum(TicketPriority), default=None)
    sentiment: Mapped[Sentiment | None] = mapped_column(_enum(Sentiment), default=None)
    ai_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    ai_source: Mapped[str | None] = mapped_column(String(80), default=None)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    triage_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attention_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    # --- duplicate detection ---
    possible_duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), default=None)
    duplicate_score: Mapped[float | None] = mapped_column(Float, default=None)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), default=None, index=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    embedder: Mapped[str | None] = mapped_column(String(80), default=None)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    # Internal notes are visible to staff only.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class KBArticle(Base):
    """A help-centre article. It is split into chunks that are embedded for search."""

    __tablename__ = "kb_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(255), default="manual")  # "manual" or "upload:<filename>"
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    chunks: Mapped[list["KBChunk"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        order_by="KBChunk.chunk_index",
    )


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("kb_articles.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # float32 vector stored as raw bytes; `embedder` records which model produced it so that
    # vectors from different models are never compared with each other.
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    embedder: Mapped[str] = mapped_column(String(80), index=True)

    article: Mapped[KBArticle] = relationship(back_populates="chunks")


class SuggestedReply(Base):
    """An AI-drafted reply. Nothing is sent to the customer until an agent accepts it."""

    __tablename__ = "suggested_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")  # [{"article_id","title","score"}]
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    llm: Mapped[str] = mapped_column(String(80))
    status: Mapped[ReplyStatus] = mapped_column(_enum(ReplyStatus), default=ReplyStatus.pending, index=True)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id"), default=None)
    reject_reason: Mapped[str | None] = mapped_column(String(255), default=None)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)
    status: Mapped[ChatStatus] = mapped_column(_enum(ChatStatus), default=ChatStatus.bot, index=True)
    handoff_reason: Mapped[str | None] = mapped_column(String(255), default=None)  # staff-only
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id])
    agent: Mapped[User | None] = relationship(foreign_keys=[agent_id])
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    sender_type: Mapped[SenderType] = mapped_column(_enum(SenderType))
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    body: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")  # knowledge-base articles behind a bot answer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    chat: Mapped[ChatSession] = relationship(back_populates="messages")
    sender: Mapped[User | None] = relationship(foreign_keys=[sender_id])
