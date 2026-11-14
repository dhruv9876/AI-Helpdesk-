"""Shared plumbing for the dashboard: sessions, CSRF, templates, HTMX helpers.

Sessions: the JWT lives in an HttpOnly, SameSite=Lax cookie, so page scripts cannot read it.
CSRF: every state-changing request must carry an `X-CSRF-Token` header (htmx adds it from <body>).
The token is an HMAC of the session cookie, so it needs no server-side storage and is useless
without the matching session. Login is the only unauthenticated POST.
"""
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.core.config import settings
from app.core.deps import DB, is_staff
from app.core.security import decode_access_token
from app.models import User, UserRole
from app.services import metrics

COOKIE = "session"
WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class LoginRequired(Exception):
    """Raised when a page needs a signed-in user. Turned into a redirect by the app."""


# ---------------------------------------------------------------------------------- sessions
def csrf_for(session_token: str) -> str:
    return hmac.new(settings.secret_key.encode(), session_token.encode(), hashlib.sha256).hexdigest()


def web_user(request: Request, db: DB) -> User:
    token = request.cookies.get(COOKIE)
    payload = decode_access_token(token) if token else None
    try:
        user = db.get(User, int(payload["sub"])) if payload else None
    except (KeyError, ValueError):
        user = None
    if user is None or not user.is_active:
        raise LoginRequired()
    return user


def web_staff(user: Annotated[User, Depends(web_user)]) -> User:
    if not is_staff(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is for support staff.")
    return user


def web_customer(user: Annotated[User, Depends(web_user)]) -> User:
    if is_staff(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is for customers. Staff use the dashboard.")
    return user


def web_admin(user: Annotated[User, Depends(web_user)]) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only administrators can open this page.")
    return user


async def csrf_guard(request: Request) -> None:
    """Router-level dependency: reject state-changing requests without a valid CSRF header."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.cookies.get(COOKIE)
    if not token:
        raise LoginRequired()
    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_for(token)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your session has changed. Reload the page and try again.")


WebUser = Annotated[User, Depends(web_user)]
WebStaff = Annotated[User, Depends(web_staff)]
WebAdmin = Annotated[User, Depends(web_admin)]
WebCustomer = Annotated[User, Depends(web_customer)]


# ---------------------------------------------------------------------------------- rendering
def timeago(value: datetime | None) -> str:
    if value is None:
        return "never"
    seconds = max(0, int((metrics.now_utc() - metrics.naive_utc(value)).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def ago(value: datetime | None) -> str:
    """'3 h ago', or 'just now' (never 'just now ago')."""
    phrase = timeago(value)
    return phrase if phrase in ("just now", "never") else f"{phrase} ago"


templates.env.filters["timeago"] = timeago
templates.env.filters["ago"] = ago
templates.env.filters["val"] = lambda x: getattr(x, "value", x)  # enum -> plain string
templates.env.filters["hours"] = metrics.format_hours
templates.env.filters["stamp"] = lambda v: metrics.naive_utc(v).strftime("%d %b %Y, %H:%M UTC") if v else ""


def render(request: Request, name: str, context: dict | None = None, *, user: User | None = None,
           status_code: int = 200, headers: dict | None = None) -> HTMLResponse:
    token = request.cookies.get(COOKIE, "")
    ctx = {"user": user, "csrf": csrf_for(token) if (user and token) else ""}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)


def page(request: Request, name: str, context: dict, user: User, db) -> HTMLResponse:
    """A full page: adds the live counts shown in the top bar."""
    nav = {}
    if is_staff(user):
        counts = metrics.ticket_counts(db)
        nav = {"attention": counts["attention"], "waiting_chats": metrics.waiting_chat_count(db)}
    return render(request, name, {**context, "nav": nav}, user=user)


def is_htmx(request: Request) -> bool:
    """True for htmx requests that want a fragment (history restores want the whole page)."""
    return bool(request.headers.get("HX-Request")) and not request.headers.get("HX-History-Restore-Request")


def toast_header(message: str, level: str = "success") -> dict:
    return {"HX-Trigger": json.dumps({"toast": {"message": message, "level": level}})}


def toast(message: str, level: str = "error", status_code: int = 200, also: dict | None = None) -> Response:
    """An htmx response that swaps nothing and just shows a message. `also` adds more
    events for the page to react to (e.g. {"chat-queue": {}} to refresh a list)."""
    headers = {"HX-Trigger": json.dumps({"toast": {"message": message, "level": level}, **(also or {})})}
    return Response(status_code=status_code, headers=headers)


def redirect(url: str, message: str | None = None) -> Response:
    headers = {"HX-Redirect": url}
    return Response(status_code=200, headers=headers)
