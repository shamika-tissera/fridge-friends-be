from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, col, select

from app.database import get_session
from app.models import GroceryItem
from app.routers.users import get_user_or_404
from app.schemas import (
    ExpiringGroceryItem,
    GroceryItemCreate,
    GroceryItemRead,
    GroceryItemUpdate,
)
from app.services import expiring_items, friend_ids

router = APIRouter(tags=["grocery-items"])


def get_item_or_404(item_id: int, session: Session) -> GroceryItem:
    item = session.get(GroceryItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Grocery item {item_id} not found")
    return item


# ---------- expiry ----------
# Declared before /grocery-items/{item_id} so "expiring" is not read as an id.
@router.get("/grocery-items/expiring", response_model=list[ExpiringGroceryItem])
def list_expiring_items(
    within_days: int = Query(3, ge=0, le=365, description="Look this many days ahead"),
    user_id: int | None = Query(None, description="Restrict to one user's pantry"),
    include_friends: bool = Query(False, description="Also include accepted friends' items"),
    include_expired: bool = True,
    include_consumed: bool = False,
    session: Session = Depends(get_session),
):
    """Everything about to expire, soonest first.

    Without `user_id` this spans every user; with it, just that user (plus
    their accepted friends when `include_friends` is set).
    """
    owner_ids: list[int] | None = None
    if user_id is not None:
        get_user_or_404(user_id, session)
        owner_ids = [user_id]
        if include_friends:
            owner_ids += friend_ids(session, user_id)

    return expiring_items(
        session,
        within_days=within_days,
        owner_ids=owner_ids,
        include_expired=include_expired,
        include_consumed=include_consumed,
    )


@router.get("/users/{user_id}/grocery-items/expiring", response_model=list[ExpiringGroceryItem])
def list_expiring_items_for_user(
    user_id: int,
    within_days: int = Query(3, ge=0, le=365),
    include_friends: bool = False,
    include_expired: bool = True,
    include_consumed: bool = False,
    session: Session = Depends(get_session),
):
    return list_expiring_items(
        within_days=within_days,
        user_id=user_id,
        include_friends=include_friends,
        include_expired=include_expired,
        include_consumed=include_consumed,
        session=session,
    )


# ---------- CRUD ----------
@router.post(
    "/users/{user_id}/grocery-items",
    response_model=GroceryItemRead,
    status_code=status.HTTP_201_CREATED,
)
def create_item(
    user_id: int, payload: GroceryItemCreate, session: Session = Depends(get_session)
):
    get_user_or_404(user_id, session)
    item = GroceryItem(**payload.model_dump(), owner_id=user_id)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.get("/users/{user_id}/grocery-items", response_model=list[GroceryItemRead])
def list_items_for_user(
    user_id: int,
    category: str | None = None,
    consumed: bool | None = None,
    expires_before: date | None = None,
    offset: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    get_user_or_404(user_id, session)
    statement = select(GroceryItem).where(col(GroceryItem.owner_id) == user_id)
    if category is not None:
        statement = statement.where(col(GroceryItem.category) == category)
    if consumed is not None:
        statement = statement.where(col(GroceryItem.consumed) == consumed)
    if expires_before is not None:
        statement = statement.where(col(GroceryItem.expires_on) < expires_before)
    statement = statement.order_by(col(GroceryItem.id).asc()).offset(offset).limit(limit)
    return session.exec(statement).all()


@router.get("/grocery-items/{item_id}", response_model=GroceryItemRead)
def get_item(item_id: int, session: Session = Depends(get_session)):
    return get_item_or_404(item_id, session)


@router.patch("/grocery-items/{item_id}", response_model=GroceryItemRead)
def update_item(
    item_id: int, payload: GroceryItemUpdate, session: Session = Depends(get_session)
):
    item = get_item_or_404(item_id, session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete("/grocery-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, session: Session = Depends(get_session)):
    item = get_item_or_404(item_id, session)
    session.delete(item)
    session.commit()
