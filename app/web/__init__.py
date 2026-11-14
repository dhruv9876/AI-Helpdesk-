"""The dashboard: server-rendered Jinja2 pages enhanced with htmx.

Everything lives on the same origin as the API. Pages never talk JSON to the API from the
browser (except the chat WebSockets); instead each action calls the same Python functions the
API uses, so permission rules and validation exist in exactly one place.
"""
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.database import get_session_factory
from app.core.deps import is_staff
from app.web import auth, chats, kb, overview, tickets, users
from app.web.deps import LoginRequired, render, toast, web_user

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
NOT_UI = ("/api", "/docs", "/redoc", "/openapi.json", "/health")

# No inline scripts or styles anywhere, so the policy can be strict.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _is_ui(request: Request) -> bool:
    return not request.url.path.startswith(NOT_UI)


def _error_page(request: Request, status_code: int, detail: str):
    headline = {403: "You don't have access to this page", 404: "We can't find that page"}.get(status_code, "Something went wrong")
    factory = request.app.dependency_overrides.get(get_session_factory, get_session_factory)()
    with factory() as db:
        try:
            user = web_user(request, db)
        except LoginRequired:
            user = None
        home, label = ("/dashboard", "the overview") if user and is_staff(user) else (("/chat", "chat") if user else ("/login", "sign in"))
        return render(request, "error.html", {"status_code": status_code, "headline": headline, "detail": detail,
                                              "home": home, "home_label": label, "nav": {}}, user=user, status_code=status_code)


def install(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for module in (auth, overview, tickets, chats, kb, users):
        app.include_router(module.router)
    if hasattr(auth, "guarded"):
        app.include_router(auth.guarded)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(NOT_UI):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "same-origin"
            if not request.url.path.startswith("/static"):
                response.headers["Cache-Control"] = "no-store"  # signed-in pages must not be cached
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        accept = request.headers.get("accept", "")
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        if "application/json" in accept and "text/html" not in accept:
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if not _is_ui(request):
            return await http_exception_handler(request, exc)
        if request.headers.get("HX-Request"):
            return toast(str(exc.detail), "error", exc.status_code)
        return _error_page(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not _is_ui(request):
            return await request_validation_exception_handler(request, exc)
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "form"
        message = f"Check the {field} field: {first.get('msg', 'invalid value')}."
        if request.headers.get("HX-Request"):
            return toast(message, "error", 422)
        return _error_page(request, 422, message)
