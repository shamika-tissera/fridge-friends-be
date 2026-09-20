from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, col, select

from app.database import get_session
from app.freshness import expiry_from, profile_for
from app.models import GroceryItem, ItemOutcome
from app.routers.users import get_user_or_404
from app.schemas import (
    ExpiringGroceryItem,
    FreshnessPreview,
    GroceryItemCreate,
    GroceryItemRead,
    GroceryItemUpdate,
    ItemResolution,
    ShelfRead,
    StatsRead,
)
from app.services import (
    expiring_items,
    friend_ids,
    grocery_read,
    resolve_item,
    shelf_for,
    waste_and_spending,
)

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


# ---------- freshness ----------
# Also declared before /grocery-items/{item_id}, for the same reason.
@router.get("/grocery-items/freshness-preview", response_model=FreshnessPreview)
def preview_freshness(
    name: str = Query(min_length=1, max_length=120, description="What the user typed"),
    purchased_on: date | None = Query(None, description="Defaults to today"),
):
    """What the add sheet shows while the user is still typing.

    No database work and no LLM call, so it is safe to call on every keystroke.
    The values it returns are exactly what POST would store for the same input.
    """
    bought = purchased_on or date.today()
    entry = profile_for(name)
    return FreshnessPreview(
        name=name,
        purchased_on=bought,
        shelf_life_days=entry.shelf_life_days,
        spoilage_profile=entry.spoilage_profile,
        shelf_buddy=entry.buddy,
        category=entry.category,
        expires_on=expiry_from(bought, entry.shelf_life_days),
        summary=f"{entry.spoilage_profile.value.title()} \u00b7 ~{entry.shelf_life_days} days",
    )


@router.get("/users/{user_id}/shelf", response_model=ShelfRead)
def get_shelf(
    user_id: int,
    include_consumed: bool = False,
    session: Session = Depends(get_session),
):
    """The home screen: every item with its freshness chip, plus the rescue counts."""
    get_user_or_404(user_id, session)
    return shelf_for(session, user_id, include_consumed=include_consumed)


@router.get("/users/{user_id}/stats", response_model=StatsRead)
def get_stats(
    user_id: int,
    period: str = Query("weeks", pattern="^(weeks|months)$"),
    buckets: int = Query(6, ge=1, le=24),
    session: Session = Depends(get_session),
):
    """Waste and spending: the three totals, the bars, and the trend sentence."""
    get_user_or_404(user_id, session)
    return waste_and_spending(session, user_id, period=period, buckets=buckets)


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
    fields = payload.model_dump()
    # An omitted expires_on is filled in from the catalogue; an explicit null
    # means "this one has no timer" (salt, sugar) and is left alone.
    sent = payload.model_dump(exclude_unset=True)

    # "Its freshness timer starts today": the client may send a date bought,
    # but if it does not, today is what the sheet is showing the user.
    fields["purchased_on"] = fields.get("purchased_on") or date.today()

    # Shelf life, spoilage profile and buddy come from the catalogue unless the
    # client sent its own. An explicit expires_on always wins: a date the user
    # read off the packet beats anything we can infer from the name.
    entry = profile_for(payload.name)
    if fields.get("shelf_life_days") is None:
        fields["shelf_life_days"] = entry.shelf_life_days
    if fields.get("spoilage_profile") is None:
        fields["spoilage_profile"] = entry.spoilage_profile
    if not fields.get("shelf_buddy"):
        fields["shelf_buddy"] = entry.buddy
    if fields.get("expires_on") is None and "expires_on" not in sent:
        fields["expires_on"] = expiry_from(
            fields["purchased_on"], fields["shelf_life_days"]
        )
    if fields.get("category") is None:
        fields["category"] = entry.category

    item = GroceryItem(**fields, owner_id=user_id)
    session.add(item)
    session.commit()
    session.refresh(item)
    return grocery_read(item)


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
    return [grocery_read(item) for item in session.exec(statement).all()]


@router.get("/grocery-items/{item_id}", response_model=GroceryItemRead)
def get_item(item_id: int, session: Session = Depends(get_session)):
    return grocery_read(get_item_or_404(item_id, session))


@router.patch("/grocery-items/{item_id}", response_model=GroceryItemRead)
def update_item(
    item_id: int, payload: GroceryItemUpdate, session: Session = Depends(get_session)
):
    item = get_item_or_404(item_id, session)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(item, field, value)

    # `consumed` predates `outcome` and plenty of callers still send it. Keep
    # the two in step: consuming without saying how counts as eaten, and
    # putting something back on the shelf clears the record of its end.
    if "consumed" in changes:
        if changes["consumed"] and item.outcome == ItemOutcome.on_shelf:
            resolve_item(item, ItemOutcome.used)
        elif not changes["consumed"]:
            item.outcome = ItemOutcome.on_shelf
            item.resolved_on = None
            item.rescued = False

    # Moving the purchase date or the shelf life moves the expiry with it,
    # unless the same call set an expiry explicitly.
    if "expires_on" not in changes and (
        "purchased_on" in changes or "shelf_life_days" in changes
    ):
        if item.purchased_on is not None and item.shelf_life_days is not None:
            item.expires_on = expiry_from(item.purchased_on, item.shelf_life_days)

    session.add(item)
    session.commit()
    session.refresh(item)
    return grocery_read(item)


@router.post("/grocery-items/{item_id}/resolve", response_model=GroceryItemRead)
def resolve(
    item_id: int, payload: ItemResolution, session: Session = Depends(get_session)
):
    """Take an item off the shelf as eaten or as binned.

    This is what feeds the waste-and-spending screen, and it is why the shelf
    does not just delete things: a deleted row cannot be counted as waste.
    Using something that was already in the red is recorded as a rescue.
    """
    item = get_item_or_404(item_id, session)
    resolve_item(item, payload.outcome, on=payload.resolved_on)
    session.add(item)
    session.commit()
    session.refresh(item)
    return grocery_read(item)


@router.delete("/grocery-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, session: Session = Depends(get_session)):
    item = get_item_or_404(item_id, session)
    session.delete(item)
    session.commit()
