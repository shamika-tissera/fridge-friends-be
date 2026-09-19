"""Populate the database with demo data.

Expiry dates are relative to the day you run this, so the /expiring endpoints
always have something interesting to return.

    python -m scripts.seed           # add demo data (skips if already seeded)
    python -m scripts.seed --reset   # delete demo users first, then re-seed
"""

import argparse
import sys
from datetime import date, timedelta

from sqlmodel import Session, col, select

from app.database import DATABASE_URL, engine
from app.models import Friend, FriendStatus, GroceryItem, User

# Everything seeded lives on this domain, so --reset can find it precisely
# instead of truncating tables that may hold real rows.
SEED_DOMAIN = "@grocerydemo.dev"

USERS = [
    ("sam" + SEED_DOMAIN, "Sam Perera"),
    ("nadia" + SEED_DOMAIN, "Nadia Khan"),
    ("theo" + SEED_DOMAIN, "Theo Alvarez"),
    ("mei" + SEED_DOMAIN, "Mei Tanaka"),
    ("obi" + SEED_DOMAIN, "Obi Nwachukwu"),
]

# (requester, addressee, status)
FRIENDSHIPS = [
    ("sam", "nadia", FriendStatus.accepted),
    ("sam", "theo", FriendStatus.accepted),
    ("nadia", "mei", FriendStatus.accepted),
    ("theo", "mei", FriendStatus.pending),   # not yet accepted
    ("sam", "obi", FriendStatus.pending),    # not yet accepted
    ("mei", "obi", FriendStatus.blocked),
]

# (owner, name, qty, unit, category, days_until_expiry, days_since_purchase, consumed)
# days_until_expiry None => pantry staple with no expiry date.
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
]


def handle(email_prefix: str) -> str:
    return email_prefix + SEED_DOMAIN


def reset(session: Session) -> int:
    """Delete the demo users. Items and friendships cascade away with them."""
    doomed = session.exec(
        select(User).where(col(User.email).like("%" + SEED_DOMAIN))
    ).all()
    for user in doomed:
        session.delete(user)
    session.commit()
    return len(doomed)


def seed(session: Session) -> None:
    today = date.today()

    users: dict[str, User] = {}
    for email, name in USERS:
        user = User(email=email, name=name)
        session.add(user)
        users[email.split("@")[0]] = user
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
        session.add(
            GroceryItem(
                name=name,
                quantity=qty,
                unit=unit,
                category=category,
                expires_on=None if expires_in is None else today + timedelta(days=expires_in),
                purchased_on=today - timedelta(days=bought_ago),
                consumed=consumed,
                owner_id=users[owner].id,
            )
        )

    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing demo users (and their data) before seeding",
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
            print(f"already seeded ({len(existing)} demo users) — pass --reset to redo")
            return 0

        if args.reset and existing:
            print(f"removed {reset(session)} demo users")

        seed(session)

        users = session.exec(select(User)).all()
        items = session.exec(select(GroceryItem)).all()
        friends = session.exec(select(Friend)).all()
        print(f"seeded {len(users)} users, {len(items)} items, {len(friends)} friendships")

    return 0


if __name__ == "__main__":
    sys.exit(main())
