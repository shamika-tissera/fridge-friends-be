import json
from datetime import date, timedelta

import pytest

from app import llm

TODAY = date.today()


def iso(days):
    return (TODAY + timedelta(days=days)).isoformat()


def add_item(client, uid, name, days=None, consumed=False):
    body = {"name": name}
    if days is not None:
        body["expires_on"] = iso(days)
    r = client.post(f"/users/{uid}/grocery-items", json=body)
    assert r.status_code == 201
    item = r.json()
    if consumed:
        client.patch(f"/grocery-items/{item['id']}", json={"consumed": True})
    return item


def set_prefs(client, uid, likes=(), dislikes=()):
    return client.post(
        f"/users/{uid}/food-preferences",
        json={"likes": list(likes), "dislikes": list(dislikes)},
    )


@pytest.fixture
def ingredients_stub(monkeypatch):
    """Ingredient lookups succeed trivially, so preferences reach `ready`."""
    def _fake(prompt, system=None):
        return json.dumps({"foods": []})
    monkeypatch.setattr(llm, "call_model", _fake)


def stub_recipes(monkeypatch, recipes, capture=None):
    """Make the model return exactly `recipes`, recording the prompt it saw."""
    def _fake(prompt, system=None):
        if capture is not None:
            capture.append(prompt)
        if "AVAILABLE ingredients" in prompt:
            return json.dumps({"recipes": recipes})
        return json.dumps({"foods": []})
    monkeypatch.setattr(llm, "call_model", _fake)


def R(name, **kw):
    return {"name": name, "cuisine": kw.get("cuisine", "Test"),
            "uses": kw.get("uses", []), "missing": kw.get("missing", []),
            "uses_expiring": kw.get("uses_expiring", []), "why": kw.get("why", ""),
            "prep_minutes": kw.get("prep_minutes"), "cook_minutes": kw.get("cook_minutes")}


def test_suggests_from_pooled_pantries(client, make_user, monkeypatch):
    a = make_user("cook_a@example.com", "Ann")
    b = make_user("cook_b@example.com", "Ben")
    add_item(client, a, "rice", days=30)
    add_item(client, b, "chicken", days=2)
    stub_recipes(monkeypatch, [R("Chicken Rice", uses=["rice", "chicken"])])

    body = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Chicken Rice"]
    assert body["considered_users"] == ["Ann", "Ben"]
    assert body["available_ingredients"] == 2


def test_disliked_dish_is_never_returned(client, make_user, monkeypatch):
    """Enforced server-side: the model is told, but not trusted."""
    a = make_user("dis_a@example.com")
    b = make_user("dis_b@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Durian Rice"), R("Plain Rice")])
    set_prefs(client, b, dislikes=["Durian Rice"])   # disliked by the *other* user

    body = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Plain Rice"]


def test_dislike_beats_like_across_users(client, make_user, monkeypatch):
    a = make_user("clash_a@example.com")
    b = make_user("clash_b@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Congee"), R("Rice Pudding")])
    set_prefs(client, a, likes=["Congee"])
    set_prefs(client, b, dislikes=["Congee"])

    body = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Rice Pudding"]


def test_liked_dishes_rank_first(client, make_user, monkeypatch):
    a = make_user("like_a@example.com", "Ann")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Rice Pudding"), R("Congee")])
    set_prefs(client, a, likes=["Congee"])

    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Congee", "Rice Pudding"]
    assert body["recipes"][0]["is_liked"] is True
    assert body["recipes"][0]["liked_by"] == ["Ann"]
    assert body["liked_matches"] == 1
    assert body["detail"] is None


def test_falls_back_when_no_liked_dish_possible(client, make_user, monkeypatch):
    a = make_user("fall@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Rice Pudding")])
    set_prefs(client, a, likes=["Paella"])

    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Rice Pudding"]
    assert body["liked_matches"] == 0
    assert "showing other options" in body["detail"]


def test_expired_and_consumed_items_are_not_offered(client, make_user, monkeypatch):
    a = make_user("stale@example.com")
    add_item(client, a, "fresh rice", days=30)
    add_item(client, a, "old milk", days=-3)          # expired
    add_item(client, a, "eaten bread", days=5, consumed=True)
    captured = []
    stub_recipes(monkeypatch, [R("Rice Pudding")], capture=captured)

    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert body["available_ingredients"] == 1
    prompt = next(p for p in captured if "AVAILABLE" in p)
    assert "fresh rice" in prompt
    assert "old milk" not in prompt
    assert "eaten bread" not in prompt


def test_expiring_ingredients_are_flagged_to_the_model(client, make_user, monkeypatch):
    a = make_user("exp@example.com")
    add_item(client, a, "spinach", days=1)
    add_item(client, a, "rice", days=300)
    captured = []
    stub_recipes(monkeypatch, [R("Rice Pudding")], capture=captured)

    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert body["expiring_ingredients"] == ["spinach"]
    prompt = next(p for p in captured if "AVAILABLE" in p)
    assert "spinach [EXPIRING]" in prompt
    assert "rice [EXPIRING]" not in prompt


def test_recipes_using_expiring_rank_above_other_unliked(client, make_user, monkeypatch):
    a = make_user("rank@example.com")
    add_item(client, a, "spinach", days=1)
    stub_recipes(monkeypatch, [
        R("Plain Toast"),
        R("Spinach Soup", uses_expiring=["spinach"]),
    ])
    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Spinach Soup", "Plain Toast"]


def test_empty_pantry_returns_no_recipes(client, make_user, monkeypatch):
    a = make_user("bare@example.com")
    def _boom(prompt, system=None):
        raise AssertionError("should not call the model with an empty pantry")
    monkeypatch.setattr(llm, "call_model", _boom)

    body = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert body["recipes"] == []
    assert "groceries" in body["detail"]


def test_llm_failure_returns_503(client, make_user, monkeypatch):
    a = make_user("down@example.com")
    add_item(client, a, "rice", days=30)
    def _down(prompt, system=None):
        raise llm.LLMUnavailable("LLM rejected the API key")
    monkeypatch.setattr(llm, "call_model", _down)

    resp = client.post("/recipes/suggest", json={"user_ids": [a]})
    assert resp.status_code == 503
    assert "API key" in resp.json()["detail"]


def test_unknown_user_is_404(client, make_user, monkeypatch):
    a = make_user("known@example.com")
    stub_recipes(monkeypatch, [])
    assert client.post("/recipes/suggest", json={"user_ids": [a, 9999]}).status_code == 404


def test_duplicate_user_ids_are_collapsed(client, make_user, monkeypatch):
    a = make_user("dup@example.com", "Ann")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Rice Pudding")])
    body = client.post("/recipes/suggest", json={"user_ids": [a, a]}).json()
    assert body["considered_users"] == ["Ann"]


def test_max_results_is_respected(client, make_user, monkeypatch):
    a = make_user("cap@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R(f"Dish {i}") for i in range(8)])
    body = client.post("/recipes/suggest", json={"user_ids": [a], "max_results": 3}).json()
    assert len(body["recipes"]) == 3


def test_empty_user_ids_rejected(client):
    assert client.post("/recipes/suggest", json={"user_ids": []}).status_code == 422


def test_uses_expiring_is_derived_not_trusted(client, make_user, monkeypatch):
    """The model calls long-life staples 'expiring'; the server must not agree."""
    a = make_user("claims@example.com")
    add_item(client, a, "spinach", days=1)      # genuinely expiring
    add_item(client, a, "lentils", days=300)    # not remotely expiring
    stub_recipes(monkeypatch, [
        R("Dal", uses=["spinach", "lentils"], uses_expiring=["spinach", "lentils"]),
    ])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"][0]
    assert recipe["uses_expiring"] == ["spinach"]


def test_invented_ingredients_are_dropped_from_uses(client, make_user, monkeypatch):
    a = make_user("invent@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Risotto", uses=["rice", "saffron", "wine"])])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"][0]
    assert [u["name"] for u in recipe["uses"]] == ["rice"]


# ---------- whose ingredient is it, and how long does it take ----------
def test_uses_say_which_user_each_item_comes_from(client, make_user, monkeypatch):
    a = make_user("own_a@example.com", "Ann")
    b = make_user("own_b@example.com", "Ben")
    add_item(client, a, "rice", days=30)
    add_item(client, b, "chicken", days=2)
    stub_recipes(monkeypatch, [R("Chicken Rice", uses=["rice", "chicken"])])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()["recipes"][0]
    owners = {u["name"]: u["from_users"] for u in recipe["uses"]}
    assert owners == {"rice": ["Ann"], "chicken": ["Ben"]}
    assert recipe["contributors"] == ["Ann", "Ben"]


def test_shared_ingredient_lists_both_owners(client, make_user, monkeypatch):
    a = make_user("share_a@example.com", "Ann")
    b = make_user("share_b@example.com", "Ben")
    add_item(client, a, "Rice", days=30)
    add_item(client, b, "  rice ", days=30)      # same thing, different spelling
    stub_recipes(monkeypatch, [R("Plain Rice", uses=["rice"])])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()["recipes"][0]
    assert len(recipe["uses"]) == 1
    assert recipe["uses"][0]["from_users"] == ["Ann", "Ben"]
    assert recipe["contributors"] == ["Ann", "Ben"]


def test_contributors_exclude_users_who_supply_nothing(client, make_user, monkeypatch):
    a = make_user("has@example.com", "Ann")
    b = make_user("hasnt@example.com", "Ben")   # empty pantry
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Plain Rice", uses=["rice"])])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()["recipes"][0]
    assert recipe["contributors"] == ["Ann"]


def test_expiring_flag_travels_with_the_ingredient(client, make_user, monkeypatch):
    a = make_user("flag@example.com", "Ann")
    add_item(client, a, "spinach", days=1)
    add_item(client, a, "rice", days=300)
    stub_recipes(monkeypatch, [R("Spinach Rice", uses=["spinach", "rice"])])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"][0]
    flags = {u["name"]: u["expiring"] for u in recipe["uses"]}
    assert flags == {"spinach": True, "rice": False}


def test_prep_and_cook_times_are_returned_and_totalled(client, make_user, monkeypatch):
    a = make_user("time@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R("Plain Rice", uses=["rice"], prep_minutes=5, cook_minutes=20)])

    recipe = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"][0]
    assert (recipe["prep_minutes"], recipe["cook_minutes"], recipe["total_minutes"]) == (5, 20, 25)


def test_absurd_or_missing_times_become_null(client, make_user, monkeypatch):
    a = make_user("badtime@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [
        R("Silly", uses=["rice"], prep_minutes=99999, cook_minutes=10),
        R("Quiet", uses=["rice"]),
    ])
    by_name = {r["name"]: r for r in
               client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"]}
    assert by_name["Silly"]["prep_minutes"] is None
    assert by_name["Silly"]["total_minutes"] is None   # cannot total a missing half
    assert by_name["Quiet"]["prep_minutes"] is None


def test_quicker_recipe_wins_when_otherwise_equal(client, make_user, monkeypatch):
    a = make_user("quick@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [
        R("Slow Bake", uses=["rice"], prep_minutes=20, cook_minutes=90),
        R("Quick Fry", uses=["rice"], prep_minutes=5, cook_minutes=10),
    ])
    names = [r["name"] for r in
             client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"]]
    assert names == ["Quick Fry", "Slow Bake"]


def test_liked_still_beats_quicker(client, make_user, monkeypatch):
    """Speed is a tiebreak, not an override of the preference rule."""
    a = make_user("tiebreak@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [
        R("Instant Snack", uses=["rice"], prep_minutes=1, cook_minutes=0),
        R("Congee", uses=["rice"], prep_minutes=10, cook_minutes=60),
    ])
    set_prefs(client, a, likes=["Congee"])
    names = [r["name"] for r in
             client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"]]
    assert names == ["Congee", "Instant Snack"]


# ---------- ranking ----------
def test_rank_is_sequential_and_matches_order(client, make_user, monkeypatch):
    a = make_user("rank1@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R(f"Dish {i}", uses=["rice"]) for i in range(4)])

    recipes = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"]
    assert [r["rank"] for r in recipes] == [1, 2, 3, 4]


def test_rank_reason_names_the_rule_that_applied(client, make_user, monkeypatch):
    a = make_user("rank2@example.com", "Ann")
    add_item(client, a, "spinach", days=1)
    add_item(client, a, "rice", days=300)
    stub_recipes(monkeypatch, [
        R("Congee", uses=["rice"], prep_minutes=5, cook_minutes=30),
        R("Spinach Soup", uses=["spinach"]),
        R("Plain Rice", uses=["rice"], prep_minutes=2, cook_minutes=10),
    ])
    set_prefs(client, a, likes=["Congee"])

    recipes = client.post("/recipes/suggest", json={"user_ids": [a]}).json()["recipes"]
    assert [(r["rank"], r["name"]) for r in recipes] == [
        (1, "Congee"), (2, "Spinach Soup"), (3, "Plain Rice"),
    ]
    assert recipes[0]["rank_reason"] == "Liked by Ann"
    assert recipes[1]["rank_reason"] == "Uses expiring: spinach"
    assert recipes[2]["rank_reason"] == "Cookable in 12 min"


def test_rank_survives_the_max_results_trim(client, make_user, monkeypatch):
    a = make_user("rank3@example.com")
    add_item(client, a, "rice", days=30)
    stub_recipes(monkeypatch, [R(f"Dish {i}", uses=["rice"]) for i in range(9)])

    recipes = client.post("/recipes/suggest",
                          json={"user_ids": [a], "max_results": 2}).json()["recipes"]
    assert [r["rank"] for r in recipes] == [1, 2]
