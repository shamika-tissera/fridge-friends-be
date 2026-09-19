from datetime import date, timedelta

from sqlmodel import Session, col, or_, select

from app.models import Friend, FriendStatus, GroceryItem, User
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
