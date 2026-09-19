from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.models import FriendStatus, IngredientStatus


# ---------- User ----------
class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)


class UserRead(BaseModel):
    id: int
    email: EmailStr
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- GroceryItem ----------
class GroceryItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(default=1.0, ge=0)
    unit: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, max_length=64)
    expires_on: Optional[date] = None
    purchased_on: Optional[date] = None


class GroceryItemUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    quantity: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, max_length=64)
    expires_on: Optional[date] = None
    purchased_on: Optional[date] = None
    consumed: Optional[bool] = None


class GroceryItemRead(BaseModel):
    id: int
    name: str
    quantity: float
    unit: Optional[str]
    category: Optional[str]
    expires_on: Optional[date]
    purchased_on: Optional[date]
    consumed: bool
    owner_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ExpiringGroceryItem(GroceryItemRead):
    """A grocery item plus how close it is to expiry."""

    owner_name: str
    days_until_expiry: int
    expired: bool


# ---------- Friend ----------
class FriendCreate(BaseModel):
    friend_id: int


class FriendUpdate(BaseModel):
    status: FriendStatus


class FriendRead(BaseModel):
    id: int
    user_id: int
    friend_id: int
    status: FriendStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class FriendWithUser(FriendRead):
    """The friendship row plus the *other* user's profile."""

    friend: UserRead


# ---------- FavoriteFood / onboarding ----------
class IngredientRead(BaseModel):
    name: str
    category: str
    essential: bool


class FavoriteFoodCreate(BaseModel):
    """What the onboarding screen sends when the user confirms their picks."""

    foods: list[str] = Field(min_length=1, max_length=20)


class FavoriteFoodRead(BaseModel):
    id: int
    user_id: int
    name: str
    cuisine: Optional[str]
    ingredients: list[IngredientRead]
    ingredient_status: IngredientStatus
    ingredient_error: Optional[str]
    ingredients_updated_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class OnboardingResult(BaseModel):
    """Outcome of confirming the onboarding screen."""

    saved: list[FavoriteFoodRead]
    skipped: list[str] = Field(
        default_factory=list, description="Foods this user had already saved"
    )
    ingredients_pending: int = Field(
        description="How many foods are having their ingredients looked up in the background"
    )
