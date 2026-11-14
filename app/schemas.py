from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import ChatStatus, ReplyStatus, Sentiment, SenderType, TicketPriority, TicketStatus, UserRole


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- users / auth ----------
class RegisterIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=64)


class AdminCreateUser(RegisterIn):
    role: UserRole = UserRole.agent


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: UserRole | None = None
    is_active: bool | None = None


class UserOut(ORMModel):
    id: int
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- tickets ----------
class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=1, max_length=5000)


class TicketUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=5000)
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: str | None = Field(default=None, max_length=50)
    assignee_id: int | None = None  # explicit null means "unassign"

    @field_validator("subject", "description", "status", "priority")
    @classmethod
    def not_null(cls, v):
        # These columns are required; only category/assignee_id may be cleared.
        if v is None:
            raise ValueError("This field cannot be null")
        return v


class AIInfo(BaseModel):
    """AI triage results. Only included for staff; customers never see these fields."""

    category: str | None
    priority: TicketPriority | None
    sentiment: Sentiment | None
    confidence: float | None
    source: str | None
    triaged_at: datetime | None
    needs_attention: bool
    attention_reason: str | None
    possible_duplicate_of_id: int | None
    duplicate_score: float | None


class TicketOut(ORMModel):
    id: int
    subject: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    category: str | None
    customer_id: int
    assignee_id: int | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    merged_into_id: int | None = None
    ai: AIInfo | None = None  # staff only


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    is_internal: bool = False


class CommentOut(ORMModel):
    id: int
    ticket_id: int
    author_id: int
    body: str
    is_internal: bool
    created_at: datetime


class TicketDetail(TicketOut):
    comments: list[CommentOut]


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int
    limit: int
    offset: int


# ---------- knowledge base ----------
class ArticleCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    content: str = Field(min_length=1, max_length=50000)


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=50000)

    @field_validator("title", "content")
    @classmethod
    def not_null(cls, v):
        if v is None:
            raise ValueError("This field cannot be null")
        return v


class ArticleSummary(ORMModel):
    id: int
    title: str
    source: str
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ArticleDetail(ArticleSummary):
    content: str


class SearchHit(BaseModel):
    article_id: int
    chunk_id: int
    title: str
    text: str
    score: float


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class AskOut(BaseModel):
    answer: str | None  # None when the bot could not answer
    escalate: bool  # True means "hand over to a human agent"
    confidence: float  # best retrieval score (0 to 1)
    sources: list[SearchHit]
    llm: str


# ---------- AI workflow ----------
class DuplicateOut(BaseModel):
    ticket_id: int
    subject: str
    status: TicketStatus
    customer_id: int
    score: float
    same_customer: bool
    can_merge: bool  # merging is only allowed between tickets from the same customer


class MergeIn(BaseModel):
    target_id: int


class ReplySource(BaseModel):
    article_id: int
    title: str
    score: float


class SuggestedReplyOut(BaseModel):
    id: int
    ticket_id: int
    body: str
    sources: list[ReplySource]
    confidence: float
    llm: str
    status: ReplyStatus
    created_at: datetime
    resolved_at: datetime | None
    comment_id: int | None
    reject_reason: str | None


class SuggestReplyOut(BaseModel):
    draft: SuggestedReplyOut | None  # None when the knowledge base can't answer
    escalate: bool
    confidence: float
    reason: str | None


class AcceptReplyIn(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=5000)  # omit to post the draft as-is


class RejectReplyIn(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class AIStats(BaseModel):
    tickets_total: int
    tickets_triaged: int
    needs_attention: int
    possible_duplicates: int
    replies_suggested: int
    replies_accepted: int
    replies_edited: int
    replies_rejected: int
    replies_pending: int
    acceptance_rate: float | None  # (accepted + edited) / decided; None until an agent decides
    chats_total: int = 0
    chats_handed_off: int = 0
    handoff_rate: float | None = None  # share of chats the bot could not finish on its own


# ---------- live chat ----------
class SendMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class ChatMessageOut(BaseModel):
    id: int
    chat_id: int
    sender_type: SenderType
    sender_id: int | None
    sender_name: str
    body: str
    sources: list[ReplySource]
    created_at: datetime


class ChatOut(BaseModel):
    id: int
    status: ChatStatus
    customer_id: int
    customer_name: str
    agent_id: int | None
    agent_name: str | None
    handoff_reason: str | None  # staff only
    waiting_since: datetime | None = None  # staff only: when it was handed off
    ticket_id: int | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    last_message: str | None = None


class ChatDetail(ChatOut):
    messages: list[ChatMessageOut]
