from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import DB, AdminUser, CurrentUser
from app.core.security import hash_password
from app.models import User, UserRole
from app.schemas import AdminCreateUser, UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def read_me(user: CurrentUser):
    return user


@router.get("", response_model=list[UserOut])
def list_users(_: AdminUser, db: DB, role: UserRole | None = None):
    stmt = select(User).order_by(User.id)
    if role:
        stmt = stmt.where(User.role == role)
    return db.scalars(stmt).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: AdminCreateUser, _: AdminUser, db: DB):
    """Admin-only: create an agent or admin account (or a customer)."""
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, admin: AdminUser, db: DB):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes = payload.model_dump(exclude_unset=True)
    # Stop an admin from locking themselves out.
    if user.id == admin.id and (
        changes.get("is_active") is False
        or ("role" in changes and changes["role"] != UserRole.admin)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or disable yourself")
    for key, value in changes.items():
        setattr(user, key, value)
    db.commit()
    return user
