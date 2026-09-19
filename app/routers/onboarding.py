"""Onboarding: the user picks foods they like, we store them with ingredients.

The ingredient lookup runs against a reasoning model that regularly takes over
a minute, so it does **not** happen inside the request. The user's picks are
saved immediately and enrichment runs in the background; the client polls the
list endpoint (or watches `ingredient_status`) until the rows turn `ready`.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session, col, select

from app import database
from app.database import get_session
from app.llm import LLMUnavailable, extract_ingredients
from app.models import FavoriteFood, IngredientStatus
from app.routers.users import get_user_or_404
from app.schemas import FavoriteFoodCreate, FavoriteFoodRead, OnboardingResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/{user_id}/favorite-foods", tags=["onboarding"])


def enrich_foods(food_ids: list[int]) -> None:
    """Fetch ingredients for the given rows and store them. Runs in the background.

    Opens its own session: the request's session is already closed by the time
    a background task runs. Never raises — a failure marks the rows `failed`,
    which leaves them retryable instead of losing the user's picks.
    """
    if not food_ids:
        return

    # Resolved at call time, not import time, so tests can swap the engine.
    with Session(database.engine) as session:
        rows = session.exec(
            select(FavoriteFood).where(col(FavoriteFood.id).in_(food_ids))
        ).all()
        if not rows:
            return

        try:
            extracted = extract_ingredients([row.name for row in rows])
        except LLMUnavailable as exc:
            logger.warning("ingredient lookup failed for %s: %s", food_ids, exc)
            for row in rows:
                row.ingredient_status = IngredientStatus.failed
                row.ingredient_error = str(exc)[:255]
                session.add(row)
            session.commit()
            return

        now = datetime.now(timezone.utc)
        for row in rows:
            entry = extracted.get(row.name)
            if entry is None:
                # The model returned nothing for this dish — unrecognised, not broken.
                row.ingredient_status = IngredientStatus.failed
                row.ingredient_error = "No ingredients returned for this food"
            else:
                row.cuisine = entry.cuisine or None
                row.ingredients = [i.model_dump() for i in entry.ingredients]
                row.ingredient_status = IngredientStatus.ready
                row.ingredient_error = None
                row.ingredients_updated_at = now
            session.add(row)
        session.commit()
        logger.info("enriched %d favorite foods", len(rows))


@router.post("", response_model=OnboardingResult, status_code=status.HTTP_202_ACCEPTED)
def confirm_favorite_foods(
    user_id: int,
    payload: FavoriteFoodCreate,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Confirm the onboarding screen: save the picks, look up ingredients after.

    Returns 202 — the foods are saved, and each comes back `pending` until the
    background lookup fills in its ingredients.
    """
    get_user_or_404(user_id, session)

    # Normalise whitespace and drop duplicates within the request itself.
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in payload.foods:
        name = " ".join(raw.split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            wanted.append(name)

    if not wanted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No usable food names given")

    already = {
        row.name.lower()
        for row in session.exec(
            select(FavoriteFood).where(col(FavoriteFood.user_id) == user_id)
        ).all()
    }

    new_rows = [FavoriteFood(user_id=user_id, name=n) for n in wanted if n.lower() not in already]
    skipped = [n for n in wanted if n.lower() in already]

    for row in new_rows:
        session.add(row)
    session.commit()
    for row in new_rows:
        session.refresh(row)

    background.add_task(enrich_foods, [row.id for row in new_rows])

    return OnboardingResult(
        saved=[FavoriteFoodRead.model_validate(r) for r in new_rows],
        skipped=skipped,
        ingredients_pending=len(new_rows),
    )


@router.get("", response_model=list[FavoriteFoodRead])
def list_favorite_foods(user_id: int, session: Session = Depends(get_session)):
    """The user's foods and their ingredients. Poll this after onboarding."""
    get_user_or_404(user_id, session)
    return session.exec(
        select(FavoriteFood)
        .where(col(FavoriteFood.user_id) == user_id)
        .order_by(col(FavoriteFood.id).asc())
    ).all()


def get_food_or_404(user_id: int, food_id: int, session: Session) -> FavoriteFood:
    food = session.get(FavoriteFood, food_id)
    if food is None or food.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Favorite food {food_id} not found")
    return food


@router.post(
    "/{food_id}/refresh-ingredients",
    response_model=FavoriteFoodRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def refresh_ingredients(
    user_id: int,
    food_id: int,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Re-ask the model for one food's ingredients — the retry for a `failed` row."""
    food = get_food_or_404(user_id, food_id, session)
    food.ingredient_status = IngredientStatus.pending
    food.ingredient_error = None
    session.add(food)
    session.commit()
    session.refresh(food)

    background.add_task(enrich_foods, [food.id])
    return food


@router.delete("/{food_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite_food(
    user_id: int, food_id: int, session: Session = Depends(get_session)
):
    session.delete(get_food_or_404(user_id, food_id, session))
    session.commit()
