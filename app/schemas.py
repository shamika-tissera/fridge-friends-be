from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models import (
    DeliveryStatus,
    FriendStatus,
    IngredientStatus,
    InviteResponse,
    Preference,
)


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


class FoodPreferenceCreate(BaseModel):
    """What the onboarding screen sends when the user confirms their picks.

    Both lists are optional on their own, but at least one must be non-empty —
    a user may like things without disliking anything, or vice versa.
    """

    likes: list[str] = Field(default_factory=list, max_length=20)
    dislikes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def at_least_one(self):
        if not self.likes and not self.dislikes:
            raise ValueError("Give at least one liked or disliked food")
        return self


class FoodPreferenceRead(BaseModel):
    id: int
    user_id: int
    name: str
    preference: Preference
    cuisine: Optional[str]
    ingredients: list[IngredientRead]
    ingredient_status: IngredientStatus
    ingredient_error: Optional[str]
    ingredients_updated_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class OnboardingResult(BaseModel):
    """Outcome of confirming the onboarding screen."""

    saved: list[FoodPreferenceRead]
    skipped: list[str] = Field(
        default_factory=list, description="Foods this user had already recorded"
    )
    ingredients_pending: int = Field(
        description="How many foods are having their ingredients looked up in the background"
    )


# ---------- recipe suggestions ----------
class RecipeRequest(BaseModel):
    """Ask what the given users could cook together right now."""

    user_ids: list[int] = Field(min_length=1, max_length=10)
    max_results: int = Field(default=5, ge=1, le=10)
    expiring_within_days: int = Field(
        default=4, ge=0, le=60,
        description="Ingredients expiring inside this window are prioritised",
    )


class RecipeIngredient(BaseModel):
    """An ingredient the group already has, and whose kitchen it is in."""

    name: str
    from_users: list[str] = Field(description="Users who have this item")
    expiring: bool = Field(description="Expires inside the requested window")


class RecipeRead(BaseModel):
    rank: int = Field(description="1 is the best match; ordering is decided server-side")
    rank_reason: str = Field(description="Which rule put this recipe at this position")
    name: str
    cuisine: Optional[str] = None
    uses: list[RecipeIngredient] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    uses_expiring: list[str] = Field(default_factory=list)
    prep_minutes: Optional[int] = Field(default=None, description="Hands-on time")
    cook_minutes: Optional[int] = Field(default=None, description="Time on the heat")
    total_minutes: Optional[int] = Field(
        default=None, description="prep + cook, when both are known"
    )
    contributors: list[str] = Field(
        default_factory=list,
        description="Users contributing at least one ingredient to this recipe",
    )
    why: Optional[str] = None
    liked_by: list[str] = Field(
        default_factory=list,
        description="Names of users who listed this dish as a food they like",
    )
    is_liked: bool = Field(description="True if this dish is on someone's liked list")


class RecipeResponse(BaseModel):
    recipes: list[RecipeRead]
    considered_users: list[str]
    available_ingredients: int
    expiring_ingredients: list[str]
    liked_matches: int = Field(description="How many suggestions are liked dishes")
    detail: Optional[str] = Field(
        default=None,
        description="Set when no liked dish could be made, or nothing could be suggested",
    )


# ---------- feasts ----------
class FeastCreate(BaseModel):
    """Create a feast from a recipe the user picked in the front end."""

    name: str = Field(min_length=1, max_length=120)
    host_id: int
    recipe: RecipeRead = Field(
        description="The chosen suggestion, passed back verbatim from /recipes/suggest"
    )
    attendee_ids: list[int] = Field(
        default_factory=list, max_length=50,
        description="Who to invite. The host is always included.",
    )
    scheduled_for: Optional[datetime] = None


class AttendeeRead(BaseModel):
    user_id: int
    name: str
    email: EmailStr
    response: InviteResponse
    responded_at: Optional[datetime]
    is_host: bool


class FeastRead(BaseModel):
    id: int
    name: str
    host_id: int
    host_name: str
    scheduled_for: Optional[datetime]
    recipe: dict
    attendees: list[AttendeeRead]
    invitations_sent: int = Field(
        default=0, description="Invitations delivered so far"
    )
    invitations_pending: int = Field(
        default=0,
        description="Invitations queued but not yet delivered. Non-zero right "
                    "after creation, since delivery happens in the background.",
    )
    created_at: datetime


class InviteResponseUpdate(BaseModel):
    response: InviteResponse


class NotificationRead(BaseModel):
    id: int
    user_id: int
    kind: str
    title: str
    body: str
    feast_id: Optional[int]
    delivery_status: DeliveryStatus
    delivery_error: Optional[str]
    read_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}
