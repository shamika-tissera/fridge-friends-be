"""Turning a user's diets and allergens into something a recipe can be judged against.

The onboarding screen promises allergens "never show up in your recipes, or in
Feast recipes with friends". A prompt cannot carry that promise on its own — the
model forgets, or calls tahini something else — so every suggestion is also
checked here, against the keyword lists below, before it reaches the client.

Group rule: constraints are pooled and every one applies. One person's peanut
allergy rules peanuts out of the shared meal, and the strictest diet in the
group is the one the meal has to satisfy.
"""

from sqlmodel import Session, col, select

from app.models import Allergen, Diet, User

# Ingredient words that mean the allergen is present. Deliberately broad:
# a false positive drops one suggestion, a false negative serves someone
# the thing they are allergic to.
ALLERGEN_KEYWORDS: dict[Allergen, tuple[str, ...]] = {
    Allergen.peanuts: ("peanut", "groundnut", "satay", "arachis"),
    Allergen.shellfish: (
        "shellfish", "shrimp", "prawn", "crab", "lobster", "crayfish",
        "scallop", "mussel", "clam", "oyster", "squid", "calamari", "octopus",
    ),
    Allergen.tree_nuts: (
        "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut",
        "macadamia", "brazil nut", "pine nut", "nutella", "marzipan", "praline",
    ),
    Allergen.sesame: ("sesame", "tahini", "halva", "za'atar", "zaatar"),
}

_MEAT = (
    "beef", "pork", "chicken", "turkey", "duck", "lamb", "mutton", "veal",
    "bacon", "ham", "sausage", "salami", "chorizo", "prosciutto", "pepperoni",
    "mince", "steak", "gelatin", "lard", "anchovy paste",
)
_FISH = (
    "fish", "salmon", "tuna", "cod", "haddock", "sardine", "anchovy", "mackerel",
    "trout", "prawn", "shrimp", "crab", "lobster", "scallop", "mussel", "clam",
    "oyster", "squid", "calamari", "fish sauce", "oyster sauce",
)
_DAIRY = (
    "milk", "butter", "cheese", "cream", "yoghurt", "yogurt", "ghee",
    "mozzarella", "parmesan", "feta", "ricotta", "custard", "paneer",
)
_EGG = ("egg", "mayonnaise", "meringue", "aioli")
_GLUTEN = (
    "wheat", "flour", "bread", "pasta", "noodle", "couscous", "barley", "rye",
    "semolina", "breadcrumb", "tortilla", "pastry", "soy sauce", "seitan",
    "cracker", "bulgur", "farro",
)

DIET_EXCLUSIONS: dict[Diet, tuple[str, ...]] = {
    Diet.vegetarian: _MEAT + _FISH,
    Diet.vegan: _MEAT + _FISH + _DAIRY + _EGG + ("honey",),
    Diet.pescatarian: _MEAT,
    Diet.gluten_free: _GLUTEN,
    Diet.dairy_free: _DAIRY,
}

DIET_INSTRUCTIONS: dict[Diet, str] = {
    Diet.vegetarian: "no meat and no fish or seafood",
    Diet.vegan: "no animal products at all (no meat, fish, dairy, egg or honey)",
    Diet.pescatarian: "no meat, though fish and seafood are fine",
    Diet.gluten_free: "no gluten (no wheat, barley, rye, regular pasta, bread or soy sauce)",
    Diet.dairy_free: "no dairy (no milk, butter, cheese, cream or yoghurt)",
}


class DietaryRules:
    """The pooled constraints for one group, ready to prompt with and filter on."""

    def __init__(self, diets: list[Diet], allergens: list[Allergen]):
        self.diets = diets
        self.allergens = allergens
        self.banned: tuple[str, ...] = tuple(
            dict.fromkeys(
                [w for d in diets for w in DIET_EXCLUSIONS.get(d, ())]
                + [w for a in allergens for w in ALLERGEN_KEYWORDS.get(a, ())]
            )
        )

    def __bool__(self) -> bool:
        return bool(self.diets or self.allergens)

    def violations(self, texts: list[str]) -> list[str]:
        """Which banned words appear in the given ingredient/dish names."""
        haystack = " | ".join(t.lower() for t in texts if t)
        return [word for word in self.banned if word in haystack]

    def allows(self, texts: list[str]) -> bool:
        return not self.violations(texts)

    def prompt_lines(self) -> list[str]:
        """The hard rules, phrased for the recipe prompt."""
        lines: list[str] = []
        if self.allergens:
            names = ", ".join(a.value.replace("_", " ") for a in self.allergens)
            lines.append(
                f"ALLERGIES (absolute, someone gets ill otherwise): the dish must "
                f"contain no {names} in any form, including sauces and garnishes."
            )
        for diet in self.diets:
            lines.append(f"DIET: {DIET_INSTRUCTIONS[diet]}.")
        return lines


def rules_for(session: Session, user_ids: list[int]) -> DietaryRules:
    """Pool the diets and allergens of everyone eating."""
    users = session.exec(select(User).where(col(User.id).in_(user_ids))).all()
    diets: list[Diet] = []
    allergens: list[Allergen] = []
    for user in users:
        for value in user.diets or []:
            diet = Diet(value)
            if diet not in diets:
                diets.append(diet)
        for value in user.avoid_allergens or []:
            allergen = Allergen(value)
            if allergen not in allergens:
                allergens.append(allergen)
    return DietaryRules(diets, allergens)


def cuisines_for(session: Session, user_ids: list[int]) -> list[str]:
    """Favourite cuisines pooled across the group, in the order first seen."""
    users = session.exec(select(User).where(col(User.id).in_(user_ids))).all()
    pooled: list[str] = []
    for user in users:
        for cuisine in user.favorite_cuisines or []:
            if cuisine not in pooled:
                pooled.append(cuisine)
    return pooled
