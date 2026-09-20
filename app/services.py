from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

from sqlmodel import Session, col, or_, select

from app.models import (
    Feast,
    FeastAttendee,
    FoodPreference,
    InviteResponse,
    Notification,
    Friend,
    FriendStatus,
    GroceryItem,
    Preference,
    User,
)
from app.schemas import ExpiringGroceryItem


def friend_ids(session: Session, user_id: int) -> list[int]:
    """Ids of users with an *accepted* friendship with `user_id`, either direction."""
    rows = session.exec(
        select(Friend).where(
            Friend.status == FriendStatus.accepted,
            or_(Friend.user_id == user_id, Friend.friend_id == user_id),
        )
    ).all()
    return [r.friend_id if r.user_id == user_id else r.user_id for r in rows]


def expiring_items(
    session: Session,
    *,
    within_days: int = 3,
    owner_ids: list[int] | None = None,
    include_expired: bool = True,
    include_consumed: bool = False,
    today: date | None = None,
) -> list[ExpiringGroceryItem]:
    """Items whose expiry falls inside the next `within_days` days.

    Sorted soonest-first so the caller can show "use this up next".
    """
    today = today or date.today()
    cutoff = today + timedelta(days=within_days)

    statement = (
        select(GroceryItem, User)
        .join(User, col(GroceryItem.owner_id) == col(User.id))
        .where(col(GroceryItem.expires_on).is_not(None))
        .where(col(GroceryItem.expires_on) <= cutoff)
    )
    if owner_ids is not None:
        if not owner_ids:
            return []
        statement = statement.where(col(GroceryItem.owner_id).in_(owner_ids))
    if not include_expired:
        statement = statement.where(col(GroceryItem.expires_on) >= today)
    if not include_consumed:
        statement = statement.where(col(GroceryItem.consumed) == False)  # noqa: E712

    statement = statement.order_by(col(GroceryItem.expires_on).asc(), col(GroceryItem.id).asc())

    results = []
    for item, owner in session.exec(statement).all():
        delta = (item.expires_on - today).days
        results.append(
            ExpiringGroceryItem(
                **item.model_dump(),
                owner_name=owner.name,
                days_until_expiry=delta,
                expired=delta < 0,
            )
        )
    return results


def normalise(name: str) -> str:
    return " ".join(name.split()).strip().lower()


class PantryEntry(NamedTuple):
    """One ingredient available to the group, and who actually has it."""

    name: str
    owners: list[str]        # display names, in the order the users were given
    expiring: bool


def pantry_for(
    session: Session, user_ids: list[int], *, expiring_within_days: int = 4
) -> dict[str, PantryEntry]:
    """Everything the given users can cook with, keyed by normalised name.

    The same ingredient held by two people collapses to one entry listing both
    owners — that is what lets a suggestion say whose fridge each item is in.

    Consumed items are excluded, and so is anything already past its expiry:
    suggesting a recipe built on food that has gone off is worse than
    suggesting nothing.
    """
    today = date.today()
    rows = session.exec(
        select(GroceryItem, User)
        .join(User, col(GroceryItem.owner_id) == col(User.id))
        .where(col(GroceryItem.owner_id).in_(user_ids))
        .where(col(GroceryItem.consumed) == False)  # noqa: E712
    ).all()

    pantry: dict[str, PantryEntry] = {}
    for item, owner in rows:
        if item.expires_on is not None and item.expires_on < today:
            continue  # already expired
        expiring = (
            item.expires_on is not None
            and (item.expires_on - today).days <= expiring_within_days
        )
        key = normalise(item.name)
        existing = pantry.get(key)
        if existing is None:
            pantry[key] = PantryEntry(item.name, [owner.name], expiring)
        else:
            owners = existing.owners
            if owner.name not in owners:
                owners.append(owner.name)
            # One person's stock expiring soon is reason enough to prioritise it.
            pantry[key] = existing._replace(expiring=existing.expiring or expiring)
    return pantry


def preferences_for(session: Session, user_ids: list[int]) -> tuple[list[str], list[str]]:
    """(liked dishes, disliked dishes) pooled across the given users.

    A dish disliked by *anyone* in the group is disliked for the group — one
    person's dislike outranks another's like, since the meal is shared.
    """
    rows = session.exec(
        select(FoodPreference).where(col(FoodPreference.user_id).in_(user_ids))
    ).all()
    liked = {normalise(r.name): r.name for r in rows if r.preference == Preference.like}
    disliked = {normalise(r.name): r.name for r in rows if r.preference == Preference.dislike}
    for key in disliked:
        liked.pop(key, None)
    return list(liked.values()), list(disliked.values())


def create_feast(
    session: Session,
    *,
    name: str,
    host: User,
    attendee_ids: list[int],
    recipe: dict,
    scheduled_for: datetime | None = None,
) -> Feast:
    """Create a feast, its attendee rows, and one pending invitation per guest.

    Shared by the API and the seed script so both take the same path. Delivery
    is deliberately *not* done here — the caller decides whether to send now or
    in the background.
    """
    from app.notifications import format_invitation

    feast = Feast(
        name=name, host_id=host.id, scheduled_for=scheduled_for, recipe=recipe
    )
    session.add(feast)
    session.commit()
    session.refresh(feast)

    title, body = format_invitation(feast, host)
    for uid in dict.fromkeys([host.id, *attendee_ids]):
        is_host = uid == host.id
        session.add(
            FeastAttendee(
                feast_id=feast.id,
                user_id=uid,
                response=InviteResponse.accepted if is_host else InviteResponse.invited,
                responded_at=datetime.now(timezone.utc) if is_host else None,
            )
        )
        if not is_host:
            session.add(
                Notification(
                    user_id=uid, kind="feast_invitation",
                    title=title, body=body, feast_id=feast.id,
                )
            )
    session.commit()
    session.refresh(feast)
    return feast
