"""Knowledge-base management. Staff add and edit articles; only admins delete them."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import select

from app.api import kb as kb_api
from app.core.deps import DB
from app.models import KBArticle
from app.schemas import ArticleCreate, ArticleUpdate
from app.services import rag
from app.services.llm import get_llm
from app.web.deps import WebAdmin, WebStaff, csrf_guard, page, redirect, render, toast
from app.web.tickets import _error_text

router = APIRouter(prefix="/dashboard/kb", include_in_schema=False, dependencies=[Depends(csrf_guard)])


@router.get("")
def kb_list(request: Request, user: WebStaff, db: DB, q: str = ""):
    stmt = select(KBArticle).order_by(KBArticle.title)
    if q.strip():
        stmt = stmt.where(KBArticle.title.ilike(f"%{q.strip()}%"))
    articles = db.scalars(stmt).all()
    return page(request, "kb.html", {"articles": articles, "q": q}, user, db)


@router.post("/ask")
def kb_ask(request: Request, user: WebStaff, db: DB, question: str = Form("")):
    """Try the assistant on a question: shows what a customer would get, or that it would hand over."""
    question = question.strip()
    if len(question) < 3:
        return toast("Type a question of at least 3 characters.", "error")
    result = rag.answer_question(db, question, get_llm())
    seen, sources = set(), []
    for hit in result.sources:
        if hit.article_id not in seen:
            seen.add(hit.article_id)
            sources.append(hit)
    return render(request, "partials/_kb_ask.html", {"result": result, "question": question, "sources": sources[:3]}, user=user)


@router.get("/new")
def kb_new(request: Request, user: WebStaff, db: DB):
    return page(request, "kb_form.html", {"article": None}, user, db)


@router.post("")
def kb_create(user: WebStaff, db: DB, title: str = Form(""), content: str = Form("")):
    try:
        article = kb_api.create_article(ArticleCreate(title=title.strip(), content=content.strip()), user, db)
    except (HTTPException, ValidationError) as exc:
        return toast(_error_text(exc), "error")
    return redirect(f"/dashboard/kb/{article.id}")


@router.post("/upload")
def kb_upload(user: WebStaff, db: DB, file: UploadFile = File(...), title: str = Form("")):
    try:
        article = kb_api.upload_article(user, db, file, title.strip() or None)
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return redirect(f"/dashboard/kb/{article.id}")


@router.get("/{article_id}")
def kb_edit(article_id: int, request: Request, user: WebStaff, db: DB):
    article = db.get(KBArticle, article_id)
    if article is None:
        raise HTTPException(404, "Article not found")
    return page(request, "kb_form.html", {"article": article}, user, db)


@router.post("/{article_id}")
def kb_update(article_id: int, user: WebStaff, db: DB, title: str = Form(""), content: str = Form("")):
    try:
        kb_api.update_article(article_id, ArticleUpdate(title=title.strip(), content=content.strip()), user, db)
    except (HTTPException, ValidationError) as exc:
        return toast(_error_text(exc), "error")
    return redirect("/dashboard/kb")


@router.post("/{article_id}/delete")
def kb_delete(article_id: int, user: WebAdmin, db: DB):
    # WebAdmin enforces the role: calling the API function directly skips its own AdminUser check.
    try:
        kb_api.delete_article(article_id, user, db)
    except HTTPException as exc:
        return toast(_error_text(exc), "error")
    return redirect("/dashboard/kb")
