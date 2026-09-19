from datetime import date, timedelta

TODAY = date.today()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def add_item(client, user_id, name, days=None, **extra):
    body = {"name": name, **extra}
    if days is not None:
        body["expires_on"] = iso(days)
    resp = client.post(f"/users/{user_id}/grocery-items", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_duplicate_email_conflicts(client, make_user):
    make_user("a@example.com")
    assert client.post("/users", json={"email": "a@example.com", "name": "X"}).status_code == 409


def test_item_crud(client, make_user):
    uid = make_user("crud@example.com")
    item = add_item(client, uid, "Milk", days=2, quantity=1, unit="L", category="dairy")
    assert item["owner_id"] == uid

    patched = client.patch(f"/grocery-items/{item['id']}", json={"consumed": True})
    assert patched.json()["consumed"] is True

    assert client.delete(f"/grocery-items/{item['id']}").status_code == 204
    assert client.get(f"/grocery-items/{item['id']}").status_code == 404


def test_expiring_window_and_ordering(client, make_user):
    uid = make_user("exp@example.com")
    add_item(client, uid, "Yogurt", days=1)
    add_item(client, uid, "Bread", days=10)
    add_item(client, uid, "Spinach", days=-1)
    add_item(client, uid, "Salt")  # no expiry -> never listed

    names = [i["name"] for i in client.get(f"/users/{uid}/grocery-items/expiring?within_days=3").json()]
    assert names == ["Spinach", "Yogurt"]

    body = client.get(f"/users/{uid}/grocery-items/expiring?within_days=3&include_expired=false").json()
    assert [i["name"] for i in body] == ["Yogurt"]
    assert body[0]["days_until_expiry"] == 1
    assert body[0]["expired"] is False

    wide = client.get(f"/users/{uid}/grocery-items/expiring?within_days=30").json()
    assert [i["name"] for i in wide] == ["Spinach", "Yogurt", "Bread"]


def test_expiring_excludes_consumed_by_default(client, make_user):
    uid = make_user("consumed@example.com")
    item = add_item(client, uid, "Cream", days=1)
    client.patch(f"/grocery-items/{item['id']}", json={"consumed": True})

    assert client.get(f"/users/{uid}/grocery-items/expiring").json() == []
    incl = client.get(f"/users/{uid}/grocery-items/expiring?include_consumed=true").json()
    assert [i["name"] for i in incl] == ["Cream"]


def test_expiring_includes_only_accepted_friends(client, make_user):
    me = make_user("me@example.com", "Me")
    pal = make_user("pal@example.com", "Pal")
    stranger = make_user("stranger@example.com", "Stranger")

    add_item(client, me, "Eggs", days=2)
    add_item(client, pal, "Tofu", days=1)
    add_item(client, stranger, "Kale", days=1)

    client.post(f"/users/{me}/friends", json={"friend_id": pal})

    # Still pending -> friend's items stay hidden.
    pending = client.get(f"/users/{me}/grocery-items/expiring?within_days=3&include_friends=true").json()
    assert [i["name"] for i in pending] == ["Eggs"]

    assert client.patch(f"/users/{pal}/friends/{me}", json={"status": "accepted"}).status_code == 200
    accepted = client.get(f"/users/{me}/grocery-items/expiring?within_days=3&include_friends=true").json()
    assert [i["name"] for i in accepted] == ["Tofu", "Eggs"]
    assert {i["owner_name"] for i in accepted} == {"Me", "Pal"}


def test_global_expiring_endpoint(client, make_user):
    a = make_user("g1@example.com")
    b = make_user("g2@example.com")
    add_item(client, a, "Ham", days=0)
    add_item(client, b, "Cheese", days=2)

    names = [i["name"] for i in client.get("/grocery-items/expiring?within_days=3").json()]
    assert names == ["Ham", "Cheese"]


def test_friend_rules(client, make_user):
    a = make_user("f1@example.com")
    b = make_user("f2@example.com")

    assert client.post(f"/users/{a}/friends", json={"friend_id": a}).status_code == 400
    assert client.post(f"/users/{a}/friends", json={"friend_id": 9999}).status_code == 404
    assert client.post(f"/users/{a}/friends", json={"friend_id": b}).status_code == 201
    # Reverse direction is the same pair.
    assert client.post(f"/users/{b}/friends", json={"friend_id": a}).status_code == 409

    listed = client.get(f"/users/{b}/friends").json()
    assert len(listed) == 1 and listed[0]["friend"]["email"] == "f1@example.com"

    assert client.delete(f"/users/{a}/friends/{b}").status_code == 204
    assert client.get(f"/users/{a}/friends").json() == []


def test_deleting_user_removes_their_items(client, make_user):
    uid = make_user("cascade@example.com")
    add_item(client, uid, "Butter", days=1)
    assert client.delete(f"/users/{uid}").status_code == 204
    assert client.get("/grocery-items/expiring?within_days=5").json() == []


def test_root_redirects_to_docs(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs"


def test_docs_are_served(client):
    assert client.get("/docs").status_code == 200
    assert "Grocery Tracker" in client.get("/openapi.json").text
