from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.database import get_session
from app.models import User
from app.schemas import (
    PasswordUpdate,
    PushTokenCreate,
    TasteProfileRead,
    TasteProfileUpdate,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.security import hash_password, verify_password

router = APIRouter(prefix="/users", tags=["users"])


def get_user_or_404(user_id: int, session: Session) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")
    return user


def get_by_username(session: Session, username: str) -> User | None:
    return session.exec(
        select(User).where(col(User.username) == username.strip().lower())
    ).first()


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, session: Session = Depends(get_session)):
    """Sign up: User ID, password and a buddy. Also served as POST /auth/signup."""
    user = User(
        username=payload.username,
        email=payload.email,
        # The join screen has no display-name field, so the User ID stands in
        # until the profile screen sets one.
        name=payload.name or payload.username,
        buddy=payload.buddy,
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Two unique columns can trip this; tell the user which one, since
        # "already taken" on the User ID field is the one they can act on.
        taken = "User ID" if get_by_username(session, payload.username) else "Email"
        raise HTTPException(status.HTTP_409_CONFLICT, f"{taken} already registered")
    session.refresh(user)
    return user


@router.get("", response_model=list[UserRead])
def list_users(
    username: str | None = Query(None, description="Exact User ID lookup, for adding friends"),
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    statement = select(User)
    if username is not None:
        statement = statement.where(col(User.username) == username.strip().lower())
    return session.exec(statement.offset(offset).limit(limit)).all()


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, session: Session = Depends(get_session)):
    return get_user_or_404(user_id, session)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int, payload: UserUpdate, session: Session = Depends(get_session)
):
    user = get_user_or_404(user_id, session)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(user, field, value)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Both columns are unique, so name the one the user can act on.
        taken = (
            "User ID"
            if "username" in changes and get_by_username(session, changes["username"])
            else "Email"
        )
        raise HTTPException(status.HTTP_409_CONFLICT, f"{taken} already registered")
    session.refresh(user)
    return user


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    user_id: int, payload: PasswordUpdate, session: Session = Depends(get_session)
):
    user = get_user_or_404(user_id, session)
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    session.add(user)
    session.commit()


# ---------- taste profile ("Let\'s get to know you") ----------
@router.get("/{user_id}/taste-profile", response_model=TasteProfileRead)
def get_taste_profile(user_id: int, session: Session = Depends(get_session)):
    user = get_user_or_404(user_id, session)
    return TasteProfileRead(
        user_id=user.id,
        diets=user.diets,
        avoid_allergens=user.avoid_allergens,
        favorite_cuisines=user.favorite_cuisines,
    )


@router.put("/{user_id}/taste-profile", response_model=TasteProfileRead)
def set_taste_profile(
    user_id: int, payload: TasteProfileUpdate, session: Session = Depends(get_session)
):
    """Save the diet, allergen and cuisine chips. Each list replaces the old one.

    Allergens saved here are enforced on every recipe suggestion, for this user
    alone and for any feast they are part of.
    """
    user = get_user_or_404(user_id, session)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "diets" in data:
        user.diets = [d.value if hasattr(d, "value") else d for d in data["diets"]]
    if "avoid_allergens" in data:
        user.avoid_allergens = [
            a.value if hasattr(a, "value") else a for a in data["avoid_allergens"]
        ]
    if "favorite_cuisines" in data:
        user.favorite_cuisines = list(data["favorite_cuisines"])
    session.add(user)
    session.commit()
    session.refresh(user)
    return TasteProfileRead(
        user_id=user.id,
        diets=user.diets,
        avoid_allergens=user.avoid_allergens,
        favorite_cuisines=user.favorite_cuisines,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_session)):
    user = get_user_or_404(user_id, session)
    session.delete(user)
    session.commit()

@router.post("/{user_id}/push-token", status_code=status.HTTP_204_NO_CONTENT)
def register_push_token(
    user_id: int, payload: PushTokenCreate, session: Session = Depends(get_session)
):
    user = get_user_or_404(user_id, session)
    user.expo_push_token = payload.expo_push_token
    session.add(user)
    session.commit()

