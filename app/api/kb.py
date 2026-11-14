from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select

from app.core.deps import DB, AdminUser, CurrentUser, StaffUser
from app.models import KBArticle
from app.schemas import (
    ArticleCreate,
    ArticleDetail,
    ArticleSummary,
    ArticleUpdate,
    AskIn,
    AskOut,
    SearchHit,
)
from app.services import kb, rag

router = APIRouter(prefix="/kb", tags=["knowledge base"])

MAX_UPLOAD_BYTES = 1_000_000


def _summary(a: KBArticle) -> ArticleSummary:
    return ArticleSummary(
        id=a.id, title=a.title, source=a.source, chunk_count=len(a.chunks),
        created_at=a.created_at, updated_at=a.updated_at,
    )


def _detail(a: KBArticle) -> ArticleDetail:
    return ArticleDetail(**_summary(a).model_dump(), content=a.content)


def _get_article(db, article_id: int) -> KBArticle:
    article = db.get(KBArticle, article_id)
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    return article


def _to_hits(hits) -> list[SearchHit]:
    return [
        SearchHit(article_id=h.article_id, chunk_id=h.chunk_id, title=h.title, text=h.text, score=h.score)
        for h in hits
    ]


# ---------- ingestion (staff) ----------
@router.post("/articles", response_model=ArticleDetail, status_code=status.HTTP_201_CREATED)
def create_article(payload: ArticleCreate, user: StaffUser, db: DB):
    return _detail(kb.create_article(db, payload.title, payload.content, "manual", user.id))


@router.post("/upload", response_model=ArticleDetail, status_code=status.HTTP_201_CREATED)
def upload_article(
    user: StaffUser,
    db: DB,
    file: UploadFile = File(..., description=".md or .txt file"),
    title: str | None = Form(default=None, description="Defaults to the file name"),
):
    filename = file.filename or "upload.txt"
    if not filename.lower().endswith((".md", ".txt")):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only .md and .txt files are supported")
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File is larger than 1 MB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File must be UTF-8 text")
    if not content.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty")
    name = (title or filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")).strip()[:200]
    if len(name) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Title must be at least 3 characters")
    return _detail(kb.create_article(db, name, content, f"upload:{filename}", user.id))


@router.put("/articles/{article_id}", response_model=ArticleDetail)
def update_article(article_id: int, payload: ArticleUpdate, _: StaffUser, db: DB):
    article = _get_article(db, article_id)
    return _detail(kb.update_article(db, article, payload.title, payload.content))


@router.delete("/articles/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_article(article_id: int, _: AdminUser, db: DB):
    db.delete(_get_article(db, article_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reindex")
def reindex(_: AdminUser, db: DB):
    """Re-embed all articles (needed after changing EMBEDDING_BACKEND)."""
    return {"reindexed_articles": kb.reindex_all(db)}


# ---------- reading and asking (any signed-in user) ----------
@router.get("/articles", response_model=list[ArticleSummary])
def list_articles(_: CurrentUser, db: DB, q: str | None = None):
    stmt = select(KBArticle).order_by(KBArticle.title)
    if q:
        stmt = stmt.where(KBArticle.title.ilike(f"%{q}%"))
    return [_summary(a) for a in db.scalars(stmt).all()]


@router.get("/articles/{article_id}", response_model=ArticleDetail)
def get_article(article_id: int, _: CurrentUser, db: DB):
    return _detail(_get_article(db, article_id))


@router.get("/search", response_model=list[SearchHit])
def search(_: CurrentUser, db: DB, q: str = Query(min_length=2, max_length=300), k: int = Query(default=4, ge=1, le=10)):
    """Raw retrieval: the best-matching passages with their similarity scores."""
    return _to_hits(kb.search(db, q, k))


@router.post("/ask", response_model=AskOut)
def ask(payload: AskIn, _: CurrentUser, db: DB):
    """RAG answer. If the bot isn't confident, `escalate` is true and `answer` is null."""
    result = rag.answer_question(db, payload.question)
    return AskOut(
        answer=result.answer,
        escalate=result.escalate,
        confidence=result.confidence,
        sources=_to_hits(result.sources),
        llm=result.llm,
    )
