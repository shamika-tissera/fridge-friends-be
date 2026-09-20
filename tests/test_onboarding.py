import json

import pytest

from app import llm
from app.models import IngredientStatus, Preference
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
        f"/users/{uid}/food-preferences", json={"likes": ["Pad Thai", "Jollof Rice"]}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [f["name"] for f in body["saved"]] == ["Pad Thai", "Jollof Rice"]
    assert body["ingredients_pending"] == 2

    # TestClient runs background tasks before returning, so enrichment is done.
    foods = client.get(f"/users/{uid}/food-preferences").json()
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

    resp = client.post(f"/users/{uid}/food-preferences", json={"likes": ["Pad Thai"]})
    assert resp.status_code == 202

    food = client.get(f"/users/{uid}/food-preferences").json()[0]
    assert food["name"] == "Pad Thai"                       # still saved
    assert food["ingredient_status"] == IngredientStatus.failed
    assert "API key" in food["ingredient_error"]


def test_refresh_ingredients_retries(client, make_user, monkeypatch):
    uid = make_user("retry@example.com")
    monkeypatch.setattr(llm, "call_model", lambda p: (_ for _ in ()).throw(llm.LLMUnavailable("down")))
    client.post(f"/users/{uid}/food-preferences", json={"likes": ["Pad Thai"]})
    food_id = client.get(f"/users/{uid}/food-preferences").json()[0]["id"]

    monkeypatch.setattr(llm, "call_model", lambda p: json.dumps(PAD_THAI))
    resp = client.post(f"/users/{uid}/food-preferences/{food_id}/refresh-ingredients")
    assert resp.status_code == 202

    food = client.get(f"/users/{uid}/food-preferences").json()[0]
    assert food["ingredient_status"] == IngredientStatus.ready
    assert food["ingredient_error"] is None


def test_unknown_food_marked_failed_not_ready(client, make_user, monkeypatch):
    """A dish the model returns nothing for should not look successfully empty."""
    monkeypatch.setattr(llm, "call_model", lambda p: json.dumps({"foods": []}))
    uid = make_user("unknown@example.com")
    client.post(f"/users/{uid}/food-preferences", json={"likes": ["Glorbf"]})

    food = client.get(f"/users/{uid}/food-preferences").json()[0]
    assert food["ingredient_status"] == IngredientStatus.failed
    assert food["ingredients"] == []


def test_duplicate_foods_are_skipped(client, make_user, fake_model):
    uid = make_user("dupes@example.com")
    client.post(f"/users/{uid}/food-preferences", json={"likes": ["Pad Thai"]})
    resp = client.post(
        f"/users/{uid}/food-preferences", json={"likes": ["  pad   thai ", "Jollof Rice"]}
    )
    body = resp.json()
    assert body["skipped"] == ["pad thai"]
    assert [f["name"] for f in body["saved"]] == ["Jollof Rice"]
    assert len(client.get(f"/users/{uid}/food-preferences").json()) == 2


def test_preferences_are_deleted_with_the_user(client, make_user, fake_model, session):
    from sqlmodel import select
    from app.models import FoodPreference

    uid = make_user("cascadefood@example.com")
    client.post(f"/users/{uid}/food-preferences", json={"likes": ["Pad Thai"]})
    assert client.delete(f"/users/{uid}").status_code == 204
    assert session.exec(select(FoodPreference)).all() == []


# ---------- dislikes ----------
def test_stores_likes_and_dislikes(client, make_user, fake_model):
    uid = make_user("tastes@example.com")
    resp = client.post(
        f"/users/{uid}/food-preferences",
        json={"likes": ["Pad Thai"], "dislikes": ["Jollof Rice"]},
    )
    assert resp.status_code == 202
    assert resp.json()["ingredients_pending"] == 2

    foods = {f["name"]: f for f in client.get(f"/users/{uid}/food-preferences").json()}
    assert foods["Pad Thai"]["preference"] == Preference.like
    assert foods["Jollof Rice"]["preference"] == Preference.dislike
    # Dislikes get ingredients too — that is how a recipe gets ruled out.
    assert foods["Jollof Rice"]["ingredient_status"] == IngredientStatus.ready
    assert len(foods["Jollof Rice"]["ingredients"]) == 2


def test_filter_by_preference(client, make_user, fake_model):
    uid = make_user("filter@example.com")
    client.post(
        f"/users/{uid}/food-preferences",
        json={"likes": ["Pad Thai"], "dislikes": ["Jollof Rice"]},
    )
    likes = client.get(f"/users/{uid}/food-preferences?preference=like").json()
    dislikes = client.get(f"/users/{uid}/food-preferences?preference=dislike").json()
    assert [f["name"] for f in likes] == ["Pad Thai"]
    assert [f["name"] for f in dislikes] == ["Jollof Rice"]


def test_same_food_liked_and_disliked_keeps_first_mention(client, make_user, fake_model):
    """A contradictory pick must not create two rows for one food."""
    uid = make_user("conflict@example.com")
    resp = client.post(
        f"/users/{uid}/food-preferences",
        json={"likes": ["Pad Thai"], "dislikes": ["pad thai"]},
    )
    body = resp.json()
    assert [f["name"] for f in body["saved"]] == ["Pad Thai"]
    assert body["skipped"] == ["pad thai"]

    rows = client.get(f"/users/{uid}/food-preferences").json()
    assert len(rows) == 1
    assert rows[0]["preference"] == Preference.like


def test_dislikes_only_is_allowed(client, make_user, fake_model):
    uid = make_user("picky@example.com")
    resp = client.post(f"/users/{uid}/food-preferences", json={"dislikes": ["Jollof Rice"]})
    assert resp.status_code == 202
    assert client.get(f"/users/{uid}/food-preferences").json()[0]["preference"] == Preference.dislike


def test_empty_payload_is_rejected(client, make_user):
    uid = make_user("empty@example.com")
    resp = client.post(f"/users/{uid}/food-preferences", json={"likes": [], "dislikes": []})
    assert resp.status_code == 422
