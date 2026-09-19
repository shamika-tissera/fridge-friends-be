from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import get_session
from app.models import User
from app.schemas import UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


def get_user_or_404(user_id: int, session: Session) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")
    return user


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, session: Session = Depends(get_session)):
    user = User(email=payload.email, name=payload.name)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    session.refresh(user)
    return user


@router.get("", response_model=list[UserRead])
def list_users(
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    return session.exec(select(User).offset(offset).limit(limit)).all()


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, session: Session = Depends(get_session)):
    return get_user_or_404(user_id, session)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int, payload: UserUpdate, session: Session = Depends(get_session)
):
    user = get_user_or_404(user_id, session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    session.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_session)):
    user = get_user_or_404(user_id, session)
    session.delete(user)
    session.commit()
