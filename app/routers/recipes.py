"""What can these users cook together, right now, from what they already have."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.database import get_session
from app.llm import LLMUnavailable, suggest_recipes
from app.routers.users import get_user_or_404
from app.schemas import RecipeIngredient, RecipeRead, RecipeRequest, RecipeResponse
from app.services import normalise, pantry_for, preferences_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])


@router.post("/suggest", response_model=RecipeResponse)
def suggest(payload: RecipeRequest, session: Session = Depends(get_session)):
    """Suggest recipes cookable from the pooled pantries of the given users.

    Dishes disliked by *any* of them are excluded, and dishes liked by any of
    them are ranked first. If nothing liked can be made, other cookable recipes
    are returned instead, with `detail` saying so.
    """
    user_ids = list(dict.fromkeys(payload.user_ids))  # de-duplicate, keep order
    users = [get_user_or_404(uid, session) for uid in user_ids]

    pantry = pantry_for(
        session, user_ids, expiring_within_days=payload.expiring_within_days
    )
    available = [entry.name for entry in pantry.values()]
    expiring = [entry.name for entry in pantry.values() if entry.expiring]
    liked, disliked = preferences_for(session, user_ids)

    if not available:
        return RecipeResponse(
            recipes=[],
            considered_users=[u.name for u in users],
            available_ingredients=0,
            expiring_ingredients=[],
            liked_matches=0,
            detail="Nobody has any unexpired, unconsumed groceries to cook with",
        )

    try:
        raw_recipes = suggest_recipes(
            available=available,
            expiring=expiring,
            liked=liked,
            disliked=disliked,
            limit=payload.max_results,
        )
    except LLMUnavailable as exc:
        logger.warning("recipe suggestion failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # --- enforce the rules here, rather than trusting the prompt ---------
    # A prompt is guidance; "never suggest a disliked dish" has to hold even
    # when the model ignores it, so the exclusion is applied again server-side.
    disliked_keys = {normalise(d) for d in disliked}
    liked_keys = {normalise(name): name for name in liked}
    expiring_keys = {normalise(name): name for name in expiring}

    # Which user likes which dish, for the `liked_by` field.
    likers: dict[str, list[str]] = {}
    for user in users:
        user_liked, _ = preferences_for(session, [user.id])
        for name in user_liked:
            likers.setdefault(normalise(name), []).append(user.name)

    kept: list[RecipeRead] = []
    used_recipes = []   # the model's version of each kept recipe, index-aligned
    seen: set[str] = set()
    for recipe in raw_recipes:
        key = normalise(recipe.name)
        if key in disliked_keys or key in seen:
            continue
        seen.add(key)
        used_recipes.append(recipe)
        total = (
            recipe.prep_minutes + recipe.cook_minutes
            if recipe.prep_minutes is not None and recipe.cook_minutes is not None
            else None
        )
        kept.append(
            RecipeRead(
                rank=0,          # assigned after sorting
                rank_reason="",
                name=recipe.name,
                cuisine=recipe.cuisine or None,
                uses=[],       # filled in below, from the pantry
                missing=recipe.missing,
                # Derived, not taken from the model: it happily reports a
                # year-away pantry staple as "expiring", and this field drives
                # the ranking below.
                uses_expiring=[
                    expiring_keys[k]
                    for k in dict.fromkeys(
                        normalise(u) for u in recipe.uses + recipe.uses_expiring
                    )
                    if k in expiring_keys
                ],
                prep_minutes=recipe.prep_minutes,
                cook_minutes=recipe.cook_minutes,
                total_minutes=total,
                why=recipe.why or None,
                liked_by=likers.get(key, []),
                is_liked=key in liked_keys,
            )
        )

    # `uses` comes back as bare names from the model. Resolve each against the
    # pantry: that drops anything invented (nobody has it) and attaches who
    # actually holds the item, which the model has no way to know.
    for item, recipe in zip(kept, used_recipes):
        resolved: list[RecipeIngredient] = []
        for key in dict.fromkeys(normalise(u) for u in recipe.uses):
            entry = pantry.get(key)
            if entry is None:
                continue
            resolved.append(
                RecipeIngredient(
                    name=entry.name, from_users=list(entry.owners), expiring=entry.expiring
                )
            )
        item.uses = resolved
        item.contributors = list(
            dict.fromkeys(name for ing in resolved for name in ing.from_users)
        )

    # Liked dishes first, then the ones that use up something expiring, then
    # the quickest. Sort the pairs so `used_recipes` stays index-aligned.
    paired = sorted(
        zip(kept, used_recipes),
        key=lambda pair: (
            not pair[0].is_liked,
            -len(pair[0].uses_expiring),
            pair[0].total_minutes if pair[0].total_minutes is not None else 10**6,
        ),
    )[: payload.max_results]
    kept = [item for item, _ in paired]
    used_recipes = [recipe for _, recipe in paired]

    # Rank is the position after the sort above, and `rank_reason` names the
    # rule that earned it — the same three keys, in the same priority order.
    for position, item in enumerate(kept, start=1):
        item.rank = position
        if item.is_liked:
            item.rank_reason = "Liked by " + ", ".join(item.liked_by)
        elif item.uses_expiring:
            item.rank_reason = "Uses expiring: " + ", ".join(item.uses_expiring)
        elif item.total_minutes is not None:
            item.rank_reason = f"Cookable in {item.total_minutes} min"
        else:
            item.rank_reason = "Cookable from what you have"

    liked_matches = sum(1 for r in kept if r.is_liked)
    detail = None
    if not kept:
        detail = "No suitable recipes could be suggested from these ingredients"
    elif liked_matches == 0:
        detail = (
            "None of the liked dishes could be made from these ingredients — "
            "showing other options instead"
        )

    return RecipeResponse(
        recipes=kept,
        considered_users=[u.name for u in users],
        available_ingredients=len(available),
        expiring_ingredients=expiring,
        liked_matches=liked_matches,
        detail=detail,
    )
