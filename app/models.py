from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, Relationship, SQLModel, UniqueConstraint


def tz_column() -> Column:
    """Timestamps are stored tz-aware; plain TIMESTAMP would drop the UTC offset."""
    return Column(DateTime(timezone=True), nullable=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON on SQLite, JSONB on Postgres — same Python type, but JSONB is binary
# and indexable, so ingredient lookups stay cheap as the table grows.
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class Buddy(str, Enum):
    """The character the user picks at sign-up, and the sprite drawn beside them."""

    sammy = "sammy"     # leaf
    milo = "milo"       # milk carton
    eddie = "eddie"     # egg
    carl = "carl"       # canned tomatoes
    bella = "bella"     # potato


class Diet(str, Enum):
    """A way of eating the user follows. A user may follow several."""

    vegetarian = "vegetarian"
    vegan = "vegan"
    pescatarian = "pescatarian"
    gluten_free = "gluten_free"
    dairy_free = "dairy_free"


class Allergen(str, Enum):
    """Something the user always avoids. Enforced, not merely preferred."""

    peanuts = "peanuts"
    shellfish = "shellfish"
    tree_nuts = "tree_nuts"
    sesame = "sesame"


class SpoilageProfile(str, Enum):
    """How an ingredient goes off, which is what the shelf animation reflects."""

    gradual = "gradual"   # wilts over days: leaves, most produce
    sudden = "sudden"     # fine until it is not: dairy, fish, meat
    stable = "stable"     # cupboard goods measured in months or years


class ItemOutcome(str, Enum):
    """How an item's life ended. The whole waste-and-spending screen rests on this.

    `consumed` only ever said "gone"; it could not tell eating something from
    throwing it away, which is exactly the difference the screen reports.
    """

    on_shelf = "on_shelf"
    used = "used"       # eaten or cooked with
    wasted = "wasted"   # thrown out


class Freshness(str, Enum):
    """The chip shown on the shelf. Derived from `expires_on`, never stored."""

    fresh = "fresh"
    use_soon = "use_soon"
    use_now = "use_now"
    expired = "expired"
    unknown = "unknown"   # no expiry date recorded


class Preference(str, Enum):
    """Whether the user likes or dislikes a food."""

    like = "like"
    dislike = "dislike"


class IngredientStatus(str, Enum):
    """How the LLM enrichment for a favourite food went."""

    pending = "pending"   # saved, ingredients not fetched yet
    ready = "ready"       # ingredients stored
    failed = "failed"     # the LLM call failed; retry via the refresh endpoint


class FriendStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    blocked = "blocked"


class User(SQLModel, table=True):
    __tablename__ = "app_user"

    id: Optional[int] = Field(default=None, primary_key=True)

    # The "User ID" on the sign-up screen (e.g. "pantry.pal"): what someone
    # types to log in, and how friends find each other. Stored lowercase.
    username: str = Field(index=True, unique=True, max_length=40)

    # Optional: the sign-up screen never asks for it. Notifications fall back
    # to the in-app inbox, which is the durable record anyway.
    email: Optional[str] = Field(default=None, index=True, unique=True, max_length=255)
    name: str = Field(max_length=120)

    # Null for accounts created before passwords existed, and for seed data.
    # verify_password() treats that as "can never log in" rather than "no check".
    password_hash: Optional[str] = Field(default=None, max_length=255)
    buddy: Buddy = Field(default=Buddy.sammy)

    # The onboarding screen's three multi-selects. They are short, fixed lists
    # always read together with the user, so they live here as JSON rather than
    # in three join tables that would never be queried independently.
    diets: list[str] = Field(default_factory=list, sa_column=Column(JSON_TYPE, nullable=False))
    avoid_allergens: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )
    favorite_cuisines: list[str] = Field(
        default_factory=list, sa_column=Column(JSON_TYPE, nullable=False)
    )

    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())

    grocery_items: list["GroceryItem"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    food_preferences: list["FoodPreference"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class GroceryItem(SQLModel, table=True):
    """A single item in a user's pantry/fridge."""

    __tablename__ = "grocery_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, max_length=120)
    quantity: float = Field(default=1.0, ge=0)
    unit: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, index=True, max_length=64)
    expires_on: Optional[date] = Field(default=None, index=True)
    purchased_on: Optional[date] = Field(default=None)
    # Kept as the "no longer on the shelf" flag it always was, and moved in
    # step with `outcome` so existing callers keep working.
    consumed: bool = Field(default=False, index=True)

    outcome: ItemOutcome = Field(default=ItemOutcome.on_shelf, index=True)
    resolved_on: Optional[date] = Field(default=None, index=True)

    # Set when an item is used while it is already in the "use now" or expired
    # band — food that would have been binned tomorrow. Recorded at that moment
    # rather than derived later, because the band it was in is not recoverable
    # once the item is gone.
    rescued: bool = Field(default=False, index=True)

    # What it cost, for the "you rescued $X of food" story. Float, like
    # quantity: these are display totals, never billed against.
    price: Optional[float] = Field(default=None, ge=0)

    # Filled from the freshness catalogue when the item is added, then kept:
    # re-deriving later would silently move an item's expiry if the catalogue
    # changed. `expires_on` = purchased_on + shelf_life_days unless the user
    # overrode the date.
    shelf_life_days: Optional[int] = Field(default=None, ge=0)
    spoilage_profile: SpoilageProfile = Field(default=SpoilageProfile.gradual, index=True)
    shelf_buddy: str = Field(default="leaf", max_length=32)
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())

    owner_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    owner: User = Relationship(back_populates="grocery_items")


class Friend(SQLModel, table=True):
    """Association entity linking two users.

    A row is directional (user_id requested friend_id) but a pair is stored
    only once; `status` tracks whether the other side accepted.
    """

    __tablename__ = "friend"
    __table_args__ = (UniqueConstraint("user_id", "friend_id", name="uq_friend_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    friend_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    status: FriendStatus = Field(default=FriendStatus.pending, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())


class FoodPreference(SQLModel, table=True):
    """A food the user likes or dislikes, plus its ingredients.

    Likes and dislikes share one table because they carry identical data and are
    almost always read together ("suggest things they like, avoiding anything
    with an ingredient they dislike"). The unique constraint is on
    (user_id, name) without the preference, so a user cannot both like and
    dislike the same food.
    """

    __tablename__ = "food_preference"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_food_preference_per_user"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    name: str = Field(index=True, max_length=120)
    preference: Preference = Field(default=Preference.like, index=True)
    cuisine: Optional[str] = Field(default=None, max_length=64)

    # [{"name": "coconut milk", "category": "pantry", "essential": true}, ...]
    ingredients: list[dict] = Field(default_factory=list, sa_column=Column(JSON_TYPE, nullable=False))

    ingredient_status: IngredientStatus = Field(
        default=IngredientStatus.pending, index=True
    )
    ingredient_error: Optional[str] = Field(default=None, max_length=255)
    ingredients_updated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())

    user: User = Relationship(back_populates="food_preferences")


class InviteResponse(str, Enum):
    """Where an attendee stands on their invitation."""

    invited = "invited"
    accepted = "accepted"
    declined = "declined"


class DeliveryStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class Feast(SQLModel, table=True):
    """A planned meal built around one chosen recipe."""

    __tablename__ = "feast"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120)
    host_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    scheduled_for: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )

    # A full snapshot of the chosen suggestion. Recipes are generated on the fly
    # and stored nowhere else, so a reference would dangle the moment the
    # suggestion call returns. Snapshotting also keeps the feast honest: it
    # records what was agreed to, even after pantries and preferences change.
    recipe: dict = Field(default_factory=dict, sa_column=Column(JSON_TYPE, nullable=False))

    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())

    host: User = Relationship()
    attendees: list["FeastAttendee"] = Relationship(
        back_populates="feast",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class FeastAttendee(SQLModel, table=True):
    """Association entity: one person invited to one feast."""

    __tablename__ = "feast_attendee"
    __table_args__ = (
        UniqueConstraint("feast_id", "user_id", name="uq_feast_attendee"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    feast_id: int = Field(foreign_key="feast.id", index=True, ondelete="CASCADE")
    user_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    response: InviteResponse = Field(default=InviteResponse.invited, index=True)
    responded_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())

    feast: Feast = Relationship(back_populates="attendees")
    user: User = Relationship()


class Notification(SQLModel, table=True):
    """An in-app message for one user. Also the delivery record for a channel."""

    __tablename__ = "notification"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", index=True, ondelete="CASCADE")
    kind: str = Field(default="feast_invitation", index=True, max_length=48)
    title: str = Field(max_length=160)
    body: str = Field(max_length=1000)
    feast_id: Optional[int] = Field(
        default=None, foreign_key="feast.id", index=True, ondelete="CASCADE"
    )
    delivery_status: DeliveryStatus = Field(default=DeliveryStatus.pending, index=True)
    delivery_error: Optional[str] = Field(default=None, max_length=255)
    read_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
