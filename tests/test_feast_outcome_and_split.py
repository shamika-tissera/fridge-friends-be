"""Solo vs friends rescues, feast outcomes, and avatar links."""

from datetime import date, timedelta

from tests.test_recipes import R, add_item, stub_recipes

TODAY = date.today()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def add(client, uid, name, days, price=None, bought=0):
    r = client.post(f"/users/{uid}/grocery-items", json={
        "name": name, "price": price,
        "expires_on": iso(days), "purchased_on": iso(-bought),
    })
    assert r.status_code == 201, r.text
    return r.json()


def make_feast(client, monkeypatch, host, guests=(), uses=("rice",)):
    """A feast built the way the app builds one: suggest, then post it back.

    The suggestion pools everyone's pantry, and `uses` only keeps ingredients
    somebody actually has — so the groceries have to exist *before* this runs,
    which is also the real order of events.
    """
    stub_recipes(monkeypatch, [R("Group Dinner", uses=list(uses))])
    recipe = client.post(
        "/recipes/suggest", json={"user_ids": [host, *guests]}
    ).json()["recipes"][0]
    resp = client.post("/feasts", json={
        "name": "Dinner", "host_id": host, "recipe": recipe,
        "attendee_ids": list(guests),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 1. solo vs friends ----------
def test_solo_rescue_is_the_default(client, make_user):
    uid = make_user("solo@example.com")
    item = add(client, uid, "Spinach", days=1, price=4.00)
    body = client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "used"}).json()
    assert body["rescued"] is True
    assert body["rescued_feast_id"] is None

    stats = client.get(f"/users/{uid}/stats").json()
    assert stats["rescued"] == 4.00
    assert stats["rescued_solo"] == 4.00 and stats["rescued_friends"] == 0.0
    assert stats["items_rescued_solo"] == 1 and stats["items_rescued_friends"] == 0


def test_rescue_attributed_to_a_feast_counts_as_friends(client, make_user, monkeypatch):
    host = make_user("fhost@example.com", "Ann")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)

    item = add(client, host, "Spinach", days=1, price=6.00)
    body = client.post(f"/grocery-items/{item['id']}/resolve",
                       json={"outcome": "used", "feast_id": feast["id"]}).json()
    assert body["rescued"] is True and body["rescued_feast_id"] == feast["id"]

    stats = client.get(f"/users/{host}/stats").json()
    assert stats["rescued"] == 6.00
    assert stats["rescued_friends"] == 6.00 and stats["rescued_solo"] == 0.0
    assert stats["items_rescued_friends"] == 1 and stats["items_rescued_solo"] == 0


def test_the_split_always_adds_up(client, make_user, monkeypatch):
    host = make_user("adds@example.com", "Ann")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)

    for name, price, with_feast in [
        ("Spinach", 3.00, True), ("Milk", 2.50, False),
        ("Salmon fillet", 8.00, True), ("Eggplant", 1.50, False),
    ]:
        item = add(client, host, name, days=1, price=price)
        body = {"outcome": "used"}
        if with_feast:
            body["feast_id"] = feast["id"]
        client.post(f"/grocery-items/{item['id']}/resolve", json=body)

    stats = client.get(f"/users/{host}/stats").json()
    assert stats["rescued_solo"] + stats["rescued_friends"] == stats["rescued"] == 15.00
    assert stats["items_rescued_solo"] + stats["items_rescued_friends"] == stats["items_rescued"] == 4
    for bucket in stats["buckets"]:
        assert round(bucket["rescued_solo"] + bucket["rescued_friends"], 2) == bucket["rescued"]
        assert bucket["items_rescued_solo"] + bucket["items_rescued_friends"] == bucket["items_rescued"]


def test_existing_fields_are_unchanged(client, make_user):
    """The mobile app reads these today; new fields must only be additions."""
    uid = make_user("additive@example.com")
    item = add(client, uid, "Spinach", days=1, price=4.00, bought=0)
    client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "used"})

    stats = client.get(f"/users/{uid}/stats").json()
    for field in ("user_id", "period", "buckets", "spent", "wasted", "rescued",
                  "waste_percent_first", "waste_percent_last", "summary",
                  "priced_items", "unpriced_items"):
        assert field in stats, field
    for field in ("label", "starts_on", "ends_on", "spent", "wasted",
                  "spent_and_used", "rescued", "items_wasted", "items_rescued"):
        assert field in stats["buckets"][0], field
    assert stats["rescued"] == 4.00        # still the combined total


def test_a_non_rescue_at_a_feast_is_not_counted_as_one(client, make_user, monkeypatch):
    """Eating a fresh item at a feast is not a rescue, wherever it happened."""
    host = make_user("fresh@example.com", "Ann")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)

    item = add(client, host, "Basmati rice", days=200, price=9.00)
    body = client.post(f"/grocery-items/{item['id']}/resolve",
                       json={"outcome": "used", "feast_id": feast["id"]}).json()
    assert body["rescued"] is False and body["rescued_feast_id"] == feast["id"]

    stats = client.get(f"/users/{host}/stats").json()
    assert stats["rescued"] == 0.0 and stats["rescued_friends"] == 0.0


def test_feast_id_must_exist_and_include_the_owner(client, make_user, monkeypatch):
    host = make_user("owner@example.com", "Ann")
    outsider = make_user("outsider@example.com", "Ben")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)

    ghost = add(client, host, "Spinach", days=1, price=3.00)
    assert client.post(f"/grocery-items/{ghost['id']}/resolve",
                       json={"outcome": "used", "feast_id": 9999}).status_code == 404

    theirs = add(client, outsider, "Spinach", days=1, price=3.00)
    resp = client.post(f"/grocery-items/{theirs['id']}/resolve",
                       json={"outcome": "used", "feast_id": feast["id"]})
    assert resp.status_code == 422
    assert "not going to that feast" in resp.json()["detail"]


# ---------- 2. feast outcome ----------
def test_a_new_feast_is_planned(client, make_user, monkeypatch):
    host = make_user("planned@example.com", "Ann")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)
    assert feast["status"] == "planned" and feast["outcome_at"] is None


def test_host_marks_a_feast_rescued_and_items_follow(client, make_user, monkeypatch):
    host = make_user("rescuer@example.com", "Ann")
    guest = make_user("guest@example.com", "Ben")
    mine = add(client, host, "Spinach", days=1, price=4.00)
    theirs = add(client, guest, "Spinach", days=0, price=2.50)
    feast = make_feast(client, monkeypatch, host, guests=[guest], uses=["spinach"])

    resp = client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": host, "status": "rescued"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feast"]["status"] == "rescued"
    assert body["feast"]["outcome_at"] is not None
    assert body["items_used"] == 2 and body["items_rescued"] == 2
    assert sorted(body["item_ids"]) == sorted([mine["id"], theirs["id"]])
    assert sorted(body["owners"]) == ["Ann", "Ben"]

    # Both now count as friends rescues, on each owner's own stats.
    assert client.get(f"/users/{host}/stats").json()["rescued_friends"] == 4.00
    assert client.get(f"/users/{guest}/stats").json()["rescued_friends"] == 2.50
    # And they are off the shelf.
    assert client.get(f"/users/{host}/shelf").json()["items"] == []


def test_outcome_response_never_exposes_prices(client, make_user, monkeypatch):
    host = make_user("priceless@example.com", "Ann")
    guest = make_user("guestprice@example.com", "Ben")
    add(client, guest, "Spinach", days=1, price=99.99)
    feast = make_feast(client, monkeypatch, host, guests=[guest], uses=["spinach"])

    body = client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": host, "status": "rescued"}).json()

    def keys(node):
        """Every key name anywhere in the response."""
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    assert not [k for k in keys(body) if "price" in k.lower()]
    assert "99.99" not in str(body)


def test_explicit_item_ids_win_over_the_recipe(client, make_user, monkeypatch):
    host = make_user("explicit@example.com", "Ann")
    chosen = add(client, host, "Eggplant", days=1, price=2.00)   # not in the recipe
    add(client, host, "Spinach", days=1, price=5.00)             # in the recipe
    feast = make_feast(client, monkeypatch, host, uses=["spinach"])

    body = client.post(f"/feasts/{feast['id']}/outcome", json={
        "host_id": host, "status": "rescued", "item_ids": [chosen["id"]],
    }).json()
    assert body["item_ids"] == [chosen["id"]]
    # The spinach was never claimed, so it is still on the shelf.
    assert [i["name"] for i in client.get(f"/users/{host}/shelf").json()["items"]] == ["Spinach"]


def test_only_the_host_can_record_an_outcome(client, make_user, monkeypatch):
    host = make_user("thehost@example.com", "Ann")
    guest = make_user("theguest@example.com", "Ben")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host, guests=[guest])

    resp = client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": guest, "status": "rescued"})
    assert resp.status_code == 403
    assert client.get(f"/feasts/{feast['id']}").json()["status"] == "planned"


def test_failed_feast_leaves_the_groceries_alone(client, make_user, monkeypatch):
    host = make_user("failed@example.com", "Ann")
    add(client, host, "Spinach", days=1, price=4.00)
    feast = make_feast(client, monkeypatch, host, uses=["spinach"])

    body = client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": host, "status": "failed"}).json()
    assert body["feast"]["status"] == "failed"
    assert body["items_used"] == 0
    assert len(client.get(f"/users/{host}/shelf").json()["items"]) == 1
    assert client.get(f"/users/{host}/stats").json()["rescued"] == 0.0


def test_a_feast_cannot_go_back_to_planned(client, make_user, monkeypatch):
    host = make_user("noback@example.com", "Ann")
    add_item(client, host, "rice", days=30)
    feast = make_feast(client, monkeypatch, host)
    assert client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": host, "status": "planned"}).status_code == 422


def test_non_attendee_groceries_are_not_swept_up(client, make_user, monkeypatch):
    host = make_user("sweep@example.com", "Ann")
    stranger = make_user("stranger2@example.com", "Cass")
    add(client, host, "Spinach", days=1, price=4.00)
    add(client, stranger, "Spinach", days=1, price=4.00)   # same name, not invited
    feast = make_feast(client, monkeypatch, host, uses=["spinach"])

    body = client.post(f"/feasts/{feast['id']}/outcome",
                       json={"host_id": host, "status": "rescued"}).json()
    assert body["items_used"] == 1 and body["owners"] == ["Ann"]
    assert len(client.get(f"/users/{stranger}/shelf").json()["items"]) == 1


def test_recording_the_outcome_twice_does_not_double_count(client, make_user, monkeypatch):
    host = make_user("twice@example.com", "Ann")
    add(client, host, "Spinach", days=1, price=4.00)
    feast = make_feast(client, monkeypatch, host, uses=["spinach"])

    first = client.post(f"/feasts/{feast['id']}/outcome",
                        json={"host_id": host, "status": "rescued"}).json()
    second = client.post(f"/feasts/{feast['id']}/outcome",
                         json={"host_id": host, "status": "rescued"}).json()
    assert first["items_used"] == 1 and second["items_used"] == 0

    stats = client.get(f"/users/{host}/stats").json()
    assert stats["rescued_friends"] == 4.00 and stats["items_rescued_friends"] == 1


# ---------- 3. avatar ----------
def test_avatar_url_round_trip(client, make_user):
    uid = make_user("avatar@example.com")
    assert client.get(f"/users/{uid}").json()["avatar_url"] is None

    url = "https://example.com/photos/me.jpg"
    assert client.patch(f"/users/{uid}", json={"avatar_url": url}).json()["avatar_url"] == url
    assert client.get(f"/users/{uid}").json()["avatar_url"] == url

    # Clearing it falls back to the buddy.
    assert client.patch(f"/users/{uid}", json={"avatar_url": ""}).json()["avatar_url"] is None


def test_avatar_url_must_be_http(client, make_user):
    uid = make_user("badavatar@example.com")
    for bad in ["javascript:alert(1)", "data:image/png;base64,AAAA", "/local/path.png"]:
        assert client.patch(f"/users/{uid}", json={"avatar_url": bad}).status_code == 422, bad


def test_avatar_appears_wherever_a_user_does(client, make_user):
    uid = make_user("everywhere@example.com", username="ava.tar")
    url = "https://example.com/a.png"
    client.patch(f"/users/{uid}", json={"avatar_url": url})

    assert client.get("/users?username=ava.tar").json()[0]["avatar_url"] == url
    login = client.post("/auth/login", json={"username": "ava.tar", "password": "shelf-life-8"})
    assert login.json()["avatar_url"] == url
