"""The screens the mockups describe: join, get-to-know-you, shelf, add sheet."""

from datetime import date, timedelta

TODAY = date.today()


def signup(client, **overrides):
    body = {"username": "pantry.pal", "password": "cold-storage-9", "buddy": "carl"}
    body.update(overrides)
    return client.post("/auth/signup", json=body)


# ---------- join screen ----------
def test_signup_stores_handle_buddy_and_password(client):
    resp = signup(client)
    assert resp.status_code == 201, resp.text
    user = resp.json()
    assert user["username"] == "pantry.pal"
    assert user["buddy"] == "carl"
    assert user["email"] is None          # the screen never asks for one
    assert user["name"] == "pantry.pal"   # stands in until a profile name is set
    assert "password" not in user and "password_hash" not in user


def test_username_is_case_insensitive_and_validated(client):
    assert signup(client, username="Pantry.PAL").json()["username"] == "pantry.pal"
    assert signup(client, username="no spaces").status_code == 422
    assert signup(client, username="ab").status_code == 422


def test_short_password_is_rejected(client):
    assert signup(client, password="short").status_code == 422


def test_login_round_trip(client):
    created = signup(client).json()

    ok = client.post("/auth/login", json={"username": "Pantry.Pal", "password": "cold-storage-9"})
    assert ok.status_code == 200
    assert ok.json()["id"] == created["id"]

    assert client.post(
        "/auth/login", json={"username": "pantry.pal", "password": "wrong"}
    ).status_code == 401
    # An unknown handle fails the same way, so accounts cannot be enumerated.
    nobody = client.post("/auth/login", json={"username": "ghost", "password": "whatever"})
    assert nobody.status_code == 401
    assert nobody.json()["detail"] == client.post(
        "/auth/login", json={"username": "pantry.pal", "password": "wrong"}
    ).json()["detail"]


def test_password_change(client):
    uid = signup(client).json()["id"]
    body = {"current_password": "cold-storage-9", "new_password": "warm-storage-9"}

    assert client.put(f"/users/{uid}/password", json={**body, "current_password": "nope"}).status_code == 403
    assert client.put(f"/users/{uid}/password", json=body).status_code == 204
    assert client.post(
        "/auth/login", json={"username": "pantry.pal", "password": "warm-storage-9"}
    ).status_code == 200


def test_lookup_by_username_for_adding_friends(client):
    signup(client)
    assert [u["username"] for u in client.get("/users?username=PANTRY.PAL").json()] == ["pantry.pal"]
    assert client.get("/users?username=nobody").json() == []


# ---------- "Let's get to know you" ----------
def test_taste_profile_accepts_the_labels_the_ui_shows(client):
    uid = signup(client).json()["id"]
    resp = client.put(
        f"/users/{uid}/taste-profile",
        json={
            "diets": ["Pescatarian", "Gluten-free"],
            "avoid_allergens": ["Peanuts", "Tree nuts"],
            "favorite_cuisines": ["italian", "Mexican", "Mexican"],
        },
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["diets"] == ["pescatarian", "gluten_free"]
    assert saved["avoid_allergens"] == ["peanuts", "tree_nuts"]
    assert saved["favorite_cuisines"] == ["Italian", "Mexican"]   # de-duplicated

    assert client.get(f"/users/{uid}/taste-profile").json() == saved
    assert client.get(f"/users/{uid}").json()["diets"] == ["pescatarian", "gluten_free"]


def test_taste_profile_lists_replace_rather_than_merge(client):
    uid = signup(client).json()["id"]
    client.put(f"/users/{uid}/taste-profile", json={"diets": ["vegan"], "avoid_allergens": ["sesame"]})
    # Only diets is sent, so allergens are left as they were.
    resp = client.put(f"/users/{uid}/taste-profile", json={"diets": []})
    assert resp.json()["diets"] == []
    assert resp.json()["avoid_allergens"] == ["sesame"]


def test_unknown_diet_is_rejected(client):
    uid = signup(client).json()["id"]
    assert client.put(f"/users/{uid}/taste-profile", json={"diets": ["carnivore"]}).status_code == 422


# ---------- add sheet ----------
def test_freshness_preview_needs_no_account_or_database(client):
    body = client.get("/grocery-items/freshness-preview?name=Spinach").json()
    assert body["spoilage_profile"] == "gradual"
    assert body["shelf_life_days"] == 7
    assert body["summary"] == "Gradual · ~7 days"
    assert body["shelf_buddy"] == "leaf"
    assert body["purchased_on"] == TODAY.isoformat()
    assert body["expires_on"] == (TODAY + timedelta(days=7)).isoformat()


def test_preview_matches_what_saving_stores(client):
    uid = client.post(
        "/users", json={"username": "shelf.tester", "password": "cold-storage-9"}
    ).json()["id"]
    preview = client.get("/grocery-items/freshness-preview?name=Spinach").json()
    saved = client.post(f"/users/{uid}/grocery-items", json={"name": "Spinach", "price": 3.49}).json()

    assert saved["shelf_life_days"] == preview["shelf_life_days"]
    assert saved["spoilage_profile"] == preview["spoilage_profile"]
    assert saved["shelf_buddy"] == preview["shelf_buddy"]
    assert saved["expires_on"] == preview["expires_on"]
    assert saved["purchased_on"] == TODAY.isoformat()
    assert saved["price"] == 3.49


def test_backdated_purchase_starts_the_timer_then(client, make_user):
    uid = make_user("backdate@example.com")
    item = client.post(
        f"/users/{uid}/grocery-items",
        json={"name": "Milk", "purchased_on": (TODAY - timedelta(days=5)).isoformat()},
    ).json()
    assert item["expires_on"] == (TODAY + timedelta(days=2)).isoformat()
    assert item["days_until_expiry"] == 2
    assert item["freshness"] == "use_now"


def test_explicit_expiry_wins_over_the_catalogue(client, make_user):
    uid = make_user("packet@example.com")
    item = client.post(
        f"/users/{uid}/grocery-items",
        json={"name": "Milk", "expires_on": (TODAY + timedelta(days=30)).isoformat()},
    ).json()
    assert item["expires_on"] == (TODAY + timedelta(days=30)).isoformat()
    assert item["freshness"] == "fresh"


def test_moving_the_purchase_date_moves_the_expiry(client, make_user):
    uid = make_user("moved@example.com")
    item = client.post(f"/users/{uid}/grocery-items", json={"name": "Spinach"}).json()
    patched = client.patch(
        f"/grocery-items/{item['id']}",
        json={"purchased_on": (TODAY - timedelta(days=6)).isoformat()},
    ).json()
    assert patched["expires_on"] == (TODAY + timedelta(days=1)).isoformat()


# ---------- the shelf ----------
def test_shelf_bands_and_rescue_counts(client, make_user):
    uid = make_user("shelf@example.com")

    def add(name, days, price=None):
        return client.post(
            f"/users/{uid}/grocery-items",
            json={"name": name, "price": price,
                  "expires_on": (TODAY + timedelta(days=days)).isoformat()},
        ).json()

    add("Salmon", 12)
    add("Lemon", 21)
    add("Eggplant", 2, price=1.50)
    add("Milk", 3)
    add("Spinach", 2, price=3.49)
    add("Canned tomatoes", 730)
    add("Old yoghurt", -2, price=2.00)

    shelf = client.get(f"/users/{uid}/shelf").json()
    bands = {i["name"]: i["freshness"] for i in shelf["items"]}
    assert bands["Salmon"] == "fresh"
    assert bands["Milk"] == "use_soon"
    assert bands["Eggplant"] == "use_now"
    assert bands["Spinach"] == "use_now"
    assert bands["Old yoghurt"] == "expired"

    labels = {i["name"]: i["freshness_label"] for i in shelf["items"]}
    assert labels["Salmon"] == "12 days"
    assert labels["Lemon"] == "3 weeks"
    assert labels["Canned tomatoes"] == "2 years"

    assert shelf["fresh"] == 3
    assert shelf["use_soon"] == 1
    assert shelf["use_now"] == 2
    assert shelf["expired"] == 1
    assert shelf["needs_rescue"] == 3
    assert shelf["rescue_value"] == 6.99


def test_shelf_hides_consumed_items(client, make_user):
    uid = make_user("eaten@example.com")
    item = client.post(f"/users/{uid}/grocery-items", json={"name": "Spinach"}).json()
    client.patch(f"/grocery-items/{item['id']}", json={"consumed": True})

    assert client.get(f"/users/{uid}/shelf").json()["items"] == []
    assert len(client.get(f"/users/{uid}/shelf?include_consumed=true").json()["items"]) == 1


def test_item_without_an_expiry_has_no_band(client, make_user):
    uid = make_user("nodate@example.com")
    item = client.post(
        f"/users/{uid}/grocery-items", json={"name": "Salt", "expires_on": None}
    ).json()
    assert item["expires_on"] is None
    assert item["freshness"] == "unknown"
    assert item["freshness_label"] == "no date"


def test_every_catalogue_buddy_has_a_declared_key(client):
    """The front end draws one sprite per key, so the catalogue cannot invent new ones."""
    from app.freshness import BUDDY_KEYS, CATALOGUE, DEFAULT_ENTRY

    used = {entry.buddy for entry in CATALOGUE.values()} | {DEFAULT_ENTRY.buddy}
    assert used <= set(BUDDY_KEYS)
