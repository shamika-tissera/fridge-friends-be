from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

from sqlmodel import Session, col, or_, select

from app.models import (
    Feast,
    Freshness,
    ItemOutcome,
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
from app.freshness import band_for, label_for
from app.schemas import (
    ExpiringGroceryItem,
    FriendShelfItem,
    FriendShelfRead,
    GroceryItemRead,
    ShelfRead,
    StatsBucket,
    StatsRead,
)


def friend_ids(session: Session, user_id: int) -> list[int]:
    """Ids of users with an *accepted* friendship with `user_id`, either direction."""
    rows = session.exec(
        select(Friend).where(
            Friend.status == FriendStatus.accepted,
            or_(Friend.user_id == user_id, Friend.friend_id == user_id),
        )
    ).all()
    return [r.friend_id if r.user_id == user_id else r.user_id for r in rows]


def days_until(expires_on: date | None, today: date | None = None) -> int | None:
    if expires_on is None:
        return None
    return (expires_on - (today or date.today())).days


def grocery_read(item: GroceryItem, today: date | None = None) -> GroceryItemRead:
    """A stored item plus the freshness the shelf draws it with.

    Freshness is computed here rather than stored: it is a function of today's
    date, so a stored copy is stale the next morning.
    """
    delta = days_until(item.expires_on, today)
    return GroceryItemRead(
        **item.model_dump(),
        days_until_expiry=delta,
        freshness=band_for(delta),
        freshness_label=label_for(delta),
    )


RESCUE_BANDS = (Freshness.use_now, Freshness.expired)


def resolve_item(
    item: GroceryItem,
    outcome: ItemOutcome,
    *,
    on: date | None = None,
    feast_id: int | None = None,
) -> GroceryItem:
    """Mark an item used or wasted, and record whether using it was a rescue.

    `rescued` is decided here, at the moment of the change, because the band
    the item was in cannot be recovered afterwards — once it is off the shelf
    its expiry date says nothing about how close a call it was.

    `feast_id` records *where* it was eaten, which is what separates a solo
    rescue from one shared with friends. It is stored whatever the outcome —
    food binned after a feast is still food that feast is answerable for — but
    only rescues are ever split by it in the stats.
    """
    when = on or date.today()
    item.outcome = outcome
    item.resolved_on = when
    item.consumed = True
    item.rescued = (
        outcome == ItemOutcome.used
        and band_for(days_until(item.expires_on, when)) in RESCUE_BANDS
    )
    item.rescued_feast_id = feast_id
    return item


def friend_shelf(
    session: Session, owner_id: int, *, today: date | None = None
) -> FriendShelfRead:
    """What a friend may see of someone's shelf.

    Only the yellow and red buddies — a friend is being shown what needs
    cooking, not an inventory — and **never a price**. `FriendShelfItem` has no
    price field at all, so the promise cannot be broken by an oversight here.
    """
    owner = session.get(User, owner_id)
    items = session.exec(
        select(GroceryItem)
        .where(col(GroceryItem.owner_id) == owner_id)
        .where(col(GroceryItem.consumed) == False)  # noqa: E712
        .order_by(col(GroceryItem.expires_on).asc(), col(GroceryItem.id).asc())
    ).all()

    visible: list[FriendShelfItem] = []
    use_soon = needs_rescue = 0
    for item in items:
        delta = days_until(item.expires_on, today)
        band = band_for(delta)
        if band == Freshness.use_soon:
            use_soon += 1
        elif band in RESCUE_BANDS:
            needs_rescue += 1
        else:
            continue
        visible.append(
            FriendShelfItem(
                id=item.id, name=item.name, quantity=item.quantity, unit=item.unit,
                category=item.category, expires_on=item.expires_on,
                shelf_buddy=item.shelf_buddy, spoilage_profile=item.spoilage_profile,
                days_until_expiry=delta, freshness=band, freshness_label=label_for(delta),
            )
        )

    return FriendShelfRead(
        user_id=owner_id,
        username=owner.username if owner else "",
        name=owner.name if owner else "",
        items=visible, use_soon=use_soon, needs_rescue=needs_rescue,
    )


def rescue_counts(
    session: Session, user_ids: list[int], *, today: date | None = None
) -> dict[int, int]:
    """How many buddies each of these users needs to rescue, in one query.

    The friends list shows this per row, so doing it per friend would be a
    query per row.
    """
    if not user_ids:
        return {}
    rows = session.exec(
        select(GroceryItem)
        .where(col(GroceryItem.owner_id).in_(user_ids))
        .where(col(GroceryItem.consumed) == False)  # noqa: E712
    ).all()
    counts = {uid: 0 for uid in user_ids}
    for item in rows:
        if band_for(days_until(item.expires_on, today)) in RESCUE_BANDS:
            counts[item.owner_id] = counts.get(item.owner_id, 0) + 1
    return counts


def _bucket_edges(period: str, buckets: int, today: date) -> list[tuple[date, date, str]]:
    """Oldest-first (start, end, label) spans, ending with the one containing today."""
    edges: list[tuple[date, date, str]] = []
    if period == "months":
        year, month = today.year, today.month
        starts: list[date] = []
        for _ in range(buckets):
            starts.append(date(year, month, 1))
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        for index, start in enumerate(reversed(starts)):
            if start.month == 12:
                end = date(start.year, 12, 31)
            else:
                end = date(start.year, start.month + 1, 1) - timedelta(days=1)
            edges.append((start, end, start.strftime("%b")))
    else:
        # Weeks run Monday-Sunday, with this week last.
        this_monday = today - timedelta(days=today.weekday())
        for index in range(buckets):
            start = this_monday - timedelta(weeks=buckets - 1 - index)
            edges.append((start, start + timedelta(days=6), f"W{index + 1}"))
    return edges


def waste_and_spending(
    session: Session,
    user_id: int,
    *,
    period: str = "weeks",
    buckets: int = 6,
    today: date | None = None,
) -> StatsRead:
    """The waste-and-spending panel.

    Spending is attributed to when an item was **bought**, waste and rescues to
    when they were **resolved** — an item bought in week 1 and binned in week 3
    is week 1's spending and week 3's waste, which is how someone reading the
    chart would expect it to behave.

    Items with no price cannot contribute to a money total, so they are counted
    separately and reported rather than silently treated as free.
    """
    today = today or date.today()
    spans = _bucket_edges(period, buckets, today)
    window_start, window_end = spans[0][0], spans[-1][1]

    items = session.exec(
        select(GroceryItem).where(col(GroceryItem.owner_id) == user_id)
    ).all()

    totals = [
        {
            "spent": 0.0, "wasted": 0.0, "rescued": 0.0,
            "rescued_solo": 0.0, "rescued_friends": 0.0,
            "items_wasted": 0, "items_rescued": 0,
            "items_rescued_solo": 0, "items_rescued_friends": 0,
        }
        for _ in spans
    ]

    def index_for(when: date | None) -> int | None:
        if when is None or when < window_start or when > window_end:
            return None
        for index, (start, end, _) in enumerate(spans):
            if start <= when <= end:
                return index
        return None

    priced = unpriced = 0
    for item in items:
        bought_at = index_for(item.purchased_on)
        resolved_at = index_for(item.resolved_on)
        if bought_at is None and resolved_at is None:
            continue
        if item.price is None:
            unpriced += 1
            continue
        priced += 1

        if bought_at is not None:
            totals[bought_at]["spent"] += item.price
        if resolved_at is not None:
            if item.outcome == ItemOutcome.wasted:
                totals[resolved_at]["wasted"] += item.price
                totals[resolved_at]["items_wasted"] += 1
            elif item.rescued:
                totals[resolved_at]["rescued"] += item.price
                totals[resolved_at]["items_rescued"] += 1
                # No feast on the row means it was eaten alone — which is also
                # what every rescue recorded before feasts were tracked was.
                where = "friends" if item.rescued_feast_id is not None else "solo"
                totals[resolved_at][f"rescued_{where}"] += item.price
                totals[resolved_at][f"items_rescued_{where}"] += 1

    rows: list[StatsBucket] = []
    for (start, end, label), t in zip(spans, totals):
        spent = round(t["spent"], 2)
        wasted = round(t["wasted"], 2)
        rows.append(
            StatsBucket(
                label=label, starts_on=start, ends_on=end,
                spent=spent, wasted=wasted,
                # Clamped at zero: something bought before the window and binned
                # inside it is waste with no matching spend in the same bar.
                spent_and_used=round(max(spent - wasted, 0.0), 2),
                rescued=round(t["rescued"], 2),
                rescued_solo=round(t["rescued_solo"], 2),
                rescued_friends=round(t["rescued_friends"], 2),
                items_wasted=t["items_wasted"], items_rescued=t["items_rescued"],
                items_rescued_solo=t["items_rescued_solo"],
                items_rescued_friends=t["items_rescued_friends"],
            )
        )

    def share(bucket: StatsBucket) -> float:
        return round(bucket.wasted / bucket.spent * 100, 1) if bucket.spent else 0.0

    # Compare against the earliest bucket that actually has spending in it.
    # A bucket from before the user joined is 0% waste only because it is
    # empty, and "waste is up from 0%" is a misreading of an empty bar.
    spending = [r for r in rows if r.spent]
    first = share(spending[0]) if spending else 0.0
    last = share(spending[-1]) if spending else 0.0
    span_words = f"{len(rows)} {'months' if period == 'months' else 'weeks'}"
    if len(spending) < 2:
        summary = (
            f"Not enough history yet — {last:g}% of spending wasted so far."
            if spending else "Nothing bought in this period yet."
        )
    elif first == last:
        summary = f"Waste has held at {last:g}% of spending over {span_words}."
    else:
        direction = "down" if last < first else "up"
        summary = (
            f"Waste is {direction} from {first:g}% of spending to {last:g}% "
            f"over {span_words}."
        )

    return StatsRead(
        user_id=user_id, period=period, buckets=rows,
        spent=round(sum(r.spent for r in rows), 2),
        wasted=round(sum(r.wasted for r in rows), 2),
        rescued=round(sum(r.rescued for r in rows), 2),
        # Summed from the rounded buckets, so the totals always agree with the
        # bars the app draws rather than being a cent out from them.
        rescued_solo=round(sum(r.rescued_solo for r in rows), 2),
        rescued_friends=round(sum(r.rescued_friends for r in rows), 2),
        items_rescued=sum(r.items_rescued for r in rows),
        items_rescued_solo=sum(r.items_rescued_solo for r in rows),
        items_rescued_friends=sum(r.items_rescued_friends for r in rows),
        waste_percent_first=first, waste_percent_last=last, summary=summary,
        priced_items=priced, unpriced_items=unpriced,
    )


def shelf_for(session: Session, user_id: int, *, include_consumed: bool = False,
              today: date | None = None) -> ShelfRead:
    """The home screen in one call: the shelf, and the counts printed above it.

    The client would otherwise have to fetch every item and re-derive the
    bands itself, and the "needs rescuing" line has to agree with the chips.
    """
    statement = select(GroceryItem).where(col(GroceryItem.owner_id) == user_id)
    if not include_consumed:
        statement = statement.where(col(GroceryItem.consumed) == False)  # noqa: E712
    items = session.exec(
        statement.order_by(col(GroceryItem.expires_on).asc(), col(GroceryItem.id).asc())
    ).all()

    reads = [grocery_read(item, today) for item in items]
    counts = {band: 0 for band in Freshness}
    for read in reads:
        counts[read.freshness] += 1

    rescuing = [r for r in reads if r.freshness in (Freshness.use_now, Freshness.expired)]
    return ShelfRead(
        user_id=user_id,
        items=reads,
        fresh=counts[Freshness.fresh],
        use_soon=counts[Freshness.use_soon],
        use_now=counts[Freshness.use_now],
        expired=counts[Freshness.expired],
        needs_rescue=len(rescuing),
        rescue_value=round(sum(r.price or 0.0 for r in rescuing), 2),
    )


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
                **grocery_read(item, today).model_dump(),
                owner_name=owner.name,
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
