"""Populate the database with demo data.

Expiry dates are relative to the day you run this, so the /expiring endpoints
always have something interesting to return.

    python -m scripts.seed               # add demo data (skips if already seeded)
    python -m scripts.seed --reset       # delete demo users first, then re-seed
    python -m scripts.seed --with-foods  # also run onboarding (makes real LLM calls)
    python -m scripts.seed --with-feasts # also plan feasts (implies --with-foods)
"""

import argparse
import sys
from datetime import date, timedelta

from sqlmodel import Session, col, or_, select

from app.database import DATABASE_URL, engine
from app.freshness import expiry_from, profile_for
from app.security import hash_password
from app.services import normalise
from app.models import (
    Buddy,
    Feast,
    FeastAttendee,
    FoodPreference,
    Friend,
    FriendStatus,
    GroceryItem,
    Notification,
    Preference,
    User,
)

# Everything seeded lives on this domain, so --reset can find it precisely
# instead of truncating tables that may hold real rows.
SEED_DOMAIN = "@grocerydemo.dev"

# (email, display name, User ID, buddy, diets, allergens, cuisines)
# Every demo account shares one password, so the join screen can be exercised
# against seeded data: see SEED_PASSWORD.
USERS = [
    ("sam" + SEED_DOMAIN, "Sam Perera", "sam.perera", "sammy",
     [], [], ["Italian", "Thai"]),
    ("nadia" + SEED_DOMAIN, "Nadia Khan", "nadia.k", "milo",
     ["vegetarian"], ["peanuts"], ["Indian", "Mediterranean"]),
    ("theo" + SEED_DOMAIN, "Theo Alvarez", "theo.a", "carl",
     [], [], ["Italian", "Mexican"]),
    ("mei" + SEED_DOMAIN, "Mei Tanaka", "mei.t", "eddie",
     [], ["shellfish"], ["Korean", "Japanese"]),
    ("obi" + SEED_DOMAIN, "Obi Nwachukwu", "obi.n", "bella",
     ["dairy_free"], [], ["West African"]),
]

SEED_PASSWORD = "fridge-friends-demo"

# Accounts created the way the join screen creates them: a User ID and a
# password, no email. (handle, User ID, display name, buddy, diets, allergens,
# cuisines) — `handle` is the key the rest of this script refers to them by.
EMAILLESS_USERS = [
    ("pal", "pantry.pal", "Pantry Pal", "carl",
     ["pescatarian"], ["peanuts"], ["Italian", "Mexican"]),
    ("rae", "rae.cooks", "Rae", "bella",
     ["gluten_free"], ["tree_nuts", "sesame"], ["Korean"]),
]

# (requester, addressee, status)
FRIENDSHIPS = [
    ("sam", "nadia", FriendStatus.accepted),
    ("sam", "theo", FriendStatus.accepted),
    ("nadia", "mei", FriendStatus.accepted),
    ("theo", "mei", FriendStatus.pending),   # not yet accepted
    ("sam", "obi", FriendStatus.pending),    # not yet accepted
    ("pal", "sam", FriendStatus.accepted),
    ("pal", "rae", FriendStatus.accepted),
    ("mei", "obi", FriendStatus.blocked),
]

# Onboarding picks per user. Only used with --with-foods, because filling in
# their ingredients means real (billed) LLM calls.
FOOD_PREFERENCES = {
    # handle: (likes, dislikes)
    "sam": (["Pad Thai", "Chicken Curry"], ["Liver and Onions"]),
    "nadia": (["Masala Dosa", "Shakshuka"], ["Oysters"]),
    "theo": (["Carbonara"], ["Durian"]),
    "mei": (["Tonkotsu Ramen"], []),
    "obi": (["Jollof Rice"], ["Marmite on Toast"]),
}

# Feasts to plan, as (name, host, guests, days_from_now, hour). The recipe for
# each is chosen by actually running the suggestion endpoint's logic, so the
# stored snapshot is a real suggestion rather than a hand-written fake.
FEASTS = [
    ("Friday Night Cook-Up", "sam", ["theo", "mei"], 2, 19),
    ("Sunday Brunch", "nadia", ["sam", "obi"], 4, 11),
]

# (owner, name, qty, unit, category, days_until_expiry, days_since_purchase, consumed)
# days_until_expiry None => let the freshness catalogue decide, the way the add
# sheet does: purchased_on + the ingredient's shelf life.
ITEMS = [
    ("sam", "Whole milk",        1,   "L",    "dairy",    -2,  9, False),
    ("sam", "Baby spinach",      200, "g",    "produce",  -1,  6, False),
    ("sam", "Greek yogurt",      500, "g",    "dairy",     1,  5, False),
    ("sam", "Chicken thighs",    750, "g",    "meat",      2,  2, False),
    ("sam", "Sourdough loaf",    1,   "loaf", "bakery",    3,  1, False),
    ("sam", "Cheddar",           250, "g",    "dairy",     9,  4, False),
    ("sam", "Eggs",              12,  "ct",   "dairy",    16,  3, False),
    ("sam", "Olive oil",         750, "ml",   "pantry",  200, 30, False),
    ("sam", "Basmati rice",      2,   "kg",   "pantry",  365, 30, False),
    ("sam", "Leftover curry",    1,   "tub",  "leftover",  1,  2, True),   # already eaten

    ("nadia", "Oat milk",        1,   "L",    "dairy",     0,  7, False),  # today
    ("nadia", "Strawberries",    400, "g",    "produce",   1,  3, False),
    ("nadia", "Silken tofu",     300, "g",    "protein",   2,  4, False),
    ("nadia", "Coriander",       1,   "bunch","produce",  -3,  8, False),
    ("nadia", "Hummus",          200, "g",    "deli",      5,  2, False),
    ("nadia", "Frozen peas",     1,   "kg",   "frozen",  180, 20, False),
    ("nadia", "Red lentils",     500, "g",    "pantry",  300, 45, False),

    ("theo", "Salmon fillet",    2,   "ct",   "seafood",   1,  1, False),
    ("theo", "Rocket",           120, "g",    "produce",   2,  3, False),
    ("theo", "Parmesan",         200, "g",    "dairy",    45, 10, False),
    ("theo", "Cold brew",        750, "ml",   "drinks",    4,  2, False),
    ("theo", "Pasta",            1,   "kg",   "pantry",  400, 60, False),

    ("mei", "Tonkotsu broth",    1,   "L",    "prepared", -1,  4, False),
    ("mei", "Spring onions",     1,   "bunch","produce",   2,  5, False),
    ("mei", "Soft tofu",         400, "g",    "protein",   3,  2, False),
    ("mei", "Miso paste",        500, "g",    "pantry",   90, 25, False),
    ("mei", "Nori sheets",       10,  "ct",   "pantry",  270, 40, False),

    ("obi", "Jollof leftovers",  2,   "tub",  "leftover",  1,  1, False),
    ("obi", "Plantain",          4,   "ct",   "produce",   3,  2, False),
    ("obi", "Scotch bonnets",    100, "g",    "produce",   6,  2, False),
    ("obi", "Egusi",             500, "g",    "pantry",  240, 35, False),

    # The two join-screen accounts. Their expiry dates are left to the
    # catalogue (None below), so their shelves show derived shelf lives.
    ("pal", "Spinach",           200, "g",    "produce",  None, 0, False),
    ("pal", "Salmon fillet",     2,   "ct",   "seafood",  None, 0, False),
    ("pal", "Whole milk",        1,   "L",    "dairy",    None, 4, False),
    ("pal", "Lemon",             3,   "ct",   "produce",  None, 0, False),
    ("pal", "Canned tomatoes",   400, "g",    "pantry",   None, 0, False),
    ("pal", "Eggplant",          1,   "ct",   "produce",  None, 5, False),

    ("rae", "Baby spinach",      150, "g",    "produce",  None, 6, False),
    ("rae", "Greek yogurt",      500, "g",    "dairy",    None, 12, False),
    ("rae", "Basmati rice",      1,   "kg",   "pantry",   None, 20, False),
    ("rae", "Scotch bonnets",    50,  "g",    "produce",  None, 3, False),
]


def handle(email_prefix: str) -> str:
    return email_prefix + SEED_DOMAIN


def reset(session: Session) -> int:
    """Delete the demo users. Items and friendships cascade away with them.

    Matched on the demo email domain, plus the User IDs of the accounts seeded
    without an email — those have nothing else to identify them by.
    """
    emailless = [username for _, username, *_ in EMAILLESS_USERS]
    doomed = session.exec(
        select(User).where(
            or_(
                col(User.email).like("%" + SEED_DOMAIN),
                col(User.username).in_(emailless),
            )
        )
    ).all()
    for user in doomed:
        session.delete(user)
    session.commit()
    return len(doomed)


def seed(session: Session) -> dict[str, User]:
    today = date.today()

    users: dict[str, User] = {}
    for email, name, username, buddy, diets, allergens, cuisines in USERS:
        user = User(
            email=email,
            name=name,
            username=username,
            buddy=Buddy(buddy),
            password_hash=hash_password(SEED_PASSWORD),
            diets=diets,
            avoid_allergens=allergens,
            favorite_cuisines=cuisines,
        )
        session.add(user)
        users[email.split("@")[0]] = user

    for key, username, name, buddy, diets, allergens, cuisines in EMAILLESS_USERS:
        user = User(
            email=None,
            name=name,
            username=username,
            buddy=Buddy(buddy),
            password_hash=hash_password(SEED_PASSWORD),
            diets=diets,
            avoid_allergens=allergens,
            favorite_cuisines=cuisines,
        )
        session.add(user)
        users[key] = user

    session.flush()  # assign ids without ending the transaction

    for requester, addressee, status in FRIENDSHIPS:
        session.add(
            Friend(
                user_id=users[requester].id,
                friend_id=users[addressee].id,
                status=status,
            )
        )

    for owner, name, qty, unit, category, expires_in, bought_ago, consumed in ITEMS:
        # The buddy and spoilage profile come from the same catalogue the API
        # uses, so seeded shelves render exactly like added ones.
        entry = profile_for(name)
        session.add(
            GroceryItem(
                name=name,
                quantity=qty,
                unit=unit,
                category=category,
                price=round(2.0 + (len(name) % 7) * 0.75, 2),
                expires_on=(
                    today + timedelta(days=expires_in) if expires_in is not None
                    else expiry_from(today - timedelta(days=bought_ago), entry.shelf_life_days)
                ),
                purchased_on=today - timedelta(days=bought_ago),
                shelf_life_days=entry.shelf_life_days,
                spoilage_profile=entry.spoilage_profile,
                shelf_buddy=entry.buddy,
                consumed=consumed,
                owner_id=users[owner].id,
            )
        )

    session.commit()
    return users


def seed_food_preferences(session: Session, users: dict[str, User]) -> None:
    """Run the onboarding path for each demo user. Makes real LLM calls."""
    from app.routers.onboarding import enrich_foods

    created: list[int] = []
    for handle, (likes, dislikes) in FOOD_PREFERENCES.items():
        user = users.get(handle)
        if user is None:
            continue
        for name, kind in (
            [(n, Preference.like) for n in likes]
            + [(n, Preference.dislike) for n in dislikes]
        ):
            row = FoodPreference(user_id=user.id, name=name, preference=kind)
            session.add(row)
            session.flush()
            created.append(row.id)
    session.commit()

    print(f"looking up ingredients for {len(created)} foods (this calls the LLM)...")
    enrich_foods(created)


def seed_feasts(session: Session, users: dict[str, User]) -> None:
    """Plan each demo feast around a genuine recipe suggestion.

    Uses the same services the API uses, so the seeded rows are exactly the
    shape the endpoints produce.
    """
    from datetime import datetime, time, timedelta, timezone

    from app.llm import LLMUnavailable, suggest_recipes
    from app.models import InviteResponse
    from app.notifications import notify_feast_invitations
    from app.services import create_feast, pantry_for, preferences_for

    for name, host_handle, guest_handles, days_out, hour in FEASTS:
        host = users.get(host_handle)
        guests = [users[g] for g in guest_handles if g in users]
        if host is None:
            continue

        group = [host.id] + [g.id for g in guests]
        pantry = pantry_for(session, group)
        liked, disliked = preferences_for(session, group)

        print(f"  {name}: asking for a recipe from {len(pantry)} ingredients...")
        try:
            recipes = suggest_recipes(
                available=[e.name for e in pantry.values()],
                expiring=[e.name for e in pantry.values() if e.expiring],
                liked=liked, disliked=disliked, limit=3,
            )
        except LLMUnavailable as exc:
            print(f"  {name}: skipped — {exc}")
            continue

        disliked_keys = {normalise(d) for d in disliked}
        chosen = next((r for r in recipes if normalise(r.name) not in disliked_keys), None)
        if chosen is None:
            print(f"  {name}: skipped — no usable suggestion")
            continue

        # Shape the snapshot the way the API stores it: resolved against the
        # real pantry, so it records who brings what.
        uses = []
        for key in dict.fromkeys(normalise(u) for u in chosen.uses):
            entry = pantry.get(key)
            if entry is not None:
                uses.append({"name": entry.name, "from_users": list(entry.owners),
                             "expiring": entry.expiring})
        total = (chosen.prep_minutes + chosen.cook_minutes
                 if chosen.prep_minutes is not None and chosen.cook_minutes is not None
                 else None)
        snapshot = {
            "rank": 1,
            "rank_reason": "Seeded demo choice",
            "name": chosen.name,
            "cuisine": chosen.cuisine or None,
            "uses": uses,
            "missing": chosen.missing,
            "uses_expiring": [u["name"] for u in uses if u["expiring"]],
            "prep_minutes": chosen.prep_minutes,
            "cook_minutes": chosen.cook_minutes,
            "total_minutes": total,
            "contributors": list(dict.fromkeys(o for u in uses for o in u["from_users"])),
            "liked_by": [], "is_liked": normalise(chosen.name) in {normalise(l) for l in liked},
            "why": chosen.why or None,
        }

        when = datetime.combine(
            date.today() + timedelta(days=days_out), time(hour, 0), tzinfo=timezone.utc
        )
        feast = create_feast(
            session, name=name, host=host,
            attendee_ids=[g.id for g in guests], recipe=snapshot, scheduled_for=when,
        )
        sent = notify_feast_invitations(session, feast.id)
        print(f"  {name}: '{chosen.name}', {len(guests)} invited, {sent} delivered")

        # Give the demo some variety: first guest accepts, second declines.
        rows = session.exec(
            select(FeastAttendee)
            .where(col(FeastAttendee.feast_id) == feast.id)
            .where(col(FeastAttendee.user_id) != host.id)
            .order_by(col(FeastAttendee.id).asc())
        ).all()
        for row, response in zip(rows, [InviteResponse.accepted, InviteResponse.declined]):
            row.response = response
            row.responded_at = datetime.now(timezone.utc)
            session.add(row)
        session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing demo users (and their data) before seeding",
    )
    parser.add_argument(
        "--with-foods",
        action="store_true",
        help="also seed food preferences, looking up ingredients via the LLM",
    )
    parser.add_argument(
        "--with-feasts",
        action="store_true",
        help="also plan feasts and send invitations (implies --with-foods)",
    )
    args = parser.parse_args()

    # The URL can carry a password; show only the host.
    where = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"target: {where}")

    with Session(engine) as session:
        existing = session.exec(
            select(User).where(col(User.email).like("%" + SEED_DOMAIN))
        ).all()

        if existing and not args.reset:
            # Feasts can be added to an already-seeded database — they depend
            # on the users and pantries, not on a fresh seed.
            if args.with_feasts:
                by_handle = {u.email.split("@")[0]: u for u in existing}
                seed_feasts(session, by_handle)
                feasts = session.exec(select(Feast)).all()
                notes = session.exec(select(Notification)).all()
                print(f"{len(feasts)} feasts, {len(notes)} notifications")
                return 0
            print(f"already seeded ({len(existing)} demo users) — pass --reset to redo")
            return 0

        if args.reset and existing:
            print(f"removed {reset(session)} demo users")

        users_by_handle = seed(session)
        # A feast needs preferences to rank against, so it pulls foods in too.
        if args.with_foods or args.with_feasts:
            seed_food_preferences(session, users_by_handle)
        if args.with_feasts:
            seed_feasts(session, users_by_handle)

        users = session.exec(select(User)).all()
        items = session.exec(select(GroceryItem)).all()
        friends = session.exec(select(Friend)).all()
        foods = session.exec(select(FoodPreference)).all()
        feasts = session.exec(select(Feast)).all()
        notes = session.exec(select(Notification)).all()
        print(
            f"seeded {len(users)} users, {len(items)} items, "
            f"{len(friends)} friendships, {len(foods)} food preferences, "
            f"{len(feasts)} feasts, {len(notes)} notifications"
        )
        if not (args.with_foods or args.with_feasts):
            print("(pass --with-foods for preferences, --with-feasts for feasts)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
