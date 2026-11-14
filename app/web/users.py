"""Team management (admins only)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select

from app.api import users as users_api
from app.core.deps import DB
from app.models import User, UserRole
from app.schemas import AdminCreateUser, UserUpdate
from app.web.deps import WebAdmin, csrf_guard, page, render, toast, toast_header
from app.web.tickets import _error_text

router = APIRouter(prefix="/dashboard/users", include_in_schema=False, dependencies=[Depends(csrf_guard)])

def _table_ctx(db) -> dict:
    people = db.scalars(select(User).order_by(User.role, User.full_name)).all()
    return {"people": people, "roles": [r.value for r in UserRole]}


@router.get("")
def users_page(request: Request, user: WebAdmin, db: DB):
    return page(request, "users.html", _table_ctx(db), user, db)


@router.post("")
def create(request: Request, user: WebAdmin, db: DB, full_name: str = Form(""), email: str = Form(""),
           password: str = Form(""), role: str = Form("agent")):
    try:
        users_api.create_user(AdminCreateUser(email=email.strip(), full_name=full_name.strip(), password=password, role=role), user, db)
    except (HTTPException, ValidationError) as exc:
        db.rollback()
        return toast(_error_text(exc), "error")
    return render(request, "partials/_users_created.html", _table_ctx(db), user=user, headers=toast_header("Account created."))


@router.post("/{user_id}")
def update(user_id: int, request: Request, user: WebAdmin, db: DB, role: str | None = Form(None), active: str | None = Form(None)):
    changes = {}
    if role is not None:
        changes["role"] = role
    if active is not None:
        changes["is_active"] = active == "1"
    try:
        users_api.update_user(user_id, UserUpdate(**changes), user, db)
    except (HTTPException, ValidationError) as exc:
        db.rollback()
        # Re-render so a rejected change snaps back to what is saved.
        return render(request, "partials/_users_table.html", _table_ctx(db), user=user, headers=toast_header(_error_text(exc), "error"))
    return render(request, "partials/_users_table.html", _table_ctx(db), user=user, headers=toast_header("Account updated."))
