from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.database import get_db, get_session_factory
from app.core.security import decode_access_token
from app.models import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

DB = Annotated[Session, Depends(get_db)]
SessionFactory = Annotated[Callable[[], Session], Depends(get_session_factory)]


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: DB) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_error
    try:
        user = db.get(User, int(payload["sub"]))
    except (KeyError, ValueError):
        raise credentials_error
    if user is None or not user.is_active:
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Dependency factory: allow only the listed roles."""

    def checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not enough permissions")
        return user

    return checker


StaffUser = Annotated[User, Depends(require_roles(UserRole.agent, UserRole.admin))]
AdminUser = Annotated[User, Depends(require_roles(UserRole.admin))]


def is_staff(user: User) -> bool:
    return user.role in (UserRole.agent, UserRole.admin)
