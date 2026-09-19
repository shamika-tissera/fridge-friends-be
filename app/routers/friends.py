from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, col, or_, select

from app.database import get_session
from app.models import Friend, FriendStatus, User
from app.routers.users import get_user_or_404
from app.schemas import FriendCreate, FriendRead, FriendUpdate, FriendWithUser

router = APIRouter(prefix="/users/{user_id}/friends", tags=["friends"])


def find_friendship(session: Session, a: int, b: int) -> Friend | None:
    return session.exec(
        select(Friend).where(
            or_(
                (col(Friend.user_id) == a) & (col(Friend.friend_id) == b),
                (col(Friend.user_id) == b) & (col(Friend.friend_id) == a),
            )
        )
    ).first()


@router.post("", response_model=FriendRead, status_code=status.HTTP_201_CREATED)
def add_friend(
    user_id: int, payload: FriendCreate, session: Session = Depends(get_session)
):
    """Request a friendship. The pair is stored once, starting as `pending`."""
    if user_id == payload.friend_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot befriend yourself")
    get_user_or_404(user_id, session)
    get_user_or_404(payload.friend_id, session)

    if find_friendship(session, user_id, payload.friend_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Friendship already exists")

    friendship = Friend(user_id=user_id, friend_id=payload.friend_id)
    session.add(friendship)
    session.commit()
    session.refresh(friendship)
    return friendship


@router.get("", response_model=list[FriendWithUser])
def list_friends(
    user_id: int,
    status_filter: FriendStatus | None = Query(None, alias="status"),
    session: Session = Depends(get_session),
):
    get_user_or_404(user_id, session)
    statement = select(Friend).where(
        or_(col(Friend.user_id) == user_id, col(Friend.friend_id) == user_id)
    )
    if status_filter is not None:
        statement = statement.where(col(Friend.status) == status_filter)

    out = []
    for row in session.exec(statement).all():
        other_id = row.friend_id if row.user_id == user_id else row.user_id
        other = session.get(User, other_id)
        out.append(FriendWithUser(**row.model_dump(), friend=other))
    return out


@router.patch("/{friend_id}", response_model=FriendRead)
def update_friendship(
    user_id: int,
    friend_id: int,
    payload: FriendUpdate,
    session: Session = Depends(get_session),
):
    """Accept/block a friendship (this is how a request becomes `accepted`)."""
    friendship = find_friendship(session, user_id, friend_id)
    if friendship is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Friendship not found")
    friendship.status = payload.status
    session.add(friendship)
    session.commit()
    session.refresh(friendship)
    return friendship


@router.delete("/{friend_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_friend(user_id: int, friend_id: int, session: Session = Depends(get_session)):
    friendship = find_friendship(session, user_id, friend_id)
    if friendship is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Friendship not found")
    session.delete(friendship)
    session.commit()
