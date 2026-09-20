import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models import (
    Allergen,
    Buddy,
    DeliveryStatus,
    Diet,
    Freshness,
    FriendStatus,
    IngredientStatus,
    InviteResponse,
    ItemOutcome,
    Preference,
    SpoilageProfile,
)


def _slug(value: str) -> str:
    """"Gluten-free" and "Tree nuts" are what the UI shows; this is what we store."""
    return "_".join(value.strip().lower().replace("-", " ").split())


# ---------- User ----------
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,39}$"


class UserCreate(BaseModel):
    """Sign-up. `username` and `password` are what the join screen collects.

    `email` stays accepted but optional — nothing on the screen asks for it,
    and the in-app inbox does not need one.
    """

    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    buddy: Buddy = Buddy.sammy
    email: Optional[EmailStr] = None

    @field_validator("username")
    @classmethod
    def normalise_username(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.match(USERNAME_PATTERN, value):
            raise ValueError(
                "User ID must be 3-40 characters: letters, digits, dot, dash or underscore"
            )
        return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def lower(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseModel):
    """The Account card's Edit, plus the profile fields.

    Changing the User ID is allowed — it is a display handle, not the row key,
    so nothing else has to move with it.
    """

    username: Optional[str] = Field(default=None, min_length=3, max_length=40)
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    buddy: Optional[Buddy] = None

    _check_username = field_validator("username")(UserCreate.normalise_username.__func__)


class PasswordUpdate(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserRead(BaseModel):
    id: int
    username: str
    email: Optional[EmailStr]
    name: str
    buddy: Buddy
    diets: list[Diet]
    avoid_allergens: list[Allergen]
    favorite_cuisines: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- taste profile (onboarding step 2) ----------
class TasteProfileUpdate(BaseModel):
    """"Let's get to know you": diets, allergens and loved cuisines.

    Every field is a full replacement, not a merge — the screen submits the
    complete selection each time, and de-selecting the last chip has to stick.
    Omit a field to leave it untouched.
    """

    diets: Optional[list[Diet]] = Field(default=None, max_length=5)
    avoid_allergens: Optional[list[Allergen]] = Field(default=None, max_length=10)
    favorite_cuisines: Optional[list[str]] = Field(default=None, max_length=15)

    @field_validator("diets", "avoid_allergens", mode="before")
    @classmethod
    def accept_ui_labels(cls, value):
        """Take "Gluten-free" and "Tree nuts" as readily as the stored slugs."""
        if value is None:
            return None
        return [_slug(v) if isinstance(v, str) else v for v in value]

    @field_validator("favorite_cuisines")
    @classmethod
    def tidy_cuisines(cls, value):
        if value is None:
            return None
        cleaned = [" ".join(v.split()).title() for v in value if v.strip()]
        return list(dict.fromkeys(cleaned))   # de-duplicate, keep order


class TasteProfileRead(BaseModel):
    user_id: int
    diets: list[Diet]
    avoid_allergens: list[Allergen]
    favorite_cuisines: list[str]


# ---------- GroceryItem ----------
class GroceryItemCreate(BaseModel):
    """What the "Add an ingredient" sheet sends.

    Only `name` is required. `purchased_on` defaults to today ("its freshness
    timer starts today"), and shelf life, spoilage profile, buddy and
    `expires_on` are all filled in server-side from the freshness catalogue
    unless the client overrides them.
    """

    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(default=1.0, ge=0)
    unit: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, max_length=64)
    price: Optional[float] = Field(default=None, ge=0, le=100_000)
    expires_on: Optional[date] = None
    purchased_on: Optional[date] = None
    shelf_life_days: Optional[int] = Field(default=None, ge=0, le=3650)
    spoilage_profile: Optional[SpoilageProfile] = None
    shelf_buddy: Optional[str] = Field(default=None, max_length=32)


class GroceryItemUpdate(BaseModel):
    """`consumed` is kept in step with `outcome`; prefer POST .../resolve."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    quantity: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, max_length=64)
    price: Optional[float] = Field(default=None, ge=0, le=100_000)
    expires_on: Optional[date] = None
    purchased_on: Optional[date] = None
    shelf_life_days: Optional[int] = Field(default=None, ge=0, le=3650)
    spoilage_profile: Optional[SpoilageProfile] = None
    shelf_buddy: Optional[str] = Field(default=None, max_length=32)
    consumed: Optional[bool] = None


class GroceryItemRead(BaseModel):
    id: int
    name: str
    quantity: float
    unit: Optional[str]
    category: Optional[str]
    price: Optional[float]
    expires_on: Optional[date]
    purchased_on: Optional[date]
    shelf_life_days: Optional[int]
    spoilage_profile: SpoilageProfile
    shelf_buddy: str
    consumed: bool
    outcome: ItemOutcome
    resolved_on: Optional[date]
    rescued: bool
    owner_id: int
    created_at: datetime

    # Derived on read, never stored — they change with the calendar, and a
    # stored copy would be wrong by morning.
    days_until_expiry: Optional[int] = None
    freshness: Freshness = Freshness.unknown
    freshness_label: str = Field(
        default="no date", description='The countdown as shown on the shelf: "3 days"'
    )

    model_config = {"from_attributes": True}


class ExpiringGroceryItem(GroceryItemRead):
    """A grocery item plus how close it is to expiry."""

    owner_name: str
    days_until_expiry: int
    expired: bool


class ItemResolution(BaseModel):
    """Marking an item used or wasted from the shelf."""

    outcome: ItemOutcome = Field(description="`used` (eaten) or `wasted` (binned)")
    resolved_on: Optional[date] = Field(
        default=None, description="Defaults to today"
    )

    @field_validator("outcome")
    @classmethod
    def must_end_its_life(cls, value: ItemOutcome) -> ItemOutcome:
        if value == ItemOutcome.on_shelf:
            raise ValueError("Use `used` or `wasted`; PATCH the item to put it back")
        return value


class FreshnessPreview(BaseModel):
    """What the add sheet shows before the item is saved."""

    name: str
    purchased_on: date
    shelf_life_days: int
    spoilage_profile: SpoilageProfile
    shelf_buddy: str
    category: str
    expires_on: date
    summary: str = Field(description='e.g. "Gradual · ~7 days"')


class ShelfRead(BaseModel):
    """The home screen: every item on the shelf, plus the counts above it."""

    user_id: int
    items: list[GroceryItemRead]
    fresh: int
    use_soon: int
    use_now: int
    expired: int
    needs_rescue: int = Field(
        description="use_now + expired — what the Rescue ingredients badge counts"
    )
    rescue_value: float = Field(
        description="Total price of the items needing rescuing, 0 where no price was given"
    )


# ---------- waste and spending ----------
class StatsBucket(BaseModel):
    """One bar on the chart."""

    label: str = Field(description='e.g. "W1" or "Mar"')
    starts_on: date
    ends_on: date
    spent: float = Field(description="Everything bought in this bucket")
    wasted: float = Field(description="Of that spend, what was thrown away")
    spent_and_used: float = Field(description="spent - wasted; the green part of the bar")
    rescued: float = Field(description="Value used up while already in the red")
    items_wasted: int
    items_rescued: int


class StatsRead(BaseModel):
    """The waste-and-spending panel: three totals, the bars, and the trend line."""

    user_id: int
    period: str = Field(description="`weeks` or `months`")
    buckets: list[StatsBucket]
    spent: float
    wasted: float
    rescued: float
    waste_percent_first: float = Field(
        description="Waste as a share of spending in the earliest bucket"
    )
    waste_percent_last: float = Field(description="The same in the most recent bucket")
    summary: str = Field(
        description='e.g. "Waste is down from 17% of spending to 3% over six weeks."'
    )
    priced_items: int = Field(description="Items counted; ones with no price are excluded")
    unpriced_items: int = Field(
        description="Items in range with no price — they are missing from the totals"
    )


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
    needs_rescue: int = Field(
        default=0,
        description="How many of their buddies are in the use-now or expired bands",
    )


class FriendInvite(BaseModel):
    """Invite by User ID, which is all the invite box on the You screen collects."""

    username: str = Field(min_length=3, max_length=40)

    @field_validator("username")
    @classmethod
    def lower(cls, value: str) -> str:
        return value.strip().lower()


class FriendShelfItem(BaseModel):
    """One of a friend's buddies, as a friend is allowed to see it.

    Deliberately not a `GroceryItemRead`: **price is never included**. The You
    screen promises "prices stay private", and the safest way to keep that
    promise is a response shape that has nowhere to put one.
    """

    id: int
    name: str
    quantity: float
    unit: Optional[str]
    category: Optional[str]
    expires_on: Optional[date]
    shelf_buddy: str
    spoilage_profile: SpoilageProfile
    days_until_expiry: Optional[int]
    freshness: Freshness
    freshness_label: str


class FriendShelfRead(BaseModel):
    """What a friend can see of someone's shelf: the yellow and red buddies only."""

    user_id: int
    username: str
    name: str
    items: list[FriendShelfItem]
    use_soon: int
    needs_rescue: int


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
    dietary_rules_applied: list[str] = Field(
        default_factory=list,
        description="The allergy and diet rules enforced for this group",
    )
    excluded_for_dietary_rules: int = Field(
        default=0, description="Suggestions dropped because they broke one of those rules"
    )
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
    username: str
    name: str
    email: Optional[EmailStr]
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
