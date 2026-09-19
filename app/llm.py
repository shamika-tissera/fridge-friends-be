"""Ask an LLM what a dish is made of, and return validated JSON.

Talks to any OpenAI-compatible endpoint (configured for NVIDIA NIM running
`z-ai/glm-5.3`). The model is a reasoning model and can take well over a
minute, so requests are streamed: a non-streamed call sits idle on an open
socket and gets closed by intermediate proxies before the answer arrives.

The model's JSON is never trusted — it is validated against a Pydantic schema
before it reaches the database.
"""

import json
import logging
import os
import re
from functools import lru_cache

import openai
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODEL = os.getenv("LLM_MODEL", "z-ai/glm-5.3")
TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))

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


def call_model(prompt: str) -> str:
    """Stream one completion and return the assembled text."""
    try:
        stream = get_client().chat.completions.create(
            model=MODEL,
            temperature=0,          # ingredient lists should not vary run to run
            max_tokens=4096,
            stream=True,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
