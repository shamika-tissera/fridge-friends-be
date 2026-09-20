"""Allergens and diets: promised on the onboarding screen, enforced everywhere."""

from tests.test_recipes import R, add_item, stub_recipes


def set_profile(client, uid, **profile):
    resp = client.put(f"/users/{uid}/taste-profile", json=profile)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_allergen_recipe_is_dropped_even_when_the_model_returns_it(client, make_user, monkeypatch):
    """The prompt says no peanuts; this checks what happens when it is ignored."""
    uid = make_user("allergic@example.com", "Ali")
    set_profile(client, uid, avoid_allergens=["Peanuts"])
    add_item(client, uid, "noodles", days=30)
    add_item(client, uid, "peanut butter", days=90)
    stub_recipes(monkeypatch, [
        R("Satay Noodles", uses=["noodles", "peanut butter"]),
        R("Garlic Noodles", uses=["noodles"]),
    ])

    body = client.post("/recipes/suggest", json={"user_ids": [uid]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Garlic Noodles"]
    assert body["excluded_for_dietary_rules"] == 1
    assert body["dietary_rules_applied"] == ["ALLERGIES (absolute, someone gets ill otherwise)"]


def test_allergen_hidden_in_the_shopping_list_still_counts(client, make_user, monkeypatch):
    uid = make_user("sesame@example.com")
    set_profile(client, uid, avoid_allergens=["sesame"])
    add_item(client, uid, "chickpeas", days=400)
    stub_recipes(monkeypatch, [R("Hummus", uses=["chickpeas"], missing=["tahini"])])

    body = client.post("/recipes/suggest", json={"user_ids": [uid]}).json()
    assert body["recipes"] == []
    assert "allergies or diets" in body["detail"]


def test_one_persons_allergy_rules_out_the_shared_meal(client, make_user, monkeypatch):
    a = make_user("host@example.com", "Ann")
    b = make_user("guest@example.com", "Ben")
    set_profile(client, b, avoid_allergens=["Shellfish"])
    add_item(client, a, "rice", days=30)
    add_item(client, a, "prawns", days=2)
    stub_recipes(monkeypatch, [R("Prawn Fried Rice", uses=["rice", "prawns"])])

    alone = client.post("/recipes/suggest", json={"user_ids": [a]}).json()
    assert [r["name"] for r in alone["recipes"]] == ["Prawn Fried Rice"]

    together = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()
    assert together["recipes"] == []


def test_diets_are_pooled_at_their_strictest(client, make_user, monkeypatch):
    a = make_user("veg@example.com", "Ann")
    b = make_user("omni@example.com", "Ben")
    set_profile(client, a, diets=["Vegetarian"])
    add_item(client, b, "beef", days=3)
    add_item(client, b, "beans", days=400)
    stub_recipes(monkeypatch, [
        R("Beef Chilli", uses=["beef", "beans"]),
        R("Bean Chilli", uses=["beans"]),
    ])

    body = client.post("/recipes/suggest", json={"user_ids": [a, b]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Bean Chilli"]


def test_pescatarian_keeps_fish_and_drops_meat(client, make_user, monkeypatch):
    uid = make_user("pesc@example.com")
    set_profile(client, uid, diets=["Pescatarian"])
    add_item(client, uid, "salmon", days=2)
    add_item(client, uid, "chicken", days=2)
    add_item(client, uid, "rice", days=300)
    stub_recipes(monkeypatch, [
        R("Chicken Rice", uses=["chicken", "rice"]),
        R("Salmon Rice Bowl", uses=["salmon", "rice"]),
    ])

    body = client.post("/recipes/suggest", json={"user_ids": [uid]}).json()
    assert [r["name"] for r in body["recipes"]] == ["Salmon Rice Bowl"]


def test_rules_and_cuisines_reach_the_prompt(client, make_user, monkeypatch):
    uid = make_user("prompt@example.com")
    set_profile(client, uid, diets=["Vegan"], avoid_allergens=["Tree nuts"],
                favorite_cuisines=["Korean"])
    add_item(client, uid, "tofu", days=5)
    prompts = []
    stub_recipes(monkeypatch, [R("Tofu Bowl", uses=["tofu"])], capture=prompts)

    client.post("/recipes/suggest", json={"user_ids": [uid]})
    prompt = next(p for p in prompts if "AVAILABLE ingredients" in p)
    assert "tree nuts" in prompt
    assert "no animal products" in prompt
    assert "Korean" in prompt


def test_feast_is_refused_when_it_clashes_with_a_guest(client, make_user, monkeypatch):
    host = make_user("feast_host@example.com", "Ann")
    guest = make_user("feast_guest@example.com", "Ben")
    set_profile(client, guest, avoid_allergens=["Peanuts"])
    add_item(client, host, "noodles", days=30)
    stub_recipes(monkeypatch, [R("Satay Noodles", uses=["noodles"], missing=["peanuts"])])

    # Suggested for the host alone, so nothing filtered it for the guest.
    recipe = client.post("/recipes/suggest", json={"user_ids": [host]}).json()["recipes"][0]

    resp = client.post("/feasts", json={
        "name": "Noodle night", "host_id": host,
        "recipe": recipe, "attendee_ids": [guest],
    })
    assert resp.status_code == 422, resp.text
    assert "peanut" in resp.json()["detail"]

    # Without that guest, the same recipe is fine.
    ok = client.post("/feasts", json={
        "name": "Noodle night", "host_id": host, "recipe": recipe, "attendee_ids": [],
    })
    assert ok.status_code == 201, ok.text
