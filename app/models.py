from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime
from sqlmodel import Column, Field, Relationship, SQLModel, UniqueConstraint


def tz_column() -> Column:
    """Timestamps are stored tz-aware; plain TIMESTAMP would drop the UTC offset."""
    return Column(DateTime(timezone=True), nullable=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
