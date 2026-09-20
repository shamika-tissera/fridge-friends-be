"""Ask an LLM what a dish is made of, and return validated JSON.

Talks to any OpenAI-compatible endpoint (configured for NVIDIA NIM running
`openai/gpt-oss-20b`). Requests are streamed: some models on this endpoint
take a minute or more, and a non-streamed call sits idle on an open socket
until an intermediate proxy closes it.

The model's JSON is never trusted — it is validated against a Pydantic schema
before it reaches the database.
"""

import json
import logging
import os
import re
from functools import lru_cache

import openai
from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
# Observed successful calls take 5-40s. The endpoint does occasionally hang
# outright, and /recipes/suggest is a request-path call — a generous timeout
# there means a user waiting minutes for an error. Fail fast instead.
TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))

CATEGORIES = ("produce", "protein", "dairy", "grain", "pantry", "spice", "other")

SYSTEM_PROMPT = f"""You list the ingredients of dishes for a grocery-tracking app.

Return ONLY a JSON object of this shape, with no prose and no markdown fence:

{{"foods": [{{"food": "<dish name exactly as given>",
             "cuisine": "<short label, e.g. Thai, Italian, West African>",
             "ingredients": [{{"name": "<lowercase shopping name>",
                              "category": "<one of: {', '.join(CATEGORIES)}>",
                              "essential": true}}]}}]}}

Rules:
- One entry per dish given, echoing the dish name back exactly.
- Use everyday shopping names ("chicken thighs", not "poultry, dark meat").
- 5-15 ingredients per dish. Include core spices; skip salt, pepper and water.
- "essential" is true if the dish is not recognisable without the ingredient.
- If a dish name is unclear or is not a food, return it with an empty
  ingredients list rather than guessing."""


class Ingredient(BaseModel):
    name: str
    category: str
    essential: bool = True


class FoodIngredients(BaseModel):
    food: str
    cuisine: str = ""
    ingredients: list[Ingredient] = Field(default_factory=list)


class IngredientExtraction(BaseModel):
    """The exact shape the model is asked to return."""

    foods: list[FoodIngredients] = Field(default_factory=list)


class LLMUnavailable(RuntimeError):
    """The model could not be reached, or returned something unusable."""


@lru_cache(maxsize=1)
def get_client() -> openai.OpenAI:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise LLMUnavailable("LLM_API_KEY is not set")
    # Retries are handled here rather than by us: the call is expensive and slow,
    # so one built-in retry is worth more than a hand-rolled backoff loop.
    return openai.OpenAI(base_url=BASE_URL, api_key=api_key, timeout=TIMEOUT, max_retries=1)


def strip_fence(text: str) -> str:
    """Drop a ```json ... ``` wrapper if the model added one anyway."""
    fenced = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    return fenced.group(1) if fenced else text.strip()


def call_model(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Stream one completion and return the assembled text."""
    try:
        stream = get_client().chat.completions.create(
            model=MODEL,
            temperature=0,          # ingredient lists should not vary run to run
            max_tokens=4096,
            stream=True,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        parts: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            # Reasoning models emit `reasoning_content` deltas too; only the
            # `content` deltas are the answer.
            piece = chunk.choices[0].delta.content
            if piece:
                parts.append(piece)
    except openai.AuthenticationError as exc:
        raise LLMUnavailable("LLM rejected the API key") from exc
    except openai.RateLimitError as exc:
        raise LLMUnavailable("LLM rate limit reached — try again shortly") from exc
    except openai.APIStatusError as exc:
        raise LLMUnavailable(f"LLM returned HTTP {exc.status_code}") from exc
    except openai.APIError as exc:
        raise LLMUnavailable(f"Could not reach the LLM: {type(exc).__name__}") from exc

    text = "".join(parts).strip()
    if not text:
        raise LLMUnavailable("LLM returned an empty response")
    return text


def extract_ingredients(foods: list[str]) -> dict[str, FoodIngredients]:
    """Map each dish name to its ingredients, in one call for the whole list.

    Raises LLMUnavailable if the call fails or the reply will not validate.
    """
    if not foods:
        return {}

    listing = "\n".join(f"- {name}" for name in foods)
    raw = call_model(f"List the ingredients for each of these dishes:\n{listing}")

    try:
        payload = json.loads(strip_fence(raw))
    except json.JSONDecodeError as exc:
        logger.warning("LLM returned non-JSON: %.200s", raw)
        raise LLMUnavailable("LLM did not return valid JSON") from exc

    try:
        parsed = IngredientExtraction.model_validate(payload)
    except ValidationError as exc:
        logger.warning("LLM JSON failed validation: %s", exc)
        raise LLMUnavailable("LLM JSON did not match the expected shape") from exc

    # Match on a normalised name — the model echoes the dish back, but its
    # casing and spacing are not worth trusting as a dictionary key.
    by_name = {e.food.strip().lower(): e for e in parsed.foods}
    return {n: by_name[n.strip().lower()] for n in foods if n.strip().lower() in by_name}


# ---------------------------------------------------------------- recipes ----
RECIPE_SYSTEM_PROMPT = """You suggest recipes for a grocery-tracking app, based
on what people already have in their kitchens.

Return ONLY a JSON object of this shape, with no prose and no markdown fence:

{"recipes": [{"name": "<dish name>",
              "cuisine": "<short label>",
              "uses": ["<ingredient from the available list>"],
              "missing": ["<ingredient they would still need to buy>"],
              "uses_expiring": ["<available ingredient marked EXPIRING>"],
              "prep_minutes": <whole minutes of hands-on prep>,
              "cook_minutes": <whole minutes of cooking, 0 if none>,
              "why": "<one short sentence>"}]}

Rules:
- Build recipes mainly from the AVAILABLE ingredients. Assume salt, pepper,
  water, oil and basic heat are always on hand; never list those as missing.
- Prefer recipes that use ingredients marked EXPIRING — they get thrown away
  otherwise.
- NEVER suggest a dish from the DISLIKED list, or an obvious restatement of one.
- If a dish from the LIKED list can be made, put it first.
- "missing" should be short. A recipe needing more than about three missing
  ingredients is not a useful suggestion.
- Suggest real, cookable dishes. Do not invent a dish to use up an odd
  ingredient.
- "prep_minutes" is hands-on time (chopping, mixing); "cook_minutes" is time
  on the heat or in the oven. Both are whole numbers for a typical home cook."""


class Recipe(BaseModel):
    name: str
    cuisine: str = ""
    uses: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    uses_expiring: list[str] = Field(default_factory=list)
    # Optional: a missing or absurd time is better dropped than shown, so these
    # stay None rather than failing the whole batch of suggestions.
    prep_minutes: Optional[int] = None
    cook_minutes: Optional[int] = None
    why: str = ""

    @field_validator("prep_minutes", "cook_minutes", mode="before")
    @classmethod
    def sane_minutes(cls, value):
        """Drop times that cannot be right — a day-long 'prep' is a bad parse."""
        if value is None:
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return None
        return minutes if 0 <= minutes <= 480 else None


class RecipeSuggestions(BaseModel):
    recipes: list[Recipe] = Field(default_factory=list)


def suggest_recipes(
    *,
    available: list[str],
    expiring: list[str],
    liked: list[str],
    disliked: list[str],
    limit: int,
) -> list[Recipe]:
    """Ask the model for recipes cookable from `available`.

    The like/dislike rules are *also* enforced by the caller — a prompt is not
    a guarantee, and "never suggest something they dislike" has to hold even
    when the model ignores the instruction.
    """
    if not available:
        return []

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (none)"

    marked = [f"{name} [EXPIRING]" if name in set(expiring) else name for name in available]

    prompt = (
        f"Suggest up to {limit} recipes.\n\n"
        f"AVAILABLE ingredients:\n{bullets(marked)}\n\n"
        f"LIKED dishes (prefer these if they can be made):\n{bullets(liked)}\n\n"
        f"DISLIKED dishes (never suggest these):\n{bullets(disliked)}"
    )

    raw = call_model(prompt, system=RECIPE_SYSTEM_PROMPT)

    try:
        payload = json.loads(strip_fence(raw))
    except json.JSONDecodeError as exc:
        logger.warning("recipe reply was not JSON: %.200s", raw)
        raise LLMUnavailable("LLM did not return valid JSON") from exc

    try:
        return RecipeSuggestions.model_validate(payload).recipes
    except ValidationError as exc:
        logger.warning("recipe JSON failed validation: %s", exc)
        raise LLMUnavailable("LLM JSON did not match the expected shape") from exc
