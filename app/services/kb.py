"""Knowledge-base ingestion and retrieval."""
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import KBArticle, KBChunk
from app.services.chunking import chunk_text
from app.services.embeddings import get_embedder


@dataclass
class Hit:
    chunk_id: int
    article_id: int
    title: str
    text: str
    score: float


def _build_chunks(article: KBArticle) -> list[KBChunk]:
    embedder = get_embedder()
    texts = chunk_text(article.content, settings.chunk_max_chars)
    # The title is embedded together with the text so title words help retrieval,
    # but only the plain text is stored/shown.
    vectors = embedder.embed([f"{article.title}\n{t}" for t in texts])
    return [
        KBChunk(
            chunk_index=i,
            text=text,
            embedding=vectors[i].astype(np.float32).tobytes(),
            embedder=embedder.name,
        )
        for i, text in enumerate(texts)
    ]


def create_article(db: Session, title: str, content: str, source: str, user_id: int | None) -> KBArticle:
    article = KBArticle(title=title, content=content, source=source, created_by_id=user_id)
    article.chunks = _build_chunks(article)
    db.add(article)
    db.commit()
    return article


def update_article(db: Session, article: KBArticle, title: str | None, content: str | None) -> KBArticle:
    if title is not None:
        article.title = title
    if content is not None:
        article.content = content
    article.chunks = _build_chunks(article)  # old chunks are deleted via delete-orphan
    db.commit()
    return article


def reindex_all(db: Session) -> int:
    """Re-embed every article, e.g. after switching EMBEDDING_BACKEND."""
    articles = db.scalars(select(KBArticle)).all()
    for article in articles:
        article.chunks = _build_chunks(article)
    db.commit()
    return len(articles)


def search(db: Session, query: str, k: int | None = None) -> list[Hit]:
    """Return the top-k chunks by cosine similarity (vectors are normalised, so dot product)."""
    k = k or settings.kb_top_k
    embedder = get_embedder()
    rows = db.execute(
        select(KBChunk, KBArticle.title)
        .join(KBArticle, KBArticle.id == KBChunk.article_id)
        .where(KBChunk.embedder == embedder.name)  # never compare vectors from different models
    ).all()
    if not rows or not query.strip():
        return []

    matrix = np.vstack([np.frombuffer(chunk.embedding, dtype=np.float32) for chunk, _ in rows])
    scores = matrix @ embedder.embed([query])[0]
    best = np.argsort(scores)[::-1][:k]
    return [
        Hit(
            chunk_id=rows[i][0].id,
            article_id=rows[i][0].article_id,
            title=rows[i][1],
            text=rows[i][0].text,
            score=round(float(scores[i]), 4),
        )
        for i in best
    ]
