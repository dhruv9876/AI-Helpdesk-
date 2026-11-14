"""Sign in / sign out, and the two small JSON endpoints the page scripts use."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.api.auth import authenticate
from app.core.config import settings
from app.core.deps import DB, is_staff
from app.core.security import create_access_token
from app.models import User
from app.services import metrics
from app.web.deps import COOKIE, LoginRequired, WebStaff, WebUser, csrf_guard, render, web_user

router = APIRouter(include_in_schema=False)
guarded = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_guard)])


def home_for(user: User) -> str:
    return "/dashboard" if is_staff(user) else "/chat"


def safe_next(value: str | None) -> str | None:
    """Only same-site paths: blocks open redirects such as //evil.example or /\\evil.example."""
    if value and value.startswith("/") and not value.startswith(("//", "/login")) and "\\" not in value:
        return value
    return None


@router.get("/")
def index(request: Request, db: DB):
    try:
        return RedirectResponse(home_for(web_user(request, db)), status_code=303)
    except LoginRequired:
        return RedirectResponse("/login", status_code=303)


@router.get("/login")
def login_form(request: Request, db: DB, next: str | None = None):
    try:
        return RedirectResponse(home_for(web_user(request, db)), status_code=303)
    except LoginRequired:
        return render(request, "login.html", {"next": safe_next(next), "demo": settings.show_demo_logins})


@router.post("/login")
def login_submit(request: Request, db: DB, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    try:
        user = authenticate(db, email, password)
    except HTTPException as exc:
        return render(request, "login.html", {"error": exc.detail, "email": email, "next": safe_next(next),
                                              "demo": settings.show_demo_logins}, status_code=exc.status_code)
    response = RedirectResponse(safe_next(next) or home_for(user), status_code=303)
    response.set_cookie(
        COOKIE, create_access_token(user.id, user.role.value),
        max_age=settings.access_token_expire_minutes * 60, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return response


@guarded.post("/logout")
def logout():
    response = Response(status_code=200, headers={"HX-Redirect": "/login"})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/ws-token")
def ws_token(user: WebUser):
    """A 2-minute token for the WebSocket auth frame. The session cookie is HttpOnly, so page
    scripts cannot read it; they ask for this short-lived token instead."""
    return JSONResponse({"token": create_access_token(user.id, user.role.value, minutes=2)},
                        headers={"Cache-Control": "no-store"})


@router.get("/dashboard/nav-counts")
def nav_counts(user: WebStaff, db: DB):
    counts = metrics.ticket_counts(db)
    return JSONResponse({"attention": counts["attention"], "waiting_chats": metrics.waiting_chat_count(db)},
                        headers={"Cache-Control": "no-store"})
