"""How long an ingredient lasts, which buddy sits with it, and how fresh it is.

The "Add an ingredient" screen shows a freshness preview the moment a name is
typed — spoilage profile, rough shelf life, and the buddy picked for the item —
so this lookup has to be instant. It is a static catalogue rather than an LLM
call: a round-trip in the middle of typing would be both slow and non-repeatable,
and the answer for "spinach" never changes.

Anything not in the catalogue falls back to DEFAULT_ENTRY, which is deliberately
short and gradual: over-estimating shelf life is how food quietly rots.
"""

from datetime import date, timedelta
from typing import NamedTuple, Optional

from app.models import Freshness, SpoilageProfile

# Bands the shelf chips are drawn from ("Fresh" / "Use soon" / "Use now").
USE_NOW_WITHIN_DAYS = 2
USE_SOON_WITHIN_DAYS = 5


# Every sprite key the catalogue can hand back. The front end needs art for
# each one, and DEFAULT_ENTRY's "leaf" is the fallback for anything unknown.
BUDDY_KEYS = (
    "leaf", "berry", "lemon", "tomato", "eggplant", "carrot", "potato",
    "egg", "milk", "cheese", "bread", "meat", "fish", "jar",
)


class ShelfProfile(NamedTuple):
    """What we know about an ingredient before the user tells us anything."""

    shelf_life_days: int
    spoilage_profile: SpoilageProfile
    buddy: str          # sprite key the front end draws on the shelf
    category: str


# Matched as substrings against the normalised item name, longest key first, so
# "canned tomatoes" beats "tomato" and "almond milk" never reads as "milk".
CATALOGUE: dict[str, ShelfProfile] = {
    # leafy + soft produce — wilt gradually, go fast
    "spinach": ShelfProfile(7, SpoilageProfile.gradual, "leaf", "produce"),
    "lettuce": ShelfProfile(7, SpoilageProfile.gradual, "leaf", "produce"),
    "kale": ShelfProfile(7, SpoilageProfile.gradual, "leaf", "produce"),
    "herbs": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "basil": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "coriander": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "cilantro": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "berries": ShelfProfile(4, SpoilageProfile.gradual, "berry", "produce"),
    "strawberries": ShelfProfile(4, SpoilageProfile.gradual, "berry", "produce"),
    "blueberries": ShelfProfile(7, SpoilageProfile.gradual, "berry", "produce"),
    "avocado": ShelfProfile(4, SpoilageProfile.sudden, "eggplant", "produce"),
    "banana": ShelfProfile(5, SpoilageProfile.gradual, "lemon", "produce"),
    "mushroom": ShelfProfile(6, SpoilageProfile.gradual, "eggplant", "produce"),
    "eggplant": ShelfProfile(7, SpoilageProfile.gradual, "eggplant", "produce"),
    "aubergine": ShelfProfile(7, SpoilageProfile.gradual, "eggplant", "produce"),
    "courgette": ShelfProfile(7, SpoilageProfile.gradual, "eggplant", "produce"),
    "zucchini": ShelfProfile(7, SpoilageProfile.gradual, "eggplant", "produce"),
    "broccoli": ShelfProfile(7, SpoilageProfile.gradual, "leaf", "produce"),
    "rocket": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "arugula": ShelfProfile(5, SpoilageProfile.gradual, "leaf", "produce"),
    "cabbage": ShelfProfile(21, SpoilageProfile.gradual, "leaf", "produce"),
    "peas": ShelfProfile(180, SpoilageProfile.stable, "leaf", "frozen"),
    "plantain": ShelfProfile(7, SpoilageProfile.gradual, "lemon", "produce"),
    "chilli": ShelfProfile(10, SpoilageProfile.gradual, "eggplant", "produce"),
    "bonnet": ShelfProfile(10, SpoilageProfile.gradual, "eggplant", "produce"),
    "cucumber": ShelfProfile(7, SpoilageProfile.gradual, "eggplant", "produce"),
    "pepper": ShelfProfile(10, SpoilageProfile.gradual, "eggplant", "produce"),
    "tomato": ShelfProfile(7, SpoilageProfile.gradual, "tomato", "produce"),
    "lemon": ShelfProfile(21, SpoilageProfile.gradual, "lemon", "produce"),
    "lime": ShelfProfile(21, SpoilageProfile.gradual, "lemon", "produce"),
    "orange": ShelfProfile(21, SpoilageProfile.gradual, "lemon", "produce"),
    "apple": ShelfProfile(30, SpoilageProfile.gradual, "berry", "produce"),
    "carrot": ShelfProfile(30, SpoilageProfile.gradual, "carrot", "produce"),
    "potato": ShelfProfile(45, SpoilageProfile.gradual, "potato", "produce"),
    "sweet potato": ShelfProfile(30, SpoilageProfile.gradual, "potato", "produce"),
    "onion": ShelfProfile(45, SpoilageProfile.gradual, "potato", "produce"),
    "garlic": ShelfProfile(60, SpoilageProfile.gradual, "potato", "produce"),

    # protein — these turn, rather than fade
    "fish": ShelfProfile(2, SpoilageProfile.sudden, "fish", "protein"),
    "salmon": ShelfProfile(2, SpoilageProfile.sudden, "fish", "protein"),
    "tuna": ShelfProfile(2, SpoilageProfile.sudden, "fish", "protein"),
    "prawns": ShelfProfile(2, SpoilageProfile.sudden, "fish", "protein"),
    "shrimp": ShelfProfile(2, SpoilageProfile.sudden, "fish", "protein"),
    "chicken": ShelfProfile(2, SpoilageProfile.sudden, "meat", "protein"),
    "turkey": ShelfProfile(2, SpoilageProfile.sudden, "meat", "protein"),
    "mince": ShelfProfile(2, SpoilageProfile.sudden, "meat", "protein"),
    "beef": ShelfProfile(4, SpoilageProfile.sudden, "meat", "protein"),
    "pork": ShelfProfile(4, SpoilageProfile.sudden, "meat", "protein"),
    "lamb": ShelfProfile(4, SpoilageProfile.sudden, "meat", "protein"),
    "bacon": ShelfProfile(7, SpoilageProfile.sudden, "meat", "protein"),
    "tofu": ShelfProfile(7, SpoilageProfile.sudden, "cheese", "protein"),
    "egg": ShelfProfile(21, SpoilageProfile.gradual, "egg", "protein"),

    # dairy
    "milk": ShelfProfile(7, SpoilageProfile.sudden, "milk", "dairy"),
    "cream": ShelfProfile(7, SpoilageProfile.sudden, "milk", "dairy"),
    "yoghurt": ShelfProfile(14, SpoilageProfile.sudden, "milk", "dairy"),
    "yogurt": ShelfProfile(14, SpoilageProfile.sudden, "milk", "dairy"),
    "butter": ShelfProfile(60, SpoilageProfile.gradual, "cheese", "dairy"),
    "cheese": ShelfProfile(21, SpoilageProfile.gradual, "cheese", "dairy"),
    "cheddar": ShelfProfile(21, SpoilageProfile.gradual, "cheese", "dairy"),
    "parmesan": ShelfProfile(60, SpoilageProfile.gradual, "cheese", "dairy"),
    "mozzarella": ShelfProfile(10, SpoilageProfile.sudden, "cheese", "dairy"),
    "feta": ShelfProfile(21, SpoilageProfile.gradual, "cheese", "dairy"),
    "halloumi": ShelfProfile(30, SpoilageProfile.gradual, "cheese", "dairy"),

    # bakery + grains
    "bread": ShelfProfile(5, SpoilageProfile.gradual, "bread", "grain"),
    "sourdough": ShelfProfile(5, SpoilageProfile.gradual, "bread", "grain"),
    "loaf": ShelfProfile(5, SpoilageProfile.gradual, "bread", "grain"),
    "baguette": ShelfProfile(2, SpoilageProfile.gradual, "bread", "grain"),
    "bagel": ShelfProfile(7, SpoilageProfile.gradual, "bread", "grain"),
    "pitta": ShelfProfile(7, SpoilageProfile.gradual, "bread", "grain"),
    "tortilla": ShelfProfile(14, SpoilageProfile.gradual, "bread", "grain"),
    "pasta": ShelfProfile(365, SpoilageProfile.stable, "jar", "grain"),
    "noodles": ShelfProfile(365, SpoilageProfile.stable, "jar", "grain"),
    "rice": ShelfProfile(730, SpoilageProfile.stable, "jar", "grain"),
    "flour": ShelfProfile(365, SpoilageProfile.stable, "jar", "grain"),

    # cupboard — these are the "2 years" chips on the shelf
    "canned": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "tinned": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "canned tomatoes": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "beans": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "chickpeas": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "lentils": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "oil": ShelfProfile(365, SpoilageProfile.stable, "jar", "pantry"),
    "vinegar": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "sauce": ShelfProfile(180, SpoilageProfile.stable, "jar", "pantry"),
    "honey": ShelfProfile(730, SpoilageProfile.stable, "jar", "pantry"),
    "jam": ShelfProfile(365, SpoilageProfile.stable, "jar", "pantry"),
    "spice": ShelfProfile(365, SpoilageProfile.stable, "jar", "spice"),
    "miso": ShelfProfile(180, SpoilageProfile.stable, "jar", "pantry"),
    "hummus": ShelfProfile(7, SpoilageProfile.sudden, "jar", "deli"),
    "nori": ShelfProfile(270, SpoilageProfile.stable, "jar", "pantry"),
    "broth": ShelfProfile(5, SpoilageProfile.sudden, "jar", "prepared"),
    "stock": ShelfProfile(5, SpoilageProfile.sudden, "jar", "prepared"),
    # Cooked food already has a head start on going off.
    "leftover": ShelfProfile(3, SpoilageProfile.sudden, "jar", "leftover"),
}

DEFAULT_ENTRY = ShelfProfile(7, SpoilageProfile.gradual, "leaf", "other")

# Longest first so a two-word key wins over the single word inside it.
_KEYS_BY_LENGTH = sorted(CATALOGUE, key=len, reverse=True)


def normalise(name: str) -> str:
    return " ".join(name.split()).strip().lower()


def profile_for(name: str) -> ShelfProfile:
    """The catalogue entry for an ingredient name, or a cautious default."""
    text = normalise(name)
    for key in _KEYS_BY_LENGTH:
        if key in text:
            return CATALOGUE[key]
    return DEFAULT_ENTRY


def expiry_from(purchased_on: date, shelf_life_days: int) -> date:
    """The freshness timer starts the day the item was bought, not the day it was added."""
    return purchased_on + timedelta(days=shelf_life_days)


def band_for(days_until_expiry: Optional[int]) -> Freshness:
    """Which shelf chip an item gets. No expiry date means we cannot judge it."""
    if days_until_expiry is None:
        return Freshness.unknown
    if days_until_expiry < 0:
        return Freshness.expired
    if days_until_expiry <= USE_NOW_WITHIN_DAYS:
        return Freshness.use_now
    if days_until_expiry <= USE_SOON_WITHIN_DAYS:
        return Freshness.use_soon
    return Freshness.fresh


def label_for(days_until_expiry: Optional[int]) -> str:
    """The countdown as the shelf shows it: "2 days", "3 weeks", "2 years"."""
    if days_until_expiry is None:
        return "no date"
    if days_until_expiry < 0:
        gone = -days_until_expiry
        return f"{gone} day{'s' if gone != 1 else ''} ago"
    if days_until_expiry == 0:
        return "today"
    if days_until_expiry == 1:
        return "1 day"
    if days_until_expiry < 14:
        return f"{days_until_expiry} days"
    if days_until_expiry < 60:
        weeks = round(days_until_expiry / 7)
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    if days_until_expiry < 365:
        months = round(days_until_expiry / 30)
        return f"{months} month{'s' if months != 1 else ''}"
    years = round(days_until_expiry / 365)
    return f"{years} year{'s' if years != 1 else ''}"
