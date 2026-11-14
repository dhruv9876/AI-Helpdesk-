from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.core.deps import DB
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User, UserRole
from app.schemas import RegisterIn, Token, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: DB):
    """Public sign-up. Always creates a customer; staff accounts are created by an admin."""
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole.customer,
    )
    db.add(user)
    db.commit()
    return user


def authenticate(db, email: str, password: str) -> User:
    """Check credentials. Shared by the JSON API and the dashboard login form."""
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")
    return user


@router.post("/login", response_model=Token)
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DB):
    """OAuth2 password flow. Send the email in the `username` field."""
    user = authenticate(db, form.username, form.password)
    return Token(access_token=create_access_token(user.id, user.role.value), user=user)
