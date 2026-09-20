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
    email: str = Field(index=True, unique=True, max_length=255)
    name: str = Field(max_length=120)
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
    consumed: bool = Field(default=False, index=True)
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
