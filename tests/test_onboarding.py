import json

import pytest

from app import llm
from app.models import IngredientStatus
from app.routers import onboarding

PAD_THAI = {
    "foods": [
        {
            "food": "Pad Thai",
            "cuisine": "Thai",
            "ingredients": [
                {"name": "rice noodles", "category": "grain", "essential": True},
                {"name": "tamarind paste", "category": "pantry", "essential": True},
                {"name": "beansprouts", "category": "produce", "essential": False},
            ],
        },
        {
            "food": "Jollof Rice",
            "cuisine": "West African",
            "ingredients": [
                {"name": "long grain rice", "category": "grain", "essential": True},
                {"name": "scotch bonnet", "category": "produce", "essential": True},
            ],
        },
    ]
}


@pytest.fixture
def fake_model(monkeypatch):
    """Stub the network call, keeping the real JSON parsing and validation."""
    calls = []

    def _fake(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(PAD_THAI)

    monkeypatch.setattr(llm, "call_model", _fake)
    return calls


def test_extract_ingredients_parses_model_json(fake_model):
    out = llm.extract_ingredients(["Pad Thai", "Jollof Rice"])
    assert set(out) == {"Pad Thai", "Jollof Rice"}
    assert out["Pad Thai"].cuisine == "Thai"
    assert [i.name for i in out["Pad Thai"].ingredients][0] == "rice noodles"
    assert len(fake_model) == 1, "all dishes should go in one call"


def test_extract_ingredients_matches_case_insensitively(fake_model):
    out = llm.extract_ingredients(["pad thai"])
    assert "pad thai" in out


def test_extract_ingredients_strips_markdown_fence(monkeypatch):
    monkeypatch.setattr(llm, "call_model", lambda p: "```json\n" + json.dumps(PAD_THAI) + "\n```")
    assert "Pad Thai" in llm.extract_ingredients(["Pad Thai"])


def test_extract_ingredients_rejects_non_json(monkeypatch):
    monkeypatch.setattr(llm, "call_model", lambda p: "Sure! Here are the ingredients:")
    with pytest.raises(llm.LLMUnavailable, match="valid JSON"):
        llm.extract_ingredients(["Pad Thai"])


def test_extract_ingredients_rejects_wrong_shape(monkeypatch):
    monkeypatch.setattr(llm, "call_model", lambda p: json.dumps({"foods": [{"nope": 1}]}))
    with pytest.raises(llm.LLMUnavailable, match="expected shape"):
        llm.extract_ingredients(["Pad Thai"])


def test_no_foods_makes_no_call(monkeypatch):
    def _boom(prompt):
        raise AssertionError("should not call the model")

    monkeypatch.setattr(llm, "call_model", _boom)
    assert llm.extract_ingredients([]) == {}


# ---------- endpoint behaviour ----------
def test_onboarding_saves_and_enriches(client, make_user, fake_model):
    uid = make_user("onboard@example.com")
    resp = client.post(
        f"/users/{uid}/favorite-foods", json={"foods": ["Pad Thai", "Jollof Rice"]}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [f["name"] for f in body["saved"]] == ["Pad Thai", "Jollof Rice"]
    assert body["ingredients_pending"] == 2

    # TestClient runs background tasks before returning, so enrichment is done.
    foods = client.get(f"/users/{uid}/favorite-foods").json()
    assert all(f["ingredient_status"] == IngredientStatus.ready for f in foods)
    pad_thai = next(f for f in foods if f["name"] == "Pad Thai")
    assert pad_thai["cuisine"] == "Thai"
    assert {i["name"] for i in pad_thai["ingredients"]} == {
        "rice noodles", "tamarind paste", "beansprouts"
    }
    assert pad_thai["ingredients_updated_at"] is not None


def test_onboarding_survives_llm_failure(client, make_user, monkeypatch):
    """A dead LLM must not lose the user's picks."""
    def _down(prompt):
        raise llm.LLMUnavailable("LLM rejected the API key")

    monkeypatch.setattr(llm, "call_model", _down)
    uid = make_user("llmdown@example.com")

    resp = client.post(f"/users/{uid}/favorite-foods", json={"foods": ["Pad Thai"]})
    assert resp.status_code == 202

    food = client.get(f"/users/{uid}/favorite-foods").json()[0]
    assert food["name"] == "Pad Thai"                       # still saved
    assert food["ingredient_status"] == IngredientStatus.failed
    assert "API key" in food["ingredient_error"]


def test_refresh_ingredients_retries(client, make_user, monkeypatch):
    uid = make_user("retry@example.com")
    monkeypatch.setattr(llm, "call_model", lambda p: (_ for _ in ()).throw(llm.LLMUnavailable("down")))
    client.post(f"/users/{uid}/favorite-foods", json={"foods": ["Pad Thai"]})
    food_id = client.get(f"/users/{uid}/favorite-foods").json()[0]["id"]

    monkeypatch.setattr(llm, "call_model", lambda p: json.dumps(PAD_THAI))
    resp = client.post(f"/users/{uid}/favorite-foods/{food_id}/refresh-ingredients")
    assert resp.status_code == 202

    food = client.get(f"/users/{uid}/favorite-foods").json()[0]
    assert food["ingredient_status"] == IngredientStatus.ready
    assert food["ingredient_error"] is None


def test_unknown_food_marked_failed_not_ready(client, make_user, monkeypatch):
    """A dish the model returns nothing for should not look successfully empty."""
    monkeypatch.setattr(llm, "call_model", lambda p: json.dumps({"foods": []}))
    uid = make_user("unknown@example.com")
    client.post(f"/users/{uid}/favorite-foods", json={"foods": ["Glorbf"]})

    food = client.get(f"/users/{uid}/favorite-foods").json()[0]
    assert food["ingredient_status"] == IngredientStatus.failed
    assert food["ingredients"] == []


def test_duplicate_foods_are_skipped(client, make_user, fake_model):
    uid = make_user("dupes@example.com")
    client.post(f"/users/{uid}/favorite-foods", json={"foods": ["Pad Thai"]})
    resp = client.post(
        f"/users/{uid}/favorite-foods", json={"foods": ["  pad   thai ", "Jollof Rice"]}
    )
    body = resp.json()
    assert body["skipped"] == ["pad thai"]
    assert [f["name"] for f in body["saved"]] == ["Jollof Rice"]
    assert len(client.get(f"/users/{uid}/favorite-foods").json()) == 2


def test_foods_are_deleted_with_the_user(client, make_user, fake_model, session):
    from sqlmodel import select
    from app.models import FavoriteFood

    uid = make_user("cascadefood@example.com")
    client.post(f"/users/{uid}/favorite-foods", json={"foods": ["Pad Thai"]})
    assert client.delete(f"/users/{uid}").status_code == 204
    assert session.exec(select(FavoriteFood)).all() == []
